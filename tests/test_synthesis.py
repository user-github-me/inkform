"""Derived forms change structure without becoming fabricated observations."""
import copy
import json
from itertools import combinations
from pathlib import Path

import numpy as np
import pytest

from inkform.errors import InkformError
from inkform.geometry import geometry_hash
from inkform.profiles import accepted_glyphs, geometry_bounds, glyph_evidence_key, load_profile, validate_profile
from inkform.smoothing import outline_topology
from inkform.synthesis import (MIN_STRUCTURAL_DISTANCE_MM, approve_variants, glyph_instance,
                              synthesize_variants, write_synthesis_review)
from inkform.word_patterns import structural_descriptor, structural_distance


@pytest.fixture
def profile():
    result = load_profile(Path(__file__).resolve().parents[1]/"examples/synthetic-profile.json")
    result["glyphs"] = [glyph for glyph in result["glyphs"] if glyph["role"] == "text" and glyph["text"] in {"b", "1", "."}]
    return result


@pytest.fixture
def outline(profile):
    profile["glyphs"] = [next(glyph for glyph in profile["glyphs"] if glyph["text"] == "b")]
    glyph = profile["glyphs"][0]
    glyph.update(representation="clean_outline", outline_calibration={"body_x_height_mm": 3, "pen_width_mm": .28})
    # Authored synthetic bowl with an ascender and one enclosed counter.
    glyph["strokes"] = [
        {"start": [0, -1.7], "closed": True, "segments": [
            {"type": "line", "end": [.15, -1.7]}, {"type": "line", "end": [.15, -.92]},
            {"type": "cubic", "control1": [1.4, -1.1], "control2": [1.4, .12], "end": [.02, 0]},
            {"type": "line", "end": [0, -1.7]}]},
        {"start": [.2, -.75], "closed": True, "segments": [
            {"type": "line", "end": [.2, -.18]},
            {"type": "cubic", "control1": [1., -.15], "control2": [1., -.85], "end": [.2, -.75]}]},
    ]
    glyph["ink_bounds"] = geometry_bounds(glyph["strokes"])
    return validate_profile(profile)


@pytest.fixture
def proposals(profile):
    return synthesize_variants(profile, characters="b1", per_glyph=3, seed=72)


def decisions(result):
    return [{"glyph_id": row["glyph_id"], "geometry_sha256": row["geometry_sha256"], "decision": "reviewed"}
            for row in result["proposals"]]


def test_source_conditioned_proposals_are_deterministic_nonmutating_and_not_renderable(profile, proposals):
    original = copy.deepcopy(profile)
    again = synthesize_variants(profile, characters="b1", per_glyph=3, seed=72)
    assert again == proposals
    assert profile == original
    assert proposals["summary"]["proposed_count"] >= 2
    assert proposals["summary"]["independent_observations_added"] == 0
    assert accepted_glyphs(proposals["profile"]) == accepted_glyphs(profile)
    assert proposals["profile"].get("observations") == profile.get("observations")
    parents = {glyph["glyph_id"]: glyph for glyph in profile["glyphs"]}
    for glyph in proposals["profile"]["glyphs"]:
        if not glyph.get("derivation"):
            continue
        assert glyph["label_status"] == glyph["geometry_status"] == "proposed"
        assert glyph["reviewed_by"] is None and glyph["variation_dimensions_observed"] is False
        assert glyph["derivation"]["basis"] == "writer_conditioned_estimate"
        assert all(identity in parents for identity in glyph["derivation"]["parent_glyph_ids"])
        assert glyph["derivation"]["independent_observation_count"] == 0
        assert glyph["derivation"]["geometry_sha256"] == geometry_hash(glyph)


def test_repeating_an_unreviewed_job_does_not_multiply_its_own_proposals(proposals):
    again = synthesize_variants(proposals["profile"], characters="b1", per_glyph=3, seed=72)
    assert again["profile"] == proposals["profile"]
    assert again["proposals"] == proposals["proposals"]


