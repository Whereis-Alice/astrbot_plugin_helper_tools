# 辅助工具合集

为 AstrBot 注册一组 bot 可自行调用的 LLM 工具。当前只有一个工具，后续可以继续在这个插件里追加。

## 功能

- `get_qq_avatar`: 获取 QQ 用户头像。
- 默认会下载头像并把图片内容作为工具结果返回给支持图片输入的模型。
- 如果下载失败，会降级返回头像 URL。
- 附带 `/qq_avatar` 命令，方便安装后直接测试。

## 用法

LLM 自然触发示例：

```text
看看 12345678 的 QQ 头像
描述一下这个人的头像
看看我的 QQ 头像
```

命令测试：

```text
/qq_avatar
/qq_avatar 12345678
/qq_avatar 12345678 640
```

`/qq_avatar` 不带 QQ 号时会尝试使用当前消息发送者的 QQ 号。

## 配置

- `general.enabled`: 启用插件。
- `qq_avatar.llm_tool_enabled`: 启用 LLM 工具。
- `qq_avatar.commands_enabled`: 启用 `/qq_avatar` 命令。
- `qq_avatar.download_image_for_llm`: 工具调用时把头像图片内容返回给模型。
- `qq_avatar.default_size`: 默认头像尺寸，可选 `40`、`100`、`140`、`640`。

## 说明

头像 URL 使用 QQ 公开头像接口：

```text
https://q.qlogo.cn/headimg_dl?dst_uin=<QQ号>&spec=<尺寸>&img_type=jpg
```

