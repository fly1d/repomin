"""Doctor and reduction must validate the same selected repository tree."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shlex
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from repomin.cli import main
from repomin.gitignore import load_gitignore
from repomin.report import validate_report_file
from repomin.session import SessionError, _validate_repository_entries


class InputPreflightTest(unittest.TestCase):
    def _source(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        # Match load_gitignore's canonical root on macOS (/var -> /private/var)
        # and Windows (short temporary paths -> long paths).
        root = Path(temporary.name).resolve()
        source = root / "source"
        source.mkdir()
        (source / "reproduce.py").write_text(
            "from pathlib import Path\n"
            "if not Path('required.txt').is_file():\n"
            "    raise SystemExit(2)\n"
            "print('ORIGINAL_FAILURE')\n"
            "raise SystemExit(1)\n",
            encoding="utf-8",
        )
        (source / "required.txt").write_text("keep\n", encoding="utf-8")
        command = [sys.executable, "reproduce.py"]
        command_text = (
            subprocess.list2cmdline(command) if os.name == "nt" else shlex.join(command)
        )
        arguments = [
            str(source), "--command", command_text,
            "--match", "ORIGINAL_FAILURE", "--exit-code", "1",
            "--adapter", "none", "--source-reducer", "none",
            "--baseline-runs", "1", "--output", str(root / "output"),
        ]
        return root, source, arguments

    def _unsafe_entry(self, path, root, kind):
        path.parent.mkdir(parents=True, exist_ok=True)
        if kind == "absolute-link":
            external = root / "external.txt"
            external.write_text("outside\n", encoding="utf-8")
            try:
                path.symlink_to(external)
            except OSError as exc:
                self.skipTest("symlinks unavailable: %s" % exc)
        elif kind == "escaping-link":
            external = root / "external.txt"
            external.write_text("outside\n", encoding="utf-8")
            try:
                path.symlink_to(os.path.relpath(external, path.parent))
            except OSError as exc:
                self.skipTest("symlinks unavailable: %s" % exc)
        elif kind == "hardlink":
            external = root / "external.txt"
            external.write_text("outside\n", encoding="utf-8")
            try:
                os.link(external, path)
            except OSError as exc:
                self.skipTest("hardlinks unavailable: %s" % exc)
        else:
            if not hasattr(os, "mkfifo"):
                self.skipTest("FIFOs unavailable")
            os.mkfifo(path)

    def _call(self, mode, arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = main([mode, *arguments])
        return result, stdout.getvalue(), stderr.getvalue()

    def test_ignored_unsafe_entries_pass_doctor_and_reduce(self):
        for selection in ("name", "path", "gitignore", "recursive"):
            for kind in ("absolute-link", "escaping-link", "hardlink", "fifo"):
                with self.subTest(selection=selection, kind=kind):
                    root, source, arguments = self._source()
                    self._unsafe_entry(source / "generated" / "unsafe", root, kind)
                    if selection == "name":
                        arguments += ["--ignore", "generated"]
                    elif selection == "path":
                        arguments += ["--ignore-path", "generated/unsafe"]
                    elif selection == "gitignore":
                        (source / ".gitignore").write_text("generated/\n", encoding="utf-8")
                        arguments += ["--gitignore"]
                    else:
                        (source / ".gitignore").write_text("# root\n", encoding="utf-8")
                        (source / "generated" / ".gitignore").write_text("unsafe\n", encoding="utf-8")
                        arguments += ["--gitignore-recursive"]

                    status, stdout, stderr = self._call("doctor", [*arguments, "--json"])
                    self.assertEqual(0, status, stdout + stderr)
                    self.assertEqual("pass", json.loads(stdout)["baseline"]["status"])
                    status, stdout, stderr = self._call("reduce", arguments)
                    self.assertEqual(0, status, stderr)
                    payload = root / "output"
                    self.assertFalse((payload / "generated" / "unsafe").exists())
                    self.assertFalse((payload / "generated" / "unsafe").is_symlink())
                    self.assertTrue((payload / "required.txt").is_file())
                    report = root / "output.repomin" / "report.json"
                    self.assertTrue(report.is_file())
                    validate_report_file(report, payload=payload)
                    self.assertTrue((source / "generated" / "unsafe").lstat())

    def test_unignored_and_kept_unsafe_entries_fail_before_execution(self):
        for kind in ("absolute-link", "escaping-link", "hardlink", "fifo"):
            for selection in ("unignored", "keep-directory", "keep-child"):
                with self.subTest(kind=kind, selection=selection):
                    root, source, arguments = self._source()
                    self._unsafe_entry(source / "generated" / "kept" / "unsafe", root, kind)
                    if selection != "unignored":
                        arguments += ["--ignore", "generated", "--keep",
                                      "generated" if selection == "keep-directory" else "generated/kept"]
                    for mode, expected, runner in (
                        ("doctor", 1, "repomin.doctor._runner"),
                        ("reduce", 2, "repomin.cli._build_runner"),
                    ):
                        with patch(runner) as build_runner:
                            status, stdout, stderr = self._call(mode, arguments)
                        self.assertEqual(expected, status, stdout + stderr)
                        self.assertIn("unsafe", stdout + stderr)
                        build_runner.assert_not_called()
                        self.assertFalse((root / "output").exists())
                        self.assertFalse((root / "output.repomin").exists())

    def test_preflight_refreshes_missing_directory_entry_link_counts(self):
        root, source, _ = self._source()
        unsafe = source / "unsafe"
        self._unsafe_entry(unsafe, root, "hardlink")
        original_scandir = os.scandir

        def incomplete_scandir(directory):
            with original_scandir(directory) as entries:
                result = []
                for entry in entries:
                    status = entry.stat(follow_symlinks=False)
                    result.append(SimpleNamespace(
                        name=entry.name,
                        path=entry.path,
                        stat=lambda follow_symlinks=False, status=status: SimpleNamespace(
                            st_mode=status.st_mode, st_nlink=0,
                        ),
                    ))
                return result

        with patch("repomin.session.os.scandir", incomplete_scandir):
            # Ignored entries still do not enter the selected-tree boundary.
            _validate_repository_entries(source, {"unsafe"})
            with self.assertRaisesRegex(SessionError, "hard-linked regular file.*unsafe"):
                _validate_repository_entries(source, set())

    def test_keep_reincludes_safe_file_without_including_unsafe_sibling(self):
        root, source, arguments = self._source()
        self._unsafe_entry(source / "generated" / "unsafe", root, "absolute-link")
        (source / "generated" / "keep.txt").write_text("keep\n", encoding="utf-8")
        arguments += ["--ignore", "generated", "--keep", "generated/keep.txt"]
        for mode in ("doctor", "reduce"):
            status, stdout, stderr = self._call(mode, arguments)
            self.assertEqual(0, status, stdout + stderr)
        self.assertEqual("keep\n", (root / "output/generated/keep.txt").read_text())
        self.assertFalse((root / "output/generated/unsafe").is_symlink())

    def test_unsafe_rule_files_are_rejected_before_reading(self):
        for nested in (False, True):
            for kind in ("absolute-link", "escaping-link", "hardlink", "fifo"):
                with self.subTest(nested=nested, kind=kind):
                    root, source, arguments = self._source()
                    if nested:
                        (source / ".gitignore").write_text("# root\n", encoding="utf-8")
                    rules = source / "nested" / ".gitignore" if nested else source / ".gitignore"
                    self._unsafe_entry(rules, root, kind)
                    arguments += ["--gitignore-recursive" if nested else "--gitignore"]
                    original_read = Path.read_bytes

                    def checked_read(path):
                        self.assertNotEqual(rules.resolve(), path)
                        return original_read(path)

                    for mode, expected in (("doctor", 1), ("reduce", 2)):
                        with patch.object(Path, "read_bytes", checked_read):
                            status, stdout, stderr = self._call(mode, arguments)
                        self.assertEqual(expected, status, stdout + stderr)
                        self.assertIn("gitignore", stdout + stderr)

    def test_safe_relative_rule_link_and_explicit_external_rules_still_load(self):
        root, source, _ = self._source()
        rules = source / "rules.ignore"
        rules.write_text("generated/\n", encoding="utf-8")
        try:
            (source / ".gitignore").symlink_to("rules.ignore")
        except OSError as exc:
            self.skipTest("symlinks unavailable: %s" % exc)
        _, labels, _, _ = load_gitignore(source, True, ())
        self.assertEqual(("rules.ignore",), labels)
        external = root / "external.ignore"
        external.write_text("generated/\n", encoding="utf-8")
        for value in (str(external), "../external.ignore"):
            matcher, labels, _, _ = load_gitignore(source, False, (value,))
            self.assertEqual((str(external),), labels)
            self.assertTrue(matcher.matches(Path("generated"), is_directory=True))

    def test_external_source_alias_rejects_unsafe_rule_links_before_reading(self):
        for kind in ("absolute-link", "escaping-link"):
            with self.subTest(kind=kind):
                root, source, arguments = self._source()
                self._unsafe_entry(source / ".gitignore", root, kind)
                # If read, these rules hide the unsafe repository entry from
                # the subsequent effective-tree validation.
                (root / "external.txt").write_text(".gitignore\n", encoding="utf-8")
                alias = root / "source-alias"
                try:
                    alias.symlink_to(source, target_is_directory=True)
                except OSError as exc:
                    self.skipTest("directory symlinks unavailable: %s" % exc)
                rules = str(alias / ".gitignore")
                with patch.object(Path, "read_bytes") as read:
                    with self.assertRaisesRegex(ValueError, "unsafe symbolic link"):
                        load_gitignore(source, False, (rules,))
                read.assert_not_called()
                arguments += ["--gitignore-file", rules]
                for mode, expected in (("doctor", 1), ("reduce", 2)):
                    with patch.object(Path, "read_bytes") as read:
                        status, stdout, stderr = self._call(mode, arguments)
                    self.assertEqual(expected, status, stdout + stderr)
                    self.assertIn("unsafe symbolic link", stdout + stderr)
                    read.assert_not_called()
                    self.assertFalse((root / "output").exists())
                    self.assertFalse((root / "output.repomin").exists())

    def test_external_source_alias_preserves_safe_rules_and_external_config(self):
        root, source, _ = self._source()
        alias = root / "source-alias"
        try:
            alias.symlink_to(source, target_is_directory=True)
        except OSError as exc:
            self.skipTest("directory symlinks unavailable: %s" % exc)
        rules = source / ".gitignore"
        rules.write_text("generated/\n", encoding="utf-8")
        matcher, labels, _, _ = load_gitignore(source, False, (str(alias / ".gitignore"),))
        self.assertEqual((".gitignore",), labels)
        self.assertTrue(matcher.matches(Path("generated"), is_directory=True))

        rules.unlink()
        (source / "rules.ignore").write_text("generated/\n", encoding="utf-8")
        rules.symlink_to("rules.ignore")
        matcher, labels, _, _ = load_gitignore(source, False, (str(alias / ".gitignore"),))
        self.assertEqual(("rules.ignore",), labels)
        self.assertTrue(matcher.matches(Path("generated"), is_directory=True))

        external = root / "external.ignore"
        external.write_text("external/\n", encoding="utf-8")
        matcher, labels, _, _ = load_gitignore(
            source, False, (str(alias / ".." / "external.ignore"),)
        )
        self.assertEqual((str(external),), labels)
        self.assertTrue(matcher.matches(Path("external"), is_directory=True))

    def test_external_junction_alias_cannot_hide_unsafe_repository_rule_link(self):
        root, source, _ = self._source()
        self._unsafe_entry(source / ".gitignore", root, "absolute-link")
        alias = root / "source-alias"
        try:
            alias.symlink_to(source, target_is_directory=True)
        except OSError as exc:
            self.skipTest("directory symlinks unavailable: %s" % exc)
        original_lstat = Path.lstat

        def junction_lstat(path):
            if path == alias:
                # Windows junctions carry a reparse tag without S_IFLNK.
                return SimpleNamespace(
                    st_mode=stat.S_IFDIR,
                    st_file_attributes=1024,
                    st_reparse_tag=0xA0000003,
                )
            return original_lstat(path)

        with patch.object(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024, create=True):
            with patch.object(stat, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003, create=True):
                with patch.object(Path, "lstat", junction_lstat):
                    with patch.object(Path, "read_bytes") as read:
                        with self.assertRaisesRegex(ValueError, "unsafe symbolic link"):
                            load_gitignore(source, False, (str(alias / ".gitignore"),))
        read.assert_not_called()

    def test_case_variant_source_alias_checks_rule_links_before_reading(self):
        for kind in ("absolute-link", "escaping-link", "hardlink"):
            with self.subTest(kind=kind):
                root, source, _ = self._source()
                alias = source.with_name(source.name.upper())
                if not alias.exists() or not alias.samefile(source):
                    self.skipTest("filesystem does not provide this case-insensitive alias")
                (source / "nested").mkdir()
                (source / "nested" / ".gitignore").write_text("generated/\n", encoding="utf-8")
                matcher, labels, _, _ = load_gitignore(
                    source, False, (str(alias / "nested" / ".gitignore"),)
                )
                self.assertEqual(("nested/.gitignore",), labels)
                self.assertTrue(matcher.matches(Path("nested/generated"), is_directory=True))
                self.assertFalse(matcher.matches(Path("generated"), is_directory=True))
                self._unsafe_entry(source / ".gitignore", root, kind)
                with patch.object(Path, "read_bytes") as read:
                    with self.assertRaisesRegex(ValueError, "unsafe symbolic link|hard-linked"):
                        load_gitignore(source, False, (str(alias / ".gitignore"),))
                read.assert_not_called()

    def test_external_nonredirect_reparse_directory_still_loads_regular_rules(self):
        root, source, _ = self._source()
        external_directory = root / "external-config"
        external_directory.mkdir()
        external = external_directory / "rules.ignore"
        external.write_text("generated/\n", encoding="utf-8")
        original_lstat = Path.lstat

        def placeholder_lstat(path):
            if path == external_directory:
                return SimpleNamespace(
                    st_mode=stat.S_IFDIR,
                    st_file_attributes=1024,
                    st_reparse_tag=0x9000001A,
                )
            return original_lstat(path)

        with patch.object(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024, create=True):
            with patch.object(Path, "lstat", placeholder_lstat):
                with patch("repomin.gitignore.os.readlink") as readlink:
                    matcher, labels, _, _ = load_gitignore(source, False, (str(external),))
        readlink.assert_not_called()
        self.assertEqual((str(external),), labels)
        self.assertTrue(matcher.matches(Path("generated"), is_directory=True))

    @unittest.skipUnless(os.name == "nt", "native directory junctions require Windows")
    def test_windows_junction_alias_checks_internal_junction_before_reading(self):
        root, source, _ = self._source()
        alias = root / "source-alias"
        external_directory = root / "external-rules"
        external_directory.mkdir()
        (external_directory / ".gitignore").write_text("nested/\n", encoding="utf-8")
        for junction, target in (
            (alias, source),
            (source / "nested", external_directory),
        ):
            created = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(junction), str(target)],
                capture_output=True,
                text=True,
                check=False,
            )
            if created.returncode != 0:
                self.skipTest("directory junctions unavailable: %s" % created.stderr.strip())

        (source / "rules.ignore").write_text("generated/\n", encoding="utf-8")
        matcher, labels, _, _ = load_gitignore(source, False, (str(alias / "rules.ignore"),))
        self.assertEqual(("rules.ignore",), labels)
        self.assertTrue(matcher.matches(Path("generated"), is_directory=True))
        with patch.object(Path, "read_bytes") as read:
            with self.assertRaisesRegex(ValueError, "reparse point"):
                load_gitignore(source, False, (str(alias / "nested" / ".gitignore"),))
        read.assert_not_called()

    def test_rule_path_parent_segments_preserve_native_resolution(self):
        _, source, _ = self._source()
        (source / "real" / "nested").mkdir(parents=True)
        (source / "real" / "rules.ignore").write_text("correct/\n", encoding="utf-8")
        (source / "rules.ignore").write_text("wrong/\n", encoding="utf-8")
        try:
            (source / "alias").symlink_to("real/nested", target_is_directory=True)
        except OSError as exc:
            self.skipTest("symlinks unavailable: %s" % exc)
        # Win32 folds the parent component before following the link; POSIX
        # resolves it after the link. Preserve the loader's native Path.resolve
        # behavior, including rule scope and contents, on both platforms.
        resolved = (source / "alias/../rules.ignore").resolve()
        matcher, labels, _, _ = load_gitignore(source, False, ("alias/../rules.ignore",))
        self.assertEqual((resolved.relative_to(source).as_posix(),), labels)
        self.assertEqual(
            resolved == source / "real/rules.ignore",
            matcher.matches(Path("real/correct"), is_directory=True),
        )
        self.assertEqual(
            resolved == source / "rules.ignore",
            matcher.matches(Path("wrong"), is_directory=True),
        )

    def test_internal_rule_link_chain_rejects_absolute_hop_before_reading(self):
        _, source, _ = self._source()
        rules = source / "rules.ignore"
        rules.write_text("generated/\n", encoding="utf-8")
        try:
            (source / "alias").symlink_to(rules)
            (source / ".gitignore").symlink_to("alias")
        except OSError as exc:
            self.skipTest("symlinks unavailable: %s" % exc)
        with patch.object(Path, "read_bytes") as read:
            with self.assertRaisesRegex(ValueError, "unsafe symbolic link"):
                load_gitignore(source, True, ())
        read.assert_not_called()

    def test_recursive_rule_discovery_rejects_reparse_directory_before_recursion(self):
        _, source, _ = self._source()
        (source / ".gitignore").write_text("# root\n", encoding="utf-8")
        junction = source / "junction"
        junction.mkdir()
        original_lstat = Path.lstat

        def fake_lstat(path):
            if path == junction:
                return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=1024)
            return original_lstat(path)

        def fake_walk(*args, **kwargs):
            directories = ["junction"]
            yield str(source), directories, [".gitignore"]
            if "junction" in directories:
                self.fail("recursive discovery entered a reparse directory")

        with patch.object(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024, create=True):
            with patch.object(Path, "lstat", fake_lstat):
                with patch("repomin.gitignore.os.walk", fake_walk):
                    with self.assertRaisesRegex(ValueError, "reparse point"):
                        load_gitignore(source, True, (), recursive=True)


if __name__ == "__main__":
    unittest.main()
