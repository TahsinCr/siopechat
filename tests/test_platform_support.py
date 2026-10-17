import unittest
from pathlib import Path, PureWindowsPath
from unittest.mock import patch

from utils import paths as platform
from utils import process


class PlatformSupportTests(unittest.TestCase):
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
                platform.legacy_data_dir(), PureWindowsPath("C:/Users/test/AppData/Local/SiopeChat")
            )
            self.assertEqual(
                platform.cache_dir(), PureWindowsPath("C:/Users/test/AppData/Local/SiopeChat/Cache")
            )
            self.assertEqual(
                platform.runtime_dir(),
                PureWindowsPath("C:/Users/test/AppData/Local/Temp/sc-test-user"),
            )
            self.assertEqual(process.detached_process_options(), {"creationflags": 0x208})

    def test_windows_permissions_use_inherited_acl(self):
        with patch.object(platform.os, "name", "nt"), patch.object(platform.os, "chmod") as chmod:
            process.set_private_permissions(Path("credentials.json"), 0o600)
        chmod.assert_not_called()

    def test_unix_xdg_paths_preserve_legacy_directory_name(self):
        with (
            patch.object(platform.os, "name", "posix"),
            patch.object(platform.sys, "platform", "linux"),
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
            self.assertEqual(platform.legacy_data_dir(), Path("/tmp/data/chatgpt-web"))
            self.assertEqual(platform.state_dir(), Path("/tmp/state/chatgpt-web"))
            self.assertEqual(platform.cache_dir(), Path("/tmp/cache/chatgpt-web"))

    def test_macos_uses_native_data_directories(self):
        with (
            patch.object(platform.os, "name", "posix"),
            patch.object(platform.sys, "platform", "darwin"),
            patch.dict(platform.os.environ, {}, clear=True),
            patch.object(Path, "home", return_value=Path("/Users/test")),
        ):
            self.assertEqual(
                platform.legacy_data_dir(),
                Path("/Users/test/Library/Application Support/SiopeChat"),
            )
            self.assertEqual(platform.cache_dir(), Path("/Users/test/Library/Caches/SiopeChat"))


if __name__ == "__main__":
    unittest.main()
