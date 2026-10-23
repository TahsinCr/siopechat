#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 TahsinCr
"""Launch SiopeChat and record process-tree CPU, memory, and thread usage."""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
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
        type=_positive_interval,
        default=1.0,
        help="sampling interval in seconds (default: 1)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="CSV output path (default: performance/siopechat_TIMESTAMP.csv)",
    )
    parser.add_argument(
        "--storage-root",
        action="append",
        type=Path,
        help="directory to size before and after the run; repeat for multiple roots (default: data)",
    )
    parser.add_argument(
        "--no-storage",
        action="store_true",
        help="skip directory metadata scans",
    )
    parser.add_argument(
        "application_args",
        nargs=argparse.REMAINDER,
        help="arguments passed to siopechat.py after --",
    )
    return parser


def _process_tree(root_process, psutil):
    try:
        processes = [root_process, *root_process.children(recursive=True)]
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        processes = [root_process]
    unique = {}
    for process in processes:
        unique[process.pid] = process
    return list(unique.values())


def _sample(root_process, psutil):
    sample = {
        "cpu_percent": 0.0,
        "rss_bytes": 0,
        "uss_bytes": 0,
        "pss_bytes": 0,
        "io_read_bytes": 0,
        "io_write_bytes": 0,
        "io_by_pid": {},
        "threads": 0,
        "processes": 0,
        "handles": 0,
        "uss_available": True,
        "pss_available": True,
        "io_available": True,
        "handles_available": True,
    }

    for process in _process_tree(root_process, psutil):
        try:
            sample["cpu_percent"] += process.cpu_percent(interval=None)
            sample["threads"] += process.num_threads()
            sample["processes"] += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

        try:
            memory = process.memory_full_info()
            sample["rss_bytes"] += memory.rss
            sample["uss_bytes"] += getattr(memory, "uss", 0)
            sample["pss_bytes"] += getattr(memory, "pss", 0)
            sample["uss_available"] &= hasattr(memory, "uss")
            sample["pss_available"] &= hasattr(memory, "pss")
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
            sample["uss_available"] = False
            sample["pss_available"] = False
            try:
                sample["rss_bytes"] += process.memory_info().rss
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue

        try:
            io = process.io_counters()
            sample["io_read_bytes"] += io.read_bytes
            sample["io_write_bytes"] += io.write_bytes
            sample["io_by_pid"][process.pid] = (io.read_bytes, io.write_bytes)
        except (AttributeError, NotImplementedError, psutil.NoSuchProcess, psutil.AccessDenied):
            sample["io_available"] = False

        try:
            counter = getattr(process, "num_fds", None) or getattr(process, "num_handles")
            sample["handles"] += counter()
        except (AttributeError, NotImplementedError, psutil.NoSuchProcess, psutil.AccessDenied):
            sample["handles_available"] = False

    return sample


def _storage_summary(root: Path) -> dict:
    """Count regular files by first-level component without reading file contents."""
    result = {"exists": root.is_dir(), "bytes": 0, "files": 0, "scan_errors": 0, "components": {}}
    if not result["exists"]:
        return result
    usage = shutil.disk_usage(root)
    result["filesystem"] = {
        "total_bytes": usage.total,
        "used_bytes": usage.used,
        "free_bytes": usage.free,
    }

    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            pending.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            size = entry.stat(follow_symlinks=False).st_size
                            relative = Path(entry.path).relative_to(root)
                            component = _storage_component(relative, root)
                            bucket = result["components"].setdefault(
                                component, {"bytes": 0, "files": 0}
                            )
                            bucket["bytes"] += size
                            bucket["files"] += 1
                            result["bytes"] += size
                            result["files"] += 1
                    except OSError:
                        result["scan_errors"] += 1
                        continue
        except OSError:
            result["scan_errors"] += 1
            continue
    return result


def _storage_component(relative, root):
    parts = tuple(part.casefold() for part in relative.parts)
    name = parts[-1] if parts else root.name.casefold()
    if root.name.casefold() == "downloads" or "downloads" in parts:
        return "downloads"
    if "chrome-profile" in parts:
        return "browser-profile"
    if any(part in {"cache", "caches", "cache_data", "code cache", "gpu cache"} for part in parts):
        return "provider-cache"
    if "attachments" in parts:
        return "attachments"
    if name in {"session.json", "cookies.json", "native.json", "verification.json"}:
        return "account-session-data"
    if name == "models.json":
        return "model-catalog"
    known = {
        "accounts": "account-data",
        "gemini-cookie-cache": "provider-session-cache",
        "files": "archive-files",
    }
    if name.startswith("archive.sqlite3"):
        return "archive-database"
    if name.startswith("workspaces.sqlite3"):
        return "workspace-database"
    return known.get(parts[0] if parts else name, "other")


def _storage_snapshots(roots):
    return {str(root): _storage_summary(root) for root in roots}


def _storage_delta(before, after):
    delta = {}
    for name in before.keys() | after.keys():
        old = before.get(name, {"exists": False, "bytes": 0, "files": 0, "components": {}})
        new = after.get(name, {"exists": False, "bytes": 0, "files": 0, "components": {}})
        components = {}
        for component in old["components"].keys() | new["components"].keys():
            old_component = old["components"].get(component, {"bytes": 0, "files": 0})
            new_component = new["components"].get(component, {"bytes": 0, "files": 0})
            components[component] = {
                "bytes": new_component["bytes"] - old_component["bytes"],
                "files": new_component["files"] - old_component["files"],
            }
        filesystem = None
        if old.get("exists") and new.get("exists"):
            filesystem = {
                key: new.get("filesystem", {}).get(key, 0) - old.get("filesystem", {}).get(key, 0)
                for key in ("total_bytes", "used_bytes", "free_bytes")
            }
        delta[name] = {
            "bytes": new["bytes"] - old["bytes"],
            "files": new["files"] - old["files"],
            "filesystem": filesystem,
            "components": components,
        }
    return delta


