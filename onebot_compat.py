"""OneBot v11 兼容层。

AstrBot 通过 ``aiocqhttp`` 对接 OneBot v11 实现（go-cqhttp、NapCat、Lagrange、
LLOneBot / LuckyLilliaBot 等）。不同实现的扩展 action 名称与参数并不统一，
而 ``aiocqhttp`` 的 ``bot.__getattr__`` 对任意 action 都会返回一个 callable，
因此无法通过 ``hasattr`` 探测某个接口是否真的存在。

本模块的做法是：为每个逻辑操作维护一组候选 action 变体，按稳定顺序尝试，
并把命中的变体缓存在 bot 实例上。只有 **可恢复失败** 才会继续尝试下一个变体：

* 未知 action（retcode 404 / 1404 / 10404，或错误文本含 "API 不存在" 等）；
* 参数被拒绝（retcode 400 / 1400 / 10003 … 或本地 ``TypeError``）。

真正的 **执行失败**（例如 LLOneBot 的 retcode 200 / 1200）会直接向上抛出，
避免"戳一戳"这类带副作用的操作被重复执行。
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from time import monotonic
from typing import Any
from weakref import WeakKeyDictionary

from astrbot.api import logger

IMPL_UNKNOWN = "unknown"
IMPL_LLONEBOT = "llonebot"
IMPL_NAPCAT = "napcat"
IMPL_LAGRANGE = "lagrange"
IMPL_GOCQ = "gocq"
IMPL_SHAMROCK = "shamrock"

#: 常见 OneBot v11 平台适配器名称（AstrBot 官方使用 ``aiocqhttp``）。
ONEBOT_PLATFORM_NAMES = frozenset(
    {
        "aiocqhttp",
        "aiocqhttp_adapter",
        "cqhttp",
        "gocq",
        "gocqhttp",
        "go-cqhttp",
        "go_cqhttp",
        "lagrange",
        "llbot",
        "llonebot",
        "luckylilliabot",
        "lucky_lillia_bot",
        "napcat",
        "onebot",
        "onebot11",
        "onebot_11",
        "onebotv11",
        "onebot_v11",
        "openshamrock",
        "shamrock",
    }
)

_IMPL_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (IMPL_LLONEBOT, ("llonebot", "llbot", "lucky", "lillia")),
    (IMPL_NAPCAT, ("napcat",)),
    (IMPL_LAGRANGE, ("lagrange",)),
    (IMPL_SHAMROCK, ("shamrock",)),
    (IMPL_GOCQ, ("go-cqhttp", "gocqhttp", "go_cqhttp", "gocq")),
)

_OPTIONAL_PARAMS = ("no_cache", "reverseOrder", "reverse_order", "count")

#: 各实现在动作失败时可能写进 ``status`` 的取值。
_FAILED_STATUSES = frozenset({"failed", "fail", "error"})

_UNSUPPORTED_RETCODES = frozenset({404, 1404, 10404})
_BAD_PARAM_RETCODES = frozenset({400, 1400, 10003, 10004, 10006})

_UNSUPPORTED_PATTERNS = (
    "api 不存在",
    "不存在的 api",
    "不存在的api",
    "no such api",
    "不支持",
    "未实现",
    "无此接口",
    "not implemented",
    "not supported",
    "unsupported",
    "unknown action",
)

_BAD_PARAM_PATTERNS = (
    "缺少参数",
    "参数错误",
    "参数不正确",
    "参数无效",
    "expected",
    "invalid parameter",
    "is required",
    "missing required",
    "unexpected keyword",
)

_RETCODE_RE = re.compile(r"retcode[=:\s]+(-?\d+)")

_IMPL_TTL_SECONDS = 1800.0
_IMPL_FAILURE_TTL_SECONDS = 60.0
_IMPL_DETECT_TIMEOUT = 5.0


@dataclass(frozen=True, slots=True)
class OneBotImplementation:
    """探测到的 OneBot 实现信息。"""

    name: str = IMPL_UNKNOWN
    app_name: str = ""
    app_version: str = ""

    @property
    def is_known(self) -> bool:
        return self.name != IMPL_UNKNOWN

    @property
    def is_llonebot(self) -> bool:
        return self.name == IMPL_LLONEBOT

    def describe(self) -> str:
        if self.app_name and self.app_version:
            return f"{self.app_name} {self.app_version}"
        return self.app_name or self.name


UNKNOWN_IMPLEMENTATION = OneBotImplementation()


# --------------------------------------------------------------------------
# 底层调用
# --------------------------------------------------------------------------


def _drop_optional_params(params: Mapping[str, Any]) -> dict[str, Any] | None:
    """剔除可选参数后返回新参数表；没有可剔除项时返回 ``None``。"""

    reduced = {k: v for k, v in params.items() if k not in _OPTIONAL_PARAMS}
    if len(reduced) == len(params):
        return None
    return reduced


async def call_onebot(bot: Any, action: str, **params: Any) -> Any:
    """调用单个 OneBot action。

    优先使用 bot 上的同名属性（测试用的 FakeBot 以属性方法暴露 action），
    否则回退到 ``bot.call_action``。若因未知关键字触发 ``TypeError``，
    会剔除可选参数后重试一次。
    """

    if bot is None:
        raise RuntimeError("当前事件没有可用的 OneBot 调用入口。")

    handler = getattr(bot, action, None)
    if callable(handler):
        try:
            return await handler(**params)
        except TypeError:
            reduced = _drop_optional_params(params)
            if reduced is None:
                raise
            return await handler(**reduced)

    call_action = getattr(bot, "call_action", None)
    if callable(call_action):
        try:
            return await call_action(action, **params)
        except TypeError:
            reduced = _drop_optional_params(params)
            if reduced is None:
                raise
            return await call_action(action, **reduced)

    raise RuntimeError("当前事件没有可用的 OneBot 调用入口。")


def unwrap_payload(payload: Any, *, depth: int = 4) -> Any:
    """逐层解包 ``{"status": ..., "retcode": ..., "data": ...}`` 包装。"""

    current = payload
    for _ in range(max(depth, 0)):
        if not isinstance(current, Mapping):
            break
        if "data" not in current:
            break
        if (
            "status" not in current
            and "retcode" not in current
            and len(current) != 1
        ):
            break
        current = current["data"]
    return current


# --------------------------------------------------------------------------
# 错误分类
# --------------------------------------------------------------------------


def _coerce_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text.lstrip("-").isdigit():
            try:
                return int(text)
            except ValueError:
                return None
    return None


def _first_text(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def onebot_error_info(exc: BaseException) -> tuple[int | None, str]:
    """从异常中提取 ``(retcode, message)``，尽量兼容各实现。"""

    retcode: int | None = None
    message = ""

    result = getattr(exc, "result", None)
    if isinstance(result, Mapping):
        retcode = _coerce_int(result.get("retcode"))
        message = _first_text(
            result.get("wording"),
            result.get("message"),
            result.get("msg"),
            result.get("error"),
        )

    if retcode is None:
        retcode = _coerce_int(getattr(exc, "retcode", None))
    if retcode is None:
        retcode = _coerce_int(getattr(exc, "status_code", None))

    text = str(exc)
    if not message:
        message = text.strip()
    if retcode is None and text:
        match = _RETCODE_RE.search(text.casefold())
        if match:
            retcode = _coerce_int(match.group(1))

    return retcode, message


def payload_failure(payload: Any) -> tuple[int | None, str]:
    """从 OneBot 返回体中提取失败信息，返回 ``(retcode, message)``。

    HTTP 或部分适配器在动作失败时不会抛异常，而是原样返回
    ``{"status": "failed", "retcode": ..., "message"/"wording": ...}``。
    调用成功（``status`` 为 ``ok``/``async`` 且 ``retcode`` 为 0 或缺失）时返回
    ``(None, "")``，便于调用方直接用真值判断。
    """

    if not isinstance(payload, Mapping):
        return None, ""
    status = payload.get("status")
    status_text = status.strip().casefold() if isinstance(status, str) else ""
    retcode = _coerce_int(payload.get("retcode"))
    failed = status_text in _FAILED_STATUSES or (retcode is not None and retcode != 0)
    if not failed:
        return None, ""
    message = _first_text(
        payload.get("wording"),
        payload.get("message"),
        payload.get("msg"),
        payload.get("error"),
    )
    return retcode, message or "OneBot 调用失败"


def is_failed_payload(payload: Any) -> bool:
    """返回体是否表示动作失败。"""

    return bool(payload_failure(payload)[1])


def is_unsupported_payload(payload: Any) -> bool:
    """返回体是否表示"该实现没有这个 action"。"""

    retcode, message = payload_failure(payload)
    if not message:
        return False
    if retcode is not None and retcode in _UNSUPPORTED_RETCODES:
        return True
    return _matches(message, _UNSUPPORTED_PATTERNS)


class OneBotPayloadError(RuntimeError):
    """协议端以返回体（而非异常）形式报告的动作失败。

    aiocqhttp 反向 WS 下动作失败会抛 ``ActionFailed``，但 HTTP 适配器与
    部分实现会原样返回 ``{"status": "failed", "retcode": ...}``。转成异常
    之后 ``onebot_error_info`` / ``is_unsupported_action_error`` 等分类函数
    才能照常工作，候选变体也才能正确回退到下一个 action。
    """

    def __init__(self, retcode: int | None, message: str) -> None:
        super().__init__(message or "OneBot 调用失败")
        self.retcode = retcode


def raise_for_payload(payload: Any) -> Any:
    """返回体表示失败时抛 :class:`OneBotPayloadError`，否则原样返回。"""

    retcode, message = payload_failure(payload)
    if message:
        raise OneBotPayloadError(retcode, message)
    return payload


def _matches(message: str, patterns: Iterable[str]) -> bool:
    folded = message.casefold()
    return any(pattern in folded for pattern in patterns)


def is_unsupported_action_error(exc: BaseException) -> bool:
    """异常是否表示"该实现没有这个 action"。"""

    if isinstance(exc, (asyncio.CancelledError, asyncio.TimeoutError)):
        return False
    if isinstance(exc, TypeError):
        return False
    retcode, message = onebot_error_info(exc)
    if retcode is not None and retcode in _UNSUPPORTED_RETCODES:
        return True
    return _matches(message, _UNSUPPORTED_PATTERNS)


def is_bad_param_error(exc: BaseException) -> bool:
    """异常是否表示"参数不被接受"（可以换一组参数重试）。"""

    if isinstance(exc, (asyncio.CancelledError, asyncio.TimeoutError)):
        return False
    if isinstance(exc, TypeError):
        return True
    retcode, message = onebot_error_info(exc)
    if retcode is not None and retcode in _BAD_PARAM_RETCODES:
        return True
    return _matches(message, _BAD_PARAM_PATTERNS)


def is_variant_error(exc: BaseException) -> bool:
    """是否属于"可以尝试下一个候选变体"的可恢复失败。"""

    if isinstance(exc, (asyncio.CancelledError, asyncio.TimeoutError)):
        return False
    return is_unsupported_action_error(exc) or is_bad_param_error(exc)


# --------------------------------------------------------------------------
# 缓存
# --------------------------------------------------------------------------

_variant_cache: WeakKeyDictionary[Any, dict[str, int]] = WeakKeyDictionary()
_variant_cache_fallback: dict[int, dict[str, int]] = {}

_impl_cache: WeakKeyDictionary[Any, tuple[float, OneBotImplementation]] = (
    WeakKeyDictionary()
)
_impl_cache_fallback: dict[int, tuple[float, OneBotImplementation]] = {}


def reset_compat_caches() -> None:
    """清空实现探测与变体命中缓存（测试与配置热重载用）。"""

    _variant_cache.clear()
    _variant_cache_fallback.clear()
    _impl_cache.clear()
    _impl_cache_fallback.clear()


def _cache_get(
    weak: WeakKeyDictionary[Any, Any], fallback: dict[int, Any], bot: Any
) -> Any:
    try:
        return weak.get(bot)
    except TypeError:
        return fallback.get(id(bot))


def _cache_set(
    weak: WeakKeyDictionary[Any, Any],
    fallback: dict[int, Any],
    bot: Any,
    value: Any,
) -> None:
    try:
        weak[bot] = value
    except TypeError:
        fallback[id(bot)] = value


def _remember_variant(bot: Any, op_key: str, index: int) -> None:
    table = _cache_get(_variant_cache, _variant_cache_fallback, bot)
    if table is None:
        table = {}
        _cache_set(_variant_cache, _variant_cache_fallback, bot, table)
    table[op_key] = index


def _recall_variant(bot: Any, op_key: str) -> int | None:
    table = _cache_get(_variant_cache, _variant_cache_fallback, bot)
    if not table:
        return None
    return table.get(op_key)


def _forget_variant(bot: Any, op_key: str) -> None:
    table = _cache_get(_variant_cache, _variant_cache_fallback, bot)
    if table:
        table.pop(op_key, None)


# --------------------------------------------------------------------------
# 实现探测
# --------------------------------------------------------------------------


def normalize_impl_name(app_name: Any) -> str:
    """把 ``get_version_info.app_name`` 归一化为内部实现标识。"""

    if not isinstance(app_name, str):
        return IMPL_UNKNOWN
    folded = app_name.casefold()
    if not folded:
        return IMPL_UNKNOWN
    for name, patterns in _IMPL_PATTERNS:
        if any(pattern in folded for pattern in patterns):
            return name
    return IMPL_UNKNOWN


def cached_implementation(bot: Any) -> OneBotImplementation:
    """只读缓存中的实现信息，不发起探测（用于有时间预算的路径）。"""

    if bot is None:
        return UNKNOWN_IMPLEMENTATION
    cached = _cache_get(_impl_cache, _impl_cache_fallback, bot)
    if cached is None:
        return UNKNOWN_IMPLEMENTATION
    expire_at, impl = cached
    if expire_at <= monotonic():
        return UNKNOWN_IMPLEMENTATION
    return impl


async def detect_implementation(bot: Any) -> OneBotImplementation:
    """探测（并缓存）当前 bot 背后的 OneBot 实现。"""

    if bot is None:
        return UNKNOWN_IMPLEMENTATION

    cached = _cache_get(_impl_cache, _impl_cache_fallback, bot)
    if cached is not None:
        expire_at, impl = cached
        if expire_at > monotonic():
            return impl

    impl = UNKNOWN_IMPLEMENTATION
    ttl = _IMPL_FAILURE_TTL_SECONDS
    try:
        payload = await asyncio.wait_for(
            call_onebot(bot, "get_version_info"), _IMPL_DETECT_TIMEOUT
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - 探测失败不应影响主流程
        logger.debug("[HelperTools/OneBot] 实现探测失败：%s", exc)
    else:
        data = unwrap_payload(payload)
        if isinstance(data, Mapping):
            app_name = data.get("app_name") or data.get("app") or ""
            app_version = data.get("app_version") or data.get("version") or ""
            impl = OneBotImplementation(
                name=normalize_impl_name(app_name),
                app_name=str(app_name or ""),
                app_version=str(app_version or ""),
            )
            ttl = _IMPL_TTL_SECONDS
            logger.info(
                "[HelperTools/OneBot] 检测到实现 %s（内部标识 %s）",
                impl.describe() or "未知",
                impl.name,
            )

    _cache_set(
        _impl_cache, _impl_cache_fallback, bot, (monotonic() + ttl, impl)
    )
    return impl


# --------------------------------------------------------------------------
# 候选变体调度
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OneBotVariant:
    """一个候选 action 调用方式。"""

    action: str
    params: Mapping[str, Any] = field(default_factory=dict)
    only_impls: tuple[str, ...] = ()
    skip_impls: tuple[str, ...] = ()

    def allowed_for(self, impl: OneBotImplementation) -> bool:
        if not impl.is_known:
            return True
        if impl.name in self.skip_impls:
            return False
        return not (self.only_impls and impl.name not in self.only_impls)


def _preferred_error(errors: Sequence[BaseException]) -> BaseException | None:
    for exc in errors:
        if not is_unsupported_action_error(exc):
            return exc
    return errors[0] if errors else None


async def call_onebot_variants(
    bot: Any,
    variants: Sequence[OneBotVariant],
    *,
    op_key: str,
    retry_on: Callable[[BaseException], bool] = is_variant_error,
    detect: bool = True,
    timeout: float | None = None,
    deadline: float | None = None,
) -> Any:
    """依次尝试候选变体，返回第一个成功调用的原始响应。

    命中的变体索引会按 ``op_key`` 缓存到 bot 上，后续调用优先使用；
    因此同一个 ``op_key`` 每次都必须以稳定顺序构造 ``variants``。

    ``timeout`` 为单次调用超时，``deadline`` 为整体截止时间（``monotonic()``
    时间戳）。指定 ``deadline`` 时不会主动发起实现探测，只读已有缓存，
    以免探测本身吃掉时间预算。默认超时不会触发回退，需要时可用
    ``retry_on`` 放行 ``TimeoutError``。
    """

    if not variants:
        raise ValueError("variants 不能为空")

    if deadline is not None:
        impl = cached_implementation(bot)
    elif detect:
        impl = await detect_implementation(bot)
    else:
        impl = UNKNOWN_IMPLEMENTATION

    order = [i for i, v in enumerate(variants) if v.allowed_for(impl)]
    if not order:
        order = list(range(len(variants)))

    remembered = _recall_variant(bot, op_key)
    if remembered is not None and remembered in order:
        order.remove(remembered)
        order.insert(0, remembered)

    errors: list[BaseException] = []
    for index in order:
        variant = variants[index]
        call_timeout = timeout
        if deadline is not None:
            remaining = deadline - monotonic()
            if remaining <= 0:
                break
            call_timeout = (
                remaining if timeout is None else min(timeout, remaining)
            )
        try:
            pending = call_onebot(bot, variant.action, **dict(variant.params))
            if call_timeout is None:
                result = await pending
            else:
                result = await asyncio.wait_for(pending, call_timeout)
            # HTTP 适配器不抛异常，而是回一个 status=failed 的返回体；
            # 转成异常才能走下面的分类与回退逻辑。
            raise_for_payload(result)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # 需要按语义决定是否回退
            _forget_variant(bot, op_key)
            if not retry_on(exc):
                raise
            errors.append(exc)
            logger.debug(
                "[HelperTools/OneBot] %s 变体 %s 不可用：%s",
                op_key,
                variant.action,
                exc,
            )
            continue
        _remember_variant(bot, op_key, index)
        return result

    error = _preferred_error(errors)
    if error is not None and not is_unsupported_action_error(error):
        # 保留原始异常，上层才能读到 retcode 并做失败分类。
        raise error
    actions = "/".join(variants[i].action for i in order)
    message = str(error) if error else "没有可用的候选接口"
    raise RuntimeError(
        f"当前 OneBot 实现不支持 {actions}（{op_key}）：{message}"
    ) from error


# --------------------------------------------------------------------------
# 逻辑操作
# --------------------------------------------------------------------------


async def send_poke(
    bot: Any, *, user_id: int | str, group_id: int | str | None = None
) -> Any:
    """戳一戳。``user_id`` 始终是被戳的人。"""

    if group_id is None:
        variants = (
            OneBotVariant("friend_poke", {"user_id": user_id}),
            OneBotVariant("send_poke", {"user_id": user_id}),
            OneBotVariant(
                "send_private_poke",
                {"user_id": user_id},
                skip_impls=(IMPL_LLONEBOT,),
            ),
            OneBotVariant(
                "poke",
                {"user_id": user_id},
                skip_impls=(IMPL_LLONEBOT, IMPL_NAPCAT),
            ),
        )
        return await call_onebot_variants(bot, variants, op_key="poke:private")

    variants = (
        OneBotVariant("group_poke", {"group_id": group_id, "user_id": user_id}),
        OneBotVariant("send_poke", {"group_id": group_id, "user_id": user_id}),
        OneBotVariant(
            "send_group_poke",
            {"group_id": group_id, "user_id": user_id},
            skip_impls=(IMPL_LLONEBOT,),
        ),
        OneBotVariant(
            "poke",
            {"group_id": group_id, "user_id": user_id},
            skip_impls=(IMPL_LLONEBOT, IMPL_NAPCAT),
        ),
    )
    return await call_onebot_variants(bot, variants, op_key="poke:group")


async def set_bot_nickname(bot: Any, nickname: str) -> Any:
    """修改机器人昵称。"""

    variants = (
        OneBotVariant("set_qq_profile", {"nickname": nickname}),
        OneBotVariant(
            "set_self_profile",
            {"nickname": nickname},
            skip_impls=(IMPL_LLONEBOT,),
        ),
    )
    return await call_onebot_variants(bot, variants, op_key="profile:nickname")


async def set_bot_signature(
    bot: Any, signature: str, *, nickname: str = ""
) -> Any:
    """修改机器人个性签名。

    LLOneBot 没有 ``set_self_longnick``，签名要走
    ``set_qq_profile.personal_note``；由于该接口会整体覆盖资料，
    传入 ``nickname`` 可避免昵称被清空。
    """

    profile_params: dict[str, Any] = {"personal_note": signature}
    if nickname:
        profile_params["nickname"] = nickname

    variants = (
        OneBotVariant(
            "set_self_longnick",
            {"longNick": signature},
            skip_impls=(IMPL_LLONEBOT,),
        ),
        OneBotVariant(
            "set_self_longnick",
            {"long_nick": signature},
            skip_impls=(IMPL_LLONEBOT,),
        ),
        OneBotVariant(
            "set_longnick",
            {"longNick": signature},
            skip_impls=(IMPL_LLONEBOT,),
        ),
        OneBotVariant("set_qq_profile", profile_params),
    )
    return await call_onebot_variants(bot, variants, op_key="profile:signature")


async def set_online_status(
    bot: Any,
    *,
    status: int,
    ext_status: int = 0,
    battery_status: int = 0,
) -> Any:
    """设置在线状态。LLOneBot 三个参数都是必填。"""

    variants = (
        OneBotVariant(
            "set_online_status",
            {
                "status": status,
                "ext_status": ext_status,
                "battery_status": battery_status,
            },
        ),
        OneBotVariant(
            "set_diy_online_status",
            {"face_id": ext_status or status},
            skip_impls=(IMPL_LLONEBOT,),
        ),
    )
    return await call_onebot_variants(
        bot, variants, op_key="profile:online_status"
    )


async def set_bot_avatar(bot: Any, file: str) -> Any:
    """设置机器人头像。"""

    variants = (
        OneBotVariant("set_qq_avatar", {"file": file}),
        OneBotVariant(
            "set_avatar", {"file": file}, skip_impls=(IMPL_LLONEBOT,)
        ),
    )
    return await call_onebot_variants(bot, variants, op_key="profile:avatar")


async def send_like(bot: Any, *, user_id: int | str, times: int = 1) -> Any:
    """给好友点赞。"""

    variants = (
        OneBotVariant("send_like", {"user_id": user_id, "times": times}),
        OneBotVariant(
            "send_profile_like",
            {"user_id": user_id, "times": times},
            skip_impls=(IMPL_LLONEBOT,),
        ),
    )
    return await call_onebot_variants(bot, variants, op_key="user:send_like")


async def get_group_msg_history(
    bot: Any,
    *,
    group_id: int | str,
    message_seq: int | str = 0,
    count: int | None = None,
    reverse: bool = True,
) -> Any:
    """拉取群历史消息。

    LLOneBot 只认 camelCase 的 ``reverseOrder``，其它实现可能用
    ``reverse_order``，也可能完全不支持，因此逐个降级。
    """

    base: dict[str, Any] = {"group_id": group_id, "message_seq": message_seq}
    if count is not None:
        base["count"] = count

    variants = (
        OneBotVariant("get_group_msg_history", {**base, "reverseOrder": reverse}),
        OneBotVariant(
            "get_group_msg_history", {**base, "reverse_order": reverse}
        ),
        OneBotVariant("get_group_msg_history", dict(base)),
        OneBotVariant(
            "get_group_msg_history",
            {"group_id": group_id, "message_seq": message_seq},
        ),
        OneBotVariant("get_group_msg_history", {"group_id": group_id}),
    )
    return await call_onebot_variants(
        bot, variants, op_key="group:msg_history"
    )


def message_lookup_variants(message_id: Any) -> tuple[OneBotVariant, ...]:
    """构造 ``get_msg`` 的候选调用（键名 × int/str 两种取值）。

    刻意不做去重，保证候选序列长度与顺序恒定，命中缓存的下标才有意义。
    """

    numeric = _numeric(message_id)
    text = str(message_id)
    return (
        OneBotVariant("get_msg", {"message_id": numeric}),
        OneBotVariant("get_msg", {"message_id": text}),
        OneBotVariant("get_msg", {"id": numeric}, skip_impls=(IMPL_LLONEBOT,)),
        OneBotVariant("get_msg", {"id": text}, skip_impls=(IMPL_LLONEBOT,)),
    )


async def get_msg(
    bot: Any,
    message_id: Any,
    *,
    timeout: float | None = None,
    deadline: float | None = None,
    retry_on: Callable[[BaseException], bool] = is_variant_error,
) -> Any:
    """按 message_id 拉取消息，自动兼容各实现的键名与取值类型。"""

    return await call_onebot_variants(
        bot,
        message_lookup_variants(message_id),
        op_key="message:get",
        retry_on=retry_on,
        timeout=timeout,
        deadline=deadline,
    )


def retry_including_timeout(exc: BaseException) -> bool:
    """``retry_on`` 变体：把超时也视为"换下一个候选"的理由。"""

    if isinstance(exc, asyncio.CancelledError):
        return False
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return True
    return is_variant_error(exc)


def _numeric(value: Any) -> Any:
    if isinstance(value, str) and value.strip().isdigit():
        try:
            return int(value.strip())
        except ValueError:
            return value
    return value


def file_lookup_variants(
    reference: Any, *, group_id: int | str | None = None
) -> tuple[OneBotVariant, ...]:
    """构造"把消息里的文件/图片引用还原成可访问地址"的候选调用。"""

    variants = [
        OneBotVariant("get_image", {"file": reference}),
        OneBotVariant("get_image", {"file_id": reference}),
        OneBotVariant("get_file", {"file_id": reference}),
        OneBotVariant("get_file", {"file": reference}),
        OneBotVariant(
            "get_image", {"id": reference}, skip_impls=(IMPL_LLONEBOT,)
        ),
        OneBotVariant(
            "get_image", {"image": reference}, skip_impls=(IMPL_LLONEBOT,)
        ),
    ]
    if group_id is not None:
        variants.append(
            OneBotVariant(
                "get_group_file_url",
                {"group_id": _numeric(group_id), "file_id": reference},
            )
        )
    return tuple(variants)


async def resolve_file(
    bot: Any,
    reference: Any,
    *,
    group_id: int | str | None = None,
    timeout: float | None = None,
    deadline: float | None = None,
    retry_on: Callable[[BaseException], bool] = is_variant_error,
) -> Any:
    """尝试解析文件引用，返回原始响应。

    群聊会多一个 ``get_group_file_url`` 候选，因此和私聊使用不同的
    ``op_key``，避免缓存下标串味。
    """

    op_key = "file:resolve:group" if group_id is not None else "file:resolve"
    return await call_onebot_variants(
        bot,
        file_lookup_variants(reference, group_id=group_id),
        op_key=op_key,
        retry_on=retry_on,
        timeout=timeout,
        deadline=deadline,
    )


_FILE_REFERENCE_KEYS = (
    "url",
    "file",
    "path",
    "local_path",
    "file_path",
    "filename",
    "file_name",
)


def extract_file_references(payload: Any) -> list[str]:
    """从 ``get_image`` / ``get_file`` 响应里提取可用的地址或路径。"""

    data = unwrap_payload(payload)
    if isinstance(data, str):
        return [data] if data.strip() else []
    if not isinstance(data, Mapping):
        return []

    references: list[str] = []
    seen: set[str] = set()
    for key in _FILE_REFERENCE_KEYS:
        value = data.get(key)
        if isinstance(value, str):
            text = value.strip()
            if text and text not in seen:
                seen.add(text)
                references.append(text)
    return references


# --------------------------------------------------------------------------
# 平台判定
# --------------------------------------------------------------------------


def event_platform_name(event: Any) -> str:
    """安全地取事件平台名（小写）。"""

    getter = getattr(event, "get_platform_name", None)
    if callable(getter):
        try:
            name = getter()
        except Exception:  # noqa: BLE001 - 平台名不可用时按未知处理
            return ""
        if isinstance(name, str):
            return name.strip().casefold()
    return ""


_extra_platform_names: set[str] = set()


def register_extra_platform_names(names: Iterable[str] | None) -> frozenset[str]:
    """注册额外的 OneBot v11 平台名（适配器 ID），供 :func:`is_onebot_platform` 识别。

    每次调用都会整体替换上一次注册的内容，便于配置热重载时直接重放。
    返回归一化后的名称集合。
    """

    normalized = {
        name.strip().casefold()
        for name in (names or ())
        if isinstance(name, str) and name.strip()
    }
    _extra_platform_names.clear()
    _extra_platform_names.update(normalized)
    return frozenset(_extra_platform_names)


def extra_platform_names() -> frozenset[str]:
    """当前已注册的额外 OneBot 平台名。"""

    return frozenset(_extra_platform_names)


def is_onebot_platform(
    platform_name: Any, extra_names: Iterable[str] | None = None
) -> bool:
    """判断平台名是否属于 OneBot v11 系（含 LLOneBot / NapCat / Lagrange）。

    除内置名单外，还会认可 :func:`register_extra_platform_names` 注册的名称
    以及本次调用传入的 ``extra_names``。

    平台名为空时按"无法判定"处理并返回 ``True``，保持既有宽松行为。
    """

    if not isinstance(platform_name, str):
        return False
    folded = platform_name.strip().casefold()
    if not folded:
        return True
    if folded in ONEBOT_PLATFORM_NAMES or folded in _extra_platform_names:
        return True
    if normalize_impl_name(folded) != IMPL_UNKNOWN:
        return True
    if extra_names:
        for name in extra_names:
            if isinstance(name, str) and name.strip().casefold() == folded:
                return True
    return False


def is_onebot_event(event: Any, extra_names: Iterable[str] | None = None) -> bool:
    """判断事件是否来自 OneBot v11 平台。"""

    return is_onebot_platform(event_platform_name(event), extra_names)
