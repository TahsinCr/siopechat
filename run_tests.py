#!/usr/bin/env python3
"""Run SiopeChat's application and provider tests from the repository root."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def python_command() -> list[str]:
    if sys.prefix != sys.base_prefix:
        return [sys.executable]

    candidates = (
        ROOT / "python" / "Scripts" / "python.exe",
        ROOT / "python" / "bin" / "python",
        ROOT / ".venv" / "Scripts" / "python.exe",
        ROOT / ".venv" / "bin" / "python",
    )
    for candidate in candidates:
        if candidate.is_file():
            return [str(candidate)]

    uv = shutil.which("uv")
    if uv:
        return [uv, "run", "--project", str(ROOT), "--locked", "python"]

    return [sys.executable]


def main() -> int:
    js_tests = sorted((ROOT / "tests" / "js").glob("*.test.cjs"))
    node = shutil.which("node")
    if js_tests and not node:
        print("Node.js is required to run tests/js/*.test.cjs.", file=sys.stderr)
        return 2

    env = os.environ.copy()
    python_paths = [str(ROOT / "src"), str(ROOT)]
    if env.get("PYTHONPATH"):
        python_paths.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(python_paths)

    python = python_command()
    commands = [
        [*python, "-m", "unittest", "discover", "-s", "tests", "-v"],
    ]
    for command in commands:
        result = subprocess.run(command, cwd=ROOT, env=env, check=False)
        if result.returncode:
            return result.returncode

    with tempfile.TemporaryDirectory(prefix="siopechat-provider-tests-") as folder:
        isolated_root = Path(folder)
        provider_root = isolated_root / "providers"
        shutil.copytree(
            ROOT / "src/providers",
            provider_root,
            ignore=shutil.ignore_patterns("__pycache__", "*.egg-info", "build", "dist"),
        )
        provider_env = os.environ.copy()
        provider_env["PYTHONPATH"] = str(isolated_root)
        provider_commands = [
            [
                *python,
                "-m",
                "unittest",
                "discover",
                "-s",
                str(provider_root / "tests"),
                "-t",
                str(isolated_root),
                "-v",
            ],
            [*python, str(provider_root / "tests/package_smoke.py")],
        ]
        for command in provider_commands:
            result = subprocess.run(command, cwd=isolated_root, env=provider_env, check=False)
            if result.returncode:
                return result.returncode

    if js_tests:
        result = subprocess.run(
            [node, "--test", *(str(path) for path in js_tests)], cwd=ROOT, check=False
        )
        if result.returncode:
            return result.returncode

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
