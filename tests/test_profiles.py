"""Profile discovery/import boundaries; never read real user profiles or secrets."""

import contextlib
import io
import json
import os
import sqlite3
import subprocess
import tempfile
import time
import unittest
from http.cookiejar import Cookie
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from providers.models import SessionState, SessionStatus
from providers.profiles import (
    BrowserProfile,
    ProfileError,
    default_profile,
    import_cookies,
    select_cookies,
)
from tests import test_accounts


def cookie(name, value="secret", domain=".google.com", expires=None):
    return Cookie(
        0,
        name,
        value,
        None,
        False,
        domain,
        True,
        domain.startswith("."),
        "/",
        True,
        True,
        expires,
        False,
        None,
        None,
        {},
        False,
    )


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.home = Path(self.folder.name)

    def chromium(self, root, last="Profile 2"):
        root.mkdir(parents=True)
        (root / "Local State").write_text(json.dumps({"profile": {"last_used": last}}))
        path = root / last / "Network/Cookies"
        path.parent.mkdir(parents=True)
        path.touch()
        return path

    def test_linux_brave_origin_uses_only_last_profile(self):
        path = self.chromium(self.home / ".config/BraveSoftware/Brave-Origin")
        self.chromium(self.home / ".config/BraveSoftware/Brave-Browser")
        with (
            patch("providers.profiles._default_id", return_value="brave-origin.desktop"),
            patch("providers.profiles.sys.platform", "linux"),
            patch("providers.profiles.Path.home", return_value=self.home),
            patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.home / ".config")}),
        ):
            result = default_profile()
        self.assertEqual(
            result, BrowserProfile("brave-origin", path, path.parents[2] / "Local State")
        )

    def test_windows_edge_and_macos_chrome_roots(self):
        cases = [
            ("win32", "MSEdgeHTM", "Microsoft/Edge/User Data", "edge"),
            ("darwin", "com.google.chrome", "Library/Application Support/Google/Chrome", "chrome"),
        ]
        for platform, name, root, browser in cases:
            path = self.chromium(self.home / root)
            with (
                patch("providers.profiles._default_id", return_value=name),
                patch("providers.profiles.sys.platform", platform),
                patch("providers.profiles.Path.home", return_value=self.home),
                patch.dict(os.environ, {"LOCALAPPDATA": str(self.home)}),
            ):
                self.assertEqual(default_profile().cookies, path)
                self.assertEqual(default_profile().browser, browser)

    def test_unsupported_default_does_not_try_other_installed_browsers(self):
        with patch("providers.profiles._default_id", return_value="safari"):
            with self.assertRaisesRegex(ProfileError, "desteklenmiyor"):
                default_profile()

    def test_expired_unrelated_and_similar_domains_are_rejected(self):
        jar = [
            cookie("__Secure-1PSID"),
            cookie("SID", "wrong", ".evilgoogle.com"),
            cookie("HSID", "wrong", ".google.com.evil.test"),
            cookie("unrelated"),
            cookie("SSID", expires=time.time() - 5),
            cookie("APISID", "bad\r\nvalue"),
        ]
        self.assertEqual(select_cookies(jar, "gemini"), {"__Secure-1PSID": "secret"})
        with self.assertRaises(ProfileError):
            select_cookies([cookie("sessionKey", domain=".claude.ai")], "gemini")

    def test_zen_and_firefox_profile_registry_locations(self):
        cases = [
            ("zen.desktop", ".config/zen"),
            ("zen-browser.desktop", ".zen"),
            ("app.zen_browser.zen.desktop", ".var/app/app.zen_browser.zen/config/zen"),
            ("firefox_firefox.desktop", "snap/firefox/common/.mozilla/firefox"),
        ]
        for name, relative in cases:
            with self.subTest(browser=name), tempfile.TemporaryDirectory() as folder:
                home = Path(folder)
                root = home / relative
                root.mkdir(parents=True)
                (root / "profiles.ini").write_text(
                    "[Profile0]\nName=Personal\nIsRelative=1\nPath=chosen\n"
                )
                (root / "installs.ini").write_text("[installation]\nDefault=chosen\n")
                (root / "chosen").mkdir()
                (root / "chosen/cookies.sqlite").touch()
                with (
                    patch("providers.profiles._default_id", return_value=name),
                    patch("providers.profiles.sys.platform", "linux"),
                    patch("providers.profiles.Path.home", return_value=home),
                    patch.dict(os.environ, {"XDG_CONFIG_HOME": str(home / ".config")}),
                ):
                    self.assertEqual(default_profile().cookies, root / "chosen/cookies.sqlite")

    def test_firefox_does_not_guess_between_multiple_unselected_profiles(self):
        from providers.profiles import _firefox_profile

        root = self.home / "firefox"
        root.mkdir()
        (root / "profiles.ini").write_text("[Profile0]\nPath=first\n[Profile1]\nPath=second\n")
        with self.assertRaisesRegex(ProfileError, "Varsayılan profil belirlenemedi"):
            _firefox_profile(root)

    def test_chunked_chatgpt_session_cookie(self):
        values = select_cookies(
            [
                cookie("__Secure-next-auth.session-token.0", domain=".chatgpt.com"),
                cookie("__Secure-next-auth.session-token.1", domain=".chatgpt.com"),
            ],
            "chatgpt",
        )
        self.assertEqual(len(values), 2)

    def test_keyring_timeout_and_unsafe_errors_are_sanitized(self):
        profile = BrowserProfile("brave", self.home / "Cookies")
        with (
            patch("providers.profiles.default_profile", return_value=profile),
            patch(
                "providers.profiles.subprocess.run",
                side_effect=subprocess.TimeoutExpired("hidden", 25),
            ),
        ):
            with self.assertRaisesRegex(ProfileError, "zaman aşımı"):
                import_cookies("claude")
        with (
            patch("providers.profiles.default_profile", return_value=profile),
            patch(
                "providers.profiles.subprocess.run",
                return_value=Mock(returncode=1, stderr="sensitive"),
            ),
        ):
            with self.assertRaises(ProfileError) as error:
                import_cookies("claude")
            self.assertNotIn("sensitive", str(error.exception))

    def test_worker_errors_have_safe_distinct_categories(self):
        from providers.profiles import _failure_code, _main

        cases = [
            (ModuleNotFoundError("private-value"), 3),
            (PermissionError("private-value"), 4),
            (sqlite3.OperationalError("database is locked: private-value"), 5),
            (RuntimeError("Can not find secret for brave: private-value"), 6),
            (RuntimeError("Unable to get key for cookie decryption: private-value"), 7),
            (RuntimeError("private-value"), 1),
        ]
        for error, code in cases:
            with self.subTest(code=code):
                self.assertEqual(_failure_code(error), code)
                if code == 3:
                    continue
                data = {
                    "provider": "claude",
                    "browser": "brave",
                    "cookies": "/unused",
                    "key_file": None,
                }
                output = io.StringIO()
                with (
                    patch("sys.stdin", io.StringIO(json.dumps(data))),
                    contextlib.redirect_stdout(output),
                    patch("browser_cookie3.brave", side_effect=error),
                ):
                    self.assertEqual(_main(), code)
                self.assertEqual(output.getvalue(), "")

    def test_dependency_failure_does_not_claim_keyring_or_browser_lock(self):
        profile = BrowserProfile("brave", self.home / "Cookies")
        with (
            patch("providers.profiles.default_profile", return_value=profile),
            patch(
                "providers.profiles.subprocess.run",
                return_value=Mock(returncode=3, stderr="private-value"),
            ),
        ):
            with self.assertRaises(ProfileError) as error:
                import_cookies("claude")
        self.assertIn("bağımlılıkları eksik", str(error.exception))
        self.assertNotIn("tarayıcıyı kapat", str(error.exception))
        self.assertNotIn("private-value", str(error.exception))

    def test_real_worker_reads_only_firefox_default_container_without_launching_browser(self):
        path = self.home / "cookies.sqlite"
        with contextlib.closing(sqlite3.connect(path)) as db:
            db.execute(
                "CREATE TABLE moz_cookies(host,path,isSecure,expiry,name,value,isHttpOnly,originAttributes)"
            )
            for attributes, value in [
                ("", "default-session"),
                ("^userContextId=1", "container-session"),
            ]:
                db.execute(
                    "INSERT INTO moz_cookies VALUES(?,?,?,?,?,?,?,?)",
                    (".claude.ai", "/", 1, time.time() + 3600, "sessionKey", value, 1, attributes),
                )
            db.commit()
        before = path.read_bytes()
        with patch(
            "providers.profiles.default_profile", return_value=BrowserProfile("firefox", path)
        ):
            browser, values = import_cookies("claude")
        with patch("providers.profiles.default_profile", return_value=BrowserProfile("zen", path)):
            zen_browser, zen_values = import_cookies("claude")
        self.assertEqual(zen_browser, "zen")
        self.assertEqual(zen_values, values)
        self.assertEqual(browser, "firefox")
        self.assertEqual(values, {"sessionKey": "default-session"})
        self.assertEqual(before, path.read_bytes())

    def test_firefox_locked_database_is_read_from_private_snapshot(self):
        from providers.profiles import _firefox_rows

        path = self.home / "locked.sqlite"
        with contextlib.closing(sqlite3.connect(path)) as db:
            db.execute(
                "CREATE TABLE moz_cookies(host,path,isSecure,expiry,name,value,isHttpOnly,originAttributes)"
            )
            db.execute(
                "INSERT INTO moz_cookies VALUES(?,?,?,?,?,?,?,?)",
                (".claude.ai", "/", 1, time.time() + 3600, "sessionKey", "committed", 1, ""),
            )
            db.commit()
            db.execute("BEGIN EXCLUSIVE")
            before = path.read_bytes()
            self.assertEqual(_firefox_rows(path, "claude")[0][5], "committed")
            self.assertEqual(path.read_bytes(), before)
            db.rollback()

    def test_firefox_snapshot_keeps_committed_wal_cookies(self):
        from providers.profiles import _firefox_rows

        path = self.home / "wal.sqlite"
        with contextlib.closing(sqlite3.connect(path)) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(
                "CREATE TABLE moz_cookies(host,path,isSecure,expiry,name,value,isHttpOnly,originAttributes)"
            )
            db.execute(
                "INSERT INTO moz_cookies VALUES(?,?,?,?,?,?,?,?)",
                (".claude.ai", "/", 1, time.time() + 3600, "sessionKey", "in-wal", 1, ""),
            )
            db.commit()
            original = sqlite3.connect

            def connect(database, **kwargs):
                if str(database).startswith(path.resolve().as_uri()):
                    raise sqlite3.OperationalError("database is locked")
                return original(database, **kwargs)

            with patch("providers.profiles.sqlite3.connect", side_effect=connect):
                self.assertEqual(_firefox_rows(path, "claude")[0][5], "in-wal")

    def test_firefox_worker_imports_live_sessionstore_cookies_without_merging_containers(self):
        import lz4.block

        path = self.home / "cookies.sqlite"
        with contextlib.closing(sqlite3.connect(path)) as db:
            db.execute(
                "CREATE TABLE moz_cookies(host,path,isSecure,expiry,name,value,isHttpOnly,originAttributes)"
            )
            db.commit()
        directory = self.home / "sessionstore-backups"
        directory.mkdir()
        current = directory / "recovery.jsonlz4"
        payload = {
            "cookies": [
                {
                    "host": ".claude.ai",
                    "path": "/",
                    "name": "sessionKey",
                    "value": "live-session",
                    "secure": True,
                },
                {
                    "host": ".claude.ai",
                    "path": "/",
                    "name": "sessionKey",
                    "value": "other-container",
                    "originAttributes": {"userContextId": 1},
                },
                {"host": ".evil.test", "path": "/", "name": "sessionKey", "value": "foreign"},
            ]
        }
        current.write_bytes(b"mozLz40\0" + lz4.block.compress(json.dumps(payload).encode()))
        for browser in ("firefox", "zen"):
            with patch(
                "providers.profiles.default_profile", return_value=BrowserProfile(browser, path)
            ):
                _, values = import_cookies("claude")
            self.assertEqual(values, {"sessionKey": "live-session"})

    def test_firefox_session_reader_uses_one_snapshot_and_legacy_windows(self):
        from providers.profiles import _firefox_session_cookies

        old = self.home / "sessionstore.js"
        old.write_text(
            json.dumps(
                {
                    "windows": [
                        {
                            "cookies": [
                                {"host": ".claude.ai", "name": "sessionKey", "value": "legacy"}
                            ]
                        }
                    ]
                }
            )
        )
        self.assertEqual(_firefox_session_cookies(self.home, "claude")[0]["value"], "legacy")
        new = self.home / "sessionstore.jsonlz4"
        import lz4.block

        new.write_bytes(b"mozLz40\0" + lz4.block.compress(b'{"cookies":[]}'))
        os.utime(old, (1, 1))
        self.assertEqual(_firefox_session_cookies(self.home, "claude"), [])
        new.write_bytes(b"invalid")
        self.assertEqual(_firefox_session_cookies(self.home, "claude"), [])

    def test_worker_failure_never_prints_library_exception(self):
        from providers.profiles import _main

        data = {
            "provider": "claude",
            "browser": "brave",
            "cookies": "/nonexistent",
            "key_file": None,
        }
        output = io.StringIO()
        with (
            patch("sys.stdin", io.StringIO(json.dumps(data))),
            contextlib.redirect_stdout(output),
            patch("browser_cookie3.brave", side_effect=RuntimeError("private-value")),
        ):
            self.assertEqual(_main(), 1)
        self.assertEqual(output.getvalue(), "")


