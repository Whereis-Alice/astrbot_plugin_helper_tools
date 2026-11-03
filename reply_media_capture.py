"""Observe successful OneBot sends without retaining message text or changing sends."""

# Observer failures are intentionally silent: exception text may contain send data.
# ruff: noqa: BLE001, S110, S112

from __future__ import annotations

import asyncio
import inspect
import json
import re
import weakref
from collections.abc import Awaitable, Callable, Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from .onebot_compat import is_onebot_platform

_MISSING = object()
_SEND_ACTIONS = frozenset(
    {
        "send_msg",
        "send_group_msg",
        "send_private_msg",
        "send_forward_msg",
        "send_group_forward_msg",
        "send_private_forward_msg",
    }
)
_CQ_SEGMENT = re.compile(r"\[CQ:([A-Za-z_]+)((?:,[^\]]*)?)\]")
_CONTENT_KEYS = ("message", "messages", "content", "nodes")
_LOGIN_TIMEOUT_SECONDS = 1.0

CaptureCallback = Callable[[str, str, str, list[dict[str, Any]]], Awaitable[Any]]


def _declared(obj: Any, name: str, default: Any = None) -> Any:
    """Read real attributes, excluding aiocqhttp's fabricated action proxies."""
    try:
        inspect.getattr_static(obj, name)
        return getattr(obj, name)
    except (AttributeError, TypeError):
        return default


