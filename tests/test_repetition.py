"""Physical shape checks, independent of IDs, floating noise and selection logs."""
import copy
import hashlib
import json
from pathlib import Path

import pytest

from inkform.document import literal_document
from inkform.layout import map_strokes, plan_document
from inkform.profiles import geometry_bounds, load_profile
from inkform.rendering import export_pdf, render_document
from inkform.repetition import (NEAR_THRESHOLD_MM, RepetitionIndex, assess_pair,
                                audit_repetition, item_distance, shape_descriptor)
from inkform.validation import check_pdf


def shape(text="n", role="text", token="one", page=0):
    return {"text": text, "role": role, "token_id": token, "page": page,
            "representation": "centerline", "pen_width_mm": .25,
            "strokes": [{"start": [0, 3], "segments": [
                {"type": "line", "end": [0, 0]},
                {"type": "cubic", "control1": [.1, 1.8], "control2": [1.6, -1.7], "end": [1.8, 1.2]},
                {"type": "line", "end": [1.8, 3]}]}]}


def moved(item, dx=17.34, dy=28.83, scale=1., token="two", page=None):
    item = copy.deepcopy(item)
    item["strokes"] = map_strokes(item["strokes"], lambda x, y: (x*scale+dx, y*scale+dy))
    item["token_id"] = token
    if page is not None:
        item["page"] = page
    return item


def arrow():
    item = shape("→", "math.operator")
    item["strokes"] = [{"start": [0, 0], "segments": [{"type": "line", "end": [3, 0]}]},
                       {"start": [2.3, -.6], "segments": [{"type": "line", "end": [3, 0]}, {"type": "line", "end": [2.3, .6]}]}]
    return item


def reversed_stroke(stroke):
    starts = [stroke["start"]] + [segment["end"] for segment in stroke["segments"]]
    segments = []
    for i in reversed(range(len(stroke["segments"]))):
        old = stroke["segments"][i]
        new = {"type": old["type"], "end": starts[i]}
        if old["type"] == "cubic":
            new.update(control1=old["control2"], control2=old["control1"])
        segments.append(new)
    return {"start": starts[-1], "segments": segments, "closed": stroke.get("closed", False)}


def outline():
    item = shape()
    item["representation"] = "clean_outline"
    item["strokes"] = [
        {"start": [0, 0], "segments": [{"type": "line", "end": [3, 0]}, {"type": "line", "end": [3, 3]}, {"type": "line", "end": [0, 3]}], "closed": True},
        {"start": [.8, .8], "segments": [{"type": "line", "end": [2.2, .8]}, {"type": "line", "end": [2.2, 2.2]}, {"type": "line", "end": [.8, 2.2]}], "closed": True}]
    return item


def test_translated_clone_ignores_token_glyph_random_metadata_and_false_bounds():
    first = shape()
    second = moved(first)
    second.update(glyph_id="new.source.claim", ink_bounds=[100, 100, 200, 300],
                  variation={"width": 1.137, "seed": 42, "unique": True})
    assert shape_descriptor(first).fingerprint == shape_descriptor(second).fingerprint
    assert item_distance(first, second) == 0
    report = audit_repetition([first, second])
    assert report["status"] == "fail" and report["exact_count"] == 1
    assert report["pairs"][0]["first_token_id"] == "one"
    assert report["pairs"][0]["second_token_id"] == "two"


def test_reversed_cubic_and_reordered_arrow_strokes_are_still_exact_clones():
    for original in (shape(), arrow()):
        other = moved(original)
        other["strokes"] = [reversed_stroke(stroke) for stroke in reversed(other["strokes"])]
        assert assess_pair(original, other)["category"] == "exact"


def test_closed_contours_ignore_start_vertex_order_orientation_and_translation():
    first = outline()
    other = moved(first)
    changed = []
    for stroke in reversed(other["strokes"]):
        vertices = [stroke["start"]] + [s["end"] for s in stroke["segments"]]
        vertices = vertices[2:] + vertices[:2]
        changed.append({"start": vertices[0], "segments": [{"type": "line", "end": p} for p in vertices[1:]], "closed": True})
    other["strokes"] = changed
    assert assess_pair(first, other)["category"] == "exact"


