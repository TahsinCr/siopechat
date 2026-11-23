"""Architecture boundary checks enforcing strict backend/TUI and extension-free separation."""

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _imported_modules(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)
    return modules


class ArchitectureTests(unittest.TestCase):
    def test_backend_has_no_tui_or_legacy_dependencies(self):
        forbidden = {
            "tui",
            "prompt_toolkit",
            "rich",
            "extension",
            "extensions",
            "backend",
            "tasks",
            "language",
            "webai_provider",
        }
        for path in sorted((ROOT / "src").rglob("*.py")):
            rel = path.relative_to(ROOT / "src")
            if rel.parts[0] == "tui" or rel == Path("cli.py"):
                continue
            for mod in _imported_modules(path):
                self.assertNotIn(
                    mod.split(".")[0],
                    forbidden,
                    f"{rel} imported forbidden module {mod!r}",
                )

    def test_cli_keeps_tui_imports_lazy_for_headless_startup(self):
        tree = ast.parse((ROOT / "src/cli.py").read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotIn(alias.name.split(".")[0], {"tui", "prompt_toolkit", "rich"})
            elif isinstance(node, ast.ImportFrom) and node.module:
                self.assertNotIn(node.module.split(".")[0], {"tui", "prompt_toolkit", "rich"})

    def test_no_legacy_extensions_or_v1_provider_imports_anywhere(self):
        legacy = {"webai_provider", "extension", "extensions", "backend", "tasks", "language"}
        for folder in ("src", "tests"):
            for path in sorted((ROOT / folder).rglob("*.py")):
                for mod in _imported_modules(path):
                    self.assertNotIn(
                        mod.split(".")[0],
                        legacy,
                        f"{path.relative_to(ROOT)} imported removed module {mod!r}",
                    )
