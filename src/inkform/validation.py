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
from .repetition import audit_repetition, is_graph_primitive, item_distance, shape_descriptor
from .word_patterns import audit_word_patterns


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
            _warning(report, "repeated_geometry", "Stored geometry hashes repeat; the independent path audit determines whether visible shapes repeat.", groups=len(repeated))
        _check_content_accounting(manifest, instances, report)

    unresolved = manifest.get("unresolved_inputs", [])
    report["unresolved_inputs"] = unresolved
    if unresolved:
        _warning(report, "declared_missing_input", "The document records unresolved source inputs; those items are not claimed as answered.", count=len(unresolved) if isinstance(unresolved, list) else None)
    report["checks"].setdefault("content_accounted_for", "not_checked")
    report["checks"].setdefault("math_correctness", "not_checked")
    report["checks"].setdefault("repetition", "not_checked")


def _emitted_glyphs(reader, manifest):
    """Decode ordinal marked glyph paints directly from the exported PDF.

    Labels identify only group boundaries. Roles/text come from the existing
    semantic manifest, while every curve and pen width comes from PDF operators.
    Unmarked older exports return None rather than claiming an emitted-ink audit.
    """
    expected = manifest.get("instances")
    if not isinstance(expected, list):
        return None
    contract = manifest.get("emitted_glyph_groups")
    if contract is not None and (not isinstance(contract, dict) or contract.get("version") != "inkform-ordinal-paths-1"
                                 or contract.get("count") != len(expected)):
        raise ValueError("Invalid emitted glyph group contract")
    result, marked_pages = [], 0
    unmarked_ink, unsupported_paint_state = False, False
    for page_index, page in enumerate(reader.pages):
        page_expected = [item for item in expected if isinstance(item, dict) and item.get("page") == page_index]
        media = list(map(float, page.mediabox))
        unit = float(page.get("/UserUnit", 1))
        state = {"matrix": (1, 0, 0, 1, 0, 0), "width": 1., "stroke_white": False, "fill_white": False, "cap": 0, "join": 0, "dash": []}
        states, marks, paths, groups = [], [], [], []
        group, group_knockout = None, False

        def point(values):
            x, y = _transform(tuple(map(float, values)), state["matrix"])
            return [(x-media[0])*unit*PT_TO_MM, (media[3]-y)*unit*PT_TO_MM]

        for values, op in ContentStream(page.get_contents(), reader).operations:
            if _nonfinite_operand(values):
                raise ValueError("Non-finite emitted glyph operand")
            if op in {b"BMC", b"BDC"}:
                name = str(values[0]) if values else ""
                marks.append(name)
                if name == "/InkformGlyph":
                    if group is not None:
                        raise ValueError("Nested Inkform glyph markers")
                    group, paths, group_knockout = [], [], False
            elif op == b"EMC":
                if not marks:
                    raise ValueError("Unbalanced PDF marked content")
                if marks.pop() == "/InkformGlyph":
                    if group is None or not group:
                        raise ValueError("A marked glyph has no visible vector ink")
                    groups.append((group, group_knockout))
                    group, paths, group_knockout = None, [], False
            elif op == b"q":
                states.append(state.copy())
            elif op == b"Q":
                if not states:
                    raise ValueError("Unbalanced PDF graphics state")
                state = states.pop()
            elif op == b"cm":
                state["matrix"] = _matrix_product(state["matrix"], tuple(map(float, values)))
            elif op == b"w":
                state["width"] = abs(float(values[0]))
            elif op == b"J":
                state["cap"] = int(values[0])
            elif op == b"j":
                state["join"] = int(values[0])
            elif op == b"d":
                state["dash"] = list(values[0])
            elif op in {b"W", b"W*", b"gs"}:
                unsupported_paint_state = True
            elif op in {b"g", b"G", b"rg", b"RG", b"k", b"K"}:
                numeric = list(map(float, values))
                white = all(v >= .999 for v in numeric) if op.lower() != b"k" else all(v <= .001 for v in numeric)
                state["stroke_white" if op in {b"G", b"RG", b"K"} else "fill_white"] = white
            elif group is not None:
                if op == b"m":
                    paths.append({"start": point(values), "segments": [], "closed": False})
                elif op == b"l":
                    if not paths:
                        raise ValueError("Emitted glyph line has no starting point")
                    paths[-1]["segments"].append({"type": "line", "end": point(values)})
                elif op in {b"c", b"v", b"y"}:
                    if not paths:
                        raise ValueError("Emitted glyph curve has no starting point")
                    current = paths[-1]["segments"][-1]["end"] if paths[-1]["segments"] else paths[-1]["start"]
                    controls = [point(values[i:i+2]) for i in range(0, len(values), 2)]
                    if op == b"v":
                        controls.insert(0, current)
                    elif op == b"y":
                        controls.append(controls[-1])
                    paths[-1]["segments"].append({"type": "cubic", "control1": controls[0], "control2": controls[1], "end": controls[2]})
                elif op == b"re":
                    x, y, w, h = map(float, values)
                    rectangle = [point((x, y)), point((x+w, y)), point((x+w, y+h)), point((x, y+h))]
                    paths.append({"start": rectangle[0], "segments": [{"type": "line", "end": p} for p in rectangle[1:]], "closed": True})
                elif op == b"h":
                    if paths:
                        paths[-1]["closed"] = True
                elif op in _PAINT_OPERATORS | {b"n"}:
                    if op in {b"s", b"b", b"b*"} and paths:
                        paths[-1]["closed"] = True
                    fills = op in {b"f", b"F", b"f*", b"B", b"B*", b"b", b"b*"}
                    strokes = op in {b"S", b"s", b"B", b"B*", b"b", b"b*"}
                    if paths and group_knockout and (fills or strokes):
                        raise ValueError("No additional paint is allowed after an open marker knockout")
                    if paths and strokes and state["stroke_white"]:
                        raise ValueError("White strokes cannot hide ink in a marked glyph")
                    if paths and fills and state["fill_white"]:
                        expected_item = page_expected[len(groups)] if len(groups) < len(page_expected) else {}
                        allowed = (expected_item.get("paper_knockout") == "open_graph_marker"
                                   and expected_item.get("role") == "graph.marker"
                                   and expected_item.get("graph_kind") == "marker"
                                   and is_graph_primitive(expected_item)
                                   and op == b"B*" and not group and not state["stroke_white"]
                                   and len(paths) == 1 and paths[0].get("closed")
                                   and (len(paths[0]["segments"]) == 4 or (
                                       expected_item.get("graph_trace", {}).get("version") == "reviewed-graph-trace-1"
                                       and expected_item.get("graph_trace", {}).get("role") == "marker_open"
                                       and 3 <= len(paths[0]["segments"]) <= 2048))
                                   and all(segment["type"] == "cubic" for segment in paths[0]["segments"]))
                        if not allowed:
                            raise ValueError("White fill is only allowed as the single simultaneous fill/stroke of an explicit open graph marker")
                        group_knockout = True
                    fill = fills and not state["fill_white"]
                    stroke = strokes and not state["stroke_white"]
                    if paths and fill:
                        if op not in {b"f*", b"B*", b"b*"}:
                            raise ValueError("Marked glyphs require auditable even-odd fills")
                        group.append({"representation": "clean_outline", "strokes": [{**p, "closed": True} for p in paths], "pen_width_mm": .25})
                    if paths and stroke:
                        if state["cap"] != 1 or state["join"] != 1 or state["dash"]:
                            raise ValueError("Marked glyphs require solid round-cap round-join pens")
                        a, b, c, d, _, _ = state["matrix"]
                        sx, sy = math.hypot(a, b), math.hypot(c, d)
                        if abs(sx-sy) > 1e-6 or abs(a*c+b*d) > 1e-6:
                            raise ValueError("Nonuniformly transformed pens are unsupported in emitted glyph audit")
                        width = state["width"]*sx*unit*PT_TO_MM
                        if group and group[-1]["representation"] == "centerline" and abs(group[-1]["pen_width_mm"]-width) < 1e-9:
                            group[-1]["strokes"].extend(paths)
                        else:
                            group.append({"representation": "centerline", "strokes": paths, "pen_width_mm": width})
                    paths = []
                elif op in {b"Do", b"INLINE IMAGE"} | _TEXT_OPERATORS:
                    raise ValueError("A marked glyph contains non-path ink")
            elif op in _PAINT_OPERATORS | {b"Do", b"INLINE IMAGE"}:
                unmarked_ink = True
        if group is not None or marks:
            raise ValueError("Unclosed PDF marked content")
        if groups:
            marked_pages += 1
            if len(groups) != len(page_expected):
                raise ValueError("Emitted glyph group count differs from manifest instances")
            for expected_item, (paints, knockout) in zip(page_expected, groups):
                result.append({"token_id": expected_item.get("token_id"), "role": expected_item.get("role"),
                               "text": expected_item.get("text"), "page": page_index, "_ink_paints": paints,
                               **({"paper_knockout": "open_graph_marker"} if knockout else {}),
                               **{key: expected_item[key] for key in ("word_id", "word_position", "semantic", "content_kind",
                                   "graph_id", "graph_kind", "graph_element_id", "graph_data_anchors", "graph_plot_anchors_mm", "graph_trace", "source_refs") if key in expected_item},
                               **({"nominal_size_mm": expected_item["nominal_size_mm"]} if "nominal_size_mm" in expected_item else {})})
    if not marked_pages:
        if contract is not None:
            raise ValueError("Export declares glyph groups but its PDF has none")
        return None
    if unmarked_ink or unsupported_paint_state:
        raise ValueError("Marked exports contain ungrouped ink or unsupported clipping/graphics-state operations")
    if marked_pages != len(reader.pages):
        raise ValueError("Only some pages contain auditable glyph groups")
    return result


