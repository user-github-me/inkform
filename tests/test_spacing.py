"""Word boundaries are measured in visible ink, without changing source forms."""
import copy
import json
from pathlib import Path

import pytest
from pypdf import PdfReader

from inkform.document import literal_document
from inkform.layout import Shaper, item_bounds, map_strokes, plan_document
from inkform.ink_clearance import InkClearance
from inkform.profiles import geometry_bounds, load_profile
from inkform.rendering import export_pdf, render_document
from inkform.validation import _emitted_glyphs, check_pdf
from inkform.variation import stable_value


@pytest.fixture
def profile():
    result = load_profile(Path(__file__).parents[1] / "examples/synthetic-profile.json")
    result["variation"] = {"amount": 0}
    return result


def paths_from_origin(strokes, size=1):
    x, y = strokes[0]["start"]
    return map_strokes(strokes, lambda px, py: (round((px-x)/size, 7), round((py-y)/size, 7)))


def gap(left, right):
    def bounds(item):
        paints = item.get("_ink_paints", [item])
        boxes = [item_bounds(paint) for paint in paints]
        return min(b[0] for b in boxes), max(b[2] for b in boxes)
    return bounds(right)[0] - bounds(left)[1]


@pytest.mark.parametrize("representation", ["centerline", "clean_outline"])
def test_crop_padding_and_overhang_do_not_define_text_spacing(profile, representation, tmp_path):
    source = copy.deepcopy(next(g for g in profile["glyphs"] if (g["role"], g["text"]) == ("text", "n")))
    # A translated source and unrelated advance exercise both overhang and
    # crop-origin dependence. The expected emitted paths remain identical.
    source["strokes"] = map_strokes(source["strokes"], lambda x, y: (x-2, y))
    source["advance"] = 8
    source["ink_bounds"] = geometry_bounds(source["strokes"])
    if representation == "clean_outline":
        source.update(representation=representation, outline_calibration={"body_x_height_mm": 3,
                      "pen_width_mm": profile["pen"]["width_mm"]})
        for stroke in source["strokes"]:
            stroke["closed"] = True
    profile["glyphs"] = [source]
    before = copy.deepcopy(profile)
    mode = "fresh_strokes" if representation == "centerline" else representation
    plan = plan_document(literal_document("nn nn"), profile, mode=mode)
    pdf = tmp_path / "spacing.pdf"
    export_pdf(plan, profile, pdf, "spacing")
    actual = _emitted_glyphs(PdfReader(pdf), plan)
    assert actual is not None
    assert gap(actual[0], actual[1]) == pytest.approx(.36, abs=.001)
    assert gap(actual[1], actual[2]) == pytest.approx(3.6, abs=.001)
    assert gap(actual[2], actual[3]) == pytest.approx(.36, abs=.001)
    for item in plan["instances"]:
        assert paths_from_origin(item["strokes"], 3) == paths_from_origin(source["strokes"])
        assert item["variation"]["source_shape_preserved"] is True
    assert profile == before
    assert plan["spacing"]["source_paths_changed"] is False


@pytest.mark.parametrize("whitespace,count", [(" ", 1), ("   ", 3), ("\t", 4)])
def test_phrase_and_paragraph_share_literal_visible_spaces(profile, whitespace, count):
    text = "nn" + whitespace + "nn"
    phrase = Shaper(profile, 72, "fresh_strokes", 3).phrase(text, "label")
    paragraph = plan_document(literal_document(text), profile)
    assert gap(phrase.items[1], phrase.items[2]) == pytest.approx(3.6*count)
    assert gap(paragraph["instances"][1], paragraph["instances"][2]) == pytest.approx(3.6*count)
    assert gap(phrase.items[0], phrase.items[1]) == pytest.approx(.36)


def test_whitespace_across_text_runs_and_inline_math_remains_visible(profile):
    document = literal_document("")
    document["blocks"][0]["children"] = [
        {"id": "first", "type": "text", "text": "n"},
        {"id": "continued", "type": "text", "text": "n "},
        {"id": "extra-space", "type": "text", "text": " "},
        {"id": "value", "type": "inline_math", "expression": {"type": "number", "value": "1"}},
        {"id": "period", "type": "text", "text": "."},
    ]
    items = plan_document(document, profile)["instances"]
    assert gap(items[0], items[1]) == pytest.approx(.36)
    assert gap(items[1], items[2]) == pytest.approx(7.2)
    assert gap(items[2], items[3]) < gap(items[1], items[2])/3


