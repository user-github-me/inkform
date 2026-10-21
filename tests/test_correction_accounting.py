"""A valid PDF cannot make an inconsistent correction ledger pass inspection."""

import copy
import json
from pathlib import Path

import pytest

from inkform.document import literal_document
from inkform.profiles import load_profile
from inkform.rendering import render_document
from inkform.validation import check_pdf


@pytest.fixture(scope="module")
def correction_export(tmp_path_factory):
    directory = tmp_path_factory.mktemp("correction-accounting")
    profile = load_profile(Path(__file__).resolve().parents[1] / "examples" / "synthetic-profile.json")
    document = literal_document("Meet on ", document_id="correction-accounting")
    document["blocks"][0]["children"].extend([
        {"id": "day-revision", "type": "correction", "deleted": "Tuesday", "text": "Thursday"},
        {"id": "ending", "type": "text", "text": "."},
    ])
    document["handwriting"] = {"corrections": "none"}
    pdf = directory / "revision.pdf"
    render_document(document, profile, pdf, seed=72, preview_dpi=72)
    manifest = json.loads(pdf.with_suffix(".build.json").read_text())
    return pdf, manifest, document


def test_exported_correction_keeps_intended_text_and_accounts_for_extra_ink(correction_export):
    pdf, manifest, document = correction_export
    report = check_pdf(pdf, manifest, preview_dpi=36)
    assert report["ok"], report["errors"]
    assert report["checks"]["content_accounted_for"] == "pass"
    assert manifest["visible_symbol_count"] > manifest["rendered_symbol_count"]
    assert manifest["corrections"][0]["kind"] == "explicit"
    assert pdf.with_suffix(".txt").read_text() == "Meet on Thursday."
    assert json.loads(pdf.with_suffix(".document.json").read_text()) == document


@pytest.mark.parametrize("mutation", [
    "changed_deleted_text",
    "reordered_deleted_text",
    "mismatched_replacement",
    "reordered_replacement",
    "dropped_mark_reference",
    "dropped_mark_instance",
    "orphan_nonsemantic_ink",
    "unknown_token_reference",
    "duplicate_token_reference",
    "wrong_mark_glyph_reference",
    "wrong_correction_id",
    "missing_ledger",
    "explicit_change_claimed_as_restart",
])
def test_tampered_correction_inventory_fails_even_with_unchanged_pdf(correction_export, mutation):
    pdf, original, _ = correction_export
    manifest = copy.deepcopy(original)
    record = manifest["corrections"][0]
    if mutation == "changed_deleted_text":
        record["deleted"] = "Wednesday"
    elif mutation == "reordered_deleted_text":
        record["deleted"] = "dayTues"  # Same letters must still match in order.
    elif mutation == "mismatched_replacement":
        record["replacement"] = "Friday"
    elif mutation == "reordered_replacement":
        record["replacement"] = "dayThurs"
    elif mutation == "dropped_mark_reference":
        record["mark_token_ids"] = []
    elif mutation == "dropped_mark_instance":
        manifest["instances"] = [item for item in manifest["instances"] if item.get("correction_part") != "mark"]
    elif mutation == "orphan_nonsemantic_ink":
        next(item for item in manifest["instances"] if "correction_id" not in item)["semantic"] = False
    elif mutation == "unknown_token_reference":
        record["deleted_token_ids"][0] = "absent-token"
    elif mutation == "duplicate_token_reference":
        record["deleted_token_ids"].append(record["deleted_token_ids"][0])
    elif mutation == "wrong_mark_glyph_reference":
        record["glyph_id"] = "a-different-crossout"
    elif mutation == "wrong_correction_id":
        record["id"] = "an-unrelated-edit"
    elif mutation == "missing_ledger":
        manifest["corrections"] = []
    elif mutation == "explicit_change_claimed_as_restart":
        record["kind"] = "sampled_restart"

    report = check_pdf(pdf, manifest, preview_dpi=36)
    assert report["checks"]["manifest_pdf_hash"] == "pass"
    assert not report["ok"]
    assert report["checks"]["content_accounted_for"] == "fail"
    # Require the correction-specific check: a geometry-hash or count mismatch
    # alone would not demonstrate that the declared edit is accounted for.
    assert "invalid_correction_inventory" in {error["code"] for error in report["errors"]}