def _check_repetition(manifest, instances, report, *, geometry_source="manifest_vector_paths"):
    """Recalculate visible shapes; stored novelty decisions are never evidence."""
    variation = manifest.get("variation", {})
    settings = manifest.get("repetition_policy", {})
    try:
        if not isinstance(settings, dict):
            raise ValueError("Repetition policy must be an object")
        policy = {key: settings[key] for key in ("threshold_mm", "adjacent_pages", "strict_near") if key in settings}
        if isinstance(variation, dict) and "amount" in variation:
            policy["amount"] = variation["amount"]
        if isinstance(variation, dict) and "mode" in variation:
            policy["mode"] = variation["mode"]
        diagnostic = audit_repetition(instances, policy=policy)
    except (TypeError, ValueError, OverflowError) as exc:
        report["checks"]["repetition"] = "fail"
        _issue(report, "invalid_repetition_audit", "Could not audit repeated ink geometry.", reason=str(exc))
        return
    diagnostic["geometry_source"] = geometry_source
    diagnostic["pdf_hash_binding"] = report["checks"].get("manifest_pdf_hash", "not_checked")
    report["repetition"] = diagnostic
    report["checks"]["repetition"] = diagnostic["status"]
    if diagnostic["status"] == "fail":
        _issue(report, "repeated_ink_geometry", "Repeated ink exceeds the declared physical-shape threshold on the same or adjacent page.",
               exact_pairs=diagnostic["exact_count"], near_pairs=diagnostic["near_count"],
               threshold_mm=diagnostic["threshold_mm"])
    elif diagnostic["status"] == "review_needed" and diagnostic["mode"] == "observed" and (diagnostic["exact_count"] or diagnostic["near_count"]):
        _warning(report, "observed_source_reuse", "Reviewed source forms are reused. Their original curves were preserved; collect more real examples to increase variety.",
                 exact_pairs=diagnostic["exact_count"], near_pairs=diagnostic["near_count"],
                 threshold_mm=diagnostic["threshold_mm"])
    elif diagnostic["status"] in {"not_checked", "partial"}:
        _warning(report, "incomplete_repetition_audit", "This manifest does not provide auditable paths and semantic roles for every ink instance.",
                 checked=diagnostic["checked_instance_count"], unchecked=len(diagnostic["unchecked_instances"]))
    if diagnostic["limited_count"]:
        _warning(report, "limited_shape_space", "Simple punctuation or bars remain close in shape; the audit does not force them into different symbols.",
                 pairs=diagnostic["limited_count"])
    try:
        word_diagnostic = audit_word_patterns(instances, amount=variation.get("amount") if isinstance(variation, dict) else None,
                                              adjacent_pages=policy.get("adjacent_pages", 1), mode=policy.get("mode", "experimental"))
        word_diagnostic["geometry_source"] = geometry_source
        word_diagnostic["pdf_hash_binding"] = report["checks"].get("manifest_pdf_hash", "not_checked")
        report["word_patterns"] = word_diagnostic
        report["checks"]["word_patterns"] = word_diagnostic["status"]
        if word_diagnostic["status"] == "fail":
            _issue(report, "repeated_word_patterns", "Repeated words reuse too many structurally similar letter forms after removing scale and slant.",
                   repeated_pairs=word_diagnostic["repeated_word_count"], threshold_mm=word_diagnostic["threshold_mm"])
        elif word_diagnostic["status"] == "review_needed":
            _warning(report, "observed_word_reuse", "Some words reuse similar source letter sequences. This is reported without inventing extra curves or new letter constructions.",
                     repeated_pairs=word_diagnostic["repeated_word_count"], threshold_mm=word_diagnostic["threshold_mm"])
        elif word_diagnostic["status"] in {"partial", "not_checked"}:
            _warning(report, "incomplete_word_pattern_audit", "Lexical boundaries are missing; repeated word constructions were not fully audited.",
                     ungrouped=len(word_diagnostic["ungrouped_tokens"]))
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        report["checks"]["word_patterns"] = "fail"
        _issue(report, "invalid_word_pattern_audit", "Could not audit repeated word constructions.", reason=str(exc))


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
    semantic = [item for item in instances if item.get("semantic", True) is not False]
    if any("semantic" in item and not isinstance(item["semantic"], bool) for item in instances):
        _issue(report, "invalid_semantic_flag", "Instance semantic flags must be booleans.")
    if (not isinstance(required_count, int) or isinstance(required_count, bool)
            or not isinstance(rendered_count, int) or isinstance(rendered_count, bool)
            or required_count != len(semantic) or rendered_count != len(semantic)):
        _issue(report, "symbol_count_mismatch", "Required and rendered symbol counts must match the semantic instance count.")
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
        actual = Counter((instance["role"], instance["text"]) for instance in semantic)
        if actual != expected:
            _issue(report, "symbol_inventory_mismatch", "Rendered roles and symbols differ from the required inventory.",
                   missing=[{"role": r, "text": t, "count": n} for (r, t), n in (expected-actual).items()],
                   unexpected=[{"role": r, "text": t, "count": n} for (r, t), n in (actual-expected).items()])
    except (KeyError, TypeError, ValueError) as exc:
        _issue(report, "invalid_content_inventory", "Could not verify role-aware content accounting.", reason=str(exc))
    _check_correction_accounting(manifest, instances, report)
    _check_graph_accounting(manifest, instances, report)
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
    report["content_accounting_scope"] = "Semantic symbols, declared corrections, explicit graph data/anchors, unique token IDs, counts, and instance hash when supplied; source transcription and mathematical truth are not inferred."


