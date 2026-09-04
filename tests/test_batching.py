import hashlib
import os
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

from repomin.batching import interval_layers, try_hierarchical_batches
from repomin.text_edits import remove_text_targets


@dataclass(frozen=True)
class _Interval:
    name: str
    path: Path
    start: int
    end: int


@dataclass(frozen=True)
class _Removal:
    path: Path
    start: int
    end: int
    content_hash: str


def _write_preserving_newlines(path: Path, text: str) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        stream.write(text)


class _RecordingSession:
    def __init__(self, accepted_description=None) -> None:
        self.accepted_description = accepted_description
        self.calls = []

    def try_mutations(self, _phase, candidates):
        descriptions = [candidate.description for candidate in candidates]
        self.calls.append(descriptions)
        if self.accepted_description in descriptions:
            return descriptions.index(self.accepted_description)
        return None


class BatchingTest(unittest.TestCase):
    def test_interval_layers_put_parents_first_and_split_partial_overlaps(self) -> None:
        root = _Interval("root", Path("a.txt"), 0, 100)
        independent = _Interval("independent", Path("b.txt"), 0, 10)
        child = _Interval("child", Path("a.txt"), 10, 40)
        overlapping_child = _Interval("overlap", Path("a.txt"), 30, 60)
        duplicate = _Interval("duplicate", Path("a.txt"), 10, 40)

        layers = interval_layers(
            [root, independent, child, overlapping_child, duplicate],
            lambda item: (item.path, item.start, item.end),
        )

        self.assertEqual(
            [["root", "independent"]],
            [[item.name for item in pack] for pack in layers[0]],
        )
        self.assertEqual(
            [["child"], ["overlap"]],
            [[item.name for item in pack] for pack in layers[1]],
        )

    def test_interval_layers_scale_to_thousands_of_independent_targets(self) -> None:
        items = [
            _Interval(str(index), Path("large.txt"), index * 2, index * 2 + 1)
            for index in range(4000)
        ]

        layers = interval_layers(
            items,
            lambda item: (item.path, item.start, item.end),
        )

        self.assertEqual(1, len(layers))
        self.assertEqual(1, len(layers[0]))
        self.assertEqual(4000, len(layers[0][0]))

    def test_interval_layers_handle_deep_containment_without_recursion(self) -> None:
        items = [
            _Interval(str(index), Path("deep.txt"), index, 4000 - index)
            for index in range(1500)
        ]

        layers = interval_layers(
            items,
            lambda item: (item.path, item.start, item.end),
        )

        self.assertEqual(1500, len(layers))
        self.assertTrue(all(len(layer) == 1 for layer in layers))

    def test_hierarchical_batches_only_descend_until_a_chunk_is_accepted(self) -> None:
        session = _RecordingSession("4,5,6,7")
        accepted = try_hierarchical_batches(
            session,
            "test",
            list(range(8)),
            lambda items: ",".join(str(item) for item in items),
            lambda _root, _items: True,
        )

        self.assertTrue(accepted)
        self.assertEqual(
            [["0,1,2,3,4,5,6,7"], ["0,1,2,3", "4,5,6,7"]],
            session.calls,
        )

    def test_small_batch_tries_the_full_set_before_singletons(self) -> None:
        session = _RecordingSession("0,1")

        accepted = try_hierarchical_batches(
            session,
            "test",
            [0, 1],
            lambda items: ",".join(str(item) for item in items),
            lambda _root, _items: True,
        )

        self.assertTrue(accepted)
        self.assertEqual([["0,1"]], session.calls)

    def test_text_batch_validates_every_range_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "fixture.txt"
            original = "alpha\nbeta\ngamma\n"
            _write_preserving_newlines(path, original)

            def removal(start, end):
                return _Removal(
                    Path("fixture.txt"),
                    start,
                    end,
                    hashlib.sha256(original[start:end].encode("utf-8")).hexdigest(),
                )

            alpha = removal(0, 6)
            gamma = removal(11, 17)
            stale = _Removal(Path("fixture.txt"), 6, 11, "0" * 64)

            self.assertFalse(remove_text_targets(root, (alpha, stale)))
            self.assertEqual(original, path.read_text(encoding="utf-8"))
            self.assertTrue(remove_text_targets(root, (alpha, gamma)))
            self.assertEqual("beta\n", path.read_text(encoding="utf-8"))

    def test_text_batch_with_stale_target_does_not_modify_any_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first.txt"
            second = root / "second.txt"
            first_original = "alpha\nbeta\n"
            second_original = "gamma\ndelta\n"
            _write_preserving_newlines(first, first_original)
            _write_preserving_newlines(second, second_original)

            valid = _Removal(
                Path("first.txt"),
                0,
                6,
                hashlib.sha256(b"alpha\n").hexdigest(),
            )
            stale = _Removal(Path("second.txt"), 0, 6, "0" * 64)

            self.assertFalse(remove_text_targets(root, (valid, stale)))
            self.assertEqual(first_original, first.read_text(encoding="utf-8"))
            self.assertEqual(second_original, second.read_text(encoding="utf-8"))

    def test_text_batch_rejects_a_target_outside_the_mutation_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            root = parent / "root"
            root.mkdir()
            outside = parent / "outside.txt"
            original = "alpha\nbeta\n"
            _write_preserving_newlines(outside, original)
            for escaped_path in (outside, Path("../outside.txt")):
                with self.subTest(path=escaped_path):
                    removal = _Removal(
                        escaped_path,
                        0,
                        6,
                        hashlib.sha256(b"alpha\n").hexdigest(),
                    )

                    self.assertFalse(remove_text_targets(root, (removal,)))
                    self.assertEqual(original, outside.read_text(encoding="utf-8"))

    @unittest.skipIf(os.name == "nt", "symlink creation is not generally available")
    def test_text_batch_rejects_a_target_through_a_parent_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            root = parent / "root"
            root.mkdir()
            outside = parent / "outside"
            outside.mkdir()
            selected = outside / "selected.txt"
            original = "alpha\nbeta\n"
            _write_preserving_newlines(selected, original)
            (root / "alias").symlink_to(outside, target_is_directory=True)
            removal = _Removal(
                Path("alias/selected.txt"),
                0,
                6,
                hashlib.sha256(b"alpha\n").hexdigest(),
            )

            self.assertFalse(remove_text_targets(root, (removal,)))
            self.assertEqual(original, selected.read_text(encoding="utf-8"))

    @unittest.skipIf(os.name == "nt", "symlink creation is not generally available")
    def test_text_batch_rejects_an_in_root_parent_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            real = root / "real"
            real.mkdir()
            selected = real / "selected.txt"
            original = "alpha\nbeta\n"
            _write_preserving_newlines(selected, original)
            (root / "alias").symlink_to(real, target_is_directory=True)
            removal = _Removal(
                Path("alias/selected.txt"),
                0,
                6,
                hashlib.sha256(b"alpha\n").hexdigest(),
            )

            self.assertFalse(remove_text_targets(root, (removal,)))
            self.assertEqual(original, selected.read_text(encoding="utf-8"))

    def test_text_batch_rolls_back_first_file_when_second_write_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first.txt"
            second = root / "second.txt"
            first_original = "alpha\nbeta\n"
            second_original = "gamma\ndelta\n"
            _write_preserving_newlines(first, first_original)
            _write_preserving_newlines(second, second_original)

            first_removal = _Removal(
                Path("first.txt"),
                0,
                6,
                hashlib.sha256(b"alpha\n").hexdigest(),
            )
            second_removal = _Removal(
                Path("second.txt"),
                0,
                6,
                hashlib.sha256(b"gamma\n").hexdigest(),
            )
            original_open = Path.open
            failed = False
            first_content_before_failure = None

            def fail_second_write_once(path, mode="r", *args, **kwargs):
                nonlocal failed, first_content_before_failure
                if path == second and mode == "w" and not failed:
                    with original_open(
                        first, "r", encoding="utf-8", newline=""
                    ) as stream:
                        first_content_before_failure = stream.read()
                    failed = True
                    raise OSError("simulated second-file write failure")
                return original_open(path, mode, *args, **kwargs)

            with mock.patch.object(Path, "open", new=fail_second_write_once):
                self.assertFalse(
                    remove_text_targets(root, (first_removal, second_removal))
                )

            self.assertTrue(failed)
            self.assertEqual("beta\n", first_content_before_failure)
            self.assertEqual(first_original, first.read_text(encoding="utf-8"))
            self.assertEqual(second_original, second.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
