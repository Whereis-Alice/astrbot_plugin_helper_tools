# AstrBot 辅助工具合集

为 AstrBot 提供一组可由 LLM 主动调用、也可通过消息或命令使用的辅助能力。插件按模块组织配置，当前包含 B 站视频理解、QQ 信息、引用媒体识别、Anime1、收款码、随机语音、Steam、唤醒增强、本地壁纸和 Bot QQ 资料管理。

- 当前版本：`v0.5.2`
- AstrBot：`>=4.16,<5`
- 更新记录：[CHANGELOG.md](CHANGELOG.md)

## 功能概览

| 模块 | 主要能力 |
| --- | --- |
| B站视频理解 | 识别链接、BV/av、b23.tv、分享文本和 QQ 小程序卡片；支持 Gemini 视频分析，或默认模型结合字幕、转写和可选抽帧识图 |
| QQ 工具 | 查看用户头像、群成员资料、综合 QQ 资料 |
| 引用媒体 | 保留引用图片识图，并标明图片来源；读取被引用的小程序、音乐和分享卡片 |
| Anime1 | 查询剧集更新和观看地址 |
| 收款码 | 由命令或 LLM 在合适场景发送收款码 |
| 随机语音 | 可配置 API、命令和触发词，不限于“哈基米” |
| Steam | 按 AppID、商店链接或关键词查询游戏 |
| 唤醒增强 | 提及/关键词唤醒、屏蔽词、指令屏蔽、阻塞判断和消息防抖 |
| 本地壁纸 | 多图库随机抽图、管理员存图/删图、自动创建图库 |
| Bot QQ 资料 | 管理员修改头像、昵称、签名、状态和人格同步 |

## 安装与更新

在 AstrBot 插件管理页使用仓库地址安装或更新：

```text
https://github.com/Whereis-Alice/astrbot_plugin_helper_tools
```

更新到 `v0.5.2` 后请重载插件。AstrBot 会根据 `requirements.txt` 安装 B 站模块所需依赖；手动部署时可在插件目录执行：

```bash
pip install -r requirements.txt
```

## B站视频理解

### 支持的输入

以下内容可直接发给 bot，也可放在完整分享文案中：

```text
https://www.bilibili.com/video/BV1GJ411x7h7
https://www.bilibili.com/video/BV1GJ411x7h7?p=2
BV1GJ411x7h7
av80433022
https://b23.tv/xxxxxxx
【视频标题-哔哩哔哩】 https://b23.tv/xxxxxxx
```

QQ 中直接发送或引用哔哩哔哩小程序卡片也能识别。多 P 视频会读取链接里的 `p` 参数；没有指定时默认分析 P1。

### 两种分析方式

在 `bilibili_video.analysis_mode` 中二选一：

| 方式 | 内容来源 | 优点 | 需要注意 |
| --- | --- | --- | --- |
| `AstrBot 默认模型（字幕/转写）` | 优先读取 B 站官方字幕；没有字幕时下载音频并调用必剪语音转写；可额外抽取视频画面 | 不需要额外模型 Key；开启抽帧后可结合当前视觉模型理解场景、人物和屏幕文字 | 抽帧默认关闭；开启时当前模型必须支持图片输入，且静态画面不能完整代替连续视频 |
| `Gemini 直接视频分析` | 下载低清视频并交给 Gemini 分析画面、声音和屏幕文字 | 能理解动作、场景、画面梗和无对白内容 | 需要 Gemini API Key，会消耗 Gemini 配额并上传视频 |

两种方式的最终回复流程相同：

1. 插件只提取视频元数据、文字资料和可选画面事实。
2. 资料被加入当前 AstrBot 主 Agent 的用户上下文。
3. 当前会话的人格、历史和默认模型负责自然回复用户。

Gemini 不直接面向用户说话，也不会替换 AstrBot 人格。外部视频标题、简介、字幕和模型分析会被标为不可信资料，其中出现的提示词或命令只按视频内容处理。
插件注入的视频事实只参与当前一轮请求，不写入长期对话历史，避免大段字幕持续占用上下文。

### 默认模型抽帧识图（可选）

在 `bilibili_video.default_model.frame_vision.enabled` 开启后，插件会下载当前分 P 的视频，从开头到结尾均匀抽取 JPEG 画面，并把它们与字幕/转写一起交给当前 AstrBot 模型。这样默认模型可以同时参考文字、人物、场景、画面梗和屏幕文字。

这不是另一个模型，也不会改变人格。它要求当前 AstrBot 会话模型支持图片输入；不支持视觉输入的模型可能忽略图片或返回模型侧错误，因此此功能默认关闭。

