#!/usr/bin/env python3
"""Inspect public release archives without extracting or searching for secrets.

This is a packaging guard, not proof that arbitrary coordinates are synthetic.
Explicitly allowed documentation media still requires a human/agent provenance
review. JSON profile checks inspect only its public-data contract.
"""

from __future__ import annotations

import argparse
import json
import re
import runpy
import stat
import sys
import tarfile
import zipfile
from pathlib import Path, PurePosixPath


PRIVATE_PARTS = {"my writtings", "output", "build", "private", "profiles"}
CACHE_PARTS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".cache",
               ".venv", "venv", "node_modules", ".npm-cache", ".git"}
MEDIA_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".heic", ".heif",
                  ".tif", ".tiff", ".bmp", ".avif", ".ico", ".pdf", ".svg"}
NESTED_ARCHIVES = {".zip", ".whl", ".tgz", ".gz", ".tar", ".7z"}
MAX_MEMBER_BYTES = 32 * 1024 * 1024


def _path_reason(name: str) -> str | None:
    if not name or "\x00" in name or "\\" in name or name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        return "unsafe archive path"
    parts = PurePosixPath(name).parts
    if ".." in parts:
        return "archive path traversal"
    lower = [part.casefold() for part in parts]
    if any(part in PRIVATE_PARTS for part in lower):
        return "private data directory"
    if any(part in CACHE_PARTS for part in lower):
        return "cache, environment, or repository history"
    basename = lower[-1] if lower else ""
    if basename in {".env", ".npmrc", ".pypirc"} or basename.startswith(".env."):
        return "local configuration must not be published"
    if basename == "personal_handwriting_skill_blueprint.md":
        return "private implementation blueprint"
    if basename == ".ds_store" or basename.endswith((".pyc", ".pyo")):
        return "generated cache file"
    if Path(basename).suffix in NESTED_ARCHIVES:
        return "nested archive is not an inspected public source asset"
    return None


def _asset_allowed(name: str, allowed: set[str]) -> bool:
    # tarballs/wheels may add a distribution prefix. Match the complete
    # requested documentation path, never only a basename or wildcard.
    parts = PurePosixPath(name).parts
    return any(tuple(parts[-len(PurePosixPath(asset).parts):]) == PurePosixPath(asset).parts for asset in allowed)


def _allowed_assets(values) -> set[str]:
    result = set()
    for value in values:
        parts = PurePosixPath(value).parts
        if _path_reason(value) or len(parts) < 3 or parts[:2] != ("docs", "assets") or Path(value).suffix.lower() not in MEDIA_SUFFIXES:
            raise ValueError("Allowed synthetic assets must be exact docs/assets/<file> media paths")
        result.add(PurePosixPath(value).as_posix())
    return result


def _profile_violations(value) -> list[str]:
    if not isinstance(value, dict) or "schema_version" not in value or "glyphs" not in value:
        return []
    reasons = []
    if value.get("synthetic") is not True:
        reasons.append("writer profile is not explicitly synthetic")
    if value.get("sources"):
        reasons.append("writer profile contains source evidence")
    glyphs = value.get("glyphs")
    if not isinstance(glyphs, list):
        reasons.append("writer profile glyphs must be a list")
    elif any(not isinstance(glyph, dict) or glyph.get("source_refs") for glyph in glyphs):
        reasons.append("writer profile glyphs contain source references")
    if value.get("observations"):
        reasons.append("writer profile contains observations")
    return reasons


def _entries(archive: Path):
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as handle:
            for member in handle.infolist():
                mode = member.external_attr >> 16
                link = stat.S_ISLNK(mode)
                yield member.filename, member.file_size, member.is_dir(), link, lambda m=member: handle.read(m)
    else:
        with tarfile.open(archive, "r:*") as handle:
            for member in handle:
                special = not (member.isfile() or member.isdir())
                def read(m=member):
                    stream = handle.extractfile(m)
                    if stream is None:
                        return b""
                    with stream:
                        return stream.read()
                yield member.name, member.size, member.isdir(), special, read


