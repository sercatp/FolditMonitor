"""Resolve Foldit save directories for one Track without scanning unrelated files."""

from __future__ import annotations

from dataclasses import dataclass
import os
import shutil
from typing import Optional


@dataclass(frozen=True)
class TrackSaveLocation:
    data_root: str
    track: str
    internal_puzzle_id: str
    user_id: str
    path: str
    modified_time: float


def track_directory_name(track: Optional[str]) -> str:
    """Return the on-disk Track directory used by Foldit.

    The empty suffix in macOS' ``scriptlog..xml`` is the default Track. Named
    Track values come from a log filename, so reject path-like values before
    using one as a directory component.
    """
    value = str(track or "").strip() or "default"
    if value in (".", "..") or os.path.basename(value) != value:
        raise ValueError(f"Invalid Foldit Track name: {value!r}")
    if os.altsep and os.altsep in value:
        raise ValueError(f"Invalid Foldit Track name: {value!r}")
    return value


def _directory_latest_mtime(path: str) -> float:
    latest = 0.0
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                try:
                    if entry.is_file(follow_symlinks=False):
                        latest = max(latest, entry.stat(follow_symlinks=False).st_mtime)
                except OSError:
                    continue
    except OSError:
        return 0.0
    return latest


def find_latest_track_save(data_root: str, track: Optional[str]) -> Optional[TrackSaveLocation]:
    """Find the newest ``puzzles/<puzzle>/<user>/<track>`` directory.

    Only the three directory levels that define Foldit's save layout are
    inspected. Files in other Tracks and unrelated files in the data root are
    never traversed.
    """
    root = os.path.realpath(os.path.abspath(str(data_root)))
    puzzles_root = os.path.join(root, "puzzles")
    track_name = track_directory_name(track)
    best: Optional[TrackSaveLocation] = None

    try:
        puzzle_entries = list(os.scandir(puzzles_root))
    except OSError:
        return None

    for puzzle_entry in puzzle_entries:
        try:
            if not puzzle_entry.is_dir(follow_symlinks=False):
                continue
            with os.scandir(puzzle_entry.path) as user_entries:
                for user_entry in user_entries:
                    if not user_entry.is_dir(follow_symlinks=False):
                        continue
                    candidate = os.path.join(user_entry.path, track_name)
                    if not os.path.isdir(candidate):
                        continue
                    modified = _directory_latest_mtime(candidate)
                    location = TrackSaveLocation(
                        data_root=root,
                        track=track_name,
                        internal_puzzle_id=puzzle_entry.name,
                        user_id=user_entry.name,
                        path=candidate,
                        modified_time=modified,
                    )
                    if best is None or location.modified_time > best.modified_time:
                        best = location
        except OSError:
            continue
    return best


def resolve_track_copy_paths(
    source_root: str,
    source_track: Optional[str],
    target_root: str,
    target_track: Optional[str],
) -> tuple[TrackSaveLocation, str]:
    """Resolve an existing source Track and the corresponding target Track path."""
    source = find_latest_track_save(source_root, source_track)
    if source is None:
        display_track = track_directory_name(source_track)
        raise FileNotFoundError(f"No saves found for Track {display_track!r}")

    target_root = os.path.realpath(os.path.abspath(str(target_root)))
    target = os.path.join(
        target_root,
        "puzzles",
        source.internal_puzzle_id,
        source.user_id,
        track_directory_name(target_track),
    )
    if os.path.normcase(os.path.realpath(source.path)) == os.path.normcase(os.path.realpath(target)):
        raise ValueError("Source and target refer to the same Foldit Track save directory")
    return source, target


def replace_track_save_tree(source: str, target: str, backup_path: Optional[str] = None) -> None:
    """Replace one Track tree while leaving sibling Track directories untouched."""
    source_key = os.path.normcase(os.path.realpath(os.path.abspath(source)))
    target_key = os.path.normcase(os.path.realpath(os.path.abspath(target)))
    if source_key == target_key:
        raise ValueError("Source and target refer to the same Foldit Track save directory")

    os.makedirs(os.path.dirname(target), exist_ok=True)
    if os.path.exists(target):
        if backup_path:
            os.makedirs(os.path.dirname(backup_path), exist_ok=True)
            shutil.move(target, backup_path)
        else:
            shutil.rmtree(target)
    shutil.copytree(source, target)
