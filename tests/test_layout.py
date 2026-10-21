"""Semantic and physical layout regressions using only invented handwriting."""

import copy
import json
from collections import Counter
from pathlib import Path

import pytest

from inkform.document import (
    literal_document,
    load_document,
    math_requirements,
    plain_text,
    requirements,
    validate_expression,
)
from inkform.errors import InkformError
from inkform.layout import Shaper, map_strokes, plan_document
from inkform.profiles import load_profile, validate_profile


@pytest.fixture
def profile():
    return load_profile(Path(__file__).resolve().parents[1] / "examples" / "synthetic-profile.json")


def symbol(value="x", role="math.variable"):
    return {"type": "symbol", "value": value, "role": role}


def number(value="2"):
    return {"type": "number", "value": value}


def fraction(numerator=None, denominator=None):
    return {"type": "fraction", "numerator": numerator or number("1"), "denominator": denominator or number("2")}


def document_math(expression, block_id="equation"):
    return {"schema_version": "1.0", "document_id": "synthetic-math", "content_mode": "literal", "blocks": [{"id": block_id, "type": "display_math", "expression": expression}], "unresolved_inputs": []}


def all_inside_margins(plan):
    page = plan["page"]
    margin = page["margins_mm"]
    for item in plan["instances"]:
        left, top, right, bottom = item["ink_bounds"]
        assert left >= margin["left"] - 1e-8
        assert top >= margin["top"] - 1e-8
        assert right <= page["width_mm"] - margin["right"] + 1e-8
        assert bottom <= page["height_mm"] - margin["bottom"] + 1e-8


@pytest.mark.parametrize("direction,expected_sign", [("left", "−"), ("right", "+"), ("both", None)])
def test_one_sided_limit_keeps_direction_attached_to_approached_value(profile, direction, expected_sign):
    expression = {"type": "limit", "variable": "x", "approaches": number("2"), "direction": direction,
                  "body": {"type": "function_call", "name": "f", "arguments": [symbol()]}}
    document = document_math(expression)
    before = copy.deepcopy(document)
    plan = plan_document(document, profile)
    assert document == before
    signs = [item for item in plan["instances"] if item["token_id"].endswith(".direction")]
    assert [item["text"] for item in signs] == ([expected_sign] if expected_sign else [])
    assert all(context["direction"] == direction for item in plan["instances"] for context in item["math_context"])
    if signs:
        approached = next(item for item in plan["instances"] if ".approaches:" in item["token_id"])
        assert signs[0]["ink_bounds"][0] >= approached["ink_bounds"][2]
        assert signs[0]["ink_bounds"][1] < approached["ink_bounds"][1]
    assert Counter((i["role"], i["text"]) for i in plan["instances"]) == Counter(requirements(document))
    all_inside_margins(plan)


def test_inline_and_display_math_account_for_exact_roles_and_symbols(profile):
    expression = {"type": "relation", "operator": "=", "left": {"type": "root", "body": fraction()},
                  "right": {"type": "superscript", "base": symbol(), "script": number("2")}}
    document = document_math(expression)
    document["blocks"].insert(0, {"id": "prose", "type": "paragraph", "children": [
        {"id": "prose-before", "type": "text", "text": "The value of x is "},
        {"id": "inline-x", "type": "inline_math", "expression": symbol()},
        {"id": "prose-after", "type": "text", "text": "."},
    ]})
    plan = plan_document(document, profile)
    actual = Counter((item["role"], item["text"]) for item in plan["instances"])
    assert actual == Counter(requirements(document))
    assert actual[("text", "x")] == 1
    assert actual[("math.variable", "x")] == 2
    assert plan["required_symbol_count"] == plan["rendered_symbol_count"] == sum(actual.values())
    assert {item["content_node_id"] for item in plan["instances"]} == {"prose-before", "inline-x", "prose-after", "equation"}
    all_inside_margins(plan)


def test_same_count_wrong_symbol_is_rejected(profile, monkeypatch):
    original = Shaper.math

    def incorrect_math(self, expression, *args, **kwargs):
        box = original(self, expression, *args, **kwargs)
        if expression["type"] == "symbol":
            box.items[0]["text"] = "y"
        return box

    monkeypatch.setattr(Shaper, "math", incorrect_math)
    with pytest.raises(InkformError) as error:
        plan_document(document_math(symbol()), profile)
    assert error.value.code == "content_accounting_failed"
    assert error.value.details["missing"] == [{"role": "math.variable", "text": "x", "count": 1}]
    assert error.value.details["unexpected"] == [{"role": "math.variable", "text": "y", "count": 1}]


