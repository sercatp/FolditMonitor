#!/usr/bin/env python3
"""Collect read-only diagnostics for Foldit Monitor on macOS.

Run this while at least two Foldit instances are actively running different
recipes. The script does not request permissions or modify any files. It maps
Foldit process IDs to AppKit applications, Quartz windows, open log/save files,
and recently changed files inside each discovered app bundle.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import math
import platform
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterable

import psutil


LOG_PATH_PATTERN = re.compile(
    r"scriptlog|log\.txt|puzzles|\.ir_solution",
    re.IGNORECASE,
)
SCRIPT_NAME_PATTERN = re.compile(
    r"<(?:[^:<>]+:)?ScriptName>(.*?)</(?:[^:<>]+:)?ScriptName>",
    re.IGNORECASE | re.DOTALL,
)


def run_command(args: list[str]) -> tuple[int, str, str]:
    try:
        result = subprocess.run(
            args,
            text=True,
            capture_output=True,
            check=False,
        )
        return result.returncode, result.stdout, result.stderr
    except Exception as exc:
        return -1, "", f"{type(exc).__name__}: {exc}"


def is_foldit_process(info: dict[str, Any]) -> bool:
    name = str(info.get("name") or "")
    executable = str(info.get("exe") or "")
    normalized_name = re.split(r"[/\\]", name.lower())[-1]
    normalized_executable = executable.replace("\\", "/").lower()
    executable_name = normalized_executable.rsplit("/", 1)[-1]
    return normalized_name in {"foldit", "foldit.exe"} or (
        ".app/contents/macos/" in normalized_executable
        and "foldit" in executable_name
    )


def collect_foldit_processes() -> list[dict[str, Any]]:
    processes: list[dict[str, Any]] = []
    attributes = ["pid", "name", "exe", "cmdline", "create_time", "username"]
    for process in psutil.process_iter(attributes, ad_value=None):
        try:
            info = dict(process.info)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
        if is_foldit_process(info):
            processes.append(info)
    return sorted(processes, key=lambda item: int(item["pid"]))


def lsof_cwd(pid: int) -> str:
    returncode, stdout, stderr = run_command(
        ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"]
    )
    paths = [line[1:] for line in stdout.splitlines() if line.startswith("n")]
    if paths:
        return paths[0]
    if stderr.strip():
        return f"<unavailable: {stderr.strip()}>"
    return f"<unavailable: lsof exited {returncode}>"


def lsof_matching_files(pid: int) -> tuple[set[tuple[str, str, str]], str]:
    returncode, stdout, stderr = run_command(
        ["lsof", "-a", "-p", str(pid), "-Ffan"]
    )
    records: set[tuple[str, str, str]] = set()
    descriptor = ""
    access = ""

    for line in stdout.splitlines():
        if line.startswith("f"):
            descriptor = line[1:]
            access = ""
        elif line.startswith("a"):
            access = line[1:]
        elif line.startswith("n"):
            path = line[1:]
            if LOG_PATH_PATTERN.search(path):
                records.add((descriptor or "?", access or "?", path))

    error = ""
    if not records and stderr.strip():
        error = stderr.strip()
    elif returncode not in (0, 1) and not records:
        error = f"lsof exited {returncode}"
    return records, error


def bundle_from_executable(executable: Any) -> Path | None:
    text = str(executable or "")
    marker = "/Contents/MacOS/"
    index = text.find(marker)
    if index < 0:
        return None
    bundle = Path(text[:index])
    return bundle if bundle.suffix.lower() == ".app" else None


def objc_value(obj: Any, method_name: str, default: Any = None) -> Any:
    try:
        method = getattr(obj, method_name)
        return method()
    except Exception:
        return default


def print_appkit_information(
    processes: Iterable[dict[str, Any]],
) -> set[Path]:
    print("\n=== APPKIT PROCESS INFORMATION ===")
    bundle_paths: set[Path] = set()
    try:
        import AppKit
    except Exception as exc:
        print("AppKit unavailable:", repr(exc))
        return bundle_paths

    for info in processes:
        pid = int(info["pid"])
        try:
            app = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        except Exception as exc:
            print(f"PID {pid}: NSRunningApplication query failed: {exc!r}")
            continue

        if app is None:
            print(f"PID {pid}: NSRunningApplication not found")
            continue

        bundle_url = objc_value(app, "bundleURL")
        bundle_path = str(objc_value(bundle_url, "path", "") or "")
        if bundle_path:
            bundle_paths.add(Path(bundle_path))

        print(
            f"PID {pid}:",
            f"name={objc_value(app, 'localizedName')!r}",
            f"bundle={bundle_path!r}",
            f"active={bool(objc_value(app, 'isActive', False))}",
            f"hidden={bool(objc_value(app, 'isHidden', False))}",
        )
    return bundle_paths


def load_quartz_and_print_permissions() -> Any:
    print("\n=== MACOS PERMISSIONS ===")
    quartz = None
    try:
        import Quartz

        quartz = Quartz
        preflight = getattr(Quartz, "CGPreflightScreenCaptureAccess", None)
        if preflight is None:
            print("Screen Recording: preflight API unavailable")
        else:
            print("Screen Recording:", bool(preflight()))
    except Exception as exc:
        print("Quartz unavailable:", repr(exc))

    try:
        from ApplicationServices import AXIsProcessTrusted

        print("Accessibility:", bool(AXIsProcessTrusted()))
    except Exception as exc:
        print("Accessibility status unavailable:", repr(exc))
    return quartz


def print_quartz_windows(quartz: Any, process_ids: set[int]) -> None:
    print("\n=== QUARTZ WINDOWS FOR FOLDIT PIDS ===")
    if quartz is None:
        print("Quartz is unavailable.")
        return

    try:
        options = (
            quartz.kCGWindowListOptionAll
            | quartz.kCGWindowListExcludeDesktopElements
        )
        windows = quartz.CGWindowListCopyWindowInfo(
            options,
            quartz.kCGNullWindowID,
        ) or []
    except Exception as exc:
        print("Quartz window query failed:", repr(exc))
        return

    found = 0
    for window in windows:
        owner_pid = window.get(quartz.kCGWindowOwnerPID)
        if owner_pid not in process_ids:
            continue
        found += 1
        print(
            f"PID={owner_pid}",
            f"window_id={window.get(quartz.kCGWindowNumber)!r}",
            f"owner={window.get(quartz.kCGWindowOwnerName)!r}",
            f"title={window.get(quartz.kCGWindowName)!r}",
            f"layer={window.get(quartz.kCGWindowLayer)!r}",
            f"onscreen={window.get(quartz.kCGWindowIsOnscreen)!r}",
            f"bounds={window.get(quartz.kCGWindowBounds)!r}",
        )

    if not found:
        print("No Quartz windows matched the Foldit PIDs.")


def read_script_name(path: Path) -> str:
    if path.suffix.lower() != ".xml":
        return ""
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as handle:
            text = handle.read(131_072)
    except OSError:
        return ""

    match = SCRIPT_NAME_PATTERN.search(text)
    if not match:
        return ""
    return " ".join(html.unescape(match.group(1)).split())


def file_snapshot(
    resource_roots: Iterable[Path],
) -> dict[str, tuple[int, int, str]]:
    result: dict[str, tuple[int, int, str]] = {}
    for root in resource_roots:
        try:
            candidates = list(root.glob("scriptlog*.xml"))
        except OSError:
            candidates = []
        candidates.append(root / "log.txt")

        for path in candidates:
            try:
                stat = path.stat()
            except OSError:
                continue
            result[str(path)] = (
                stat.st_size,
                stat.st_mtime_ns,
                read_script_name(path),
            )
    return result


def print_file_changes(
    before: dict[str, tuple[int, int, str]],
    after: dict[str, tuple[int, int, str]],
) -> None:
    print("\n=== SCRIPT LOG FILES AFTER POLLING ===")
    all_paths = sorted(set(before) | set(after), key=str.lower)
    if not all_paths:
        print("No scriptlog*.xml or log.txt files were found in discovered bundles.")
        return

    for path in all_paths:
        if path not in after:
            print("REMOVED", f"path={path}")
            continue

        size, mtime_ns, script_name = after[path]
        changed = before.get(path, ())[:2] != (size, mtime_ns)
        timestamp = dt.datetime.fromtimestamp(
            mtime_ns / 1_000_000_000
        ).isoformat(timespec="seconds")
        print(
            "CHANGED" if changed else "unchanged",
            f"size={size}",
            f"mtime={timestamp}",
            f"script={script_name!r}",
            f"path={path}",
        )


def poll_open_files(
    process_ids: set[int],
    duration: float,
    interval: float,
) -> None:
    observed: dict[int, set[tuple[str, str, str]]] = {
        pid: set() for pid in process_ids
    }
    errors: dict[int, str] = {}

    print(f"\n=== POLLING OPEN FILES FOR {duration:g} SECONDS ===")
    if process_ids and duration > 0:
        samples = max(1, math.ceil(duration / interval))
        for sample_index in range(samples):
            for pid in process_ids:
                records, error = lsof_matching_files(pid)
                observed[pid].update(records)
                if error:
                    errors[pid] = error
            if sample_index + 1 < samples:
                time.sleep(interval)

    for pid in sorted(observed):
        print(f"\nPID {pid}:")
        records = sorted(observed[pid], key=lambda item: item[2].lower())
        if not records:
            print("  No matching open files observed.")
            if errors.get(pid):
                print("  lsof error:", errors[pid])
        for descriptor, access, path in records:
            print(f"  fd={descriptor} access={access} path={path}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--duration",
        type=float,
        default=10.0,
        help="Number of seconds to poll open Foldit files (default: 10).",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=0.5,
        help="Seconds between lsof samples (default: 0.5).",
    )
    args = parser.parse_args()
    if args.duration < 0:
        parser.error("--duration must be zero or greater")
    if args.interval <= 0:
        parser.error("--interval must be greater than zero")
    return args


def main() -> int:
    args = parse_args()

    print("=== SYSTEM ===")
    print("Timestamp:", dt.datetime.now().isoformat(timespec="seconds"))
    print("macOS:", platform.mac_ver()[0] or "<not macOS>")
    print("Architecture:", platform.machine())
    print("Python:", sys.version.replace("\n", " "))
    print("Python executable:", sys.executable)
    print("psutil:", psutil.__version__)

    processes = collect_foldit_processes()
    process_ids = {int(info["pid"]) for info in processes}

    print("\n=== FOLDIT PROCESSES ===")
    if not processes:
        print("No Foldit processes found.")
    for info in processes:
        pid = int(info["pid"])
        creation_time = info.get("create_time")
        if creation_time:
            creation_time = dt.datetime.fromtimestamp(creation_time).isoformat(
                timespec="seconds"
            )
        print(f"\nPID: {pid}")
        print("Name:", info.get("name"))
        print("Username:", info.get("username"))
        print("Executable:", info.get("exe"))
        print("Command line:", repr(info.get("cmdline")))
        print("Creation time:", creation_time)
        print("CWD from lsof:", lsof_cwd(pid))

    bundle_paths = print_appkit_information(processes)
    for info in processes:
        bundle = bundle_from_executable(info.get("exe"))
        if bundle is not None:
            bundle_paths.add(bundle)

    quartz = load_quartz_and_print_permissions()
    print_quartz_windows(quartz, process_ids)

    resource_roots = {
        bundle / "Contents" / "Resources"
        for bundle in bundle_paths
        if (bundle / "Contents" / "Resources").is_dir()
    }
    print("\n=== DISCOVERED RESOURCE ROOTS ===")
    if not resource_roots:
        print("No Foldit Contents/Resources directories found.")
    for root in sorted(resource_roots, key=lambda path: str(path).lower()):
        print(root)

    before = file_snapshot(resource_roots)
    poll_open_files(process_ids, args.duration, args.interval)
    after = file_snapshot(resource_roots)
    print_file_changes(before, after)
    print("\n=== END ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
