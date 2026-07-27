from __future__ import annotations

import unittest
from types import SimpleNamespace

from astrbot_plugin_helper_tools.main import HelperToolsPlugin


class FakeRollPigService:
    def __init__(self) -> None:
        self.calls = 0

    async def build_chain(self, event):
        self.calls += 1
        return ["rollpig-card"], ""


class FakeEvent:
    def __init__(self) -> None:
        self.stopped = False

    def plain_result(self, text: str):
        return ("plain", text)

    def chain_result(self, chain):
        return ("chain", chain)

    def stop_event(self) -> None:
        self.stopped = True


class RollPigCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_enabled_command_returns_the_service_chain(self) -> None:
        service = FakeRollPigService()
        plugin = SimpleNamespace(
            config={"rollpig": {"enabled": True, "commands_enabled": True}},
            enabled=lambda: True,
            rollpig=service,
        )
        event = FakeEvent()

        results = [
            result async for result in HelperToolsPlugin.rollpig_command(plugin, event)
        ]

        self.assertEqual(results, [("chain", ["rollpig-card"])])
        self.assertEqual(service.calls, 1)
        self.assertTrue(event.stopped)

    async def test_disabled_command_does_not_call_the_service(self) -> None:
        service = FakeRollPigService()
        plugin = SimpleNamespace(
            config={"rollpig": {"enabled": False, "commands_enabled": True}},
            enabled=lambda: True,
            rollpig=service,
        )
        event = FakeEvent()

        results = [
            result async for result in HelperToolsPlugin.rollpig_command(plugin, event)
        ]

        self.assertEqual(results, [("plain", "今日小猪命令当前未启用。")])
        self.assertEqual(service.calls, 0)
        self.assertTrue(event.stopped)


if __name__ == "__main__":
    unittest.main()
