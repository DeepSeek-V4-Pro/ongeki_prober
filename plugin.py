"""オンゲキ 谱面查询插件 — 获取曲目信息、谱面定数、Note 配置等"""

import asyncio
import base64
from collections import deque
import html as _html
import json
import logging
import random
import re
import time
from pathlib import Path
from typing import Any, Optional

import aiohttp

from maibot_sdk import Command, Field, MaiBotPlugin, PluginConfigBase

logger = logging.getLogger(__name__)

DIFFICULTY_DISPLAY: dict[str, str] = {
    "basic": "BASIC",
    "advanced": "ADVANCED",
    "expert": "EXPERT",
    "master": "MASTER",
    "lunatic": "LUNATIC",
}

TYPE_DISPLAY: dict[str, str] = {
    "std": "STD",
    "lun": "LUN",
}

DIFF_COLORS: dict[str, str] = {
    "basic": "#16ff47",
    "advanced": "#ffba00",
    "expert": "#fa0667",
    "master": "#a810ff",
    "lunatic": "#dee600",
}

_BASE_HTML_STYLE = (
    "*{margin:0;padding:0;box-sizing:border-box}"
    "body{background:linear-gradient(180deg,#1a1a2e 0%,#222240 100%);"
    "color:#d0d0dc;font-family:'Segoe UI','Microsoft YaHei',sans-serif;"
    "padding:28px 32px}"
)


class AliasStore:
    def __init__(self, filepath: str) -> None:
        self._filepath = Path(filepath)
        self._lock = asyncio.Lock()
        self._data: dict[str, list[str]] = {}
        self._index: dict[str, str] = {}

    async def load(self) -> None:
        try:
            if self._filepath.exists():
                content = self._filepath.read_text(encoding="utf-8")
                if content.strip():
                    self._data = json.loads(content)
                    self._rebuild_index()
        except (json.JSONDecodeError, OSError):
            self._data = {}
            self._index = {}

    def _rebuild_index(self) -> None:
        self._index.clear()
        for sid, aliases in self._data.items():
            for a in aliases:
                self._index[a.lower()] = sid

    async def _save(self) -> None:
        try:
            self._filepath.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._filepath.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            tmp.replace(self._filepath)
        except OSError:
            pass

    async def add(self, song_id: str, alias: str) -> tuple[bool, str]:
        async with self._lock:
            normalized = alias.strip()
            if not normalized or len(normalized) > 30:
                return False, "别称无效（长度 1-30）"
            key = normalized.lower()
            if key in self._index and self._index[key] != str(song_id):
                return False, f"别称「{normalized}」已被歌曲 {self._index[key]} 使用"
            sid = str(song_id)
            if sid not in self._data:
                self._data[sid] = []
            if normalized not in self._data[sid]:
                self._data[sid].append(normalized)
                self._index[key] = sid
                await self._save()
                return True, "添加成功"
            return True, "别称已存在"

    async def delete(self, song_id: str, alias: str) -> tuple[bool, str]:
        async with self._lock:
            sid = str(song_id)
            if sid not in self._data:
                return False, f"歌曲 {song_id} 没有别称"
            normalized = alias.strip()
            if normalized not in self._data[sid]:
                return False, f"别称「{normalized}」不存在"
            self._data[sid].remove(normalized)
            if not self._data[sid]:
                del self._data[sid]
            key = normalized.lower()
            if self._index.get(key) == sid:
                del self._index[key]
            await self._save()
            return True, "删除成功"

    async def list_aliases(self, song_id: str) -> list[str]:
        async with self._lock:
            return list(self._data.get(str(song_id), []))

    async def search(self, keyword: str) -> list[str]:
        async with self._lock:
            key = keyword.lower()
            results: list[str] = []
            if key in self._index:
                results.append(self._index[key])
            for alias_lower, sid in self._index.items():
                if key in alias_lower and sid not in results:
                    results.append(sid)
            return results


class PluginSectionConfig(PluginConfigBase):
    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0
    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(default="1.0.0", description="配置版本")


class ServerConfig(PluginConfigBase):
    __ui_label__ = "服务器"
    __ui_icon__ = "server"
    __ui_order__ = 1
    data_source_url: str = Field(
        default="https://dp4p6x0xfi5o9.cloudfront.net/ongeki",
        description="数据源 URL",
    )
    request_timeout: int = Field(default=30, description="请求超时时间(秒)")
    data_cache_ttl: int = Field(default=300, description="数据缓存时间(秒)")


