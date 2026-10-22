"""Natural repetition and visible edits must preserve content and print geometry."""
import copy
import json
from collections import Counter
from pathlib import Path

import pytest

from inkform.document import literal_document, plain_text, requirements
from inkform.errors import InkformError
from inkform.layout import Shaper, map_strokes, plan_document
from inkform.profiles import geometry_bounds, load_profile, validate_profile
from inkform.rendering import render_document
from inkform.variation import OccurrenceVariation, stable_value


@pytest.fixture
def profile():
    result = load_profile(Path(__file__).resolve().parents[1] / "examples" / "synthetic-profile.json")
    result.setdefault("variation", {})["mode"] = "experimental"
    return result


def normalized(item):
    x, y = item["strokes"][0]["start"]
    return map_strokes(item["strokes"], lambda px, py: (round(px - x, 8), round(py - y, 8)))


def assert_inside(plan):
    p, m = plan["page"], plan["page"]["margins_mm"]
    for item in plan["instances"]:
        left, top, right, bottom = item["ink_bounds"]
        assert m["left"] - 1e-8 <= left <= right <= p["width_mm"] - m["right"] + 1e-8
        assert m["top"] - 1e-8 <= top <= bottom <= p["height_mm"] - m["bottom"] + 1e-8


def with_mark(profile, rate=None):
    profile = copy.deepcopy(profile)
    profile["glyphs"] = [glyph for glyph in profile["glyphs"] if glyph["role"] != "mark.correction"]
    mark = copy.deepcopy(next(g for g in profile["glyphs"] if (g["role"], g["text"]) == ("math.operator", "−")))
    mark.update(glyph_id="synthetic.correction.crossout", role="mark.correction", text="crossout")
    profile["glyphs"].append(mark)
    if rate is not None:
        profile["corrections"] = {"rate": rate, "basis": "synthetic_demo", "sampled_word_count": 100,
                                  "correction_count": int(rate * 100), "observation_ids": [], "source_refs": []}
    return validate_profile(profile)


def correction_document(deleted="old word", text="new word", mode="none"):
    document = literal_document("")
    document["blocks"][0]["children"] = [{"id": "edit", "type": "correction", "deleted": deleted, "text": text}]
    document["handwriting"] = {"corrections": mode}
    return document


def test_repeated_single_exemplar_n_differs_inside_same_word_and_seed_is_stable(profile):
    # Occurrence deformation is not proof of new word constructions. Repeated
    # lexical words from this single template are covered by word-pattern tests.
    document = literal_document("nnnnnnnnnnnn")
    plan = plan_document(document, profile, seed=31)
    assert len({item["glyph_id"] for item in plan["instances"]}) == 1
    assert len({json.dumps(normalized(item), sort_keys=True) for item in plan["instances"]}) == 12
    widths = [item["ink_bounds"][2] - item["ink_bounds"][0] for item in plan["instances"]]
    assert max(widths) / min(widths) > 1.025
    assert plan == plan_document(document, profile, seed=31)
    assert plan["geometry_hash"] != plan_document(document, profile, seed=32)["geometry_hash"]
    assert all(item["variation"]["basis"] == "conservative_estimate" for item in plan["instances"])
    assert_inside(plan)


def test_amount_zero_preserves_selected_geometry_and_baselines(profile):
    profile["variation"] = {"mode": "experimental", "amount": 0}
    plan = plan_document(literal_document("nnnn"), profile)
    assert len({json.dumps(normalized(item), sort_keys=True) for item in plan["instances"]}) == 1
    source = next(g for g in profile["glyphs"] if g["glyph_id"] == plan["instances"][0]["glyph_id"])
    original = {"strokes": map_strokes(source["strokes"], lambda x, y: (x * 3, y * 3))}
    assert normalized(plan["instances"][0]) == normalized(original)
    assert len({item["strokes"][0]["start"][1] for item in plan["instances"]}) == 1


