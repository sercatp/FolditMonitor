import ast
import os
from pathlib import Path
import tempfile
import unittest

from track_saves import (
    find_latest_track_save,
    replace_track_save_tree,
    resolve_track_copy_paths,
    track_directory_name,
)


def make_track(root: Path, puzzle: str, user: str, track: str, filename: str, mtime: float) -> Path:
    directory = root / "puzzles" / puzzle / user / track
    directory.mkdir(parents=True, exist_ok=True)
    save = directory / filename
    save.write_text(f"{puzzle}-{track}", encoding="utf-8")
    os.utime(save, (mtime, mtime))
    return directory


class TrackSaveCases(unittest.TestCase):
    def test_empty_log_track_uses_default_save_directory(self):
        self.assertEqual(track_directory_name(""), "default")
        self.assertEqual(track_directory_name(None), "default")

    def test_latest_save_is_selected_only_within_requested_track(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            expected = make_track(root, "0000000002", "0000000042", "red", "new.ir_solution", 30)
            make_track(root, "0000000001", "0000000042", "red", "old.ir_solution", 10)
            make_track(root, "0000000099", "0000000042", "blue", "newest.ir_solution", 50)

            result = find_latest_track_save(str(root), "red")

            self.assertIsNotNone(result)
            self.assertEqual(Path(result.path), expected)
            self.assertEqual(result.internal_puzzle_id, "0000000002")
            self.assertEqual(result.user_id, "0000000042")

    def test_same_root_copy_resolves_to_sibling_track(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = make_track(root, "0002014370", "0000918066", "default", "quicksave.ir_solution", 20)

            location, target = resolve_track_copy_paths(str(root), "default", str(root), "newtrack123")

            self.assertEqual(Path(location.path), source)
            self.assertEqual(
                Path(target),
                root / "puzzles" / "0002014370" / "0000918066" / "newtrack123",
            )

    def test_cross_install_copy_preserves_puzzle_and_user_but_changes_track(self):
        with tempfile.TemporaryDirectory() as temp:
            source_root = Path(temp) / "Foldit1"
            target_root = Path(temp) / "Foldit2"
            make_track(source_root, "0002014370", "0000918066", "drw", "autosave.ir_solution", 20)

            _, target = resolve_track_copy_paths(str(source_root), "drw", str(target_root), "cw")

            self.assertEqual(
                Path(target),
                target_root / "puzzles" / "0002014370" / "0000918066" / "cw",
            )

    def test_replace_backs_up_only_target_track_and_preserves_siblings(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = make_track(root, "0002014370", "0000918066", "source", "source.ir_solution", 20)
            target = make_track(root, "0002014370", "0000918066", "target", "target.ir_solution", 10)
            sibling = make_track(root, "0002014370", "0000918066", "untouched", "keep.ir_solution", 5)
            backup = root / "backup" / "target"

            replace_track_save_tree(str(source), str(target), str(backup))

            self.assertTrue((target / "source.ir_solution").is_file())
            self.assertFalse((target / "target.ir_solution").exists())
            self.assertTrue((backup / "target.ir_solution").is_file())
            self.assertTrue((sibling / "keep.ir_solution").is_file())

    def test_same_track_copy_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            make_track(root, "0002014370", "0000918066", "default", "save.ir_solution", 20)

            with self.assertRaisesRegex(ValueError, "same Foldit Track"):
                resolve_track_copy_paths(str(root), "default", str(root), "default")

    def test_path_like_track_name_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Invalid Foldit Track"):
            track_directory_name("../other")


class MonitorTrackIntegrationSourceCases(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.monitor_path = Path(__file__).resolve().parents[1] / "Foldit Monitor.pyw"
        cls.source = cls.monitor_path.read_text(encoding="utf-8-sig")

    def test_launch_prefers_unused_installation_and_duplicates_only_as_fallback(self):
        module = ast.parse(self.source, filename=str(self.monitor_path))
        function = next(
            node for node in module.body
            if isinstance(node, ast.FunctionDef) and node.name == "launch_next_client"
        )
        launches = []
        state = {"installations": (["Foldit1", "Foldit2"], {"Foldit1"})}
        namespace = {
            "find_foldit_installations": lambda: state["installations"],
            "launch_client_folder": launches.append,
            "normalize_path": lambda path: path,
        }
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(self.monitor_path), "exec"), namespace)

        namespace["launch_next_client"]()
        self.assertEqual(launches, ["Foldit2"])

        launches.clear()
        state["installations"] = (["Foldit1", "Foldit2"], {"Foldit1", "Foldit2"})
        namespace["launch_next_client"]()
        self.assertEqual(launches, ["Foldit1"])

    def test_compact_client_column_does_not_append_binding_status_text(self):
        self.assertNotIn("[waiting for log]", self.source)
        self.assertNotIn("[shared log]", self.source)
        self.assertNotIn("[ambiguous log]", self.source)
        self.assertIn('client.binding_status in ("collision", "ambiguous")', self.source)

    def test_copy_jobs_carry_both_tracks_and_no_longer_reject_shared_root(self):
        self.assertIn("'source_track':", self.source)
        self.assertIn("'target_track':", self.source)
        self.assertIn("resolve_track_copy_paths(", self.source)
        self.assertNotIn("Track saves are not supported", self.source)

    def test_duplicate_launch_menu_is_hidden_while_an_installation_is_unused(self):
        self.assertIn("if unused_folders and is_running:", self.source)
        self.assertIn('menu.add_command(label=f"✓ {name}", state="disabled")', self.source)


if __name__ == "__main__":
    unittest.main()