class ProfileAccountTests(unittest.TestCase):
    setUp = test_accounts.AccountTests.setUp

    def test_rejected_candidate_preserves_current_session_and_verification(self):
        path = self.accounts.credential_path(self.gemini)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"__Secure-1PSID":"previous"}')
        self.accounts._remember_verification(self.gemini)
        candidate = Mock(
            authenticate=AsyncMock(side_effect=RuntimeError("rejected")), close=AsyncMock()
        )
        with (
            patch(
                "accounts.import_cookies", return_value=("brave-origin", {"__Secure-1PSID": "new"})
            ),
            patch("accounts.GeminiProvider", return_value=candidate),
        ):
            with self.assertRaisesRegex(RuntimeError, "rejected"):
                self.accounts.session_action(self.gemini, "verify_profile")
        self.assertEqual(json.loads(path.read_text()), {"__Secure-1PSID": "previous"})
        self.assertTrue(self.accounts.status(self.gemini)["authenticated"])
        self.assertFalse(self.accounts._session_changes)
        candidate.close.assert_awaited_once()

    def test_success_saves_only_selected_account_after_remote_verification(self):
        path = self.accounts.credential_path(self.gemini)
        old = Mock(close=AsyncMock())
        self.accounts._providers[self.gemini] = old
        self.accounts._catalogs[self.gemini] = "old catalog"
        candidate = Mock(
            authenticate=AsyncMock(return_value=SessionStatus(SessionState.AUTHENTICATED, True)),
            close=AsyncMock(),
        )
        with (
            patch(
                "accounts.import_cookies", return_value=("brave-origin", {"__Secure-1PSID": "new"})
            ),
            patch("accounts.GeminiProvider", return_value=candidate),
            patch(
                "providers.chatgpt.session.BrowserSession.launch",
                side_effect=AssertionError("browser"),
            ),
        ):
            state = self.accounts.session_action(self.gemini, "verify_profile")
        self.assertTrue(state["authenticated"])
        self.assertEqual(state["imported_browser"], "brave-origin")
        self.assertEqual(json.loads(path.read_text()), {"__Secure-1PSID": "new"})
        self.assertNotIn(self.gemini, self.accounts._catalogs)
        self.assertFalse(self.accounts.credential_path("chatgpt/default").exists())
        if os.name != "nt":
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        old.close.assert_awaited_once()

    def test_import_is_blocked_during_send_before_reading_profile(self):
        self.accounts._sending[self.gemini] = 1
        with patch("accounts.import_cookies") as read:
            with self.assertRaises(RuntimeError):
                self.accounts.session_action(self.gemini, "verify_profile")
        read.assert_not_called()


