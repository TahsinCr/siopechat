import ast
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ArchitectureTests(unittest.TestCase):
    def test_requested_module_and_resource_boundaries(self):
        for name in (
            "settings",
            "actions",
            "tasks",
            "runtime",
            "extension",
            "backend",
            "language",
            "theme",
        ):
            self.assertTrue((ROOT / "src" / f"{name}.py").is_file(), name)
            self.assertFalse((ROOT / "src" / name).is_dir(), name)
        self.assertTrue((ROOT / "extensions/layout/plugin.py").is_file())
        self.assertTrue((ROOT / "themes/soft.json").is_file())
        self.assertTrue((ROOT / "src/lang/tr.json").is_file())
        for name in ("services", "conversations", "jobs", "core", "app"):
            self.assertFalse((ROOT / "src" / name).exists(), name)

    def test_providers_have_no_application_imports(self):
        files = list((ROOT / "src/providers").rglob("*.py"))
        self.assertGreater(len(files), 5)
        allowed = set(sys.stdlib_module_names) | {
            "httpx",
            "providers",
            "__future__",
            "gemini_webapi",
            "curl_cffi",
        }
        for path in files:
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    modules = [node.module or ""]
                else:
                    continue
                for module in modules:
                    with self.subTest(file=str(path), module=module):
                        self.assertIn(module.split(".")[0], allowed)

    def test_providers_can_be_copied_and_imported_without_application(self):
        with tempfile.TemporaryDirectory() as directory:
            shutil.copytree(ROOT / "src/providers", Path(directory) / "providers")
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "from providers.chatgpt import ChatGPTProvider, BrowserSession, SessionPaths; "
                    "from providers import Backend, ProviderRegistry",
                ],
                cwd=directory,
                env=os.environ | {"PYTHONPATH": directory},
                capture_output=True,
                text=True,
                timeout=10,
            )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_job_runtime_has_no_terminal_or_storage_dependencies(self):
        files = [ROOT / "src/tasks.py"]
        self.assertGreater(len(files), 0)
        for path in files:
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.ImportFrom):
                    self.assertNotIn(
                        (node.module or "").split(".")[0],
                        {"cli", "services", "sqlite3", "httpx", "extensions"},
                    )

    def test_provider_helpers_exist_at_packaged_location(self):
        directory = ROOT / "src/providers/chatgpt/scripts"
        for name in (
            "browser/auth.cjs",
            "browser/capture.cjs",
            "browser/send.cjs",
            "sentinel/sign.cjs",
        ):
            self.assertTrue((directory / name).is_file(), name)