def test_missing_graph_remains_unresolved_without_inventing_answer(profile):
    document = literal_document("Question 5 requires the missing graph.")
    document["content_mode"] = "solve"
    document["unresolved_inputs"] = [{"source_item": "q5", "reason": "required_graph_missing"}]
    original = copy.deepcopy(document)
    plan = plan_document(document, profile)
    assert document == original
    assert plan["content_status"] == "partial"
    assert plan["unresolved_inputs"] == original["unresolved_inputs"]
    assert "".join(item["text"] for item in plan["instances"]) == "Question5requiresthemissinggraph."
    plan["unresolved_inputs"][0]["reason"] = "changed-in-plan-only"
    assert document["unresolved_inputs"] == original["unresolved_inputs"]


@pytest.mark.parametrize("expression", [
    {"type": "integral", "body": symbol()},
    {"type": "matrix", "rows": [[number()]]},
    {"type": "root", "body": symbol(), "index": number("3")},
    {"type": ["symbol"], "value": "x"},
])
def test_unsupported_math_fails_before_shaping(profile, expression, monkeypatch):
    def never_shape(*args, **kwargs):
        raise AssertionError("Unsupported mathematics reached the shaper")

    monkeypatch.setattr(Shaper, "glyph", never_shape)
    with pytest.raises(InkformError) as error:
        plan_document(document_math(expression), profile)
    assert error.value.code == "unsupported_math"


@pytest.mark.parametrize("expression", [
    {"type": "limit", "variable": "x", "approaches": number(), "body": symbol()},
    {"type": "limit", "variable": "x", "approaches": number(), "body": symbol(), "direction": []},
    {"type": "symbol", "value": "x", "role": []},
    {"type": "number", "value": "2e3"},
])
def test_invalid_math_has_actionable_diagnostic(expression):
    with pytest.raises(InkformError) as error:
        validate_expression(expression)
    assert error.value.code == "invalid_document"


def test_math_coverage_never_uses_text_role(profile):
    profile["glyphs"] = [glyph for glyph in profile["glyphs"] if (glyph["role"], glyph["text"]) != ("math.variable", "x")]
    assert any(g["role"] == "text" and g["text"] == "x" for g in profile["glyphs"])
    with pytest.raises(InkformError) as error:
        plan_document(document_math(symbol()), profile)
    assert error.value.code == "missing_glyphs"
    assert error.value.details["missing_glyphs"] == [{"role": "math.variable", "text": "x", "count": 1}]


def test_long_text_reflows_and_paginates_without_compressing_geometry(profile):
    source = "Small writing keeps its original size. " * 70
    document = literal_document(source)
    document["page"] = {"width_mm": 90, "height_mm": 70, "margins_mm": 10, "body_x_height_mm": 3.1}
    narrow = plan_document(document, profile, seed=81)
    wider_document = copy.deepcopy(document)
    wider_document["page"]["width_mm"] = 210
    wide = plan_document(wider_document, profile, seed=81)
    assert narrow["page_count"] > wide["page_count"]
    assert narrow["page_count"] > 1
    assert len(narrow["lines"]) > len(wide["lines"])
    assert narrow["page"]["body_x_height_mm"] == wide["page"]["body_x_height_mm"] == 3.1
    assert Counter((i["role"], i["text"]) for i in narrow["instances"]) == Counter(requirements(document))
    for small, large in zip(narrow["instances"], wide["instances"]):
        assert small["token_id"] == large["token_id"]
        small_bounds, large_bounds = small["ink_bounds"], large["ink_bounds"]
        assert [small_bounds[2] - small_bounds[0], small_bounds[3] - small_bounds[1]] == pytest.approx([large_bounds[2] - large_bounds[0], large_bounds[3] - large_bounds[1]])
    assert plain_text(document) == source
    all_inside_margins(narrow)