def test_new_families_survive_removing_global_scale_shear_and_position(proposals):
    grouped = {}
    for glyph in proposals["profile"]["glyphs"]:
        if glyph["text"] == ".":
            continue
        grouped.setdefault(glyph["text"], []).append(glyph)
    for glyphs in grouped.values():
        for first, second in combinations(glyphs, 2):
            if not (first.get("derivation") or second.get("derivation")):
                continue
            distance = structural_distance(structural_descriptor(glyph_instance(first)), structural_descriptor(glyph_instance(second)))
            # The metric examines paths, independent of changed IDs and hashes.
            assert distance >= MIN_STRUCTURAL_DISTANCE_MM-1e-7


def test_outline_variants_preserve_counter_and_containment(outline):
    result = synthesize_variants(outline, characters="b", per_glyph=3)
    assert result["proposals"], result["summary"]
    original = outline_topology(outline["glyphs"][0]["strokes"])
    assert original["holes"] == 1
    for glyph in result["profile"]["glyphs"][1:]:
        topology = outline_topology(glyph["strokes"])
        assert topology["parents"] == original["parents"]
        assert topology["holes"] == 1 and topology["components"] == 1
        assert topology["intersections"] is False
        assert all(stroke["closed"] for stroke in glyph["strokes"])
        assert any(segment["type"] == "cubic" for stroke in glyph["strokes"] for segment in stroke["segments"])


def test_digit_one_changes_hook_and_foot_without_bending_stem_into_two(proposals):
    source = next(glyph for glyph in proposals["profile"]["glyphs"] if glyph["text"] == "1" and not glyph.get("derivation"))
    variants = [glyph for glyph in proposals["profile"]["glyphs"] if glyph["text"] == "1" and glyph.get("derivation")]
    assert variants
    for glyph in variants:
        derivation = glyph["derivation"]
        assert derivation["recipe"] == "recorded_hook_and_foot"
        parameters = derivation["parameters"]
        assert parameters["anatomy"] == "one_stem_hook_foot"
        assert parameters["jacobian_review"]["minimum_sampled_singular_value"] >= .3
        slope, intercept = parameters["stem_axis"]
        top, bottom = parameters["top"], parameters["bottom"]
        points = np.asarray([point for stroke in glyph["strokes"] for point in [stroke["start"]]+[seg["end"] for seg in stroke["segments"]]])
        middle = points[(points[:, 1] > top+.3*(bottom-top)) & (points[:, 1] < top+.7*(bottom-top))]
        # A long digit stem stays close to its recorded straight axis; its serif
        # lengths/angles can differ. This caught the generic warp's 2-like result.
        residual = middle[:, 0]-(slope*middle[:, 1]+intercept)
        assert np.max(np.abs(residual)) < .09
        assert glyph["text"] == source["text"] and len(glyph["strokes"]) == len(source["strokes"])


def test_review_is_bound_to_generated_bytes_and_parent_evidence(proposals):
    approved = approve_variants(proposals["profile"], decisions(proposals), "test-geometry-review")
    derived = [glyph for glyph in approved["glyphs"] if glyph.get("derivation")]
    assert all(glyph in accepted_glyphs(approved) for glyph in derived)
    assert all(glyph["derivation"]["review_status"] == "reviewed" for glyph in derived)
    bad_decisions = decisions(proposals)
    bad_decisions[0]["geometry_sha256"] = "0"*64
    with pytest.raises(InkformError, match="exact generated vector"):
        approve_variants(proposals["profile"], bad_decisions, "test-review")
    wrong = copy.deepcopy(proposals["profile"])
    changed = next(glyph for glyph in wrong["glyphs"] if glyph.get("derivation"))
    changed["strokes"][0]["start"][0] += .001
    with pytest.raises(InkformError):
        approve_variants(wrong, decisions(proposals), "test-review")


def test_derived_forms_cannot_become_new_observed_parents(proposals):
    approved = approve_variants(proposals["profile"], decisions(proposals), "test-review")
    derived_ids = {glyph["glyph_id"] for glyph in approved["glyphs"] if glyph.get("derivation")}
    result = synthesize_variants(approved, characters="b", per_glyph=1, seed=74)
    assert all(not derived_ids.intersection(row["parent_glyph_ids"]) for row in result["summary"]["groups"])
    assert all(not derived_ids.intersection(row["parent_glyph_ids"]) for row in result["proposals"])


