from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from astrbot_plugin_helper_tools.avatar_rotation_service import (
    AvatarRotationService,
    normalize_cron_expression,
)
from astrbot_plugin_helper_tools.onebot_compat import reset_compat_caches


class FakeActionFailed(Exception):
    """模拟 aiocqhttp.exceptions.ActionFailed。"""

    def __init__(self, retcode: int, message: str) -> None:
        super().__init__(f"ActionFailed(retcode={retcode})")
        self.retcode = retcode
        self.result = {
            "status": "failed",
            "retcode": retcode,
            "message": message,
            "wording": message,
        }


class FakeOneBot:
    """只通过 call_action 暴露 action 的假协议端（LLOneBot 风格）。"""

    def __init__(self, *, fail: FakeActionFailed | None = None) -> None:
        self.fail = fail
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def calls_of(self, action: str) -> list[dict[str, Any]]:
        return [params for name, params in self.calls if name == action]

    async def call_action(self, action: str, **params: Any) -> Any:
        self.calls.append((action, dict(params)))
        if action == "get_version_info":
            return {
                "status": "ok",
                "retcode": 0,
                "data": {"app_name": "LLOneBot", "app_version": "8.1.9"},
            }
        if action == "set_qq_avatar":
            if self.fail is not None:
                raise self.fail
            return {"status": "ok", "retcode": 0, "data": None}
        raise FakeActionFailed(1404, f"{action} API 不存在")


class FakeEvent:
    def __init__(self, platform_name: str, bot: Any) -> None:
        self._platform_name = platform_name
        self.bot = bot

    def get_platform_name(self) -> str:
        return self._platform_name


def make_service(
    tmp: Path,
    *,
    auto_change: dict[str, Any] | None = None,
    context: Any = None,
) -> AvatarRotationService:
    config: dict[str, Any] = {
        "qq_avatar": {
            "enabled": True,
            "auto_change": {"enabled": True, **(auto_change or {})},
        }
    }
    return AvatarRotationService(config, tmp, context or SimpleNamespace())


class AvatarRotationTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        reset_compat_caches()
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(reset_compat_caches)

    def write_images(self, service: AvatarRotationService, *names: str) -> list[Path]:
        root = service.image_dir()
        root.mkdir(parents=True, exist_ok=True)
        created = []
        for name in names:
            path = root / name
            path.write_bytes(b"fake-image")
            created.append(path.resolve(strict=False))
        return created


class CronNormalizationTests(unittest.TestCase):
    def test_five_fields_pass_through(self) -> None:
        self.assertEqual(normalize_cron_expression(" 30  7 * * 1 "), "30 7 * * 1")

    def test_four_fields_get_padded(self) -> None:
        self.assertEqual(normalize_cron_expression("0 8 * *"), "0 8 * * *")

    def test_six_field_seconds_prefix_is_dropped(self) -> None:
        self.assertEqual(normalize_cron_expression("0 30 7 * * *"), "30 7 * * *")

    def test_garbage_falls_back_to_default(self) -> None:
        self.assertEqual(normalize_cron_expression("not a cron"), "0 8 * * *")
        self.assertEqual(normalize_cron_expression(None), "0 8 * * *")


class ImagePickingTests(AvatarRotationTestCase):
    async def test_image_files_async_filters_by_extension(self) -> None:
        service = make_service(self.tmp)
        self.write_images(service, "a.png", "b.jpg", "c.txt", "d.gif")
        found = {path.name for path in await service.image_files_async()}
        self.assertEqual(found, {"a.png", "b.jpg"})

    async def test_image_files_async_respects_recursive_flag(self) -> None:
        flat = make_service(self.tmp)
        nested_dir = flat.image_dir() / "sub"
        nested_dir.mkdir(parents=True, exist_ok=True)
        (nested_dir / "deep.png").write_bytes(b"x")
        self.assertEqual(await flat.image_files_async(), [])

        deep = make_service(self.tmp, auto_change={"recursive": True})
        names = {path.name for path in await deep.image_files_async()}
        self.assertEqual(names, {"deep.png"})

    async def test_missing_directory_returns_empty_list(self) -> None:
        service = make_service(self.tmp)
        self.assertEqual(await service.image_files_async(), [])

    def test_pick_image_accepts_injected_state_without_disk_read(self) -> None:
        service = make_service(self.tmp)
        images = self.write_images(service, "a.png", "b.png")
        service.load_state = lambda: (_ for _ in ()).throw(  # type: ignore[method-assign]
            AssertionError("pick_image must not read state when one is provided")
        )
        picked = service.pick_image(images, {"last_path": str(images[0])})
        self.assertEqual(picked, images[1])

    def test_pick_image_reuses_last_when_only_one_candidate(self) -> None:
        service = make_service(self.tmp)
        images = self.write_images(service, "only.png")
        picked = service.pick_image(images, {"last_path": str(images[0])})
        self.assertEqual(picked, images[0])

    def test_pick_image_ignores_last_when_avoid_repeat_off(self) -> None:
        service = make_service(self.tmp, auto_change={"avoid_repeat": False})
        images = self.write_images(service, "a.png")
        self.assertEqual(service.pick_image(images, {"last_path": str(images[0])}), images[0])

    async def test_pick_image_async_loads_persisted_state(self) -> None:
        service = make_service(self.tmp)
        images = self.write_images(service, "a.png", "b.png")
        await service.save_state_async({"last_path": str(images[0])})
        self.assertEqual(await service.pick_image_async(images), images[1])

    async def test_load_state_async_tolerates_corrupt_file(self) -> None:
        service = make_service(self.tmp)
        service.state_path.parent.mkdir(parents=True, exist_ok=True)
        service.state_path.write_text("{not json", encoding="utf-8")
        self.assertEqual(await service.load_state_async(), {})


