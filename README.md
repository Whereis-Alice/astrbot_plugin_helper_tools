# AstrBot 辅助工具合集

为 AstrBot 提供一组可由 LLM 主动调用、也可通过消息或命令使用的辅助能力。插件按模块组织配置，当前包含 B 站视频理解、QQ 信息、QQ 名片点赞、引用媒体识别、Anime1、收款码、随机语音、Steam、唤醒增强、本地壁纸和 Bot QQ 资料管理。

- 当前版本：`v0.5.9`
- AstrBot：`>=4.16,<5`
- 更新记录：[CHANGELOG.md](CHANGELOG.md)

## 功能概览

| 模块 | 主要能力 |
| --- | --- |
| B站视频理解 | 识别链接、BV/av、b23.tv、分享文本和 QQ 小程序卡片；支持管理员扫码登录、Gemini 视频分析，或默认模型结合字幕、转写和可选抽帧识图 |
| QQ 工具 | 查看用户头像、群成员资料、综合 QQ 资料 |
| QQ 名片点赞 | 自动响应“赞我”或“赞@用户”；可选由当前人设自然回复 |
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

更新到 `v0.5.9` 后请重载插件。AstrBot 会根据 `requirements.txt` 安装 B 站模块所需依赖；手动部署时可在插件目录执行：

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
| `cookie` / `cookies_file` | 可选，用于登录后才能访问的视频或字幕；也可使用下面的管理员扫码登录 |
| `qr_login` | 管理员扫码登录、私聊限制、轮询间隔、超时和凭据优先级 |
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

### 管理员扫码登录

默认开启 `bilibili_video.qr_login` 后，管理员可在**私聊**中发送：

```text
/helper_bili_login
```

插件会发送 B 站登录二维码，使用哔哩哔哩 App 扫码并在手机确认后，会自动取得登录凭据、写入插件数据目录，并立即向 B 站检查是否已登录。整个过程不会在聊天或日志里输出 Cookie。

相关管理员命令：

| 命令 | 作用 |
| --- | --- |
| `/helper_bili_login` | 获取或继续等待当前登录二维码 |
| `/helper_bili_login_status` | 不暴露 Cookie 内容地检查当前登录状态 |
| `/helper_bili_login_cancel` | 取消正在等待确认的二维码，不删除已有凭据 |
| `/helper_bili_logout` | 删除本插件扫码保存的凭据，不修改配置页的 Cookie 文本或 cookies.txt |

二维码请求会明确只请求 `gzip` / `deflate` 压缩，避免部分环境收到无法自动解压的 Brotli（`br`）数据；即使中转仍返回 `br`，插件也会使用 `Brotli` 依赖兜底解压。`qr_login.direct_retry_on_invalid_response` 默认开启：如果代理或网络中转返回网页、空内容或其它非 JSON 数据，插件会自动再试一次不使用系统代理的直连。两次都失败时，日志会明确说明是网页拦截、压缩异常、空内容还是 HTTP 拒绝，不会输出二维码内容或 Cookie。

#### 与 `astrbot_plugin_bilibili` 同时使用