def test_word_clearance_participates_in_wrapping_without_leading_space(profile):
    shaper = Shaper(profile, 72, "fresh_strokes", 3)
    pair = shaper.phrase("nn nn", "measure")
    document = literal_document("nn nn")
    document["page"] = {"width_mm": 20+pair.advance-.01, "height_mm": 90, "margins_mm": 10}
    plan = plan_document(document, profile)
    assert len(plan["lines"]) == 2
    items = plan["instances"]
    assert item_bounds(items[0])[0] == pytest.approx(10)
    assert item_bounds(items[2])[0] == pytest.approx(10)
    assert "spacing_before" not in items[2]
    assert paths_from_origin(items[0]["strokes"]) == paths_from_origin(items[2]["strokes"])
    for item in items:
        assert item_bounds(item)[2] <= document["page"]["width_mm"]-10


def test_correction_words_and_restart_clear_the_entire_crossout(profile):
    mark = copy.deepcopy(next(g for g in profile["glyphs"] if (g["role"], g["text"]) == ("math.operator", "−")))
    mark.update(glyph_id="fixture-crossout", role="mark.correction", text="crossout")
    profile["glyphs"].append(mark)
    shaper = Shaper(profile, 72, "fresh_strokes", 3)
    corrected = shaper.correction("old word", "new word", "edit", "explicit")
    deleted = [i for i in corrected.items if i["correction_part"] == "deleted"]
    replacement = [i for i in corrected.items if i["correction_part"] == "replacement"]
    crossed = [i for i in corrected.items if i["correction_part"] != "replacement"]
    assert gap(deleted[2], deleted[3]) == pytest.approx(3.6)
    assert gap(replacement[2], replacement[3]) == pytest.approx(3.6)
    assert min(item_bounds(i)[0] for i in replacement)-max(item_bounds(i)[2] for i in crossed) == pytest.approx(3.6)


def test_reviewed_spacing_overrides_fallback_and_small_word_gaps_keep_distinction(profile, monkeypatch):
    # Evidence validation belongs to measurements; this exercises the shaper's
    # contract, including its explicit floor when source gaps would collide.
    monkeypatch.setattr("inkform.layout.spacing_metrics", lambda profile: {
        "letter_gap_xheight": .18, "word_gap_xheight": .8, "basis": "reviewed_source_layout"})
    shaper = Shaper(profile, 72, "fresh_strokes", 3)
    phrase = shaper.phrase("nn nn", "source-spaced")
    assert gap(phrase.items[0], phrase.items[1]) == pytest.approx(.54)
    assert gap(phrase.items[1], phrase.items[2]) == pytest.approx(2.7)
    shaper.spacing["word_gap_xheight"] = .1
    phrase = shaper.phrase("nn nn", "small-gap")
    assert gap(phrase.items[1], phrase.items[2]) >= 3*gap(phrase.items[0], phrase.items[1])-.000001
    spacing = phrase.items[2]["spacing_before"]
    assert spacing["minimum_ink_gap_mm"] > spacing["requested_ink_gap_mm"]


def test_sampled_partial_restart_exports_exact_intended_text_with_covered_ink(profile, tmp_path):
    profile["glyphs"] = [g for g in profile["glyphs"] if g["role"] == "mark.correction"
                         or (g["role"] == "text" and g["text"] in "notes")]
    profile["corrections"] = {"rate": .35, "basis": "synthetic_demo", "sampled_word_count": 100,
                              "correction_count": 35, "observation_ids": [], "source_refs": []}
    seed = next(seed for seed in range(100) if stable_value(seed, '"body-text":0', "sampled-correction") < .35)
    document = literal_document("notes")
    before = copy.deepcopy(document)
    result = render_document(document, profile, tmp_path / "restart.pdf", seed=seed, preview_dpi=36)
    manifest = json.loads(Path(result["manifest"]).read_text())
    record, = manifest["corrections"]
    assert record["replacement"] == "notes" and record["deleted"] != "notes"
    assert "notes".startswith(record["deleted"]) and record["deleted"]
    assert Path(result["pdf"]).with_suffix(".txt").read_text() == "notes"
    assert document == before
    assert "".join(i["text"] for i in manifest["instances"] if i["semantic"]) == "notes"
    assert manifest["coverage"]["complete"]
    for item in manifest["instances"]:
        if item["role"] == "text":
            source = next(g for g in profile["glyphs"] if g["glyph_id"] == item["glyph_id"])
            assert paths_from_origin(item["strokes"], 3) == paths_from_origin(source["strokes"])
    record["attempt_observed"] = True
    report = check_pdf(Path(result["pdf"]), manifest, preview_dpi=36)
    assert not report["ok"]
    assert "invalid_correction_inventory" in {e["code"] for e in report["errors"]}