def test_learning_controls_sampled_axes_without_disabling_unsupported_gap_estimate(profile):
    glyph = next(g for g in profile["glyphs"] if (g["role"], g["text"]) == ("text", "n"))
    # Contract-level test: the actual evidence references are independently
    # checked by the profile and learning tests before a profile can be shaped.
    profile["variation"] = {"mode": "experimental", "learned": {"by_glyph": [{"role": "text", "text": "n", "width": 0,
        "height": .12, "glyph_ids": ["n-one", "n-two"], "observation_ids": ["one", "two"]}],
        "word_gap": 0, "word_gap_sample_count": 0}}
    variation = OccurrenceVariation(profile, 2)
    factors = variation.factors(glyph, "n:0", "word")
    assert factors["width"] == 1 and factors["height"] != 1
    assert factors["basis"] == "reviewed_samples" and "width" not in factors["estimated_axes"]
    assert factors["sample_observation_ids"] == ["one", "two"]
    assert variation.word_gap("word") != 1  # zero unsupported gaps is not measured zero spread
    profile["variation"]["learned"]["word_gap_sample_count"] = 3
    assert OccurrenceVariation(profile, 2).word_gap("word") == 1


def test_aliases_do_not_inflate_exemplar_count_and_preceding_nodes_share_history(profile):
    alternate = copy.deepcopy(next(g for g in profile["glyphs"] if (g["role"], g["text"]) == ("text", "n")))
    alternate["glyph_id"] += ".alternate"
    profile["glyphs"].append(alternate)
    document = literal_document("nn nn nn nn")
    document["blocks"].insert(0, {"id": "other", "type": "paragraph", "children": [{"id": "other-text", "type": "text", "text": "nn nn"}]})
    plan = plan_document(document, profile)
    assert len({item["glyph_id"] for item in plan["instances"]}) == 1
    assert {item["variation"]["independent_form_count"] for item in plan["instances"]} == {1}
    assert plan["repetition"]["exact_count"] == plan["repetition"]["near_count"] == 0
    assert plan == plan_document(document, profile)


def test_limit_arrows_vary_lengths_without_flipping_or_losing_signs(profile):
    expression = {"type": "limit", "variable": "x", "approaches": {"type": "number", "value": "2"}, "direction": "left", "body": {"type": "symbol", "value": "x"}}
    document = literal_document("")
    document["blocks"] = [{"id": f"limit-{i}", "type": "display_math", "expression": expression} for i in range(8)]
    plan = plan_document(document, profile)
    arrows = [item for item in plan["instances"] if item["text"] == "→"]
    assert len({json.dumps(normalized(item), sort_keys=True) for item in arrows}) == 8
    spans = [item["ink_bounds"][2] - item["ink_bounds"][0] for item in arrows]
    assert max(spans) / min(spans) > 1.02
    for arrow in arrows:
        shaft, head = arrow["strokes"]
        assert shaft["segments"][-1]["end"][0] > shaft["start"][0]
        assert head["segments"][0]["end"][0] > max(head["start"][0], head["segments"][-1]["end"][0])
    assert len([item for item in plan["instances"] if item["token_id"].endswith(".direction") and item["text"] == "−"]) == 8
    assert_inside(plan)


def test_fraction_and_root_bars_still_span_variable_content_at_maximum_variation(profile):
    profile["variation"] = {"mode": "experimental", "amount": .3}
    expression = {"type": "fraction", "numerator": {"type": "number", "value": "123"},
                  "denominator": {"type": "root", "body": {"type": "number", "value": "1234"}}}
    document = literal_document("")
    document["blocks"] = [{"id": "math", "type": "display_math", "expression": expression}]
    plan = plan_document(document, profile)
    bar = next(item for item in plan["instances"] if item["token_id"] == '"math".bar')
    numerator = [item for item in plan["instances"] if item["token_id"].startswith('"math".numerator')]
    denominator = [item for item in plan["instances"] if item["token_id"].startswith('"math".denominator')]
    assert max(item["ink_bounds"][3] for item in numerator) < bar["ink_bounds"][1]
    assert min(item["ink_bounds"][1] for item in denominator) > bar["ink_bounds"][3]
    assert all(bar["ink_bounds"][0] <= item["ink_bounds"][0] <= item["ink_bounds"][2] <= bar["ink_bounds"][2] for item in numerator + denominator)
    root_bar = next(item for item in denominator if item["token_id"].endswith(".bar"))
    radicand = [item for item in denominator if ".body:" in item["token_id"]]
    assert all(root_bar["ink_bounds"][0] <= item["ink_bounds"][0] <= item["ink_bounds"][2] <= root_bar["ink_bounds"][2] for item in radicand)
    assert_inside(plan)


