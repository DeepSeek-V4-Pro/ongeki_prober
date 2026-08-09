# 更新日志

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [1.1.0] - 2026-08-09

### 问题修复

- **修复 LUNATIC 特殊难度曲目搜索异常**：数据源把 LUNATIC 谱面作为独立条目
  提供（songId 即曲名，如 `(LUN) MEGALOVANIA`，category 为 LUNATIC），旧版搜索
  “MEGALOVANIA”会返回两条重复结果且无法直接查看详情。现在加载曲库时自动把
  `(LUN) 曲名` 条目并入同名基础曲目（MEGALOVANIA 一次显示 BASIC~MASTER +
  LUNATIC 全部 5 个谱面）；没有同名基础曲目的纯 LUNATIC 曲目（如「怨撃」
  「No Remorse」）保持独立显示。
- **别称自动迁移**：升级后原挂在 LUNATIC 变体 ID 上的别称会在首次加载曲库时
  自动迁移到基础曲目 ID，避免旧别称“查不到”。
- **None 数值容错**：BPM、追加日、Note/Bell 数等字段为空时显示 `?` 而不是
  `None`（LUNATIC 谱面的 Note 数据多为空，旧版会渲染出 “Notes: None”）。
- **特殊谱面等级显示**：LUNATIC 等级为 `0` 的特殊谱面无法正常定级，显示为
  `Lv.?? [未知]`，不再显示误导性的 `Lv.0`。
- 数据获取异常捕获精简，缓存命中逻辑不变。

### 图片化更多消息

- 搜索结果列表在图片模式下渲染为图片卡片（标题、作者、带难度颜色的等级标签），
  超过 15 首自动折叠提示。
- `/og help` 帮助消息在图片模式下渲染为命令总览卡片。
- 曲绘下载失败不再整体回退文字模式，改为显示「曲绘缺失」占位。

### 渲染效果优化（参考 maimaidx_prober v2.0 同批修改）

- 渲染链路优先调用 MaiBot 宿主 `render.html2png` 能力，失败时回退内置
  Playwright，并默认追加 `--no-sandbox`。
- 全部图片默认 2x 设备像素比（高清）输出，可通过 `[render] device_scale_factor`
  配置。
- 图片加载等待逻辑由「仅判断 `img.complete`」改为可选的
  「`complete && naturalWidth > 0`」，避免坏图静默通过。
- 卡片样式微调（换行、溢出处理），新增 `[render]` 配置段。

### 其他

- 插件版本更新为 `1.1.0`，`_manifest.json` 新增 `send.image` 与
  `render.html2png` 能力声明。
- 新增 `requirements.txt`、`install_deps.py` 一键安装脚本与
  `Dockerfile.example` 容器构建示例。
- 新增本文件 `CHANGELOG.md`，`README.md` 同步更新。

---

## [1.0.0] - 2026-07

- 首个正式版本：连接 arcade-songs 数据源，提供曲目搜索、谱面详情、
  随机推荐、曲绘获取、别称管理与可选图片渲染模式。

[1.1.0]: https://github.com/DeepSeek-V4-Pro/ongeki_prober/releases/tag/v1.1.0
[1.0.0]: https://github.com/DeepSeek-V4-Pro/ongeki_prober/releases/tag/v1.0.0
