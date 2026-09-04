import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from repomin.text_reducer import (
    TextReducer,
    TextLineTarget,
    _describe_targets,
    _discover_targets,
    _target_location,
)


def _write_text(path: Path, text: str) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        stream.write(text)


class TextReducerDiscoveryTest(unittest.TestCase):
    def test_discover_targets_produces_ordered_non_overlapping_lines(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_text(root / "data.txt", "alpha\nbeta\ngamma\n")
            targets = _discover_targets(root, ["data.txt"])
            self.assertEqual(3, len(targets))
            self.assertEqual([0, 6, 11], [target.start for target in targets])
            self.assertEqual([6, 11, 17], [target.end for target in targets])
            self.assertEqual(
                [
                    hashlib.sha256(line.encode("utf-8")).hexdigest()
                    for line in ("alpha\n", "beta\n", "gamma\n")
                ],
                [target.content_hash for target in targets],
            )

    def test_discover_targets_skips_missing_binary_and_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_text(root / "data.txt", "a\nb\n")
            (root / "binary.dat").write_bytes(b"\xff\xfe\xfa")
            self.assertEqual(2, len(_discover_targets(root, ["data.txt", "missing.txt", "binary.dat"])))

    @unittest.skipIf(os.name == "nt", "symlink creation is not generally available")
    def test_discover_targets_does_not_read_through_a_parent_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            root = parent / "root"
            root.mkdir()
            outside = parent / "outside"
            outside.mkdir()
            _write_text(outside / "selected.txt", "secret\n")
            (root / "alias").symlink_to(outside, target_is_directory=True)

            with mock.patch.object(
                Path,
                "open",
                side_effect=AssertionError("unexpected external read"),
            ) as opened:
                self.assertEqual([], _discover_targets(root, ["alias/selected.txt"]))
            opened.assert_not_called()
            reducer = TextReducer(
                SimpleNamespace(current=root),
                ["alias/selected.txt"],
            )
            self.assertFalse(reducer.is_applicable())

    def test_describe_and_location_helpers(self) -> None:
        target = TextLineTarget(
            Path("data.txt"),
            0,
            6,
            "remove line 1 of data.txt",
            "a" * 64,
        )
        self.assertEqual((Path("data.txt"), 0, 6), _target_location(target))
        self.assertEqual("remove 1 line(s) from data.txt", _describe_targets([target]))


if __name__ == "__main__":
    unittest.main()
