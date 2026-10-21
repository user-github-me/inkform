"""Source-evidence statistics must not turn aliases or crop gaps into learning."""

import copy
import math

import pytest

from inkform.measurements import measure_style


@pytest.fixture
def profile():
    return {"schema_version": "1.0", "profile_id": "public-fixture", "profile_version": "1.0.0",
            "synthetic": False, "scripts": ["Latin"], "default_render_mode": "clean_outline",
            "physical_scale": {"body_x_height_mm": 3, "status": "user_chosen"},
            "pen": {"width_mm": .25, "color": "#202020"}, "metrics": {"word_gap": .72, "line_height": 2.8},
            "dynamics": {"stroke_order": "unknown", "timing": "unknown", "pressure": "unknown"},
            "calibration_status": "agent_reviewed", "sources": [{"source_id": "public-source"}],
            "observations": [], "glyphs": []}


def add_glyph(profile, name, text="a", width=1., height=1., bottom=0., role="text", observation=None):
    observation = observation or name
    if not any(record["observation_id"] == observation for record in profile["observations"]):
        profile["observations"].append({"observation_id": observation, "source_id": "public-source"})
    glyph = {"glyph_id": name, "text": text, "role": role, "representation": "centerline", "advance": width+.2,
             "ink_bounds": [0, -height, width, bottom],
             "strokes": [{"start": [0, -height], "segments": [{"type": "line", "end": [width, bottom]}]}],
             "source_refs": [{"source_id": "public-source", "observation_id": observation}],
             "label_status": "reviewed", "geometry_status": "reviewed", "reviewed_by": "fixture-author",
             "stroke_order_status": "unknown"}
    profile["glyphs"].append(glyph)
    return glyph


def add_words(profile, line, positions, *, indices=None, layout_status="reviewed"):
    indices = list(range(len(positions))) if indices is None else indices
    for index, x in zip(indices, positions):
        profile["observations"].append({"observation_id": f"word-{line}-{index}", "source_id": "public-source",
            "kind": "word", "page_index": 0, "line_id": f"line-{line}", "word_index": index,
            "bbox_xywh": [x, line*25, 10, 12], "xheight_px": 10,
            "label_status": "reviewed", "layout_status": layout_status, "reviewed_by": "fixture-author"})


def test_median_mad_and_variant_counts_use_independent_reviewed_samples(profile):
    for index, width in enumerate((1., 1.2, 20.)):
        add_glyph(profile, f"sample-{index}", width=width)
    proposed = add_glyph(profile, "unreviewed", width=500.)
    proposed["geometry_status"] = "proposed"
    before = copy.deepcopy(profile)
    result = measure_style(profile)
    statistics = result["measurements"]["glyph_width_xheight"]
    assert statistics["count"] == 3
    assert statistics["median"] == pytest.approx(1.2)
    assert statistics["mad"] == pytest.approx(.2)
    assert statistics["max"] == 20
    assert result["measurements"]["by_glyph"][0]["variant_count"] == 3
    assert profile == before


def test_roles_and_derived_outlines_do_not_inflate_sample_count(profile):
    add_glyph(profile, "original", text="a", width=1., observation="observed-a")
    add_glyph(profile, "math-alias", text="a", width=1., role="math.variable", observation="observed-a")
    outline = add_glyph(profile, "outline", text="a", width=1.3, observation="observed-a")
    outline["representation"] = "clean_outline"
    outline["strokes"][0]["closed"] = True
    result = measure_style(profile)
    assert result["accepted_glyph_records"] == 3
    assert result["unique_letter_form_samples"] == 1
    assert result["measurements"]["glyph_width_xheight"]["median"] == 1.3
    assert result["measurements"]["glyph_width_xheight"]["mad"] is None
    roles = {record["role"]: record for record in result["measurements"]["by_glyph"]}
    assert roles["text"]["variant_count"] == roles["math.variable"]["variant_count"] == 1


