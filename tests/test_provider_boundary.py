"""The provider directory must work without the host application's source tree."""

import ast
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class ProviderBoundaryTests(unittest.TestCase):
    def test_no_provider_imports_application_modules(self):
        root = Path(__file__).resolve().parents[1] / "src/providers"
        forbidden = {
            "accounts",
            "backend",
            "bootstrap",
            "cli",
            "downloads",
            "extensions",
            "settings",
            "tasks",
            "utils",
            "runtime",
            "views",
            "extension",
        }
        for path in root.rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                imports = []
                if isinstance(node, ast.Import):
                    imports = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    imports = [node.module]
                self.assertFalse({name.split(".")[0] for name in imports} & forbidden, str(path))

    def test_portable_api_suite_runs_with_only_the_copied_provider_directory(self):
        root = Path(__file__).resolve().parents[1] / "src/providers"
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "providers"
            shutil.copytree(
                root,
                target,
                ignore=shutil.ignore_patterns("__pycache__", "*.egg-info", "build", "dist"),
            )
            code = (
                "import sys, unittest; sys.path.insert(0, sys.argv[1]); "
                "suite=unittest.defaultTestLoader.discover(sys.argv[2]); "
                "result=unittest.TextTestRunner().run(suite); "
                "sys.exit(not result.wasSuccessful())"
            )
            env = dict(os.environ)
            env.pop("PYTHONPATH", None)
            result = subprocess.run(
                [sys.executable, "-I", "-c", code, folder, str(target / "tests")],
                cwd=folder,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
