# 辅助工具合集

给 AstrBot 注册一组 bot 可以自己调用的 LLM 工具，也提供常用命令入口。当前包含 QQ 信息、Anime1、收款码、随机语音、Steam 查询、唤醒增强和本地随机壁纸。

## 功能

### LLM 工具

- `get_qq_avatar`：获取 QQ 用户头像，可把头像图片内容返回给支持视觉输入的模型。
- `get_qq_group_member_info`：获取 QQ 群成员信息，包含 QQ号、QQ名、群昵称、群身份、群等级、群专属头衔，并补充 OneBot 可返回的其它字段。
- `get_qq_profile`：整合 QQ 用户资料、群成员资料、群信息和头像。
- `send_payment_qr`：在“打钱、转账、赞助、请客、发红包”等场景发送配置好的收款码。
- `get_anime1_updates`：查询 Anime1 番剧剧集更新列表，支持缓存、时间范围、关键词和数量限制。
- `get_anime1_watch_url`：按 Anime1 条目 ID 生成观看地址。
- `send_random_voice`：发送配置好的随机语音，默认兼容“哈基米”语音 API，也可以换成其它音频 API。
- `search_steam_game`：按 Steam AppID、商店链接或关键词查询游戏信息，可附带封面图。
- `set_bot_qq_profile`：管理员会话可用，用于修改 bot QQ 昵称、签名、状态、头像或同步人格；默认关闭。

### QQ 头像自动更换

`qq_avatar.auto_change` 可以让 bot 按时间表从本地头像池随机拿一张图片，自动更换自己的 QQ 头像。这个功能默认关闭；开启后，头像池目录留空时会使用插件数据目录下的 `avatar_pool`。

时间表使用 5 段 cron：

- 每天 8 点：`0 8 * * *`
- 每 6 小时：`0 */6 * * *`
- 每周一 9 点：`0 9 * * 1`

也可以用管理员命令立刻测试一次：

```text
/random_avatar
/随机头像
/换头像
```

上面示例里的 `/` 仍然表示 AstrBot 全局唤醒词缀；如果你的唤醒词缀是 `!`，就改成 `!随机头像`。

### 唤醒增强

- 支持阻塞判断：全局黑名单、唤醒冷却、QQ 机器人账号段过滤、复读 bot 最近发言过滤、可自由删改的 wakepro 默认屏蔽词。
- 支持指令屏蔽：可拦截内置指令、唤醒词缀命令消息、唤醒词缀普通消息，避免把命令或“唤醒词缀 + 随便聊聊”这类消息误交给 LLM。
- 支持消息防抖：bot 被唤醒后，同一用户短时间内连续发言会尝试合并到上一轮请求，避免一句话拆成多次 LLM 调用。
- 支持 `@ bot` 唤醒。
- 支持通用唤醒词和管理员专属唤醒词。
- 唤醒词触发方式可多选：自由触发、前缀触发、后缀触发。
- 纯引用 bot 消息默认不会唤醒 LLM；只有同一条消息同时 `@ bot` 或命中唤醒词才会唤醒。
- 不并入 wakepro 的智能唤醒和沉默检测。

### 本地随机壁纸

可以配置多个壁纸库，每个壁纸库有自己的本地目录、触发指令、发送文案和发送方式。

示例：

假设 AstrBot 全局唤醒词缀是 `/`：

```text
卡比壁纸
/卡比壁纸
//卡比壁纸        # 历史双前缀写法也兼容
存图 卡比壁纸       # 随消息带图，或引用图片后使用；图库不存在时会自动新建
删图               # 引用本插件发出的壁纸后删除对应本地文件
```

壁纸发送方式支持：

- 同一条消息：文案和图片在一条消息里发送。
- 先发文案再发图：先单独发文案，再发图片。
- 只发图片。

存图/删图默认仅管理员可用，存图时自动新建图库也同样受这个权限限制。删除功能会优先根据“本插件发送的消息 ID -> 本地图片路径”记录精准删除，并且只允许删除已配置图库目录内的文件。

## 常用命令

下面示例假设 AstrBot 全局唤醒词缀是 `/`；如果你配置成 `!`，就把示例开头的 `/` 换成 `!`。

```text
/qq_avatar [QQ号|@用户] [40|100|140|640]
/random_avatar       # 管理员手动随机换 bot 头像
/qq_member [QQ号|@用户] [群号]
/qq_profile [QQ号|@用户] [群号]
/box [QQ号|@用户]
/payqr
/anime1_update
/anime1 [关键词] [年|月|周|日|全部] [数量]
/anime1_url <Anime1 ID>
```

随机语音、Steam 查询和壁纸随机抽图使用可配置命令名。插件会自动套用 AstrBot 全局唤醒词缀；如果你的全局唤醒词缀是 `!`，下面示例里的 `/steam` 就对应 `!steam`。

```text
/voice_meme
/随机语音
/steam <AppID|商店链接|关键词>
/查找 <AppID|商店链接|关键词>
/778666        # 纯数字 AppID 默认需要 AstrBot 唤醒词缀才会触发
```

AstrBot 会先把全局唤醒词缀从消息文本里去掉，再把消息交给插件。因此 `/steam 778666` 在插件里实际会变成 `steam 778666`；本插件会根据原始消息判断是否真的带了唤醒词缀，同时兼容这两种形态，并在命令处理后阻止消息继续进入 LLM。