def test_maximum_variation_preserves_outline_counters_and_ink_extents(profile):
    glyph = copy.deepcopy(next(g for g in profile["glyphs"] if (g["role"], g["text"]) == ("text", "n")))
    glyph["representation"] = "clean_outline"
    glyph["outline_calibration"] = {"body_x_height_mm": 3, "pen_width_mm": profile["pen"]["width_mm"]}
    glyph["strokes"] = [
        {"start": [0, -1], "closed": True, "segments": [{"type": "line", "end": [1, -1]}, {"type": "line", "end": [1, 0]}, {"type": "line", "end": [0, 0]}]},
        {"start": [0.3, -0.7], "closed": True, "segments": [{"type": "line", "end": [0.3, -0.3]}, {"type": "line", "end": [0.7, -0.3]}, {"type": "line", "end": [0.7, -0.7]}]},
    ]
    glyph["ink_bounds"] = [0, -1, 1, 0]
    profile["glyphs"] = [glyph]
    profile["variation"] = {"mode": "experimental", "amount": 0.3}
    document = literal_document("nn nn nn nn nn nn nn nn")
    document["page"] = {"width_mm": 50, "height_mm": 45, "margins_mm": 8}
    plan = plan_document(document, profile, mode="clean_outline")
    for item in plan["instances"]:
        outer, hole = item["strokes"]
        assert outer["closed"] and hole["closed"]
        ob, hb = geometry_bounds([outer]), geometry_bounds([hole])
        assert ob[0] < hb[0] < hb[2] < ob[2] and ob[1] < hb[1] < hb[3] < ob[3]
    assert_inside(plan)


@pytest.mark.parametrize("replacement", ["new word", ""])
def test_explicit_correction_keeps_deleted_ink_out_of_semantic_counts(profile, replacement):
    profile = with_mark(profile)
    profile["variation"] = {"mode": "experimental", "amount": 0}  # This fixture tests exact semantic accounting.
    document = correction_document(text=replacement)
    original = copy.deepcopy(document)
    plan = plan_document(document, profile)
    assert document == original
    assert plain_text(document) == replacement
    semantic = [item for item in plan["instances"] if item["semantic"]]
    auxiliary = [item for item in plan["instances"] if not item["semantic"]]
    assert Counter((item["role"], item["text"]) for item in semantic) == Counter(requirements(document))
    assert plan["required_symbol_count"] == plan["rendered_symbol_count"] == len(semantic)
    assert plan["visible_symbol_count"] == len(semantic) + len(auxiliary)
    record, = plan["corrections"]
    assert (record["kind"], record["deleted"], record["replacement"]) == ("explicit", "old word", replacement)
    assert set(record["deleted_token_ids"] + record["mark_token_ids"]) == {item["token_id"] for item in auxiliary}
    assert set(record["replacement_token_ids"]) == {item["token_id"] for item in semantic}
    assert all(item["correction_id"] == record["id"] for item in plan["instances"])
    assert_inside(plan)


def test_explicit_correction_checks_deleted_coverage_and_requires_reviewed_mark(profile):
    profile["glyphs"] = [glyph for glyph in profile["glyphs"] if glyph["role"] != "mark.correction"]
    with pytest.raises(InkformError) as error:
        plan_document(correction_document(), profile)
    assert error.value.code == "missing_correction_mark"
    profile = with_mark(profile)
    with pytest.raises(InkformError) as error:
        plan_document(correction_document(deleted="missing Ж"), profile)
    assert error.value.code == "missing_glyphs"
    assert {"role": "text", "text": "Ж", "count": 1} in error.value.details["missing_glyphs"]