def polygon(points):
    return {"start": list(points[0]), "segments": [{"type": "line", "end": list(p)} for p in points[1:]], "closed": True}


def swash_profile(profile, representation):
    """Simple authored test paths, not claimed observations of a writer."""
    forms = {}
    if representation == "centerline":
        forms["l"] = [{"start": [0, -1.8], "segments": [{"type": "line", "end": [0, 0]}]}]
        forms["y"] = [{"start": [.85, -1], "segments": [
            {"type": "line", "end": [1.15, -.35]}, {"type": "line", "end": [1.4, -1]}]},
            {"start": [1.15, -.35], "segments": [
                {"type": "line", "end": [1.1, .6]}, {"type": "line", "end": [-.5, .8]}]}]
        forms["i"] = [{"start": [0, -1], "segments": [{"type": "line", "end": [0, 0]}]},
                      {"start": [0, -1.55], "segments": [{"type": "line", "end": [.015, -1.55]}]}]
    else:
        forms["l"] = [polygon([[-.04, -1.8], [.04, -1.8], [.04, 0], [-.04, 0]])]
        forms["y"] = [polygon([[.8,-1],[.9,-1],[1.1,-.45],[1.35,-1],[1.46,-.96],
                                [1.2,-.3],[1.13,.64],[-.5,.85],[-.52,.72],[1,.52],[1.07,-.27]])]
        forms["i"] = [polygon([[-.04,-1],[.04,-1],[.04,0],[-.04,0]]),
                      polygon([[-.07,-1.65],[.07,-1.65],[.07,-1.5],[-.07,-1.5]])]
    glyphs = []
    for text, strokes in forms.items():
        source = copy.deepcopy(next(g for g in profile["glyphs"] if (g["role"], g["text"]) == ("text", text)))
        source.update(strokes=strokes, ink_bounds=geometry_bounds(strokes), advance=7,
                      representation=representation)
        if representation == "clean_outline":
            source["outline_calibration"] = {"body_x_height_mm": 3, "pen_width_mm": profile["pen"]["width_mm"]}
        glyphs.append(source)
    profile["glyphs"] = glyphs
    return profile


@pytest.mark.parametrize("representation", ["centerline", "clean_outline"])
def test_descender_body_placement_preserves_emitted_paths_dots_and_visible_word_space(profile, representation, tmp_path):
    swash_profile(profile, representation)
    mode = "fresh_strokes" if representation == "centerline" else representation
    document = literal_document("lyi lyi")
    plan = plan_document(document, profile, mode=mode)
    pdf = tmp_path / "swash.pdf"
    export_pdf(plan, profile, pdf, "swash")
    actual = _emitted_glyphs(PdfReader(pdf), plan)
    assert actual is not None
    checker = InkClearance()
    for start in (0, 3):
        ell, why, eye = plan["instances"][start:start+3]
        spacing = why["spacing_before"]
        assert spacing["body_gap_mm"] == pytest.approx(.36)
        assert spacing["horizontal_full_ink_gap_mm"] < -1
        assert spacing["minimum_ink_clearance_mm"] > 0
        assert "minimum_ink_gap_mm" not in spacing  # Horizontal overlap is explicit.
        for i in range(start, start+3):
            assert checker.separated(actual[start:i], [actual[i]], 0, .12)
            source = next(g for g in profile["glyphs"] if g["glyph_id"] == plan["instances"][i]["glyph_id"])
            assert paths_from_origin(plan["instances"][i]["strokes"], 3) == paths_from_origin(source["strokes"])
            emitted_paths = [stroke for paint in actual[i]["_ink_paints"] for stroke in paint["strokes"]]
            assert len(emitted_paths) == len(source["strokes"])
            assert [s.get("closed",False) for s in emitted_paths] == [s.get("closed",False) for s in source["strokes"]]
            # Compare actual coordinates within PDF number serialization
            # precision (0.00015 mm at this size), not a preservation flag.
            for emitted, observed in zip(paths_from_origin(emitted_paths,3), paths_from_origin(source["strokes"])):
                assert emitted["start"] == pytest.approx(observed["start"],abs=.00005)
                assert len(emitted["segments"]) == len(observed["segments"])
                for segment, original in zip(emitted["segments"],observed["segments"]):
                    assert segment["type"] == original["type"]
                    for key in ("end","control1","control2"):
                        if key in original:
                            assert segment[key] == pytest.approx(original[key],abs=.00005)
        assert len(eye["strokes"]) == 2  # The detached dot was not removed.
    left = max(item_bounds(item)[2] for item in plan["instances"][:3])
    right = min(item_bounds(item)[0] for item in plan["instances"][3:])
    assert right-left == pytest.approx(3.6)
    assert plan["spacing"]["letter_placement"] == "body_band_with_verified_complete_2d_ink_clearance"