def test_breakable_trailing_whitespace_does_not_overflow_a_fitting_word(profile):
    document = literal_document("small" + " " * 100 + "word" + " " * 100)
    document["page"] = {"width_mm": 50, "height_mm": 90, "margins_mm": 10}
    plan = plan_document(document, profile)
    assert len(plan["lines"]) == 2
    assert "".join(item["text"] for item in plan["instances"]) == "smallword"
    all_inside_margins(plan)


def test_single_unbreakable_word_reports_overflow_without_scaling(profile):
    document = literal_document("abcdefghijklmno" * 3)
    document["page"] = {"width_mm": 45, "height_mm": 90, "margins_mm": 10}
    with pytest.raises(InkformError) as error:
        plan_document(document, profile)
    assert error.value.code == "layout_overflow"


def test_geometry_is_deterministic_and_stable_node_survives_preceding_edit(profile):
    document = literal_document("A stable sentence.")
    first = plan_document(document, profile, seed=2)
    assert first == plan_document(document, profile, seed=2)
    assert first["geometry_hash"] != plan_document(document, profile, seed=3)["geometry_hash"]
    edited = copy.deepcopy(document)
    edited["blocks"].insert(0, {"id": "inserted", "type": "paragraph", "children": [{"id": "inserted-text", "type": "text", "text": "A new paragraph."}]})
    second = plan_document(edited, profile, seed=2)
    surviving = {item["token_id"]: item for item in second["instances"] if item["content_node_id"] == "body-text"}
    for original in first["instances"]:
        moved = surviving[original["token_id"]]
        assert original["glyph_id"] == moved["glyph_id"]
        ox, oy = original["strokes"][0]["start"]
        mx, my = moved["strokes"][0]["start"]
        normalized_original = map_strokes(original["strokes"], lambda x, y: (round(x - ox, 9), round(y - oy, 9)))
        normalized_moved = map_strokes(moved["strokes"], lambda x, y: (round(x - mx, 9), round(y - my, 9)))
        assert normalized_original == normalized_moved


def test_distinct_node_ids_cannot_collide_with_generated_math_paths(profile):
    document = document_math(fraction(), "eq")
    document["blocks"].append({"id": "eq.numerator", "type": "display_math", "expression": number("1")})
    plan = plan_document(document, profile)
    tokens = [item["token_id"] for item in plan["instances"]]
    assert len(set(tokens)) == len(tokens)


def test_nested_fractions_scripts_and_roots_stay_separated_and_inside_margins(profile):
    expression = fraction(
        numerator=fraction(numerator={"type": "superscript", "base": symbol(), "script": number("2")}),
        denominator={"type": "subscript", "base": {"type": "root", "body": symbol("y")}, "script": number("1")},
    )
    plan = plan_document(document_math(expression), profile)
    all_inside_margins(plan)
    assert len(plan["instances"]) == len(math_requirements(expression))
    for bar in [item for item in plan["instances"] if item["token_id"].endswith(".bar") and ".denominator.base." not in item["token_id"]]:
        owner = bar["token_id"][:-4]
        numerator = [item for item in plan["instances"] if item["token_id"].startswith(owner + ".numerator")]
        denominator = [item for item in plan["instances"] if item["token_id"].startswith(owner + ".denominator")]
        assert max(item["ink_bounds"][3] for item in numerator) < bar["ink_bounds"][1]
        assert min(item["ink_bounds"][1] for item in denominator) > bar["ink_bounds"][3]
        for item in numerator + denominator:
            assert item["ink_bounds"][0] >= bar["ink_bounds"][0]
            assert item["ink_bounds"][2] <= bar["ink_bounds"][2]


def test_literal_source_is_unchanged_including_unicode_and_whitespace(profile):
    source = "Literal text:\tA  B\r\nC\n\nD − 2."
    document = literal_document(source)
    original = copy.deepcopy(document)
    plan = plan_document(document, profile)
    assert document == original
    assert plain_text(document) == source
    assert "".join(item["text"] for item in plan["instances"]) == "".join(ch for ch in source if not ch.isspace())


@pytest.mark.parametrize("raw", ['{"schema_version":"1.0","schema_version":"2.0"}', '{"page":{"width_mm":NaN}}'])
def test_duplicate_or_nonfinite_document_json_is_rejected(tmp_path, raw):
    path = tmp_path / "document.json"
    path.write_text(raw)
    with pytest.raises(InkformError) as error:
        load_document(path)
    assert error.value.code == "document_read_error"


