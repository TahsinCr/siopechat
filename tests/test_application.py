import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]


class ApplicationTests(unittest.TestCase):
    def setUp(self):
        import backend as module
        from backend import BackendService

        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.app = BackendService(Path(self.directory.name))
        self.module = module

    def test_legacy_snapshots_remain_readable_and_new_writes_use_project_data(self):
        cache = self.app

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = root / "old"
            new = root / "data"
            (old / "conversations").mkdir(parents=True)
            original = {"id": "example", "title": "Old", "messages": []}
            (old / "conversations/example.json").write_text(json.dumps(original))
            (old / "active-conversation.json").write_text('{"conversation_id":"example"}')
            with (
                patch.object(cache, "root", new.parent),
                patch.object(self.module, "legacy_data_dir", return_value=old),
            ):
                self.assertEqual(cache.load_cached_conversation(), original)
                cache.rename_cached_conversation("example", "New")
                self.assertEqual(cache.load_cached_conversation()["title"], "New")
            self.assertEqual(json.loads((old / "conversations/example.json").read_text()), original)
            self.assertTrue((new / "conversations/example.json").is_file())

    def test_source_entrypoint_works_outside_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, str(ROOT / "siopechat.py"), "--help"],
                cwd=directory,
                env=os.environ | {"SIOPECHAT_HOME": directory},
                capture_output=True,
                text=True,
                timeout=10,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("workspace", result.stdout)
        self.assertIn("auth", result.stdout)

    def test_auth_helper_path_survives_package_move(self):
        from providers.chatgpt import BrowserSession, SessionPaths
        from providers.chatgpt import session as module

        completed = Mock(
            returncode=0,
            stdout=json.dumps(
                {
                    "url": "https://chatgpt.com/backend-api/me",
                    "headers": {"Authorization": "test-token"},
                }
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = BrowserSession(
                SessionPaths(root / "session.json", root / "cache", root / "profile")
            )
            with patch.object(module.subprocess, "run", return_value=completed) as run:
                session.capture_auth()
        self.assertEqual(
            Path(run.call_args.args[0][1]), ROOT / "src/providers/chatgpt/scripts/browser/auth.cjs"
        )

    def test_daemon_spawn_uses_module_and_preserves_foreground_diagnostic(self):
        process = Mock(returncode=1)
        process.poll.return_value = 1
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(self.module.subprocess, "Popen", return_value=process) as spawn:
                with patch.object(self.app, "daemon_is_ready", return_value=False):
                    with self.assertRaisesRegex(self.module.ChatGPTWebError, "foreground"):
                        self.app.start_daemon(
                            Path(directory) / "daemon.sock", "http://localhost:9222"
                        )
        self.assertEqual(spawn.call_args.args[0][1:3], ["-m", "bootstrap"])
        self.assertTrue(spawn.call_args.kwargs["env"]["PYTHONPATH"].startswith(str(ROOT / "src")))

    def test_fullscreen_network_callbacks_do_not_duplicate_daemon_cache_writes(self):
        from cli import chat, commands

        backend = Mock()
        backend.daemon_request.return_value = {"conversation": {"id": "example", "messages": []}}
        backend.daemon_stream_request.return_value = {
            "conversation": {"id": "example", "messages": []}
        }
        api = Mock()
        from settings import DEFAULTS

        api.settings.snapshot.return_value = DEFAULTS
        accounts = Mock()
        accounts.active.return_value = "chatgpt/default"
        accounts.archive.return_value = None
        accounts.callbacks.return_value = {
            "load_conversation": Mock(return_value={"id": "example"}),
            "send_prompt": Mock(return_value={}),
        }
        api.get.side_effect = lambda key: {
            "accounts": accounts,
            "backend": backend,
            "paths": Path("unused-root"),
        }.get(key, Mock())
        with patch.object(chat, "run_tui") as run_tui:
            with patch.object(self.app, "cache_conversation") as cache:
                commands.run_fullscreen(api, Path("unused.sock"))
                callbacks = run_tui.call_args.args
                self.assertEqual(callbacks[1]("example")["id"], "example")
                callbacks[2]("example", "hello", [], Mock(), Mock())
                cache.assert_not_called()

    def test_rename_updates_cache_at_daemon_boundary(self):
        client = Mock()
        with patch.object(self.app, "rename_cached_conversation") as cache:
            self.app.handle_daemon_command(
                client,
                {
                    "command": "rename",
                    "conversation_id": "example",
                    "title": " New name ",
                },
            )
        client.rename_conversation.assert_called_once_with("example", "New name")
        cache.assert_called_once_with("example", "New name")

    def test_rate_limit_is_not_automatically_retried(self):
        import httpx

        from providers.chatgpt import ChatGPTClient
        from providers.errors import RateLimited

        calls = []

        def handle(request):
            calls.append(request)
            return httpx.Response(429, headers={"retry-after": "30"}, json={})

        client = object.__new__(ChatGPTClient)
        client.headers = {}
        with httpx.Client(transport=httpx.MockTransport(handle)) as http:
            client.http = http
            with self.assertRaises(RateLimited) as raised:
                client.request("GET", "https://chatgpt.com/backend-api/me")
        self.assertEqual(raised.exception.retry_after, 30)
        self.assertEqual(len(calls), 1)
