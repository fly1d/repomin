#!/usr/bin/env python3
"""Validate ReproMin release archives before they are published.

The checker intentionally uses only the Python standard library. It reads the
wheel and source distribution without extracting them, verifies their public
packaging identity, and emits deterministic JSON containing their SHA-256
digests.
"""

from __future__ import annotations

import argparse
from email.message import Message
from email.parser import BytesParser
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sys
import tarfile
from typing import Dict, List, Optional, Sequence, Tuple
import zipfile
import zlib


_PROJECT_NAME = "repomin"
_WHEEL_TAG = "py3-none-any"
_TAG_PATTERN = re.compile(
    r"^v(?P<version>[0-9]+\.[0-9]+\.[0-9]+"
    r"(?:(?:a|b|rc)[0-9]+)?(?:\.post[0-9]+)?(?:\.dev[0-9]+)?)$"
)
_HASH_CHUNK_SIZE = 1024 * 1024


class ArtifactValidationError(ValueError):
    """A release artifact does not match the expected public contract."""


def _version_from_tag(tag: str) -> str:
    match = _TAG_PATTERN.fullmatch(tag)
    if match is None:
        raise ArtifactValidationError(
            "tag must match vX.Y.Z (development tags such as v1.2.3.dev4 "
            "are also supported): %s" % tag
        )
    return match.group("version")


def _require_filename(path: Path, expected: str, kind: str) -> None:
    if path.name != expected:
        raise ArtifactValidationError(
            "%s filename must be %s (found %s)" % (kind, expected, path.name)
        )
    if not path.is_file():
        raise ArtifactValidationError("%s file does not exist: %s" % (kind, path))


def _read_unique_zip_member(
    archive: zipfile.ZipFile, member: str, kind: str
) -> bytes:
    matches = [info for info in archive.infolist() if info.filename == member]
    if len(matches) != 1:
        raise ArtifactValidationError(
            "%s must contain exactly one %s (found %d)"
            % (kind, member, len(matches))
        )
    if matches[0].is_dir():
        raise ArtifactValidationError("%s member is not a file: %s" % (kind, member))
    return archive.read(matches[0])


def _metadata_message(data: bytes, source: str) -> Message:
    try:
        return BytesParser().parsebytes(data, headersonly=True)
    except (TypeError, ValueError) as exc:
        raise ArtifactValidationError(
            "%s could not be parsed as package metadata: %s" % (source, exc)
        ) from exc


def _single_header(message: Message, header: str, source: str) -> str:
    values = message.get_all(header, [])
    if len(values) != 1:
        raise ArtifactValidationError(
            "%s must contain exactly one %s header (found %d)"
            % (source, header, len(values))
        )
    return values[0].strip()


def _check_package_metadata(data: bytes, source: str, version: str) -> None:
    metadata = _metadata_message(data, source)
    name = _single_header(metadata, "Name", source)
    if name != _PROJECT_NAME:
        raise ArtifactValidationError(
            "%s Name must be %s (found %s)" % (source, _PROJECT_NAME, name)
        )
    actual_version = _single_header(metadata, "Version", source)
    if actual_version != version:
        raise ArtifactValidationError(
            "%s Version must be %s (found %s)"
            % (source, version, actual_version)
        )


def _check_wheel_metadata(data: bytes, source: str) -> None:
    metadata = _metadata_message(data, source)
    purelib = _single_header(metadata, "Root-Is-Purelib", source)
    if purelib.lower() != "true":
        raise ArtifactValidationError(
            "%s Root-Is-Purelib must be true (found %s)" % (source, purelib)
        )
    tags = tuple(value.strip() for value in metadata.get_all("Tag", []))
    if tags != (_WHEEL_TAG,):
        rendered = ", ".join(tags) if tags else "none"
        raise ArtifactValidationError(
            "%s Tag must be %s (found %s)" % (source, _WHEEL_TAG, rendered)
        )


def _validate_wheel(path: Path, version: str) -> None:
    dist_info = "%s-%s.dist-info" % (_PROJECT_NAME, version)
    metadata_member = dist_info + "/METADATA"
    wheel_member = dist_info + "/WHEEL"
    try:
        with zipfile.ZipFile(path, "r") as archive:
            bad_member = archive.testzip()
            if bad_member is not None:
                raise ArtifactValidationError(
                    "wheel archive failed its CRC check at %s" % bad_member
                )
            package_metadata = _read_unique_zip_member(
                archive, metadata_member, "wheel archive"
            )
            wheel_metadata = _read_unique_zip_member(
                archive, wheel_member, "wheel archive"
            )
    except ArtifactValidationError:
        raise
    except (EOFError, OSError, RuntimeError, zipfile.BadZipFile, zlib.error) as exc:
        raise ArtifactValidationError("wheel archive is invalid: %s" % exc) from exc

    _check_package_metadata(package_metadata, "wheel METADATA", version)
    _check_wheel_metadata(wheel_metadata, "wheel WHEEL")