def outline_profile(profile):
    result = copy.deepcopy(profile)
    result["variation"] = {"amount": 0}
    for glyph in result["glyphs"]:
        glyph["representation"] = "clean_outline"
        glyph["strokes"] = [{"start": [0, -1], "closed": True, "segments": [
            {"type": "line", "end": [0.5, -1]}, {"type": "line", "end": [0.5, 0]}, {"type": "line", "end": [0, 0]},
        ]}]
        glyph["ink_bounds"] = [0, -1, 0.5, 0]
        glyph["outline_calibration"] = {"body_x_height_mm": result["physical_scale"]["body_x_height_mm"], "pen_width_mm": result["pen"]["width_mm"]}
    return validate_profile(result)


def test_outline_pen_and_script_size_changes_require_compatible_reviewed_variant(profile):
    profile = outline_profile(profile)
    plan_document(literal_document("abc"), profile, mode="clean_outline")
    changed_pen = copy.deepcopy(profile)
    changed_pen["pen"]["width_mm"] *= 2
    with pytest.raises(InkformError) as error:
        plan_document(literal_document("abc"), changed_pen, mode="clean_outline")
    assert error.value.code == "outline_pen_incompatible"
    expression = {"type": "superscript", "base": symbol(), "script": number("2")}
    with pytest.raises(InkformError) as error:
        plan_document(document_math(expression), profile, mode="clean_outline")
    assert error.value.code == "outline_scale_incompatible"
    script = copy.deepcopy(next(g for g in profile["glyphs"] if (g["role"], g["text"]) == ("math.digit", "2")))
    script["glyph_id"] += ".reviewed-script"
    script["outline_calibration"]["body_x_height_mm"] *= 0.68
    profile["glyphs"].append(script)
    for seed in (1, 2, 3, 4):
        plan = plan_document(document_math(expression), profile, seed=seed, mode="clean_outline")
        chosen = next(item for item in plan["instances"] if item["role"] == "math.digit")
        assert chosen["glyph_id"] == script["glyph_id"]
        all_inside_margins(plan)


def test_outline_cannot_be_stretched_into_fraction_primitive(profile):
    profile = outline_profile(profile)
    with pytest.raises(InkformError) as error:
        Shaper(profile, seed=1, mode="clean_outline", size=profile["physical_scale"]["body_x_height_mm"]).bar(8, 0, "bar", profile["physical_scale"]["body_x_height_mm"])
    assert error.value.code == "unsupported_outline_primitive"


def test_vector_export_preserves_literal_source_unresolved_state_and_degenerate_dots(profile, tmp_path):
    from inkform.rendering import render_document

    for glyph in profile["glyphs"]:
        if (glyph["role"], glyph["text"]) == ("text", "."):
            glyph["strokes"] = [{"start": [0, 0], "segments": [{"type": "line", "end": [0, 0]}],
                                 "width_profile": [{"s": 0, "multiplier": 0.8}, {"s": 1, "multiplier": 1.2}]}]
            glyph["ink_bounds"] = [-0.1, -0.1, 0.1, 0.1]
    source = "A.\nPartial."
    document = literal_document(source)
    document["unresolved_inputs"] = [{"source_item": "q5", "reason": "required_graph_missing"}]
    first = render_document(document, profile, tmp_path / "first.pdf", seed=9)
    second = render_document(document, profile, tmp_path / "second.pdf", seed=9)
    assert first["ok"] and second["ok"]
    assert first["geometry_hash"] == second["geometry_hash"]
    assert (tmp_path / "first.pdf").read_bytes() == (tmp_path / "second.pdf").read_bytes()
    assert (tmp_path / "first.txt").read_text() == source
    assert json.loads((tmp_path / "first.document.json").read_text()) == document
    manifest = json.loads((tmp_path / "first.build.json").read_text())
    report = json.loads((tmp_path / "first.report.json").read_text())
    assert manifest["content_status"] == "partial"
    assert manifest["unresolved_inputs"] == document["unresolved_inputs"]
    assert report["handwriting_raster_objects"] == 0
    assert report["checks"]["content_accounted_for"] == "pass_with_declared_missing_input"
    assert len(first["previews"]) == 1 and Path(first["previews"][0]).is_file()