class ChangeOnceTests(AvatarRotationTestCase):
    async def test_disabled_service_reports_instead_of_calling(self) -> None:
        service = make_service(self.tmp, auto_change={"enabled": False})
        self.assertIn("未启用", await service.change_once())

    async def test_empty_pool_reports_directory(self) -> None:
        service = make_service(self.tmp)
        message = await service.change_once()
        self.assertIn("没有可用图片", message)

    async def test_success_uses_set_qq_avatar_and_persists_state(self) -> None:
        service = make_service(self.tmp)
        images = self.write_images(service, "only.png")
        bot = FakeOneBot()
        event = FakeEvent("aiocqhttp", bot)

        message = await service.change_once(event)

        self.assertIn("only.png", message)
        self.assertEqual(bot.calls_of("set_qq_avatar"), [{"file": str(images[0])}])
        state = await service.load_state_async()
        self.assertEqual(state["last_path"], str(images[0]))
        self.assertEqual(state["reason"], "manual")
        self.assertIsInstance(state["last_changed_at"], int)

    async def test_llonebot_platform_name_is_accepted(self) -> None:
        service = make_service(self.tmp)
        self.write_images(service, "only.png")
        bot = FakeOneBot()
        message = await service.change_once(FakeEvent("llonebot", bot))
        self.assertIn("only.png", message)
        self.assertEqual(len(bot.calls_of("set_qq_avatar")), 1)

    async def test_action_failure_returns_readable_message_with_retcode(self) -> None:
        service = make_service(self.tmp)
        self.write_images(service, "only.png")
        bot = FakeOneBot(fail=FakeActionFailed(1200, "上传头像失败"))

        message = await service.change_once(FakeEvent("aiocqhttp", bot))

        self.assertIn("设置 QQ 头像失败", message)
        self.assertIn("上传头像失败", message)
        self.assertIn("1200", message)
        self.assertEqual(await service.load_state_async(), {})

    async def test_non_onebot_event_bot_is_not_used(self) -> None:
        service = make_service(self.tmp)
        self.write_images(service, "only.png")
        bot = FakeOneBot()

        message = await service.change_once(FakeEvent("telegram", bot))

        self.assertIn("没有找到可用的", message)
        self.assertEqual(bot.calls, [])

    async def test_bot_is_resolved_from_platform_manager(self) -> None:
        bot = FakeOneBot()
        platform = SimpleNamespace(
            meta=lambda: SimpleNamespace(name="aiocqhttp", id="default"),
            bot=bot,
        )
        context = SimpleNamespace(
            platform_manager=SimpleNamespace(platform_insts=[platform])
        )
        service = make_service(self.tmp, context=context)
        self.write_images(service, "only.png")

        message = await service.change_once()

        self.assertIn("only.png", message)
        self.assertEqual(len(bot.calls_of("set_qq_avatar")), 1)

    async def test_platform_manager_skips_non_onebot_platforms(self) -> None:
        bot = FakeOneBot()
        platform = SimpleNamespace(
            meta=lambda: SimpleNamespace(name="telegram", id="tg"),
            bot=bot,
        )
        context = SimpleNamespace(
            platform_manager=SimpleNamespace(platform_insts=[platform])
        )
        service = make_service(self.tmp, context=context)
        self.write_images(service, "only.png")

        self.assertIn("没有找到可用的", await service.change_once())
        self.assertEqual(bot.calls, [])


class StartupTaskLifecycleTests(AvatarRotationTestCase):
    async def test_start_creates_directory_and_no_task_without_run_on_start(self) -> None:
        service = make_service(self.tmp)
        await service.start()
        self.assertTrue(service.image_dir().is_dir())
        self.assertIsNone(service._startup_task)
        await service.stop()

    async def test_startup_task_reference_is_kept_and_cleared(self) -> None:
        service = make_service(self.tmp, auto_change={"run_on_start": True})
        self.write_images(service, "only.png")
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_rotation(reason: str = "schedule") -> None:
            started.set()
            await release.wait()

        service._run_scheduled_rotation = slow_rotation  # type: ignore[method-assign]
        await service.start()
        await asyncio.wait_for(started.wait(), timeout=1)
        task = service._startup_task
        self.assertIsNotNone(task)

        release.set()
        await asyncio.wait_for(task, timeout=1)  # type: ignore[arg-type]
        await asyncio.sleep(0)
        self.assertIsNone(service._startup_task)
        await service.stop()

    async def test_stop_cancels_pending_startup_task(self) -> None:
        service = make_service(self.tmp, auto_change={"run_on_start": True})
        started = asyncio.Event()

        async def never_ending(reason: str = "schedule") -> None:
            started.set()
            await asyncio.Event().wait()

        service._run_scheduled_rotation = never_ending  # type: ignore[method-assign]
        await service.start()
        await asyncio.wait_for(started.wait(), timeout=1)
        task = service._startup_task
        self.assertIsNotNone(task)

        await service.stop()

        self.assertIsNone(service._startup_task)
        self.assertTrue(task.cancelled())  # type: ignore[union-attr]

    async def test_stop_swallows_startup_task_failure(self) -> None:
        service = make_service(self.tmp, auto_change={"run_on_start": True})

        async def boom(reason: str = "schedule") -> None:
            raise RuntimeError("rotation exploded")

        service._run_scheduled_rotation = boom  # type: ignore[method-assign]
        await service.start()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await service.stop()
        self.assertIsNone(service._startup_task)


if __name__ == "__main__":
    unittest.main()