def _qq_id(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return ""
    text = str(value).strip()
    return str(int(text)) if re.fullmatch(r"[0-9]+", text) and int(text) > 0 else ""


def _payload(value: Any) -> Mapping[str, Any] | None:
    """Unwrap data/result envelopes, rejecting failure and queued responses."""
    for _ in range(8):
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (ValueError, TypeError):
                return None
        if not isinstance(value, Mapping):
            return None
        if "status" in value and str(value["status"]).casefold() not in {
            "ok",
            "success",
        }:
            return None
        if "retcode" in value and str(value["retcode"]) != "0":
            return None
        if value.get("success") is False:
            return None
        if "message_id" in value or "user_id" in value:
            return value
        nested = value.get("data", value.get("result"))
        if not isinstance(nested, (Mapping, str)):
            return value
        value = nested
    return None


def _images(message: Any) -> list[dict[str, Any]]:
    """Copy only image/flash data, including inline forward nodes and CQ strings."""
    images: list[dict[str, Any]] = []
    visiting: set[int] = set()

    def visit(value: Any, depth: int = 0) -> None:
        if depth > 32 or value is None:
            return
        if isinstance(value, str):
            for match in _CQ_SEGMENT.finditer(value):
                data = {}
                for part in match[2].lstrip(",").split(","):
                    key, separator, text = part.partition("=")
                    if separator:
                        for entity, replacement in (
                            ("&#91;", "["),
                            ("&#93;", "]"),
                            ("&#44;", ","),
                            ("&amp;", "&"),
                        ):
                            text = text.replace(entity, replacement)
                        data[key] = text
                visit({"type": match[1], "data": data}, depth + 1)
            return
        if id(value) in visiting:
            return
        visiting.add(id(value))
        try:
            segment_type = (
                value.get("type")
                if isinstance(value, Mapping)
                else _declared(value, "type")
            )
            if segment_type is not None:
                data = (
                    value.get("data")
                    if isinstance(value, Mapping)
                    else _declared(value, "data")
                )
                if str(segment_type).casefold() in {"image", "flash"}:
                    if isinstance(data, Mapping):
                        images.append(deepcopy(dict(data)))
                elif str(segment_type).casefold() in {"node", "forward"}:
                    visit(data, depth + 1)
            elif isinstance(value, Mapping):
                for key in _CONTENT_KEYS:
                    if key in value:
                        visit(value[key], depth + 1)
            elif isinstance(value, Iterable) and not isinstance(
                value, (bytes, bytearray)
            ):
                for component in value:
                    visit(component, depth + 1)
        finally:
            visiting.remove(id(value))

    visit(message)
    return images


@dataclass(eq=False)
class _BotState:
    bot: Any
    platform_ids: set[str] = field(default_factory=set)
    observed_ids: set[str] = field(default_factory=set)


@dataclass(eq=False)
class _Binding:
    owner: Any
    original: Callable[..., Any]
    previous_attribute: Any
    states: list[_BotState] = field(default_factory=list)
    wrapper: Any = None
    active: bool = True
    login_task: asyncio.Task[str] | None = None


class ReplyMediaCapture:
    """Capture one lifecycle of successful OneBot sends at the shared API layer.

    Args:
        context: AstrBot context used to discover platform clients.
        callback: Async sink receiving platform, QQ account, message ID and image data.
        enabled: Dynamic switch. Errors in the switch or sink never affect a send.
    """

    def __init__(
        self, context: Any, callback: CaptureCallback, enabled: Callable[[], bool]
    ) -> None:
        self._context = context
        self._callback: CaptureCallback | None = callback
        self._enabled: Callable[[], bool] | None = enabled
        self._active = False
        self._closed = False
        self._states: dict[int, _BotState] = {}
        self._bindings: dict[int, _Binding] = {}
        self._tasks: set[asyncio.Task[Any]] = set()

    def _is_enabled(self) -> bool:
        try:
            return self._active and self._enabled is not None and bool(self._enabled())
        except Exception:
            return False

    async def start(self) -> None:
        """Install existing OneBot clients before any incoming quote is needed."""
        if self._closed:
            return
        self._active = True
        manager = _declared(self._context, "platform_manager")
        getter = _declared(manager, "get_insts")
        try:
            try:
                platforms = getter() if callable(getter) else None
            except Exception:
                platforms = None
            if platforms is None:
                platforms = _declared(manager, "platform_insts", ())
            if isinstance(platforms, Mapping):
                platforms = platforms.values()
            for platform in platforms:
                try:
                    meta = _declared(platform, "meta")
                    meta = (
                        meta()
                        if callable(meta)
                        else _declared(platform, "metadata", meta)
                    )
                    name = (
                        meta.get("name", "")
                        if isinstance(meta, Mapping)
                        else _declared(meta, "name", "")
                    )
                    if not name or not is_onebot_platform(name):
                        continue
                    platform_id = (
                        meta.get("id", "")
                        if isinstance(meta, Mapping)
                        else _declared(meta, "id", "")
                    )
                    client = _declared(platform, "get_client")
                    bot = client() if callable(client) else _declared(platform, "bot")
                    self._install(bot, str(platform_id or ""))
                except Exception:
                    continue
        except Exception:
            # Some adapters are not ready yet; observe_event installs them later.
            pass
        for binding in tuple(self._bindings.values()):
            if self._is_enabled() and len(binding.states) == 1:
                account, ambiguous = self._identity(binding, {})
                if not account and not ambiguous and self._has_http(binding):
                    await self._login(binding)

    def observe_event(self, event: Any) -> None:
        """Learn real platform/self IDs and install new or reconnected clients."""
        if not self._active:
            return
        try:
            bot = _declared(event, "bot")
            get_platform = _declared(event, "get_platform_id")
            meta = _declared(event, "platform_meta")
            platform_id = (
                get_platform() if callable(get_platform) else _declared(meta, "id", "")
            )
            get_name = _declared(event, "get_platform_name")
            name = get_name() if callable(get_name) else _declared(meta, "name", "")
            if not name or not is_onebot_platform(name):
                return
            get_self = _declared(event, "get_self_id")
            message = _declared(event, "message_obj")
            self_id = (
                get_self() if callable(get_self) else _declared(message, "self_id")
            )
            self._install(bot, str(platform_id or ""), _qq_id(self_id))
        except Exception:
            pass

    def _install(self, bot: Any, platform_id: str, self_id: str = "") -> None:
        if bot is None or not platform_id:
            return
        api = _declared(bot, "api", _MISSING)
        if api is _MISSING:
            api = _declared(bot, "_api", _MISSING)
        owner = bot if api is _MISSING or api is None else api
        original = _declared(owner, "call_action")
        if not callable(original):
            return
        state = self._states.setdefault(id(bot), _BotState(bot))
        state.platform_ids.add(platform_id)
        if self_id:
            state.observed_ids.add(self_id)
        binding = self._bindings.get(id(owner))
        if binding is not None:
            if state not in binding.states:
                binding.states.append(state)
            return
        namespace = _declared(owner, "__dict__", _MISSING)
        previous = (
            namespace.get("call_action", _MISSING)
            if isinstance(namespace, Mapping)
            else original
        )
        binding = _Binding(owner, original, previous, [state])
        observer_ref = weakref.ref(self)

        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            observer = observer_ref()
            snapshot = None
            if observer is not None and binding.active and observer._is_enabled():
                try:
                    action = args[0] if args else kwargs.get("action")
                    if action in _SEND_ACTIONS:
                        params = (
                            dict(args[1])
                            if len(args) > 1 and isinstance(args[1], Mapping)
                            else {}
                        )
                        params.update(kwargs)
                        images = _images(params)
                        account, ambiguous = observer._identity(binding, params)
                        platforms = {
                            p for state in binding.states for p in state.platform_ids
                        }
                        if images and not ambiguous and len(platforms) == 1:
                            snapshot = (next(iter(platforms)), account, images)
                except Exception:
                    pass
            del observer
            # The original call is never caught, retried or given modified parameters.
            result = original(*args, **kwargs)
            if inspect.isawaitable(result):
                result = await result
            observer = observer_ref()
            if (
                snapshot
                and observer is not None
                and binding.active
                and observer._is_enabled()
            ):
                try:
                    await observer._record(binding, snapshot, result)
                except (Exception, asyncio.CancelledError):
                    pass
            return result

        binding.wrapper = wrapper
        try:
            owner.call_action = wrapper
        except (AttributeError, TypeError):
            return
        self._bindings[id(owner)] = binding

    @staticmethod
    def _identity(binding: _Binding, params: Mapping[str, Any]) -> tuple[str, bool]:
        account = _qq_id(params.get("self_id"))
        if account:
            return account, False
        keys: set[str] = set()
        has_registry = False
        for state in binding.states:
            for source in (
                state.bot,
                binding.owner,
                _declared(binding.owner, "_wsr_api"),
            ):
                for name in ("_wsr_api_clients", "_ws_api_clients", "_api_clients"):
                    clients = _declared(source, name)
                    if isinstance(clients, Mapping):
                        has_registry = True
                        keys.update(str(key) for key in clients)
        if len(keys) == 1:
            account = _qq_id(next(iter(keys)))
            return account, not bool(account)
        if len(keys) > 1 or len(binding.states) != 1:
            return "", True
        observed = binding.states[0].observed_ids
        if len(observed) > 1:
            return "", True
        # An empty WS registry can be between accounts during reconnect. Do not
        # associate a new send with the previous connection's observed account.
        return (next(iter(observed)) if observed and not has_registry else ""), False

    @staticmethod
    def _has_http(binding: _Binding) -> bool:
        for source in (
            binding.owner,
            _declared(binding.owner, "_http_api"),
            binding.states[0].bot,
        ):
            if _declared(source, "_api_root"):
                return True
        return False

    def _track(self, task: asyncio.Task[Any]) -> asyncio.Task[Any]:
        self._tasks.add(task)
        observer_ref = weakref.ref(self)

        def done(completed: asyncio.Task[Any]) -> None:
            observer = observer_ref()
            if observer is not None:
                observer._tasks.discard(completed)
            if not completed.cancelled():
                completed.exception()

        task.add_done_callback(done)
        return task

    async def _login(self, binding: _Binding) -> str:
        if len(binding.states) != 1 or not self._has_http(binding):
            return ""
        if binding.login_task is None:
            original = binding.original

            async def read_login() -> str:
                try:
                    value = original("get_login_info")
                    if inspect.isawaitable(value):
                        value = await asyncio.wait_for(value, _LOGIN_TIMEOUT_SECONDS)
                    payload = _payload(value)
                    return _qq_id(payload.get("user_id")) if payload is not None else ""
                except (Exception, asyncio.CancelledError):
                    return ""

            binding.login_task = self._track(asyncio.create_task(read_login()))
        return await asyncio.shield(binding.login_task)

    async def _record(
        self,
        binding: _Binding,
        snapshot: tuple[str, str, list[dict[str, Any]]],
        result: Any,
    ) -> None:
        payload = _payload(result)
        value = payload.get("message_id") if payload is not None else None
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            return
        message_id = str(value).strip()
        if not message_id or (
            re.fullmatch(r"[+-]?[0-9]+", message_id) and int(message_id) == 0
        ):
            return
        platform_id, account, images = snapshot
        if not account:
            account = await self._login(binding)
        callback = self._callback
        if account and callback is not None and binding.active and self._is_enabled():
            task = self._track(
                asyncio.create_task(callback(platform_id, account, message_id, images))
            )
            await asyncio.shield(task)

    async def stop(self) -> None:
        """Deactivate retained wrappers and restore only wrappers still on top."""
        self._active = False
        self._closed = True
        self._callback = None
        self._enabled = None
        self._context = None
        for binding in self._bindings.values():
            binding.active = False
            try:
                if _declared(binding.owner, "call_action") is binding.wrapper:
                    if binding.previous_attribute is _MISSING:
                        delattr(binding.owner, "call_action")
                    else:
                        binding.owner.call_action = binding.previous_attribute
            except (AttributeError, TypeError):
                pass
        pending = [task for task in self._tasks if task is not asyncio.current_task()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._tasks.clear()
        self._bindings.clear()
        self._states.clear()
