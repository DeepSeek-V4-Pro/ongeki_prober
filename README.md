# オンゲキ 谱面查询器 (ongeki-prober)

查询《オンゲキ》（音击）曲目信息、谱面定数、Note 配置等，数据来源 [arcade-songs](https://dp4p6x0xfi5o9.cloudfront.net/ongeki)。

## 功能概览

- **曲目搜索与查询**：按标题、作者、ID、别称搜索曲目，单曲直接显示详情（含所有难度谱面信息），多曲返回列表
- **谱面详情**：展示 BASIC / ADVANCED / EXPERT / MASTER / LUNATIC 各难度等级、定数、Note 数、Bell 数、谱师
- **曲绘获取**：实时下载曲绘大图
- **随机推荐**：从曲库随机推荐一首，自动去重最近推荐的曲目
- **别称管理**：为曲目添加自定义别称，方便搜索
- **图片渲染模式**：可选启用 playwright 渲染，查询结果以图文卡片形式发送（自动回退文字模式）

## 前置要求

- MaiBot >= 1.0.0（推荐 1.x 系列最新版本）
- SDK >= 2.0.0（推荐 2.x 系列最新版本）
- Python >= 3.10

## 安装

### 1. 放置插件

将 `ongeki_prober/` 目录复制到 MaiBot 的 `plugins/` 目录下：

```text
plugins/
  ongeki_prober/
    _manifest.json
    plugin.py
    config.toml
    README.md
```

### 2. 安装依赖

必需依赖（aiohttp）：

```bash
uv pip install aiohttp>=3.8
```

可选依赖（playwright，用于图片渲染模式）：

```bash
uv pip install playwright>=1.40
python -m playwright install chromium
```

如果使用 `pip` 而非 `uv`，将 `uv pip install` 替换为 `pip install` 即可。

### 3. 验证安装

启动 MaiBot，发送 `/og` 或 `/og help`，应返回帮助信息。

## 配置

编辑插件目录下的 `config.toml`，修改后无需重启，MaiBot 会自动热重载。

### plugin 段

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `enabled` | bool | `true` | 是否启用插件 |
| `config_version` | string | `"1.0.0"` | 配置版本标识，用于追踪配置结构变更 |

### server 段

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `data_source_url` | string | `"https://dp4p6x0xfi5o9.cloudfront.net/ongeki"` | 曲库数据源 CDN 地址。该地址需提供 `data.json`（曲库元数据）和 `img/cover/`（曲绘） |
| `request_timeout` | int | `30` | HTTP 请求超时时间（秒）。网络状况不佳时可适当增大 |
| `data_cache_ttl` | int | `300` | 曲库数据缓存时间（秒）。缓存期内重复查询不会重新请求 CDN，减小延迟和服务器压力 |

### image 段

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `enabled` | bool | `false` | 启用图片渲染模式。需要安装 playwright 和 Chromium 浏览器。启用后查询结果以图片卡片形式发送，包含曲绘、谱面信息等，渲染失败自动回退文字模式 |

### 配置示例

```toml
[plugin]
enabled = true
config_version = "1.0.0"

[server]
data_source_url = "https://dp4p6x0xfi5o9.cloudfront.net/ongeki"
request_timeout = 30
data_cache_ttl = 300

[image]
enabled = false
```

## 命令参考

所有命令均支持 `/ongeki` 和 `/og` 两种前缀。`/og` 为简写形式。

### 曲目查询

```
/og <关键词>
```

按关键词搜索曲目，匹配标题、作者、ID、已添加的别称。单曲匹配时直接显示完整详情（含所有难度谱面信息）；多曲匹配时返回结果列表（含难度等级概览）。

**示例**：

```
/og 花たちの旅
/og 10001
/og megumin
```

### 显式搜索

```
/og search <关键词>
```

与直接查询行为一致，但使用 `search` 子命令显式调用。适合关键词可能与其他子命令冲突时使用。

**示例**：

```
/og search random
```

### 随机推荐

```
/og random
```

从曲库中随机选取一首曲目并显示详情。自动跳过最近已推荐过的曲目（最多保留 200 条历史），避免短时间内重复推荐同一曲目。当全部曲目都被推荐过后，历史自动清空重新开始。

### 曲绘获取

```
/og cover <关键词>
```

搜索曲目并返回其曲绘大图。匹配到多首时取第一首。

**示例**：

```
/og cover 花たちの旅
```

### 文字模式

所有查询命令前插入 `t` 可强制使用文字模式输出，不依赖 playwright，即使 `image.enabled = true` 也以纯文本形式返回结果。

| 命令 | 说明 |
|------|------|
| `/og t <关键词>` | 文字模式查询 |
| `/og t search <关键词>` | 文字模式搜索 |
| `/og t random` | 文字模式随机推荐 |

**示例**：

```
/og t 花たちの旅
/og t search kagura
/og t random
```

### 别称管理

别称系统允许为曲目添加自定义名称，方便用习惯用语、缩写、昵称等搜索曲目。别称数据持久化存储在插件 data 目录下的 `aliases.json` 中。

#### 添加别称

```
/og alias add <歌曲ID> <别称>
```

歌曲 ID 即数据源中的 `songId`，在查询曲目详情时可看到 `ID:` 字段。

**示例**：

```
/og alias add 10001 花花
/og alias add 10002 脑力
```

**限制**：
- 别称长度 1-30 个字符
- 一个别称只能绑定一首曲目
- 一首曲目可以绑定多个别称

#### 删除别称

```
/og alias del <歌曲ID> <别称>
```

**示例**：

```
/og alias del 10001 花花
```

#### 查看别称

```
/og alias list <歌曲ID>
```

**示例**：

```
/og alias list 10001
```

#### 别称帮助

```
/og alias
```

显示别称管理使用帮助。

### 帮助

```
/og
/og help
```

显示插件帮助信息，列出所有可用命令。

## 图片渲染模式

### 启用

1. 安装 playwright：`uv pip install playwright>=1.40 && python -m playwright install chromium`
2. 在 `config.toml` 中设置 `[image] enabled = true`

### 行为

启用后，查询和随机推荐命令的结果以 HTML 渲染的图片卡片形式发送，包含：
- 曲绘（实时从 CDN 下载）
- 曲目标题、ID
- 作者、BPM、分类、版本、追加日期
- 各难度谱面详情（难度名、等级、定数、Note/Bell 数、谱师）

### 回退机制

以下情况会自动回退文字模式：
- playwright 未安装
- 浏览器启动失败
- 曲绘下载失败
- 图片渲染或发送异常

用户无需手动干预，插件会自动降级。

## 搜索算法

输入关键词后按以下优先级匹配：

1. **精确匹配**：标题、作者、歌曲 ID（忽略大小写）、已注册别称
2. **分词模糊匹配**：将关键词按空格拆分，过滤掉单个字符后，只要任意分词出现在标题或作者中即匹配

匹配结果去重后返回，按数据源原始顺序排列。

## 数据说明

- **曲库数据**：运行时从 arcade-songs CDN 获取 `data.json`，按配置的 `data_cache_ttl` 缓存，减少重复请求
- **曲绘**：从 `data_source_url/img/cover/` 目录实时下载，支持最多 3 次重试
- **数据准确度**：取决于上游数据源 arcade-songs 的更新及时性。若发现曲目缺失或信息有误，请向 arcade-songs 反馈

## 文件结构

```text
ongeki_prober/
  _manifest.json   插件元数据（ID、版本、依赖、能力声明）
  plugin.py        插件主逻辑（904 行）
  config.toml      配置文件（用户可编辑）
  aliases.json     别称持久化数据（运行时自动生成）
  .gitignore       版本管理忽略规则
  README.md        本文件
```

### 插件 ID

`deepseek-v4-pro.ongeki-prober`

### 声明的能力

| 能力 | 用途 |
|------|------|
| `send.text` | 发送文本消息（查询结果、错误提示、帮助信息等） |
| `send.hybrid` | 发送图文混合消息（图片渲染模式下发送曲绘卡片） |
| `config.get` | 读取插件配置 |

### 声明的依赖

| 包 | 必需 | 用途 |
|----|------|------|
| `aiohttp >= 3.8` | 是 | 异步 HTTP 请求，用于获取曲库数据和曲绘 |
| `playwright >= 1.40` | 否 | HTML 渲染为图片（图片模式） |

## 常见问题

### Q: 插件加载失败，日志显示 "SDK 版本不兼容"

A: 检查 MaiBot 和 SDK 版本是否符合 manifest 中的声明范围。如需在旧版本上运行，可适当放宽 `_manifest.json` 中的 `host_application` 和 `sdk` 版本限制，但不保证功能完整性。

### Q: 查询时提示 "获取曲库数据失败"

A: 依次排查：
1. 检查服务器是否能访问 `data_source_url`（默认 CDN 地址）
2. 检查 `request_timeout` 是否过小（网络状况不佳时可增大到 60）
3. 查看 MaiBot 日志中的详细错误信息

### Q: 图片模式不生效，始终显示文字

A: 确认以下条件：
1. `config.toml` 中 `[image] enabled = true`
2. playwright 已安装：`python -m playwright install chromium` 执行成功
3. 系统支持 Chromium 无头模式运行（Linux 服务器可能需要安装额外系统依赖）

### Q: 如何迁移别称数据？

A: 别称存储在插件 data 目录下的 `aliases.json` 中（路径由 MaiBot 运行时分配）。如需迁移，找到该文件并复制到新环境的对应目录即可。

## 许可

GPL-3.0-or-later