def _check_correction_accounting(manifest: dict, instances: list, report: dict) -> None:
    """Every extra ink token must belong to a declared, internally exact edit."""
    from .document import clusters, validate_expression, math_text, math_ink_order, math_restart_fragments

    auxiliary = [item for item in instances if item.get("semantic", True) is False and not is_graph_primitive(item)]
    records = manifest.get("corrections", [])
    if not auxiliary and not records:
        return
    try:
        if not isinstance(records, list) or not records:
            raise ValueError("nonsemantic ink needs correction records")
        if manifest.get("visible_symbol_count") != len(instances):
            raise ValueError("visible_symbol_count must include deleted text and marks")
        by_token = {item["token_id"]: item for item in instances}
        used_tokens, edit_ids = set(), set()
        declared_auxiliary = set()
        for record in records:
            identity = record["id"]
            if not isinstance(identity, str) or not identity or identity in edit_ids:
                raise ValueError("correction IDs must be nonempty and unique")
            edit_ids.add(identity)
            if record["kind"] not in {"explicit", "sampled_restart"}:
                raise ValueError("unsupported correction kind")
            if not isinstance(record["deleted"], str) or not record["deleted"].strip() or not isinstance(record["replacement"], str):
                raise ValueError("corrections need deleted text and a replacement string")
            content_type = record.get("content_type", "text")
            if content_type not in {"text", "math"}:
                raise ValueError("correction content must be typed text or math")
            if content_type == "math":
                for part in ("deleted", "replacement"):
                    expr = record[part + "_expression"]
                    validate_expression(expr)
                    if record[part] != math_text(expr):
                        raise ValueError("math correction text must describe its exact expression")
            if record["kind"] == "sampled_restart":
                if content_type == "math":
                    if (record.get("attempt_basis") != "estimated_partial_fragment" or record.get("attempt_observed") is not False
                            or record["deleted_expression"] not in math_restart_fragments(record["replacement_expression"])):
                        raise ValueError("sampled math restarts must be declared estimated proper fragments of the intended expression")
                elif record.get("attempt_basis") == "estimated_partial_word_restart":
                    if (record.get("attempt_observed") is not False or record["deleted"] == record["replacement"]
                            or not record["replacement"].startswith(record["deleted"])
                            or not any(char.isalpha() for char in record["deleted"])):
                        raise ValueError("sampled partial restarts must be declared estimates and proper prefixes of the intended word")
                elif record["deleted"] != record["replacement"]:
                    # Historical whole-word restart exports remain checkable.
                    raise ValueError("sampled restarts need a declared partial attempt or the historical repeated word")
            for part in ("deleted", "mark", "replacement"):
                tokens = record[part + "_token_ids"]
                if not isinstance(tokens, list) or not all(isinstance(t, str) for t in tokens):
                    raise ValueError("correction token lists must be arrays of strings")
                if len(set(tokens)) != len(tokens) or used_tokens.intersection(tokens):
                    raise ValueError("correction token lists must be disjoint")
                used_tokens.update(tokens)
                items = [by_token[token] for token in tokens]
                if any(item.get("correction_id") != identity or item.get("correction_part") != part
                       or (item.get("semantic", True) is False) != (part != "replacement") for item in items):
                    raise ValueError("correction token flags do not match their ledger")
                if part != "replacement":
                    declared_auxiliary.update(tokens)
                if part == "mark":
                    if not items or any(item["role"] != "mark.correction" or item["text"] != "crossout"
                        or item["glyph_id"] != record["glyph_id"] or item.get("source_refs") != record["source_refs"] for item in items):
                        raise ValueError("each correction needs its declared sampled mark")
                else:
                    expected = (math_ink_order(record[part + "_expression"]) if content_type == "math" else
                                [("text", c) for c in clusters(record[part]) if not c.isspace()])
                    if [(item["role"], item["text"]) for item in items] != expected:
                        raise ValueError("correction text does not match its visible glyphs")
        if declared_auxiliary != {item["token_id"] for item in auxiliary}:
            raise ValueError("undeclared or missing nonsemantic ink")
        if {item["token_id"] for item in instances if "correction_id" in item} != used_tokens:
            raise ValueError("undeclared correction references")
    except (KeyError, TypeError, ValueError, InkformError) as exc:
        _issue(report, "invalid_correction_inventory", "Could not account for correction ink.", reason=str(exc))


