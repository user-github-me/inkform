"""Faithful output consumes actual forms, and never hides limited evidence."""
import copy
import hashlib
import json
from collections import Counter
from pathlib import Path

import pytest

from inkform.document import literal_document
from inkform.layout import Shaper, map_strokes, plan_document
from inkform.profiles import geometry_bounds, load_profile
from inkform.rendering import export_pdf, render_document
from inkform.synthesis import approve_variants, synthesize_variants
from inkform.validation import check_pdf


@pytest.fixture
def profile():
    return load_profile(Path(__file__).parents[1] / "examples/synthetic-profile.json")


def relative_paths(strokes, size=1):
    x, y = strokes[0]["start"]
    return map_strokes(strokes, lambda px, py: (round((px-x)/size, 8), round((py-y)/size, 8)))


def add_n_form(profile):
    # A second explicitly authored fixture, standing for a distinct reviewed
    # source occurrence. No generated derivation is relabeled as observation.
    source = copy.deepcopy(next(g for g in profile["glyphs"] if (g["role"], g["text"]) == ("text", "n")))
    source["glyph_id"] = "authored-other-n"
    source["strokes"][1] = map_strokes([source["strokes"][1]], lambda x, y: (x+.16, y-.11))[0]
    source["ink_bounds"] = geometry_bounds(source["strokes"])
    source["advance"] += .16
    profile["glyphs"].append(source)


@pytest.mark.parametrize("amount", [0, .1, .3])
def test_default_keeps_original_control_points_stroke_types_and_aspect(profile, amount):
    profile["variation"] = {"amount": amount}
    document = literal_document("a b n s t 1 a b n s t 1")
    document["blocks"].append({"id": "signs", "type": "display_math", "expression": {
        "type": "relation", "operator": "→", "left": {"type": "symbol", "value": "x"},
        "right": {"type": "number", "value": "1"}}})
    plan = plan_document(document, profile)
    sources = {glyph["glyph_id"]: glyph for glyph in profile["glyphs"]}
    assert plan["variation"]["mode"] == "observed"
    assert plan["variation"]["audit_passes"] == 1
    for item in plan["instances"]:
        source = sources[item["glyph_id"]]
        assert relative_paths(item["strokes"], item["nominal_size_mm"]) == relative_paths(source["strokes"])
        assert item["pen_width_mm"] == profile["pen"]["width_mm"]
        assert item["source_refs"] == source["source_refs"]
        assert item["variation"]["source_shape_preserved"] is True
        assert "interpolation" not in item["variation"]
        assert "selected_derivation" not in item["variation"]
    assert plan["repetition"]["status"] == "review_needed"
    assert plan["repetition"]["exact_count"] > 0


def test_default_retains_outline_counters_without_curve_field(profile):
    source = next(g for g in profile["glyphs"] if (g["role"], g["text"]) == ("text", "n"))
    source.update(representation="clean_outline", outline_calibration={"body_x_height_mm": 3,
                  "pen_width_mm": profile["pen"]["width_mm"]})
    source["strokes"] = [
        {"start": [0, -1], "closed": True, "segments": [{"type": "line", "end": [1, -1]},
         {"type": "line", "end": [1, 0]}, {"type": "line", "end": [0, 0]}]},
        {"start": [.3, -.7], "closed": True, "segments": [{"type": "line", "end": [.3, -.3]},
         {"type": "line", "end": [.7, -.3]}, {"type": "line", "end": [.7, -.7]}]},
    ]
    source["ink_bounds"] = geometry_bounds(source["strokes"])
    profile["glyphs"] = [source]
    profile["variation"] = {"amount": .3}
    plan = plan_document(literal_document("nnnn"), profile, mode="clean_outline")
    for item in plan["instances"]:
        assert relative_paths(item["strokes"], 3) == relative_paths(source["strokes"])


def test_legacy_source_reference_without_observation_id_still_renders(profile):
    source = copy.deepcopy(next(g for g in profile['glyphs'] if (g['role'], g['text']) == ('text', 'a')))
    source['source_refs'] = [{'source_id': 'legacy-source'}]
    profile.update(synthetic=False, sources=[{'source_id': 'legacy-source', 'sha256': 'a'*64}], glyphs=[source])
    plan = plan_document(literal_document('aa'), profile)
    assert len(plan['instances']) == 2
    for item in plan['instances']:
        assert item['variation']['sample_observation_ids'] == []
        assert item['source_refs'] == source['source_refs']
        assert relative_paths(item['strokes'], item['nominal_size_mm']) == relative_paths(source['strokes'])


