"""Failures that visual previews or trusting a manifest alone would miss."""

import hashlib
import json
from pathlib import Path

import pytest
from PIL import Image
from pypdf import PdfReader, PdfWriter
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen.canvas import Canvas

from inkform.errors import InkformError
from inkform.validation import check_pdf, compare


MM = 72 / 25.4


def vector_pdf(path, pages=1, size=A4):
    canvas = Canvas(str(path), pagesize=size, invariant=1)
    for _ in range(pages):
        canvas.setLineWidth(.26 * MM)
        canvas.setLineCap(1)
        canvas.line(25 * MM, 250 * MM, 100 * MM, 250 * MM)
        canvas.showPage()
    canvas.save()
    return path


def codes(report):
    return {error["code"] for error in report["errors"]}


def manifest_for(path):
    return {"pdf_sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "page_count": 1,
            "page": {"width_mm": 210, "height_mm": 297,
                     "margins_mm": {"top": 18, "right": 18, "bottom": 18, "left": 18}},
            "instances": [{"token_id": "t0", "glyph_id": "line", "page": 0,
                           "representation": "centerline", "ink_bounds": [24.87, 46.87, 100.13, 47.13]}]}


def test_inspects_real_a4_pdf_and_rasterizes_every_page(tmp_path):
    pdf = vector_pdf(tmp_path / "vector.pdf", pages=2)
    report = check_pdf(pdf, preview_dir=tmp_path / "previews")
    assert report["ok"], report["errors"]
    assert report["page_count"] == 2
    assert report["handwriting_raster_objects"] == 0
    assert report["vector_path_count"] == 2
    assert report["checks"]["content_accounted_for"] == "not_checked"
    assert all(page["rasterized"] and page["nonwhite_pixel_count"] > 0 for page in report["pages"])
    assert [Path(page["preview"]).name for page in report["pages"]] == ["page-001.png", "page-002.png"]
    assert report["pages"][0]["width_mm"] == pytest.approx(210, abs=.01)
    assert report["pages"][0]["painted_path_bounds_mm"] == pytest.approx([24.87, 46.87, 100.13, 47.13], abs=.001)


def test_nested_raster_form_cannot_masquerade_as_vector(tmp_path):
    image = tmp_path / "ink.png"
    Image.new("RGB", (16, 16), "black").save(image)
    pdf = tmp_path / "wrapped-raster.pdf"
    canvas = Canvas(str(pdf), pagesize=A4)
    canvas.beginForm("inner")
    canvas.drawImage(str(image), 50, 600, width=50, height=50)
    canvas.endForm()
    canvas.beginForm("outer")
    canvas.doForm("inner")
    canvas.endForm()
    canvas.doForm("outer")
    # A decorative vector stroke must not make the image pass as handwriting.
    canvas.line(80, 500, 200, 500)
    canvas.save()
    report = check_pdf(pdf)
    assert not report["ok"]
    assert report["handwriting_raster_objects"] == 1
    assert report["pages"][0]["form_xobjects"] == 2
    assert "raster_handwriting" in codes(report)


def test_inline_images_are_detected(tmp_path):
    pdf = tmp_path / "inline.pdf"
    canvas = Canvas(str(pdf), pagesize=A4)
    canvas.drawInlineImage(Image.new("RGB", (8, 8), "black"), 60, 600, 60, 60)
    canvas.save()
    report = check_pdf(pdf)
    assert report["handwriting_raster_objects"] == 1
    assert "raster_handwriting" in codes(report)


def test_font_text_does_not_pass_as_fresh_vector_handwriting(tmp_path):
    pdf = tmp_path / "font.pdf"
    canvas = Canvas(str(pdf), pagesize=A4)
    canvas.drawString(60, 700, "A substitute font is not a personal glyph")
    canvas.line(60, 650, 250, 650)
    canvas.save()
    report = check_pdf(pdf)
    assert "font_text_present" in codes(report)