def test_float_differences_and_invisible_resize_cannot_pass():
    first = shape()
    other = moved(first, scale=1.00007)
    assert shape_descriptor(first).fingerprint != shape_descriptor(other).fingerprint
    pair = assess_pair(first, other)
    assert pair["category"] == "near"
    assert pair["distance_mm"] < .025  # Below the sampling grid, not false subpixel precision.
    assert audit_repetition([first, other])["status"] == "fail"


def test_real_bend_changes_and_outline_counters_affect_the_ink_distance():
    original = shape()
    changed = moved(original, dx=0, dy=0)
    changed["strokes"][0]["segments"][1]["control1"][0] += .9
    changed["strokes"][0]["segments"][1]["control2"][0] += .65
    assert item_distance(original, changed) > NEAR_THRESHOLD_MM
    first = outline()
    no_counter = copy.deepcopy(first)
    no_counter["strokes"].pop()
    assert item_distance(first, no_counter) > NEAR_THRESHOLD_MM


def test_nominal_math_scaling_is_normalized_but_native_resize_is_visible():
    body = arrow()
    script = moved(body, scale=.6)
    script["pen_width_mm"] *= .6
    assert item_distance(body, script) > NEAR_THRESHOLD_MM
    body["nominal_size_mm"], script["nominal_size_mm"] = 3., 1.8
    assert assess_pair(body, script)["category"] == "exact"
    assert shape_descriptor(script).scale_basis == "nominal_3mm"


def test_same_visible_character_across_roles_cannot_hide_a_copy():
    original = shape()
    different_role = moved(original, token="variable")
    different_role["role"] = "math.variable"
    different_text = moved(original, token="other")
    different_text["text"] = "u"
    report = audit_repetition([original, different_role, different_text])
    assert report["status"] == "fail" and report["comparison_count"] == 1
    pair = report["pairs"][0]
    assert pair["first_role"] == "text" and pair["second_role"] == "math.variable"
    assert pair["category"] == "exact"


def test_same_and_adjacent_pages_are_checked_and_older_history_is_pruned():
    original = shape()
    adjacent = moved(original, page=1)
    remote = moved(original, token="remote", page=3)
    report = audit_repetition([remote, original, adjacent])
    assert report["exact_count"] == report["comparison_count"] == 1
    assert (report["pairs"][0]["first_page"], report["pairs"][0]["second_page"]) == (0, 1)
    index = RepetitionIndex()
    index.add(original)
    index.add(adjacent)
    index.advance(3)
    assert not index.history


def test_candidate_can_exclude_its_own_rebuild_but_final_audit_never_trusts_ids():
    original = shape()
    clone = moved(original, token=original["token_id"])
    index = RepetitionIndex()
    index.add(original)
    assert index.collisions(clone)
    assert not index.collisions(clone, exclude_token_id=clone["token_id"])
    assert audit_repetition([original, clone])["exact_count"] == 1


def test_simple_bar_near_match_is_limited_but_exact_copy_still_fails():
    bar = shape("−", "math.operator")
    bar["strokes"] = [{"start": [0, 0], "segments": [{"type": "line", "end": [3, 0]}]}]
    near = moved(bar, scale=1.002)
    assert assess_pair(bar, near)["category"] == "limited"
    report = audit_repetition([bar, near])
    assert report["status"] == "review_needed" and report["limited_count"] == 1
    assert audit_repetition([bar, moved(bar)])["status"] == "fail"
    pointed = arrow()
    assert assess_pair(pointed, moved(pointed, scale=1.002))["category"] == "near"
    assert not shape_descriptor(pointed).limited


def test_degenerate_pressure_stroke_is_visible_round_ink():
    dot = shape(".")
    dot["strokes"] = [{"start": [0, 0], "segments": [{"type": "line", "end": [0, 0]}],
                       "width_profile": [{"s": 0, "multiplier": .8}, {"s": 1, "multiplier": 1.2}]}]
    descriptor = shape_descriptor(dot)
    assert descriptor.limited and descriptor.boundary.size
    assert assess_pair(dot, moved(dot))["category"] == "exact"


def test_only_explicit_numeric_zero_variation_opts_out():
    pair = [shape(), moved(shape())]
    for policy in ({"enabled": False}, {"amount": "0"}, {"amount": False}, {"strict_near": False}):
        assert audit_repetition(pair, policy=policy)["status"] == "fail"
    report = audit_repetition(pair, policy={"amount": 0})
    assert report["status"] == "opted_out" and report["exact_count"] == 1
    assert report["opt_out"] == "explicit_zero_variation"