def test_entire_real_bank_is_used_before_reuse_without_making_new_shapes(profile):
    add_n_form(profile)
    plan = plan_document(literal_document("nn nn nn nn nn nn"), profile, seed=17)
    ids = [item["glyph_id"] for item in plan["instances"]]
    assert len(set(ids)) == 2
    assert all(len(set(ids[index:index+2])) == 2 for index in range(0, len(ids), 2))
    assert list(Counter(ids).values()) == [6, 6]
    usage = next(row for row in plan["source_usage"]["groups"] if row["text"] == "n")
    assert usage["available_source_forms"] == usage["selected_source_forms"] == 2
    assert usage["reused_occurrences"] == 10
    assert plan["source_usage"]["collection_requests"][0]["text"] == "n"
    assert plan == plan_document(literal_document("nn nn nn nn nn nn"), profile, seed=17)


def test_even_reviewed_derived_forms_are_excluded_from_normal_rendering(profile):
    proposed = synthesize_variants(profile, characters="b", per_glyph=2, role="text")
    assert proposed["proposals"]
    decisions = [{"glyph_id": row["glyph_id"], "geometry_sha256": row["geometry_sha256"],
                  "decision": "reviewed"} for row in proposed["proposals"]]
    approved = approve_variants(proposed["profile"], decisions, "fixture review")
    generated = {glyph["glyph_id"] for glyph in approved["glyphs"] if glyph.get("derivation")}
    plan = plan_document(literal_document("bb bb bb"), approved)
    assert generated.isdisjoint(item["glyph_id"] for item in plan["instances"])
    assert plan["source_usage"]["excluded_derived_forms"] == len(generated)
    approved.setdefault("variation", {})["mode"] = "experimental"
    enabled = Shaper(approved, 72, "fresh_strokes", 3)
    assert generated <= {glyph["glyph_id"] for glyph in enabled.forms[("text", "b")]}


def test_sparse_word_bank_exports_and_reports_actual_pdf_reuse(profile, tmp_path):
    result = render_document(literal_document("does not exist. does not exist."), profile,
                             tmp_path / "faithful.pdf", preview_dpi=36)
    report = json.loads(Path(result["report"]).read_text())
    assert report["ok"]
    assert result["variation_mode"] == "observed"
    assert result["source_reuse"]["reused_occurrences"] > 0
    assert result["ink_repetition"]["status"] == result["word_repetition"]["status"] == "review_needed"
    assert result["ink_repetition"]["geometry_source"] == "emitted_pdf_vector_paths"
    assert report["checks"]["emitted_glyph_geometry"] == "pass"
    assert report["repetition"]["geometry_source"] == "emitted_pdf_vector_paths"
    assert report["repetition"]["exact_count"] + report["repetition"]["near_count"] > 0
    assert report["repetition"]["status"] == "review_needed"
    assert report["word_patterns"]["repeated_word_count"] == 3
    assert report["word_patterns"]["status"] == "review_needed"
    assert {warning["code"] for warning in report["warnings"]} >= {"observed_source_reuse", "observed_word_reuse"}
    assert report["handwriting_raster_objects"] == 0


def test_observed_mode_still_rejects_tampered_actual_vector_geometry(profile, tmp_path):
    plan = plan_document(literal_document("nn"), profile)
    tampered = copy.deepcopy(plan)
    item = tampered["instances"][1]
    x0 = item["strokes"][0]["start"][0]
    item["strokes"] = map_strokes(item["strokes"], lambda x, y: (x0+(x-x0)*1.3, y))
    pdf = tmp_path / "tampered.pdf"
    export_pdf(tampered, profile, pdf, "tampered")
    plan["pdf_sha256"] = hashlib.sha256(pdf.read_bytes()).hexdigest()
    report = check_pdf(pdf, plan, preview_dpi=36)
    assert not report["ok"]
    assert report["checks"]["emitted_glyph_geometry"] == "fail"
