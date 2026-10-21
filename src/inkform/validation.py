"""Inspect exported PDF objects and pixels, and compare at a physical scale.

These checks establish observable file properties. They do not infer a writer's
identity, certify mathematical answers, or assign a handwriting similarity score.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
from collections import Counter
from numbers import Number
from pathlib import Path

import numpy as np
import pypdfium2 as pdfium
from PIL import Image, ImageDraw
from pypdf import PdfReader, PdfWriter, Transformation
from pypdf.generic import ContentStream, DictionaryObject, NameObject

from .errors import InkformError


PT_TO_MM = 25.4 / 72
PREVIEW_DPI = 300
MAX_PREVIEW_PIXELS = 40_000_000
MAX_PREVIEW_EDGE = 16_000
_PAINT_OPERATORS = {b"S", b"s", b"f", b"F", b"f*", b"B", b"B*", b"b", b"b*"}
_PATH_OPERATORS = {b"m", b"l", b"c", b"v", b"y", b"h", b"re"}
_TEXT_OPERATORS = {b"Tj", b"TJ", b"'", b'"'}


def _finite(value) -> bool:
    return isinstance(value, Number) and not isinstance(value, bool) and math.isfinite(float(value))


def _preview_resolution(preview_dpi: int | float) -> float:
    if not _finite(preview_dpi) or not 36 <= preview_dpi <= 600:
        raise InkformError("invalid_preview_dpi", "Preview resolution must be a finite number between 36 and 600 dpi.")
    return float(preview_dpi)


def _bounds(value) -> bool:
    return (isinstance(value, (list, tuple)) and len(value) == 4
            and all(_finite(v) for v in value) and value[0] <= value[2] and value[1] <= value[3])


def _object(value):
    return value.get_object() if hasattr(value, "get_object") else value


def _identity(value):
    indirect = getattr(value, "indirect_reference", None)
    if indirect is not None:
        return (indirect.idnum, indirect.generation)
    if hasattr(value, "idnum"):
        return (value.idnum, value.generation)
    return id(value)


def _inspect_resources(resources, seen=None, depth=0) -> dict:
    """Count distinct image objects, including resources nested in forms."""
    seen = set() if seen is None else seen
    result = {"image_xobjects": 0, "form_xobjects": 0}
    if depth > 64:
        raise ValueError("PDF resource nesting exceeds 64 levels")
    resources = _object(resources) or {}
    for reference in (_object(resources.get("/XObject", {})) or {}).values():
        obj = _object(reference)
        identity = _identity(reference)
        if identity in seen:
            continue
        seen.add(identity)
        subtype = obj.get("/Subtype")
        if subtype == "/Image":
            result["image_xobjects"] += 1
            # A soft mask is itself a raster object, even if not independently
            # named in the page's resource dictionary.
            mask = obj.get("/SMask")
            if mask is not None and _identity(mask) not in seen:
                seen.add(_identity(mask))
                if _object(mask).get("/Subtype") == "/Image":
                    result["image_xobjects"] += 1
        elif subtype == "/Form":
            result["form_xobjects"] += 1
            nested = _inspect_resources(obj.get("/Resources", resources), seen, depth + 1)
            for key in result:
                result[key] += nested[key]
    return result


def _nonfinite_operand(value) -> bool:
    if isinstance(value, Number):
        return not _finite(value)
    if isinstance(value, (list, tuple)):
        return any(_nonfinite_operand(v) for v in value)
    if isinstance(value, dict):
        return any(_nonfinite_operand(v) for v in value.values())
    return False


def _inspect_content(stream, resources, reader, ancestors=None, depth=0) -> Counter:
    """Inspect actual content operators and each invoked Form XObject."""
    counters = Counter()
    if stream is None:
        return counters
    if depth > 64:
        raise ValueError("PDF content nesting exceeds 64 levels")
    ancestors = set() if ancestors is None else set(ancestors)
    resources = _object(resources) or {}
    for operands, operator in ContentStream(stream, reader).operations:
        counters["operators"] += 1
        if _nonfinite_operand(operands):
            counters["nonfinite_operands"] += 1
        if operator in _PAINT_OPERATORS:
            counters["vector_paint_operators"] += 1
        if operator in _PATH_OPERATORS:
            counters["path_operators"] += 1
        if operator in _TEXT_OPERATORS:
            # A font-based text show remains visible even if paths also exist.
            if any(bool(value) for value in operands):
                counters["font_text_operators"] += 1
        if operator in {b"W", b"W*"}:
            counters["clipping_operators"] += 1
        if operator == b"INLINE IMAGE":
            counters["inline_images"] += 1
        if operator == b"Do":
            xobjects = _object(resources.get("/XObject", {})) or {}
            if not operands or operands[0] not in xobjects:
                raise ValueError("PDF content references a missing XObject")
            reference = xobjects[operands[0]]
            obj = _object(reference)
            if obj.get("/Subtype") == "/Form":
                identity = _identity(reference)
                if identity in ancestors:
                    raise ValueError("PDF content contains a recursive form cycle")
                counters.update(_inspect_content(obj, obj.get("/Resources", resources), reader,
                                                 ancestors | {identity}, depth + 1))
    return counters


def _matrix_product(left, right):
    a, b, c, d, e, f = left
    g, h, i, j, k, l = right
    return (a*g+c*h, b*g+d*h, a*i+c*j, b*i+d*j, a*k+c*l+e, b*k+d*l+f)


def _transform(point, matrix):
    x, y = point
    a, b, c, d, e, f = matrix
    return (a*x+c*y+e, b*x+d*y+f)


def _curve_values(p0, p1, p2, p3):
    a, b, c = -p0+3*p1-3*p2+p3, 2*(p0-2*p1+p2), p1-p0
    roots = []
    if abs(a) < 1e-12:
        if abs(b) >= 1e-12:
            roots.append(-c/b)
    elif b*b-4*a*c >= 0:
        radical = math.sqrt(b*b-4*a*c)
        roots.extend(((-b-radical)/(2*a), (-b+radical)/(2*a)))
    values = [p0, p3]
    for t in roots:
        if 0 < t < 1:
            values.append((1-t)**3*p0 + 3*(1-t)**2*t*p1 + 3*(1-t)*t*t*p2 + t**3*p3)
    return values


def _painted_bounds(stream, resources, reader, matrix=(1, 0, 0, 1, 0, 0), ancestors=None, depth=0, inherited_state=None):
    """Find finite path extents in page coordinates, before any clipping.

    This catches off-page paths that rasterization alone cannot reveal. Bounds
    account for affine transforms and Bézier extrema. Round pen extents use the
    largest singular value of the active transform. Font/image bounds are
    handled by independent object and raster checks instead.
    """
    if stream is None:
        return []
    if depth > 64:
        raise ValueError("PDF path nesting exceeds 64 levels")
    resources = _object(resources) or {}
    ancestors = set() if ancestors is None else set(ancestors)
    state = dict(inherited_state or {"width": 1., "stroke_white": False, "fill_white": False})
    state["matrix"] = matrix
    stack, painted, xs, ys = [], [], [], []
    current = None
    start = None
    for values, op in ContentStream(stream, reader).operations:
        if _nonfinite_operand(values):
            raise ValueError("Non-finite PDF path operand")
        if op == b"q":
            stack.append(state.copy())
        elif op == b"Q":
            if not stack:
                raise ValueError("Unbalanced PDF graphics state")
            state = stack.pop()
        elif op == b"cm":
            state["matrix"] = _matrix_product(state["matrix"], tuple(map(float, values)))
        elif op == b"w":
            state["width"] = abs(float(values[0]))
        elif op in {b"g", b"G", b"rg", b"RG", b"k", b"K"}:
            numeric = list(map(float, values))
            white = all(v >= .999 for v in numeric) if op.lower() != b"k" else all(v <= .001 for v in numeric)
            state["stroke_white" if op in {b"G", b"RG", b"K"} else "fill_white"] = white
        elif op in {b"m", b"l"}:
            current = _transform(tuple(map(float, values)), state["matrix"])
            xs.append(current[0]); ys.append(current[1])
            if op == b"m":
                start = current
        elif op in {b"c", b"v", b"y"}:
            if current is None:
                raise ValueError("PDF curve has no starting point")
            coords = [_transform(tuple(map(float, values[i:i+2])), state["matrix"]) for i in range(0, len(values), 2)]
            if op == b"v":
                coords.insert(0, current)
            elif op == b"y":
                coords.append(coords[-1])
            xs.extend(_curve_values(current[0], *(p[0] for p in coords)))
            ys.extend(_curve_values(current[1], *(p[1] for p in coords)))
            current = coords[-1]
        elif op == b"re":
            x, y, w, h = map(float, values)
            for point in ((x, y), (x+w, y), (x+w, y+h), (x, y+h)):
                px, py = _transform(point, state["matrix"])
                xs.append(px); ys.append(py)
            current = start = _transform((x, y), state["matrix"])
        elif op == b"h":
            current = start
        elif op in _PAINT_OPERATORS | {b"n"}:
            stroke = op in {b"S", b"s", b"B", b"B*", b"b", b"b*"} and not state["stroke_white"]
            fill = op in {b"f", b"F", b"f*", b"B", b"B*", b"b", b"b*"} and not state["fill_white"]
            if xs and (stroke or fill):
                a, b, c, d, _, _ = state["matrix"]
                trace, determinant = a*a+b*b+c*c+d*d, (a*d-b*c)**2
                singular = math.sqrt(max(0, (trace+math.sqrt(max(0, trace*trace-4*determinant)))/2))
                pad = state["width"] * singular / 2 if stroke else 0
                painted.append([min(xs)-pad, min(ys)-pad, max(xs)+pad, max(ys)+pad])
            xs, ys, current, start = [], [], None, None
        elif op == b"Do":
            reference = (_object(resources.get("/XObject", {})) or {})[values[0]]
            obj = _object(reference)
            if obj.get("/Subtype") == "/Form":
                identity = _identity(reference)
                if identity in ancestors:
                    raise ValueError("PDF path contains a recursive form cycle")
                nested_matrix = _matrix_product(state["matrix"], tuple(map(float, obj.get("/Matrix", (1, 0, 0, 1, 0, 0)))))
                painted.extend(_painted_bounds(obj, obj.get("/Resources", resources), reader, nested_matrix, ancestors | {identity}, depth+1, state))
    return painted


def _issue(report, code: str, message: str, **details) -> None:
    report["errors"].append({"code": code, "message": message, **details})


def _warning(report, code: str, message: str, **details) -> None:
    report["warnings"].append({"code": code, "message": message, **details})


def _page_spec(manifest: dict | None, report: dict) -> tuple[float, float, dict | None]:
    spec = (manifest or {}).get("page", {})
    if not isinstance(spec, dict):
        _issue(report, "invalid_page_spec", "Manifest page specification must be an object.")
        return 210, 297, None
    width, height = spec.get("width_mm", 210), spec.get("height_mm", 297)
    if not all(_finite(v) and v > 0 for v in (width, height)):
        _issue(report, "invalid_page_spec", "Expected page dimensions must be finite and positive.")
        width, height = 210, 297
    margins = spec.get("margins_mm")
    if margins is not None:
        if (not isinstance(margins, dict) or any(not _finite(margins.get(k)) or margins[k] < 0
                                               for k in ("top", "right", "bottom", "left"))
                or margins["left"] + margins["right"] >= width
                or margins["top"] + margins["bottom"] >= height):
            _issue(report, "invalid_margins", "Manifest margins must leave a finite positive writing area.")
            margins = None
    return float(width), float(height), margins


def _check_manifest(manifest: dict, report: dict, width: float, height: float, margins: dict | None) -> None:
    digest = manifest.get("pdf_sha256")
    if digest is None:
        report["checks"]["manifest_pdf_hash"] = "not_checked"
        _warning(report, "missing_pdf_hash", "The manifest does not contain a PDF hash.")
    elif not isinstance(digest, str) or digest.lower() != report["sha256"]:
        report["checks"]["manifest_pdf_hash"] = "fail"
        _issue(report, "pdf_hash_mismatch", "The exported PDF does not match the manifest hash.")
    else:
        report["checks"]["manifest_pdf_hash"] = "pass"
    if "page_count" in manifest and manifest["page_count"] != report["page_count"]:
        _issue(report, "page_count_mismatch", "PDF page count differs from the build manifest.")
    instances = manifest.get("instances")
    if instances is None:
        report["checks"]["instance_geometry"] = "not_checked"
    elif not isinstance(instances, list):
        _issue(report, "invalid_instances", "Manifest instances must be an array.")
        report["checks"]["instance_geometry"] = "fail"
    else:
        start_errors = len(report["errors"])
        hashes = Counter()
        for index, instance in enumerate(instances):
            if not isinstance(instance, dict):
                _issue(report, "invalid_instance", "Each instance must be an object.", instance=index)
                continue
            page = instance.get("page")
            if not isinstance(page, int) or isinstance(page, bool) or not 0 <= page < report["page_count"]:
                _issue(report, "invalid_instance_page", "An instance names a nonexistent PDF page.", instance=index)
            ink = instance.get("ink_bounds")
            if not _bounds(ink):
                _issue(report, "invalid_ink_bounds", "Instance ink bounds must be finite and ordered.", instance=index)
                continue
            if ink[0] < -.01 or ink[1] < -.01 or ink[2] > width + .01 or ink[3] > height + .01:
                _issue(report, "ink_outside_page", "Instance ink extends beyond the physical page.", instance=index)
            if margins is not None and (ink[0] < margins["left"] - .01 or ink[1] < margins["top"] - .01
                                        or ink[2] > width - margins["right"] + .01
                                        or ink[3] > height - margins["bottom"] + .01):
                _issue(report, "ink_outside_safe_area", "Instance ink extends beyond configured margins.", instance=index)
            if instance.get("representation") not in {"centerline", "clean_outline", "primitive"}:
                _issue(report, "invalid_representation", "An instance is not declared as vector geometry.", instance=index)
            if not isinstance(instance.get("token_id"), str) or not instance["token_id"]:
                _issue(report, "missing_instance_token", "An instance must map to a content token.", instance=index)
            geometry_hash = instance.get("geometry_hash")
            if isinstance(geometry_hash, str):
                hashes[(instance.get("glyph_id"), geometry_hash)] += 1
        report["checks"]["instance_geometry"] = "pass" if len(report["errors"]) == start_errors else "fail"
        report["instance_count"] = len(instances)
        repeated = [{"glyph_id": glyph, "geometry_hash": digest, "count": count}
                    for (glyph, digest), count in hashes.items() if count > 1]
        report["repeated_geometry"] = repeated
        if repeated:
            _warning(report, "repeated_geometry", "Some instances have identical geometry; this is a style diagnostic, not a failure.", groups=len(repeated))
        _check_content_accounting(manifest, instances, report)
    unresolved = manifest.get("unresolved_inputs", [])
    report["unresolved_inputs"] = unresolved
    if unresolved:
        _warning(report, "declared_missing_input", "The document records unresolved source inputs; those items are not claimed as answered.", count=len(unresolved) if isinstance(unresolved, list) else None)
    report["checks"].setdefault("content_accounted_for", "not_checked")
    report["checks"].setdefault("math_correctness", "not_checked")


def _check_content_accounting(manifest: dict, instances: list, report: dict) -> None:
    """Recount manifest instances against its role-aware coverage inventory.

    This checks the build's internal accounting, not the correctness of a source
    transcription or an answer. The PDF hash binds this inventory to the file
    that was inspected; the separately preserved document is still the semantic
    authority.
    """
    required_count, rendered_count = manifest.get("required_symbol_count"), manifest.get("rendered_symbol_count")
    inventory = manifest.get("coverage", {}).get("glyph_counts") if isinstance(manifest.get("coverage"), dict) else None
    if required_count is None and rendered_count is None and inventory is None:
        report["checks"]["content_accounted_for"] = "not_checked"
        return
    start_errors = len(report["errors"])
    if (not isinstance(required_count, int) or isinstance(required_count, bool)
            or not isinstance(rendered_count, int) or isinstance(rendered_count, bool)
            or required_count != len(instances) or rendered_count != len(instances)):
        _issue(report, "symbol_count_mismatch", "Required and rendered symbol counts must match the actual instance count.")
    try:
        ids = [instance["token_id"] for instance in instances]
        if len(set(ids)) != len(ids):
            _issue(report, "duplicate_instance_token", "Multiple glyph instances reuse the same content token ID.")
        expected = Counter()
        if not isinstance(inventory, list):
            raise ValueError("role-aware coverage inventory is missing")
        for record in inventory:
            key, count = (record["role"], record["text"]), record["required_count"]
            if not all(isinstance(value, str) and value for value in key) or key in expected or not isinstance(count, int) or isinstance(count, bool) or count <= 0:
                raise ValueError("coverage inventory contains invalid or duplicate symbol records")
            expected[key] = count
        actual = Counter((instance["role"], instance["text"]) for instance in instances)
        if actual != expected:
            _issue(report, "symbol_inventory_mismatch", "Rendered roles and symbols differ from the required inventory.",
                   missing=[{"role": r, "text": t, "count": n} for (r, t), n in (expected-actual).items()],
                   unexpected=[{"role": r, "text": t, "count": n} for (r, t), n in (actual-expected).items()])
    except (KeyError, TypeError, ValueError) as exc:
        _issue(report, "invalid_content_inventory", "Could not verify role-aware content accounting.", reason=str(exc))
    geometry_hash = manifest.get("geometry_hash")
    if geometry_hash is not None:
        try:
            actual_hash = hashlib.sha256(json.dumps(instances, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
            if geometry_hash != actual_hash:
                _issue(report, "instance_hash_mismatch", "Instance geometry differs from its recorded build hash.")
        except (ValueError, TypeError):
            _issue(report, "invalid_instance_hash_input", "Instance geometry is not finite JSON data.")
    report["checks"]["content_accounted_for"] = ("fail" if len(report["errors"]) != start_errors else
        "pass_with_declared_missing_input" if manifest.get("unresolved_inputs") else "pass")
    report["content_accounting_scope"] = "Manifest symbol inventory, unique token IDs, counts, and instance hash when supplied; source transcription and mathematical truth are not inferred."


def _render_pages(pdf_path: Path, preview_dir: Path | None, report: dict, margins: dict | None,
                  preview_dpi: float) -> None:
    document = pdfium.PdfDocument(str(pdf_path))
    try:
        if len(document) != report["page_count"]:
            _issue(report, "rasterizer_page_count", "Independent PDF reader and rasterizer disagree on page count.")
        for index in range(len(document)):
            record = report["pages"][index] if index < len(report["pages"]) else {"page": index}
            page = document[index]
            try:
                scale = preview_dpi / 72
                pixels_wide, pixels_high = math.ceil(page.get_width() * scale), math.ceil(page.get_height() * scale)
                if (min(pixels_wide, pixels_high) <= 0 or max(pixels_wide, pixels_high) > MAX_PREVIEW_EDGE
                        or pixels_wide * pixels_high > MAX_PREVIEW_PIXELS):
                    raise ValueError("Requested page preview exceeds the 40 megapixel or 16000 pixel edge limit")
                # Render directly from vector geometry at the requested scale.
                # PDFium path antialiasing is enabled; no existing PNG is enlarged.
                bitmap = page.render(scale=scale, no_smoothpath=False, no_smoothtext=False, no_smoothimage=False)
                try:
                    raster = bitmap.to_pil().convert("RGB")
                    pixels = np.asarray(raster)
                    ink = np.any(pixels < 245, axis=2)
                    record["rasterized"] = True
                    record["preview_dpi"] = preview_dpi
                    record["preview_pixels"] = list(raster.size)
                    record["nonwhite_pixel_count"] = int(ink.sum())
                    if not ink.any():
                        _issue(report, "empty_page", "The exported page has no visible ink after rasterization.", page=index)
                    else:
                        # Row/column reductions avoid allocating coordinate
                        # pairs for every dark pixel in a high-resolution page.
                        xx = np.flatnonzero(np.any(ink, axis=0))
                        yy = np.flatnonzero(np.any(ink, axis=1))
                        bounds = [float(xx[0]), float(yy[0]), float(xx[-1] + 1), float(yy[-1] + 1)]
                        bounds = [round(value * 25.4 / preview_dpi, 5) for value in bounds]
                        record["visible_ink_bounds_mm"] = bounds
                        # Allow one raster pixel at the margin to cover antialiasing.
                        tolerance = 25.4 / preview_dpi
                        w, h = page.get_width() * PT_TO_MM, page.get_height() * PT_TO_MM
                        if margins is not None and (bounds[0] < margins["left"] - tolerance
                                                    or bounds[1] < margins["top"] - tolerance
                                                    or bounds[2] > w - margins["right"] + tolerance
                                                    or bounds[3] > h - margins["bottom"] + tolerance):
                            _issue(report, "visible_ink_outside_safe_area", "Rasterized PDF ink extends beyond configured margins.", page=index)
                    if preview_dir is not None:
                        preview = preview_dir / f"page-{index + 1:03}.png"
                        raster.save(preview, dpi=(preview_dpi, preview_dpi))
                        record["preview"] = str(preview)
                finally:
                    bitmap.close()
            except Exception as exc:
                record["rasterized"] = False
                _issue(report, "page_rasterization_failed", "A PDF page could not be rasterized.", page=index, reason=str(exc))
            finally:
                page.close()
    finally:
        document.close()


def check_pdf(pdf_path: Path, manifest: dict | None = None, preview_dir: Path | None = None,
              preview_dpi: int | float = PREVIEW_DPI) -> dict:
    """Inspect the real exported PDF; return structured checks and diagnostics.

    Bounds in manifests and reports use millimeters from the top-left. A4 is
    expected by default. Passing a manifest permits another explicit page size.
    The default fresh-ink policy rejects raster objects and font text operators.
    Previews are antialiased PDF rasterizations at 300 dpi by default; their
    resolution never changes the PDF's vector paths. Whole pages are limited to
    600 dpi and 40 megapixels to bound bitmap memory use.
    """
    preview_dpi = _preview_resolution(preview_dpi)
    pdf_path = Path(pdf_path).expanduser().resolve()
    if manifest is not None and not isinstance(manifest, dict):
        raise InkformError("invalid_manifest", "Build manifest must be an object.")
    report = {"ok": False, "pdf_path": str(pdf_path), "pages": [], "errors": [], "warnings": [],
              "checks": {}, "style_review": "pending_user_feedback", "handwriting_raster_objects": 0,
              "vector_path_count": 0, "font_text_operators": 0}
    try:
        report["sha256"] = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
        reader = PdfReader(str(pdf_path))
        if reader.is_encrypted:
            raise InkformError("encrypted_pdf", "Encrypted PDFs cannot be inspected as a fresh-ink export.")
        report["page_count"] = len(reader.pages)
    except InkformError:
        raise
    except Exception as exc:
        raise InkformError("invalid_pdf", "Could not open the exported PDF.", {"path": str(pdf_path), "reason": str(exc)}) from exc
    if not reader.pages:
        _issue(report, "no_pages", "The exported PDF contains no pages.")
    width, height, margins = _page_spec(manifest, report)
    report["expected_page_mm"] = [width, height]
    for index, page in enumerate(reader.pages):
        record = {"page": index}
        report["pages"].append(record)
        try:
            unit = float(page.get("/UserUnit", 1))
            media, crop = [float(v) for v in page.mediabox], [float(v) for v in page.cropbox]
            if not _bounds(media) or not _bounds(crop) or not math.isfinite(unit) or unit <= 0:
                raise ValueError("page boxes and user units must be finite and ordered")
            rotation = int(page.get("/Rotate", 0)) % 360
            actual = [(media[2] - media[0]) * unit * PT_TO_MM, (media[3] - media[1]) * unit * PT_TO_MM]
            if rotation in (90, 270):
                actual.reverse()
            record["media_box_pt"] = media
            record["crop_box_pt"] = crop
            record["width_mm"], record["height_mm"] = [round(v, 7) for v in actual]
            record["rotation"] = rotation
            if rotation not in (0, 90, 180, 270) or any(abs(a - b) > .01 for a, b in zip(actual, (width, height))):
                _issue(report, "page_size_mismatch", "PDF page dimensions differ from the expected physical size.", page=index, actual_mm=actual)
            if any(abs(a - b) * unit * PT_TO_MM > .01 for a, b in zip(media, crop)):
                _issue(report, "cropped_page", "PDF CropBox differs from its physical MediaBox.", page=index)
        except Exception as exc:
            _issue(report, "invalid_page_geometry", "PDF page geometry is invalid.", page=index, reason=str(exc))
        try:
            resources = page.get("/Resources", {})
            record.update(_inspect_resources(resources))
            content = _inspect_content(page.get_contents(), resources, reader)
            record.update({key: content[key] for key in ("vector_paint_operators", "path_operators", "font_text_operators", "inline_images", "clipping_operators", "nonfinite_operands")})
            report["handwriting_raster_objects"] += record["image_xobjects"] + record["inline_images"]
            report["vector_path_count"] += content["vector_paint_operators"]
            report["font_text_operators"] += content["font_text_operators"]
            if content["nonfinite_operands"]:
                _issue(report, "nonfinite_pdf_operand", "PDF content contains non-finite numeric operands.", page=index)
            if content["vector_paint_operators"] == 0 or content["path_operators"] == 0:
                _issue(report, "no_vector_ink", "PDF page contains no painted vector paths.", page=index)
            if content["clipping_operators"]:
                _warning(report, "clipping_present", "PDF uses clipping paths; visual inspection is needed to distinguish intended masks from clipped ink.", page=index)
            painted = _painted_bounds(page.get_contents(), resources, reader)
            if any(not _bounds(box) for box in painted):
                _issue(report, "nonfinite_pdf_path", "Painted PDF paths exceed finite coordinate precision.", page=index)
            elif painted and record.get("rotation") == 0 and "media_box_pt" in record:
                # PDF paths have bottom-left coordinates. Include MediaBox
                # origin and UserUnit before comparing with the physical plan.
                media = record["media_box_pt"]
                extents = [min(b[0] for b in painted), min(b[1] for b in painted),
                           max(b[2] for b in painted), max(b[3] for b in painted)]
                bounds = [(extents[0]-media[0])*unit*PT_TO_MM, (media[3]-extents[3])*unit*PT_TO_MM,
                          (extents[2]-media[0])*unit*PT_TO_MM, (media[3]-extents[1])*unit*PT_TO_MM]
                record["painted_path_bounds_mm"] = [round(v, 7) for v in bounds]
                if bounds[0] < -.01 or bounds[1] < -.01 or bounds[2] > width + .01 or bounds[3] > height + .01:
                    _issue(report, "pdf_path_outside_page", "Painted PDF geometry extends beyond the physical page before clipping.", page=index)
                if margins is not None and (bounds[0] < margins["left"] - .01 or bounds[1] < margins["top"] - .01
                                            or bounds[2] > width-margins["right"] + .01 or bounds[3] > height-margins["bottom"] + .01):
                    _issue(report, "pdf_path_outside_safe_area", "Painted PDF geometry extends beyond configured margins.", page=index)
        except Exception as exc:
            _issue(report, "invalid_pdf_content", "Could not inspect PDF resources or content streams.", page=index, reason=str(exc))
    if report["handwriting_raster_objects"]:
        _issue(report, "raster_handwriting", "Fresh-ink exports must not contain raster image objects.", count=report["handwriting_raster_objects"])
    if report["font_text_operators"]:
        _issue(report, "font_text_present", "Fresh-ink exports must draw glyph geometry instead of font text.", count=report["font_text_operators"])
    if manifest is not None:
        _check_manifest(manifest, report, width, height, margins)
    else:
        report["checks"].update({"manifest_pdf_hash": "not_checked", "instance_geometry": "not_checked",
                                  "content_accounted_for": "not_checked", "math_correctness": "not_checked"})
    if preview_dir is not None:
        preview_dir = Path(preview_dir).expanduser().resolve()
        preview_dir.mkdir(parents=True, exist_ok=True)
    try:
        _render_pages(pdf_path, preview_dir, report, margins, preview_dpi)
    except Exception as exc:
        _issue(report, "pdf_rasterization_failed", "The independent rasterizer could not open this PDF.", reason=str(exc))
    codes = {error["code"] for error in report["errors"]}
    for check, failures in {
        "page_geometry": {"no_pages", "invalid_page_spec", "invalid_page_geometry", "page_size_mismatch", "cropped_page", "page_count_mismatch"},
        "vector_handwriting": {"raster_handwriting", "no_vector_ink", "font_text_present", "invalid_pdf_content"},
        "finite_content_streams": {"nonfinite_pdf_operand", "nonfinite_pdf_path", "invalid_pdf_content"},
        "pdf_rasterization": {"pdf_rasterization_failed", "page_rasterization_failed", "rasterizer_page_count"},
        "visible_ink": {"empty_page", "no_pages", "pdf_rasterization_failed", "page_rasterization_failed"},
    }.items():
        report["checks"][check] = "fail" if codes & failures else "pass"
    report["checks"]["ink_inside_safe_area"] = ("not_checked" if margins is None else
        "fail" if codes & {"invalid_margins", "ink_outside_safe_area", "ink_outside_page", "visible_ink_outside_safe_area", "invalid_ink_bounds", "pdf_path_outside_page", "pdf_path_outside_safe_area"} else "pass")
    report["checks"]["clipping_and_collisions"] = "requires_visual_review"
    report["ok"] = not report["errors"]
    return report


def _vector_comparison(reference: Path, candidate: Path, destination: Path) -> dict:
    """Merge original page content at actual physical size, without rasterizing.

    The extra top strip uses an ordinary diagnostic font. It is intentionally
    separate from the source handwriting and is not a fresh-ink document.
    """
    from reportlab.pdfgen.canvas import Canvas

    readers = [PdfReader(str(path)) for path in (reference, candidate)]
    sources = []
    for reader in readers:
        pages = []
        for page in reader.pages:
            # Normalize only the in-memory reader objects. Original PDF bytes
            # remain untouched, including when a source carries /Rotate.
            if page.rotation:
                page.transfer_rotation_to_content()
            unit = float(page.get("/UserUnit", 1))
            box = [float(v) for v in page.mediabox]
            width, height = (box[2]-box[0])*unit, (box[3]-box[1])*unit
            if not _bounds(box) or not all(math.isfinite(v) and v > 0 for v in (unit, width, height)):
                raise InkformError("invalid_comparison_page", "Comparison sources need finite physical page dimensions.")
            pages.append({"page": page, "width": width, "height": height, "origin": box[:2], "unit": unit})
        sources.append(pages)
    count = max(map(len, sources))
    if not count:
        raise InkformError("empty_comparison", "Comparison needs at least one source page.")
    writer = PdfWriter()
    label_strip = 12 / PT_TO_MM
    records = []
    for index in range(count):
        pair = [pages[index] if index < len(pages) else None for pages in sources]
        dimensions = []
        for side, source in enumerate(pair):
            fallback = source or (sources[side][0] if sources[side] else next(p for p in pair if p))
            dimensions.append((fallback["width"], fallback["height"]))
        width = dimensions[0][0] + dimensions[1][0]
        content_height = max(height for _, height in dimensions)
        target = writer.add_blank_page(width=width, height=content_height+label_strip)
        x = 0
        for source, (cell_width, _) in zip(pair, dimensions):
            if source is not None:
                transform = (Transformation().translate(-source["origin"][0], -source["origin"][1])
                             .scale(source["unit"]).translate(x, content_height-source["height"]))
                target.merge_transformed_page(source["page"], transform, over=True, expand=False)
            x += cell_width
        labels_buffer = io.BytesIO()
        labels = Canvas(labels_buffer, pagesize=(width, content_height+label_strip), invariant=1)
        labels.setFont("Helvetica", 9)
        x = 0
        for name, source, (cell_width, _) in zip(("Reference", "Candidate"), pair, dimensions):
            detail = f"page {index+1} - original physical size" if source is not None else "no corresponding page"
            labels.drawString(x + 6/PT_TO_MM, content_height + 4/PT_TO_MM, f"{name}: {detail}")
            x += cell_width
        labels.save()
        target.merge_page(PdfReader(labels_buffer).pages[0], over=True)
        records.append({"page": index, "width_mm": round(width*PT_TO_MM, 7),
                        "height_mm": round((content_height+label_strip)*PT_TO_MM, 7),
                        "reference_page": index if pair[0] else None, "candidate_page": index if pair[1] else None})
    writer.add_metadata({"/Title": "Inkform vector comparison", "/Author": "Inkform",
                         "/Subject": "Diagnostic comparison; source pages retain original physical scale"})
    writer._root_object[NameObject("/ViewerPreferences")] = DictionaryObject({NameObject("/PrintScaling"): NameObject("/None")})
    output = io.BytesIO()
    writer.write(output)
    destination.write_bytes(output.getvalue())
    return {"page_count": count, "pages": records, "source_scale": "original_physical_size",
            "diagnostic_font_labels": True, "label_strip_mm": 12,
            "handwriting_validation": "Source PDFs are checked separately; this comparison has diagnostic font labels."}


def compare(reference: Path, candidate: Path, output_dir: Path,
            preview_dpi: int | float = PREVIEW_DPI) -> dict:
    """Create a vector comparison, same-DPI previews, and structural reports."""
    preview_dpi = _preview_resolution(preview_dpi)
    output_dir = Path(output_dir).expanduser().resolve()
    reference, candidate = Path(reference).expanduser().resolve(), Path(candidate).expanduser().resolve()
    vector_path = output_dir / "comparison.pdf"
    if vector_path in (reference, candidate):
        raise InkformError("input_output_conflict", "Comparison output must not replace either source PDF.")
    output_dir.mkdir(parents=True, exist_ok=True)
    reference_report = check_pdf(reference, preview_dir=output_dir / "reference", preview_dpi=preview_dpi)
    candidate_report = check_pdf(candidate, preview_dir=output_dir / "candidate", preview_dpi=preview_dpi)
    vector_comparison = _vector_comparison(reference, candidate, vector_path)
    pairs = []
    for index in range(max(reference_report["page_count"], candidate_report["page_count"])):
        images = []
        for report in (reference_report, candidate_report):
            preview = report["pages"][index].get("preview") if index < len(report["pages"]) else None
            images.append(Image.open(preview).convert("RGB") if preview else None)
        available = [im for im in images if im is not None]
        if not available:
            continue
        cell_width, cell_height = max(im.width for im in available), max(im.height for im in available)
        gap, label_height = 24, 36
        canvas = Image.new("RGB", (cell_width * 2 + gap * 3, cell_height + gap * 2 + label_height), "#e9e9e9")
        draw = ImageDraw.Draw(canvas)
        for column, (label, im) in enumerate(zip(("Reference", "Candidate"), images)):
            x = gap + column * (cell_width + gap)
            draw.text((x, gap), f"{label} — page {index + 1} — {preview_dpi:g} dpi (same physical scale)", fill="#252525")
            if im is not None:
                canvas.paste(im, (x, gap + label_height))
                im.close()
            else:
                draw.text((x, gap + label_height), "No corresponding page", fill="#555555")
        pair = output_dir / f"compare-page-{index + 1:03}.png"
        canvas.save(pair, dpi=(preview_dpi, preview_dpi))
        canvas.close()
        pairs.append(str(pair))
    result = {
        "reference": reference_report, "candidate": candidate_report,
        "common_preview_dpi": preview_dpi, "page_comparisons": pairs,
        "vector_comparison_pdf": str(vector_path), "vector_comparison": vector_comparison,
        "page_count_equal": reference_report["page_count"] == candidate_report["page_count"],
        "style_review": "pending_user_feedback",
        "review_axes": ["letter identity", "resemblance", "fresh ink appearance", "readability", "spacing", "math alignment"],
        "notes": ["The comparison PDF merges original source content at physical size without rasterization; its separate label strip uses a diagnostic print font.",
                  "Images share a fixed dots-per-inch scale; no per-page scaling was applied.",
                  "Structural diagnostics describe each PDF; they do not measure handwriting similarity.",
                  "Physical print appearance still requires an A4 actual-size print proof."],
    }
    result["report_path"] = str(output_dir / "comparison.json")
    Path(result["report_path"]).write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return result
