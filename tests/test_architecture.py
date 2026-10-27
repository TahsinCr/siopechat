import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ArchitectureTests(unittest.TestCase):
    def test_providers_have_no_application_imports(self):
        files = list((ROOT / "src/providers").rglob("*.py"))
        forbidden = {
            "accounts",
            "backend",
            "bootstrap",
            "cli",
            "tui",
            "downloads",
            "extensions",
            "settings",
            "tasks",
            "utils",
            "runtime",
            "views",
            "extension",
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
                        self.assertNotIn(module.split(".")[0], forbidden)

    def test_job_runtime_has_no_terminal_or_storage_dependencies(self):
        path = ROOT / "src/tasks.py"
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom):
                self.assertNotIn(
                    (node.module or "").split(".")[0],
                    {"cli", "tui", "services", "sqlite3", "httpx", "extensions"},
                )
