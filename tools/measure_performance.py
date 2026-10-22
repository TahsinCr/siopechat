#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 TahsinCr
"""Launch SiopeChat and record process-tree CPU, memory, and thread usage."""

from __future__ import annotations

import argparse
import csv
import sys
import time
from datetime import datetime
from pathlib import Path
from subprocess import Popen, TimeoutExpired

ROOT = Path(__file__).resolve().parents[1]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--interval",
        type=float,
        default=1.0,
        help="sampling interval in seconds (default: 1)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="CSV output path (default: performance/siopechat_TIMESTAMP.csv)",
    )
    parser.add_argument(
        "application_args",
        nargs=argparse.REMAINDER,
        help="arguments passed to siopechat.py after --",
    )
    return parser


def _process_tree(root_process, psutil):
    try:
        return [root_process, *root_process.children(recursive=True)]
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return [root_process]


def _sample(root_process, psutil):
    cpu_percent = 0.0
    rss_bytes = 0
    thread_count = 0
    process_count = 0

    for process in _process_tree(root_process, psutil):
        try:
            cpu_percent += process.cpu_percent(interval=None)
            rss_bytes += process.memory_info().rss
            thread_count += process.num_threads()
            process_count += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

    return cpu_percent, rss_bytes, thread_count, process_count


def main() -> int:
    args = _parser().parse_args()
    if args.interval <= 0:
        print("--interval must be greater than zero", file=sys.stderr)
        return 2

    try:
        import psutil
    except ImportError:
        print(
            "Missing optional dependency 'psutil'. Install it with "
            "'uv sync --group performance' or 'python -m pip install psutil'.",
            file=sys.stderr,
        )
        return 2

    application_args = args.application_args
    if application_args[:1] == ["--"]:
        application_args = application_args[1:]

    output = args.output or (
        ROOT / "performance" / f"siopechat_{datetime.now():%Y%m%d_%H%M%S_%f}.csv"
    )
    if not output.is_absolute():
        output = Path.cwd() / output
    output.parent.mkdir(parents=True, exist_ok=True)

    command = [sys.executable, str(ROOT / "siopechat.py"), *application_args]
    print(f"Starting SiopeChat; performance samples will be saved to: {output}")

    started_at = time.monotonic()
    peak_rss = 0
    peak_cpu = 0.0
    samples = 0
    try:
        with output.open("w", newline="", encoding="utf-8") as csv_file:
            writer = csv.writer(csv_file)
            writer.writerow(["elapsed_seconds", "cpu_percent", "rss_mib", "threads", "processes"])
            with Popen(command, cwd=ROOT) as application:
                root_process = psutil.Process(application.pid)
                for process in _process_tree(root_process, psutil):
                    try:
                        process.cpu_percent(interval=None)
                    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                        continue

                while application.poll() is None:
                    time.sleep(args.interval)
                    cpu, rss, threads, process_count = _sample(root_process, psutil)
                    elapsed = time.monotonic() - started_at
                    writer.writerow(
                        [
                            f"{elapsed:.2f}",
                            f"{cpu:.1f}",
                            f"{rss / (1024 * 1024):.2f}",
                            threads,
                            process_count,
                        ]
                    )
                    csv_file.flush()
                    samples += 1
                    peak_cpu = max(peak_cpu, cpu)
                    peak_rss = max(peak_rss, rss)

                return_code = application.wait()
    except KeyboardInterrupt:
        print("\nStopping SiopeChat…", file=sys.stderr)
        return_code = 130
    finally:
        if "application" in locals() and application.poll() is None:
            application.terminate()
            try:
                application.wait(timeout=5)
            except TimeoutExpired:
                application.kill()
                application.wait()

    print(
        f"Finished after {time.monotonic() - started_at:.1f}s; {samples} samples. "
        f"Peak process-tree RSS: {peak_rss / (1024 * 1024):.1f} MiB; "
        f"peak CPU: {peak_cpu:.1f}%. CSV: {output}"
    )
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
