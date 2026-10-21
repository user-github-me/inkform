"""Synthetic profile fixtures only: no personal handwriting or identifiers."""

import copy
import json
import os
import stat

import pytest

from inkform.errors import InkformError
from inkform.profiles import (
    accepted_glyphs,
    coverage,
    default_data_dir,
    geometry_bounds,
    load_profile,
    merge_profile,
    resolve_render_mode,
    save_profile,
    validate_profile,
)


@pytest.fixture
def profile():
    return {
        "schema_version": "1.0",
        "profile_id": "synthetic-test",
        "profile_version": "0.1.0",
        "scripts": ["Latin"],
        "synthetic": True,
        "physical_scale": {"body_x_height_mm": 2.8, "source": "synthetic"},
        "pen": {"width_mm": 0.3, "color": "#172238"},
        "metrics": {"word_gap": 0.7, "line_height": 2.5},
        "dynamics": {"stroke_order": "estimated", "timing": "unknown", "pressure": "unknown"},
        "calibration_status": "unreviewed",
        "sources": [],
        "glyphs": [{
            "glyph_id": "synthetic.text.x.1",
            "text": "x",
            "role": "text",
            "representation": "centerline",
            "advance": 1.1,
            "ink_bounds": [-0.1, -1.1, 1.1, 0.1],
            "strokes": [
                {"start": [0, -1], "segments": [{"type": "line", "end": [1, 0]}], "width_profile": [{"s": 0, "multiplier": 0.9}, {"s": 1, "multiplier": 1.1}]},
                {"start": [1, -1], "segments": [{"type": "line", "end": [0, 0]}]},
            ],
            "source_refs": [],
            "label_status": "reviewed",
            "geometry_status": "reviewed",
            "reviewed_by": "synthetic-fixture",
            "stroke_order_status": "estimated",
        }],
    }


def test_immutable_snapshot_and_current_pointer(profile, tmp_path):
    first = save_profile(profile, tmp_path)
    original_bytes = first.read_bytes()
    assert save_profile(profile, tmp_path) == first
    changed = copy.deepcopy(profile)
    changed["pen"]["color"] = "#000000"
    with pytest.raises(InkformError) as error:
        save_profile(changed, tmp_path)
    assert error.value.code == "profile_version_exists"
    changed["profile_version"] = "0.1.1"
    second = save_profile(changed, tmp_path)
    assert first.read_bytes() == original_bytes
    assert load_profile("synthetic-test", tmp_path) == changed
    assert load_profile(second.parent) == changed
    assert load_profile(first) == profile
    assert load_profile(first.parent.parent) == changed
    if os.name == "posix":
        assert stat.S_IMODE(first.stat().st_mode) == 0o600
        assert stat.S_IMODE(first.parent.stat().st_mode) == 0o700


def test_proposed_forms_do_not_cover_and_roles_never_fall_back(profile):
    candidate = copy.deepcopy(profile["glyphs"][0])
    candidate.update(glyph_id="synthetic.math.x.1", role="math.variable", geometry_status="proposed")
    profile["glyphs"].append(candidate)
    validate_profile(profile)
    result = coverage(profile, [("text", "x"), ("math.variable", "x"), ("math.variable", "x")])
    assert result["required_count"] == 2
    assert result["covered_count"] == 1
    assert result["missing_glyphs"] == [{"role": "math.variable", "text": "x", "count": 2}]
    assert [g["glyph_id"] for g in accepted_glyphs(profile)] == ["synthetic.text.x.1"]
    candidate["geometry_status"] = "reviewed"
    assert coverage(profile, [("math.variable", "x")])["complete"]


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_values_rejected_even_in_metadata(profile, value):
    profile["observation_quality"] = value
    with pytest.raises(InkformError, match="finite"):
        validate_profile(profile)


@pytest.mark.parametrize("profile_id", ["../outside", "x/y", "x\\y", ".", "..", "", "x\n"])
def test_unsafe_profile_ids_rejected(profile, profile_id):
    profile["profile_id"] = profile_id
    with pytest.raises(InkformError):
        validate_profile(profile)


@pytest.mark.parametrize("path", ["../source.png", "/private/source.png", "C:\\private\\source.png", "C:source.png", "a/../../source.png"])
def test_unsafe_owned_asset_paths_rejected(profile, path):
    profile["calibration_file"] = path
    with pytest.raises(InkformError, match="relative paths"):
        validate_profile(profile)


