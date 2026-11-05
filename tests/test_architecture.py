import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ArchitectureTests(unittest.TestCase):
    def test_job_runtime_has_no_terminal_or_storage_dependencies(self):
        path = ROOT / "src/tasks.py"
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom):
                self.assertNotIn(
                    (node.module or "").split(".")[0],
                    {"cli", "tui", "services", "sqlite3", "httpx", "extensions"},
                )
