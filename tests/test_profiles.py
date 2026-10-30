"""Browser profile discovery and cookie import boundaries."""

import contextlib
import io
import json
import os
import sqlite3
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from providers.profiles import (
    BrowserProfile,
    ProfileError,
    default_profile,
    import_cookies,
    select_cookies,
)
from providers.tests.fakes import cookie


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
        self.assertEqual(result.family, "chromium")
        self.assertEqual(result.cookie_backend, "brave")
        self.assertEqual(result.cookies, path)
        self.assertEqual(result.key_file, path.parents[2] / "Local State")

    def test_oversized_chromium_state_does_not_select_another_profile(self):
        root = self.home / ".config/BraveSoftware/Brave-Origin"
        self.chromium(root)
        with (root / "Local State").open("wb") as stream:
            stream.truncate(8 * 1024 * 1024 + 1)
        with (
            patch("providers.profiles._default_id", return_value="brave-origin.desktop"),
            patch("providers.profiles.sys.platform", "linux"),
            patch("providers.profiles.Path.home", return_value=self.home),
            patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.home / ".config")}),
        ):
            with self.assertRaises(ProfileError):
                default_profile()

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
                profile = default_profile()
                self.assertEqual(profile.cookies, path)
                self.assertEqual(profile.family, "chromium")
                self.assertEqual(profile.cookie_backend, browser)

    def test_safari_profile_is_rejected_outside_macos(self):
        with (
            patch("providers.profiles._default_id", return_value="safari"),
            patch("providers.profiles.sys.platform", "linux"),
        ):
            with self.assertRaisesRegex(ProfileError, "macOS"):
                default_profile()

    def test_macos_safari_reads_only_its_binary_cookie_profile(self):
        roots = (
            "Library/Containers/com.apple.Safari/Data/Library/Cookies/Cookies.binarycookies",
            "Library/Cookies/Cookies.binarycookies",
        )
        for root in roots:
            with self.subTest(root=root), tempfile.TemporaryDirectory() as folder:
                home = Path(folder)
                cookie_file = home / root
                cookie_file.parent.mkdir(parents=True)
                cookie_file.touch()
                with (
                    patch("providers.profiles._default_id", return_value="com.apple.safari"),
                    patch("providers.profiles.sys.platform", "darwin"),
                    patch("providers.profiles.Path.home", return_value=home),
                ):
                    profile = default_profile()
                self.assertEqual(profile, BrowserProfile("safari", "safari", cookie_file))

    def test_macos_safari_uses_last_active_named_profile_cookie_file(self):
        profile_id = "13EC6641-7983-4826-99EE-1D6D57AE3388"
        cookie_file = (
            self.home
            / "Library/Containers/com.apple.Safari/Data/Library/WebKit/WebsiteDataStore"
            / profile_id.lower()
            / "Cookies/Cookies.binarycookies"
        )
        cookie_file.parent.mkdir(parents=True)
        cookie_file.touch()
        active_profile = subprocess.CompletedProcess(["defaults"], 0, profile_id + "\n", "")
        with (
            patch("providers.profiles._default_id", return_value="com.apple.safari"),
            patch("providers.profiles.sys.platform", "darwin"),
            patch("providers.profiles.Path.home", return_value=self.home),
            patch("providers.profiles.subprocess.run", return_value=active_profile),
        ):
            profile = default_profile()
        self.assertEqual(profile, BrowserProfile("safari", "safari", cookie_file))

    def test_macos_safari_never_falls_back_from_missing_active_profile(self):
        profile_id = "13EC6641-7983-4826-99EE-1D6D57AE3388"
        default_cookie = (
            self.home
            / "Library/Containers/com.apple.Safari/Data/Library/Cookies/Cookies.binarycookies"
        )
        default_cookie.parent.mkdir(parents=True)
        default_cookie.touch()
        active_profile = subprocess.CompletedProcess(["defaults"], 0, profile_id + "\n", "")
        with (
            patch("providers.profiles._default_id", return_value="com.apple.safari"),
            patch("providers.profiles.sys.platform", "darwin"),
            patch("providers.profiles.Path.home", return_value=self.home),
            patch("providers.profiles.subprocess.run", return_value=active_profile),
        ):
            with self.assertRaisesRegex(ProfileError, "Son kullanılan Safari profilinin"):
                default_profile()

    def test_profile_worker_uses_safari_reader_and_provider_cookie_filter(self):
        from providers.profiles import _main

        cookie_file = self.home / "Cookies.binarycookies"
        cookie_file.touch()
        data = {
            "provider": "claude",
            "family": "safari",
            "cookie_backend": "safari",
            "cookies": str(cookie_file),
            "key_file": None,
        }
        output = io.StringIO()
        with (
            patch("sys.stdin", io.StringIO(json.dumps(data))),
            contextlib.redirect_stdout(output),
            patch(
                "browser_cookie3.safari",
                return_value=[cookie("sessionKey", domain=".claude.ai")],
            ) as safari,
        ):
            self.assertEqual(_main(), 0)

        safari.assert_called_once_with(
            cookie_file=str(cookie_file), key_file=None, domain_name="claude.ai"
        )
        self.assertEqual(json.loads(output.getvalue()), {"sessionKey": "secret"})

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
                    profile = default_profile()
                    self.assertEqual(profile.cookies, root / "chosen/cookies.sqlite")
                    self.assertEqual(profile.family, "firefox")
                    self.assertIsNone(profile.cookie_backend)

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
        profile = BrowserProfile("chromium", "brave", self.home / "Cookies")
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
                    "family": "chromium",
                    "cookie_backend": "brave",
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
        profile = BrowserProfile("chromium", "brave", self.home / "Cookies")
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
            "providers.profiles.default_profile",
            return_value=BrowserProfile("firefox", None, path),
        ):
            browser, values = import_cookies("claude")
        with patch(
            "providers.profiles.default_profile",
            return_value=BrowserProfile("firefox", None, path),
        ):
            zen_browser, zen_values = import_cookies("claude")
        self.assertEqual(zen_browser, "firefox")
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
                "providers.profiles.default_profile",
                return_value=BrowserProfile("firefox", None, path),
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
            "family": "chromium",
            "cookie_backend": "brave",
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