def test_sampled_restarts_are_deterministic_and_require_habit_plus_reviewed_mark(profile):
    document = literal_document("small notes " * 8)
    profile["variation"] = {"mode": "experimental", "amount": 0}  # Restart policy is independent of allograph synthesis.
    no_habit = with_mark(profile)
    assert not plan_document(document, no_habit)["corrections"]
    profile = with_mark(profile, .25)
    plan = plan_document(document, profile, seed=17)
    assert plan["corrections"] and plan == plan_document(document, profile, seed=17)
    for record in plan["corrections"]:
        assert record["kind"] == "sampled_restart"
        assert 0 < len(record["deleted"]) < len(record["replacement"])
        assert record["replacement"].startswith(record["deleted"])
        assert record["attempt_basis"] == "estimated_partial_word_restart"
        assert record["attempt_observed"] is False
        assert record["basis"] == "synthetic_demo" and record["sampled_rate"] == .25
    assert Counter((item["role"], item["text"]) for item in plan["instances"] if item["semantic"]) == Counter(requirements(document))
    assert_inside(plan)
    document["handwriting"] = {"corrections": "none"}
    assert not plan_document(document, profile)["corrections"]
    document.pop("handwriting")
    next(g for g in profile["glyphs"] if g["role"] == "mark.correction")["geometry_status"] = "proposed"
    assert not plan_document(document, profile)["corrections"]


def test_math_corrections_can_be_disabled_for_the_document(profile):
    profile = with_mark(profile, .35)
    document = literal_document("")
    document["blocks"] = [{"id": f"math-{i}", "type": "display_math", "expression": {"type": "symbol", "value": "nnnn"}} for i in range(12)]
    document["handwriting"] = {"corrections": "none"}
    plan = plan_document(document, profile)
    assert not plan["corrections"]
    assert all(item["semantic"] for item in plan["instances"])


def test_sampled_restart_that_cannot_fit_falls_back_to_the_original_word(profile):
    profile = with_mark(profile, .35)
    identity = '"body-text":0'
    seed = next(seed for seed in range(100) if stable_value(seed, identity, "sampled-correction") < .35)
    original = Shaper(profile, seed, "fresh_strokes", 3).text("nnnn", "text", identity)
    document = literal_document("nnnn")
    document["page"] = {"width_mm": original.advance + 20.5, "height_mm": 80, "margins_mm": 10}
    plan = plan_document(document, profile, seed=seed)
    assert not plan["corrections"] and len(plan["instances"]) == 4
    assert_inside(plan)


def test_clean_outline_body_can_use_separate_centerline_crossout(profile):
    profile = with_mark(profile)
    profile["variation"] = {"mode": "experimental", "amount": 0}
    for glyph in profile["glyphs"]:
        if glyph["role"] != "mark.correction":
            glyph["representation"] = "clean_outline"
            glyph["outline_calibration"] = {"body_x_height_mm": 3, "pen_width_mm": profile["pen"]["width_mm"]}
            for stroke in glyph["strokes"]:
                stroke["closed"] = True
    plan = plan_document(correction_document(), profile, mode="clean_outline")
    assert {item["representation"] for item in plan["instances"] if item["role"] == "mark.correction"} == {"centerline"}
    assert {item["representation"] for item in plan["instances"] if item["role"] == "text"} == {"clean_outline"}


def test_pdf_correction_sidecars_retain_source_and_account_for_vector_marks(profile, tmp_path):
    document = correction_document()
    profile = with_mark(profile)
    profile["variation"] = {"mode": "experimental", "amount": 0}  # Semantic sidecar/export contract, not diversity evidence.
    result = render_document(document, profile, tmp_path / "correction.pdf", preview_dpi=72)
    assert result["ok"]
    manifest = json.loads((tmp_path / "correction.build.json").read_text())
    report = json.loads((tmp_path / "correction.report.json").read_text())
    assert manifest["corrections"][0]["source_refs"] == []
    assert manifest["visible_symbol_count"] > manifest["rendered_symbol_count"]
    assert report["handwriting_raster_objects"] == 0
    assert report["checks"]["content_accounted_for"] == "pass"
    assert (tmp_path / "correction.txt").read_text() == "new word"
    assert json.loads((tmp_path / "correction.document.json").read_text()) == document
