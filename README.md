# AstrBot 辅助工具合集

为 AstrBot 提供一组可由 LLM 主动调用、也可通过消息或命令使用的辅助能力。插件按模块组织配置，当前包含 B 站视频与专栏理解、X/Twitter 资料检索、网页浏览、环境感知、QQ 防撤回、群聊历史检索、QQ 信息、QQ 名片点赞、戳一戳互动、今日小猪、引用媒体识别、Anime1、收款码、随机语音、Steam、唤醒增强、本地壁纸和 Bot QQ 资料管理。

- 当前版本：`v0.10.8`
- AstrBot：`>=4.16,<5`
- 更新记录：[CHANGELOG.md](CHANGELOG.md)

## 功能概览

| 模块 | 主要能力 |
| --- | --- |
| B站视频理解 | 识别链接、BV/av、b23.tv、分享文本和 QQ 小程序卡片；支持管理员扫码登录、Gemini 视频分析，或默认模型结合字幕、转写和可选抽帧识图 |
| B站专栏理解 | 引用 B站专栏卡片或发送专栏链接时自动读取正文；可选分析封面图片，正文长度可配置，资料只保留当前轮 |
| X / Twitter 资料检索 | 查找公开账号、最近动态、推文和图片；区分本人发布与转推，可优先使用自建 Nitter，失败自动回退 FxTwitter，并提供 R18 过滤与可选 AI 图片审核 |
| 网页浏览 | 可选的 Playwright 只读网页读取；返回正文、标题和可选截图，默认关闭 |
| 环境感知 | 将当前时间、节假日、农历、节气、平台、可选的真实 QQ 身份和 Bot 自己的群身份作为本轮可信信息交给模型，默认关闭 |
| QQ 防撤回 | 缓存 QQ 群 OneBot 消息，撤回后转发原消息到配置目标；支持按群目标、忽略名单和管理员管理命令，默认关闭 |
| 群聊历史检索 | 让模型只检索当前 QQ 群的本地/OneBot 历史；支持关键词、时间、发送者和可选 T2I 摘要卡片，默认关闭 |
| QQ 工具 | 查看用户头像、群成员资料、综合 QQ 资料 |
| QQ 名片点赞 | 自动响应“赞我”或“赞@用户”；可选由当前人设自然回复 |
| 戳一戳互动 | 被戳后按权重反戳、人设回复、发 QQ 表情/图片/语音、可选禁言或调用其它插件；支持主动命令、定时任务和 LLM 工具，默认关闭 |
| 今日小猪 | 每日抽取固定的小猪卡片；可选查看被 @ 用户的结果和使用自定义素材库 |
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

更新到 `v0.10.8` 后请重载插件。AstrBot 会根据 `requirements.txt` 安装模块所需依赖；手动部署时可在插件目录执行：

```bash
pip install -r requirements.txt
```

网页浏览默认关闭。实际启用 `web_browser.enabled` 前，还需要在 **AstrBot 正在使用的同一 Python 环境** 执行一次：

```bash
python -m playwright install chromium
```

## 辅助工具控制台

安装或更新插件后，进入 AstrBot Dashboard 的插件详情页，打开“辅助工具控制台”页面即可使用。它是本插件的原生管理页，不会额外监听端口，也不需要单独登录，仍使用 AstrBot Dashboard 已有的鉴权。

- **概览**：显示已启用模块、当前实际可调用的 LLM 工具、本地数据占用、今日运行记录，以及 B 站登录、扫码、X/Twitter、群聊历史、壁纸图库和自动换头像任务等状态。
- **模块配置**：根据当前 `_conf_schema.json` 自动渲染全部模块。可搜索字段、只看已启用模块、展开或收起模块；模板列表、布尔开关、数值边界、选项和文件类型均会在保存时校验。
- **壁纸库**：单独管理每个图库的本地目录、随机抽图指令、文案、发送方式和子目录扫描；图片墙优先占据页面主体，搜索、排序、上传和刷新集中在紧凑工具栏，图库统计与批量管理放在“管理图库”弹窗中。手机端默认收起上传和筛选。可预览、下载、批量上传、重命名或删除图片；删除图库可选择只删配置，或在输入图库名称确认后连同磁盘目录一起删除。
- **运行记录**：查看插件的重要动作、结果、时间和群聊/私聊等匿名会话类型。默认不保存聊天正文、图片、语音、外部链接、Cookie、Token 或 API Key；可在 `【控制台与运行记录】` 中调整记录开关、保留时间和条数。只有明确开启“运行记录显示会话标识”时，才会额外保存并显示 AstrBot 会话标识，其中可能含有群号或用户号。
- **存储诊断**：只扫描本插件自己的数据目录，显示文件数量、占用、最近写入和分目录统计，不会展示文件内容、聊天原文或凭据。

敏感文本和上传文件在页面中只会显示“已配置/未配置”，不会回显明文。保存模块总开关或 `llm_tool_enabled` 后，控制台会立即同步本插件当前已注册的 LLM 工具；定时任务、浏览器、下载器、扫码和缓存等已初始化资源的设置改动，仍建议在 AstrBot 插件页重载一次。

控制台桌面侧栏采用紧凑宽度，底部的主题、刷新、保存和状态信息会固定在当前视口内；窄屏时会自动恢复为横向导航，不占用内容区宽度。

## 网页浏览（Playwright，可选）

开启 `web_browser.enabled` 后，当前会话的模型可调用 `browse_webpage` 读取公开网页。工具会返回页面标题、正文与可选的当前视口截图，随后继续按 AstrBot 当前人格自然回复。

它是只读浏览，不提供点击、输入、登录、Cookie 复用、文件下载或表单提交。每次读取都会创建新的无 Cookie 浏览器上下文，完成后立即关闭该上下文；不会把某个用户的登录状态或网页会话交给下一次调用。

### 启用方式

