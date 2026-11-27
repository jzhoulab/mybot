"""The owner, asking where only they read the answer, gets an answer agent
with the reach of their own Claude Code; everyone else keeps the sandbox."""

from __future__ import annotations

import json
import os
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.server import AppState, ProviderClient


def state(backend: str = "claude_cli") -> SimpleNamespace:
    return SimpleNamespace(
        actor_is_owner=lambda actor: actor == "owner",
        provider=SimpleNamespace(active_model=lambda: {"backend": backend}),
    )


class OwnerFullAccessTest(unittest.TestCase):
    def test_only_the_owner_in_a_private_channel(self) -> None:
        with patch.dict(os.environ, {"MYBOT_OWNER_FULL_ACCESS": "1"}):
            allowed = {
                (actor, kind): AppState.owner_full_access(state(), actor_id=actor, channel_kind=kind)
                for actor in ("owner", "teammate") for kind in ("", "dm", "group")
            }
        self.assertEqual(allowed, {
            ("owner", ""): True, ("owner", "dm"): True, ("owner", "group"): False,
            ("teammate", ""): False, ("teammate", "dm"): False, ("teammate", "group"): False,
        })

    def test_off_switch_and_other_backends(self) -> None:
        with patch.dict(os.environ, {"MYBOT_OWNER_FULL_ACCESS": "0"}):
            self.assertFalse(AppState.owner_full_access(state(), actor_id="owner", channel_kind="dm"))
        with patch.dict(os.environ, {"MYBOT_OWNER_FULL_ACCESS": "1"}):
            self.assertFalse(AppState.owner_full_access(state("codex_cli"), actor_id="owner", channel_kind="dm"))


class ClaudeCommandTest(unittest.TestCase):
    def provider(self) -> SimpleNamespace:
        fake = SimpleNamespace(config=SimpleNamespace(
            claude_command="claude", claude_bash_sandbox=True, mybot_tool_python="python",
            mybot_tool_path="/rt/mybot_tool.py", codex_cwd="/rt"))
        for name in ("_claude_owner_args", "_claude_isolation_args", "_claude_sandbox_settings_json"):
            setattr(fake, name, types.MethodType(getattr(ProviderClient, name), fake))
        return fake

    def cmd(self, **kwargs) -> list[str]:
        args = dict(model="opus", effort="high", system_prompt="sys", ephemeral=False, streaming=False)
        args.update(kwargs)
        return ProviderClient.claude_answer_cmd(self.provider(), **args)

    @staticmethod
    def settings(cmd: list[str]) -> dict:
        return json.loads(cmd[cmd.index("--settings") + 1])

    def test_owner_mode_runs_like_their_own_claude_code(self) -> None:
        cmd = self.cmd(owner_mode=True)
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "auto")
        self.assertEqual(cmd[cmd.index("--setting-sources") + 1], "user")
        self.assertEqual(self.settings(cmd), {"disableAllHooks": True, "autoMemoryEnabled": False})
        self.assertEqual(cmd[cmd.index("--add-dir") + 1], os.path.expanduser("~"))
        self.assertNotIn("--strict-mcp-config", cmd)
        self.assertNotIn("--allowedTools", cmd)

    def test_everyone_else_keeps_the_sandbox(self) -> None:
        for cmd in (self.cmd(), self.cmd(owner_mode=True, ephemeral=True)):
            self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "default")
            self.assertIn("--strict-mcp-config", cmd)
            self.assertEqual(cmd[cmd.index("--setting-sources") + 1], "")
            self.assertTrue(self.settings(cmd)["disableClaudeAiConnectors"])
            self.assertNotIn("--add-dir", cmd)
        sandboxed = self.cmd()
        self.assertEqual(self.settings(sandboxed)["sandbox"]["filesystem"]["denyRead"], ["~/"])
        self.assertEqual(sandboxed[sandboxed.index("--allowedTools") + 1], "Bash")


class OwnerModePromptTest(unittest.TestCase):
    def test_owner_mode_drops_the_teammate_limits(self) -> None:
        fake = SimpleNamespace(owner_name=lambda: "Jian")
        section = AppState.owner_mode_section(fake)
        self.assertIn("grants and reviews", section)
        self.assertIn("Box-Box", section)
        self.assertIn("confirm", section)


if __name__ == "__main__":
    unittest.main()