def _check_graph_accounting(manifest, instances, report, *, emitted=False):
    """Bind every graph primitive and data anchor to an explicit graph spec.

    Graph axes/curves may share geometry because their coordinates carry data.
    That exception is never granted merely by relabeling a handwriting token:
    an exact primitive inventory, data mapping, anchors and marker semantics
    must agree. The emitted pass checks actual PDF path coordinates as well.
    """
    from .graphs import _curve_stroke, graph_axis_anchors, graph_requirements, validate_graph, validate_graph_style
    from .graph_traces import (axis_trace_path, curve_trace_path, marker_trace_path, path_distance,
                               path_samples, paths_touch, resolve_trace, tick_trace_path, trace_candidates)
    records = manifest.get("graphs", [])
    primitives = [item for item in instances if is_graph_primitive(item)]
    attributed = [item for item in instances if "graph_id" in item or item.get("content_kind") == "graph_primitive"]
    if not records and not attributed:
        return
    check = "emitted_graph_geometry" if emitted else "graph_data_accounted_for"
    report["checks"][check] = "fail"
    try:
        if not isinstance(records, list) or not records:
            raise ValueError("graph ink needs explicit graph ledger records")
        if manifest.get("visible_symbol_count") != len(instances):
            raise ValueError("visible_symbol_count must include graph primitives, labels, and arrows")
        by_token = {item["token_id"]: item for item in instances}
        if len(by_token) != len(instances):
            raise ValueError("graph inventory needs unique token identities")
        paint_order = {item["token_id"]: index for index, item in enumerate(instances)}
        used, graph_ids, declared_primitives = set(), set(), set()
        for record in records:
            identity, spec = record["graph_id"], record["spec"]
            if not isinstance(identity, str) or not identity or identity in graph_ids:
                raise ValueError("graph IDs must be nonempty and unique")
            graph_ids.add(identity)
            validate_graph(spec)
            style = record.get("style", {})
            evidence = style.get("source_evidence")
            if evidence is not None:
                validate_graph_style(evidence)
                if style.get("source_refs") != evidence["graph_style"]["source_refs"]:
                    raise ValueError("graph trace ledger must retain its exact reviewed source references")
            primitive_ids, label_ids, arrow_ids = (record[key] for key in ("primitive_token_ids", "label_token_ids", "arrow_token_ids"))
            for tokens in (primitive_ids, label_ids, arrow_ids):
                if not isinstance(tokens, list) or any(not isinstance(token, str) for token in tokens):
                    raise ValueError("graph token inventories must be string arrays")
                if len(tokens) != len(set(tokens)) or used.intersection(tokens):
                    raise ValueError("graph inventories must be disjoint")
                used.update(tokens)
                if any(by_token[token].get("graph_id") != identity for token in tokens):
                    raise ValueError("graph token points to another graph")
            declared_primitives.update(primitive_ids)
            if any(not is_graph_primitive(by_token[token]) for token in primitive_ids):
                raise ValueError("primitive inventory contains ordinary or semantic ink")
            if any(by_token[token].get("semantic", True) is False or by_token[token].get("role") != "text" or by_token[token].get("graph_kind") != "label" for token in label_ids):
                raise ValueError("graph labels must be semantic writer text")
            if any(by_token[token].get("semantic", True) is False or by_token[token].get("role") != "math.operator" or by_token[token].get("text") != "→" or by_token[token].get("graph_kind") != "arrow" for token in arrow_ids):
                raise ValueError("graph arrows must be semantic sampled writer arrows")
            actual_symbols = Counter((by_token[token]["role"], by_token[token]["text"]) for token in label_ids+arrow_ids)
            if actual_symbols != Counter(graph_requirements(spec)):
                raise ValueError("graph labels or sampled arrows differ from their explicit specification")
            expected, tick_axes, drawn_paths = {}, {}, {}
            for axis in spec.get("axes", []):
                expected[f"{identity}.axis.{axis['id']}"] = ("axis", graph_axis_anchors(axis), axis)
                for tick in axis.get("ticks", []):
                    expected[f"{identity}.tick.{tick['id']}"] = ("tick", [tick["at"]], tick)
                    tick_axes[f"{identity}.tick.{tick['id']}"] = axis
            for curve in spec.get("curves", []):
                expected[f"{identity}.curve.{curve['id']}"] = ("curve", curve["points"], curve)
            for marker in spec.get("markers", []):
                expected[f"{identity}.marker.{marker['id']}"] = ("marker", [marker["at"]], marker)
            if set(primitive_ids) != set(expected):
                raise ValueError("graph primitive inventory differs from explicit elements")
            padding = spec.get("padding_mm", 12)
            xmin, xmax = spec["x_range"]; ymin, ymax = spec["y_range"]
            def mapped(point):
                return [padding+(point[0]-xmin)/(xmax-xmin)*(spec["width_mm"]-2*padding),
                        spec["height_mm"]-padding-(point[1]-ymin)/(ymax-ymin)*(spec["height_mm"]-2*padding)]
            offset = None
            observed_only = manifest.get("variation", {}).get("mode") == "observed"
            pages = {by_token[token].get("page") for token in primitive_ids+label_ids+arrow_ids}
            if len(pages) != 1:
                raise ValueError("one graph must occupy one page")
            for token, (kind, data_anchors, element) in expected.items():
                item = by_token[token]
                if item.get("graph_kind") != kind or item.get("role") != "graph."+kind or item.get("graph_data_anchors") != data_anchors:
                    raise ValueError("graph kind or recorded data anchors differ from its specification")
                anchors = item["graph_plot_anchors_mm"]
                if len(anchors) != len(data_anchors) or any(len(point) != 2 or any(not isinstance(value, (int, float)) or not math.isfinite(value) for value in point) for point in anchors):
                    raise ValueError("graph plot anchors must be finite coordinates")
                if offset is None:
                    offset = [anchors[0][axis]-mapped(data_anchors[0])[axis] for axis in (0, 1)]
                expected_anchors = [[value+offset[axis] for axis, value in enumerate(mapped(point))] for point in data_anchors]
                if any(math.dist(actual, expected_point) > .002 for actual, expected_point in zip(anchors, expected_anchors)):
                    raise ValueError("graph coordinate mapping differs between data elements")
                paints = item.get("_ink_paints")
                paths = [path for paint in paints for path in paint["strokes"]] if paints else item["strokes"]
                if len(paths) != 1:
                    raise ValueError("each graph primitive must have one explicit path")
                path = paths[0]
                drawn_paths[token] = path
                reference = item.get("graph_trace")
                if reference is not None:
                    candidate, size = resolve_trace(evidence,reference)
                    if abs(size-manifest["page"]["body_x_height_mm"]) > 1e-8:
                        raise ValueError("graph source transfer scale differs from the declared writing size")
                    if item.get("source_refs") != [{"source_id": candidate["source_id"], "observation_id": candidate["observation_id"]}]:
                        raise ValueError("graph primitive source reference differs from its reviewed trace")
                    pen_width = paints[0].get("pen_width_mm",0) if paints else item["pen_width_mm"]
                    if kind != "marker" or element["style"] != "filled":
                        if abs(pen_width-evidence["graph_style"]["pen_width_xheight"]*size) > .002:
                            raise ValueError("graph pen differs from its reviewed source measurement")
                    if kind == "axis":
                        a,b=expected_anchors
                        role = "axis_horizontal" if abs(b[0]-a[0]) >= abs(b[1]-a[1]) else "axis_vertical"
                        canonical = axis_trace_path(expected_anchors,candidate,size,has_ticks=bool(element.get("ticks")))
                    elif kind == "curve":
                        role = "curve"
                        canonical = curve_trace_path(expected_anchors,element.get("interpolation","linear"),candidate,size)
                    elif kind == "tick":
                        role = "tick"
                        axis = tick_axes[token]; a,b = mapped(axis["from"]),mapped(axis["to"])
                        dx,dy=b[0]-a[0],b[1]-a[1]; length=math.hypot(dx,dy)
                        canonical = tick_trace_path(expected_anchors[0],[-dy/length,dx/length],candidate,size)
                    else:
                        role = "marker_"+element["style"]
                        canonical = marker_trace_path(expected_anchors[0],element.get("radius_mm",.8),candidate)
                    if candidate["role"] != role or path_distance(path,canonical) > .002:
                        raise ValueError("drawn graph geometry differs from its reviewed source trace transfer")
                    # All source paths are reconstructed at the semantic
                    # anchors above; additional knots are evidence, not data.
                    actual = expected_anchors
                elif kind in {"axis", "curve"}:
                    actual = [path["start"]]+[segment["end"] for segment in path["segments"]]
                    if observed_only:
                        interpolation = element.get("interpolation", "linear") if kind == "curve" else "linear"
                        canonical = _curve_stroke(expected_anchors, interpolation, lambda _: 0, 0)
                        if len(path["segments"]) != len(canonical["segments"]) or any(
                            segment["type"] != "cubic" or any(math.dist(segment[key], target[key]) > .002
                            for key in ("control1", "control2", "end"))
                            for segment, target in zip(path["segments"], canonical["segments"])):
                            raise ValueError("observed-mode graph paths must retain explicit interpolation without invented bends")
                elif kind == "tick":
                    if len(path["segments"]) != 1:
                        raise ValueError("graph tick must have one span")
                    actual = [[(a+b)/2 for a,b in zip(path["start"], path["segments"][0]["end"])]]
                if kind == "marker":
                    if reference is not None:
                        samples = path_samples(path)
                        if not path.get("closed"):
                            raise ValueError("reviewed graph marker must remain closed")
                        radius = element.get("radius_mm",.8)
                        radii = [math.dist(point,expected_anchors[0]) for point in samples]
                        if max(radii) > radius+.002:
                            raise ValueError("reviewed graph marker radii exceed their declared bounded size")
                        rx=ry=min(radii)
                    else:
                        if not path.get("closed") or len(path["segments"]) != 4:
                            raise ValueError("graph marker must be a closed four-span outline")
                        actual = [[(a+b)/2 for a,b in zip(path["start"], path["segments"][1]["end"])]]
                    representation = paints[0]["representation"] if paints else item["representation"]
                    if (representation == "clean_outline") != (element["style"] == "filled"):
                        raise ValueError("open and filled graph marker semantics changed")
                    cx, cy = expected_anchors[0]
                    if reference is None:
                        rx, ry = path["start"][0]-cx, path["segments"][0]["end"][1]-cy
                    amount = manifest.get("variation", {}).get("amount", .1)
                    if not isinstance(amount, (int, float)) or isinstance(amount, bool) or not math.isfinite(amount) or not 0 <= amount <= .3:
                        raise ValueError("graph marker needs a valid bounded variation amount")
                    spread, radius = (0 if observed_only else .025*min(amount/.1, 3)), element.get("radius_mm", .8)
                    if reference is None and not all(radius*(1-spread)-.002 <= value <= radius*(1+spread)+.002 for value in (rx, ry)):
                        raise ValueError("graph marker radii exceed their declared bounded size")
                    k = .5522847498307936
                    canonical = [([cx+rx, cy+k*ry], [cx+k*rx, cy+ry], [cx, cy+ry]),
                                 ([cx-k*rx, cy+ry], [cx-rx, cy+k*ry], [cx-rx, cy]),
                                 ([cx-rx, cy-k*ry], [cx-k*rx, cy-ry], [cx, cy-ry]),
                                 ([cx+k*rx, cy-ry], [cx+rx, cy-k*ry], [cx+rx, cy])]
                    if reference is None and any(segment["type"] != "cubic" or any(math.dist(segment[key], value) > .002 for key, value in zip(("control1", "control2", "end"), controls))
                           for segment, controls in zip(path["segments"], canonical)):
                        raise ValueError("graph marker must keep its declared bounded ellipse rather than masking arbitrary ink")
                    open_marker = element["style"] == "open"
                    if (item.get("paper_knockout") == "open_graph_marker") != open_marker:
                        raise ValueError("an open graph marker needs its actual white paper fill before the ink outline")
                    if open_marker:
                        pen_width = paints[0]["pen_width_mm"] if paints else item["pen_width_mm"]
                        if min(rx, ry)-pen_width/2 < .15-.002:
                            raise ValueError("an open graph marker has no reliably visible paper interior")
                        underlying = [entry for entry in primitive_ids if by_token[entry].get("graph_kind") in {"axis", "curve", "tick"}]+arrow_ids
                        if any(paint_order[entry] >= paint_order[token] for entry in underlying):
                            raise ValueError("data strokes and arrows must be painted before open graph markers")
                if len(actual) != len(expected_anchors) or any(math.dist(a,b) > .002 for a,b in zip(actual, expected_anchors)):
                    raise ValueError("drawn graph endpoints/centres moved away from explicit data anchors")
                if reference is None and evidence is not None:
                    if kind == "axis":
                        a,b=expected_anchors
                        role="axis_horizontal" if abs(b[0]-a[0]) >= abs(b[1]-a[1]) else "axis_vertical"
                    else:
                        role="marker_"+element["style"] if kind == "marker" else kind
                    if trace_candidates(evidence,role):
                        raise ValueError("available reviewed graph stroke was replaced by unsupported geometry")
            for tick_token,axis in tick_axes.items():
                if not paths_touch(drawn_paths[f"{identity}.axis.{axis['id']}"],drawn_paths[tick_token]):
                    raise ValueError("reviewed axis must intersect each visible tick at its declared data position")
        if declared_primitives != {item["token_id"] for item in primitives}:
            raise ValueError("undeclared graph primitive ink")
        if used != {item["token_id"] for item in attributed}:
            raise ValueError("undeclared graph labels, arrows, or references")
        report["checks"][check] = "pass"
    except (InkformError, KeyError, TypeError, ValueError, OverflowError) as exc:
        _issue(report, "invalid_graph_inventory", "Could not verify explicit graph data and drawn anchors.", reason=str(exc), geometry_source="emitted_pdf_vector_paths" if emitted else "manifest_vector_paths")