1. 在配置页开启 `网页浏览（Playwright） -> 启用网页浏览模块`。
2. 确认已经执行 `python -m playwright install chromium`。
3. 重载插件。模型看到网址并且用户明确需要查询、核对或总结网页时，会自行调用工具。

`browse_webpage` 没有聊天命令。它和其它 LLM 工具一样由模型按当前对话判断调用，避免群聊里单发网址就自动访问。

### 关键配置

| 配置 | 说明 |
| --- | --- |
| `enabled` | 总开关，默认关闭；关闭时工具不会注册给模型 |
| `llm_tool_enabled` | 单独控制 `browse_webpage` 是否可调用 |
| `navigation_timeout_seconds` / `wait_until` / `extra_wait_ms` | 控制网页加载等待，普通网站推荐保持“DOM 就绪” |
| `max_page_text_chars` | 单页交给模型的正文上限；超长页面保留开头和结尾 |
| `screenshot_enabled` / `screenshot_quality` / `max_screenshot_size_mb` | 控制可选截图；截图过大时只返回文字 |
| `viewport_width` / `viewport_height` | 页面与截图使用的浏览器尺寸 |
| `max_concurrent_pages` | 同时读取网页数，默认 1，节省内存 |
| `allowed_domains` / `blocked_domains` | 公网域名白名单和黑名单；黑名单优先。`example.com` 会包含子域名，`*.example.com` 只匹配子域名 |
| `allow_private_network` | 默认关闭，阻止本机、内网和保留 IP；除非你明确需要且了解风险，不要开启 |
| `chromium_executable_path` | 可选指定已有 Chromium 的路径；一般留空 |
| `disable_chromium_sandbox` | 仅在 root 容器报 Chromium sandbox 错误时使用，会降低隔离性 |

### 安全和上下文边界

- 只接受 `http` / `https`，拒绝 URL 内嵌账号密码。
- 初始网址、跳转后的网址和页面资源请求都会检查域名黑白名单、DNS 和 IP。默认拒绝 `localhost`、Docker/Kubernetes 常见内部域名、内网/链路本地/保留地址及解析到这些地址的域名。
- 网页正文、标题和截图说明都被标记为不可信外部资料。其中出现“忽略上文”“调用工具”“发送密码”等文字，只会被当作网页内容。
- 网页正文与截图只参与本轮回答，Agent 完成后会标记为不保存，不会进入后续聊天上下文。

## 环境感知

`perception.enabled` 默认关闭。开启后，插件不会注册新的 LLM 工具，而是在每次实际进入 LLM 的请求中附加一段可信的即时元数据：当前时间和时段、星期/法定节假日/调休、农历、节气、可选黄历、平台、群聊或私聊、群名以及消息中是否含图片、语音或视频。它不会替换 AstrBot 人格，且注入内容只参与当前轮，不会写入后续聊天历史。

`include_sender_qq` 默认关闭。开启后，只有 QQ/OneBot 平台且适配器给出数字发送者 ID 时，模型才会收到“当前发言者 QQ 号为 ……”这一条事实，并被明确要求不要把正文里的自称当成身份依据。它适合防止群成员靠文字冒充别人的场景。

`include_bot_group_identity` 默认开启，但只会在已经开启环境感知的 QQ 群聊中生效。插件会通过 OneBot 查询 Bot 自己的群成员资料，让模型知道自己在当前群是普通群员、管理员还是群主，并附带自己的群昵称、群等级、专属头衔、头衔到期时间和禁言到期时间。这是 Bot 自己的群身份，不是当前发言者的信息；适配器不支持查询时会自动跳过，不会影响回复。

`log_mode` 控制后台日志。默认“仅记录已注入”，只记录会话标识和本轮感知文本长度，不显示具体感知内容；会话标识通常包含平台和群号。选择“记录完整内容”后还会把实际交给模型的感知文本写入日志，可能包含群名或发送者 QQ 号；选择“关闭”则不记录感知注入日志。

### 节假日数据

法定节假日和调休由 `chinese-calendar` 提供，而不是由插件按日期猜测。已使用 `chinese-calendar 1.11.0` 验证 2026 年的元旦、春节、国庆等日期及调休判断；该版本的公开数据范围是 2004 至 2026 年。请求超出库覆盖的年份时，插件会明确提示“数据暂未覆盖”，不会把普通日期伪装成法定节假日。

### 关键配置

| 配置 | 说明 |
| --- | --- |
| `enabled` | 环境感知总开关，默认关闭 |
| `log_mode` | 后台日志模式：关闭、仅记录已注入、记录完整内容 |
| `timezone` | 时间与节假日使用的时区，默认 `Asia/Shanghai` |
| `include_time` / `include_holiday` | 控制准确时间、时段、周末、法定节假日和调休信息 |
| `include_lunar` / `include_solar_term` / `include_almanac` | 控制农历、节气和可选的民俗黄历信息 |
| `include_platform` / `include_group_name` / `include_media_types` | 控制平台、群名和媒体类型说明 |
| `include_sender_qq` | 可选提供当前消息真实 QQ 号，用于身份校验 |
| `include_bot_group_identity` | 提供 Bot 自己在当前 QQ 群的身份、群昵称、等级和专属头衔，默认开启 |
| `bot_group_identity_cache_seconds` | Bot 群身份缓存秒数，默认 `60`；设为 `0` 则每轮重新查询 |

## QQ 防撤回

`anti_revoke.enabled` 默认关闭，且只处理 `aiocqhttp`/OneBot 群聊。开启后，插件会在内存和插件数据目录中短暂保存群消息的 OneBot 原始消息段；撤回发生时，会把撤回说明和原消息内容组合成一条普通 QQ 消息发送到配置的私聊或群聊目标。支持文本、图片、语音、视频、文件、卡片和合并转发等消息段；适配器拒绝混合消息时会自动降级为同一条文字说明，不会让异常打断 AstrBot。

### 配置方式