def test_legacy_missing_paths_are_honestly_unchecked():
    missing = shape()
    missing.pop("strokes")
    assert audit_repetition([missing])["status"] == "not_checked"
    report = audit_repetition([shape(), missing])
    assert report["status"] == "partial" and report["checked_instance_count"] == 1
    assert report["unchecked_instances"][0]["token_id"] == "one"
    assert audit_repetition([{"page": "bad"}, None])["status"] == "not_checked"


@pytest.mark.parametrize("policy", [{"threshold_mm": 0}, {"threshold_mm": float("nan")}, {"adjacent_pages": 4}, {"strict_near": "yes"}])
def test_invalid_audit_policies_fail_explicitly(policy):
    with pytest.raises(ValueError):
        audit_repetition([shape()], policy=policy)


def test_large_duplicate_reports_count_all_pairs_but_bound_diagnostics():
    report = audit_repetition([moved(shape(), token=f"n-{i}") for i in range(47)])
    assert report["exact_count"] == 47*46//2
    assert len(report["pairs"]) == 1000 and report["truncated"]


def test_page_wide_fraction_bar_fits_the_bounded_descriptor():
    bar = shape("−", "math.operator")
    bar["strokes"] = [{"start": [0, 0], "segments": [{"type": "line", "end": [170, 0]}]}]
    descriptor = shape_descriptor(bar)
    assert descriptor.limited and descriptor.extent_mm[0] == 170
    assert descriptor.distance_field.size < 2_000_000


def test_giant_or_nonfinite_geometry_cannot_allocate_an_unbounded_raster():
    giant = shape()
    giant["strokes"][0]["segments"][0]["end"] = [1e9, 0]
    with pytest.raises(ValueError, match="bounded"):
        shape_descriptor(giant)
    bad = shape()
    bad["strokes"][0]["start"] = [float("nan"), 0]
    with pytest.raises(ValueError):
        shape_descriptor(bad)


@pytest.fixture
def profile():
    return load_profile(Path(__file__).resolve().parents[1]/"examples"/"synthetic-profile.json")


def test_final_pdf_audits_emitted_paths_without_embedding_written_text(tmp_path, profile):
    result = render_document(literal_document("nnnn nnnn"), profile, tmp_path/"normal.pdf", preview_dpi=36)
    report = json.loads(Path(result["report"]).read_text())
    assert report["checks"]["repetition"] == "pass"
    assert report["checks"]["emitted_glyph_geometry"] == "pass"
    assert report["repetition"]["geometry_source"] == "emitted_pdf_vector_paths"
    assert report["repetition"]["checked_instance_count"] == 8
    from pypdf import PdfReader
    data = PdfReader(result["pdf"]).pages[0].get_contents().get_data()
    assert data.count(b"/InkformGlyph BMC") == 8
    assert b"body-text" not in data and b"nnnn" not in data