def _graph_geometry_distance(emitted, expected):
    """Compare large plot paths directly instead of allocating a glyph raster."""
    paints = emitted.get("_ink_paints", [])
    if len(paints) != 1 or paints[0]["representation"] != expected["representation"]:
        return float("inf")
    paths, originals = paints[0]["strokes"], expected["strokes"]
    if len(paths) != len(originals):
        return float("inf")
    distance = abs(paints[0].get("pen_width_mm", .25)-expected["pen_width_mm"]) if expected["representation"] != "clean_outline" else 0
    for path, original in zip(paths, originals):
        if bool(path.get("closed")) != bool(original.get("closed")) or len(path["segments"]) != len(original["segments"]):
            return float("inf")
        distance = max(distance, math.dist(path["start"], original["start"]))
        for segment, source in zip(path["segments"], original["segments"]):
            if segment["type"] != source["type"]:
                return float("inf")
            for key in ("end", "control1", "control2"):
                if key in source:
                    distance = max(distance, math.dist(segment[key], source[key]))
    return distance


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
        try:
            emitted = _emitted_glyphs(reader, manifest)
            if emitted is not None:
                _check_repetition(manifest, emitted, report, geometry_source="emitted_pdf_vector_paths")
                _check_graph_accounting(manifest, emitted, report, emitted=True)
                # The ordinal mapping must also describe the ink that was
                # actually emitted. Detect a changed PDF even if someone has
                # recomputed its file hash while retaining an old sidecar.
                by_token = {item.get("token_id"): item for item in manifest.get("instances", []) if isinstance(item, dict)}
                mismatches = []
                for item in emitted:
                    expected_item = by_token.get(item.get("token_id"))
                    if expected_item is None:
                        raise ValueError("Emitted glyph has no manifest token")
                    distance = _graph_geometry_distance(item, expected_item) if is_graph_primitive(item) else item_distance(shape_descriptor(item), shape_descriptor(expected_item))
                    if distance > .025:
                        mismatches.append({"token_id": item.get("token_id"), "page": item["page"], "distance_mm": round(distance, 7) if math.isfinite(distance) else None})
                report["checks"]["emitted_glyph_geometry"] = "fail" if mismatches else "pass"
                if mismatches:
                    _issue(report, "emitted_glyph_mismatch", "PDF glyph ink differs from its manifest geometry.", instances=mismatches[:100], count=len(mismatches))
            elif isinstance(manifest.get("instances"), list):
                _check_repetition(manifest, manifest["instances"], report)
                report["checks"]["emitted_glyph_geometry"] = "not_checked"
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            report["checks"]["repetition"] = "fail"
            report["checks"]["emitted_glyph_geometry"] = "fail"
            _issue(report, "invalid_emitted_glyph_geometry", "Could not independently verify emitted glyph paths.", reason=str(exc))
    else:
        report["checks"].update({"manifest_pdf_hash": "not_checked", "instance_geometry": "not_checked",
                                  "content_accounted_for": "not_checked", "math_correctness": "not_checked", "repetition": "not_checked"})
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
