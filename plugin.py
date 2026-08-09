# -*- coding: utf-8 -*-
"""オンゲキ 谱面查询插件 — 获取曲目信息、谱面定数、Note 配置等 (v1.1.0)

v1.1.0 主要变更：
- 合并 (LUN) 变体条目：同名 LUNATIC 谱面并入基础曲目，修复 MEGALOVANIA 等
  特殊难度歌曲搜索时被拆成两条结果的问题。
- 搜索结果列表、帮助消息在图片模式下也渲染为图片卡片。
- 渲染链路优先使用 MaiBot 宿主 render.html2png，失败回退内置 Playwright，
  默认 2x 设备像素比输出高清图片。
- 曲绘缺失占位、None 数值容错、无法定级的特殊 LUNATIC 谱面显示 Lv.?? [未知]。
"""

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
from typing import Any, Callable, Optional

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

LUNATIC_ID_PREFIX = "(LUN) "
MAX_LIST_IMAGE_ROWS = 15


def safe_str(value: Any, default: str = "?") -> str:
    """None/空值统一转默认占位，避免渲染出 'None' 文本。"""
    return str(value) if value not in (None, "") else default


class HtmlRenderer:
    """HTML → PNG 渲染器。

    渲染链路与 maimaidx_prober v2.0 一致：
    1. 优先调用 MaiBot 宿主提供的 render.html2png 能力（宿主统一管理 Chromium、
       并发与沙箱参数）；
    2. 宿主能力不可用时回退到插件内置 Playwright（懒加载单例浏览器）。

    默认以 2x 设备像素比输出高清图片。
    """

    def __init__(
        self,
        ctx_provider: Callable[[], Any],
        device_scale_factor: float = 2.0,
        image_timeout_ms: int = 15000,
        no_sandbox: bool = True,
    ) -> None:
        self._ctx_provider = ctx_provider
        self._device_scale_factor = device_scale_factor
        self._image_timeout_ms = image_timeout_ms
        self._no_sandbox = no_sandbox

        self._playwright_inst = None
        self._browser = None
        self._browser_lock = asyncio.Lock()

    # ---- 生命周期 ----

    async def close(self) -> None:
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

    # ---- 渲染 ----

    async def render(
        self,
        html: str,
        width: int = 680,
        height: int = 500,
        wait_images: bool = False,
        image_timeout: int = 0,
        strict_images: bool = True,
    ) -> str:
        """渲染 HTML 并返回 PNG base64。"""
        image_timeout = image_timeout or self._image_timeout_ms
        try:
            result = await self._render_via_host(
                html, width, height, wait_images, image_timeout,
            )
            if result:
                return result
        except Exception as e:
            logger.debug("宿主渲染能力不可用，回退 Playwright: %s", e)
        return await self._render_via_playwright(
            html, width, height, wait_images, image_timeout, strict_images,
        )

    async def _render_via_host(
        self,
        html: str,
        width: int,
        height: int,
        wait_images: bool,
        image_timeout: int,
    ) -> Optional[str]:
        ctx = self._ctx_provider()
        if ctx is None or not hasattr(ctx, "render"):
            return None
        result = await ctx.render.html2png(
            html,
            selector="body",
            viewport={"width": width, "height": height},
            device_scale_factor=self._device_scale_factor,
            full_page=True,
            wait_until="load",
            wait_for_timeout_ms=min(image_timeout, 3000) if wait_images else 800,
            allow_network=False,
        )
        if isinstance(result, dict):
            b64 = result.get("image_base64") or ""
        else:
            b64 = getattr(result, "image_base64", "") or ""
        return b64 or None

    async def _ensure_browser(self):
        if self._browser is None:
            async with self._browser_lock:
                if self._browser is None:
                    try:
                        from playwright.async_api import async_playwright
                    except ImportError:
                        raise RuntimeError(
                            "playwright 未安装，请执行: pip install playwright && "
                            "python -m playwright install chromium"
                        )
                    self._playwright_inst = await async_playwright().start()
                    launch_args: dict[str, Any] = {"headless": True}
                    if self._no_sandbox:
                        launch_args["args"] = [
                            "--no-sandbox",
                            "--disable-setuid-sandbox",
                        ]
                    self._browser = await self._playwright_inst.chromium.launch(
                        **launch_args
                    )
        return self._browser

    async def _render_via_playwright(
        self,
        html: str,
        width: int,
        height: int,
        wait_images: bool,
        image_timeout: int,
        strict_images: bool,
    ) -> str:
        browser = await self._ensure_browser()
        page = await browser.new_page(
            viewport={"width": width, "height": height},
            device_scale_factor=self._device_scale_factor,
        )
        try:
            await page.set_content(html)
            await page.wait_for_load_state("domcontentloaded")
            if wait_images:
                # 同时校验 naturalWidth > 0，避免坏图静默通过
                expr = (
                    "() => [...document.querySelectorAll('img')]"
                    ".every(i => i.complete && i.naturalWidth > 0)"
                    if strict_images
                    else "() => [...document.querySelectorAll('img')].every(i => i.complete)"
                )
                try:
                    await page.wait_for_function(expr, timeout=image_timeout)
                except Exception:
                    logger.debug("等待曲绘加载超时或失败，继续渲染")
            await page.wait_for_timeout(500)
            shot = await page.screenshot(full_page=True, type="png")
        finally:
            await page.close()
        return base64.b64encode(shot).decode()


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

    async def remap(self, mapping: dict[str, str]) -> bool:
        """将变体 ID 的别称迁移到基础 ID（如 (LUN) xxx → xxx）。

        合并 LUNATIC 变体后，原来挂在变体 ID 上的别称需要迁移到基础曲目，
        否则用户添加的别称会变成“查不到”。返回是否发生了迁移。
        """
        async with self._lock:
            if not mapping or not self._data:
                return False
            new_data: dict[str, list[str]] = {}
            changed = False
            for sid, aliases in self._data.items():
                target = mapping.get(str(sid), str(sid))
                bucket = new_data.setdefault(target, [])
                for alias in aliases:
                    if alias not in bucket:
                        bucket.append(alias)
                        changed = True
            if changed:
                self._data = new_data
                self._rebuild_index()
                await self._save()
            return changed