def test_no_samples_and_single_samples_do_not_imply_a_distribution(profile):
    result = measure_style(profile)
    assert result["measurements"]["glyph_width_xheight"]["status"] == "unavailable"
    assert result["measurements"]["glyph_width_xheight"]["median"] is None
    add_glyph(profile, "one-form")
    result = measure_style(profile)
    assert result["measurements"]["glyph_width_xheight"]["status"] == "single_sample"
    assert result["measurements"]["glyph_width_xheight"]["mad"] is None
    assert result["metric_updates"] == {}
    assert result["retained_metrics"] == {"word_gap": .72, "line_height": 2.8}
    assert result["physical_scale"]["inferred_from_images"] is False
    assert result["physical_scale"]["retained"] == profile["physical_scale"]


def test_ascender_descender_and_body_band_are_separate_from_crop_origin(profile):
    add_glyph(profile, "ascender", text="h", height=1.6)
    descender = add_glyph(profile, "descender", text="y", height=1., bottom=.8)
    descender["ink_bounds"] = [-.8, -1., .6, .8]
    descender["strokes"] = [{"start": [.2, -1.], "segments": [
        {"type": "line", "end": [.6, 0.]}, {"type": "line", "end": [-.8, .8]}]}]
    result = measure_style(profile)
    assert result["measurements"]["ascender_height_xheight"]["median"] == 1.6
    assert result["measurements"]["descender_depth_xheight"]["median"] == .8
    form = next(record for record in result["form_geometry"] if record["glyph_id"] == "descender")
    assert form["body_path_bounds_xheight"] == pytest.approx([.2, -1., .6, 0.])
    assert form["body_left_offset_from_ink_xheight"] == pytest.approx(1.)
    assert "not observed interletter gaps" in form["spacing_evidence"]
    assert result["metric_updates"] == {}


def test_stem_slant_estimate_has_correct_sign_and_ignores_curves(profile):
    for index, angle in enumerate((10., 12., 14.)):
        dx = math.tan(math.radians(angle))*1.6
        glyph = add_glyph(profile, f"stem-{index}", text="l", width=dx, height=1.6)
        glyph["strokes"] = [{"start": [0., 0.], "segments": [{"type": "line", "end": [dx, -1.6]}]}]
    curved = add_glyph(profile, "loop", text="l", height=1.5)
    curved["strokes"] = [{"start": [0., 0.], "segments": [{"type": "cubic", "control1": [1., -.5], "control2": [1., -1.], "end": [0., -1.5]}]}]
    result = measure_style(profile)
    statistic = result["measurements"]["slant_degrees"]
    assert statistic["count"] == 3
    assert statistic["median"] == pytest.approx(12.)
    assert statistic["mad"] == pytest.approx(2.)
    assert "not pen direction" in statistic["basis"]


def test_reviewed_adjacent_word_boxes_support_only_word_gap_update(profile):
    add_words(profile, 0, [0, 16, 34])  # gaps .6 and .8 x-height
    add_words(profile, 1, [0, 18, 38])  # gaps .8 and 1.0 x-height
    before = copy.deepcopy(profile)
    result = measure_style(profile)
    assert result["measurements"]["word_gap_xheight"]["count"] == 4
    assert result["metric_updates"] == {"word_gap": .8}
    assert result["metric_update_evidence"]["word_gap"]["line_count"] == 2
    assert result["retained_metrics"]["line_height"] == 2.8
    assert profile == before


def test_ocr_only_nonadjacent_or_single_line_words_do_not_train_spacing(profile):
    add_words(profile, 0, [0, 80], indices=[0, 2])
    add_words(profile, 1, [0, 20], layout_status="proposed")
    add_words(profile, 2, [0, 18, 36, 54])
    result = measure_style(profile)
    assert result["measurements"]["word_gap_xheight"]["count"] == 3
    assert result["metric_updates"] == {}
    assert result["word_gap_exclusions"]["nonadjacent_words"] == 1
    assert result["word_gap_exclusions"]["unreviewed_layout_or_label"] == 2


def test_synthetic_geometry_cannot_be_claimed_as_personal_spacing_training(profile):
    profile["synthetic"] = True
    add_words(profile, 0, [0, 18, 36])
    add_words(profile, 1, [0, 18, 36])
    result = measure_style(profile)
    assert result["measurements"]["word_gap_xheight"]["count"] == 4
    assert result["metric_updates"] == {}
    assert any("synthetic demonstration" in item for item in result["uncertainties"])