class ProfileUiTests(unittest.IsolatedAsyncioTestCase):
    async def test_profile_button_triggers_common_async_action_and_refresh(self):
        from tests.test_cli_interaction import terminal

        with terminal(
            session_action=lambda action: {
                "authenticated": True,
                "imported_browser": "brave-origin",
            }
        ) as (view, _):
            pending = []
            view._read_in_background = lambda read, success, failure, **kw: pending.append(
                (read, success)
            )
            view.refresh_sidebar = Mock()
            view.refresh_provider_catalog = Mock()
            self.assertEqual(
                set(view.session_dialog.buttons), {"verify_profile", "logout", "remove"}
            )
            view.session_dialog.buttons["verify_profile"].handler()
            self.assertTrue(view.session_dialog.loading)
            read, success = pending.pop()
            success(read())
            self.assertFalse(view.session_dialog.loading)
            self.assertTrue(view.session_state["authenticated"])
            self.assertIn("brave-origin", view.session_dialog.message)
            view.refresh_sidebar.assert_called_once()
            view.refresh_provider_catalog.assert_called_once()

    async def test_chatgpt_profile_auth_never_contacts_debugging_browser(self):
        from providers.chatgpt import BrowserSession, ChatGPTProvider, SessionPaths
        from tests.test_provider_adapter import FakeClient

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            session = BrowserSession(
                SessionPaths(root / "session.json", root / "cache", root / "profile")
            )
            client = FakeClient(session)
            provider = ChatGPTProvider(session, client_factory=lambda _: client)
            with (
                patch.object(session, "is_ready", side_effect=AssertionError("CDP probe")),
                patch.object(
                    session, "capture_auth", side_effect=AssertionError("browser capture")
                ),
            ):
                status = await provider.authenticate(capture_browser=False)
            self.assertEqual(status.state, SessionState.AUTHENTICATED)
            self.assertTrue(client.closed.is_set())
            await provider.close()

    async def test_profile_action_is_visible_in_resized_accounts_panel(self):
        import asyncio

        from tests.test_cli_interaction import InputTests, terminal
        from tests.test_terminal_responsiveness import ResizableOutput
        from tests.test_ui_controls import immediate

        output = ResizableOutput()
        with terminal(output=output, session_action=lambda _: {}) as (view, _):
            view._read_in_background = immediate
            run = asyncio.create_task(view.app.run_async())
            try:
                await InputTests.settle(self, lambda: view.app.is_running)
                view.settings_dialog.open("accounts")
                for columns, rows in ((100, 30), (80, 24), (60, 18), (40, 12)):
                    output.columns, output.rows = columns, rows
                    view.app.layout.focus(view.session_dialog.buttons["verify_profile"])
                    view.app.invalidate()
                    await asyncio.sleep(0.06)
                    screen = view.app.renderer._last_screen
                    text = "\n".join(
                        "".join(screen.data_buffer[y][x].char for x in range(columns))
                        for y in range(rows)
                    )
                    for buttons in (
                        [
                            view.session_dialog.add_account_button,
                            view.session_dialog.edit_account_button,
                            view.session_dialog.buttons["remove"],
                        ],
                        [
                            view.session_dialog.buttons["verify_profile"],
                            view.session_dialog.buttons["logout"],
                        ],
                    ):
                        labels = [button.text for button in buttons]
                        # The settings viewport may scroll either row off-screen.
                        visible = [line for line in text.splitlines() if labels[0] in line]
                        if visible:
                            self.assertTrue(
                                any(all(label in line for label in labels) for line in visible)
                            )
                    self.assertNotIn("Window too small", text)
                    self.assertNotIn("Bağlı tarayıcı", text)
                    self.assertNotIn("Tarayıcıda aç", text)
            finally:
                view.app.exit()
                await run


