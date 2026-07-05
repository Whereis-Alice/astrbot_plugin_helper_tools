# 辅助工具合集

给 AstrBot 注册一组 bot 可以自己调用的 LLM 工具，也提供常用命令入口。这个仓库会逐步收纳一些轻量辅助能力，当前重点是 QQ 信息、Anime1、收款码、随机语音和 Steam 查询。

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

### 命令

```text
/qq_avatar [QQ号|@用户] [40|100|140|640]
/qq_member [QQ号|@用户] [群号]
/qq_profile [QQ号|@用户] [群号]
/box [QQ号|@用户]
/payqr
/anime1_update
/anime1 [关键词] [年|月|周|日|全部] [数量]
/anime1_url <Anime1 ID>
```

随机语音和 Steam 查询使用可配置前缀：

```text
/voice_meme
/随机语音
/steam <AppID|商店链接|关键词>
/查找 <AppID|商店链接|关键词>
```

Bot QQ 资料管理命令需要管理员权限：

```text
设置头像 [图片URL]     # 也支持引用/发送图片
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
- `qq_avatar`：QQ 头像工具、默认尺寸、图片下载限制。
- `qq_member`：QQ群成员信息工具、是否输出原始额外字段。
- `qq_profile`：QQ 资料查询、保护名单、是否仅管理员可查他人。
- `payqr`：收款码图片和发送文案。
- `anime1`：缓存刷新时间、启动更新、默认返回数量。
- `voice`：随机语音 API、指令前缀、触发关键词、缓存数量。
- `steam`：Steam 查询指令、自动解析链接、展示字段、限速。
- `bot_profile`：bot QQ 资料管理命令和高风险 LLM 工具开关。

`set_bot_qq_profile` 默认不注册为可用 LLM 工具；如需让模型主动修改 bot QQ 资料，请在 `bot_profile.llm_tool_enabled` 中显式开启。工具内部仍会检查当前会话是否为管理员。

## 平台说明

QQ 相关功能依赖 OneBot/AIOCQHTTP/NapCat 一类适配器提供的接口：

- `get_group_member_info`
- `get_stranger_info`
- `get_group_info`
- `set_qq_profile`
- `set_self_longnick`
- `set_online_status`
- `set_qq_avatar`

不同适配器可能支持字段不同。本插件会优先输出已知字段，并在配置允许时附带其它原始字段。

## 上游来源

本插件把下列插件的能力并入到一个维护成本更低的工具合集里，并做了模块化重写与配置整理：

- QQ 资料卡能力参考 [Zhalslar/astrbot_plugin_box](https://github.com/Zhalslar/astrbot_plugin_box)
- Anime1 更新列表能力参考 [zhist2028/astrbot_plugin_anime1_list](https://github.com/zhist2028/astrbot_plugin_anime1_list)
- 收款码工具能力参考 [luori7hao/astrbot_plugin_payqr](https://github.com/luori7hao/astrbot_plugin_payqr)
- Bot QQ 资料管理能力参考 [Zhalslar/astrbot_plugin_qqprofile](https://github.com/Zhalslar/astrbot_plugin_qqprofile)
- 随机哈基米语音能力参考 [oxoax/zhiyu-astrbot-hjm](https://github.com/oxoax/zhiyu-astrbot-hjm)
- Steam 链接解析能力参考 [xu654/SteamLink](https://github.com/xu654/SteamLink)

## 依赖

当前实现只使用 AstrBot 运行环境和 Python 标准库，没有额外第三方依赖。