Bot QQ 资料管理命令需要管理员权限：

```text
设置头像 [图片URL]
设置昵称 <昵称>
设置签名 <签名>
设置状态 <状态名>
切换人格 [人格名]
同步人格 [人格名]
人格列表
```

## 配置

配置项按模块分组：

- `general`：总开关。
- `wake`：阻塞判断、唤醒屏蔽词、指令屏蔽、消息防抖、提及唤醒、唤醒词触发方式、禁用纯引用唤醒、黑名单。
- `wallpaper`：多壁纸库、图库路径、随机抽图指令、存图/删图指令、存图自动新建图库、权限、去重和发送方式。
- `qq_avatar`：QQ 头像工具、默认尺寸、图片下载限制、`auto_change` 自动随机更换 bot 头像。
- `qq_member`：QQ群成员信息工具、是否输出原始额外字段。
- `qq_profile`：QQ 资料查询、保护名单、是否仅管理员可查他人。
- `payqr`：收款码图片和发送文案。
- `anime1`：缓存刷新时间、启动更新、默认返回数量。
- `voice`：随机语音 API、指令前缀、触发关键词、缓存数量。
- `steam`：Steam 查询指令、Steam 商店链接自动解析、纯数字 AppID 触发方式、展示字段、限速。
- `bot_profile`：bot QQ 资料管理命令和高风险 LLM 工具开关。

唤醒增强里几个容易混淆的开关：

- `block_keywords`：唤醒屏蔽词，默认带 wakepro 的默认列表，可以在配置页里自由删、改、加；删掉的词不会再生效。
- 如果你是从旧版本更新过来，配置页里看到屏蔽词是空的，重载一次插件后会自动补入 wakepro 默认列表；补完后插件会写入初始化标记。之后你再手动清空、删除或修改词表，插件不会偷偷改回去。
- `block_prefix_commands`：屏蔽“当前 AstrBot 唤醒词缀 + 指令”的消息，例如 `/qq_avatar` 或 `!qq_avatar`。
- `block_prefix_llm`：屏蔽“当前 AstrBot 唤醒词缀 + 普通聊天”的消息，例如 `/帮我写个文案` 或 `!帮我写个文案`，但不影响真正的指令。

壁纸库的随机抽图指令会同时兼容三种写法：直接发配置里的命令名，例如 `卡比壁纸`；加当前 AstrBot 唤醒词缀，例如 `/卡比壁纸`；以及旧习惯里的双前缀写法，例如 `//卡比壁纸`。如果你的 AstrBot 全局唤醒词缀不是 `/`，就把示例里的 `/` 换成你自己的前缀。

Steam 的 `auto_parse_links` 只负责自动解析 `store.steampowered.com/app/...` 这类商店链接。单独发纯数字是否触发 Steam 查询由 `appid_auto_parse_mode` 控制，默认是“需要唤醒词缀”，也就是 `778666` 不会触发，`/778666` 才会触发；也可以改成“关闭”或“直接触发”。

`set_bot_qq_profile` 默认不注册为可用 LLM 工具；如需让模型主动修改 bot QQ 资料，请在 `bot_profile.llm_tool_enabled` 中显式开启。工具内部仍会检查当前会话是否为管理员。

## 平台说明

QQ 相关功能依赖 OneBot/AIOCQHTTP/NapCat 一类适配器提供的接口。不同适配器可能支持字段不同，本插件会优先输出已知字段，并在配置允许时附带其它原始字段。

自动更换 bot QQ 头像同样依赖 OneBot 的 `set_qq_avatar` 接口；如果你接了多个 QQ 号，可以在 `qq_avatar.auto_change.platform_id` 里指定要操作的 AstrBot 平台 ID。头像池支持 `.jpg`、`.jpeg`、`.png`、`.webp`，可在配置里改。

壁纸的随机发送可在通用平台上工作；“引用 bot 发出的图片后删除对应本地文件”在 OneBot 平台上最稳，因为可以拿到发送消息 ID 做持久化映射。其它平台会尝试从引用链中的本地图片路径兜底。

## 上游来源

本插件把下列插件的能力并入到一个维护成本更低的工具合集里，并做了模块化重写与配置整理：

- QQ 资料卡能力参考 [Zhalslar/astrbot_plugin_box](https://github.com/Zhalslar/astrbot_plugin_box)
- Anime1 更新列表能力参考 [zhist2028/astrbot_plugin_anime1_list](https://github.com/zhist2028/astrbot_plugin_anime1_list)
- 收款码工具能力参考 [luori7hao/astrbot_plugin_payqr](https://github.com/luori7hao/astrbot_plugin_payqr)
- Bot QQ 资料管理能力参考 [Zhalslar/astrbot_plugin_qqprofile](https://github.com/Zhalslar/astrbot_plugin_qqprofile)
- 随机哈基米语音能力参考 [oxoax/zhiyu-astrbot-hjm](https://github.com/oxoax/zhiyu-astrbot-hjm)
- Steam 链接解析能力参考 [xu654/SteamLink](https://github.com/xu654/SteamLink)
- 提及唤醒增强能力参考 [Zhalslar/astrbot_plugin_wakepro](https://github.com/Zhalslar/astrbot_plugin_wakepro)

## 依赖

当前实现只使用 AstrBot 运行环境和 Python 标准库，没有额外第三方依赖。