1. 在 `【QQ 防撤回】` 中开启模块。
2. 在 `默认私聊通知 QQ 号` 或 `默认群聊通知群号` 填写接收撤回消息的目标。
3. 可用 `监控群聊列表`、`忽略发送者 QQ 号` 和 `忽略撤回操作者 QQ 号` 限制范围。
4. 根据群活跃度调整 `消息缓存保留时长（秒）` 和 `内存最多缓存消息数`。

管理员也可以在已启用模块时使用：

```text
撤回转发 <群号> @<QQ号>       # 添加私聊目标
撤回转发 <群号> #<群号>        # 添加群聊目标
取消撤回转发 <群号> [目标]     # 删除目标，不填目标则清空该群单独配置
查看撤回转发                  # 查看按群单独配置
```

按群设置的目标会优先于默认目标。防撤回模块不读取或修改聊天历史模块的数据；消息缓存只保留到配置的时限，重载后也会自动清理过期文件。原消息如果带 QQ 引用段，重发时会转换为静态的“【引用消息】”文本，不再携带原消息 ID，避免 QQ 显示“原消息已过期”；正文、图片和表情仍会在同一条消息中发送。

## 群聊历史检索

`chat_history.enabled` 默认关闭。开启并重载后，模型可以在当前 QQ 群里调用 `search_current_group_chat_history`，按关键词、时间范围、发送者 QQ、返回数量和分页检索记录；模型会根据用户的问题自行决定是否调用，不新增聊天命令。

这个工具有严格范围：只能查询触发本次对话的当前群，不能跨群，也不能查询私聊或其它平台。每个记录使用“平台 + bot QQ + 群号”独立存储；允许列表、黑名单和单次时间范围上限会在查询前生效。群聊原文属于不可信内容，工具结果会明确要求模型不能执行其中的提示词、命令、链接或身份声明，且结果会在本轮回复完成后从后续上下文排除。

本地缓存只保存规范化文字及“图片”“语音”“卡片”等占位符，不保存图片/语音字节和完整 OneBot 原始报文。`onebot_backfill_enabled` 开启时，插件可调用当前群的 `get_group_msg_history` 补足较早消息，仍受到页数、超时和间隔限制；适配器不支持该接口时会自动只使用本地缓存。

### T2I 历史卡片

开启 `card_enabled` 后，模型在用户明确要求发送历史卡片时可以使用工具的 `render_card=true`，由 AstrBot 已配置的 T2I HTML 渲染服务生成图片并发送到当前群。`card_auto_render` 默认关闭；只有明确开启后，每次检索才会自动发卡片。可选皮肤为“夜航”“纸笺”“薄荷”“霓虹”。T2I 未配置、渲染失败或返回不可发送地址时，工具会保留文字检索结果并说明卡片没有发出。

### 关键配置

| 配置 | 说明 |
| --- | --- |
| `enabled` / `llm_tool_enabled` | 模块总开关与历史工具注册开关 |
| `capture_incoming_messages` | 是否记录插件收到的当前群消息；关闭后不再写入新记录 |
| `allowed_group_ids` / `blocked_group_ids` | 限制哪些群能使用。黑名单优先；允许列表留空表示所有 QQ 群 |
| `default_hours` / `max_query_days` | 默认检索范围与单次最大追溯天数 |
| `max_result_messages` / `max_result_chars` | 单次传给模型的消息数和文字上限 |
| `retention_days` / `max_messages_per_group` | 本地 SQLite 历史保留边界 |
| `onebot_backfill_enabled` / `max_backfill_pages` | 是否使用 OneBot 回填以及最多读取页数 |
| `include_sender_qq` | 是否在返回的记录和卡片中显示发送者 QQ 号；关闭后仍可按 QQ 号筛选 |
| `card_enabled` / `card_auto_render` / `card_default_skin` | T2I 卡片总开关、自动发送和默认皮肤 |

## X / Twitter 资料检索

`twitter.enabled` 默认关闭。开启后，当前 AstrBot 人格可以调用工具查公开账号、最近推文、指定推文和关键词结果；用户发送 `x.com`、`twitter.com` 链接时，也能按配置自动把资料交给当前会话再自然回复。它不会替换人格，也不会保存 X 的登录 Cookie。

### 使用自建 Nitter

你的 Nitter 可以直接作为优先数据源。配置页依次设置：

1. 开启 `【X / Twitter 资料检索】 -> 启用 X / Twitter 模块`。
2. 在 `自建 Nitter 服务地址` 填写 **AstrBot 进程实际能访问** 的地址。
3. 保持数据来源为“自动（优先 Nitter，失败回退 FxTwitter）”。

如果 Nitter 与 AstrBot 在同一台宿主机，端口是 `8585`，填写：

```text
http://127.0.0.1:8585
```

如果 AstrBot 和 Nitter 分别运行在 Docker 容器中，`127.0.0.1` 指向的是 AstrBot 容器自身，通常应填写同一 Docker 网络里的服务名，例如：

```text
http://nitter:8080
```

自动模式会先读取 Nitter；Nitter 超时、返回异常页、搜索功能不可用或数据无法识别时，自动改用 FxTwitter。选择“仅 Nitter”后不会走备用源，适合必须只经由自建实例访问的部署。

### 自动触发与命令

| `auto_parse_mode` | 效果 |
| --- | --- |
| `跟随 AstrBot（推荐）` | 只有消息原本因 @、唤醒词等规则进入 LLM 时，才补充链接对应的推文资料。不会因为群友单发链接而插话。 |
| `看到 X/Twitter 链接就主动回复` | 裸 `x.com` / `twitter.com` / 已配置 Nitter 链接也会调用当前人格回复。现有唤醒屏蔽和指令屏蔽仍优先。 |
| `关闭自动解析` | 不读取用户消息里的链接，但 LLM 工具和下面的命令仍可用。 |

命令都使用 `helper_x_` 前缀，避免与已安装的 Twitter/X 插件冲突：

