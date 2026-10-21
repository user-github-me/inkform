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


@pytest.mark.parametrize("amount", [-.1, .31, float("nan"), float("inf"), "0.1", True])
def test_variation_amount_requires_a_bounded_finite_number(profile, amount):
    profile["variation"] = {"amount": amount}
    with pytest.raises(InkformError) as error:
        validate_profile(profile)
    assert error.value.code == "invalid_profile"


def test_synthetic_profile_can_author_demo_rate_but_cannot_claim_personal_learning(profile):
    profile["corrections"] = {"rate": .1, "basis": "synthetic_demo", "sampled_word_count": 100,
        "correction_count": 10, "observation_ids": [], "source_refs": []}
    validate_profile(profile)
    profile["corrections"]["basis"] = "reviewed_samples"
    with pytest.raises(InkformError):
        validate_profile(profile)
    profile.pop("corrections")
    profile["variation"] = {"learned": {"version": "inkform-variation-1", "basis": "reviewed_samples",
        "by_glyph": [], "word_gap": 0, "word_gap_sample_count": 0, "word_gap_observation_ids": []}}
    with pytest.raises(InkformError):
        validate_profile(profile)


def personal_frequency_profile(profile):
    profile["synthetic"] = False
    profile["sources"] = [{"source_id": "page", "sha256": "a"*64}]
    profile["glyphs"][0]["source_refs"] = [{"source_id": "page"}]
    profile["observations"] = [{"observation_id": "page-count", "source_id": "page", "source_sha256": "a"*64,
        "kind": "correction_frequency", "page_index": 0, "scope": "page", "representative": True,
        "word_count": 100, "correction_count": 7, "review_status": "reviewed", "reviewed_by": "fixture-author"}]
    profile["corrections"] = {"rate": .07, "basis": "reviewed_samples", "sampled_word_count": 100,
        "correction_count": 7, "observation_ids": ["page-count"],
        "source_refs": [{"source_id": "page", "observation_id": "page-count"}]}
    return profile


@pytest.mark.parametrize("field,value", [
    ("reviewed_by", "unknown"), ("source_id", "missing"), ("source_sha256", "b"*64),
    ("review_status", "proposed"), ("representative", False), ("word_count", 0),
    ("correction_count", 101), ("correction_count", float("nan")),
])
def test_learned_rates_reject_missing_unreviewed_or_invalid_evidence(profile, field, value):
    personal_frequency_profile(profile)
    validate_profile(profile)
    profile["observations"][0][field] = value
    with pytest.raises(InkformError):
        validate_profile(profile)


def test_learned_rate_cannot_change_counts_or_duplicate_one_page(profile):
    personal_frequency_profile(profile)
    profile["corrections"]["rate"] = .1
    with pytest.raises(InkformError):
        validate_profile(profile)
    profile["corrections"]["rate"] = .07
    duplicate = copy.deepcopy(profile["observations"][0])
    duplicate["observation_id"] = "page-alias"
    profile["observations"].append(duplicate)
    profile["corrections"].update(sampled_word_count=200, correction_count=14, observation_ids=["page-count", "page-alias"])
    profile["corrections"]["source_refs"].append({"source_id": "page", "observation_id": "page-alias"})
    with pytest.raises(InkformError):
        validate_profile(profile)


def test_synthetic_and_personal_evidence_cannot_be_mixed(profile):
    with pytest.raises(InkformError):
        merge_profile(profile, {"synthetic": False})
    profile["sources"].append({"source_id": "personal", "synthetic": False})
    with pytest.raises(InkformError):
        validate_profile(profile)


def test_learned_form_evidence_must_match_reviewed_glyphs_and_not_aliases(profile):
    profile["synthetic"] = False
    profile["sources"] = [{"source_id": "page"}]
    profile["observations"] = [{"observation_id": "first", "source_id": "page"}, {"observation_id": "second", "source_id": "page"}]
    first = profile["glyphs"][0]
    first["source_refs"] = [{"source_id": "page", "observation_id": "first"}]
    second = copy.deepcopy(first)
    second.update(glyph_id="second-x", source_refs=[{"source_id": "page", "observation_id": "second"}])
    profile["glyphs"].append(second)
    model = {"role": "text", "text": "x", "width": .1, "height": .02, "source_count": 2,
        "glyph_ids": [first["glyph_id"], second["glyph_id"]], "observation_ids": ["first", "second"]}
    profile["variation"] = {"learned": {"version": "inkform-variation-1", "basis": "reviewed_samples",
        "by_glyph": [model], "word_gap": 0, "word_gap_sample_count": 0, "word_gap_observation_ids": []}}
    validate_profile(profile)
    model["glyph_ids"][1] = "missing-glyph"
    with pytest.raises(InkformError):
        validate_profile(profile)
    model["glyph_ids"][1] = second["glyph_id"]
    second["source_refs"][0]["observation_id"] = "first"
    with pytest.raises(InkformError):
        validate_profile(profile)
