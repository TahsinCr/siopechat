import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from utils import paths, process


class PlatformSupportTests(unittest.TestCase):
    def test_storage_paths_use_platform_defaults_and_explicit_portable_mode(self):
        project = Path("/tmp/siopechat-project")
        standard = SimpleNamespace(
            user_config_path=Path("/user/config/siopechat"),
            user_data_path=Path("/user/data/siopechat"),
            user_state_path=Path("/user/state/siopechat"),
            user_downloads_path=Path("/user/Downloads"),
            user_cache_path=Path("/user/cache/siopechat"),
            user_runtime_path=Path("/run/user/1000/siopechat"),
        )
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(paths, "project_root", return_value=project),
            patch.object(paths, "_APP_DIRS", standard),
        ):
            self.assertEqual(paths.config_path(), standard.user_config_path / "config.json")
            self.assertEqual(paths.data_dir(), standard.user_data_path)
            self.assertEqual(paths.state_dir(), standard.user_state_path)
            self.assertEqual(paths.downloads_dir(), standard.user_downloads_path)
            self.assertEqual(paths.cache_dir(), standard.user_cache_path)
            self.assertEqual(paths.runtime_dir(), standard.user_runtime_path)

        with (
            patch.dict(os.environ, {"SIOPECHAT_PORTABLE": "1"}, clear=True),
            patch.object(paths, "project_root", return_value=project),
        ):
            self.assertEqual(paths.config_path(), project / "config.json")
            self.assertEqual(paths.data_dir(), project / "data")
            self.assertEqual(paths.state_dir(), project / "data")
            self.assertEqual(paths.downloads_dir(), project / "downloads")
            self.assertEqual(paths.cache_dir(), project / ".cache")
            self.assertEqual(paths.runtime_dir(), project / ".runtime")

    def test_each_storage_path_has_an_independent_environment_override(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "project"
            resolved_root = root.resolve()
            external_data = Path(folder) / "external-data"
            external_cache = Path(folder) / "external-cache"
            overrides = {
                "SIOPECHAT_CONFIG_PATH": "settings/app.json",
                "SIOPECHAT_DATA_DIR": str(external_data),
                "SIOPECHAT_STATE_DIR": "state",
                "SIOPECHAT_DOWNLOADS_DIR": "files",
                "SIOPECHAT_CACHE_DIR": str(external_cache),
                "SIOPECHAT_RUNTIME_DIR": "run",
            }
            with patch.dict(os.environ, overrides, clear=True):
                self.assertEqual(paths.config_path(root), resolved_root / "settings/app.json")
                self.assertEqual(paths.data_dir(root), external_data.resolve())
                self.assertEqual(paths.state_dir(root), resolved_root / "state")
                self.assertEqual(paths.downloads_dir(root), resolved_root / "files")
                self.assertEqual(paths.cache_dir(root), external_cache.resolve())
                self.assertEqual(paths.runtime_dir(root), resolved_root / "run")

    def test_runtime_directory_is_private_and_honors_its_override(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch.dict(os.environ, {"SIOPECHAT_RUNTIME_DIR": str(root / "private-run")}):
                runtime = paths.ensure_runtime_dir(paths.runtime_dir(root))
            self.assertEqual(runtime, (root / "private-run").resolve())
            if os.name != "nt":
                self.assertEqual(runtime.stat().st_mode & 0o777, 0o700)

    def test_windows_detached_process_flags(self):
        with patch.object(process.os, "name", "nt"):
            self.assertEqual(process.detached_process_options(), {"creationflags": 0x208})

    def test_windows_permissions_use_inherited_acl(self):
        path = Path("credentials.json")
        with (
            patch.object(process.os, "name", "nt"),
            patch.object(process.os, "chmod") as chmod,
        ):
            process.set_private_permissions(path, 0o600)
        chmod.assert_not_called()


if __name__ == "__main__":
    unittest.main()