class ImageConfig(PluginConfigBase):
    __ui_label__ = "图片模式"
    __ui_icon__ = "image"
    __ui_order__ = 2
    enabled: bool = Field(default=False, description="启用图片渲染模式（需安装 playwright）")


class OngekiProberConfig(PluginConfigBase):
    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    image: ImageConfig = Field(default_factory=ImageConfig)


class OngekiProberPlugin(MaiBotPlugin):
    config_model = OngekiProberConfig

    async def on_load(self) -> None:
        self._http_session: Optional[aiohttp.ClientSession] = None
        self._client_lock = asyncio.Lock()
        self._songs_cache: Optional[list[dict]] = None
        self._cache_time: float = 0
        self._playwright_inst = None
        self._browser = None
        self._browser_lock = asyncio.Lock()
        self._recommended_ids: deque[str] = deque(maxlen=200)
        data_dir = self.ctx.paths.data_dir
        self._aliases = AliasStore(str(data_dir / "aliases.json"))
        await self._aliases.load()

    async def on_unload(self) -> None:
        async with self._browser_lock:
            if self._browser:
                try:
                    await self._browser.close()
                except Exception:
                    logger.debug("关闭 browser 时出错", exc_info=True)
                self._browser = None
            if self._playwright_inst:
                try:
                    await self._playwright_inst.stop()
                except Exception:
                    logger.debug("关闭 playwright 时出错", exc_info=True)
                self._playwright_inst = None
        if self._http_session:
            await self._http_session.close()
            self._http_session = None

    async def on_config_update(
        self, scope: str, config_data: dict[str, object], version: str
    ) -> None:
        del config_data, version
        if scope == "self":
            async with self._client_lock:
                if self._http_session:
                    await self._http_session.close()
                    self._http_session = None
            self._songs_cache = None
            self._cache_time = 0

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._http_session is None:
            async with self._client_lock:
                if self._http_session is None:
                    self._http_session = aiohttp.ClientSession()
        return self._http_session

    async def _fetch_data(self) -> Optional[dict]:
        now = time.time()
        ttl = self.config.server.data_cache_ttl
        if self._songs_cache is not None and (now - self._cache_time) < ttl:
            return {"songs": self._songs_cache}

        url = f"{self.config.server.data_source_url.rstrip('/')}/data.json"
        timeout = aiohttp.ClientTimeout(total=self.config.server.request_timeout)

        try:
            session = await self._get_session()
            async with session.get(url, timeout=timeout) as resp:
                if resp.status != 200:
                    logger.warning("数据源返回 %s", resp.status)
                    if self._songs_cache is not None:
                        return {"songs": self._songs_cache}
                    return None
                data = await resp.json()
        except (asyncio.TimeoutError, aiohttp.ClientError, Exception) as e:
            logger.warning("获取数据失败: %s", e)
            if self._songs_cache is not None:
                return {"songs": self._songs_cache}
            return None

        songs = data.get("songs", [])
        if not isinstance(songs, list):
            logger.warning("数据格式异常: songs 不是列表")
            if self._songs_cache is not None:
                return {"songs": self._songs_cache}
            return None

        self._songs_cache = songs
        self._cache_time = now
        data["songs"] = songs
        return data

    async def _match_songs(self, keyword: str) -> list[dict]:
        data = await self._fetch_data()
        if not data:
            return []
        songs = data.get("songs", [])
        if not songs:
            return []

        kw = keyword.lower().strip()
        results: list[dict] = []
        seen_ids: set[str] = set()

        alias_sids = set(await self._aliases.search(keyword))

        for song in songs:
            if not isinstance(song, dict):
                continue
            sid = str(song.get("songId", "") or "")
            if not sid or sid in seen_ids:
                continue
            title = str(song.get("title", "") or "").lower()
            artist = str(song.get("artist", "") or "").lower()

            if kw in title or kw in artist or kw == sid.lower() or sid in alias_sids:
                seen_ids.add(sid)
                results.append(song)

        if not results:
            words = [
                w.strip(" '\"/-()[]{}")
                for w in kw.split()
                if len(w.strip(" '\"/-()[]{}")) > 1
            ]
            for song in songs:
                if not isinstance(song, dict):
                    continue
                sid = str(song.get("songId", "") or "")
                if not sid or sid in seen_ids:
                    continue
                title = str(song.get("title", "") or "").lower()
                artist = str(song.get("artist", "") or "").lower()
                if any(w in title or w in artist for w in words):
                    seen_ids.add(sid)
                    results.append(song)

        return results

    async def _ensure_browser(self):
        if self._browser is None:
            async with self._browser_lock:
                if self._browser is None:
                    try:
                        from playwright.async_api import async_playwright
                    except ImportError:
                        raise RuntimeError(
                            "playwright 未安装，请执行: pip install playwright && python -m playwright install chromium"
                        )
                    self._playwright_inst = await async_playwright().start()
                    self._browser = await self._playwright_inst.chromium.launch(headless=True)
        return self._browser

    async def _render_html_to_png(
        self, html: str, width: int = 680, height: int = 500, wait_for_images: bool = False, image_timeout: int = 15000,
    ) -> str:
        browser = await self._ensure_browser()
        page = await browser.new_page(viewport={"width": width, "height": height})
        try:
            await page.set_content(html)
            await page.wait_for_load_state("domcontentloaded")
            if wait_for_images:
                try:
                    await page.wait_for_function(
                        "() => [...document.querySelectorAll('img')].every(i => i.complete)",
                        timeout=image_timeout,
                    )
                except Exception:
                    logger.debug("等待曲绘加载超时，继续渲染")
            await page.wait_for_timeout(500)
            screenshot = await page.screenshot(full_page=True, type="png")
        finally:
            await page.close()
        return base64.b64encode(screenshot).decode()

    async def _download_cover_base64(self, song: dict) -> Optional[str]:
        image_name = song.get("imageName")
        if not image_name:
            return None
        url = f"{self.config.server.data_source_url.rstrip('/')}/img/cover/{image_name}"
        timeout = aiohttp.ClientTimeout(total=10)
        session = await self._get_session()
        for attempt in range(3):
            try:
                async with session.get(url, timeout=timeout) as resp:
                    if resp.status == 200:
                        data = await resp.read()
                        return base64.b64encode(data).decode()
            except Exception:
                if attempt < 2:
                    await asyncio.sleep(0.5)
        logger.debug("曲绘下载失败: %s", image_name)
        return None

    async def _render_song_detail_image(self, song: dict, cover_b64: str) -> str:
        title = _html.escape(str(song.get("title", "") or "?"))
        artist = _html.escape(str(song.get("artist", "") or "?"))
        bpm = song.get("bpm", "?")
        category = _html.escape(str(song.get("category", "") or "?"))
        version = _html.escape(str(song.get("version", "") or "?"))
        release = str(song.get("releaseDate", "") or "?")
        sid = _html.escape(str(song.get("songId", "") or "?"))

        sheets = song.get("sheets", [])
        diff_rows_html = ""
        for sheet in sheets:
            if not isinstance(sheet, dict):
                continue
            tp = sheet.get("type", "std")
            diff = sheet.get("difficulty", "?")
            level = str(sheet.get("level", "") or "?")
            internal = sheet.get("internalLevel")
            internal_str = str(internal) if internal else "-"
            designer = _html.escape(str(sheet.get("noteDesigner", "") or "-"))
            notes = sheet.get("noteCounts", {}) or {}
            total = notes.get("total", "?")
            bell = notes.get("bell", "?")
            color = DIFF_COLORS.get(diff, "#ffffff")

            tp_str = TYPE_DISPLAY.get(tp, tp.upper())
            diff_str = DIFFICULTY_DISPLAY.get(diff, diff.upper())

            diff_rows_html += (
                f'<div class="diff-row">'
                f'<span class="diff-type">{tp_str}</span>'
                f'<span class="diff-name" style="color:{color}">{diff_str}</span>'
                f'<span class="diff-lvl">Lv.{_html.escape(level)}</span>'
                f'<span class="diff-ilvl">(定数 {_html.escape(internal_str)})</span>'
                f'<span class="diff-notes">Notes: {total} Bell: {bell}</span>'
            )
            if designer and designer != "-":
                diff_rows_html += f'<span class="diff-designer">谱师: {designer}</span>'
            diff_rows_html += "</div>"

        html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
{_BASE_HTML_STYLE}
body{{padding:28px 32px 14px 32px}}
.header{{margin-bottom:20px}}
.header .title{{font-size:22px;color:#e8e8f0;font-weight:600;letter-spacing:1px}}
.header .id{{font-size:13px;color:#6868a0;margin-top:4px}}
.body2{{display:flex;gap:24px;margin-top:16px}}
.cover{{flex-shrink:0;width:200px;height:200px;border-radius:10px;overflow:hidden;border:2px solid #444460;box-shadow:0 2px 10px rgba(0,0,0,.3)}}
.cover img{{width:100%;height:100%;object-fit:cover}}
.info{{flex:1;display:flex;flex-direction:column;gap:8px}}
.info .row{{font-size:14px;color:#c8c8d8}}
.info .row .label{{color:#7878a8;margin-right:6px}}
.diff-section{{margin-top:16px;padding-top:10px;border-top:1px solid #333350}}
.diff-section .sec-label{{font-size:14px;color:#9090b8;margin-bottom:8px}}
.diff-row{{font-size:13px;color:#a0a0c0;padding:3px 0;display:flex;gap:10px;flex-wrap:wrap;align-items:center}}
.diff-type{{color:#6868a0;font-size:11px;min-width:32px;font-weight:600}}
.diff-name{{font-weight:600;min-width:80px}}
.diff-lvl{{color:#c8c8d8}}
.diff-ilvl{{color:#8888b0;font-size:12px}}
.diff-notes{{color:#9090b8;font-size:12px}}
.diff-designer{{color:#7878a0;font-size:12px}}
.footer{{margin-top:16px;padding-top:10px;border-top:1px solid #333350;text-align:right;font-size:12px;color:#585878}}
</style></head>
<body>
<div class="header">
<div class="title">{title}</div>
<div class="id">ID: {sid}</div>
</div>
<div class="body2">
<div class="cover"><img src="data:image/png;base64,{cover_b64}" /></div>
<div class="info">
<div class="row"><span class="label">作者</span>{artist}</div>
<div class="row"><span class="label">BPM</span>{bpm}</div>
<div class="row"><span class="label">分类</span>{category}</div>
<div class="row"><span class="label">版本</span>{version}</div>
<div class="row"><span class="label">追加日</span>{_html.escape(release)}</div>
</div>
</div>
<div class="diff-section">
<div class="sec-label">谱面详情</div>
{diff_rows_html}
</div>
<div class="footer">数据来源: arcade-songs &middot; MaiBot</div>
</body></html>"""

        return await self._render_html_to_png(html, width=680, height=100, wait_for_images=True)

    async def _send_song_detail(self, song: dict, stream_id: str) -> None:
        if self.config.image.enabled:
            cover_b64 = None
            try:
                cover_b64 = await self._download_cover_base64(song)
            except Exception as e:
                logger.debug("曲绘下载异常: %s", e)
            if cover_b64:
                try:
                    image_b64 = await self._render_song_detail_image(song, cover_b64)
                    segments = [{"type": "image", "content": image_b64}]
                    await self.ctx.send.hybrid(segments, stream_id)
                    return
                except Exception as e:
                    logger.warning("图片渲染/发送失败: %s，回退到文本模式", e)
            else:
                await self.ctx.send.text("曲绘下载失败，已切换为文字模式", stream_id)
        detail = self._build_song_detail(song)
        await self.ctx.send.text(detail, stream_id)

    async def _send_song_detail_text(self, song: dict, stream_id: str) -> None:
        detail = self._build_song_detail(song)
        await self.ctx.send.text(detail, stream_id)

    async def _send_cover_image(self, song: dict, stream_id: str) -> bool:
        cover_b64 = await self._download_cover_base64(song)
        if not cover_b64:
            return False
        try:
            segments = [{"type": "image", "content": cover_b64}]
            await self.ctx.send.hybrid(segments, stream_id)
            return True
        except Exception as e:
            logger.warning("曲绘发送失败: %s", e)
            return False

    async def _format_search_results(self, results: list[dict]) -> str:
        lines = [f"找到 {len(results)} 首曲目:", ""]
        for i, song in enumerate(results[:20], 1):
            title = song.get("title", "?")
            artist = song.get("artist", "?")
            sheets = song.get("sheets", [])
            lvl_parts = []
            seen_diff = set()
            for s in sheets:
                if not isinstance(s, dict):
                    continue
                diff = s.get("difficulty", "")
                lvl = s.get("level", "")
                if diff and lvl and diff not in seen_diff:
                    seen_diff.add(diff)
                    d = DIFFICULTY_DISPLAY.get(diff, diff.upper())
                    lvl_parts.append(f"{d} {lvl}")
            line = f"{i}. {title}  -  {artist}"
            lines.append(line)
            if lvl_parts:
                lines.append(f"   {' / '.join(lvl_parts)}")
        if len(results) > 20:
            lines.append(f"...以及另外 {len(results) - 20} 首")
        return "\n".join(lines)

    @staticmethod
    def _build_song_detail(song: dict) -> str:
        title = str(song.get("title", "") or "?")
        artist = str(song.get("artist", "") or "?")
        bpm = song.get("bpm", "?")
        category = str(song.get("category", "") or "?")
        version = str(song.get("version", "") or "?")
        release = str(song.get("releaseDate", "") or "?")
        sid = str(song.get("songId", "") or "?")

        lines = [
            f"╭─ {title}",
            f"│ ID: {sid}",
            f"│ 作者: {artist}  |  BPM: {bpm}",
            f"│ 分类: {category}  |  版本: {version}",
            f"│ 追加日: {release}",
            f"╰────────────────",
            "",
            "─ 谱面信息 ─",
        ]

        sheets = song.get("sheets", [])
        if not sheets:
            lines.append("  (无谱面数据)")
        else:
            for sheet in sheets:
                if not isinstance(sheet, dict):
                    continue
                tp = sheet.get("type", "std")
                diff = sheet.get("difficulty", "?")
                level = str(sheet.get("level", "") or "?")
                internal = sheet.get("internalLevel")
                internal_str = str(internal) if internal else "-"
                designer = str(sheet.get("noteDesigner", "") or "-")
                notes = sheet.get("noteCounts", {}) or {}
                total = notes.get("total", "?")
                bell = notes.get("bell", "?")

                tp_str = TYPE_DISPLAY.get(tp, tp.upper())
                diff_str = DIFFICULTY_DISPLAY.get(diff, diff.upper())

                line = (
                    f"  [{tp_str}] {diff_str}  "
                    f"Lv.{level} (定数: {internal_str})  "
                    f"Notes: {total}  Bell: {bell}"
                )
                if designer and designer != "-":
                    line += f"  谱师: {designer}"
                lines.append(line)

        return "\n".join(lines)

    @Command(
        "ongeki_random",
        description="随机推荐一首オンゲキ曲目",
        pattern=r"^/(?:ongeki|og)\s+random$",
    )
    async def handle_random(self, stream_id: str = "", **kwargs: Any):
        del kwargs
        data = await self._fetch_data()
        if not data:
            await self.ctx.send.text("获取曲库数据失败，请稍后再试", stream_id)
            return False, "数据获取失败", False

        songs = data.get("songs", [])
        if not songs:
            await self.ctx.send.text("曲库为空", stream_id)
            return False, "曲库为空", False

        candidates = [s for s in songs if isinstance(s, dict) and s.get("songId") not in self._recommended_ids]
        if not candidates:
            self._recommended_ids.clear()
            candidates = [s for s in songs if isinstance(s, dict)]

        song = random.choice(candidates)
        sid = str(song.get("songId", "") or "")
        if sid:
            self._recommended_ids.append(sid)

        await self._send_song_detail(song, stream_id)
        return True, f"随机曲目: {song.get('title', '?')}", True

    @Command(
        "ongeki_random_text",
        description="随机推荐一首オンゲキ曲目（文字模式）",
        pattern=r"^/(?:ongeki|og)\s+t\s+random$",
    )
    async def handle_random_text(self, stream_id: str = "", **kwargs: Any):
        del kwargs
        data = await self._fetch_data()
        if not data:
            await self.ctx.send.text("获取曲库数据失败，请稍后再试", stream_id)
            return False, "数据获取失败", False
        songs = data.get("songs", [])
        if not songs:
            await self.ctx.send.text("曲库为空", stream_id)
            return False, "曲库为空", False

        candidates = [s for s in songs if isinstance(s, dict) and s.get("songId") not in self._recommended_ids]
        if not candidates:
            self._recommended_ids.clear()
            candidates = [s for s in songs if isinstance(s, dict)]
        song = random.choice(candidates)
        sid = str(song.get("songId", "") or "")
        if sid:
            self._recommended_ids.append(sid)

        await self._send_song_detail_text(song, stream_id)
        return True, f"随机曲目: {song.get('title', '?')}", True

    @Command(
        "ongeki_cover",
        description="获取オンゲキ曲目曲绘大图",
        pattern=r"^/(?:ongeki|og)\s+cover\s+(?P<keyword>.+)$",
    )
    async def handle_cover(self, stream_id: str = "", **kwargs: Any):
        matched = kwargs.get("matched_groups") or {}
        keyword = str(matched.get("keyword", "") or "").strip()
        if not keyword:
            raw = str(kwargs.get("text", "") or "").strip()
            m = re.match(r"^/(?:ongeki|og)\s+cover\s+(.+)$", raw, re.DOTALL)
            if m:
                keyword = m.group(1).strip()
        if not keyword:
            await self.ctx.send.text("用法: /og cover <曲名关键词>", stream_id)
            return True, "缺少关键词", True

        data = await self._fetch_data()
        if not data:
            await self.ctx.send.text("获取曲库数据失败，请稍后再试", stream_id)
            return False, "数据获取失败", False

        results = await self._match_songs(keyword)
        if not results:
            await self.ctx.send.text(f"未找到与「{keyword}」相关的曲目", stream_id)
            return False, "未找到曲目", False

        song = results[0]
        sent = await self._send_cover_image(song, stream_id)
        if sent:
            return True, f"曲绘: {song.get('title', '?')}", True
        await self.ctx.send.text("曲绘获取失败", stream_id)
        return False, "曲绘获取失败", False

    @Command(
        "ongeki_alias_add",
        description="为オンゲキ曲目添加别称",
        pattern=r"^/(?:ongeki|og)\s+alias\s+add\s+(?P<song_id>\S+)\s+(?P<alias>.+)$",
    )
    async def handle_alias_add(self, stream_id: str = "", **kwargs: Any):
        matched = kwargs.get("matched_groups") or {}
        song_id = str(matched.get("song_id", "") or "").strip()
        alias = str(matched.get("alias", "") or "").strip()
        if not song_id or not alias:
            raw = str(kwargs.get("text", "") or "").strip()
            m = re.match(r"^/(?:ongeki|og)\s+alias\s+add\s+(\S+)\s+(.+)$", raw, re.DOTALL)
            if m:
                song_id = m.group(1).strip()
                alias = m.group(2).strip()
        if not song_id or not alias:
            await self.ctx.send.text("用法: /og alias add <歌曲ID> <别称>", stream_id)
            return True, "参数不足", True

        ok, msg = await self._aliases.add(song_id, alias)
        await self.ctx.send.text(msg, stream_id)
        return ok, msg, True

    @Command(
        "ongeki_alias_del",
        description="删除オンゲキ曲目别称",
        pattern=r"^/(?:ongeki|og)\s+alias\s+del\s+(?P<song_id>\S+)\s+(?P<alias>.+)$",
    )
    async def handle_alias_del(self, stream_id: str = "", **kwargs: Any):
        matched = kwargs.get("matched_groups") or {}
        song_id = str(matched.get("song_id", "") or "").strip()
        alias = str(matched.get("alias", "") or "").strip()
        if not song_id or not alias:
            raw = str(kwargs.get("text", "") or "").strip()
            m = re.match(r"^/(?:ongeki|og)\s+alias\s+del\s+(\S+)\s+(.+)$", raw, re.DOTALL)
            if m:
                song_id = m.group(1).strip()
                alias = m.group(2).strip()
        if not song_id or not alias:
            await self.ctx.send.text("用法: /og alias del <歌曲ID> <别称>", stream_id)
            return True, "参数不足", True

        ok, msg = await self._aliases.delete(song_id, alias)
        await self.ctx.send.text(msg, stream_id)
        return ok, msg, True

    @Command(
        "ongeki_alias_list",
        description="列出オンゲキ曲目的所有别称",
        pattern=r"^/(?:ongeki|og)\s+alias\s+list\s+(?P<song_id>\S+)$",
    )
    async def handle_alias_list(self, stream_id: str = "", **kwargs: Any):
        matched = kwargs.get("matched_groups") or {}
        song_id = str(matched.get("song_id", "") or "").strip()
        if not song_id:
            raw = str(kwargs.get("text", "") or "").strip()
            m = re.match(r"^/(?:ongeki|og)\s+alias\s+list\s+(\S+)$", raw)
            if m:
                song_id = m.group(1).strip()
        if not song_id:
            await self.ctx.send.text("用法: /og alias list <歌曲ID>", stream_id)
            return True, "参数不足", True

        aliases = await self._aliases.list_aliases(song_id)
        if aliases:
            await self.ctx.send.text(f"歌曲 {song_id} 的别称: {', '.join(aliases)}", stream_id)
        else:
            await self.ctx.send.text(f"歌曲 {song_id} 没有别称", stream_id)
        return True, f"列出别称: {len(aliases)} 个", True

    @Command(
        "ongeki_alias_help",
        description="别称管理帮助",
        pattern=r"^/(?:ongeki|og)\s+alias\s*$",
    )
    async def handle_alias_help(self, stream_id: str = "", **kwargs: Any):
        del kwargs
        help_text = (
            "别称管理:\n"
            "/og alias add <歌曲ID> <别称>  - 添加别称\n"
            "/og alias del <歌曲ID> <别称>  - 删除别称\n"
            "/og alias list <歌曲ID>  - 查看别称\n\n"
            "使用 songId（曲名原文即为 ID）添加别称后，可用别称搜索曲目"
        )
        await self.ctx.send.text(help_text, stream_id)
        return True, "别名帮助", True

    @Command(
        "ongeki_search",
        description="搜索オンゲキ曲目",
        pattern=r"^/(?:ongeki|og)\s+search\s+(?P<keyword>.+)$",
    )
    async def handle_search(self, stream_id: str = "", **kwargs: Any):
        matched = kwargs.get("matched_groups") or {}
        keyword = str(matched.get("keyword", "") or "").strip()
        if not keyword:
            raw = str(kwargs.get("text", "") or "").strip()
            m = re.match(r"^/(?:ongeki|og)\s+search\s+(.+)$", raw, re.DOTALL)
            if m:
                keyword = m.group(1).strip()

        if not keyword:
            await self.ctx.send.text(
                "用法: /ongeki search <关键词> 或 /og search <关键词>", stream_id
            )
            return True, "缺少搜索关键词", True

        data = await self._fetch_data()
        if not data:
            await self.ctx.send.text("获取曲库数据失败，请稍后再试", stream_id)
            return False, "数据获取失败", False

        results = await self._match_songs(keyword)
        if not results:
            await self.ctx.send.text(f"未找到与「{keyword}」相关的曲目", stream_id)
            return False, f"未找到曲目: {keyword}", False

        if len(results) == 1:
            await self._send_song_detail(results[0], stream_id)
            return True, f"显示曲目详情: {results[0].get('title', '?')}", True

        text = await self._format_search_results(results)
        await self.ctx.send.text(text, stream_id)
        return True, f"搜索结果: {len(results)} 首", True

    @Command(
        "ongeki_search_text",
        description="搜索オンゲキ曲目（文字模式）",
        pattern=r"^/(?:ongeki|og)\s+t\s+search\s+(?P<keyword>.+)$",
    )
    async def handle_search_text(self, stream_id: str = "", **kwargs: Any):
        matched = kwargs.get("matched_groups") or {}
        keyword = str(matched.get("keyword", "") or "").strip()
        if not keyword:
            raw = str(kwargs.get("text", "") or "").strip()
            m = re.match(r"^/(?:ongeki|og)\s+t\s+search\s+(.+)$", raw, re.DOTALL)
            if m:
                keyword = m.group(1).strip()
        if not keyword:
            await self.ctx.send.text("用法: /og t search <关键词>", stream_id)
            return True, "缺少关键词", True

        data = await self._fetch_data()
        if not data:
            await self.ctx.send.text("获取曲库数据失败，请稍后再试", stream_id)
            return False, "数据获取失败", False
        results = await self._match_songs(keyword)
        if not results:
            await self.ctx.send.text(f"未找到与「{keyword}」相关的曲目", stream_id)
            return False, "未找到曲目", False

        if len(results) == 1:
            await self._send_song_detail_text(results[0], stream_id)
            return True, f"显示曲目详情: {results[0].get('title', '?')}", True

        text = await self._format_search_results(results)
        await self.ctx.send.text(text, stream_id)
        return True, f"搜索结果: {len(results)} 首", True

    @Command(
        "ongeki_query",
        description="查询オンゲキ曲目详情",
        pattern=r"^/(?:ongeki|og)\s+(?!help\b|search\b|random\b|t\b|cover\b|alias\b)(?P<keyword>.+)$",
    )
    async def handle_query(self, stream_id: str = "", **kwargs: Any):
        matched = kwargs.get("matched_groups") or {}
        keyword = str(matched.get("keyword", "") or "").strip()
        if not keyword:
            raw = str(kwargs.get("text", "") or "").strip()
            m = re.match(r"^/(?:ongeki|og)\s+(.+)$", raw, re.DOTALL)
            if m:
                keyword = m.group(1).strip()

        if not keyword:
            await self.ctx.send.text(
                "用法: /ongeki <曲名关键词> 或 /og <曲名关键词>", stream_id
            )
            return True, "缺少关键词", True

        data = await self._fetch_data()
        if not data:
            await self.ctx.send.text("获取曲库数据失败，请稍后再试", stream_id)
            return False, "数据获取失败", False

        results = await self._match_songs(keyword)
        if not results:
            await self.ctx.send.text(f"未找到与「{keyword}」相关的曲目", stream_id)
            return False, f"未找到曲目: {keyword}", False

        if len(results) > 1:
            text = await self._format_search_results(results)
            await self.ctx.send.text(text, stream_id)
            return True, f"搜索结果: {len(results)} 首", True

        await self._send_song_detail(results[0], stream_id)
        return True, f"显示曲目详情: {results[0].get('title', '?')}", True

    @Command(
        "ongeki_query_text",
        description="查询オンゲキ曲目详情（文字模式）",
        pattern=r"^/(?:ongeki|og)\s+t\s+(?P<keyword>.+)$",
    )
    async def handle_query_text(self, stream_id: str = "", **kwargs: Any):
        matched = kwargs.get("matched_groups") or {}
        keyword = str(matched.get("keyword", "") or "").strip()
        if not keyword:
            raw = str(kwargs.get("text", "") or "").strip()
            m = re.match(r"^/(?:ongeki|og)\s+t\s+(.+)$", raw, re.DOTALL)
            if m:
                keyword = m.group(1).strip()
        if not keyword:
            await self.ctx.send.text("用法: /og t <曲名关键词>", stream_id)
            return True, "缺少关键词", True

        data = await self._fetch_data()
        if not data:
            await self.ctx.send.text("获取曲库数据失败，请稍后再试", stream_id)
            return False, "数据获取失败", False
        results = await self._match_songs(keyword)
        if not results:
            await self.ctx.send.text(f"未找到与「{keyword}」相关的曲目", stream_id)
            return False, "未找到曲目", False

        if len(results) > 1:
            text = await self._format_search_results(results)
            await self.ctx.send.text(text, stream_id)
            return True, f"搜索结果: {len(results)} 首", True

        await self._send_song_detail_text(results[0], stream_id)
        return True, f"显示曲目详情: {results[0].get('title', '?')}", True


    @Command(
        "ongeki_help",
        description="显示オンゲキ谱面查询帮助",
        pattern=r"^/(?:ongeki|og)(?:\s+help)?\s*$",
    )
    async def handle_help(self, stream_id: str = "", **kwargs: Any):
        del kwargs
        help_text = (
            "オンゲキ谱面查询:\n"
            "/og <曲名>  - 查询曲目详情\n"
            "/og search <关键词>  - 搜索曲目\n"
            "/og random  - 随机推荐\n"
            "/og cover <曲名>  - 获取曲绘\n"
            "/og alias ...  - 管理别称\n"
            "/og t <曲名>  - 文字模式查询"
        )
        await self.ctx.send.text(help_text, stream_id)
        return True, "显示帮助", True

def create_plugin() -> OngekiProberPlugin:
    return OngekiProberPlugin()