```text
/helper_x_search <关键词或 from:用户名>
/helper_x_account <@用户名、主页链接或名称>
/helper_x_recent <用户名> [数量]
/helper_x_post <x.com/twitter.com/Nitter 推文链接或数字 ID>
```

`helper_x_search` 和 `helper_x_account` 最多接收六个以空格分开的词；需要更复杂的条件时，推荐让 LLM 直接调用工具，例如“查一下某位 VTuber 最近的推文”。

### 本人发布与转推

`include_reposts_by_default` 默认关闭。查询账号最近动态或使用 `from:用户名` 搜索时，插件会读取 `reposted_by`、Nitter 转推标记，并校验帖子作者是否与目标账号一致；转推会被排除，避免把其他作者的图片当成目标画师作品。每条保留结果都会明确标注“作者本人发布”；允许转推时则会同时标注转推账号和原作者。

部分数据源会把原生转推退化成“目标账号作为作者，正文以 `RT @原作者:` 开头”的结果。插件会在缺少结构化转推标记时识别这种格式，还原转推账号和原作者并去掉正文中的 RT 前缀；只有正文开头的完整格式会触发，普通正文中间讨论 `RT @...` 不会被误判。

LLM 工具 `get_x_recent_posts` 和 `search_x_posts` 另有 `include_reposts` 参数。模型在查找画师本人作品时必须保持关闭；只有用户明确要求查看转推、推荐或分享内容时才会临时开启。后台开启 `include_reposts_by_default` 后，命令和未指定该参数的工具调用都会默认包含转推。

关闭默认包含转推后，“默认返回条数”指过滤后的最终条数，转推不会占名额。若第一页过滤后不足，插件会通过 Nitter 的“加载更多”链接或 FxTwitter 的底部游标继续读取更早结果；R18 文字或平台敏感标记过滤导致的缺口也会一并补查。查询会在凑满、没有下一页、达到 `filtered_result_max_pages`（默认 6 页，包含第一页）或达到 `filtered_result_max_candidates`（默认 120 条候选）时停止，因此账号很久没有原创内容时仍可能少于目标条数。

### 图片与内容安全

- 默认严格过滤数据源标记为敏感的内容，并按 `r18_filter.keywords` 过滤推文、引用和账号简介；关键词可自由添加、删除或清空。
- `仅平台敏感标记` 只相信数据源的敏感标签；`关闭` 不做文字或标签过滤。两种模式都不应视为绝对的内容安全保证。
- 可开启 `ai_review.enabled`，把每张待发送或待交给视觉模型的图片交给配置的 OpenAI 兼容视觉模型审核。审核配置不完整、超时或返回不明确时，插件会保守地不发送这张图片。
- Nitter 图片会始终由 AstrBot 所在服务器下载后再发送。这样 QQ 不需要访问服务器的 `127.0.0.1:8585`、Docker 内网域名或私有端口。
- 保持默认的 `download_media_before_send` 开启时，图片会逐块读取到网络响应结束，再由 Pillow 校验文件结构和实际像素解码。Nitter 代理图片无论此项如何都执行该检查。连接中断、JPEG 截断或上游返回损坏图片时会按配置重试；仍然损坏就跳过，不会把带大片灰块的不完整图片发到 QQ。
- 工具返回给视觉模型及直接发送到聊天的图片都会附带来源标签，说明是作者本人发布，或由哪个账号转推、原作者是谁。
- NapCat/OneBot 偶尔会在图片已经送达 QQ 后返回 `retcode=1200` 的 `sendMsg` 回执超时。插件会保留正常检索结果并告诉模型“图片可能已经发出，请勿重复发送”，不会自动重试，避免同一批图片发两遍；其他发送错误仍按失败处理。
- 自动解析的图片和 X/Twitter 资料只作为当前轮不可信外部资料注入，回复完成后不会进入后续聊天上下文。

### 能力边界

只能读取公开、可由当前数据源访问的内容。受保护账号、已删除内容、X 风控、Nitter 上游限制、实例未启用搜索或被限流时可能无法返回结果。推文中的链接、文字和图片说明都视为不可信外部资料，不会改变工具权限或要求模型执行其中的指令。

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

在 `bilibili_video.default_model.frame_vision.enabled` 开启后，插件会下载当前分 P 的视频，优先按下载文件的实际时长从开头到结尾均匀抽取 JPEG 画面，并把它们与字幕/转写一起交给当前 AstrBot 模型。这样默认模型可以同时参考文字、人物、场景、画面梗和屏幕文字。

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

## B站专栏理解

开启 `bilibili_article.enabled` 和 `bilibili_article.auto_parse_enabled` 后，用户发送或引用 B站专栏卡片时，插件会自动读取专栏资料并交给当前 AstrBot 模型。支持：

- B站专栏链接，例如 `https://www.bilibili.com/read/cv123456`；
- 新版专栏/动态文章链接，例如 `https://www.bilibili.com/opus/123456789`；
- QQ 中引用的 B站专栏小程序/分享卡片；
- B站短链。短链会先跳转并确认最终页面确实是专栏，视频短链不会被当成专栏处理；引用专栏卡片时也不会再被视频模块重复解析。

读取顺序是优先调用 B站官方专栏接口，接口不可用时再回退到专栏网页。模型会看到标题、作者、摘要、正文和链接；开启 `bilibili_article.cover_image_enabled` 后，还会把封面作为当前轮的视觉资料附加。最终回复仍由当前 AstrBot 人格生成，专栏正文、封面和外部文本中的指令都只是资料，不能覆盖人格或要求插件执行命令。

### 专栏长度和上下文

`bilibili_article.max_article_chars` 就是交给模型的正文长度上限，默认 `20000`，可配置范围为 `1000-100000`。超过上限时保留正文开头和结尾，中间插入省略标记。这个限制只影响当前轮的分析资料，不会截断或修改 B站原文。

