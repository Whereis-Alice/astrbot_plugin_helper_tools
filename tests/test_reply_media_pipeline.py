from __future__ import annotations

import ast
import functools
import gc
import os
import unittest
import weakref
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, patch

from astrbot_plugin_helper_tools.reply_media_pipeline import ReplyMediaRequestPipeline
from astrbot_plugin_helper_tools.reply_media_request import (
    IMAGE_REF_MAP_ATTRIBUTE,
    PROVENANCE_MARKER,
    annotate_quoted_images,
)


class ReplyMediaPipelineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.module = ModuleType("offline_core")
        self.return_value = object()
        self.original = AsyncMock(return_value=self.return_value)
        self.module.prepare_request_images = self.original
        self.import_patch = patch(
            "astrbot_plugin_helper_tools.reply_media_pipeline.importlib.import_module",
            return_value=self.module,
        )
        self.import_patch.start()
        self.addCleanup(self.import_patch.stop)

    async def test_initial_prepare_publishes_map_without_callback_or_selection_changes(
        self,
    ) -> None:
        callback = AsyncMock()
        pipeline = ReplyMediaRequestPipeline(callback)
        self.assertTrue(await pipeline.start())
        self.assertTrue(pipeline.installed)
        wrapper = self.module.prepare_request_images
        self.assertTrue(await pipeline.start())
        self.assertIs(wrapper, self.module.prepare_request_images)
        request = SimpleNamespace(image_urls=["initial.png"])
        event = object()
        prepared = {"original.png": {"path": "initial.png", "montage": False}}
        result = await wrapper(request, event, prepared=prepared, finalize=False)
        self.assertIs(result, self.return_value)
        self.assertEqual(request.image_urls, ["initial.png"])
        self.assertEqual(getattr(request, IMAGE_REF_MAP_ATTRIBUTE), prepared)
        self.assertIsNot(getattr(request, IMAGE_REF_MAP_ATTRIBUTE), prepared)
        getattr(request, IMAGE_REF_MAP_ATTRIBUTE)["original.png"]["path"] = "edited"
        self.assertEqual(prepared["original.png"]["path"], "initial.png")
        callback.assert_not_awaited()
        await pipeline.stop()
        self.assertFalse(pipeline.installed)
        self.assertIs(self.module.prepare_request_images, self.original)

    async def test_final_callback_sees_updated_map_after_original_finishes(
        self,
    ) -> None:
        event = object()
        request = SimpleNamespace(image_urls=["raw.png"])
        prepared: dict[str, object] = {}

        async def prepare(req, evt, **kwargs):
            self.assertIs(evt, event)
            req.image_urls = ["final.png"]
            kwargs["prepared"]["raw.png"] = {"path": "final.png", "montage": False}
            return self.return_value

        self.module.prepare_request_images = prepare

        async def annotate(evt, req):
            self.assertIs(evt, event)
            self.assertEqual(req.image_urls, ["final.png"])
            self.assertEqual(
                getattr(req, IMAGE_REF_MAP_ATTRIBUTE)["raw.png"]["path"], "final.png"
            )
            req.annotated = True

        callback = AsyncMock(side_effect=annotate)
        pipeline = ReplyMediaRequestPipeline(callback)
        await pipeline.start()
        self.assertIs(
            await self.module.prepare_request_images(request, event, prepared=prepared),
            self.return_value,
        )
        callback.assert_awaited_once_with(event, request)
        self.assertTrue(request.annotated)
        await pipeline.stop()

    async def test_provider_fallback_runs_before_images_are_relocated(self) -> None:
        request = SimpleNamespace(image_urls=["quote.png"], extra_user_content_parts=[])
        event = SimpleNamespace(selected=False)

        async def callback(evt, req):
            self.assertTrue(evt.selected)
            req.extra_user_content_parts.extend(req.image_urls)
            req.image_urls.clear()

        pipeline = ReplyMediaRequestPipeline(callback)
        await pipeline.start()
        await self.module.prepare_request_images(
            request, event, prepared={}, finalize=False
        )
        # This is the core selector's condition; the observer must not clear it early.
        self.assertTrue(request.image_urls)
        event.selected = True
        await self.module.prepare_request_images(request, event, prepared={})
        self.assertEqual(request.extra_user_content_parts, ["quote.png"])
        self.assertEqual(request.image_urls, [])
        await pipeline.stop()

    async def test_original_exception_is_same_object_and_does_not_call_callback(
        self,
    ) -> None:
        failure = RuntimeError("original failure")
        self.original.side_effect = failure
        callback = AsyncMock()
        pipeline = ReplyMediaRequestPipeline(callback)
        await pipeline.start()
        request = SimpleNamespace()
        with self.assertRaises(RuntimeError) as caught:
            await self.module.prepare_request_images(request, object(), prepared={})
        self.assertIs(caught.exception, failure)
        self.assertFalse(hasattr(request, IMAGE_REF_MAP_ATTRIBUTE))
        callback.assert_not_awaited()
        await pipeline.stop()

    async def test_callback_exception_does_not_change_original_result(self) -> None:
        callback = AsyncMock(side_effect=ValueError("callback failure"))
        pipeline = ReplyMediaRequestPipeline(callback)
        await pipeline.start()
        request = SimpleNamespace()
        with self.assertLogs(
            "astrbot_plugin_helper_tools.reply_media_pipeline", level="WARNING"
        ) as logs:
            result = await self.module.prepare_request_images(
                req=request, event=object(), prepared={}
            )
        self.assertIs(result, self.return_value)
        self.assertIn("ValueError", logs.output[0])
        self.assertNotIn("callback failure", logs.output[0])
        await pipeline.stop()

    async def test_stop_preserves_another_plugins_outer_wrapper(self) -> None:
        callback = AsyncMock()
        pipeline = ReplyMediaRequestPipeline(callback)
        await pipeline.start()
        observed = self.module.prepare_request_images
        calls = []

        async def external(*args, **kwargs):
            calls.append("external")
            return await observed(*args, **kwargs)

        self.module.prepare_request_images = external
        await pipeline.stop()
        self.assertIs(self.module.prepare_request_images, external)
        self.assertIs(
            await external(SimpleNamespace(), object(), prepared={}), self.return_value
        )
        self.assertEqual(calls, ["external"])
        callback.assert_not_awaited()

    async def test_nested_pipeline_uninstall_prunes_only_inactive_own_wrappers(
        self,
    ) -> None:
        first_callback, second_callback = AsyncMock(), AsyncMock()
        first, second = (
            ReplyMediaRequestPipeline(first_callback),
            ReplyMediaRequestPipeline(second_callback),
        )
        await first.start()
        await second.start()
        await first.stop()
        await self.module.prepare_request_images(
            SimpleNamespace(), object(), prepared={}
        )
        first_callback.assert_not_awaited()
        second_callback.assert_awaited_once()
        await second.stop()
        self.assertIs(self.module.prepare_request_images, self.original)

    async def test_uninstall_does_not_prune_foreign_functools_wraps_metadata(
        self,
    ) -> None:
        first = ReplyMediaRequestPipeline(AsyncMock())
        await first.start()
        observed = self.module.prepare_request_images
        external_calls = []

        @functools.wraps(observed)
        async def external(*args, **kwargs):
            external_calls.append(True)
            return await observed(*args, **kwargs)

        self.module.prepare_request_images = external
        second = ReplyMediaRequestPipeline(AsyncMock())
        await second.start()
        await first.stop()
        await second.stop()
        self.assertIs(self.module.prepare_request_images, external)
        await external(SimpleNamespace(), object(), prepared={})
        self.assertEqual(external_calls, [True])

    async def test_actual_core_prepare_selector_and_assemble_preserve_visual_fallback(
        self,
    ) -> None:
        snapshot = os.environ.get("HELPER_TOOLS_REVIEW_PROVIDER_ENTITIES")
        if not snapshot:
            self.skipTest(
                "set HELPER_TOOLS_REVIEW_PROVIDER_ENTITIES to the runtime snapshot"
            )
        from astrbot.core.agent.message import ImageURLPart, TextPart

        from astrbot_plugin_helper_tools.tests.test_reply_media_request import (
            Event,
            component,
            image_data,
            quote,
        )

        current, prepared_quote = image_data("red"), image_data("green")
        raw_quote = str(Path("downloaded-late.png").resolve())
        prepared_calls = []

        async def prepare_image(ref, **kwargs):
            prepared_calls.append(ref)
            return (prepared_quote if ref == raw_quote else ref, False, False, ref)

        async def resolve(ref):
            self.assertTrue(ref.startswith("data:image/"))
            return SimpleNamespace(to_data_url=lambda: ref)

        namespace = {
            "__name__": __name__,
            "dataclass": dataclass,
            "field": field,
            "Path": Path,
            "ImageURLPart": ImageURLPart,
            "TextPart": TextPart,
            "prepare_model_image": prepare_image,
            "normalize_and_dedupe_strings": lambda refs: list(
                dict.fromkeys(ref for ref in refs if ref)
            ),
            "describe_media_ref": lambda ref: ref,
            "ImageInputTooLargeError": type(
                "ImageInputTooLargeError", (Exception,), {}
            ),
            "MODEL_IMAGE_MAX_INPUT_BYTES": 128 * 1024 * 1024,
            "resolve_image_ref_to_base64_data": resolve,
            "is_recoverable_image_error": lambda exc: False,
            "_provider_supports_modality": lambda provider, modality: (
                modality in provider.provider_config["modalities"]
            ),
            "logger": SimpleNamespace(warning=lambda *args: None),
        }
        # Execute only the downloaded functions/classes, bypassing core startup,
        # services, provider calls, and image preparation's file-writing boundary.
        for filename, symbol in (
            (Path(snapshot), "ProviderRequest"),
            (Path(snapshot).with_name("image_input.py"), "prepare_request_images"),
            (
                Path(snapshot).with_name("astr_main_agent.py"),
                "_select_image_chat_provider",
            ),
        ):
            tree = ast.parse(filename.read_text(encoding="utf-8"))
            node = next(
                node
                for node in tree.body
                if isinstance(
                    node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
                )
                and node.name == symbol
            )
            module = ast.Module(
                body=[
                    ast.ImportFrom(
                        module="__future__",
                        names=[ast.alias(name="annotations")],
                        level=0,
                    ),
                    node,
                ],
                type_ignores=[],
            )
            exec(  # noqa: S102 - trusted local runtime symbols only
                compile(ast.fix_missing_locations(module), str(filename), "exec"),
                namespace,
            )

        self.module.prepare_request_images = namespace["prepare_request_images"]
        late_image = component("https://example.invalid/late.png")
        object.__setattr__(late_image, "_helper_tools_resolved_refs", (raw_quote,))
        event = Event(component(current), quote("bot", "late", late_image))
        request = namespace["ProviderRequest"](
            prompt="compare", image_urls=[current, raw_quote]
        )
        callback = AsyncMock(side_effect=annotate_quoted_images)
        pipeline = ReplyMediaRequestPipeline(callback)
        await pipeline.start()
        prepared = {}
        await self.module.prepare_request_images(
            request,
            event,
            prepared=prepared,
            max_size=1024,
            output_dir=Path("."),
            quoted_refs={raw_quote},
            finalize=False,
        )
        callback.assert_not_awaited()
        self.assertEqual(request.image_urls, [current, prepared_quote])
        text_provider = SimpleNamespace(
            provider_config={"id": "text", "modalities": ["text"]}
        )
        visual_provider = SimpleNamespace(
            provider_config={"id": "visual", "modalities": ["text", "image"]}
        )
        selected = namespace["_select_image_chat_provider"](
            text_provider, request, [visual_provider]
        )
        self.assertIs(selected, visual_provider)
        await self.module.prepare_request_images(
            request,
            event,
            prepared=prepared,
            max_size=1024,
            output_dir=Path("."),
            supports_image=True,
        )
        callback.assert_awaited_once_with(event, request)
        self.assertEqual(prepared_calls, [current, raw_quote])
        self.assertEqual(request.image_urls, [])
        serialized = await request.assemble_context()
        content = serialized["content"]
        self.assertEqual(
            [p["image_url"]["url"] for p in content if p["type"] == "image_url"],
            [current, prepared_quote],
        )
        index = next(
            i
            for i, p in enumerate(content)
            if p.get("text", "").startswith(PROVENANCE_MARKER)
        )
        self.assertEqual(content[index + 1]["image_url"]["url"], prepared_quote)
        self.assertTrue(content[index]["_no_save"])
        self.assertTrue(content[index + 1]["_no_save"])
        await pipeline.stop()

    async def test_wrapper_does_not_keep_pipeline_alive(self) -> None:
        callback = AsyncMock()
        pipeline = ReplyMediaRequestPipeline(callback)
        await pipeline.start()
        owner_ref = weakref.ref(pipeline)
        wrapper = self.module.prepare_request_images
        del pipeline
        gc.collect()
        self.assertIsNone(owner_ref())
        self.assertIs(
            await wrapper(SimpleNamespace(), object(), prepared={}), self.return_value
        )
        callback.assert_not_awaited()

    async def test_missing_old_core_function_leaves_compatibility_path_available(
        self,
    ) -> None:
        del self.module.prepare_request_images
        pipeline = ReplyMediaRequestPipeline(AsyncMock())
        self.assertFalse(await pipeline.start())
        self.assertFalse(pipeline.installed)
        await pipeline.stop()
        await pipeline.stop()
        self.assertFalse(hasattr(self.module, "prepare_request_images"))


if __name__ == "__main__":
    unittest.main()
