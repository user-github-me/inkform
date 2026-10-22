"""Review and persistence preserve evidence counts and concurrent preferences."""
import copy
import json
from pathlib import Path

import pytest

from inkform.cli import main
from inkform.errors import InkformError
from inkform.geometry import geometry_hash
from inkform.measurements import measure_style
from inkform.profiles import load_profile, merge_profile, save_profile, validate_profile
from inkform.synthesis import approve_variants, synthesize_variants, write_synthesis_review


@pytest.fixture
def proposal(tmp_path, monkeypatch):
    monkeypatch.setenv("INKFORM_DATA_DIR", str(tmp_path / "private"))
    profile = load_profile(Path(__file__).resolve().parents[1] / "examples/synthetic-profile.json")
    profile["glyphs"] = [g for g in profile["glyphs"] if g["role"] == "text" and g["text"] == "b"]
    save_profile(profile)
    result = synthesize_variants(profile, "b", 2, 72)
    assert result["proposals"]
    return profile, result, write_synthesis_review(result, tmp_path / "review")


def test_review_command_merges_only_decisions_into_latest_snapshot(proposal, tmp_path, capsys):
    profile, result, paths = proposal
    updated = merge_profile(profile, {"output_preferences": {"corrections": "none", "selected_by": "writer"}})
    original = save_profile(updated).read_bytes()
    row = result["proposals"][0]
    review = tmp_path / "decisions.json"
    review.write_text(json.dumps({"reviewed_by": "fixture-visual-review", "decisions": [{
        "glyph_id": row["glyph_id"], "geometry_sha256": row["geometry_sha256"], "decision": "reviewed"}]}))
    assert main(["profile", "review-variants", paths["profile"], "--review", str(review)]) == 0
    capsys.readouterr()
    current = load_profile(profile["profile_id"])
    assert current["output_preferences"] == updated["output_preferences"]
    assert current["profile_version"] != updated["profile_version"]
    assert save_profile(updated).read_bytes() == original
    assert {g["glyph_id"] for g in current["glyphs"]} == {g["glyph_id"] for g in profile["glyphs"]} | {row["glyph_id"]}


@pytest.mark.parametrize("edit", ["parent_hash", "source_count", "observed_dimensions", "own_hash", "role"])
def test_derived_evidence_cannot_be_relabelled_as_an_independent_capture(proposal, edit):
    _, result, _ = proposal
    profile = copy.deepcopy(result["profile"])
    glyph = next(g for g in profile["glyphs"] if g.get("derivation"))
    if edit == "parent_hash":
        glyph["derivation"]["parent_geometry_sha256"][0] = "0" * 64
    elif edit == "source_count":
        glyph["derivation"]["independent_observation_count"] = 1
    elif edit == "observed_dimensions":
        glyph["variation_dimensions_observed"] = True
    elif edit == "own_hash":
        glyph["advance"] += .1
    elif edit == "role":
        glyph["role"] = "math.variable"
    with pytest.raises(InkformError):
        validate_profile(profile)


def test_generated_review_does_not_inflate_measured_sample_statistics(proposal):
    profile, result, _ = proposal
    decisions = [{"glyph_id": row["glyph_id"], "geometry_sha256": row["geometry_sha256"], "decision": "reviewed"}
                 for row in result["proposals"]]
    approved = approve_variants(result["profile"], decisions, "fixture-review")
    before, after = measure_style(profile), measure_style(approved)
    assert after["observed_glyph_records"] == before["observed_glyph_records"]
    assert after["derived_glyph_records"] == len(decisions)
    assert after["accepted_glyph_records"] == before["accepted_glyph_records"] + len(decisions)


@pytest.mark.parametrize("review", [[], None, {}, {"decisions": []}, {"decisions": [None], "reviewed_by": "fixture-review"}])
def test_malformed_review_is_diagnostic_and_does_not_save(proposal, tmp_path, capsys, review):
    profile, _, paths = proposal
    review_path = tmp_path / "invalid.json"
    review_path.write_text(json.dumps(review))
    assert main(["profile", "review-variants", paths["profile"], "--review", str(review_path)]) == 2
    assert json.loads(capsys.readouterr().err)["error"]
    assert load_profile(profile["profile_id"])["profile_version"] == profile["profile_version"]


def test_correction_preference_command_is_immutable_and_remembered(proposal, capsys):
    profile, _, _ = proposal
    original = save_profile(profile).read_bytes()
    assert main(["profile", "preferences", profile["profile_id"], "--corrections", "none", "--selected-by", "writer"]) == 0
    capsys.readouterr()
    updated = load_profile(profile["profile_id"])
    assert updated["output_preferences"] == {"corrections": "none", "selected_by": "writer"}
    assert updated["profile_version"] != profile["profile_version"]
    assert save_profile(profile).read_bytes() == original
