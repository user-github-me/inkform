"""Evidence-limited, reusable summaries of reviewed handwriting geometry.

This module never opens photos, guesses millimeters from pixels, or edits a
profile. It describes curated forms rather than claiming a population model of
a writer. Geometry-derived advances are not observations of actual spacing.

Optional word layout observations have this explicit contract::

    {"kind": "word", "source_id": "page-source", "observation_id": "word-1",
     "page_index": 0, "line_id": "line-1", "word_index": 0,
     "bbox_xywh": [x, y, width, height], "xheight_px": 30,
     "label_status": "reviewed", "layout_status": "reviewed",
     "reviewed_by": "reviewer"}

Boxes and x-height must share source-pixel coordinates. Word indices must be
consecutive to contribute a gap. A column_id may further disambiguate lines.
OCR confidence, crop ordering, and isolated glyph advances do not satisfy this
layout contract. No synthetic profile receives learned metric updates.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from collections import defaultdict
from statistics import median

import numpy as np

from .profiles import accepted_glyphs, validate_profile


MEASUREMENT_VERSION = "inkform-style-1"
_BODY_LETTERS = frozenset("acemnorsuvwxz")
_ASCENDERS = frozenset("bdfhklt")
_DESCENDERS = frozenset("gjpqy")
_STEM_FORMS = frozenset("bdhkltILH1")


def _finite(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _stats(values, units: str, *, basis: str) -> dict:
    values = [float(value) for value in values]
    center = median(values) if values else None
    count = len(values)
    return {"count": count, "median": center,
            "mad": median(abs(value-center) for value in values) if count >= 2 else None,
            "min": min(values) if values else None, "max": max(values) if values else None,
            "units": units, "basis": basis,
            "status": "unavailable" if not count else "single_sample" if count == 1 else "limited_samples" if count < 3 else "sample_summary"}


def _evidence_key(glyph: dict, observations: dict) -> tuple:
    refs = glyph.get("source_refs", [])
    identities = []
    for ref in refs:
        observation_id = ref.get("observation_id")
        if observation_id:
            observation = observations.get(observation_id, {})
            # Two representations or aliases of one mask are one observation.
            identities.append(("mask", observation["mask_sha256"]) if observation.get("mask_sha256")
                              else (ref["source_id"], observation_id))
    if identities:
        return tuple(sorted(set(identities)))
    # Older profiles may link only a source page. Do not collapse every letter
    # on that page into one sample; deduplicate exact geometry aliases instead.
    digest = hashlib.sha256(json.dumps(glyph["strokes"], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return ("geometry_record", digest)


def _roots(coefficients):
    coefficients = np.trim_zeros(np.asarray(coefficients, dtype=float), trim="f")
    if len(coefficients) < 2:
        return []
    return [float(root.real) for root in np.roots(coefficients)
            if abs(root.imag) < 1e-8 and -1e-9 <= root.real <= 1+1e-9]


def _body_path_bounds(strokes: list[dict]) -> list[float] | None:
    """Exact path extents within y=[-1,0], excluding a centerline's pen width.

    Intersections with the band and curve extrema are included. This keeps a
    long descender or detached dot from redefining the body attachment region.
    It is a geometry diagnostic, not a learned interletter advance.
    """
    points = []
    for stroke in strokes:
        start = current = stroke["start"]
        segments = list(stroke["segments"])
        if stroke.get("closed"):
            segments.append({"type": "line", "end": start})
        for segment in segments:
            end = segment["end"]
            coefficients = []
            for axis in (0, 1):
                if segment["type"] == "line":
                    coefficients.append([0., 0., end[axis]-current[axis], current[axis]])
                else:
                    p0, p1, p2, p3 = current[axis], segment["control1"][axis], segment["control2"][axis], end[axis]
                    coefficients.append([-p0+3*p1-3*p2+p3, 3*p0-6*p1+3*p2, -3*p0+3*p1, p0])
            xc, yc = coefficients
            times = [0., 1., *_roots(np.polyder(xc)), *_roots(np.polyder(yc))]
            for boundary in (-1., 0.):
                polynomial = list(yc)
                polynomial[-1] -= boundary
                times.extend(_roots(polynomial))
            for t in times:
                t = min(1., max(0., t))
                x, y = float(np.polyval(xc, t)), float(np.polyval(yc, t))
                if -1-1e-8 <= y <= 1e-8:
                    points.append((x, min(0., max(-1., y))))
            current = end
    if not points:
        return None
    return [min(p[0] for p in points), min(p[1] for p in points),
            max(p[0] for p in points), max(p[1] for p in points)]


def _slant(glyph: dict) -> float | None:
    """Estimate rightward tilt only from nearly straight, long stem paths."""
    if glyph["representation"] != "centerline" or glyph["text"] not in _STEM_FORMS:
        return None
    angles = []
    for stroke in glyph["strokes"]:
        points, current = [stroke["start"]], stroke["start"]
        for segment in stroke["segments"]:
            end = segment["end"]
            for t in np.linspace(0, 1, 17)[1:]:
                if segment["type"] == "line":
                    point = [(1-t)*current[k]+t*end[k] for k in (0, 1)]
                else:
                    point = [(1-t)**3*current[k]+3*(1-t)**2*t*segment["control1"][k]
                             + 3*(1-t)*t*t*segment["control2"][k]+t**3*end[k] for k in (0, 1)]
                points.append(point)
            current = end
        band = np.asarray([point for point in points if -1.5 <= point[1] <= -.1])
        if len(band) < 3 or np.ptp(band[:, 1]) < .65:
            continue
        slope, intercept = np.polyfit(band[:, 1], band[:, 0], 1)
        residual = band[:, 0]-(slope*band[:, 1]+intercept)
        if abs(slope) > math.tan(math.radians(35)) or float(np.max(np.abs(residual))) > .06:
            continue
        # y is down; an upper point farther right has a negative x/y slope.
        angles.append(math.degrees(math.atan(-float(slope))))
    return median(angles) if angles else None


def _word_gaps(observations: list[dict]) -> tuple[list[dict], dict]:
    groups = defaultdict(list)
    excluded = defaultdict(int)
    for observation in observations:
        if observation.get("kind", observation.get("type")) != "word":
            continue
        if (observation.get("label_status") != "reviewed" or observation.get("layout_status") != "reviewed"
                or not isinstance(observation.get("reviewed_by"), str) or not observation["reviewed_by"].strip()):
            excluded["unreviewed_layout_or_label"] += 1
            continue
        box, xheight = observation.get("bbox_xywh"), observation.get("xheight_px")
        if (not isinstance(box, list) or len(box) != 4 or not all(_finite(v) for v in box)
                or min(box[2:]) <= 0 or not _finite(xheight) or xheight <= 0):
            excluded["missing_pixel_box_or_xheight"] += 1
            continue
        page, index, line = observation.get("page_index"), observation.get("word_index"), observation.get("line_id")
        if (not isinstance(page, int) or isinstance(page, bool) or page < 0
                or not isinstance(index, int) or isinstance(index, bool) or index < 0
                or not isinstance(line, str) or not line):
            excluded["missing_explicit_line_order"] += 1
            continue
        column = observation.get("column_id", "body")
        if not isinstance(column, str) or not column:
            excluded["missing_explicit_line_order"] += 1
            continue
        scope = (observation["source_id"], page, column, line)
        groups[scope].append(observation)
    records = []
    for scope, words in sorted(groups.items()):
        words.sort(key=lambda record: record["word_index"])
        indices = [word["word_index"] for word in words]
        if len(indices) != len(set(indices)):
            excluded["ambiguous_duplicate_word_index"] += len(words)
            continue
        for left, right in zip(words, words[1:]):
            if right["word_index"] != left["word_index"]+1:
                excluded["nonadjacent_words"] += 1
                continue
            height_ratio = max(left["xheight_px"], right["xheight_px"]) / min(left["xheight_px"], right["xheight_px"])
            lb, rb = left["bbox_xywh"], right["bbox_xywh"]
            gap = rb[0]-(lb[0]+lb[2])
            if gap <= 0:
                excluded["overlapping_or_reversed_boxes"] += 1
                continue
            if height_ratio > 1.25 or max(lb[1], rb[1]) >= min(lb[1]+lb[3], rb[1]+rb[3]):
                excluded["inconsistent_same_line_geometry"] += 1
                continue
            records.append({"source_id": scope[0], "page_index": scope[1], "column_id": scope[2], "line_id": scope[3],
                            "left_observation_id": left["observation_id"], "right_observation_id": right["observation_id"],
                            "gap_xheight": gap / median((left["xheight_px"], right["xheight_px"]))})
    return records, dict(excluded)


def measure_style(profile: dict) -> dict:
    """Summarize reviewed forms and trusted layout evidence without mutation."""
    validate_profile(profile)
    observations = {record["observation_id"]: record for record in profile.get("observations", [])}
    accepted = accepted_glyphs(profile)
    selected = {}
    for glyph in sorted(accepted, key=lambda g: (g["representation"] != "clean_outline", g["glyph_id"])):
        key = (glyph["role"], glyph["text"], _evidence_key(glyph, observations))
        selected.setdefault(key, glyph)
    grouped = defaultdict(list)
    for (role, text, evidence), glyph in selected.items():
        grouped[(role, text)].append((glyph, evidence))
    by_glyph, forms = [], []
    basis = "Reviewed normalized form bounds; curated examples, not frequency-weighted natural writing."
    for (role, text), variants in sorted(grouped.items()):
        by_glyph.append({"role": role, "text": text, "variant_count": len(variants),
                         "width_xheight": _stats([g["ink_bounds"][2]-g["ink_bounds"][0] for g, _ in variants], "x_height", basis=basis),
                         "height_xheight": _stats([g["ink_bounds"][3]-g["ink_bounds"][1] for g, _ in variants], "x_height", basis=basis),
                         "glyph_ids": [g["glyph_id"] for g, _ in variants]})
        for glyph, _ in variants:
            body = _body_path_bounds(glyph["strokes"])
            bounds = glyph["ink_bounds"]
            forms.append({"glyph_id": glyph["glyph_id"], "role": role, "text": text,
                          "representation": glyph["representation"], "ink_bounds_xheight": copy.deepcopy(bounds),
                          "body_path_bounds_xheight": body, "body_band_y_xheight": [-1, 0],
                          "body_left_offset_from_ink_xheight": body[0]-bounds[0] if body else None,
                          "stored_advance_xheight": glyph["advance"],
                          "stored_left_bearing_xheight": bounds[0],
                          "stored_right_bearing_xheight": glyph["advance"]-bounds[2],
                          "spacing_evidence": "isolated_form_only; stored advances and bearings are not observed interletter gaps"})
    # Text/math aliases and multiple representations must not inflate samples.
    distinct = {}
    for (_, text, evidence), glyph in sorted(selected.items(), key=lambda item: (item[0][0] != "text", item[1]["representation"] != "clean_outline", item[1]["glyph_id"])):
        if len(text) == 1 and (text.isalpha() or text.isdigit()):
            distinct.setdefault(evidence, glyph)
    glyphs = list(distinct.values())
    values = {
        "glyph_width_xheight": [g["ink_bounds"][2]-g["ink_bounds"][0] for g in glyphs],
        "glyph_height_xheight": [g["ink_bounds"][3]-g["ink_bounds"][1] for g in glyphs],
        "body_height_xheight": [max(0., -g["ink_bounds"][1]) for g in glyphs if g["text"] in _BODY_LETTERS],
        "ascender_height_xheight": [max(0., -g["ink_bounds"][1]) for g in glyphs if g["text"] in _ASCENDERS],
        "descender_depth_xheight": [max(0., g["ink_bounds"][3]) for g in glyphs if g["text"] in _DESCENDERS],
        "capital_height_xheight": [max(0., -g["ink_bounds"][1]) for g in glyphs if g["text"] in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"],
    }
    measurements = {key: _stats(value, "x_height", basis=basis) for key, value in values.items()}
    slants = {}
    for glyph in accepted:
        angle = _slant(glyph)
        if angle is not None:
            slants.setdefault(_evidence_key(glyph, observations), angle)
    measurements["slant_degrees"] = _stats(slants.values(), "degrees_right_of_vertical", basis="Estimated static tilt of qualifying reviewed centerline stems; not pen direction or captured motion.")
    gaps, exclusions = _word_gaps(list(observations.values()))
    measurements["word_gap_xheight"] = _stats([record["gap_xheight"] for record in gaps], "x_height", basis="Reviewed consecutive source word ink boxes on explicitly identified lines, normalized by their local x-height.")
    measurements["by_glyph"] = by_glyph
    line_count = len({(record["source_id"], record["page_index"], record["column_id"], record["line_id"]) for record in gaps})
    updates, evidence = {}, {}
    if not profile["synthetic"] and len(gaps) >= 3 and line_count >= 2:
        updates["word_gap"] = measurements["word_gap_xheight"]["median"]
        evidence["word_gap"] = {"measurement": "word_gap_xheight", "sample_count": len(gaps), "line_count": line_count,
                                "status": "supported_estimate", "units": "x_height", "scope": "word gaps only; interletter spacing remains unmeasured"}
    uncertainties = [
        "Curated forms are not a random sample; aggregate glyph widths depend on which letters were reviewed.",
        "Normalized proportions inherit the original baseline and x-height calibration uncertainty.",
        "Isolated crop bounds, stored advances, and body anchors do not establish natural interletter spacing.",
        "Line spacing is not inferred from isolated forms; existing line_height is retained.",
        "Photos and static paths do not establish stroke order, timing, or pressure.",
    ]
    if not slants:
        uncertainties.append("No sufficiently straight reviewed centerline stems support a slant estimate; outline bounding boxes alone are insufficient.")
    if not updates:
        uncertainties.append("No word-gap metric update: need at least three reviewed adjacent gaps across two lines of nonsynthetic source evidence.")
    if any(not any(ref.get("observation_id") for ref in g["source_refs"]) for g in accepted):
        uncertainties.append("Some forms lack observation IDs; their deduplication uses exact geometry and cannot establish independent capture counts.")
    if profile["synthetic"]:
        uncertainties.append("This is synthetic demonstration geometry, not learned personal handwriting; no learned metrics are proposed.")
    return {"version": MEASUREMENT_VERSION, "synthetic": profile["synthetic"], "measurements": measurements,
            "accepted_glyph_records": len(accepted), "unique_letter_form_samples": len(glyphs),
            "form_geometry": forms, "word_gap_samples": gaps, "word_gap_exclusions": exclusions,
            "metric_updates": updates, "metric_update_evidence": evidence, "retained_metrics": copy.deepcopy(profile["metrics"]),
            "physical_scale": {"inferred_from_images": False, "retained": copy.deepcopy(profile["physical_scale"]),
                               "requirement": "Known physical source calibration or an explicit user-selected writing size is required; image pixel dimensions alone provide no millimeter scale."},
            "uncertainties": uncertainties}
