"""Application resources and writable data, independent of the working directory."""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import stat
import sys


@dataclass(frozen=True)
class AppPaths:
    root: Path
    resources: Path

    @property
    def data(self):
        return self.root / "data"

    @property
    def puzzle_logs(self):
        return self.data / "puzzle_logs"

    @property
    def reports(self):
        return self.data / "ligand_viewer_reports"

    @property
    def logs(self):
        return self.data / "logs"

    def prepare_data(self):
        """Move the old local layout once; never replace a different existing file.

        Call only during startup, before opening settings, CSVs, or SQLite.
        Interrupted migrations can be resumed by the next launch.
        """
        if self._is_link(self.data):
            raise RuntimeError(f"Data directory must be a regular folder: {self.data}")
        self.data.mkdir(exist_ok=True)
        entries = [self.root / "Foldit Monitor.json"]
        entries += list(self.root.glob("Foldit Monitor.json.*"))
        entries += [self.root / name for name in (
            "puzzle_logs", "ligand_viewer_reports", "logs", "foldit_backup",
        )]
        for source in entries:
            moving_profile = source.name == "Foldit Monitor.json" and source.is_file()
            self._move_legacy(source, self.data / source.name)
            if moving_profile:
                self._migrate_settings_paths()

    @classmethod
    def _move_legacy(cls, source, target):
        if not source.exists() and not source.is_symlink():
            return
        if cls._is_link(source) or cls._is_link(target):
            raise RuntimeError(f"Cannot migrate a linked path: {source}")
        if not target.exists():
            source.rename(target)
            return
        if source.is_dir() and target.is_dir():
            for child in source.iterdir():
                cls._move_legacy(child, target / child.name)
            source.rmdir()
        else:
            raise FileExistsError(
                f"Both legacy and new data exist; neither was overwritten: {source}, {target}"
            )

    @staticmethod
    def _is_link(path):
        # Includes Windows junctions on Python 3.11, which has no Path.is_junction.
        if path.is_symlink():
            return True
        try:
            attributes = getattr(path.lstat(), "st_file_attributes", 0)
        except FileNotFoundError:
            return False
        return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))

    def _migrate_settings_paths(self):
        settings_file = self.data / "Foldit Monitor.json"
        if not settings_file.is_file():
            return
        try:
            settings = json.loads(settings_file.read_text(encoding="utf-8-sig"))
        except (ValueError, OSError):
            # Settings handles a damaged profile without replacing it.
            return
        if not isinstance(settings, dict):
            return
        changed = False
        for section, key in (
            ("logging", "logs_folder"),
            ("sound", "alert_file"),
        ):
            group = settings.get(section, {})
            value = group.get(key) if isinstance(group, dict) else None
            if not isinstance(value, str) or not value:
                continue
            path = Path(value)
            location = Path(os.path.abspath(path if path.is_absolute() else self.root / path))
            replacement = value
            if section == "sound" and location == self.root / "alert.wav":
                # The usual relative name is resolved from bundled resources.
                replacement = "alert.wav" if value == "alert.wav" else str(self.resources / "alert.wav")
            else:
                for name in ("puzzle_logs", "ligand_viewer_reports", "logs", "foldit_backup"):
                    legacy_folder = self.root / name
                    if location.is_relative_to(legacy_folder):
                        relocated = self.data / name / location.relative_to(legacy_folder)
                        replacement = str(relocated.relative_to(self.data)) if section == "logging" else str(relocated)
                        break
                else:
                    if not path.is_absolute():
                        # Custom locations outside the moved folders stay put.
                        replacement = str(location)
            if replacement != value:
                group[key] = replacement
                changed = True
        if changed:
            temporary = settings_file.with_suffix(".json.paths.tmp")
            try:
                temporary.write_text(json.dumps(settings, indent=4, ensure_ascii=False) + "\n", encoding="utf-8")
                os.replace(temporary, settings_file)
            finally:
                temporary.unlink(missing_ok=True)


def get_app_paths():
    if getattr(sys, "frozen", False):
        root = Path(sys.executable).resolve().parent
        bundle = Path(getattr(sys, "_MEIPASS", root))
        resources = bundle / "modules" / "resources"
    else:
        root = Path(__file__).resolve().parent.parent
        resources = root / "modules" / "resources"
    return AppPaths(root, resources)