def test_body_compaction_checks_dot_against_tall_prior_ink(profile):
    swash_profile(profile, "centerline")
    ell = next(g for g in profile["glyphs"] if g["text"] == "l")
    ell["strokes"] = [{"start": [0,-1.5], "segments": [
        {"type":"line", "end":[1.2,-1.5]}, {"type":"line", "end":[0,-1]},
        {"type":"line", "end":[0,0]}]}]
    ell["ink_bounds"] = geometry_bounds(ell["strokes"])
    box = Shaper(profile, 72, "fresh_strokes", 3).text("li", "text", "dot-obstacle")
    left, right = box.items
    spacing = right["spacing_before"]
    assert spacing["body_gap_mm"] > spacing["requested_body_gap_mm"] + .25
    assert InkClearance().separated([left], [right], 0, spacing["minimum_ink_clearance_mm"])
    # Trying only the body gap would put the i dot in the l's upper loop.
    shift = spacing["requested_body_gap_mm"]-spacing["body_gap_mm"]
    assert not InkClearance().separated([left], [right], shift, .12)
    assert len(right["strokes"]) == 2


def test_ink_clearance_uses_filled_regions_and_preserves_empty_counters():
    ring = {"representation":"clean_outline", "pen_width_mm":.25, "strokes":[
        polygon([[0,0],[5,0],[5,5],[0,5]]), polygon([[1,1],[4,1],[4,4],[1,4]])]}
    dot = {"representation":"clean_outline", "pen_width_mm":.25,
           "strokes":[polygon([[2,2],[2.5,2],[2.5,2.5],[2,2.5]])]}
    checker = InkClearance()
    assert checker.separated([ring], [dot], 0, .2)
    # This small dot is contained inside the solid ring, with no contour cross.
    assert not checker.separated([ring], [dot], -1.9, .2)


def test_ink_mask_cache_keeps_width_profiles_attached_to_their_paths():
    first = {"representation":"centerline", "pen_width_mm":.2, "strokes":[
        {"start":[0,0], "segments":[{"type":"line", "end":[1,0]}],
         "width_profile":[{"s":0,"multiplier":1},{"s":1,"multiplier":1}]},
        {"start":[0,3], "segments":[{"type":"line", "end":[1,3]}],
         "width_profile":[{"s":0,"multiplier":5},{"s":1,"multiplier":5}]}]}
    second = copy.deepcopy(first)
    second["strokes"][0]["width_profile"], second["strokes"][1]["width_profile"] = (
        second["strokes"][1]["width_profile"], second["strokes"][0]["width_profile"])
    dot = {"representation":"clean_outline", "pen_width_mm":.2,
           "strokes":[polygon([[.3,.37],[.5,.37],[.5,.45],[.3,.45]])]}
    checker = InkClearance()
    assert checker.separated([first], [dot], 0, .05)
    assert not checker.separated([second], [dot], 0, .05)


def test_body_bounds_cache_tracks_baseline_translation_without_changing_shape(profile):
    swash_profile(profile, "centerline")
    shaper = Shaper(profile,72,"fresh_strokes",3)
    item = shaper.glyph("y","text","y").items[0]
    checker = InkClearance()
    first = checker.body_bounds(item)
    other = copy.deepcopy(item)
    other["strokes"] = map_strokes(other["strokes"], lambda x,y:(x+12.3,y+.2))
    other["variation"]["baseline_x_height"] += .2/3
    second = checker.body_bounds(other)
    assert second == pytest.approx([first[0]+12.3,first[1]+.2,first[2]+12.3,first[3]+.2])


def test_swash_words_wrap_using_complete_ink_span(profile):
    swash_profile(profile, "centerline")
    shaper = Shaper(profile, 72, "fresh_strokes", 3)
    pair = shaper.phrase("ly ly", "measure")
    document = literal_document("ly ly")
    span = max(pair.advance, pair.bounds[2])-min(0,pair.bounds[0])
    document["page"] = {"width_mm":20+span-.01, "height_mm":90, "margins_mm":10}
    plan = plan_document(document, profile)
    assert len(plan["lines"]) == 2
    for start in (0,2):
        items = plan["instances"][start:start+2]
        assert min(item_bounds(item)[0] for item in items) == pytest.approx(10)
        assert max(item_bounds(item)[2] for item in items) < document["page"]["width_mm"]-10
