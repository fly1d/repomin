import base64
import contextlib
import csv
from email.message import Message
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
import zipfile


_PATH = Path(__file__).resolve().parents[1] / "scripts" / "check_release_artifacts.py"
_SPEC = importlib.util.spec_from_file_location("repomin_release_artifact_check", _PATH)
if _SPEC is None or _SPEC.loader is None:
    raise ImportError("could not load release artifact check utility")
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)


def _package_metadata(name: str, version: str) -> bytes:
    message = Message()
    message["Metadata-Version"] = "2.4"
    message["Name"] = name
    message["Version"] = version
    return message.as_bytes() + b"\n"


def _wheel_metadata(*, purelib: str = "true", tag: str = "py3-none-any") -> bytes:
    message = Message()
    message["Wheel-Version"] = "1.0"
    message["Generator"] = "repomin-test"
    message["Root-Is-Purelib"] = purelib
    message["Tag"] = tag
    return message.as_bytes() + b"\n"


def _wheel_record(members) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    for name, data in members:
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
        writer.writerow((name, "sha256=" + digest.decode("ascii"), len(data)))
    writer.writerow((members[-1][0].rsplit("/", 1)[0] + "/RECORD", "", ""))
    return output.getvalue().encode("utf-8")


def _write_wheel(
    path: Path,
    version: str,
    *,
    name: str = "repomin",
    metadata_version: str = "",
    purelib: str = "true",
    tag: str = "py3-none-any",
) -> None:
    dist_info = "repomin-%s.dist-info" % version
    members = (
        ("repomin/__init__.py", b""),
        (
            dist_info + "/METADATA",
            _package_metadata(name, metadata_version or version),
        ),
        (
            dist_info + "/WHEEL",
            _wheel_metadata(purelib=purelib, tag=tag),
        ),
    )
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for member, data in members:
            archive.writestr(member, data)
        archive.writestr(dist_info + "/RECORD", _wheel_record(members))