自动解析时的抽帧图片和说明文字会以 AstrBot 临时上下文注入；LLM 工具产生的帧会由 AstrBot 临时工具缓存交给当前 Agent，结束前会标记为不保存。两种路径都不会写入会话历史，也不会带入后续聊天上下文。

### 自动触发方式

`bilibili_video.auto_parse_mode` 有三个选项：

| 选项 | 行为 |
| --- | --- |
| `跟随 AstrBot（推荐）` | 不单独唤醒 bot；消息本来会进入 LLM 时才补充视频资料，兼容 AstrBot 唤醒词、@ 和其它唤醒规则 |
| `看到视频就主动回复` | 裸链接、BV/av、分享文本和小程序卡片都会主动拉起 LLM |
| `关闭自动解析` | 不扫描用户消息，但 `understand_bilibili_video` LLM 工具仍可使用 |

推荐群聊使用“跟随 AstrBot”，避免有人只分享链接时 bot 主动插话。

### 关键配置

| 配置 | 说明 |
| --- | --- |
| `max_duration_seconds` | 当前分 P 的最大时长，默认 600 秒 |
| `max_file_size_mb` | 下载视频或音频的大小上限，默认 80 MB |
| `download_quality` | Gemini 或抽帧来源的视频下载画质，默认 360p |
| `processing_timeout_seconds` | 整条解析流水线的超时 |
| `cache_ttl_minutes` | 同一视频分析结果缓存时间 |
| `cookie` / `cookies_file` | 可选，用于登录后才能访问的视频或字幕；启动日志会校验登录状态 |
| `default_model.bcut_fallback_enabled` | 无官方字幕时是否使用必剪转写 |
| `default_model.max_transcript_chars` | 交给默认模型的字幕上限；超长时保留首段、中段和结尾 |
| `default_model.frame_vision.enabled` | 是否让默认模型结合抽帧识图，默认关闭 |
| `default_model.frame_vision.frame_count` | 均匀抽取的画面数量，默认 6，范围 1-24 |
| `default_model.frame_vision.max_frame_width` | 单张画面的最大宽度，默认 960 像素 |
| `default_model.frame_vision.jpeg_quality` | JPEG 清晰度，数值越小越清晰、文件越大，默认 5 |
| `default_model.frame_vision.max_total_size_mb` | 所有画面的总大小上限，默认 8 MB |
| `gemini.api_key` | Gemini 模式必填，也可使用环境变量 `GEMINI_API_KEY` |
| `gemini.api_base` / `gemini.model` | Gemini REST API 根地址和模型名 |
| `gemini.upload_mode` | 自动选择、File API 或内嵌 Base64 |

B 站 Cookie 属于敏感信息，不要发送到聊天中，也不要提交到仓库。`cookies_file` 需要 Netscape 格式，通常可由浏览器扩展或 yt-dlp 工具导出。

### 隐私与安全

- 只接受 B 站官方长链和已知短链域名；短链的每次跳转都会重新校验域名，避免访问任意地址。
- 默认模型回退会把音频上传到 B 站必剪转写服务。插件会把必剪返回的 HTTP 上传地址强制升级为 HTTPS，并限制为 B 站上传域名。
- Gemini 模式会把下载的视频上传到配置的 Gemini API；默认在分析完成后删除 Gemini File API 文件。
- 下载文件位于插件数据目录的临时目录，成功、失败和取消后都会清理。
- 字幕、转写和 Gemini 分析文字会缓存；自动注入的抽帧只在本轮内存中使用，工具返回的抽帧只进入 AstrBot 临时工具缓存供本轮读取；两者均不写入会话历史或插件持久缓存。

### 能力边界

- 会员、付费、地区限制、已删除或风控视频可能需要 Cookie，也可能仍无法读取。
- 必剪是 B 站的非公开稳定接口，未来失效时可关闭回退，或改用 Gemini 模式。
- 默认模型方式未开启抽帧时没有视频画面输入。仅有音乐或纯视觉内容时，转写可能为空或不足以回答画面细节。
- 抽帧只能提供若干静态时点；快速动作、镜头间细节和音画同步仍可能无法完整判断。
- Gemini API 代理必须同时兼容 `v1beta generateContent` 和 File API；只兼容 OpenAI 格式的代理不能直接使用。

## LLM 工具

| 工具名 | 作用 |
| --- | --- |
| `understand_bilibili_video` | 读取 B 站视频事实；默认模型开启抽帧时会在当前工具回合附带画面，随后自动从历史移除 |
| `get_qq_avatar` | 获取 QQ 用户头像，可把图片交给视觉模型 |
| `get_qq_group_member_info` | 获取 QQ号、QQ名、群昵称、群身份、群等级、专属头衔及 OneBot 额外字段 |
| `get_qq_profile` | 整合用户资料、群成员资料、群信息和头像 |
| `send_payment_qr` | 在转账、赞助、请客等场景发送收款码 |
| `get_anime1_updates` | 查询 Anime1 更新列表 |
| `get_anime1_watch_url` | 按 Anime1 ID 获取观看地址 |
| `send_random_voice` | 发送可配置来源的随机语音 |
| `search_steam_game` | 查询 Steam 游戏并可返回封面 |
| `set_bot_qq_profile` | 管理员会话修改 Bot QQ 资料，默认不注册 |