@pytest.mark.parametrize("malformed", [None, {}, [], [None], ["reviewed"], [{"glyph_id": []}]])
def test_review_requires_typed_nonempty_decisions(proposals, malformed):
    with pytest.raises(InkformError) as error:
        approve_variants(proposals["profile"], malformed, "test-review")
    assert error.value.code == "invalid_synthesis_review"


def test_absent_review_decision_does_not_approve_geometry(proposals):
    incomplete = decisions(proposals)
    incomplete[0].pop("decision")
    with pytest.raises(InkformError, match="reviewed or rejected"):
        approve_variants(proposals["profile"], incomplete, "test-review")


def test_private_source_provenance_is_retained_without_new_independent_samples(profile):
    profile["glyphs"] = [next(glyph for glyph in profile["glyphs"] if glyph["text"] == "b")]
    profile["synthetic"] = False
    profile["physical_scale"]["source"] = "synthetic-test-metadata"
    profile["sources"] = [{"source_id": "fixture-page", "sha256": "a"*64, "synthetic": False}]
    profile["glyphs"][0]["source_refs"] = [{"source_id": "fixture-page", "observation_id": "fixture-crop"}]
    result = synthesize_variants(profile, characters="b", per_glyph=2)
    approved = approve_variants(result["profile"], decisions(result), "test-review")
    assert result["proposals"]
    assert len({glyph_evidence_key(glyph, {}) for glyph in accepted_glyphs(approved)}) == 1
    assert approved["sources"] == profile["sources"]


def test_finite_punctuation_does_not_generate_scribbles_for_uniqueness(profile):
    result = synthesize_variants(profile, characters=".", per_glyph=6)
    assert result["proposals"] == []
    assert result["summary"]["groups"][0]["status"] == "limited_shape_space"


def test_atlas_keeps_review_private_and_vector(proposals, tmp_path):
    files = write_synthesis_review(proposals, tmp_path/"private-review")
    assert json.loads(Path(files["profile"]).read_text()) == proposals["profile"]
    review = json.loads(Path(files["review"]).read_text())
    assert all(decision["decision"] is None for decision in review["decisions"])
    assert files["review_sheets"]
    atlas = Path(files["review_sheets"][0]).read_text()
    assert "<path" in atlas and "<image" not in atlas
    assert "inferred style" in atlas and "generated estimate" in atlas
    assert Path(files["profile"]).stat().st_mode & 0o777 == 0o600
    assert Path(files["review_pdf"]).is_file() and files["review_previews"]


def test_pdf_atlas_paginates_all_candidates_without_thumbnail_cropping(proposals, tmp_path):
    from PIL import Image
    from pypdf import PdfReader
    expanded = copy.deepcopy(proposals)
    expanded["proposals"] = [copy.deepcopy(proposals["proposals"][index % len(proposals["proposals"])]) for index in range(9)]
    files = write_synthesis_review(expanded, tmp_path/"complete-atlas")
    document = PdfReader(files["review_pdf"])
    assert len(document.pages) == len(files["review_previews"]) == 2
    assert "Candidates 1-8 of 9" in document.pages[0].extract_text()
    assert "Candidates 9-9 of 9" in document.pages[1].extract_text()
    for path in files["review_previews"]:
        with Image.open(path) as picture:
            assert picture.size == (1650, 1410)
    assert all(not page.get("/Resources", {}).get("/XObject") for page in document.pages)


@pytest.mark.parametrize("options", [{"per_glyph": 0}, {"per_glyph": True}, {"per_glyph": 25},
                                     {"seed": False}, {"characters": ""}, {"role": "mark.correction"}])
def test_invalid_synthesis_limits_are_rejected(profile, options):
    with pytest.raises(InkformError, match="."):
        synthesize_variants(profile, **options)