class ChatGPTProfileBootstrapTests(unittest.TestCase):
    def test_bootstrap_saves_provider_token_and_browser_headers_without_cdp(self):
        from unittest.mock import MagicMock

        from providers.chatgpt import BrowserSession, SessionPaths

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            session = BrowserSession(
                SessionPaths(root / "session.json", root / "cache", root / "profile")
            )
            http = MagicMock()
            http.get.return_value.status_code = 200
            http.get.return_value.json.return_value = {"accessToken": "verified-token"}
            http.cookies.jar = [
                cookie("__cf_bm", "fresh", domain=".chatgpt.com"),
                cookie("foreign", "ignore", domain=".evil.test"),
            ]
            factory = MagicMock()
            factory.return_value.__enter__.return_value = http
            with (
                patch("curl_cffi.requests.Session", factory),
                patch.object(session, "is_ready", side_effect=AssertionError("CDP")),
            ):
                session.import_cookies(
                    {"__Secure-next-auth.session-token": "login", "oai-did": "device"}
                )
            url, headers = session.load()
            self.assertEqual(url, "https://chatgpt.com/backend-api/conversations")
            self.assertEqual(headers["authorization"], "Bearer verified-token")
            self.assertIn("Chrome/", headers["user-agent"])
            self.assertEqual(headers["oai-device-id"], "device")
            self.assertIn("__cf_bm=fresh", headers["cookie"])
            self.assertNotIn("foreign", headers["cookie"])
            self.assertFalse(http.get.call_args.kwargs["allow_redirects"])
            self.assertEqual(http.get.call_args.args[0], "https://chatgpt.com/api/auth/session")

    def test_bootstrap_rejection_retains_previous_snapshot_and_does_not_follow_redirect(self):
        from unittest.mock import MagicMock

        from providers.chatgpt import BrowserSession, SessionPaths
        from providers.errors import ProviderError

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            session = BrowserSession(
                SessionPaths(root / "session.json", root / "cache", root / "profile")
            )
            session.save(
                "https://chatgpt.com/backend-api/conversations", {"authorization": "previous"}
            )
            before = session.paths.session_file.read_bytes()
            http = MagicMock()
            http.get.return_value.status_code = 302
            factory = MagicMock()
            factory.return_value.__enter__.return_value = http
            with patch("curl_cffi.requests.Session", factory), self.assertRaises(ProviderError):
                session.import_cookies({"__Secure-next-auth.session-token": "login"})
            self.assertEqual(session.paths.session_file.read_bytes(), before)
            http.get.assert_called_once()