def _mib(value):
    return "" if value is None else f"{value / (1024 * 1024):.2f}"


def _available_bytes(sample, field):
    available_field = {
        "uss_bytes": "uss_available",
        "pss_bytes": "pss_available",
        "io_read_bytes": "io_available",
        "io_write_bytes": "io_available",
        "handles": "handles_available",
    }.get(field)
    return sample[field] if available_field is None or sample[available_field] else None


def _positive_interval(value):
    interval = float(value)
    if interval <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return interval


def _storage_roots(args):
    if args.no_storage:
        return []
    if args.storage_root:
        candidates = args.storage_root
    else:
        candidates = [ROOT / "data"]
        download_directory = "downloads"
        try:
            config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
            value = config.get("application", {}).get("download_directory")
            if isinstance(value, str) and value.strip():
                download_directory = value
        except (OSError, json.JSONDecodeError):
            pass
        download_root = Path(download_directory).expanduser()
        candidates.append(download_root if download_root.is_absolute() else ROOT / download_root)
    unique = {}
    for path in candidates:
        resolved = path.expanduser().resolve()
        unique.setdefault(resolved, resolved)
    return list(unique.values())


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
    storage_roots = _storage_roots(args)
    storage_before = _storage_snapshots(storage_roots)
    summary_output = output.with_suffix(".summary.json")

    command = [sys.executable, str(ROOT / "siopechat.py"), *application_args]
    print(f"Starting SiopeChat; performance samples will be saved to: {output}")

    started_at = time.monotonic()
    peak_rss = 0
    peak_uss = 0
    peak_pss = 0
    peak_cpu = 0.0
    peak_threads = 0
    last_io_by_pid = {}
    total_read_bytes = 0
    total_write_bytes = 0
    cpu_sum = 0.0
    samples = 0
    return_code = 1
    try:
        with output.open("w", newline="", encoding="utf-8") as csv_file:
            writer = csv.writer(csv_file)
            writer.writerow(
                [
                    "elapsed_seconds",
                    "cpu_percent",
                    "rss_mib",
                    "uss_mib",
                    "pss_mib",
                    "read_mib_total",
                    "write_mib_total",
                    "open_handles",
                    "threads",
                    "processes",
                ]
            )
            with Popen(command, cwd=ROOT) as application:
                root_process = psutil.Process(application.pid)
                for process in _process_tree(root_process, psutil):
                    try:
                        process.cpu_percent(interval=None)
                    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                        continue

                while application.poll() is None:
                    time.sleep(args.interval)
                    current = _sample(root_process, psutil)
                    cpu = current["cpu_percent"]
                    rss = current["rss_bytes"]
                    uss = _available_bytes(current, "uss_bytes")
                    pss = _available_bytes(current, "pss_bytes")
                    if current["io_available"] and total_read_bytes is not None:
                        interval_read = interval_write = 0
                        for pid, (read_bytes, write_bytes) in current["io_by_pid"].items():
                            previous = last_io_by_pid.get(pid, (0, 0))
                            interval_read += max(0, read_bytes - previous[0])
                            interval_write += max(0, write_bytes - previous[1])
                        total_read_bytes += interval_read
                        total_write_bytes += interval_write
                        last_io_by_pid = current["io_by_pid"]
                    elif not current["io_available"]:
                        total_read_bytes = total_write_bytes = None
                    elapsed = time.monotonic() - started_at
                    writer.writerow(
                        [
                            f"{elapsed:.2f}",
                            f"{cpu:.1f}",
                            _mib(rss),
                            _mib(uss),
                            _mib(pss),
                            _mib(total_read_bytes),
                            _mib(total_write_bytes),
                            _available_bytes(current, "handles")
                            if _available_bytes(current, "handles") is not None
                            else "",
                            current["threads"],
                            current["processes"],
                        ]
                    )
                    csv_file.flush()
                    samples += 1
                    cpu_sum += cpu
                    peak_cpu = max(peak_cpu, cpu)
                    peak_rss = max(peak_rss, rss)
                    peak_uss = max(peak_uss, uss or 0)
                    peak_pss = max(peak_pss, pss or 0)
                    peak_threads = max(peak_threads, current["threads"])

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

    storage_after = _storage_snapshots(storage_roots)
    summary = {
        "duration_seconds": round(time.monotonic() - started_at, 2),
        "samples": samples,
        "cpu_percent": {
            "average": round(cpu_sum / samples, 2) if samples else None,
            "peak": round(peak_cpu, 2),
        },
        "process_tree_peak_mib": {
            "rss": round(peak_rss / (1024 * 1024), 2),
            "uss": round(peak_uss / (1024 * 1024), 2) if peak_uss else None,
            "pss": round(peak_pss / (1024 * 1024), 2) if peak_pss else None,
        },
        "process_tree_peak_threads": peak_threads,
        "disk_io_delta_mib": {
            "read": round(total_read_bytes / (1024 * 1024), 2)
            if total_read_bytes is not None
            else None,
            "written": round(total_write_bytes / (1024 * 1024), 2)
            if total_write_bytes is not None
            else None,
        },
        "storage_before": storage_before,
        "storage_after": storage_after,
        "storage_delta": _storage_delta(storage_before, storage_after),
        "csv": str(output),
    }
    summary_output.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        f"Finished after {time.monotonic() - started_at:.1f}s; {samples} samples. "
        f"Peak process-tree RSS: {peak_rss / (1024 * 1024):.1f} MiB; "
        f"peak CPU: {peak_cpu:.1f}%; summary: {summary_output}"
    )
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