def test_blank_page_and_white_vector_rectangle_are_not_ink(tmp_path):
    pdf = tmp_path / "white.pdf"
    canvas = Canvas(str(pdf), pagesize=A4)
    canvas.setFillColorRGB(1, 1, 1)
    canvas.rect(0, 0, *A4, fill=1, stroke=0)
    canvas.save()
    report = check_pdf(pdf)
    assert "empty_page" in codes(report)
    assert report["checks"]["visible_ink"] == "fail"


def test_physical_dimensions_and_cropbox_are_checked(tmp_path):
    pdf = vector_pdf(tmp_path / "letter.pdf", size=(612, 792))
    report = check_pdf(pdf)
    assert "page_size_mismatch" in codes(report)
    good = vector_pdf(tmp_path / "a4.pdf")
    reader = PdfReader(good)
    reader.pages[0].cropbox.lower_left = (20, 20)
    writer = PdfWriter()
    writer.add_page(reader.pages[0])
    cropped = tmp_path / "cropped.pdf"
    writer.write(cropped)
    assert "cropped_page" in codes(check_pdf(cropped))


def test_manifest_hash_catches_changed_pdf_and_invalid_physical_bounds(tmp_path):
    pdf = vector_pdf(tmp_path / "vector.pdf")
    manifest = manifest_for(pdf)
    assert check_pdf(pdf, manifest)["ok"]
    pdf.write_bytes(pdf.read_bytes() + b"\n% changed after export\n")
    manifest["instances"][0]["ink_bounds"] = [float("nan"), 20, 40, 50]
    report = check_pdf(pdf, manifest)
    assert {"pdf_hash_mismatch", "invalid_ink_bounds"} <= codes(report)


def test_manifest_page_index_and_safe_margins_are_checked(tmp_path):
    pdf = vector_pdf(tmp_path / "vector.pdf")
    manifest = manifest_for(pdf)
    manifest["instances"][0].update(page=3, ink_bounds=[10, 46, 100, 48])
    report = check_pdf(pdf, manifest)
    assert {"invalid_instance_page", "ink_outside_safe_area"} <= codes(report)


def test_actual_ink_outside_margins_detected_when_manifest_claims_safe(tmp_path):
    pdf = tmp_path / "outside.pdf"
    canvas = Canvas(str(pdf), pagesize=A4)
    canvas.setLineWidth(1)
    canvas.line(5 * MM, 250 * MM, 100 * MM, 250 * MM)
    canvas.save()
    report = check_pdf(pdf, manifest_for(pdf))
    assert "visible_ink_outside_safe_area" in codes(report)
    assert "pdf_path_outside_safe_area" in codes(report)


def test_transformed_nested_off_page_path_is_not_hidden_by_raster_clipping(tmp_path):
    pdf = tmp_path / "off-page.pdf"
    canvas = Canvas(str(pdf), pagesize=A4)
    canvas.beginForm("hidden")
    canvas.line(0, 0, 30, 30)
    canvas.endForm()
    canvas.line(60, 600, 150, 600)
    canvas.saveState()
    canvas.translate(-100, -100)
    canvas.doForm("hidden")
    canvas.restoreState()
    canvas.save()
    report = check_pdf(pdf)
    assert report["pages"][0]["nonwhite_pixel_count"] > 0
    assert "pdf_path_outside_page" in codes(report)


def test_manifest_recheck_detects_changed_math_sign_even_with_equal_counts(tmp_path):
    pdf = vector_pdf(tmp_path / "vector.pdf")
    manifest = manifest_for(pdf)
    manifest.update(required_symbol_count=1, rendered_symbol_count=1,
                    coverage={"glyph_counts": [{"role": "math.operator", "text": "−", "required_count": 1}]})
    manifest["instances"][0].update(role="math.operator", text="−")
    assert check_pdf(pdf, manifest)["checks"]["content_accounted_for"] == "pass"
    manifest["instances"][0]["text"] = "+"
    report = check_pdf(pdf, manifest)
    assert "symbol_inventory_mismatch" in codes(report)
    assert report["checks"]["content_accounted_for"] == "fail"


