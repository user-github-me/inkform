"""Recognizable word copies cannot pass by resizing or renaming their letters."""
import copy
import hashlib
from pathlib import Path

import pytest

from inkform.document import literal_document
from inkform.errors import InkformError
from inkform.layout import Shaper, map_strokes, plan_document
from inkform.profiles import geometry_bounds, load_profile
from inkform.rendering import export_pdf
from inkform.repetition import audit_repetition
from inkform.validation import check_pdf
from inkform.word_patterns import (audit_word_patterns, compare_words,
                                    structural_distance, word_descriptor)


@pytest.fixture
def profile():
    return load_profile(Path(__file__).resolve().parents[1]/"examples"/"synthetic-profile.json")


def word(profile, text="exist", identity="first", page=0):
    items = Shaper(profile, 72, "fresh_strokes", 3).text(text, "text", identity).items
    for item in items:
        item["page"] = page
    return items


def disguised(items, identity="another", page=0):
    result = copy.deepcopy(items)
    for i, item in enumerate(result):
        item.update(token_id=f"{identity}:{i}", word_id=identity, glyph_id=f"new-{i}", page=page)
        item["strokes"] = map_strokes(item["strokes"], lambda x,y:(1.23*x+.18*y+20, .82*y+10))
        item["variation"] = {"unique": True, "source_family": "new", "width": 1.23}
    return result


def test_affine_letter_disguises_pass_old_distance_but_fail_word_structure(profile):
    first = word(profile)
    second = disguised(first)
    # This is the reported failure: all physical glyphs look sufficiently
    # different to the former 0.035 mm test, but still form the same word.
    assert audit_repetition(first+second)["status"] == "pass"
    report = audit_word_patterns(first+second)
    assert report["status"] == "fail"
    assert report["repeated_word_count"] == 1
    assert report["pairs"][0]["changed_letter_count"] == 0


def test_spacing_changes_and_different_ids_do_not_disguise_same_letter_sequence(profile):
    first = word(profile)
    second = disguised(first)
    for i, item in enumerate(second):
        item["strokes"] = map_strokes(item["strokes"], lambda x,y:(x+(.8 if i%2 else -.8), y+(.35 if i%2 else -.35)))
    pair = compare_words(word_descriptor(first), word_descriptor(second))
    assert pair["spacing_rhythm_distance"] > .1
    assert pair["baseline_rhythm_distance"] > .03
    assert pair["category"] == "repeated_template"


def test_local_crossbar_and_terminal_changes_survive_normalization(profile):
    first = word(profile, "ttt")
    second = disguised(first)
    for item in second:
        # Move only the crossbar, keeping the stem and tail where they were.
        item["strokes"][1] = map_strokes([item["strokes"][1]], lambda x,y:(x-.5, y+.95))[0]
    pair = compare_words(word_descriptor(first), word_descriptor(second))
    assert pair["changed_letter_count"] == 3
    assert pair["category"] == "distinct"


def test_punctuation_does_not_hide_repeated_word_and_page_window_is_bounded(profile):
    first = word(profile)
    second = word(profile, "exist.", "other", page=1)
    # Same original paths in its first five letters, changed IDs/punctuation.
    for index, item in enumerate(second[:5]):
        item["strokes"] = copy.deepcopy(first[index]["strokes"])
        item["pen_width_mm"] = first[index]["pen_width_mm"]
    assert audit_word_patterns(first+second)["repeated_word_count"] == 1
    for item in second:
        item["page"] = 2
    assert audit_word_patterns(first+second)["repeated_word_count"] == 0


def test_missing_lexical_boundaries_are_unchecked_and_zero_is_explicit_optout(profile):
    first = word(profile)
    for item in first:
        del item["word_id"]
    assert audit_word_patterns(first)["status"] == "not_checked"
    items = word(profile)
    items += disguised(items)
    assert audit_word_patterns(items, amount=0)["status"] == "opted_out"
    assert audit_word_patterns(items, amount=False)["status"] == "fail"


def test_layout_refuses_to_call_sparse_warped_word_templates_distinct(profile):
    with pytest.raises(InkformError) as error:
        plan_document(literal_document("exist exist"), profile)
    assert error.value.code == "repetition_exhausted"
    report = error.value.details["word_patterns"]
    assert report["status"] == "fail"
    assert report["pairs"][0]["text"] == "exist"


def test_actual_pdf_word_ink_is_checked_instead_of_manifest_claim(profile, tmp_path):
    # Start from a valid one-word plan, then emit a second affine copy. Give
    # the manifest an intentionally false stored word-pattern pass claim.
    plan = plan_document(literal_document("exist"), profile)
    plan["instances"] += disguised(plan["instances"])
    plan["word_patterns"] = {"status": "pass", "repeated_word_count": 0}
    pdf = tmp_path/"copied-word.pdf"
    export_pdf(plan, profile, pdf, "audit")
    plan["pdf_sha256"] = hashlib.sha256(pdf.read_bytes()).hexdigest()
    report = check_pdf(pdf, plan, preview_dpi=36)
    assert report["checks"]["word_patterns"] == "fail"
    assert report["word_patterns"]["geometry_source"] == "emitted_pdf_vector_paths"
    assert report["word_patterns"]["repeated_word_count"] == 1
    assert any(error["code"] == "repeated_word_patterns" for error in report["errors"])


def test_distinct_local_form_bank_varies_words_across_blocks_and_pages(profile):
    source = next(g for g in profile["glyphs"] if (g["role"], g["text"]) == ("text", "n"))
    # Three explicitly invented n constructions: shoulder placement changes
    # relative to the unchanged stem. These are independent synthetic drawings,
    # not purported new user observations or renamed affine copies.
    for index, (dx, dy) in enumerate([(-.12, .28), (.17, -.22), (.20, .27)]):
        other = copy.deepcopy(source)
        other["glyph_id"] = f"invented-shoulder-n-{index}"
        other["strokes"][1] = map_strokes([other["strokes"][1]], lambda x,y:(x+dx,y+dy))[0]
        other["ink_bounds"] = geometry_bounds(other["strokes"])
        other["advance"] += max(0, dx)
        profile["glyphs"].append(other)
    document = literal_document("")
    document["page"] = {"width_mm": 60, "height_mm": 21, "margins_mm": 5}
    document["blocks"] = [{"id": f"p-{i}", "type": "paragraph", "children": [
        {"id": f"t-{i}", "type": "text", "text": "nnnn"}]} for i in range(4)]
    plan = plan_document(document, profile, seed=31)
    assert plan["page_count"] >= 2
    assert plan["word_patterns"]["comparison_count"] >= 3
    assert plan["word_patterns"]["status"] == "pass"
    assert plan["repetition"]["exact_count"] == plan["repetition"]["near_count"] == 0
    assert plan == plan_document(document, profile, seed=31)
