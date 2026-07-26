from __future__ import annotations

import ast
import unittest
from pathlib import Path

MAIN_PATH = Path(__file__).resolve().parents[1] / "main.py"


class BilibiliCommandNamespaceTests(unittest.TestCase):
    def test_qr_login_commands_use_helper_namespace(self) -> None:
        tree = ast.parse(MAIN_PATH.read_text(encoding="utf-8"))
        command_names: dict[str, str] = {}
        registered_tokens: set[str] = set()

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call):
                    continue
                if not isinstance(decorator.func, ast.Attribute):
                    continue
                if decorator.func.attr != "command" or not decorator.args:
                    continue
                argument = decorator.args[0]
                if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                    command_names[node.name] = argument.value
                    registered_tokens.add(argument.value)
                for keyword in decorator.keywords:
                    if keyword.arg != "alias" or not isinstance(
                        keyword.value, (ast.Set, ast.List, ast.Tuple)
                    ):
                        continue
                    for alias in keyword.value.elts:
                        if isinstance(alias, ast.Constant) and isinstance(alias.value, str):
                            registered_tokens.add(alias.value)

        expected = {
            "bilibili_login_command": "helper_bili_login",
            "bilibili_login_status_command": "helper_bili_login_status",
            "bilibili_login_cancel_command": "helper_bili_login_cancel",
            "bilibili_logout_command": "helper_bili_logout",
        }
        self.assertEqual({name: command_names[name] for name in expected}, expected)
        self.assertFalse(
            {"bili_login", "bili_logout", "B站登录", "B站扫码登录", "哔哩登录"}
            & registered_tokens
        )