专栏正文和封面都标记为临时上下文，当前请求结束后不会写入后续聊天历史。若没有权限、接口失败、网页结构变化或封面读取失败，插件会保留能取得的卡片/链接信息并降级，不会把失败信息伪装成正文。

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
| `browse_webpage` | 读取公开网页标题、正文和可选截图；默认关闭，网页资料只在当前轮保留 |
| `understand_bilibili_video` | 读取 B 站视频事实；默认模型开启抽帧时会在当前工具回合附带画面，随后自动从历史移除 |
| `find_x_account` | 按用户名、主页链接或名称查找公开 X/Twitter 账号，适合定位画师、VTuber、公司或个人 |
| `get_x_post` | 读取一条公开 X/Twitter 推文；按需返回或发送通过安全过滤的图片 |
| `get_x_recent_posts` | 读取指定公开账号的最近动态；默认只看本人发布，可按需包含转推并返回安全图片 |
| `search_x_posts` | 用关键词或 `from:用户名` 检索公开推文；默认排除转推并标明图片原作者 |
| `search_current_group_chat_history` | 只检索当前 QQ 群的历史记录；支持关键词、时间、发送者、分页和按需发送 T2I 摘要卡片，默认关闭 |
| `get_qq_avatar` | 获取 QQ 用户头像，可把图片交给视觉模型 |
| `get_qq_group_member_info` | 获取 QQ号、QQ名、群昵称、群身份、群等级、专属头衔及 OneBot 额外字段 |
| `get_qq_group_member_list` | 获取群成员列表；可按 QQ号、昵称、群昵称、头衔等筛选并分页，返回每位成员的可用详情 |
| `get_qq_group_info` | 获取群名称、人数、等级、备注、建群时间、群头像，以及可选的成员统计、群荣誉、@全体成员配额 |
| `get_qq_profile` | 整合用户资料、群成员资料、群信息和头像 |
| `poke_qq_user` | 在当前 QQ 会话戳指定用户，默认随戳一戳模块关闭，并受次数上限约束 |
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
/rollpig
/今日小猪
/今日小猪 @用户                 # 需开启 rollpig.allow_mentioned_user
/戳 @用户 2
/戳我
/戳全体成员                     # 仅管理员
/payqr
/anime1_update
/anime1 [关键词] [年|月|周|日|全部] [数量]
/anime1_url <Anime1 ID>
/random_avatar
/helper_bili_login                  # 管理员私聊扫码登录
/helper_bili_login_status           # 管理员检查登录状态
/helper_bili_login_cancel           # 管理员取消当前扫码
/helper_bili_logout                 # 管理员清除扫码保存的凭据
/helper_x_search <关键词或 from:用户名>
/helper_x_account <用户名、主页链接或名称>
/helper_x_recent <用户名> [数量]
/helper_x_post <推文链接或数字 ID>
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

### QQ 群成员与群详情

`qq_member` 现在注册三个互不冲突的 LLM 工具：`get_qq_group_member_info` 用于查单个成员，`get_qq_group_member_list` 用于查成员列表，`get_qq_group_info` 用于查群详情。后两项默认使用当前 QQ 群；需要查 Bot 已加入的其它群时可明确传入群号。

成员列表包含 OneBot 可提供的 QQ号、QQ昵称、群昵称、群身份、群等级、专属头衔、性别、年龄、地区、入群/最后发言/禁言/头衔到期时间，以及适配器额外返回的字段。它支持 `keyword`、`offset`、`limit`，大群会提示下一页的 `offset`，不会一次把全群资料塞满模型上下文。

群详情会读取标准 `get_group_info` 资料，并可选读取成员统计、`get_group_honor_info` 群荣誉和 `get_group_at_all_remain` 的当前 Bot 账号配额。它还可按群号构造 QQ 公开群头像地址，并把头像图片交给视觉模型查看；在 `qq_member.group_info_include_avatar` 关闭、模型不需要图片或下载失败时，仍会返回群头像 URL 和其它群资料。群荣誉与 `@全体` 配额取决于 OneBot/AIOCQHTTP/NapCat 实现，适配器不支持时会明确说明而不影响其它群资料。

这里的“QQ昵称”来自 OneBot 返回的公开/群内昵称字段，不等同于 QQ 实名信息；插件不会尝试获取或推断实名认证姓名。可在 `qq_member` 配置里调整单页人数、单次硬上限、文本上限，以及群详情是否默认附带统计、荣誉和 `@全体` 配额。

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

陌生人点赞不能由插件绕过：即使对方已开启“允许陌生人点赞”，QQ 仍可能对机器人账号做无提示风控。此时 OneBot/适配器可能返回正常，但对方资料卡不会增加点赞数；插件也没有读取对方实际收赞数的接口。对陌生人或无法确认好友关系的目标，插件会明确提示“请求已提交，到账未核验”，不会再把它说成已实际收到。需要稳定点赞时，添加 bot 为好友比反复重试可靠得多。

开启 `qq_like.persona_reply.enabled` 后，固定的“已点赞”文案会改为由当前 AstrBot 默认模型和当前人设自然回复。插件只向本轮请求附加一次点赞结果，回复完成后不会写入后续聊天历史。

### 今日小猪

`rollpig.enabled` 和 `rollpig.commands_enabled` 均默认开启。用户使用 `/rollpig`、`/今日小猪`、`/抽小猪` 或 `/我的小猪` 即可抽取当天结果；同一用户在同一天反复查询会得到同一只小猪，按 `rollpig.timezone` 跨天后自动刷新。

开启 `rollpig.allow_mentioned_user` 后，可在群里使用 `/今日小猪 @某人` 查看对方当天的结果。该功能直接读取 QQ 的真实 @ 消息段，因此不会再出现上游插件“明明 @ 了人却还是查自己”的问题。`mention_target_in_group` 控制回复时是否 @ 目标，`protect_admins` 可阻止查看管理员的结果。

