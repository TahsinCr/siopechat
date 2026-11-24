import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from webaiprovider import MemoryCredentialStore, RateLimited
from webaiprovider.providers.chatgpt.auth import BrowserSession
from webaiprovider.providers.chatgpt.client import ChatGPTClient
from webaiprovider.transport.base import Transport

from bootstrap import create_application

ROOT = Path(__file__).resolve().parents[1]


class ApplicationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.app = create_application(Path(self.directory.name))
        self.addCleanup(self.app.runtime.close)

    def test_source_entrypoint_works_outside_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            env = os.environ | {
                "SIOPECHAT_CONFIG_PATH": str(Path(directory) / "config.json"),
                "SIOPECHAT_DATA_DIR": str(Path(directory) / "data"),
                "SIOPECHAT_STATE_DIR": str(Path(directory) / "state"),
                "SIOPECHAT_DOWNLOADS_DIR": str(Path(directory) / "downloads"),
                "SIOPECHAT_CACHE_DIR": str(Path(directory) / ".cache"),
                "SIOPECHAT_RUNTIME_DIR": str(Path(directory) / ".runtime"),
            }
            result = subprocess.run(
                [sys.executable, str(ROOT / "siopechat.py"), "--help"],
                cwd=directory,
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("workspace", result.stdout)
        self.assertIn("auth", result.stdout)

    def test_fullscreen_wires_native_account_callbacks_and_workspaces(self):
        from prompt_toolkit.application import Application as PTApplication
        from prompt_toolkit.input import DummyInput
        from prompt_toolkit.output import DummyOutput

        from tui.application import run_fullscreen
        from tui.chat.view import ChatView

        captured = {}

        def fake_run(view):
            captured["view"] = view
            captured["rows"] = view.settings_rows()
            captured["updated"] = view.change_setting("application", "language", "tr")
            captured["workspaces"] = view.list_workspaces()
            captured["layout_list"] = view.invoke_action("layout.list")
            captured["actions"] = view.list_actions()
            captured["sidebar_caps"] = view._sidebar_provider_capabilities("chatgpt")
            captured["grok_caps"] = view._sidebar_provider_capabilities("grok")

        with (
            patch(
                "tui.chat.view.Application",
                side_effect=lambda **kw: PTApplication(
                    input=DummyInput(), output=DummyOutput(), **kw
                ),
            ),
            patch.object(ChatView, "run", fake_run),
        ):
            result = run_fullscreen(self.app)

        self.assertEqual(result, 0)
        view = captured["view"]
        self.assertEqual(view.title, "New conversation")
        self.assertIs(view.account_service, self.app.accounts)
        self.assertEqual(captured["workspaces"], [])
        self.assertEqual(captured["layout_list"], [])
        self.assertEqual(captured["updated"]["settings"]["application"]["language"], "tr")
        self.assertTrue(any(row["section"] == "application" for row in captured["rows"]))
        self.assertTrue(any(action["id"] == "layout.list" for action in captured["actions"]))
        self.assertIn("rename", {c.value for c in captured["sidebar_caps"]})
        self.assertIn("delete", {c.value for c in captured["sidebar_caps"]})
        self.assertNotIn("effort", {c.value for c in captured["grok_caps"]})

    def test_fullscreen_initializes_with_configured_account_real_callbacks(self):
        from prompt_toolkit.application import Application as PTApplication
        from prompt_toolkit.input import DummyInput
        from prompt_toolkit.output import DummyOutput

        from tui.application import run_fullscreen
        from tui.chat.view import ChatView

        key = self.app.accounts.add("chatgpt", "Work")
        self.app.accounts.select(key)
        captured = {}

        def fake_run(view):
            captured["view"] = view

        with (
            patch(
                "tui.chat.view.Application",
                side_effect=lambda **kw: PTApplication(
                    input=DummyInput(), output=DummyOutput(), **kw
                ),
            ),
            patch.object(ChatView, "run", fake_run),
        ):
            result = run_fullscreen(self.app)

        self.assertEqual(result, 0)
        view = captured["view"]
        self.assertEqual(view.account_key, key)
        self.assertTrue(callable(view.list_projects_callback))
        self.assertTrue(callable(view.get_project_callback))
        self.assertTrue(callable(view.list_project_chats_callback))
        self.assertTrue(callable(view.attach_file_callback))
        self.assertTrue(callable(view.attachment_policy_callback))
        self.assertTrue(callable(view.release_conversation))

    def test_cli_providers_reports_real_per_provider_capabilities(self):
        from io import StringIO

        from cli import main as cli_main

        stdout = StringIO()
        with patch("sys.stdout", stdout):
            code = cli_main(self.app, ["providers"])
        self.assertEqual(code, 0)
        providers = {item["id"]: item for item in json.loads(stdout.getvalue())}
        self.assertIn("effort", providers["chatgpt"]["capabilities"])
        self.assertIn("chat_branch", providers["chatgpt"]["capabilities"])
        self.assertNotIn("effort", providers["grok"]["capabilities"])
        self.assertNotIn("chat_branch", providers["grok"]["capabilities"])
        self.assertNotIn("chat_branch", providers["claude"]["capabilities"])
        self.assertNotIn("project_membership", providers["gemini"]["capabilities"])

    def test_settings_change_rejects_unknown_provider_setting_key(self):
        with self.assertRaises(ValueError):
            self.app.actions.invoke_sync(
                "settings.change",
                section="providers.chatgpt",
                key="unknown_field",
                value=True,
            )

    def test_rate_limit_is_not_automatically_retried(self):
        calls = []

        class RateLimitTransport(Transport):
            def __init__(self):
                super().__init__("https://chatgpt.com")

            def send(self, method, path, **options):
                calls.append((method, path))
                response = SimpleNamespace(
                    status_code=429,
                    headers={"retry-after": "30"},
                    content=json.dumps({}).encode(),
                    text="{}",
                    json=lambda: {},
                )
                self._check_response(response, options.get("check_status", True))
                return response

            def stream(self, method, path, **options):
                return nullcontext()

            def close(self):
                pass

        store = MemoryCredentialStore(
            {
                "version": 1,
                "url": "https://chatgpt.com/backend-api/conversations",
                "headers": {"authorization": "Bearer token"},
            }
        )
        transport = RateLimitTransport()
        client = ChatGPTClient(BrowserSession(store), authenticate=False, transport=transport)
        try:
            with self.assertRaises(RateLimited) as raised:
                client.request("GET", "https://chatgpt.com/backend-api/me")
        finally:
            client.close()
        self.assertEqual(raised.exception.retry_after, 30)
        self.assertEqual(len(calls), 1)
