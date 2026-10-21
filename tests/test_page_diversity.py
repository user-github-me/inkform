"""Visible print geometry, not distinct IDs, establishes page-level novelty."""
import copy
from pathlib import Path

import pytest

from inkform.document import literal_document
from inkform.errors import InkformError
from inkform.layout import Shaper, map_strokes, plan_document
from inkform.profiles import geometry_bounds, load_profile
from inkform.repetition import assess_pair, audit_repetition, shape_descriptor
from inkform.variation import OccurrenceVariation


@pytest.fixture
def profile():
    return load_profile(Path(__file__).resolve().parents[1] / "examples" / "synthetic-profile.json")


def assert_distinct(plan):
    report = audit_repetition(plan["instances"])
    assert report["exact_count"] == report["near_count"] == 0, report["pairs"]
    assert report["checked_instance_count"] == len(plan["instances"])


def test_distinct_ink_spans_content_nodes_and_adjacent_pages(profile):
    document = literal_document("")
    document["page"] = {"width_mm": 60, "height_mm": 35, "margins_mm": 5}
    document["blocks"] = [{"id": f"paragraph-{i}", "type": "paragraph", "children": [
        {"id": f"text-{i}", "type": "text", "text": "nnnn"}]} for i in range(8)]
    plan = plan_document(document, profile, seed=31)
    assert plan["page_count"] >= 3
    assert_distinct(plan)
    assert plan == plan_document(document, profile, seed=31)


def test_page_history_releases_old_pages_but_retains_adjacent_ink(profile):
    shaper = Shaper(profile, 31, "fresh_strokes", 3)
    one = shaper.text("nn", "text", '"first"')
    shaper.commit_page(0, one.items)
    two = shaper.text("nn", "text", '"second"')
    shaper.commit_page(1, two.items)
    assert {item["page"] for item in shaper.history["n"]} == {0, 1}
    three = shaper.text("nn", "text", '"third"')
    shaper.commit_page(2, three.items)
    assert {item["page"] for item in shaper.history["n"]} == {1, 2}


def test_entire_independent_pool_is_used_before_any_family_repeats(profile):
    source = next(g for g in profile["glyphs"] if (g["role"], g["text"]) == ("text", "n"))
    for index, width in enumerate((.72, .85, 1.15, 1.30)):
        glyph = copy.deepcopy(source)
        glyph["glyph_id"] = f"distinct-n-{index}"
        glyph["strokes"] = map_strokes(glyph["strokes"], lambda x, y: (x*width, y))
        glyph["advance"] *= width
        # Actual geometry bounds with a conservative pen allowance.
        bounds = geometry_bounds(glyph["strokes"])
        glyph["ink_bounds"] = [bounds[0]-.06, bounds[1]-.06, bounds[2]+.06, bounds[3]+.06]
        profile["glyphs"].append(glyph)
    shaper = Shaper(profile, 8, "fresh_strokes", 3)
    items = shaper.text("nnnnnnnnnn", "text", '"pool"').items
    assert len({item["glyph_id"] for item in items[:5]}) == 5
    assert len({item["glyph_id"] for item in items[5:]}) == 5
    assert {item["variation"]["independent_form_count"] for item in items} == {5}
    assert any(item["variation"].get("interpolation") for item in items)


def test_arrow_shaft_and_head_change_independently_across_math_blocks(profile):
    expression = {"type": "limit", "variable": "x", "approaches": {"type": "number", "value": "2"},
                  "direction": "both", "body": {"type": "symbol", "value": "x"}}
    document = literal_document("")
    document["blocks"] = [{"id": f"limit-{i}", "type": "display_math", "expression": expression} for i in range(12)]
    plan = plan_document(document, profile, seed=17)
    assert_distinct(plan)
    arrows = [item for item in plan["instances"] if item["text"] == "→"]
    head_ratios, shaft_bends = [], []
    for item in arrows:
        shaft, head = item["strokes"]
        tip = head["segments"][0]["end"]
        assert shaft["segments"][-1]["end"][0] > shaft["start"][0]
        assert tip[0] > head["start"][0] and tip[0] > head["segments"][-1]["end"][0]
        upper = tip[1]-head["start"][1]
        lower = head["segments"][-1]["end"][1]-tip[1]
        head_ratios.append(upper/lower)
        segment = shaft["segments"][0]
        assert segment["type"] == "cubic"
        start, end = shaft["start"], segment["end"]
        shaft_bends.append((segment["control1"][1] - (start[1]+(end[1]-start[1])/3))/(end[0]-start[0]))
    assert max(head_ratios)-min(head_ratios) > .15
    assert max(shaft_bends)-min(shaft_bends) > .01