素材、图片和字体已随插件本地打包，生成的卡片会短暂缓存到插件数据目录。`card_cache_days` 控制保留天数。需要自定义时，上传 `custom_catalog_file` JSON，格式为对象列表，每项包含 `id`、`name`、`description`、`analysis`；图片目录可在 `custom_image_dir` 指定，图片名称使用对应 `id`，例如 `test-pig.png`。找不到自定义图片时会回退到内置图片。

图片渲染会把所有尺寸、字体字号和坐标转换成整数，因此已修复上游在部分环境中报出的 `'float' object cannot be interpreted as an integer`。渲染失败时会自动降级为原图和文字说明，不会中断命令处理。

本模块与原独立插件使用相同的命令名。更新到本版本后，请在 AstrBot 中禁用或卸载 `astrbot_plugin_rollpig`，避免两个插件同时处理 `/今日小猪` 等命令。

### 戳一戳互动

`poke.enabled` 默认关闭。开启后，用户戳 Bot 时会从当前可用且权重大于 0 的动作中随机选择一个；实际概率为本动作权重除以所有可用动作的权重总和。图片、语音或命令池为空时，对应动作会自动退出候选，不会因空配置抛异常。

可选动作包括：

- 反戳用户 1 至配置上限次。
- 使用当前会话的默认模型、上下文和 AstrBot 人格自然回复；内部提示和本次回复只参与当前轮，不伪装成用户消息留在后续历史中。戳一戳触发模型时还会明确本次触发者的 QQ 号和昵称，并提醒模型：群聊历史中的“用户”消息可能来自其它成员，不能把其它成员的话归到本次触发者名下。
- 发送随机 QQ 表情、本地图片或本地语音；图片和语音目录可递归扫描。
- 在 Bot 具备足够群权限时禁言触发者，再由当前人格回复；默认权重为 0，权限不足会明确按失败事实回复。
- 在群聊中随机调用 `command_reply.commands` 的一条命令，并把原先戳一戳的用户作为真实 @ 目标和命令发送者交给其它插件。默认列表包含 `怒撕`、`咖波撕` 等可供 `astrbot_plugin_memelite` 使用的命令，也可自由删除或替换。

随机命令不会先向 QQ 发送一条可见文字，而是作为内部事件交给 AstrBot 的插件命令系统。因此目标插件生成的表情包或其它结果会正常发出，同时不会额外触发默认 LLM。配置里可以写 `怒撕`、`/怒撕` 或当前 AstrBot 的其它唤醒词缀写法，插件会规范化后再分发。

后台日志会记录这条动作的完整调度状态：先记录选中的命令，再记录是否成功加入 AstrBot 事件队列；如果目标插件处理函数抛出异常，还会记录目标插件名、处理函数名和异常原因。日志中的 `command_reply success` 表示命令事件已经成功交给 AstrBot 处理，不代表目标插件一定生成了图片；目标插件异常时会另有 `command_reply failed` 日志，便于区分入队失败和插件自身报错。

为解决原插件把 `怒撕` 一类内部命令记成用户发言的问题，本模块会把合成事件标记为戳一戳模块的内部命令，并在本地群聊历史中跳过该事件；但在下游命令真正处理时，事件发送者保持为原始戳一戳用户，因此依赖 `get_sender_id()`、`get_sender_name()` 或 OneBot `sender` 的指令会拿到正确参数。如果目标插件显式调用 Agent，模型会收到“这是戳一戳模块自动发起的内部命令，不是群成员手动发送”的本轮归属说明，相关用户/助手消息也会在保存前标为临时，不进入下一轮上下文。

主动能力：

```text
/戳 @用户 2
/戳我
/戳全体成员
```

- 普通 `/戳` 一次最多处理 `outgoing.max_direct_targets` 位目标；`/戳全体成员` 仅管理员可用，人数超过 `outgoing.max_group_targets` 时随机抽取，并按 `interval_seconds` 控制调用间隔。
- `poke_qq_user` 允许 LLM 在当前 QQ 会话按需戳明确的数字 QQ 号；不能戳自己，也不能突破 `outgoing.max_times`。
- 关键词主动戳默认关闭；开启后建议保留“必须先正常唤醒 Bot”，避免普通群聊误触发。
- 定时戳默认关闭且不预置任何群号或 QQ 号。目标格式为 `群号:QQ号`，Cron 使用 AstrBot 的五段“分 时 日 月 周”格式。
- 随机调用其它插件命令仅在群聊参与候选。私聊适配器通常依赖发送者 ID 决定回复目标，把作者改成 Bot 后可能误发给自己，因此私聊会自动选择其它回复动作。
- 管理员可使用 `/戳命令列表`（别名：`/导出戳命令`、`/随机命令列表`）导出当前实际可随机调用的命令。输出不带标题，每行一个命令；插件会自动去掉配置中的 AstrBot 唤醒词缀并去重，复制输出即可作为新的命令列表参考。

本模块与 `astrbot_plugin_pokepro` 都会监听同一类事件。迁移完成后应禁用或卸载原独立插件，避免一次戳一戳得到两份回复。

### 引用图片和卡片

- `reply_media_guard`：用户引用你先前发出，或者自带插件自动发出的图片时，图片仍会交给 LLM 识图，同时明确“这是你先前发出，或者自带插件自动发出的图，不是当前用户上传的图片”。
- 对于其它插件自动发送后、引用段里丢失发送者或图片链的情况，插件会按引用消息 ID 调用 OneBot `get_msg` 回查。来源说明会在消息阶段尽早写入，并在真正发送 LLM 请求前再次作为仅本轮内容注入，避免适配器晚加载引用详情时又把旧图误当成当前用户上传。
- `onebot_lookup_enabled` 默认开启；可用 `max_onebot_lookups_per_message`、`onebot_lookup_timeout_seconds` 和缓存项限制回查开销。适配器不支持 `get_msg` 时，仍会使用引用段本身已经提供的发送者和图片信息。
- `reply_card_reader`：提取被引用的小程序、音乐、普通分享、位置和联系人卡片的来源、标题、描述和链接，不删除原卡片，也不改变引用消息 ID。
- B 站视频模块会在上述卡片资料基础上继续解析视频本身；普通卡片仍只由引用卡片模块处理。

