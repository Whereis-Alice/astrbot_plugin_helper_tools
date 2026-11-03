"""Observe core image preparation without changing provider selection or policy."""

from __future__ import annotations

import functools
import importlib
import inspect
import logging
import weakref
from collections.abc import Awaitable, Callable
from types import ModuleType
from typing import Any

from .reply_media_request import IMAGE_REF_MAP_ATTRIBUTE

_LOGGER = logging.getLogger(__name__)
_OWNER_ATTRIBUTE = "_helper_tools_reply_media_pipeline_owner"
_PREVIOUS_ATTRIBUTE = "_helper_tools_reply_media_pipeline_previous"
_SELF_ATTRIBUTE = "_helper_tools_reply_media_pipeline_wrapper"


class ReplyMediaRequestPipeline:
    """Run provenance callbacks after the core's final image preparation.

    Args:
        callback: Async callback receiving (event, final_request). It must apply
            existing wake guards and must not make additional provider requests.
    """

    def __init__(self, callback: Callable[[Any, Any], Awaitable[Any]]) -> None:
        self._callback = callback
        self._active = False
        self._module: ModuleType | None = None
        self._wrapper: Callable[..., Any] | None = None
        self._previous: Callable[..., Any] | None = None

    @property
    def installed(self) -> bool:
        """Whether this observer is active, including inside another wrapper."""
        return self._active

    async def start(self) -> bool:
        """Install one observer on the core's module-level function reference.

        Returns:
            True when installed, False when this core lacks the prepare function.
        """
        if self._active:
            return True
        try:
            module = importlib.import_module("astrbot.core.astr_main_agent")
        except ImportError:
            return False
        previous = getattr(module, "prepare_request_images", None)
        if not callable(previous):
            return False
        owner_ref = weakref.ref(self)

        @functools.wraps(previous)
        async def observe(*args: Any, **kwargs: Any) -> Any:
            # Original failures and cancellation propagate before any callback.
            result = previous(*args, **kwargs)
            if inspect.isawaitable(result):
                result = await result
            owner = owner_ref()
            if owner is None or not owner._active:
                return result
            request = kwargs.get("req", kwargs.get("request"))
            event = kwargs.get("event")
            if request is None and args:
                request = args[0]
            if event is None and len(args) > 1:
                event = args[1]
            if request is None or event is None:
                return result
            try:
                prepared = kwargs.get("prepared")
                if isinstance(prepared, dict):
                    # Snapshot the core-owned cache; callbacks must not mutate it.
                    mapping = {
                        source: dict(value) if isinstance(value, dict) else value
                        for source, value in prepared.items()
                    }
                    setattr(request, IMAGE_REF_MAP_ATTRIBUTE, mapping)
                if kwargs.get("finalize", True) is not False:
                    await owner._callback(event, request)
            except Exception as exc:  # noqa: BLE001 - provenance cannot fail the core request
                _LOGGER.warning(
                    "Reply image provenance observer failed (%s).", type(exc).__name__
                )
            return result

        setattr(observe, _OWNER_ATTRIBUTE, owner_ref)
        setattr(observe, _PREVIOUS_ATTRIBUTE, previous)
        setattr(observe, _SELF_ATTRIBUTE, weakref.ref(observe))
        self._module = module
        self._previous = previous
        self._wrapper = observe
        self._active = True
        module.prepare_request_images = observe
        return True

    async def stop(self) -> None:
        """Deactivate and remove this observer only when it owns the outer slot.

        Observers inside another plugin's wrapper remain inert pass-throughs.
        Dead/inactive observers of this class can be pruned when restoring a slot;
        wrappers belonging to other plugins are never removed.
        """
        self._active = False
        module, wrapper, previous = self._module, self._wrapper, self._previous
        if (
            module is not None
            and getattr(module, "prepare_request_images", None) is wrapper
        ):
            while previous is not None:
                wrapper_ref = getattr(previous, _SELF_ATTRIBUTE, None)
                # functools.wraps can copy our attributes onto another plugin's
                # wrapper. Only prune a function identified as our actual wrapper.
                if (
                    not isinstance(wrapper_ref, weakref.ReferenceType)
                    or wrapper_ref() is not previous
                ):
                    break
                owner_ref = getattr(previous, _OWNER_ATTRIBUTE, None)
                if not isinstance(owner_ref, weakref.ReferenceType):
                    break
                owner = owner_ref()
                if owner is not None and owner._active:
                    break
                inner = getattr(previous, _PREVIOUS_ATTRIBUTE, None)
                if not callable(inner) or inner is previous:
                    break
                previous = inner
            module.prepare_request_images = previous
        self._module = None
        self._wrapper = None
        self._previous = None