def test_strict_exhaustion_is_reported_when_candidate_geometry_cannot_change(profile, monkeypatch):
    original = OccurrenceVariation.factors
    def fixed(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        result.update(width=1, height=1, shear=0, pen_width=1, baseline_x_height=0,
                      bow_x_height=0, bend_x_height=0, shape_field_x_height=[0]*6)
        return result
    monkeypatch.setattr(OccurrenceVariation, "factors", fixed)
    with pytest.raises(InkformError) as error:
        plan_document(literal_document("nn"), profile)
    assert error.value.code == "repetition_exhausted"
    assert error.value.details["needed_samples"][0]["text"] == "n"
    assert error.value.details["attempted_passes"] == 3
    assert error.value.details["repetition"]["exact_count"] == 1


def test_simple_marks_keep_meaning_and_report_their_limited_shape_space(profile):
    plan = plan_document(literal_document(". . . . . ."), profile)
    assert plan["repetition"]["exact_count"] == 0
    assert plan["repetition"]["limited_count"] > 0
    assert plan["repetition"]["status"] == "review_needed"
    assert all(item["text"] == "." for item in plan["instances"])


def test_maximum_smooth_field_preserves_orientation(profile):
    glyph = next(g for g in profile["glyphs"] if (g["role"], g["text"]) == ("text", "n"))
    profile["variation"] = {"amount": .3}
    variation = OccurrenceVariation(profile, 31)
    epsilon = 1e-5
    for occurrence in range(30):
        factors = variation.factors(glyph, f"n-{occurrence}", "word")
        transform = variation.transform(glyph, factors, 1)
        for x in (0, .25, .5, .75, 1):
            for y in (-1.2, -.9, -.6, -.3, 0):
                px, py = transform(x, y)
                xx, xy = transform(x+epsilon, y)
                yx, yy = transform(x, y+epsilon)
                determinant = ((xx-px)*(yy-py)-(xy-py)*(yx-px))/epsilon**2
                assert determinant > .10


def test_overlapping_source_provenance_does_not_create_a_third_exemplar(profile):
    original = next(g for g in profile["glyphs"] if (g["role"], g["text"]) == ("text", "n"))
    originals = []
    for index, refs in enumerate((['a'], ['b'], ['a', 'b'])):
        glyph = copy.deepcopy(original)
        glyph['glyph_id'] = f'provenance-{index}'
        glyph['source_refs'] = [{'source_id':'photo', 'observation_id':identity} for identity in refs]
        glyph['strokes'] = map_strokes(glyph['strokes'], lambda x,y:(x*(1+index*.1),y))
        originals.append(glyph)
    profile['glyphs'] = originals
    profile['observations'] = [{'observation_id':'a','mask_sha256':'a'*64}, {'observation_id':'b','mask_sha256':'b'*64}]
    shaper = Shaper(profile, 7, 'fresh_strokes', 3)
    assert {glyph['glyph_id'] for glyph in shaper.forms[('text','n')]} == {'provenance-0','provenance-1'}


def test_true_point_dots_have_bounded_ink_variation_and_no_exact_copies(profile):
    glyph = next(g for g in profile['glyphs'] if (g['role'],g['text']) == ('text','.'))
    glyph['strokes'] = [{'start':[0,0], 'segments':[]}]
    glyph['ink_bounds'] = [-.1,-.1,.1,.1]
    shaper = Shaper(profile, 17, 'fresh_strokes', 3)
    items = shaper.text('...', 'text', 'points').items
    for item in items:
        item['page'] = 0
        assert .96*profile['pen']['width_mm'] <= item['pen_width_mm'] <= 1.04*profile['pen']['width_mm']
    report = audit_repetition(items)
    assert report['exact_count'] == 0
    assert report['limited_count'] == 3


def test_text_and_math_occurrences_share_visible_repetition_history(profile):
    document = literal_document("")
    document["blocks"] = []
    for index in range(8):
        document["blocks"].append({"id":f"mixed-{index}","type":"paragraph","children":[
            {"id":f"text-x-{index}","type":"text","text":"x "},
            {"id":f"math-x-{index}","type":"inline_math","expression":{"type":"symbol","value":"x"}},
        ]})
    plan = plan_document(document, profile, seed=18)
    assert {item["role"] for item in plan["instances"]} == {"text","math.variable"}
    descriptors = [shape_descriptor(item) for item in plan["instances"]]
    for index, item in enumerate(plan["instances"]):
        for earlier in range(index):
            if abs(item["page"]-plan["instances"][earlier]["page"]) <= 1:
                assert assess_pair(descriptors[index], descriptors[earlier])["category"] == "distinct"
    assert plan["required_symbol_count"] == plan["rendered_symbol_count"] == 16