def test_tampered_emitted_duplicate_fails_even_with_fresh_pdf_hash(tmp_path, profile):
    plan = plan_document(literal_document("nn"), profile)
    bad_plan = copy.deepcopy(plan)
    first, second = bad_plan["instances"]
    dx = second["strokes"][0]["start"][0]-first["strokes"][0]["start"][0]
    dy = second["strokes"][0]["start"][1]-first["strokes"][0]["start"][1]
    second["strokes"] = map_strokes(first["strokes"], lambda x, y: (x+dx, y+dy))
    second["pen_width_mm"] = first["pen_width_mm"]
    path = tmp_path/"tampered.pdf"
    export_pdf(bad_plan, profile, path, "n")
    manifest = {**plan, "pdf_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    report = check_pdf(path, manifest, preview_dpi=36)
    assert not report["ok"] and report["checks"]["repetition"] == "fail"
    assert report["repetition"]["geometry_source"] == "emitted_pdf_vector_paths"
    assert report["repetition"]["near_count"] + report["repetition"]["exact_count"] == 1
    assert report["checks"]["emitted_glyph_geometry"] == "fail"


def test_forged_unique_manifest_does_not_hide_marked_glyph_count_mismatch(tmp_path, profile):
    plan = plan_document(literal_document("nn"), profile)
    path = tmp_path/"count.pdf"
    export_pdf(plan, profile, path, "n")
    manifest = {**plan, "instances": plan["instances"][:1], "pdf_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    report = check_pdf(path, manifest, preview_dpi=36)
    assert report["checks"]["emitted_glyph_geometry"] == "fail"
    assert any(error["code"] == "invalid_emitted_glyph_geometry" for error in report["errors"])


def test_punctuation_label_does_not_exempt_a_complex_letter_shape():
    bogus_dot = shape(".")
    assert not shape_descriptor(bogus_dot).limited
    assert assess_pair(bogus_dot, moved(bogus_dot, scale=1.002))["category"] == "near"


def rewrite_pdf_operations(path, mutate):
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import ContentStream, NameObject
    reader = PdfReader(path)
    writer = PdfWriter()
    for page in reader.pages:
        content = ContentStream(page.get_contents(), reader)
        content.operations = mutate(content.operations)
        page[NameObject("/Contents")] = content
        writer.add_page(page)
    with path.open("wb") as stream:
        writer.write(stream)


@pytest.mark.parametrize("mutation", ["remove_markers", "ungrouped_ink", "non_round_caps", "clipping", "nonzero_fill"])
def test_emitted_audit_rejects_paint_bypasses_even_when_pdf_hash_is_updated(tmp_path, profile, mutation):
    from pypdf.generic import FloatObject, NumberObject
    result = render_document(literal_document("nn"), profile, tmp_path/"bypass.pdf", preview_dpi=36)
    path = Path(result["pdf"])
    manifest = json.loads(Path(result["manifest"]).read_text())
    def mutate(operations):
        if mutation == "remove_markers":
            return [(args, op) for args, op in operations if op not in {b"BMC", b"EMC"}]
        if mutation == "ungrouped_ink":
            return operations + [([FloatObject(100), FloatObject(300)], b"m"), ([FloatObject(110), FloatObject(300)], b"l"), ([], b"S")]
        if mutation == "non_round_caps":
            return [([NumberObject(0)], op) if op == b"J" else (args, op) for args, op in operations]
        if mutation == "clipping":
            return [([FloatObject(0), FloatObject(0), FloatObject(595), FloatObject(842)], b"re"), ([], b"W"), ([], b"n")] + operations
        return [(args, b"f") if op == b"S" else (args, op) for args, op in operations]
    rewrite_pdf_operations(path, mutate)
    manifest["pdf_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    report = check_pdf(path, manifest, preview_dpi=36)
    assert not report["ok"]
    assert report["checks"]["repetition"] == report["checks"]["emitted_glyph_geometry"] == "fail"
    assert any(error["code"] == "invalid_emitted_glyph_geometry" for error in report["errors"])


def small_comma(text=","):
    item = shape(text)
    item["pen_width_mm"] = .16
    item["strokes"] = [{"start": [.28, 0], "segments": [
        {"type": "cubic", "control1": [.45, .12], "control2": [.22, .43], "end": [0, .6]}]}]
    return item


@pytest.mark.parametrize("text", [",", "'", "’", "‘", ";"])
def test_small_tail_punctuation_has_bounded_shape_space_but_exact_copies_fail(text):
    item = small_comma(text)
    if text == ";":
        item["strokes"].append({"start": [.26, -.8], "segments": []})
    assert shape_descriptor(item).limited
    near = moved(item, scale=1.002)
    assert assess_pair(item, near)["category"] == "limited"
    assert audit_repetition([item, near])["status"] == "review_needed"
    assert audit_repetition([item, moved(item)])["status"] == "fail"


def test_long_or_complex_comma_shapes_do_not_get_punctuation_exception():
    long_comma = moved(small_comma(), scale=4)
    assert not shape_descriptor(long_comma).limited
    assert assess_pair(long_comma, moved(long_comma, scale=1.002))["category"] == "near"
    complex_comma = outline()
    complex_comma["text"] = ","
    complex_comma["strokes"] = map_strokes(complex_comma["strokes"], lambda x, y: (x*.3, y*.3))
    assert max(shape_descriptor(complex_comma).extent_mm) < 1.2
    assert not shape_descriptor(complex_comma).limited  # Enclosed counter despite small extents.
    assert audit_repetition([complex_comma, moved(complex_comma, scale=1.002)])["status"] == "fail"