def test_provenance_paths_are_metadata_and_never_loaded(profile):
    profile["synthetic"] = False
    profile["sources"] = [{"source_id": "source-1", "sha256": "a" * 64, "source_path": "/does/not/exist/source.png", "original_path": "/does/not/exist/original.png"}]
    profile["glyphs"][0]["source_refs"] = [{"source_id": "source-1", "observation_id": "observation-1"}]
    assert validate_profile(profile) is profile


def test_missing_or_unknown_provenance_rejected(profile):
    profile["synthetic"] = False
    with pytest.raises(InkformError, match="provenance"):
        validate_profile(profile)
    profile["glyphs"][0]["source_refs"] = [{"source_id": "missing-source"}]
    with pytest.raises(InkformError, match="stored source"):
        validate_profile(profile)


def test_review_actor_required(profile):
    profile["glyphs"][0]["reviewed_by"] = None
    with pytest.raises(InkformError, match="review actor"):
        validate_profile(profile)


def test_duplicate_glyph_ids_rejected(profile):
    profile["glyphs"].append(copy.deepcopy(profile["glyphs"][0]))
    with pytest.raises(InkformError, match="unique"):
        validate_profile(profile)


def test_outline_must_be_closed(profile):
    profile["glyphs"][0]["representation"] = "clean_outline"
    with pytest.raises(InkformError):
        validate_profile(profile)
    for stroke in profile["glyphs"][0]["strokes"]:
        stroke["closed"] = True
    validate_profile(profile)


def test_bounds_enclose_curve_extrema_but_need_not_enclose_control_points(profile):
    stroke = {"start": [0, 0], "segments": [{"type": "cubic", "control1": [0, -2], "control2": [1, -2], "end": [1, 0]}]}
    profile["glyphs"][0].update(strokes=[stroke], ink_bounds=[0, -1.5, 1, 0])
    assert geometry_bounds([stroke]) == pytest.approx([0, -1.5, 1, 0])
    validate_profile(profile)
    profile["glyphs"][0]["ink_bounds"][1] = -1.4
    with pytest.raises(InkformError, match="enclose"):
        validate_profile(profile)


def test_width_profile_order_required(profile):
    profile["glyphs"][0]["strokes"][0]["width_profile"] = [{"s": 1, "multiplier": 1}, {"s": 0, "multiplier": 1}]
    with pytest.raises(InkformError, match="strictly increasing"):
        validate_profile(profile)


def test_raster_shape_rejected(profile):
    profile["glyphs"][0]["image_data"] = "data:image/png;base64,unused"
    with pytest.raises(InkformError, match="vector paths"):
        validate_profile(profile)


def test_update_preserves_unrelated_records_and_inputs(profile, tmp_path):
    profile["sources"] = [{"source_id": "evidence-a", "sha256": "a" * 64}]
    old = copy.deepcopy(profile)
    added = copy.deepcopy(profile["glyphs"][0])
    added.update(glyph_id="synthetic.math.x.1", role="math.variable")
    update = {"glyphs": [added], "sources": [{"source_id": "evidence-b", "sha256": "b" * 64}], "metrics": {"word_gap": 0.9}}
    result = merge_profile(profile, update)
    assert profile == old
    assert result["profile_version"] == "0.1.1"
    assert result["glyphs"][0] == profile["glyphs"][0]
    assert result["sources"][0] == profile["sources"][0]
    assert result["metrics"] == {"word_gap": 0.9, "line_height": 2.5}
    save_profile(profile, tmp_path)
    save_profile(result, tmp_path)
    assert len(load_profile("synthetic-test", tmp_path)["glyphs"]) == 2


def test_update_cannot_reassign_source_evidence(profile):
    profile["sources"] = [{"source_id": "evidence-a", "sha256": "a" * 64}]
    with pytest.raises(InkformError) as error:
        merge_profile(profile, {"sources": [{"source_id": "evidence-a", "sha256": "b" * 64}]})
    assert error.value.code == "profile_source_conflict"


