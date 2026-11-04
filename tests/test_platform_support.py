import tempfile
import unittest
from pathlib import Path, PurePosixPath, PureWindowsPath
from unittest.mock import patch

from utils import paths as platform
from utils import process


class TestPosixPath(PurePosixPath):
    @classmethod
    def home(cls):
        return cls("/Users/test")


class PlatformSupportTests(unittest.TestCase):
    def test_application_root_defaults_to_user_data_and_honors_override(self):
        with (
            patch.dict(platform.os.environ, {}, clear=True),
            patch.object(platform, "user_data_dir", return_value=Path("/tmp/siopechat-data")),
        ):
            self.assertEqual(platform.application_root(), Path("/tmp/siopechat-data"))

        with tempfile.TemporaryDirectory() as folder:
            configured = Path(folder) / "custom-data"
            with patch.dict(platform.os.environ, {"SIOPECHAT_HOME": str(configured)}, clear=True):
                self.assertEqual(platform.application_root(), configured.resolve())

    def test_checkout_user_data_is_copied_once_without_deleting_source(self):
        with tempfile.TemporaryDirectory() as folder:
            checkout = Path(folder) / "checkout"
            root = Path(folder) / "user-data"
            (checkout / "src").mkdir(parents=True)
            (checkout / "pyproject.toml").touch()
            (checkout / "src" / "bootstrap.py").touch()
            (checkout / "data").mkdir()
            (checkout / "data" / "accounts.sqlite3").write_bytes(b"account data")
            (checkout / "downloads").mkdir()
            (checkout / "downloads" / "saved.txt").write_text("download", encoding="utf-8")
            (checkout / "config.json").write_text('{"theme":"dark"}', encoding="utf-8")

            with patch.dict(platform.os.environ, {}, clear=True):
                platform.migrate_checkout_data(checkout, root)
                (root / "config.json").write_text('{"theme":"light"}', encoding="utf-8")
                platform.migrate_checkout_data(checkout, root)

            self.assertEqual((root / "data" / "accounts.sqlite3").read_bytes(), b"account data")
            self.assertEqual((root / "downloads" / "saved.txt").read_text(), "download")
            self.assertEqual((root / "config.json").read_text(), '{"theme":"light"}')
            self.assertTrue((checkout / "data" / "accounts.sqlite3").is_file())

    def test_checkout_data_migration_is_disabled_by_explicit_home(self):
        with tempfile.TemporaryDirectory() as folder:
            checkout = Path(folder) / "checkout"
            root = Path(folder) / "user-data"
            (checkout / "src").mkdir(parents=True)
            (checkout / "pyproject.toml").touch()
            (checkout / "src" / "bootstrap.py").touch()
            (checkout / "config.json").touch()
            with patch.dict(platform.os.environ, {"SIOPECHAT_HOME": str(root)}, clear=True):
                platform.migrate_checkout_data(checkout, root)
            self.assertFalse(root.exists())

    def test_windows_paths_and_detached_process_flags(self):
        with (
            patch.object(platform.os, "name", "nt"),
            patch.object(platform.sys, "platform", "win32"),
            patch.object(platform, "Path", PureWindowsPath),
            patch.dict(
                platform.os.environ, {"LOCALAPPDATA": "C:/Users/test/AppData/Local"}, clear=True
            ),
            patch.object(
                platform.tempfile, "gettempdir", return_value="C:/Users/test/AppData/Local/Temp"
            ),
            patch.object(platform.getpass, "getuser", return_value="test-user"),
        ):
            self.assertEqual(
                platform.user_data_dir(), PureWindowsPath("C:/Users/test/AppData/Local/siopechat")
            )
            self.assertEqual(
                platform.cache_dir(), PureWindowsPath("C:/Users/test/AppData/Local/siopechat/Cache")
            )
            self.assertEqual(
                platform.runtime_dir(),
                PureWindowsPath("C:/Users/test/AppData/Local/Temp/sc-test-user"),
            )
            self.assertEqual(process.detached_process_options(), {"creationflags": 0x208})

    def test_windows_permissions_use_inherited_acl(self):
        with (
            patch.object(platform.os, "name", "nt"),
            patch.object(platform, "Path", PureWindowsPath),
            patch.object(platform.os, "chmod") as chmod,
        ):
            process.set_private_permissions(PureWindowsPath("credentials.json"), 0o600)
        chmod.assert_not_called()

    def test_unix_xdg_paths_use_package_name(self):
        with (
            patch.object(platform.os, "name", "posix"),
            patch.object(platform.sys, "platform", "linux"),
            patch.object(platform, "Path", TestPosixPath),
            patch.dict(
                platform.os.environ,
                {
                    "XDG_DATA_HOME": "/tmp/data",
                    "XDG_STATE_HOME": "/tmp/state",
                    "XDG_CACHE_HOME": "/tmp/cache",
                },
                clear=True,
            ),
        ):
            self.assertEqual(platform.user_data_dir(), TestPosixPath("/tmp/data/siopechat"))
            self.assertEqual(platform.state_dir(), TestPosixPath("/tmp/state/siopechat"))
            self.assertEqual(platform.cache_dir(), TestPosixPath("/tmp/cache/siopechat"))

    def test_macos_uses_native_data_directories(self):
        with (
            patch.object(platform.os, "name", "posix"),
            patch.object(platform.sys, "platform", "darwin"),
            patch.dict(platform.os.environ, {}, clear=True),
            patch.object(platform, "Path", TestPosixPath),
        ):
            self.assertEqual(
                platform.user_data_dir(),
                TestPosixPath("/Users/test/Library/Application Support/siopechat"),
            )
            self.assertEqual(
                platform.cache_dir(), TestPosixPath("/Users/test/Library/Caches/siopechat")
            )


if __name__ == "__main__":
    unittest.main()