### 唤醒增强

- 阻塞判断：全局黑名单、冷却、已知 QQ 机器人账号、复读 bot 发言和可自由删改的 wakepro 默认屏蔽词。QQ 机器人判断会优先读取 OneBot 原始事件中的真实用户号，避免其它插件改写发送者信息后误拦截普通用户。
- 指令屏蔽：可分别处理唤醒词缀指令、唤醒词缀普通消息和指令执行后的额外 LLM 回复；完整命令名、别名、多词命令和命令组都会识别，第三方插件错误改写 LLM 开关时也会在最终请求前拦截。
- 消息防抖：同一用户短时间内连续发言可合并到上一轮请求。
- 提及唤醒：支持 `@ bot`、通用唤醒词和管理员唤醒词。
- 唤醒词位置可多选：自由触发、前缀触发、后缀触发。
- 无文字唤醒：可用 `empty_wake_response_enabled` 控制只 `@ bot`、只发一个完整唤醒词或只发 AstrBot 全局唤醒词缀时是否仍回复；`empty_wake_prompt` 可自定义本轮给模型的提示。该提示只参与当前轮，不会写入后续聊天历史。只发完整唤醒词时同样遵守自由、前缀、后缀三种已勾选的触发方式。
- 纯引用 bot 消息默认不唤醒；同一条消息带 `@` 或唤醒词时仍可唤醒。
- 私聊默认不使用唤醒增强，而是完全交给 AstrBot 原生私聊流程，避免群聊防抖、冷却和屏蔽规则造成偶发无回复。确实需要私聊也应用这些规则时，开启 `wake.apply_to_private_messages`；全局黑名单不受此开关影响。
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

### 控制台图库管理

在“辅助工具控制台”的“壁纸库”页可以集中管理本地图片，无需手动编辑长列表配置：

- 查看每个图库的图片数量、占用空间、目录状态、最近修改时间和是否递归扫描。
- 按文件名或子目录搜索，可按最近修改、文件名或文件大小排序；图片会显示尺寸、格式、帧数、大小和修改时间。
- 支持原图预览和下载，以及批量上传、重命名和删除。上传会使用壁纸模块现有的扩展名、单图大小和去重设置；控制台仅处理 JPG、PNG、WebP、GIF，且会校验真实图片格式。
- 新增或编辑图库时可直接设置名称、目录、随机抽图指令、文案、发送方式和“扫描子目录”。目录不存在时可勾选自动创建；删除图库可选择“仅删配置，保留磁盘文件”，或“删除配置和图库目录中的全部文件”。后者必须输入完全一致的图库名称确认，且会拒绝插件数据根目录、`wallpapers` 总目录、符号链接和与其它图库重叠的路径。
- 图片删除或改名会同步本插件已发送图片的本地记录，避免之后引用该图使用“删图”时指向错误文件。图库操作仍走 AstrBot Dashboard 的登录鉴权，不会额外开放端口。
- 图片浏览页不会常驻占用大面积的统计和图库管理区：桌面端会优先显示图片墙，点击“管理图库”才打开完整统计和图库列表；手机端的“上传与筛选”默认收起，需要时再展开。

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
| `perception` | 当前时间、节假日、农历、平台、真实 QQ 身份和 Bot 自己的群身份感知 |
| `chat_history` | 当前 QQ 群历史检索、本地保留、OneBot 回填和可选 T2I 卡片 |
| `bilibili_video` | B 站分析模式、触发方式、下载限制、Cookie、默认模型和 Gemini 子配置 |
| `bilibili_article` | B 站专栏自动读取、封面视觉资料、正文长度限制和缓存 |
| `twitter` | X/Twitter 数据源、Nitter、自动解析、图片、R18 过滤和可选 AI 审核 |
| `reply_media_guard` | 引用自身图片来源标记 |
| `reply_card_reader` | 引用卡片结构化读取 |
| `wake` | 唤醒、屏蔽、阻塞和防抖 |
| `wallpaper` | 多图库、抽图、存图和删图 |
| `qq_avatar` / `qq_member` / `qq_profile` | QQ 头像、单成员资料、群成员列表、群详情和综合资料 |
| `qq_like` | 自动 QQ 名片点赞、陌生人限制提示和可选人设回复 |
| `rollpig` | 今日小猪、查看被 @ 用户、自定义素材和卡片缓存 |
| `poke` | 被戳随机回复、主动戳、随机插件命令、LLM 工具和定时戳 |
| `payqr` / `anime1` / `voice` / `steam` | 各辅助工具独立配置 |
| `bot_profile` | Bot QQ 资料管理和高风险工具开关 |

## 平台与依赖

QQ 资料、群成员、自动换头像、名片点赞、戳一戳和部分壁纸删除能力依赖 OneBot/AIOCQHTTP/NapCat 一类适配器。不同实现返回的资料字段或动作支持可能不同，插件会输出已知字段，并按配置附加可用的其它字段。

B 站模块依赖：

- `aiohttp`：异步 API、短链、字幕和模型请求。
- `yt-dlp`：下载 B 站视频或音频。
- `imageio-ffmpeg`：提供可随插件安装的 ffmpeg，用于合并视频音轨和提取音频。
- `Pillow`：生成今日小猪图片卡片。
- `beautifulsoup4`：解析自建 Nitter 返回的公开页面。
- `chinese-calendar`：中国法定节假日和调休判断。
- `lunar-python`：农历、节气和可选黄历信息。

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

### Nitter 没有被使用或连接失败