各工具可通过对应模块的 `llm_tool_enabled` 单独开关。`set_bot_qq_profile` 涉及账号资料修改，默认关闭；开启后仍会检查管理员权限。

## 常用命令

下面示例假设 AstrBot 全局唤醒词缀是 `/`。如果实际使用 `!`，请把开头替换为 `!`。

```text
/qq_avatar [QQ号|@用户] [40|100|140|640]
/qq_member [QQ号|@用户] [群号]
/qq_profile [QQ号|@用户] [群号]
/box [QQ号|@用户]
/payqr
/anime1_update
/anime1 [关键词] [年|月|周|日|全部] [数量]
/anime1_url <Anime1 ID>
/random_avatar
```

随机语音、Steam 和壁纸使用配置中的动态命令名：

```text
/voice_meme
/随机语音
/steam <AppID|商店链接|关键词>
/查找 <AppID|商店链接|关键词>
/778666
```

AstrBot 会先去掉全局唤醒词缀再把文本交给插件，本插件会同时检查原始消息，因此 `/指令`、其它唤醒词缀和历史双前缀写法能按各模块规则正确处理。

## 其它模块说明

### QQ 头像自动更换

开启 `qq_avatar.auto_change` 后，bot 会按 5 段 cron 从本地头像池随机选择图片并调用 OneBot `set_qq_avatar`：

```text
0 8 * * *      # 每天 8 点
0 */6 * * *    # 每 6 小时
0 9 * * 1      # 每周一 9 点
```

手动测试使用管理员命令 `/random_avatar`、`/随机头像` 或 `/换头像`。多 QQ 平台时可配置 `platform_id` 指定账号。

### 引用图片和卡片

- `reply_media_guard`：用户引用你先前发出，或者自带插件自动发出的图片时，图片仍会交给 LLM 识图，同时明确“这是你先前发出，或者自带插件自动发出的图，不是当前用户上传的图片”。
- `reply_card_reader`：提取被引用的小程序、音乐、普通分享、位置和联系人卡片的来源、标题、描述和链接，不删除原卡片，也不改变引用消息 ID。
- B 站视频模块会在上述卡片资料基础上继续解析视频本身；普通卡片仍只由引用卡片模块处理。

### 唤醒增强

- 阻塞判断：全局黑名单、冷却、QQ 机器人账号段、复读 bot 发言和可自由删改的 wakepro 默认屏蔽词。
- 指令屏蔽：可分别处理唤醒词缀指令、唤醒词缀普通消息和指令执行后的额外 LLM 回复。
- 消息防抖：同一用户短时间内连续发言可合并到上一轮请求。
- 提及唤醒：支持 `@ bot`、通用唤醒词和管理员唤醒词。
- 唤醒词位置可多选：自由触发、前缀触发、后缀触发。
- 纯引用 bot 消息默认不唤醒；同一条消息带 `@` 或唤醒词时仍可唤醒。
- 未并入 wakepro 的智能唤醒和沉默检测。

`block_keywords` 的默认列表只在旧配置迁移时补一次。初始化后可以自由删除、修改或清空，插件不会偷偷恢复。

### 本地随机壁纸

每个图库可独立配置名字、路径、抽图命令、文案和发送方式：

```text
卡比壁纸
/卡比壁纸
//卡比壁纸
存图 卡比壁纸
删图
```

- `存图 图库名` 支持随消息带图或引用图片。
- 图库不存在时会自动创建目录，并把新条目写回插件配置。
- `删图` 需引用本插件发送的图片，会根据消息 ID 到本地路径的持久化记录精准删除。
- 存图、删图和自动创建图库默认仅管理员可用。
- 开启递归扫描后，随机抽图会包含图库路径下所有子目录的图片。

### Steam 数字 AppID

`steam.appid_auto_parse_mode` 控制纯数字触发：

- `关闭`：纯数字不触发。
- `需要唤醒词缀`：默认；`/778666` 触发，裸 `778666` 不触发。
- `直接触发`：裸数字也触发。

`auto_parse_links` 只负责 `store.steampowered.com/app/...` 商店链接，两项互不混淆。

## 配置分组

