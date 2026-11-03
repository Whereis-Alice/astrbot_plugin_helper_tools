from __future__ import annotations

import ast
import base64
import copy
import io
import os
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import astrbot.api.message_components as Comp
from astrbot.core.agent.message import (
    ImageURLPart,
    Message,
    TextPart,
    dump_messages_with_checkpoints,
)
from astrbot.core.provider.entities import ProviderRequest
from PIL import Image

from astrbot_plugin_helper_tools.reply_media_request import (
    IMAGE_REF_MAP_ATTRIBUTE,
    PROVENANCE_MARKER,
    UNAVAILABLE_MARKER,
    annotate_quoted_images,
)


def image_data(color: str) -> str:
    stream = io.BytesIO()
    Image.new("RGB", (3, 2), color).save(stream, format="PNG")
    return "data:image/png;base64," + base64.b64encode(stream.getvalue()).decode()


def component(ref: str) -> Comp.Image:
    return Comp.Image(file=ref)


def quote(sender: str | int, message_id: str, *images: Comp.Image) -> Comp.Reply:
    return Comp.Reply(id=message_id, sender_id=sender, chain=list(images))


class Event:
    def __init__(self, *chain: Any) -> None:
        self.message_obj = SimpleNamespace(message=list(chain))
        self.extras: dict[str, Any] = {}
        self.is_at_or_wake_command = True
        self.is_wake = True
        self.call_llm = False
        self.stopped = False
        self.send = AsyncMock(side_effect=AssertionError("must not send"))
        self.should_call_llm = unittest.mock.Mock(
            side_effect=AssertionError("must not change wake")
        )

    def get_messages(self) -> list[Any]:
        return self.message_obj.message

    def get_self_id(self) -> str:
        return "bot"

    def get_extra(self, key: str, default: Any = None) -> Any:
        return self.extras.get(key, default)

    def is_stopped(self) -> bool:
        return self.stopped


def image_parts(request: Any) -> list[ImageURLPart]:
    return [
        part
        for part in request.extra_user_content_parts
        if isinstance(part, ImageURLPart)
    ]


def notes(request: Any) -> list[TextPart]:
    return [
        part
        for part in request.extra_user_content_parts
        if isinstance(part, TextPart) and part.text.startswith(PROVENANCE_MARKER)
    ]


class ReplyMediaRequestTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.current, self.bot, self.other = map(image_data, ("red", "blue", "green"))

    async def test_mixed_authors_preserve_visual_order_and_other_fields(self) -> None:
        request = ProviderRequest(
            prompt="compare",
            system_prompt="persona",
            image_urls=[self.current, self.bot, self.other],
            audio_urls=["voice.wav"],
            contexts=[{"role": "assistant", "content": "history"}],
            extra_user_content_parts=[TextPart(text="other plugin's metadata")],
        )
        event = Event(
            component(self.current),
            quote("bot", "b1", component(self.bot)),
            quote("member", "u1", component(self.other)),
        )
        wake_state = event.is_at_or_wake_command, event.is_wake, event.call_llm

        self.assertEqual(await annotate_quoted_images(event, request), 2)
        self.assertEqual(request.image_urls, [])
        self.assertEqual(
            [part.image_url.url for part in image_parts(request)],
            [self.current, self.bot, self.other],
        )
        self.assertFalse(image_parts(request)[0]._no_save)
        self.assertTrue(all(part._no_save for part in image_parts(request)[1:]))
        self.assertIn("bot 自己", notes(request)[0].text)
        self.assertIn("引用消息 ID：b1", notes(request)[0].text)
        self.assertIn("其它用户", notes(request)[1].text)
        self.assertIn("引用发送者：member", notes(request)[1].text)
        self.assertEqual(request.prompt, "compare")
        self.assertEqual(request.system_prompt, "persona")
        self.assertEqual(request.audio_urls, ["voice.wav"])
        self.assertEqual(
            request.contexts, [{"role": "assistant", "content": "history"}]
        )
        self.assertEqual(
            request.extra_user_content_parts[0].text, "other plugin's metadata"
        )
        self.assertEqual(
            wake_state, (event.is_at_or_wake_command, event.is_wake, event.call_llm)
        )
        event.send.assert_not_called()
        event.should_call_llm.assert_not_called()
        for index, part in enumerate(request.extra_user_content_parts):
            if part in notes(request):
                self.assertIsInstance(
                    request.extra_user_content_parts[index + 1], ImageURLPart
                )

    async def test_only_quote_and_repeated_invocation_are_idempotent(self) -> None:
        request = ProviderRequest(image_urls=[self.bot])
        event = Event(quote("bot", "b", component(self.bot)))
        self.assertEqual(await annotate_quoted_images(event, request), 1)
        original = [
            part.model_dump_for_context() for part in request.extra_user_content_parts
        ]
        self.assertEqual(await annotate_quoted_images(event, request), 0)
        self.assertEqual(
            original,
            [
                part.model_dump_for_context()
                for part in request.extra_user_content_parts
            ],
        )

    async def test_shared_duplicate_content_reports_all_origins_without_claiming_only_bot(
        self,
    ) -> None:
        event = Event(
            component(self.bot),
            quote("bot", "b", component(self.bot)),
            quote("member", "u", component(self.bot)),
        )
        request = ProviderRequest(image_urls=[self.bot])
        self.assertEqual(await annotate_quoted_images(event, request), 1)
        self.assertEqual(len(image_parts(request)), 1)
        self.assertIn("引用消息 ID：b", notes(request)[0].text)
        self.assertIn("引用消息 ID：u", notes(request)[0].text)
        self.assertIn("当前用户本轮新发图片", notes(request)[0].text)
        self.assertNotIn("不是本轮用户新上传", notes(request)[0].text)
        self.assertFalse(image_parts(request)[0]._no_save)

    async def test_input_repeated_images_are_not_silently_dropped(self) -> None:
        request = ProviderRequest(image_urls=[self.bot, self.bot])
        event = Event(quote("bot", "b", component(self.bot)))
        self.assertEqual(await annotate_quoted_images(event, request), 2)
        self.assertEqual(len(image_parts(request)), 2)
        self.assertEqual(await annotate_quoted_images(event, request), 0)

    async def test_guard_image_attributes_override_reply_without_changing_sender(
        self,
    ) -> None:
        image = component(self.bot)
        for key, value in {
            "_helper_tools_quote_sender_id": "bot",
            "_helper_tools_quote_message_id": "actual",
            "_helper_tools_quote_is_bot": True,
        }.items():
            object.__setattr__(image, key, value)
        reply = quote("stale-member", "stale", image)
        request = ProviderRequest(image_urls=[self.bot])
        self.assertEqual(await annotate_quoted_images(Event(reply), request), 1)
        self.assertIn("引用发送者：bot", notes(request)[0].text)
        self.assertIn("引用消息 ID：actual", notes(request)[0].text)
        self.assertEqual(reply.sender_id, "stale-member")

    async def test_unknown_author_is_explicitly_unknown(self) -> None:
        request = ProviderRequest(image_urls=[self.bot])
        self.assertEqual(
            await annotate_quoted_images(
                Event(quote(0, "x", component(self.bot))), request
            ),
            1,
        )
        self.assertIn("来源未知", notes(request)[0].text)
        self.assertIn("引用发送者：未知", notes(request)[0].text)

    async def test_guard_false_bot_flag_without_sender_does_not_invent_an_author(
        self,
    ) -> None:
        image = component(self.bot)
        object.__setattr__(image, "_helper_tools_quote_sender_id", "")
        object.__setattr__(image, "_helper_tools_quote_is_bot", False)
        request = ProviderRequest(image_urls=[self.bot])
        await annotate_quoted_images(Event(quote(0, "unknown", image)), request)
        self.assertIn("来源未知", notes(request)[0].text)

    async def test_late_http_download_alias_uses_guard_recorded_return_path(
        self,
    ) -> None:
        source = "https://example.invalid/late.png"
        downloaded = str(Path("late-download.png").resolve())
        image = component(source)
        original_method = AsyncMock(return_value=downloaded)

        async def guard_resolver():
            path = await original_method()
            object.__setattr__(image, "_helper_tools_resolved_refs", (path,))
            return path

        object.__setattr__(image, "convert_to_file_path", guard_resolver)
        await image.convert_to_file_path()
        original_method.assert_awaited_once()
        request = ProviderRequest(image_urls=[self.current, self.other])
        setattr(request, IMAGE_REF_MAP_ATTRIBUTE, {downloaded: {"path": self.other}})
        event = Event(component(self.current), quote("bot", "late", image))
        self.assertEqual(await annotate_quoted_images(event, request), 1)
        self.assertIn("引用消息 ID：late", notes(request)[0].text)
        self.assertEqual(
            [p.image_url.url for p in image_parts(request)], [self.current, self.other]
        )
        original_method.assert_awaited_once()

    async def test_bare_expired_reply_does_not_claim_current_image(self) -> None:
        request = ProviderRequest(image_urls=[self.current])
        self.assertEqual(
            await annotate_quoted_images(
                Event(component(self.current), quote(0, "old")), request
            ),
            0,
        )
        self.assertEqual(request.image_urls, [self.current])
        self.assertEqual(request.extra_user_content_parts, [])

    async def test_ledger_without_pixels_gets_temporary_unavailable_note(self) -> None:
        reply = quote(0, "old")
        for key, value in {
            "_helper_tools_quote_sender_id": "bot",
            "_helper_tools_quote_message_id": "old",
            "_helper_tools_quote_is_bot": True,
            "_helper_tools_quote_image_count": 3,
        }.items():
            object.__setattr__(reply, key, value)
        event = Event(component(self.current), reply)
        request = ProviderRequest(image_urls=[self.current])
        self.assertEqual(await annotate_quoted_images(event, request), 0)
        self.assertEqual(request.image_urls, [self.current])
        part = request.extra_user_content_parts[0]
        self.assertTrue(part.text.startswith(UNAVAILABLE_MARKER))
        self.assertIn("3 张图片", part.text)
        self.assertIn("不得", part.text)
        self.assertIn("引用发送者：bot", part.text)
        self.assertTrue(part._no_save)
        self.assertEqual(await annotate_quoted_images(event, request), 0)
        self.assertEqual(len(request.extra_user_content_parts), 1)
        reply.chain = [component(self.bot)]
        request.image_urls.append(self.bot)
        self.assertEqual(await annotate_quoted_images(event, request), 1)
        self.assertFalse(
            any(
                isinstance(p, TextPart) and p.text.startswith(UNAVAILABLE_MARKER)
                for p in request.extra_user_content_parts
            )
        )

    async def test_late_hydration_then_provenance_update(self) -> None:
        reply = quote(0, "late")
        event = Event(reply)
        request = ProviderRequest(image_urls=[self.bot])
        self.assertEqual(await annotate_quoted_images(event, request), 0)
        reply.chain = [component(self.bot)]
        self.assertEqual(await annotate_quoted_images(event, request), 1)
        object.__setattr__(reply.chain[0], "_helper_tools_quote_sender_id", "bot")
        object.__setattr__(reply.chain[0], "_helper_tools_quote_is_bot", True)
        self.assertEqual(await annotate_quoted_images(event, request), 1)
        self.assertIn("bot 自己", notes(request)[0].text)
        self.assertEqual(len(image_parts(request)), 1)

    async def test_prepared_map_handles_download_compression_and_data_urls(
        self,
    ) -> None:
        source = "https://example.invalid/original.gif"
        downloaded = str(Path("downloaded.gif").resolve())
        compressed = str(Path("prepared-montage.jpg").resolve())
        request = ProviderRequest(image_urls=[self.current, compressed])
        setattr(
            request,
            IMAGE_REF_MAP_ATTRIBUTE,
            {source: downloaded, downloaded: {"path": compressed, "montage": True}},
        )
        event = Event(component(self.current), quote("bot", "b", component(source)))
        self.assertEqual(await annotate_quoted_images(event, request), 1)
        self.assertEqual(
            [p.image_url.url for p in image_parts(request)], [self.current, compressed]
        )
        # A later finalization can encode the prepared path without losing provenance.
        image_parts(request)[1].image_url.url = self.bot
        with (
            patch.object(Path, "is_file", return_value=True),
            patch.object(
                Path,
                "read_bytes",
                return_value=base64.b64decode(self.bot.split(",", 1)[1]),
            ),
        ):
            self.assertEqual(await annotate_quoted_images(event, request), 0)

    async def test_local_file_and_data_uri_match_by_exact_bytes(self) -> None:
        path = Path("quote.png").resolve()
        request = ProviderRequest(image_urls=[self.current, self.bot])
        event = Event(
            component(self.current), quote("bot", "b", component(path.as_uri()))
        )
        with (
            patch.object(Path, "is_file", return_value=True),
            patch.object(
                Path,
                "read_bytes",
                return_value=base64.b64decode(self.bot.split(",", 1)[1]),
            ),
        ):
            self.assertEqual(await annotate_quoted_images(event, request), 1)
        self.assertIn("bot 自己", notes(request)[0].text)

    async def test_unmapped_compressed_content_is_preserved_without_guessing(
        self,
    ) -> None:
        event = Event(component(self.current), quote("bot", "b", component(self.bot)))
        request = ProviderRequest(image_urls=[self.current, self.other])
        self.assertEqual(await annotate_quoted_images(event, request), 0)
        self.assertEqual(request.image_urls, [self.current, self.other])
        self.assertEqual(request.extra_user_content_parts, [])

    async def test_existing_extra_images_dicts_and_media_keep_their_order(self) -> None:
        extra_image = {
            "type": "image_url",
            "image_url": {"url": self.bot, "detail": "high"},
        }
        audio = {"type": "audio_url", "audio_url": {"url": "voice.wav"}}
        request = ProviderRequest(
            image_urls=[self.current, self.other],
            extra_user_content_parts=[extra_image, audio, TextPart(text="keep")],
        )
        event = Event(
            quote("bot", "b", component(self.bot)),
            quote("member", "u", component(self.other)),
        )
        self.assertEqual(await annotate_quoted_images(event, request), 2)
        parts = request.extra_user_content_parts
        self.assertTrue(parts[0].text.startswith(PROVENANCE_MARKER))
        self.assertEqual(parts[1]["image_url"]["detail"], "high")
        self.assertTrue(parts[1]["_no_save"])
        self.assertIs(parts[2], audio)
        self.assertEqual(parts[3].text, "keep")
        self.assertEqual(
            [p.image_url.url for p in image_parts(request)], [self.current, self.other]
        )
        self.assertEqual(await annotate_quoted_images(event, request), 0)

    async def test_wake_block_and_stopped_event_do_no_work(self) -> None:
        for stopped, blocked in ((True, False), (False, True)):
            event = Event(quote("bot", "b", component(self.bot)))
            event.stopped = stopped
            event.extras["helper_tools_wake_block_llm_request"] = blocked
            request = ProviderRequest(image_urls=[self.bot])
            with patch(
                "astrbot_plugin_helper_tools.reply_media_request._local_identity",
                side_effect=AssertionError("blocked events must not read images"),
            ):
                self.assertEqual(await annotate_quoted_images(event, request), 0)
            self.assertEqual(request.image_urls, [self.bot])
            self.assertEqual(request.extra_user_content_parts, [])

    async def test_no_save_keeps_pairs_in_tool_rounds_but_removes_only_quotes_from_history(
        self,
    ) -> None:
        request = ProviderRequest(prompt="compare", image_urls=[self.current, self.bot])
        await annotate_quoted_images(
            Event(component(self.current), quote("bot", "b", component(self.bot))),
            request,
        )
        runtime = Message.model_validate(
            {
                "role": "user",
                "content": [
                    TextPart(text=request.prompt).model_dump_for_context(),
                    *[
                        part.model_dump_for_context()
                        for part in request.extra_user_content_parts
                    ],
                ],
            }
        )
        rounds = [runtime]
        for index in range(2):
            rounds.append(Message(role="assistant", content=f"tool step {index}"))
            rounds.append(
                Message(
                    role="tool", content=f"result {index}", tool_call_id=f"call-{index}"
                )
            )
            provider_copy = copy.deepcopy([message.model_dump() for message in rounds])
            self.assertEqual(
                len(
                    [p for p in provider_copy[0]["content"] if p["type"] == "image_url"]
                ),
                2,
            )
            self.assertTrue(
                any(
                    PROVENANCE_MARKER in p.get("text", "")
                    for p in provider_copy[0]["content"]
                )
            )
        history = dump_messages_with_checkpoints(rounds)
        self.assertFalse(any(PROVENANCE_MARKER in str(message) for message in history))
        self.assertEqual(
            [
                p["image_url"]["url"]
                for p in history[0]["content"]
                if p["type"] == "image_url"
            ],
            [self.current],
        )

    async def test_actual_runtime_assemble_context_preserves_adjacent_provenance_and_flags(
        self,
    ) -> None:
        snapshot = os.environ.get("HELPER_TOOLS_REVIEW_PROVIDER_ENTITIES")
        if not snapshot:
            self.skipTest(
                "set HELPER_TOOLS_REVIEW_PROVIDER_ENTITIES to the downloaded runtime source"
            )
        tree = ast.parse(Path(snapshot).read_text(encoding="utf-8"), filename=snapshot)
        request_node = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "ProviderRequest"
        )
        module = ast.Module(
            body=[
                ast.ImportFrom(
                    module="__future__", names=[ast.alias(name="annotations")], level=0
                ),
                request_node,
            ],
            type_ignores=[],
        )

        async def resolve(ref: str) -> Any:
            if not ref.startswith("data:image/"):
                raise AssertionError("no downloads or local writes are allowed")
            return SimpleNamespace(to_data_url=lambda: ref)

        namespace = {
            "__name__": __name__,
            "dataclass": dataclass,
            "field": field,
            "resolve_image_ref_to_base64_data": resolve,
            "is_recoverable_image_error": lambda exc: False,
            "logger": SimpleNamespace(warning=lambda *args: None),
        }
        exec(compile(ast.fix_missing_locations(module), snapshot, "exec"), namespace)  # noqa: S102 - trusted local runtime class only
        runtime_request = namespace["ProviderRequest"](
            prompt="compare",
            image_urls=[self.current, self.bot, self.other],
            extra_user_content_parts=[TextPart(text="metadata")],
        )
        event = Event(
            component(self.current),
            quote("bot", "b", component(self.bot)),
            quote("member", "u", component(self.other)),
        )
        self.assertEqual(await annotate_quoted_images(event, runtime_request), 2)
        serialized = await runtime_request.assemble_context()
        self.assertEqual(serialized["role"], "user")
        content = serialized["content"]
        self.assertEqual(
            [p["image_url"]["url"] for p in content if p["type"] == "image_url"],
            [self.current, self.bot, self.other],
        )
        for index, part in enumerate(content):
            if part.get("text", "").startswith(PROVENANCE_MARKER):
                self.assertEqual(content[index + 1]["type"], "image_url")
                self.assertTrue(part["_no_save"])
                self.assertTrue(content[index + 1]["_no_save"])
        saved = dump_messages_with_checkpoints([Message.model_validate(serialized)])
        self.assertEqual(
            [
                p["image_url"]["url"]
                for p in saved[0]["content"]
                if p["type"] == "image_url"
            ],
            [self.current],
        )


if __name__ == "__main__":
    unittest.main()
