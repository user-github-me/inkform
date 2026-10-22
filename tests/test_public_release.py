"""Release archive privacy checks use invented contents and never extract them."""

import importlib.util
import io
import json
import stat
import tarfile
import zipfile
from pathlib import Path

import pytest


SPEC = importlib.util.spec_from_file_location("release_guard", Path(__file__).resolve().parents[1] / "scripts/check_public_release.py")
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)


def archive(path, entries):
    if path.suffix in {".zip", ".whl"}:
        with zipfile.ZipFile(path, "w") as handle:
            for name, content in entries.items():
                handle.writestr(name, content)
    else:
        with tarfile.open(path, "w:gz") as handle:
            for name, content in entries.items():
                content = content.encode() if isinstance(content, str) else content
                info = tarfile.TarInfo(name); info.size = len(content)
                handle.addfile(info, io.BytesIO(content))
    return path


def profile(**changes):
    value = {"schema_version": "1.0", "synthetic": True, "license": "MIT", "sources": [],
             "glyphs": [{"source_refs": [], "strokes": []}]}
    value.update(changes)
    return json.dumps(value)


@pytest.mark.parametrize("name", ["release.tgz", "release.tar.gz", "release.whl", "release.zip"])
def test_public_code_docs_and_synthetic_profile_pass_without_extraction(tmp_path, name):
    path = archive(tmp_path/name, {"package/src/module.py": "# source_refs in code are legitimate\n",
        "package/README.md": "Public documentation", "package/examples/demo.json": profile(),
        "package/schema.json": '{"$schema":"https://json-schema.org/draft/2020-12/schema","properties":{"glyphs":{}}}'})
    report = guard.inspect_archive(path)
    assert report["ok"]
    assert report["files"] == 4
    assert report["profile_json"] == 1
    assert sorted(p.name for p in tmp_path.iterdir()) == [name]


@pytest.mark.parametrize("name", ["package/my writtings/photo.txt", "package/output/page.json", "package/build/result.txt",
    "package/private/profile.json", "package/profiles/profile.json", "package/__pycache__/code.pyc", "package/.env",
    "package/.env.local", "package/.npmrc", "package/PERSONAL_HANDWRITING_SKILL_BLUEPRINT.md", "../outside.txt"])
def test_private_names_and_unsafe_paths_rejected(tmp_path, name):
    path = archive(tmp_path/"release.tgz", {name: "Never needs to be read"})
    result = guard.inspect_archive(path)
    assert not result["ok"]
    assert result["violations"][0]["path"] == name


@pytest.mark.parametrize("changes", [{"synthetic": False}, {"sources": [{"source_id": "personal"}]},
    {"glyphs": [{"source_refs": [{"source_id": "personal"}]}]}, {"observations": [{"observation_id": "personal"}]}])
def test_real_profile_data_rejected_even_with_a_public_filename(tmp_path, changes):
    path = archive(tmp_path/"release.whl", {"inkform/data/example.json": profile(**changes)})
    report = guard.inspect_archive(path)
    assert not report["ok"]
    assert report["profile_json"] == 1


def test_media_requires_exact_explicit_synthetic_docs_allowance(tmp_path):
    path = archive(tmp_path/"release.zip", {"package/docs/assets/demo.png": b"\x89PNG\r\n\x1a\n"})
    assert not guard.inspect_archive(path)["ok"]
    report = guard.inspect_archive(path, ["docs/assets/demo.png"])
    assert report["ok"] and report["synthetic_assets"] == 1
    assert not guard.inspect_archive(path, ["docs/assets/another.png"])["ok"]
    with pytest.raises(ValueError): guard.inspect_archive(path, ["demo.png"])


def test_disguised_binary_and_nested_archives_rejected(tmp_path):
    path = archive(tmp_path/"release.tgz", {"package/notes.txt": b"\x89PNG\r\n\x1a\n", "package/evidence.zip": b"not inspected"})
    report = guard.inspect_archive(path)
    assert not report["ok"] and len(report["violations"]) == 2


def test_tar_and_zip_links_rejected(tmp_path):
    tar = tmp_path/"release.tgz"
    with tarfile.open(tar, "w:gz") as handle:
        info = tarfile.TarInfo("package/private-link"); info.type = tarfile.SYMTYPE; info.linkname = "/elsewhere"
        handle.addfile(info)
    assert not guard.inspect_archive(tar)["ok"]
    zipped = tmp_path/"release.zip"
    with zipfile.ZipFile(zipped, "w") as handle:
        info = zipfile.ZipInfo("link"); info.create_system = 3; info.external_attr = (stat.S_IFLNK | 0o777) << 16
        handle.writestr(info, "/elsewhere")
    assert not guard.inspect_archive(zipped)["ok"]


def test_bundled_fixture_must_match_authoritative_generator(tmp_path):
    expected = json.loads(profile())
    path = archive(tmp_path/"release.whl", {"examples/synthetic-profile.json": profile(glyphs=[])})
    assert not guard.inspect_archive(path, expected_synthetic_profile=expected)["ok"]
    _, errors = guard.check_source_fixture(Path(__file__).resolve().parents[1])
    assert errors == []


def test_invalid_archive_returns_failure_without_traceback(tmp_path):
    path = tmp_path/"broken.tgz"; path.write_text("broken")
    assert not guard.inspect_archive(path)["ok"]


def test_varied_public_fixture_matches_its_authored_generator(tmp_path):
    repository = Path(__file__).resolve().parents[1]
    expected, errors = guard.check_varied_source_fixture(repository)
    assert errors == [] and expected["synthetic"] is True
    path = archive(tmp_path/"varied.whl", {"examples/synthetic-varied-profile.json": profile(glyphs=[])})
    assert not guard.inspect_archive(path, expected_synthetic_varied_profile=expected)["ok"]
