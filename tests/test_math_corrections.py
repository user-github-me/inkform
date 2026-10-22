"""Typed math cuts keep exact intended expressions and account for actual ink."""
import copy
import json
from collections import Counter
from pathlib import Path

import pytest

from inkform.document import (literal_document, math_ink_order, math_requirements,
                              math_restart_fragments, math_text, plain_text,
                              requirements, validate_document, visible_requirements)
from inkform.errors import InkformError
from inkform.layout import Shaper, map_strokes, plan_document
from inkform.profiles import load_profile
from inkform.rendering import render_document
from inkform.validation import check_pdf
from inkform.variation import stable_value


def number(value):
    return {"type": "number", "value": str(value)}


def relation(value=2):
    return {"type": "relation", "left": {"type": "symbol", "value": "x"}, "operator": "=", "right": number(value)}


@pytest.fixture
def profile():
    result = load_profile(Path(__file__).parents[1] / "examples/synthetic-profile.json")
    result["variation"] = {"amount": 0}
    return result


def corrected_document(location="inline"):
    document = literal_document("")
    replacement = {"type": "fraction", "numerator": number(1), "denominator": number(2)}
    edit = {"id": "edit", "type": "math_correction" if location == "inline" else "display_math",
            "deleted_expression": relation(9), "expression": replacement}
    if location == "inline":
        document["blocks"][0]["children"] = [edit]
    else:
        document["blocks"] = [edit]
    document["handwriting"] = {"corrections": "none"}
    return document


def with_habit(profile):
    profile["corrections"] = {"rate": .35, "basis": "synthetic_demo", "sampled_word_count": 100,
                              "correction_count": 35, "observation_ids": [], "source_refs": []}
    return profile


def seed_for(identity):
    return next(seed for seed in range(100) if stable_value(seed, identity, "sampled-correction") < .35)


@pytest.mark.parametrize("location", ["inline", "display"])
def test_explicit_math_edit_preserves_intended_ast_plaintext_and_roles(profile, location, tmp_path):
    document = corrected_document(location)
    before = copy.deepcopy(document)
    assert plain_text(document) == "(1)/(2)"
    assert Counter(visible_requirements(document))-Counter(requirements(document)) == Counter(math_requirements(relation(9)))
    output = render_document(document, profile, tmp_path / f"{location}.pdf", preview_dpi=36)
    manifest = json.loads(Path(output["manifest"]).read_text())
    report = json.loads(Path(output["report"]).read_text())
    assert document == before
    assert Path(output["pdf"]).with_suffix(".txt").read_text() == "(1)/(2)"
    assert json.loads(Path(output["pdf"]).with_suffix(".document.json").read_text()) == document
    assert report["ok"] and report["checks"]["emitted_glyph_geometry"] == "pass"
    record, = manifest["corrections"]
    assert record["kind"] == "explicit" and record["content_type"] == "math"
    assert record["deleted_expression"] == relation(9)
    for part in ("deleted", "replacement"):
        items = [i for i in manifest["instances"] if i.get("correction_part") == part]
        assert [(i["role"], i["text"]) for i in items] == math_ink_order(record[part+"_expression"])
        assert all(i["semantic"] is (part == "replacement") for i in items)
    assert Counter((i["role"], i["text"]) for i in manifest["instances"] if i["semantic"]) == Counter(requirements(document))
    # A ledger cannot reinterpret the same visible letters/digits as text.
    record["content_type"] = "text"
    checked = check_pdf(Path(output["pdf"]), manifest, preview_dpi=36)
    assert "invalid_correction_inventory" in {error["code"] for error in checked["errors"]}


@pytest.mark.parametrize("location", ["inline", "display"])
def test_deleted_math_needs_its_exact_role_coverage(profile, location):
    profile["glyphs"] = [g for g in profile["glyphs"] if (g["role"], g["text"]) != ("math.digit", "9")]
    assert any((g["role"], g["text"]) == ("text", "9") for g in profile["glyphs"])
    with pytest.raises(InkformError) as caught:
        plan_document(corrected_document(location), profile)
    assert caught.value.code == "missing_glyphs"
    assert {"role": "math.digit", "text": "9", "count": 1} in caught.value.details["missing_glyphs"]


