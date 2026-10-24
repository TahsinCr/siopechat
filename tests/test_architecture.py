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
                    {"cli", "services", "sqlite3", "httpx", "extensions"},
                )

    def test_packaged_node_helpers_are_reachable_from_current_entrypoints(self):
        import re

        from providers.chatgpt.resources import Script, script_path

        directory = (ROOT / "src/providers").resolve()
        pending = [script_path(script).resolve() for script in Script]
        pending += [directory / "scripts" / name for name in ("page.cjs", "cookies.cjs")]
        reachable = set()
        while pending:
            path = pending.pop()
            if path in reachable:
                continue
            self.assertTrue(path.is_file(), path)
            reachable.add(path)
            for reference in re.findall(r"require\(['\"](\.[^'\"]+)['\"]\)", path.read_text()):
                target = (path.parent / reference).resolve()
                pending.append(target if target.suffix else target.with_suffix(".cjs"))
        self.assertEqual(reachable, set(directory.rglob("*.cjs")))