class PluginSectionConfig(PluginConfigBase):
    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0
    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(default="1.1.0", description="配置版本")


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


class RenderConfig(PluginConfigBase):
    __ui_label__ = "渲染"
    __ui_icon__ = "image"
    __ui_order__ = 3
    device_scale_factor: float = Field(default=2.0, description="图片设备像素比（2.0 为高清）")
    image_timeout_ms: int = Field(default=15000, description="图片等待加载超时(毫秒)")


class OngekiProberConfig(PluginConfigBase):
    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    image: ImageConfig = Field(default_factory=ImageConfig)
    render: RenderConfig = Field(default_factory=RenderConfig)


class OngekiProberPlugin(MaiBotPlugin):
    config_model = OngekiProberConfig

    async def on_load(self) -> None:
        self._http_session: Optional[aiohttp.ClientSession] = None
        self._client_lock = asyncio.Lock()
        self._songs_cache: Optional[list[dict]] = None
        self._cache_time: float = 0
        self._recommended_ids: deque[str] = deque(maxlen=200)
        self._aliases_remapped = False
        self._renderer = HtmlRenderer(
            ctx_provider=lambda: self.ctx,
            device_scale_factor=self.config.render.device_scale_factor,
            image_timeout_ms=self.config.render.image_timeout_ms,
        )
        data_dir = self.ctx.paths.data_dir
        self._aliases = AliasStore(str(data_dir / "aliases.json"))
        await self._aliases.load()

    async def on_unload(self) -> None:
        await self._renderer.close()
        if self._http_session:
            await self._http_session.close()
            self._http_session = None

    async def on_config_update(
        self, scope: str, config_data: dict[str, object], version: str
    ) -> None:
        del config_data, version
        if scope == "self":
            await self._renderer.close()
            async with self._client_lock:
                if self._http_session:
                    await self._http_session.close()
                    self._http_session = None
            self._songs_cache = None
            self._cache_time = 0
            self._renderer = HtmlRenderer(
                ctx_provider=lambda: self.ctx,
                device_scale_factor=self.config.render.device_scale_factor,
                image_timeout_ms=self.config.render.image_timeout_ms,
            )

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._http_session is None:
            async with self._client_lock:
                if self._http_session is None:
                    self._http_session = aiohttp.ClientSession()
        return self._http_session

    @staticmethod
    def _normalize_songs(songs: list[dict]) -> tuple[list[dict], dict[str, str]]:
        """合并 (LUN) 变体条目，返回 (规范化曲目列表, 变体ID→基础ID映射)。

        arcade-songs 数据中，LUNATIC 特殊难度以独立条目出现：
        其 songId 直接是名字（如 ``(LUN) MEGALOVANIA``），category 为 LUNATIC，
        sheet 类型为 lun。若同名基础曲目存在，则把 LUNATIC 谱面并入基础曲目，
        避免搜索“MEGALOVANIA”时返回两条重复结果；没有基础曲目的纯 LUNATIC
        曲目（如「怨撃」「No Remorse」）保持独立条目。
        """
        result: list[dict] = []
        base_by_title: dict[str, dict] = {}
        lun_entries: list[tuple[int, dict]] = []
        lun_mapping: dict[str, str] = {}

        # 第一遍：收集基础曲目，暂存 LUNATIC 变体（变体可能排在基础条目之前）
        for idx, song in enumerate(songs):
            if not isinstance(song, dict):
                continue
            sid = str(song.get("songId", "") or "")
            if sid.startswith(LUNATIC_ID_PREFIX):
                lun_entries.append((idx, song))
            else:
                result.append(song)
                title_key = str(song.get("title", "") or "").strip().lower()
                base_by_title.setdefault(title_key, song)

        # 第二遍：合并有同名基础的变体，纯 LUNATIC 曲目按原顺序插回
        for idx, song in lun_entries:
            sid = str(song.get("songId", "") or "")
            title_key = str(song.get("title", "") or "").strip().lower()
            base = base_by_title.get(title_key)
            if base is None:
                insert_pos = min(idx, len(result))
                result.insert(insert_pos, song)
                continue
            base_sid = str(base.get("songId", "") or "")
            lun_mapping[sid] = base_sid
            variant_ids = base.setdefault("_variantSongIds", [])
            if sid not in variant_ids:
                variant_ids.append(sid)
            sheets = base.setdefault("sheets", [])
            if not isinstance(sheets, list):
                sheets = []
                base["sheets"] = sheets
            existing = {
                (
                    str(s.get("type", "")),
                    str(s.get("difficulty", "")),
                    str(s.get("level", "")),
                    str(s.get("levelValue", "") or s.get("internalLevelValue", "") or ""),
                )
                for s in sheets
                if isinstance(s, dict)
            }
            for sheet in song.get("sheets", []) or []:
                if not isinstance(sheet, dict):
                    continue
                key = (
                    str(sheet.get("type", "")),
                    str(sheet.get("difficulty", "")),
                    str(sheet.get("level", "")),
                    str(sheet.get("levelValue", "") or sheet.get("internalLevelValue", "") or ""),
                )
                if key not in existing:
                    sheets.append(sheet)
                    existing.add(key)
            base["_hasLunatic"] = True

        return result, lun_mapping

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
        except Exception as e:
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

        songs, lun_mapping = self._normalize_songs(songs)
        self._songs_cache = songs
        self._cache_time = now
        data["songs"] = songs

        if lun_mapping and not self._aliases_remapped:
            try:
                await self._aliases.remap(lun_mapping)
            except Exception as e:
                logger.debug("别称迁移失败: %s", e)
            self._aliases_remapped = True
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
            variant_ids = {
                str(v).lower() for v in song.get("_variantSongIds", []) or []
            }

            if (
                kw in title
                or kw in artist
                or kw == sid.lower()
                or kw in variant_ids
                or sid in alias_sids
            ):
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

    async def _render_and_send(
        self,
        stream_id: str,
        render_call: Callable[[], Any],
        fail_prefix: str = "图片生成失败",
    ) -> bool:
        """渲染图片并发送；失败时发送错误文本并返回 False。"""
        try:
            image_b64 = await render_call()
        except RuntimeError as e:
            await self.ctx.send.text(str(e), stream_id)
            return False
        except Exception as e:
            logger.warning("%s: %s", fail_prefix, e, exc_info=True)
            await self.ctx.send.text(f"{fail_prefix}: {e}", stream_id)
            return False
        try:
            await self.ctx.send.image(image_b64, stream_id)
        except Exception as e:
            logger.warning("send.image 失败，回退 send.hybrid: %s", e)
            try:
                segments = [{"type": "image", "content": image_b64}]
                await self.ctx.send.hybrid(segments, stream_id)
            except Exception as e2:
                logger.warning("send.hybrid 发送失败: %s", e2)
                return False
        return True

    # ---- 图片渲染 ----

    async def _render_song_detail_image(
        self, song: dict, cover_b64: Optional[str]
    ) -> str:
        title = _html.escape(safe_str(song.get("title"), "?"))
        artist = _html.escape(safe_str(song.get("artist"), "?"))
        bpm = _html.escape(safe_str(song.get("bpm"), "?"))
        category = _html.escape(safe_str(song.get("category"), "?"))
        version = _html.escape(safe_str(song.get("version"), "?"))
        release = _html.escape(safe_str(song.get("releaseDate"), "?"))
        sid = _html.escape(safe_str(song.get("songId"), "?"))

        cover_html = (
            f'<div class="cover"><img src="data:image/png;base64,{cover_b64}" /></div>'
            if cover_b64
            else '<div class="cover cover-missing">曲绘缺失</div>'
        )

        diff_rows_html = ""
        for sheet in song.get("sheets", []) or []:
            if not isinstance(sheet, dict):
                continue
            tp = sheet.get("type", "std")
            diff = sheet.get("difficulty", "?")
            level = str(sheet.get("level", "") or "?")
            unreleased = level == "0"
            lvl_display = "??" if unreleased else _html.escape(level)
            internal = sheet.get("internalLevel")
            internal_str = _html.escape(safe_str(internal, "-"))
            designer = _html.escape(safe_str(sheet.get("noteDesigner"), "-"))
            notes = sheet.get("noteCounts", {}) or {}
            total = _html.escape(safe_str(notes.get("total"), "-"))
            bell = _html.escape(safe_str(notes.get("bell"), "-"))
            color = DIFF_COLORS.get(diff, "#ffffff")

            tp_str = TYPE_DISPLAY.get(tp, tp.upper())
            diff_str = DIFFICULTY_DISPLAY.get(diff, diff.upper())

            diff_rows_html += (
                f'<div class="diff-row">'
                f'<span class="diff-type">{tp_str}</span>'
                f'<span class="diff-name" style="color:{color}">{diff_str}</span>'
                f'<span class="diff-lvl">Lv.{lvl_display}</span>'
                f'<span class="diff-ilvl">(定数 {internal_str})</span>'
                f'<span class="diff-notes">Notes: {total} Bell: {bell}</span>'
            )
            if unreleased:
                diff_rows_html += '<span class="diff-unknown">未知</span>'
            if designer and designer != "-":
                diff_rows_html += f'<span class="diff-designer">谱师: {designer}</span>'
            diff_rows_html += "</div>"

        style = (
            "body{padding:28px 32px 14px 32px}"
            ".header{margin-bottom:20px}"
            ".header .title{font-size:22px;color:#e8e8f0;font-weight:600;letter-spacing:1px}"
            ".header .id{font-size:13px;color:#6868a0;margin-top:4px}"
            ".body2{display:flex;gap:24px;margin-top:16px}"
            ".cover{flex-shrink:0;width:200px;height:200px;border-radius:10px;"
            "overflow:hidden;border:2px solid #444460;box-shadow:0 2px 10px rgba(0,0,0,.3);"
            "display:flex;align-items:center;justify-content:center}"
            ".cover img{width:100%;height:100%;object-fit:cover;display:block}"
            ".cover-missing{font-size:14px;color:#a0a0c0;background:#24243a}"
            ".info{flex:1;display:flex;flex-direction:column;gap:8px;min-width:0}"
            ".info .row{font-size:14px;color:#c8c8d8}"
            ".info .row .label{color:#7878a8;margin-right:6px}"
            ".diff-section{margin-top:16px;padding-top:10px;border-top:1px solid #333350}"
            ".diff-section .sec-label{font-size:14px;color:#9090b8;margin-bottom:8px}"
            ".diff-row{font-size:13px;color:#a0a0c0;padding:3px 0;display:flex;"
            "gap:10px;flex-wrap:wrap;align-items:center}"
            ".diff-type{color:#6868a0;font-size:11px;min-width:32px;font-weight:600}"
            ".diff-name{font-weight:600;min-width:80px}"
            ".diff-lvl{color:#c8c8d8}"
            ".diff-ilvl{color:#8888b0;font-size:12px}"
            ".diff-notes{color:#9090b8;font-size:12px}"
            ".diff-designer{color:#7878a0;font-size:12px}"
            ".diff-unknown{color:#ff9aa8;font-size:12px}"
            ".footer{margin-top:16px;padding-top:10px;border-top:1px solid #333350;"
            "text-align:right;font-size:12px;color:#7878a8}"
        )
        body = (
            '<div class="header">'
            f'<div class="title">{title}</div>'
            f'<div class="id">ID: {sid}</div>'
            "</div>"
            '<div class="body2">'
            f"{cover_html}"
            '<div class="info">'
            f'<div class="row"><span class="label">作者</span>{artist}</div>'
            f'<div class="row"><span class="label">BPM</span>{bpm}</div>'
            f'<div class="row"><span class="label">分类</span>{category}</div>'
            f'<div class="row"><span class="label">版本</span>{version}</div>'
            f'<div class="row"><span class="label">追加日</span>{release}</div>'
            "</div></div>"
            '<div class="diff-section">'
            '<div class="sec-label">谱面详情</div>'
            f"{diff_rows_html}"
            "</div>"
            '<div class="footer">数据来源: arcade-songs &middot; MaiBot</div>'
        )
        html = (
            "<!DOCTYPE html><html><head><meta charset=\"utf-8\"><style>"
            f"{_BASE_HTML_STYLE}{style}"
            "</style></head><body>"
            f"{body}"
            "</body></html>"
        )
        return await self._renderer.render(
            html, width=680, height=100, wait_images=bool(cover_b64), strict_images=True
        )

    @staticmethod
    def _difficulty_summary(song: dict) -> list[tuple[str, str, str]]:
        """返回 [(difficulty, 显示名, 等级)]，按数据顺序去重同难度。"""
        out: list[tuple[str, str, str]] = []
        seen: set[str] = set()
        for s in song.get("sheets", []) or []:
            if not isinstance(s, dict):
                continue
            diff = s.get("difficulty", "")
            lvl = str(s.get("level", "") or "?")
            if diff and diff not in seen:
                seen.add(diff)
                lvl_display = "??" if lvl == "0" else lvl
                out.append((diff, DIFFICULTY_DISPLAY.get(diff, diff.upper()), lvl_display))
        return out

    async def _render_search_list_image(self, keyword: str, results: list[dict]) -> str:
        kw = _html.escape(keyword)
        rows = results[:MAX_LIST_IMAGE_ROWS]
        items_html = ""
        for i, song in enumerate(rows, 1):
            title = _html.escape(safe_str(song.get("title"), "?"))
            artist = _html.escape(safe_str(song.get("artist"), "?"))
            chips = "".join(
                '<span class="chip">'
                f'<b style="color:{DIFF_COLORS.get(d, "#ffffff")}">{_html.escape(dl)}</b>'
                f" {_html.escape(lv)}"
                "</span>"
                for d, dl, lv in self._difficulty_summary(song)
            )
            items_html += (
                '<div class="item">'
                '<div class="item-top">'
                f'<span class="item-idx">#{i}</span>'
                f'<span class="item-title">{title}</span>'
                "</div>"
                f'<div class="item-artist">{artist}</div>'
                f'<div class="chips">{chips}</div>'
                "</div>"
            )
        if len(results) > MAX_LIST_IMAGE_ROWS:
            more = (
                '<div class="more">…以及另外 '
                f"{len(results) - MAX_LIST_IMAGE_ROWS} 首，请用更精确的关键词缩小范围"
                "</div>"
            )
        else:
            more = ""

        style = (
            "body{padding:28px 32px 14px 32px}"
            ".header{margin-bottom:18px}"
            ".header .title{font-size:20px;color:#e8e8f0;font-weight:600;letter-spacing:1px}"
            ".header .sub{font-size:13px;color:#6868a0;margin-top:4px}"
            ".item{padding:12px 0;border-bottom:1px solid #2e2e48;display:flex;"
            "flex-direction:column;gap:6px}"
            ".item:last-child{border-bottom:none}"
            ".item-top{display:flex;align-items:baseline;gap:10px;min-width:0}"
            ".item-idx{color:#5b8fd4;font-size:13px;font-weight:600;flex-shrink:0}"
            ".item-title{font-size:16px;color:#e8e8f0;font-weight:600;"
            "white-space:nowrap;overflow:hidden;text-overflow:ellipsis}"
            ".item-artist{font-size:12px;color:#7878a8;"
            "white-space:nowrap;overflow:hidden;text-overflow:ellipsis}"
            ".chips{display:flex;flex-wrap:wrap;gap:6px}"
            ".chip{font-size:11px;padding:2px 8px;border-radius:4px;"
            "background:#24243a;color:#c8c8d8}"
            ".chip b{font-weight:600}"
            ".more{font-size:12px;color:#7878a8;margin-top:10px}"
            ".footer{margin-top:14px;padding-top:10px;border-top:1px solid #333350;"
            "display:flex;font-size:12px}"
            ".footer .hint{flex:1;color:#7878a8}"
            ".footer .mai{color:#585878}"
        )
        body = (
            '<div class="header">'
            f'<div class="title">搜索结果: 「{kw}」</div>'
            f'<div class="sub">共 {len(results)} 首 &middot; 发送 /og &lt;曲名或ID&gt; 查看详情</div>'
            "</div>"
            f"{items_html}"
            f"{more}"
            '<div class="footer">'
            '<span class="hint">数据来源: arcade-songs</span>'
            '<span class="mai">MaiBot</span>'
            "</div>"
        )
        html = (
            "<!DOCTYPE html><html><head><meta charset=\"utf-8\"><style>"
            f"{_BASE_HTML_STYLE}{style}"
            "</style></head><body>"
            f"{body}"
            "</body></html>"
        )
        return await self._renderer.render(html, width=680, height=100)

    async def _render_help_image(self) -> str:
        sections = [
            (
                "曲目查询",
                [
                    ("/og <曲名/ID>", "查询曲目详情（含 LUNATIC 谱面）"),
                    ("/og search <关键词>", "显式搜索曲目"),
                    ("/og random", "随机推荐一首曲目"),
                    ("/og cover <曲名>", "获取曲绘大图"),
                ],
            ),
            (
                "文字模式",
                [
                    ("/og t <曲名>", "文字模式查询"),
                    ("/og t search <关键词>", "文字模式搜索"),
                    ("/og t random", "文字模式随机推荐"),
                ],
            ),
            (
                "别称管理",
                [
                    ("/og alias add <ID> <别称>", "添加别称"),
                    ("/og alias del <ID> <别称>", "删除别称"),
                    ("/og alias list <ID>", "查看别称"),
                    ("/og alias", "别称帮助"),
                ],
            ),
        ]
        style = (
            "body{padding:36px 44px}"
            ".header{text-align:center;margin-bottom:28px}"
            ".header h2{font-size:30px;color:#e8e8f0;letter-spacing:2px;margin-bottom:6px}"
            ".header .sub{font-size:14px;color:#7878a8}"
            ".section{margin-bottom:22px}"
            ".sec-label{font-size:17px;color:#c0c0d8;font-weight:600;margin-bottom:8px;"
            "padding-bottom:6px;border-bottom:1px solid #2e2e48}"
            ".cmd{display:flex;padding:6px 0;align-items:baseline}"
            ".cmd-name{flex-shrink:0;width:300px;font-size:14px;color:#5b8fd4;"
            "font-family:'Consolas','Courier New',monospace}"
            ".cmd-desc{font-size:14px;color:#9090b8}"
            ".footer{margin-top:18px;padding-top:10px;border-top:1px solid #333350;"
            "display:flex;font-size:12px}"
            ".footer .hint{flex:1;color:#7878a8}"
            ".footer .mai{color:#585878}"
        )
        sections_html = ""
        for label, cmds in sections:
            items = "".join(
                '<div class="cmd">'
                f'<span class="cmd-name">{_html.escape(cmd)}</span>'
                f'<span class="cmd-desc">{_html.escape(desc)}</span>'
                "</div>"
                for cmd, desc in cmds
            )
            sections_html += (
                f'<div class="section"><div class="sec-label">{_html.escape(label)}</div>'
                f"{items}</div>"
            )
        body = (
            '<div class="header">'
            "<h2>オンゲキ 谱面查询器</h2>"
            '<div class="sub">数据来源 arcade-songs &middot; 支持 LUNATIC 谱面合并显示</div>'
            "</div>"
            f"{sections_html}"
            '<div class="footer">'
            '<span class="hint">/og help 显示本帮助</span>'
            '<span class="mai">MaiBot</span>'
            "</div>"
        )
        html = (
            "<!DOCTYPE html><html><head><meta charset=\"utf-8\"><style>"
            f"{_BASE_HTML_STYLE}{style}"
            "</style></head><body>"
            f"{body}"
            "</body></html>"
        )
        return await self._renderer.render(html, width=760, height=100)

    # ---- 文本输出 ----

    async def _send_song_detail(self, song: dict, stream_id: str) -> None:
        if self.config.image.enabled:
            cover_b64 = None
            try:
                cover_b64 = await self._download_cover_base64(song)
            except Exception as e:
                logger.debug("曲绘下载异常: %s", e)
            rendered = await self._render_and_send(
                stream_id,
                lambda: self._render_song_detail_image(song, cover_b64),
                "曲目详情图片生成失败",
            )
            if rendered:
                return
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
            title = safe_str(song.get("title"), "?")
            artist = safe_str(song.get("artist"), "?")
            lvl_parts = [
                f"{dl} {lv}" for _, dl, lv in self._difficulty_summary(song)
            ]
            lines.append(f"{i}. {title}  -  {artist}")
            if lvl_parts:
                lines.append(f"   {' / '.join(lvl_parts)}")
        if len(results) > 20:
            lines.append(f"...以及另外 {len(results) - 20} 首")
        return "\n".join(lines)

    @staticmethod
    def _build_song_detail(song: dict) -> str:
        title = safe_str(song.get("title"), "?")
        artist = safe_str(song.get("artist"), "?")
        bpm = safe_str(song.get("bpm"), "?")
        category = safe_str(song.get("category"), "?")
        version = safe_str(song.get("version"), "?")
        release = safe_str(song.get("releaseDate"), "?")
        sid = safe_str(song.get("songId"), "?")

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
                unreleased = level == "0"
                lvl_display = "??" if unreleased else level
                internal = sheet.get("internalLevel")
                internal_str = safe_str(internal, "-")
                designer = safe_str(sheet.get("noteDesigner"), "-")
                notes = sheet.get("noteCounts", {}) or {}
                total = safe_str(notes.get("total"), "-")
                bell = safe_str(notes.get("bell"), "-")

                tp_str = TYPE_DISPLAY.get(tp, tp.upper())
                diff_str = DIFFICULTY_DISPLAY.get(diff, diff.upper())

                line = (
                    f"  [{tp_str}] {diff_str}  "
                    f"Lv.{lvl_display} (定数: {internal_str})  "
                    f"Notes: {total}  Bell: {bell}"
                )
                if unreleased:
                    line += "  [未知]"
                if designer and designer != "-":
                    line += f"  谱师: {designer}"
                lines.append(line)

        return "\n".join(lines)

    @staticmethod
    def _build_help_text() -> str:
        return (
            "オンゲキ谱面查询:\n"
            "/og <曲名>  - 查询曲目详情\n"
            "/og search <关键词>  - 搜索曲目\n"
            "/og random  - 随机推荐\n"
            "/og cover <曲名>  - 获取曲绘\n"
            "/og alias ...  - 管理别称\n"
            "/og t <曲名>  - 文字模式查询"
        )

    # ---- 命令 ----

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

        candidates = [
            s
            for s in songs
            if isinstance(s, dict) and s.get("songId") not in self._recommended_ids
        ]
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

        candidates = [
            s
            for s in songs
            if isinstance(s, dict) and s.get("songId") not in self._recommended_ids
        ]
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

        if self.config.image.enabled:
            rendered = await self._render_and_send(
                stream_id,
                lambda: self._render_search_list_image(keyword, results),
                "搜索结果图片生成失败",
            )
            if rendered:
                return True, f"搜索结果: {len(results)} 首", True

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
            if self.config.image.enabled:
                rendered = await self._render_and_send(
                    stream_id,
                    lambda: self._render_search_list_image(keyword, results),
                    "搜索结果图片生成失败",
                )
                if rendered:
                    return True, f"搜索结果: {len(results)} 首", True
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
        if self.config.image.enabled:
            rendered = await self._render_and_send(
                stream_id,
                lambda: self._render_help_image(),
                "帮助图片生成失败",
            )
            if rendered:
                return True, "显示帮助", True
        await self.ctx.send.text(self._build_help_text(), stream_id)
        return True, "显示帮助", True


def create_plugin() -> OngekiProberPlugin:
    return OngekiProberPlugin()