def test_unresolved_input_is_reported_as_partial_not_invented_answer(tmp_path):
    pdf = vector_pdf(tmp_path / "vector.pdf")
    manifest = manifest_for(pdf)
    manifest.update(required_symbol_count=1, rendered_symbol_count=1,
                    coverage={"glyph_counts": [{"role": "math.operator", "text": "−", "required_count": 1}]},
                    unresolved_inputs=[{"source_item": "q5", "reason": "required_graph_missing"}])
    manifest["instances"][0].update(role="math.operator", text="−")
    report = check_pdf(pdf, manifest)
    assert report["ok"]
    assert report["checks"]["content_accounted_for"] == "pass_with_declared_missing_input"
    assert report["checks"]["math_correctness"] == "not_checked"
    assert report["unresolved_inputs"][0]["reason"] == "required_graph_missing"


def test_exported_outline_keeps_counter_hole_and_vector_ink(tmp_path):
    from inkform.rendering import draw_instance

    def rectangle(left, top, right, bottom):
        return {"start": [left, top], "segments": [
            {"type": "line", "end": [right, top]}, {"type": "line", "end": [right, bottom]},
            {"type": "line", "end": [left, bottom]}], "closed": True}

    pdf = tmp_path / "outline.pdf"
    canvas = Canvas(str(pdf), pagesize=A4)
    draw_instance(canvas, {"representation": "clean_outline",
                           "strokes": [rectangle(30, 30, 50, 50), rectangle(35, 35, 45, 45)]}, 297)
    canvas.save()
    report = check_pdf(pdf, preview_dir=tmp_path / "previews")
    assert report["ok"], report["errors"]
    assert report["vector_path_count"] == 1
    image = Image.open(report["pages"][0]["preview"]).convert("RGB")
    scale = report["pages"][0]["preview_dpi"] / 25.4
    assert image.getpixel((round(40 * scale), round(40 * scale))) == (255, 255, 255)
    assert max(image.getpixel((round(32 * scale), round(40 * scale)))) < 30


def test_comparison_preserves_common_physical_scale_without_similarity_score(tmp_path):
    reference = vector_pdf(tmp_path / "reference.pdf")
    candidate = vector_pdf(tmp_path / "candidate.pdf", pages=2)
    report = compare(reference, candidate, tmp_path / "comparison")
    assert report["common_preview_dpi"] == 300
    assert not report["page_count_equal"]
    assert len(report["page_comparisons"]) == 2
    assert all(Path(path).exists() for path in report["page_comparisons"])
    assert report["reference"]["pages"][0]["preview_pixels"] == report["candidate"]["pages"][0]["preview_pixels"]
    stored = json.loads(Path(report["report_path"]).read_text())
    assert "similarity_score" not in stored
    assert stored["style_review"] == "pending_user_feedback"