def _tar_member_parts(name: str) -> Tuple[str, ...]:
    path = PurePosixPath(name)
    if path.is_absolute() or not path.parts or any(
        part in {"", ".", ".."} for part in path.parts
    ):
        raise ArtifactValidationError(
            "source distribution contains an unsafe member path: %s" % name
        )
    return path.parts


def _read_sdist_members(
    archive: tarfile.TarFile, expected_root: str, metadata_member: str
) -> bytes:
    members = archive.getmembers()
    if not members:
        raise ArtifactValidationError("source distribution archive is empty")

    metadata_matches: List[tarfile.TarInfo] = []
    for member in members:
        parts = _tar_member_parts(member.name)
        if parts[0] != expected_root:
            raise ArtifactValidationError(
                "source distribution top-level directory must be %s (found %s)"
                % (expected_root, parts[0])
            )
        if member.name == metadata_member:
            metadata_matches.append(member)

    if len(metadata_matches) != 1:
        raise ArtifactValidationError(
            "source distribution must contain exactly one %s (found %d)"
            % (metadata_member, len(metadata_matches))
        )
    if not metadata_matches[0].isfile():
        raise ArtifactValidationError(
            "source distribution member is not a file: %s" % metadata_member
        )

    metadata_file = archive.extractfile(metadata_matches[0])
    if metadata_file is None:
        raise ArtifactValidationError(
            "source distribution could not read %s" % metadata_member
        )
    package_metadata = metadata_file.read()

    # Reading every regular member catches truncated payloads, not only broken
    # archive headers, without extracting untrusted paths to disk.
    for member in members:
        if not member.isfile() or member is metadata_matches[0]:
            continue
        extracted = archive.extractfile(member)
        if extracted is None:
            raise ArtifactValidationError(
                "source distribution could not read %s" % member.name
            )
        while extracted.read(_HASH_CHUNK_SIZE):
            pass
    return package_metadata


def _validate_sdist(path: Path, version: str) -> None:
    expected_root = "%s-%s" % (_PROJECT_NAME, version)
    metadata_member = expected_root + "/PKG-INFO"
    try:
        with tarfile.open(path, "r:gz") as archive:
            package_metadata = _read_sdist_members(
                archive, expected_root, metadata_member
            )
    except ArtifactValidationError:
        raise
    except (EOFError, OSError, tarfile.TarError) as exc:
        raise ArtifactValidationError(
            "source distribution archive is invalid: %s" % exc
        ) from exc

    _check_package_metadata(package_metadata, "source distribution PKG-INFO", version)


def _sha256(path: Path, kind: str) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as artifact:
            while True:
                chunk = artifact.read(_HASH_CHUNK_SIZE)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError as exc:
        raise ArtifactValidationError(
            "could not hash %s file %s: %s" % (kind, path, exc)
        ) from exc
    return digest.hexdigest()


def validate_release_artifacts(
    tag: str, wheel: Path, sdist: Path
) -> Dict[str, object]:
    """Validate two release artifacts and return their stable summary."""

    version = _version_from_tag(tag)
    wheel = Path(wheel)
    sdist = Path(sdist)
    expected_wheel = "%s-%s-%s.whl" % (_PROJECT_NAME, version, _WHEEL_TAG)
    expected_sdist = "%s-%s.tar.gz" % (_PROJECT_NAME, version)
    _require_filename(wheel, expected_wheel, "wheel")
    _require_filename(sdist, expected_sdist, "source distribution")

    _validate_wheel(wheel, version)
    _validate_sdist(sdist, version)
    return {
        "artifacts": {
            "sdist": {
                "filename": sdist.name,
                "sha256": _sha256(sdist, "source distribution"),
            },
            "wheel": {
                "filename": wheel.name,
                "sha256": _sha256(wheel, "wheel"),
            },
        },
        "name": _PROJECT_NAME,
        "schema_version": 1,
        "tag": tag,
        "valid": True,
        "version": version,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate ReproMin wheel and source release artifacts."
    )
    parser.add_argument(
        "--tag",
        required=True,
        help="release tag, for example v1.2.3 or v1.2.3.dev4",
    )
    parser.add_argument("wheel", type=Path, help="path to the release wheel")
    parser.add_argument(
        "sdist", type=Path, help="path to the release source distribution"
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = validate_release_artifacts(args.tag, args.wheel, args.sdist)
    except ArtifactValidationError as exc:
        print("Release artifact check failed: %s" % exc, file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