@pytest.mark.parametrize("location", ["inline", "display"])
def test_cli_coverage_includes_the_mark_for_explicit_math_edits(profile, location, tmp_path):
    from inkform.cli import parser, run
    profile["glyphs"] = [g for g in profile["glyphs"] if g["role"] != "mark.correction"]
    profile_path, document_path = tmp_path / "profile.json", tmp_path / "document.json"
    profile_path.write_text(json.dumps(profile))
    document_path.write_text(json.dumps(corrected_document(location)))
    result = run(parser().parse_args(["profile", "coverage", str(profile_path), "--document", str(document_path)]))
    assert not result["ok"]
    assert result["missing_glyphs"] == [{"role": "mark.correction", "text": "crossout", "count": 1}]


@pytest.mark.parametrize("invalid", [None, "x = 2", {"type": "graph"}])
def test_explicit_math_correction_requires_a_supported_deleted_expression(invalid):
    document = corrected_document()
    document["blocks"][0]["children"][0]["deleted_expression"] = invalid
    with pytest.raises(InkformError):
        validate_document(document)


@pytest.mark.parametrize("expression", [
    relation(), number("-12.4"), {"type": "symbol", "value": "xy"},
    {"type": "row", "items": [number(1), number(2), number(3)]},
    {"type": "fraction", "numerator": number(1), "denominator": number(2)},
    {"type": "root", "body": number(2)},
    {"type": "superscript", "base": {"type": "symbol", "value": "x"}, "script": number(2)},
    {"type": "subscript", "base": {"type": "symbol", "value": "x"}, "script": number(2)},
    {"type": "function_call", "name": "f", "arguments": [number(1), number(2)]},
    {"type": "limit", "variable": "x", "approaches": number(1), "direction": "right", "body": number(2)},
])
def test_sampled_math_attempt_is_proper_fragment_and_intended_geometry_stays_intact(profile, expression):
    document = literal_document("")
    document["blocks"] = [{"type": "display_math", "id": "formula", "expression": expression}]
    before = copy.deepcopy(document)
    seed = seed_for('"formula"')
    original = plan_document(document, profile, seed=seed)
    sampled = plan_document(document, with_habit(profile), seed=seed)
    assert sampled == plan_document(document, profile, seed=seed)
    assert document == before and plain_text(document) == math_text(expression)
    record, = sampled["corrections"]
    assert record["attempt_basis"] == "estimated_partial_fragment" and record["attempt_observed"] is False
    assert record["deleted_expression"] in math_restart_fragments(expression)
    assert Counter(math_requirements(record["deleted_expression"])) < Counter(math_requirements(expression))
    semantic = [item for item in sampled["instances"] if item["semantic"]]
    assert [(i["role"], i["text"]) for i in semantic] == math_ink_order(expression)
    for actual, expected in zip(semantic, original["instances"]):
        def relative(item):
            x, y = item["strokes"][0]["start"]
            return map_strokes(item["strokes"], lambda px, py: (round(px-x, 7), round(py-y, 7)))
        assert relative(actual) == relative(expected)
        assert actual["glyph_id"] == expected["glyph_id"]


def test_inline_sampled_math_exports_and_rejects_undeclared_wrong_attempt(profile, tmp_path):
    document = literal_document("")
    expression = relation()
    document["blocks"][0]["children"] = [{"type": "inline_math", "id": "formula", "expression": expression}]
    result = render_document(document, with_habit(profile), tmp_path / "sampled.pdf", seed=seed_for('"formula"'), preview_dpi=36)
    manifest = json.loads(Path(result["manifest"]).read_text())
    record, = manifest["corrections"]
    assert record["content_type"] == "math" and record["replacement_expression"] == expression
    assert json.loads(Path(result["report"]).read_text())["ok"]
    record["deleted_expression"] = number(9)
    record["deleted"] = "9"
    report = check_pdf(Path(result["pdf"]), manifest, preview_dpi=36)
    assert not report["ok"]
    assert "invalid_correction_inventory" in {e["code"] for e in report["errors"]}


def test_optional_math_cut_cannot_overflow_or_invent_partial_single_digit(profile):
    profile = with_habit(profile)
    expression, identity = relation(), '"formula"'
    seed = seed_for(identity)
    original = Shaper(profile, seed, "fresh_strokes", 3).math(expression, identity)
    document = literal_document("")
    document["blocks"] = [{"type": "display_math", "id": "formula", "expression": expression}]
    document["page"] = {"width_mm": original.advance+20.01, "height_mm": 90, "margins_mm": 10}
    planned = plan_document(document, profile, seed=seed)
    assert not planned["corrections"]
    assert all(i["semantic"] for i in planned["instances"])
    document["blocks"][0]["expression"] = number(2)
    planned = plan_document(document, profile, seed=seed)
    assert not planned["corrections"] and len(planned["instances"]) == 1