本插件刻意使用 `helper_bili_*` 命名空间，不占用 `/bili_login` 和 `/bili_logout`。因此同时安装 [Soulter/astrbot_plugin_bilibili](https://github.com/Soulter/astrbot_plugin_bilibili) 时：

| 命令 | 由哪个插件处理 |
| --- | --- |
| `/bili_login`、`/bili_logout` | `astrbot_plugin_bilibili` |
| `/helper_bili_login`、`/helper_bili_login_status`、`/helper_bili_login_cancel`、`/helper_bili_logout` | 本插件 |

本插件的中文别名也都以“助手”开头，例如 `/助手B站登录`，避免与其它 B 站插件的常用中文指令重名。

二维码登录凭据保存为插件数据目录下的 `bilibili_qr_credentials.json`；支持文件权限的系统会限制为当前用户可读写。它不是加密文件，因此仍应保护 AstrBot 数据目录，不要把该文件提交、上传或分享。当前选中的凭据会同时用于 B 站网页/API、字幕转写、yt-dlp 下载、Gemini 视频分析和抽帧，不会出现“登录状态正常但下载未登录”的情况。

`qr_login.prefer_saved_credentials` 默认开启，扫码保存的凭据会优先于 `cookie` 和 `cookies_file` 使用。关闭它后，手工配置的 Cookie 会优先；在两者都不存在时才使用扫码凭据。`private_chat_only` 默认开启，关闭后管理员可以在群里发起登录，但二维码会被群成员看到，不建议这样做。

### 隐私与安全

- 只接受 B 站官方长链和已知短链域名；短链的每次跳转都会重新校验域名，避免访问任意地址。
- 默认模型回退会把音频上传到 B 站必剪转写服务。插件会把必剪返回的 HTTP 上传地址强制升级为 HTTPS，并限制为 B 站上传域名。
- 官方字幕只读取 `bilibili.com`、`hdslb.com` 和 `bilivideo.com` 的已知 B 站 CDN 地址；可信的 HTTP 地址会先升级为 HTTPS，字幕下载不会携带登录 Cookie。
- Gemini 模式会把下载的视频上传到配置的 Gemini API；默认在分析完成后删除 Gemini File API 文件。
- 下载文件位于插件数据目录的临时目录，成功、失败和取消后都会清理。
- 字幕、转写和 Gemini 分析文字会缓存；自动注入的抽帧只在本轮内存中使用，工具返回的抽帧只进入 AstrBot 临时工具缓存供本轮读取；两者均不写入会话历史或插件持久缓存。
- 二维码生成与轮询请求不会携带已有 B 站 Cookie；扫码结果只在取得 `SESSDATA` 后才会保存。遇到代理或中转返回异常页面时，默认会再进行一次不读取系统代理的直连重试。视频网页、API 和下载请求会使用当前选中的凭据，但 `b23.tv` 短链不会收到 Cookie。

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
/helper_bili_login                  # 管理员私聊扫码登录
/helper_bili_login_status           # 管理员检查登录状态
/helper_bili_login_cancel           # 管理员取消当前扫码
/helper_bili_logout                 # 管理员清除扫码保存的凭据
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

### QQ 名片点赞

`qq_like.enabled` 默认关闭。开启后，不需要注册 LLM 工具，也没有新的命令：用户直接发送 `赞我`、`给我点赞`、`赞一下我`，或发送 `赞@某人` 即可触发。`allow_astrbot_wake_prefix` 默认开启，因此全局唤醒词缀为 `/` 时，`/赞我` 也可以触发。

默认每个目标只调用一次 OneBot `send_like(times=10)`；可在 `likes_per_target` 调整为 1 至 10。原上游插件会连续调用五次，陌生人更容易撞到 QQ 的每日额度或风控。`group_whitelist`、私聊/群聊开关、冷却、最多 @ 几人均可在 `qq_like` 下单独配置。

陌生人点赞不能由插件绕过：对方可能关闭了陌生人收赞权限，机器人也可能达到 QQ 对非好友的当日额度。新模块会短暂缓存好友列表，以便把“陌生人权限限制”“额度上限”“当前适配器不支持”分别提示。需要稳定点赞时，最有效的方法仍是让对方允许陌生人点赞或添加 bot 为好友。

开启 `qq_like.persona_reply.enabled` 后，固定的“已点赞”文案会改为由当前 AstrBot 默认模型和当前人设自然回复。插件只向本轮请求附加一次点赞结果，回复完成后不会写入后续聊天历史。

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
| `qq_like` | 自动 QQ 名片点赞、陌生人限制提示和可选人设回复 |
| `payqr` / `anime1` / `voice` / `steam` | 各辅助工具独立配置 |
| `bot_profile` | Bot QQ 资料管理和高风险工具开关 |

## 平台与依赖

QQ 资料、群成员、自动换头像、名片点赞和部分壁纸删除能力依赖 OneBot/AIOCQHTTP/NapCat 一类适配器。不同实现返回的资料字段可能不同，插件会输出已知字段，并按配置附加可用的其它字段。

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

可以在 `cookies_file` 上传 Netscape 格式 `cookies.txt`、填写 Cookie 文本，或由管理员私聊执行 `/helper_bili_login` 扫码。重载插件、扫码成功后或执行 `/helper_bili_login_status` 时，会出现以下不含敏感内容的状态日志之一：

- `Cookie verification succeeded`：B 站确认当前为登录状态。
- `Bilibili reports not logged in`：Cookie 已读取，但已失效、不完整或不属于当前账号。
- `could not be verified`：网络或 B 站接口暂时不可用，不能据此判断 Cookie 是否失效。

Cookie 只会发送给 `bilibili.com` 的网页/API 与下载请求，不会发送到 `b23.tv` 短链。QQ 分享附带的短链追踪参数会在解析时自动清理；遇到短链 `GET 400` 时还会尝试用 `HEAD` 读取跳转地址。

### 扫码二维码没有发出或一直等待

- 确认 `bilibili_video.qr_login.enabled` 和 `commands_enabled` 都已开启。
- 默认只允许管理员私聊发起；群聊被拒绝时，使用私聊发送 `/helper_bili_login`，或明确关闭 `private_chat_only`。
- 二维码过期、取消或超时后，重新执行 `/helper_bili_login` 获取新二维码。
- 扫码成功但视频仍提示未登录时，执行 `/helper_bili_login_status`。若显示“暂时无法确认”，通常是服务器到 B 站的网络或风控问题；若显示“未识别为登录”，重新扫码即可。

## 参考与致谢

本插件对下列 MIT 开源项目的相关能力进行了参考和模块化重写，没有并入与辅助工具合集无关的订阅、登录界面、网页渲染等功能：

- Gemini 视频上传与分析流程参考 [YUMU1658/astrbot_plugin_qq_tools](https://github.com/YUMU1658/astrbot_plugin_qq_tools)，Copyright (c) 2026 YUMU1658。
- B 站识别、字幕优先、yt-dlp 下载和必剪转写流程参考 [storyAura/astrbot_plugin_biliVideo](https://github.com/storyAura/astrbot_plugin_biliVideo)，Copyright (c) 2025 storyAura。
- B 站短链的无 Cookie 展开请求策略参考 [drdon1234/astrbot_plugin_media_parser](https://github.com/drdon1234/astrbot_plugin_media_parser)。
- B 站二维码获取、扫码轮询和凭据保存流程参考 [Soulter/astrbot_plugin_bilibili](https://github.com/Soulter/astrbot_plugin_bilibili)，并按本插件的视频理解场景重新实现。
- QQ 名片点赞触发与 OneBot 调用思路参考 [Futureppo/astrbot_plugin_zanwo](https://github.com/Futureppo/astrbot_plugin_zanwo)，并重写为独立限流模块和可选的当前人设回复。
- QQ 资料卡能力参考 [Zhalslar/astrbot_plugin_box](https://github.com/Zhalslar/astrbot_plugin_box)。
- Anime1 更新列表能力参考 [zhist2028/astrbot_plugin_anime1_list](https://github.com/zhist2028/astrbot_plugin_anime1_list)。
- 收款码能力参考 [luori7hao/astrbot_plugin_payqr](https://github.com/luori7hao/astrbot_plugin_payqr)。
- Bot QQ 资料管理能力参考 [Zhalslar/astrbot_plugin_qqprofile](https://github.com/Zhalslar/astrbot_plugin_qqprofile)。
- 随机语音能力参考 [oxoax/zhiyu-astrbot-hjm](https://github.com/oxoax/zhiyu-astrbot-hjm)。
- Steam 链接解析能力参考 [xu654/SteamLink](https://github.com/xu654/SteamLink)。
- 提及唤醒增强能力参考 [Zhalslar/astrbot_plugin_wakepro](https://github.com/Zhalslar/astrbot_plugin_wakepro)。

## 许可证

本项目使用 [MIT License](LICENSE)。