| 分组 | 内容 |
| --- | --- |
| `general` | 插件总开关 |
| `bilibili_video` | B 站分析模式、触发方式、下载限制、Cookie、默认模型和 Gemini 子配置 |
| `reply_media_guard` | 引用自身图片来源标记 |
| `reply_card_reader` | 引用卡片结构化读取 |
| `wake` | 唤醒、屏蔽、阻塞和防抖 |
| `wallpaper` | 多图库、抽图、存图和删图 |
| `qq_avatar` / `qq_member` / `qq_profile` | QQ 头像、群成员和综合资料 |
| `payqr` / `anime1` / `voice` / `steam` | 各辅助工具独立配置 |
| `bot_profile` | Bot QQ 资料管理和高风险工具开关 |

## 平台与依赖

QQ 资料、群成员、自动换头像和部分壁纸删除能力依赖 OneBot/AIOCQHTTP/NapCat 一类适配器。不同实现返回的资料字段可能不同，插件会输出已知字段，并按配置附加可用的其它字段。

B 站模块依赖：

- `aiohttp`：异步 API、短链、字幕和模型请求。
- `yt-dlp`：下载 B 站视频或音频。
- `imageio-ffmpeg`：提供可随插件安装的 ffmpeg，用于合并视频音轨和提取音频。

## 故障排查

### 发送 B 站链接后 bot 不回复

检查 `bilibili_video.auto_parse_mode`。默认“跟随 AstrBot”不会让裸链接单独唤醒群聊 bot，需要使用当前 AstrBot 唤醒词、`@ bot`，或者改成“看到视频就主动回复”。

### 能取到标题，但无法理解内容

- 默认模型方式：视频可能没有字幕，且必剪回退被关闭或暂时不可用。
- Gemini 方式：检查 API Key、模型名、API 地址和额度。
- 检查视频是否超过时长、文件大小或处理超时限制。

### yt-dlp 或 ffmpeg 缺失

重载或重新安装插件，让 AstrBot 重新安装 `requirements.txt`；也可手动执行 `pip install -r requirements.txt`。日志中会给出缺失依赖的明确提示。

### 需要登录或触发 B 站风控

在 `cookies_file` 上传 Netscape 格式 `cookies.txt`，或填写 Cookie 文本。重载插件后会出现以下不含敏感内容的状态日志之一：

- `Cookie verification succeeded`：B 站确认当前为登录状态。
- `Bilibili reports not logged in`：Cookie 已读取，但已失效、不完整或不属于当前账号。
- `could not be verified`：网络或 B 站接口暂时不可用，不能据此判断 Cookie 是否失效。

Cookie 只会发送给 `bilibili.com` 的网页/API 与下载请求，不会发送到 `b23.tv` 短链。QQ 分享附带的短链追踪参数会在解析时自动清理；遇到短链 `GET 400` 时还会尝试用 `HEAD` 读取跳转地址。

## 参考与致谢

本插件对下列 MIT 开源项目的相关能力进行了参考和模块化重写，没有并入与辅助工具合集无关的订阅、登录界面、网页渲染等功能：

- Gemini 视频上传与分析流程参考 [YUMU1658/astrbot_plugin_qq_tools](https://github.com/YUMU1658/astrbot_plugin_qq_tools)，Copyright (c) 2026 YUMU1658。
- B 站识别、字幕优先、yt-dlp 下载和必剪转写流程参考 [storyAura/astrbot_plugin_biliVideo](https://github.com/storyAura/astrbot_plugin_biliVideo)，Copyright (c) 2025 storyAura。
- B 站短链的无 Cookie 展开请求策略参考 [drdon1234/astrbot_plugin_media_parser](https://github.com/drdon1234/astrbot_plugin_media_parser)。
- QQ 资料卡能力参考 [Zhalslar/astrbot_plugin_box](https://github.com/Zhalslar/astrbot_plugin_box)。
- Anime1 更新列表能力参考 [zhist2028/astrbot_plugin_anime1_list](https://github.com/zhist2028/astrbot_plugin_anime1_list)。
- 收款码能力参考 [luori7hao/astrbot_plugin_payqr](https://github.com/luori7hao/astrbot_plugin_payqr)。
- Bot QQ 资料管理能力参考 [Zhalslar/astrbot_plugin_qqprofile](https://github.com/Zhalslar/astrbot_plugin_qqprofile)。
- 随机语音能力参考 [oxoax/zhiyu-astrbot-hjm](https://github.com/oxoax/zhiyu-astrbot-hjm)。
- Steam 链接解析能力参考 [xu654/SteamLink](https://github.com/xu654/SteamLink)。
- 提及唤醒增强能力参考 [Zhalslar/astrbot_plugin_wakepro](https://github.com/Zhalslar/astrbot_plugin_wakepro)。

## 许可证

本项目使用 [MIT License](LICENSE)。