def inspect_archive(archive: Path, allowed_synthetic_assets=(), expected_synthetic_profile=None, expected_synthetic_varied_profile=None) -> dict:
    """Return counts and path-level violations; never extract an archive member."""
    archive = Path(archive)
    allowed = _allowed_assets(allowed_synthetic_assets)
    report = {"archive": archive.name, "ok": True, "files": 0, "profile_json": 0,
              "synthetic_assets": 0, "violations": []}
    seen = set()
    def reject(name, reason):
        report["violations"].append({"path": name, "reason": reason})
    try:
        for name, size, directory, special, read in _entries(archive):
            reason = _path_reason(name)
            if reason:
                reject(name, reason)
                continue  # In particular, do not read local configuration files.
            if special:
                reject(name, "links and special files are not public source entries")
                continue
            if directory:
                continue
            report["files"] += 1
            if name in seen:
                reject(name, "duplicate archive member")
                continue
            seen.add(name)
            if size > MAX_MEMBER_BYTES:
                reject(name, "entry exceeds the public source inspection size limit")
                continue
            suffix = PurePosixPath(name).suffix.lower()
            if suffix in MEDIA_SUFFIXES:
                if _asset_allowed(name, allowed):
                    report["synthetic_assets"] += 1
                else:
                    reject(name, "media must be explicitly reviewed and allowed as a synthetic documentation asset")
                continue
            data = read()
            # Reject undisclosed binary assets even when their extension hides
            # them. No credential patterns or secret values are inspected.
            if b"\x00" in data or data.startswith((b"\x89PNG", b"\xff\xd8\xff", b"GIF87a", b"GIF89a", b"%PDF-", b"RIFF")):
                reject(name, "binary/media content is not an allowed public source asset")
                continue
            try:
                text = data.decode("utf-8-sig")
            except UnicodeDecodeError:
                reject(name, "non-UTF-8 binary content is not an allowed public source asset")
                continue
            value = None
            if suffix == ".json" or text.lstrip().startswith("{"):
                try:
                    value = json.loads(text)
                except ValueError:
                    if suffix == ".json":
                        reject(name, "malformed JSON cannot be checked for private profile data")
                    continue
            if isinstance(value, dict) and "schema_version" in value and "glyphs" in value:
                report["profile_json"] += 1
                for reason in _profile_violations(value):
                    reject(name, reason)
                if PurePosixPath(name).name == "synthetic-profile.json" and expected_synthetic_profile is not None and value != expected_synthetic_profile:
                    reject(name, "bundled synthetic profile differs from its authored generator")
                if PurePosixPath(name).name == "synthetic-varied-profile.json" and expected_synthetic_varied_profile is not None and value != expected_synthetic_varied_profile:
                    reject(name, "bundled varied synthetic profile differs from its authored generator")
    except (OSError, tarfile.TarError, zipfile.BadZipFile, RuntimeError) as error:
        reject(archive.name, f"archive could not be inspected: {type(error).__name__}")
    report["ok"] = not report["violations"]
    return report


def check_source_fixture(repository: Path) -> tuple[dict | None, list[str]]:
    """Check only the trusted checkout generator, never execute archive content."""
    generator = repository / "scripts" / "make_synthetic_profile.py"
    fixture = repository / "examples" / "synthetic-profile.json"
    if not generator.is_file() or not fixture.is_file():
        return None, ["Authored synthetic generator and fixture must exist in the release checkout"]
    expected = runpy.run_path(str(generator))["build_profile"]()
    actual = json.loads(fixture.read_text(encoding="utf-8"))
    errors = _profile_violations(actual)
    if actual != expected:
        errors.append("Synthetic source fixture differs from its authored generator")
    if actual.get("license") != "MIT":
        errors.append("Synthetic source fixture must declare the project MIT license")
    return expected, errors


def check_varied_source_fixture(repository: Path) -> tuple[dict | None, list[str]]:
    generator = repository / "scripts" / "make_synthetic_profile.py"
    fixture = repository / "examples" / "synthetic-varied-profile.json"
    if not generator.is_file() or not fixture.is_file():
        return None, ["Authored varied generator and fixture must exist in the release checkout"]
    expected = runpy.run_path(str(generator))["build_varied_profile"]()
    actual = json.loads(fixture.read_text(encoding="utf-8"))
    errors = _profile_violations(actual)
    if actual != expected:
        errors.append("Varied synthetic source fixture differs from its authored generator")
    if actual.get("license") != "MIT":
        errors.append("Varied synthetic source fixture must declare the project MIT license")
    return expected, errors


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archives", type=Path, nargs="+", help="npm .tgz, Python .tar.gz/.whl, or skill .zip")
    parser.add_argument("--allow-synthetic-asset", action="append", default=[], metavar="docs/assets/FILE",
                        help="Exact documentation media path whose synthetic provenance was reviewed")
    args = parser.parse_args(argv)
    try:
        expected, errors = check_source_fixture(Path(__file__).resolve().parents[1])
        varied, varied_errors = check_varied_source_fixture(Path(__file__).resolve().parents[1])
        errors.extend(varied_errors)
        reports = [inspect_archive(path, args.allow_synthetic_asset, expected, varied) for path in args.archives]
    except (OSError, ValueError, KeyError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, indent=2))
        return 1
    ok = not errors and all(report["ok"] for report in reports)
    print(json.dumps({"ok": ok, "source_fixture_errors": errors, "archives": reports}, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
