#!/usr/bin/env python3
"""Run SiopeChat's Python and JavaScript test suites from the repository root."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def python_command() -> list[str]:
    uv = shutil.which("uv")
    if uv:
        return [uv, "run", "--locked", "python"]

    candidates = (
        ROOT / ".venv" / "Scripts" / "python.exe",
        ROOT / ".venv" / "bin" / "python",
        ROOT / "python" / "Scripts" / "python.exe",
        ROOT / "python" / "bin" / "python",
    )
    for candidate in candidates:
        if candidate.is_file():
            return [str(candidate)]

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

    suites = [
        (
            "Python tests",
            [*python_command(), "-m", "unittest", "discover", "-s", "tests"],
        )
    ]
    if js_tests:
        suites.append(("JavaScript tests", [node, "--test", *(str(path) for path in js_tests)]))

    for name, command in suites:
        print(f"\n== {name} ==", flush=True)
        result = subprocess.run(command, cwd=ROOT, env=env, check=False)
        if result.returncode:
            return result.returncode

    print("\nAll test suites passed.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