def test_update_preserves_observations_and_their_sources(profile):
    profile["sources"] = [{"source_id": "evidence-a", "sha256": "a" * 64}]
    profile["observations"] = [{"observation_id": "observation-a", "source_id": "evidence-a", "mask_sha256": "b" * 64}]
    result = merge_profile(profile, {"observations": [{"observation_id": "observation-b", "source_id": "evidence-a", "mask_sha256": "c" * 64}]})
    assert result["observations"][0] == profile["observations"][0]
    assert len(result["observations"]) == 2
    with pytest.raises(InkformError) as error:
        merge_profile(profile, {"observations": [{"observation_id": "observation-a", "mask_sha256": "d" * 64}]})
    assert error.value.code == "profile_source_conflict"


def test_glyph_cannot_reference_an_observation_from_another_source(profile):
    profile["sources"] = [{"source_id": "evidence-a"}, {"source_id": "evidence-b"}]
    profile["observations"] = [{"observation_id": "observation-a", "source_id": "evidence-a"}]
    profile["glyphs"][0]["source_refs"] = [{"source_id": "evidence-b", "observation_id": "observation-a"}]
    with pytest.raises(InkformError, match="same source"):
        validate_profile(profile)


def test_pointer_cannot_escape_private_profile_directory(profile, tmp_path):
    snapshot = save_profile(profile, tmp_path)
    pointer = snapshot.parent.parent / "current.json"
    pointer.write_text(json.dumps({"profile_id": profile["profile_id"], "profile_version": "0.1.0", "snapshot": "../../outside.json"}))
    with pytest.raises(InkformError) as error:
        load_profile("synthetic-test", tmp_path)
    assert error.value.code == "invalid_profile_pointer"


def test_symlink_snapshot_directory_rejected(profile, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    parent = tmp_path / "profiles"
    parent.mkdir()
    (parent / profile["profile_id"]).symlink_to(outside, target_is_directory=True)
    with pytest.raises(InkformError, match="symbolic links"):
        save_profile(profile, tmp_path)
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("content", ['{"profile_id":"a","profile_id":"b"}', '{"value":NaN}'])
def test_invalid_json_rejected(tmp_path, content):
    path = tmp_path / "profile.json"
    path.write_text(content)
    with pytest.raises(InkformError) as error:
        load_profile(path)
    assert error.value.code == "invalid_profile_json"


def test_data_directory_override(monkeypatch, tmp_path):
    monkeypatch.setenv("INKFORM_DATA_DIR", str(tmp_path / "private"))
    assert default_data_dir() == tmp_path / "private"


def test_profile_render_preference_persists_without_changing_geometry(profile, tmp_path):
    original = copy.deepcopy(profile)
    original_path = save_profile(profile, tmp_path)
    original_bytes = original_path.read_bytes()
    preferred = merge_profile(profile, {"default_render_mode": "clean_outline"})
    save_profile(preferred, tmp_path)
    reloaded = load_profile(profile["profile_id"], tmp_path)
    assert reloaded["default_render_mode"] == "clean_outline"
    assert resolve_render_mode(reloaded) == "clean_outline"
    assert resolve_render_mode(reloaded, "fresh_strokes") == "fresh_strokes"
    assert reloaded["glyphs"] == original["glyphs"]
    assert profile == original
    assert original_path.read_bytes() == original_bytes
    assert resolve_render_mode(load_profile(original_path)) == "fresh_strokes"


def test_render_mode_defaults_and_explicit_selection(profile):
    assert resolve_render_mode(profile) == "fresh_strokes"
    assert resolve_render_mode(profile, "clean_outline") == "clean_outline"
    profile["default_render_mode"] = "fresh_strokes"
    assert resolve_render_mode(profile, "clean_outline") == "clean_outline"


@pytest.mark.parametrize("mode", ["profile", "automatic", "", None, []])
def test_invalid_profile_render_preference_is_rejected(profile, mode):
    profile["default_render_mode"] = mode
    with pytest.raises(InkformError) as error:
        validate_profile(profile)
    assert error.value.code == "invalid_profile"


@pytest.mark.parametrize("mode", ["profile", "automatic", "", []])
def test_invalid_explicit_render_mode_has_actionable_error(profile, mode):
    with pytest.raises(InkformError) as error:
        resolve_render_mode(profile, mode)
    assert error.value.code == "invalid_mode"