- `twitter.nitter_base_url` 必须是 AstrBot 容器或进程能访问的地址，而不是你自己浏览器能访问的地址。AstrBot 与 Nitter 分容器时，`127.0.0.1:8585` 通常无效，使用服务名和容器端口，例如 `http://nitter:8080`。
- 查看插件日志中是否出现 `via nitter failed; trying fallback`。这表示自动模式已经尝试 Nitter，并正常切到 FxTwitter；可检查 Nitter 容器、上游访问和实例搜索功能。
- 只想检查 Nitter 时选择“仅 Nitter”。若还是失败，先在 AstrBot 同一环境执行 `curl http://127.0.0.1:8585` 或访问你实际配置的 URL，确认网络路径。
- Nitter 的图片会先由插件下载。若日志提示媒体下载失败，检查 Nitter 的 `/pic/` 代理路径和服务器到上游媒体的网络。
- 如果日志出现 `Nitter HTTP 404 for http://127.0.0.1:8585/pic`，通常不是 Nitter 没启动，而是把 Nitter 的图片代理地址误当成了账号地址；新版会拒绝这类参数，不再请求 `/pic`。

## 参考与致谢

本插件对下列开源项目的相关能力、交互或流程进行了参考和模块化重写，没有并入与辅助工具合集无关的订阅、登录界面、网页渲染等功能：

- Gemini 视频上传与分析、Playwright 浏览器配置和 SSRF 防护思路参考 [YUMU1658/astrbot_plugin_qq_tools](https://github.com/YUMU1658/astrbot_plugin_qq_tools)，Copyright (c) 2026 YUMU1658。本插件的网页模块按辅助工具场景独立重写为无状态只读读取，不并入点击、输入和浏览器会话控制能力。
- X/Twitter 账号、推文和媒体检索的产品场景参考 [Ars1027/astrbot_plugin_twitter](https://github.com/Ars1027/astrbot_plugin_twitter)。该上游仓库采用 AGPL-3.0；本插件没有复制或并入其代码，实现为独立的 Nitter/FxTwitter 数据源模块。
- 环境感知的产品场景参考 [miaoxutao123/astrbot_plugin_LLMPerception](https://github.com/miaoxutao123/astrbot_plugin_LLMPerception)。该上游仓库采用 AGPL-3.0；本模块独立实现，使用可验证的节假日、农历和节气数据，不复制或并入其代码。
- 当前群聊历史工具的产品场景参考 [kawayiYokami/astrbot_plugin_angel_eye](https://github.com/kawayiYokami/astrbot_plugin_angel_eye)。该上游仓库采用 AGPL-3.0；本模块独立实现为有范围和保留上限的本地 SQLite + OneBot 回填方案，不复制或并入其代码。
- QQ 防撤回的产品场景参考 [Foolllll-J/astrbot_plugin_anti_revoke](https://github.com/Foolllll-J/astrbot_plugin_anti_revoke)。该上游仓库采用 AGPL-3.0；本模块独立实现了新版 AstrBot/OneBot 事件兼容、原始消息缓存和失败降级，不复制或并入上游源码。
- B 站识别、字幕优先、yt-dlp 下载和必剪转写流程参考 [storyAura/astrbot_plugin_biliVideo](https://github.com/storyAura/astrbot_plugin_biliVideo)，Copyright (c) 2025 storyAura。
- B 站短链的无 Cookie 展开请求策略参考 [drdon1234/astrbot_plugin_media_parser](https://github.com/drdon1234/astrbot_plugin_media_parser)。
- B 站二维码获取、扫码轮询和凭据保存流程参考 [Soulter/astrbot_plugin_bilibili](https://github.com/Soulter/astrbot_plugin_bilibili)，并按本插件的视频理解场景重新实现。
- QQ 名片点赞触发与 OneBot 调用思路参考 [Futureppo/astrbot_plugin_zanwo](https://github.com/Futureppo/astrbot_plugin_zanwo)，并重写为独立限流模块和可选的当前人设回复。
- 今日小猪素材库、图片和原始玩法参考 [MegSopern/astrbot_plugin_rollpig](https://github.com/MegSopern/astrbot_plugin_rollpig)，Copyright (c) 2025 Bear_lele、MegSopern；本插件重写了缓存、@ 目标解析和图片渲染。完整许可证声明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
- 戳一戳互动的产品场景参考 [Zhalslar/astrbot_plugin_pokepro](https://github.com/Zhalslar/astrbot_plugin_pokepro)。该上游仓库采用 GPL-3.0；本模块按照 AstrBot 事件接口独立实现，没有复制或并入其源码，并重新设计了安全默认值、内置 Cron、OneBot 兼容和内部命令归属保护。与 `astrbot_plugin_memelite` 的默认命令仅使用其公开、MIT 许可的命令关键词。
- QQ 资料卡能力参考 [Zhalslar/astrbot_plugin_box](https://github.com/Zhalslar/astrbot_plugin_box)。
- Anime1 更新列表能力参考 [zhist2028/astrbot_plugin_anime1_list](https://github.com/zhist2028/astrbot_plugin_anime1_list)。
- 收款码能力参考 [luori7hao/astrbot_plugin_payqr](https://github.com/luori7hao/astrbot_plugin_payqr)。
- Bot QQ 资料管理能力参考 [Zhalslar/astrbot_plugin_qqprofile](https://github.com/Zhalslar/astrbot_plugin_qqprofile)。
- 随机语音能力参考 [oxoax/zhiyu-astrbot-hjm](https://github.com/oxoax/zhiyu-astrbot-hjm)。
- Steam 链接解析能力参考 [xu654/SteamLink](https://github.com/xu654/SteamLink)。
- 提及唤醒增强能力参考 [Zhalslar/astrbot_plugin_wakepro](https://github.com/Zhalslar/astrbot_plugin_wakepro)。

## 许可证

本项目使用 [MIT License](LICENSE)；已打包的第三方素材许可证见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