def _add_tar_bytes(archive: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    archive.addfile(info, io.BytesIO(data))


def _write_sdist(
    path: Path,
    version: str,
    *,
    name: str = "repomin",
    metadata_version: str = "",
    root: str = "",
) -> None:
    top_level = root or "repomin-%s" % version
    with tarfile.open(path, "w:gz") as archive:
        _add_tar_bytes(
            archive,
            top_level + "/PKG-INFO",
            _package_metadata(name, metadata_version or version),
        )
        _add_tar_bytes(archive, top_level + "/README.md", b"# ReproMin\n")


class ReleaseArtifactCheckTest(unittest.TestCase):
    def _paths(self, root: Path, version: str):
        return (
            root / ("repomin-%s-py3-none-any.whl" % version),
            root / ("repomin-%s.tar.gz" % version),
        )

    def test_cli_validates_archives_and_emits_stable_json_with_hashes(self) -> None:
        version = "0.1.0.dev10"
        tag = "v" + version
        with tempfile.TemporaryDirectory() as directory:
            wheel, sdist = self._paths(Path(directory), version)
            _write_wheel(wheel, version)
            _write_sdist(sdist, version)
            output = io.StringIO()
            errors = io.StringIO()

            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                status = _MODULE.main(["--tag", tag, str(wheel), str(sdist)])

            expected = {
                "artifacts": {
                    "sdist": {
                        "filename": sdist.name,
                        "sha256": hashlib.sha256(sdist.read_bytes()).hexdigest(),
                    },
                    "wheel": {
                        "filename": wheel.name,
                        "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
                    },
                },
                "name": "repomin",
                "schema_version": 1,
                "tag": tag,
                "valid": True,
                "version": version,
            }
            self.assertEqual(0, status)
            self.assertEqual("", errors.getvalue())
            self.assertEqual(json.dumps(expected, sort_keys=True) + "\n", output.getvalue())

    def test_filenames_must_match_tag_version(self) -> None:
        version = "1.2.3"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wheel, sdist = self._paths(root, version)
            _write_wheel(wheel, version)
            _write_sdist(sdist, version)

            for supplied, expected_message in (
                (
                    (root / "other.whl", sdist),
                    "wheel filename must be repomin-1.2.3-py3-none-any.whl",
                ),
                (
                    (wheel, root / "other.tar.gz"),
                    "source distribution filename must be repomin-1.2.3.tar.gz",
                ),
            ):
                with self.subTest(path=supplied):
                    with self.assertRaisesRegex(
                        _MODULE.ArtifactValidationError, expected_message
                    ):
                        _MODULE.validate_release_artifacts(
                            "v" + version, supplied[0], supplied[1]
                        )

    def test_package_name_and_version_must_match(self) -> None:
        version = "1.2.3"
        cases = (
            ("wheel", {"name": "other"}, "wheel METADATA Name must be repomin"),
            (
                "wheel",
                {"metadata_version": "1.2.4"},
                "wheel METADATA Version must be 1.2.3",
            ),
            (
                "sdist",
                {"name": "other"},
                "source distribution PKG-INFO Name must be repomin",
            ),
            (
                "sdist",
                {"metadata_version": "1.2.4"},
                "source distribution PKG-INFO Version must be 1.2.3",
            ),
        )
        for artifact, overrides, expected_message in cases:
            with self.subTest(artifact=artifact, overrides=overrides):
                with tempfile.TemporaryDirectory() as directory:
                    wheel, sdist = self._paths(Path(directory), version)
                    _write_wheel(
                        wheel, version, **(overrides if artifact == "wheel" else {})
                    )
                    _write_sdist(
                        sdist, version, **(overrides if artifact == "sdist" else {})
                    )
                    with self.assertRaisesRegex(
                        _MODULE.ArtifactValidationError, expected_message
                    ):
                        _MODULE.validate_release_artifacts(
                            "v" + version, wheel, sdist
                        )

    def test_wheel_must_be_pure_python_with_one_expected_tag(self) -> None:
        version = "1.2.3"
        cases = (
            ({"purelib": "false"}, "Root-Is-Purelib must be true"),
            ({"tag": "cp39-cp39-manylinux_2_17_x86_64"}, "Tag must be py3-none-any"),
        )
        for overrides, expected_message in cases:
            with self.subTest(overrides=overrides):
                with tempfile.TemporaryDirectory() as directory:
                    wheel, sdist = self._paths(Path(directory), version)
                    _write_wheel(wheel, version, **overrides)
                    _write_sdist(sdist, version)
                    with self.assertRaisesRegex(
                        _MODULE.ArtifactValidationError, expected_message
                    ):
                        _MODULE.validate_release_artifacts(
                            "v" + version, wheel, sdist
                        )

    def test_sdist_top_level_directory_must_match_name_and_version(self) -> None:
        version = "1.2.3"
        with tempfile.TemporaryDirectory() as directory:
            wheel, sdist = self._paths(Path(directory), version)
            _write_wheel(wheel, version)
            _write_sdist(sdist, version, root="unexpected-root")

            with self.assertRaisesRegex(
                _MODULE.ArtifactValidationError,
                "top-level directory must be repomin-1.2.3",
            ):
                _MODULE.validate_release_artifacts("v" + version, wheel, sdist)

    def test_corrupt_archives_fail_with_clear_artifact_context(self) -> None:
        version = "1.2.3"
        cases = (
            ("wheel", "wheel archive is invalid"),
            ("sdist", "source distribution archive is invalid"),
        )
        for artifact, expected_message in cases:
            with self.subTest(artifact=artifact):
                with tempfile.TemporaryDirectory() as directory:
                    wheel, sdist = self._paths(Path(directory), version)
                    _write_wheel(wheel, version)
                    _write_sdist(sdist, version)
                    (wheel if artifact == "wheel" else sdist).write_bytes(
                        b"not an archive"
                    )
                    errors = io.StringIO()

                    with contextlib.redirect_stderr(errors):
                        status = _MODULE.main(
                            ["--tag", "v" + version, str(wheel), str(sdist)]
                        )

                    self.assertEqual(1, status)
                    self.assertIn(expected_message, errors.getvalue())

    def test_invalid_tag_fails_before_reading_artifacts(self) -> None:
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            status = _MODULE.main(
                ["--tag", "1.2.3", "missing.whl", "missing.tar.gz"]
            )

        self.assertEqual(1, status)
        self.assertIn("tag must match vX.Y.Z", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