def test_vector_comparison_merges_original_paths_at_physical_size(tmp_path):
    from inkform.validation import _inspect_content, _inspect_resources, _painted_bounds

    reference = vector_pdf(tmp_path / "reference.pdf")
    candidate = vector_pdf(tmp_path / "candidate.pdf", pages=2)
    original_reference, original_candidate = reference.read_bytes(), candidate.read_bytes()
    report = compare(reference, candidate, tmp_path / "comparison", preview_dpi=72)
    assert reference.read_bytes() == original_reference
    assert candidate.read_bytes() == original_candidate
    merged = PdfReader(report["vector_comparison_pdf"])
    assert len(merged.pages) == 2
    for index, page in enumerate(merged.pages):
        assert float(page.mediabox.width) / MM == pytest.approx(420, abs=.01)
        assert float(page.mediabox.height) / MM == pytest.approx(309, abs=.01)
        resources = page["/Resources"]
        assert _inspect_resources(resources)["image_xobjects"] == 0
        content = _inspect_content(page.get_contents(), resources, merged)
        assert content["vector_paint_operators"] == (2 if index == 0 else 1)
        assert content["font_text_operators"] == 2  # Explicit diagnostic strip.
        paths = _painted_bounds(page.get_contents(), resources, merged)
        expected = [[24.87, 249.87, 100.13, 250.13], [234.87, 249.87, 310.13, 250.13]] if index == 0 else [[234.87, 249.87, 310.13, 250.13]]
        for actual, bounds_mm in zip(paths, expected):
            assert [value / MM for value in actual] == pytest.approx(bounds_mm, abs=.001)
    assert report["vector_comparison"]["diagnostic_font_labels"]
    assert report["vector_comparison"]["pages"][1]["reference_page"] is None
    assert "no corresponding page" in merged.pages[1].extract_text()
    assert "vector_comparison_pdf" in json.loads(Path(report["report_path"]).read_text())


def test_vector_comparison_cannot_overwrite_a_source_pdf(tmp_path):
    original = vector_pdf(tmp_path / "comparison.pdf")
    previous = original.read_bytes()
    with pytest.raises(InkformError) as error:
        compare(original, original, tmp_path)
    assert error.value.code == "input_output_conflict"
    assert original.read_bytes() == previous


def test_preview_resolution_changes_pixels_without_changing_vector_pdf(tmp_path):
    pdf = vector_pdf(tmp_path / "vector.pdf")
    original = pdf.read_bytes()
    low = check_pdf(pdf, preview_dir=tmp_path / "preview-144", preview_dpi=144)
    high = check_pdf(pdf, preview_dir=tmp_path / "preview-300")
    assert low["ok"] and high["ok"]
    assert low["pages"][0]["preview_dpi"] == 144
    assert high["pages"][0]["preview_dpi"] == 300
    assert low["pages"][0]["preview_pixels"] == [1191, 1684]
    assert high["pages"][0]["preview_pixels"] == [2481, 3508]
    assert low["sha256"] == high["sha256"] == hashlib.sha256(original).hexdigest()
    assert pdf.read_bytes() == original
    assert high["handwriting_raster_objects"] == low["handwriting_raster_objects"] == 0
    with Image.open(high["pages"][0]["preview"]) as preview:
        assert preview.info["dpi"] == pytest.approx((300, 300), abs=.01)
        # Antialiasing produces intermediate edge coverage, not hard pixel steps.
        grayscale = preview.convert("L")
        histogram = grayscale.histogram()
        assert sum(histogram[1:255]) > 0


def test_comparison_uses_requested_resolution_for_both_documents(tmp_path):
    pdf = vector_pdf(tmp_path / "vector.pdf")
    result = compare(pdf, pdf, tmp_path / "comparison-150", preview_dpi=150)
    assert result["common_preview_dpi"] == 150
    for side in ("reference", "candidate"):
        assert result[side]["pages"][0]["preview_dpi"] == 150
        assert result[side]["pages"][0]["preview_pixels"] == [1241, 1754]


@pytest.mark.parametrize("dpi", [0, -1, 601, float("nan"), float("inf"), True])
def test_preview_resolution_rejects_invalid_or_excessive_values(tmp_path, dpi):
    pdf = vector_pdf(tmp_path / "vector.pdf")
    with pytest.raises(InkformError) as error:
        check_pdf(pdf, preview_dpi=dpi)
    assert error.value.code == "invalid_preview_dpi"


def test_corrupt_pdf_reports_a_structured_error(tmp_path):
    pdf = tmp_path / "corrupt.pdf"
    pdf.write_bytes(b"This is not a PDF")
    with pytest.raises(InkformError) as error:
        check_pdf(pdf)
    assert error.value.code == "invalid_pdf"
