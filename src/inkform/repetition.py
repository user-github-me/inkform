"""Measure repeated *ink*, independently of occurrence IDs or random factors.

A canonical segment multiset catches translated, reordered and reversed copies.
A second test samples the visible ink on a 0.025 mm grid and measures symmetric
boundary distance. The default 0.035 mm threshold is a conservative diagnostic
at print size, not a learned similarity score or a claim of human handwriting.

The same visible symbol is compared across semantic roles. History is bounded to the current
and preceding page. Dots and simple bars have a finite useful shape space: their
near matches are reported separately, never a reason to damage their meaning.
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
import hashlib
import json
import math

import numpy as np
from PIL import Image, ImageDraw
from scipy.ndimage import binary_erosion, binary_fill_holes, distance_transform_edt, label, map_coordinates


GRID_MM = 0.025
NEAR_THRESHOLD_MM = 0.035
MAX_DESCRIPTOR_EDGE = 20_000  # Thin page-wide fraction/root bars remain auditable.
MAX_DESCRIPTOR_PIXELS = 2_000_000
MAX_REPORTED_PAIRS = 1000


def is_graph_primitive(item):
    """Data geometry has fixed coordinates; its separate ledger is validated.

    Writer labels and sampled arrows are ordinary glyphs and never excluded.
    The PDF checker requires every primitive to match its explicit graph data,
    and still verifies the emitted paths against the planned geometry.
    """
    return (isinstance(item, dict) and item.get("content_kind") == "graph_primitive"
            and item.get("semantic") is False and item.get("role") in
            {"graph.axis", "graph.curve", "graph.marker", "graph.tick"})


@dataclass
class ShapeDescriptor:
    fingerprint: str
    boundary: np.ndarray = field(repr=False)
    distance_field: np.ndarray = field(repr=False)
    origin_mm: tuple[float, float]
    grid_mm: float
    limited: bool
    scale_basis: str
    role: str
    text: str
    extent_mm: tuple[float, float]


def _finite(value):
    return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value)


def _point(value):
    if not isinstance(value, (list, tuple)) or len(value) != 2 or not all(_finite(v) for v in value):
        raise ValueError("Repetition audit needs finite two-dimensional geometry")
    return tuple(float(v) for v in value)


def _segments(strokes):
    """Yield oriented segment geometry; no path order or pen-up IDs involved."""
    for stroke in strokes:
        start = current = _point(stroke["start"])
        if not stroke["segments"]:
            yield (current,)
        for segment in stroke["segments"]:
            end = _point(segment["end"])
            if segment["type"] == "line":
                yield (current, end)
            elif segment["type"] == "cubic":
                yield (current, _point(segment["control1"]), _point(segment["control2"]), end)
            else:
                raise ValueError("Unsupported path segment in repetition audit")
            current = end
        if stroke.get("closed") and current != start:
            yield (current, start)


def _canonical(strokes, factor, representation, width, origin=None):
    segments = list(_segments(strokes))
    if not segments:
        raise ValueError("Repetition audit needs at least one stroke")
    points = [point for segment in segments for point in segment]
    origin = origin if origin is not None else (min(x for x, y in points), min(y for x, y in points))
    # 0.1 nanometre precision catches copies after ordinary float placement
    # arithmetic. Sub-visible numerical noise is additionally caught below.
    normalized = []
    for segment in segments:
        value = tuple((round((x-origin[0])*factor, 7), round((y-origin[1])*factor, 7)) for x, y in segment)
        normalized.append(min(value, value[::-1]))
    widths = []
    if representation != "clean_outline":
        for stroke in strokes:
            samples = _width_samples(stroke)
            forward = tuple((round(p["s"], 7), round(p["multiplier"]*width, 7)) for p in samples)
            backward = tuple((round(1-p["s"], 7), round(p["multiplier"]*width, 7)) for p in reversed(samples))
            widths.append(min(forward, backward))
    ink_kind = "fill_even_odd" if representation == "clean_outline" else "round_stroke"
    value = [ink_kind, sorted(normalized), sorted(widths)]
    return hashlib.sha256(json.dumps(value, separators=(",", ":")).encode()).hexdigest()


def _sample(stroke, factor, center, step):
    current = np.asarray(_point(stroke["start"]), dtype=float)
    points = [current]
    for segment in stroke["segments"]:
        end = np.asarray(_point(segment["end"]), dtype=float)
        controls = [current, end] if segment["type"] == "line" else [current, np.asarray(_point(segment["control1"])), np.asarray(_point(segment["control2"])), end]
        length = sum(float(np.linalg.norm(b-a)) for a, b in zip(controls, controls[1:])) * factor
        count = max(1, min(12000, math.ceil(length / step)))
        t = np.arange(1, count+1, dtype=float)[:, None] / count
        if len(controls) == 2:
            values = (1-t)*current + t*end
        else:
            values = (1-t)**3*current + 3*(1-t)**2*t*controls[1] + 3*(1-t)*t**2*controls[2] + t**3*end
        points.extend(values)
        current = end
    if stroke.get("closed") and not np.array_equal(points[0], points[-1]):
        points.append(points[0])
    return (np.asarray(points)-center) * factor


def _width_samples(stroke):
    samples = stroke.get("width_profile", [{"s": 0, "multiplier": 1}, {"s": 1, "multiplier": 1}])
    if not samples or any(not _finite(p.get("s")) or not _finite(p.get("multiplier")) or p["multiplier"] <= 0 for p in samples):
        raise ValueError("Invalid stroke width profile in repetition audit")
    return samples


def _limited(item, paths, mask, grid_mm):
    # Restrict the exception by *both* semantic identity and actual geometry.
    # An arrow is never a bar merely because its bounding box is very shallow.
    text = item.get("text", "")
    if text in {".", "·", "•", "∙", "⋅", "…", ":"}:
        return len(paths) <= 3 and all(float(np.max(np.ptp(path, axis=0))) <= .65 for path in paths)
    if text in {",", "'", "’", "‘", ";"}:
        # A small tail cannot supply an unlimited visible shape vocabulary.
        # Use painted ink, not path count: variable-width PDF strokes may be
        # emitted as many short segments of the same comma.
        yy, xx = np.nonzero(mask)
        width, height = (xx.max()-xx.min()+1)*grid_mm, (yy.max()-yy.min()+1)*grid_mm
        if width > 1.2 or height > (2.4 if text == ";" else 1.2):
            return False
        components, count = label(mask, structure=np.ones((3, 3)))
        if not 1 <= count <= (2 if text == ";" else 1):
            return False
        for component_id in range(1, count+1):
            component = components == component_id
            cy, cx = np.nonzero(component)
            span = max(cx.max()-cx.min()+1, cy.max()-cy.min()+1)*grid_mm
            boundary_length = int(np.count_nonzero(component & ~binary_erosion(component)))*grid_mm
            # Exclude enclosed counters and intricate small scribbles/letters.
            if (span > 1.2 or np.any(binary_fill_holes(component) & ~component)
                    or boundary_length > 3.75*span + 2*grid_mm):
                return False
        return True
    if text not in {"−", "-", "_", "|", "‖", "―", "—", "–", "="}:
        return False
    points = np.concatenate(paths)
    if len(points) <= 2:
        return True
    spans = np.ptp(points, axis=0)
    # Includes hand-drawn slight bow and thin outline bars, but not arbitrary
    # letter shapes falsely tagged with a punctuation ID.
    return min(spans) <= max(.5, max(spans)*.22)


def shape_descriptor(item: dict, *, grid_mm: float = GRID_MM) -> ShapeDescriptor:
    """Describe rendered paths at physical scale, independently of metadata IDs.

    ``nominal_size_mm`` is optional. When present, intended script/body scaling
    is normalized to a 3 mm nominal size, preserving relative width/height
    differences. Old manifests remain measurable in their native millimetres.
    Placement, glyph IDs, seeds and source references never affect the metric.
    """
    if not _finite(grid_mm) or not .005 <= grid_mm <= .1:
        raise ValueError("Repetition grid must be between 0.005 and 0.1 mm")
    paints = item.get("_ink_paints")
    if paints is None:
        paints = [{"strokes": item.get("strokes"), "representation": item.get("representation", "centerline"),
                   "pen_width_mm": item.get("pen_width_mm", .25)}]
    if not isinstance(paints, list) or not paints:
        raise ValueError("Repetition audit requires actual painted paths")
    strokes = []
    for paint in paints:
        if not isinstance(paint, dict) or not isinstance(paint.get("strokes"), list) or not paint["strokes"]:
            raise ValueError("Repetition audit requires actual instance paths")
        strokes.extend(paint["strokes"])
    nominal = item.get("nominal_size_mm")
    if nominal is not None and (not _finite(nominal) or nominal <= 0):
        raise ValueError("Invalid nominal writing size in repetition audit")
    factor = 3. / nominal if nominal else 1.
    from .profiles import geometry_bounds
    left, top, right, bottom = geometry_bounds(strokes)
    if not all(_finite(v) for v in (left, top, right, bottom)):
        raise ValueError("Non-finite geometry in repetition audit")
    center = np.array([(left+right)/2, (top+bottom)/2])
    paths = [_sample(stroke, factor, center, grid_mm / 2) for stroke in strokes]
    pad = grid_mm*3
    for paint in paints:
        representation = paint["representation"]
        if representation not in {"centerline", "clean_outline", "primitive"}:
            raise ValueError("Unsupported repetition audit representation")
        width = paint.get("pen_width_mm", .25)
        if not _finite(width) or width <= 0:
            raise ValueError("Invalid pen width in repetition audit")
        if representation != "clean_outline":
            max_width = max(max(p["multiplier"] for p in _width_samples(stroke)) for stroke in paint["strokes"]) * width * factor
            pad = max(pad, max_width/2 + grid_mm*3)
    # The raster has an origin fixed relative to the actual geometry centre,
    # not its page position or a chosen start point. Integer grid centring keeps
    # repeated translated shapes bit-for-bit stable.
    half_x = math.ceil(((right-left)*factor/2 + pad)/grid_mm)
    half_y = math.ceil(((bottom-top)*factor/2 + pad)/grid_mm)
    shape = (2*half_x+1, 2*half_y+1)
    if max(shape) > MAX_DESCRIPTOR_EDGE or shape[0]*shape[1] > MAX_DESCRIPTOR_PIXELS:
        raise ValueError("Glyph exceeds bounded repetition raster dimensions")
    origin = -half_x*grid_mm, -half_y*grid_mm
    pixel_paths = [(path-np.array(origin))/grid_mm for path in paths]
    mask = np.zeros((shape[1], shape[0]), dtype=bool)
    offset = 0
    fingerprints = []
    for paint in paints:
        representation, width = paint["representation"], paint.get("pen_width_mm", .25)*factor
        selected_paths = pixel_paths[offset:offset+len(paint["strokes"])]
        offset += len(paint["strokes"])
        fingerprints.append(_canonical(paint["strokes"], factor, representation, width, center))
        if representation == "clean_outline":
            # Each PDF even-odd fill is one paint. Holes combine by XOR within
            # that paint; separately painted outlines combine by union.
            layer_mask = np.zeros_like(mask)
            for path in selected_paths:
                layer = Image.new("1", shape, 0)
                ImageDraw.Draw(layer).polygon([tuple(p) for p in path], fill=1)
                layer_mask ^= np.asarray(layer, dtype=bool)
            mask |= layer_mask
        else:
            image = Image.new("1", shape, 0)
            draw = ImageDraw.Draw(image)
            for stroke, path in zip(paint["strokes"], selected_paths):
                samples = _width_samples(stroke)
                multipliers = {p["multiplier"] for p in samples}
                if len(path) == 1 or float(np.max(np.ptp(path, axis=0))) < 1e-9:
                    radius = width*samples[0]["multiplier"]/grid_mm/2
                    x, y = path[0]
                    draw.ellipse((x-radius, y-radius, x+radius, y+radius), fill=1)
                elif len(multipliers) == 1:
                    pen = max(1, round(width*next(iter(multipliers))/grid_mm))
                    draw.line([tuple(p) for p in path], fill=1, width=pen, joint="curve")
                    radius = pen/2
                    for x, y in (path[0], path[-1]):
                        draw.ellipse((x-radius, y-radius, x+radius, y+radius), fill=1)
                else:
                    lengths = np.linalg.norm(np.diff(path, axis=0), axis=1)
                    total = float(lengths.sum())
                    if total <= 1e-9:
                        continue
                    positions = (np.cumsum(lengths)-lengths/2)/total
                    factors = np.interp(positions, [p["s"] for p in samples], [p["multiplier"] for p in samples])
                    for a, b, multiplier in zip(path, path[1:], factors):
                        pen = max(1, round(width*multiplier/grid_mm))
                        draw.line([tuple(a), tuple(b)], fill=1, width=pen)
                        radius = pen/2
                        for x, y in (a, b):
                            draw.ellipse((x-radius, y-radius, x+radius, y+radius), fill=1)
            mask |= np.asarray(image, dtype=bool)
    if not mask.any():
        raise ValueError("Repetition descriptor contains no visible ink")
    edge = mask & ~binary_erosion(mask)
    yy, xx = np.nonzero(edge)
    boundary = np.column_stack((xx*grid_mm + origin[0], yy*grid_mm + origin[1]))
    # A boundary distance field retains counters and holes, unlike testing only
    # black-pixel overlap or bounding box width. Outside-grid distances are
    # handled explicitly by _directed_distance.
    field = distance_transform_edt(~edge).astype(np.float32)*grid_mm
    fingerprint = hashlib.sha256(json.dumps(sorted(fingerprints)).encode()).hexdigest()
    return ShapeDescriptor(fingerprint, boundary, field, origin, grid_mm,
                           _limited(item, paths, mask, grid_mm), "nominal_3mm" if nominal else "physical_mm",
                           item.get("role", ""), item.get("text", ""),
                           ((right-left)*factor, (bottom-top)*factor))


def _directed_distance(first, second):
    coordinates = (first.boundary-np.array(second.origin_mm))/second.grid_mm
    clipped = np.column_stack((np.clip(coordinates[:, 0], 0, second.distance_field.shape[1]-1),
                               np.clip(coordinates[:, 1], 0, second.distance_field.shape[0]-1)))
    distances = map_coordinates(second.distance_field, [clipped[:, 1], clipped[:, 0]], order=1, mode="nearest")
    # Pad distance conservatively when the other shape has a shorter extent.
    distances = np.hypot(distances, np.linalg.norm(coordinates-clipped, axis=1)*second.grid_mm)
    return float(np.mean(distances))


def item_distance(first, second) -> float:
    """Symmetric mean ink-boundary distance in mm; lower means more alike.

    Inputs may be descriptors (preferred for candidate loops) or instance dicts.
    This is a shape diagnostic. It is deliberately not a percentage of realism.
    """
    first = first if isinstance(first, ShapeDescriptor) else shape_descriptor(first)
    second = second if isinstance(second, ShapeDescriptor) else shape_descriptor(second)
    if first.fingerprint == second.fingerprint:
        return 0.
    return (_directed_distance(first, second)+_directed_distance(second, first))/2


def assess_pair(first, second, *, threshold_mm=NEAR_THRESHOLD_MM) -> dict:
    first = first if isinstance(first, ShapeDescriptor) else shape_descriptor(first)
    second = second if isinstance(second, ShapeDescriptor) else shape_descriptor(second)
    distance = item_distance(first, second)
    exact = first.fingerprint == second.fingerprint
    limited = first.limited and second.limited
    category = "exact" if exact else "limited" if limited and distance < threshold_mm else "near" if distance < threshold_mm else "distinct"
    return {"category": category, "distance_mm": round(distance, 7), "threshold_mm": threshold_mm,
            "limited_shape_space": limited, "scale_basis": first.scale_basis if first.scale_basis == second.scale_basis else "mixed"}


class RepetitionIndex:
    """Cache descriptors, retaining only same-symbol current/previous-page ink."""
    def __init__(self, *, threshold_mm=NEAR_THRESHOLD_MM, adjacent_pages=1):
        if not _finite(threshold_mm) or not .005 <= threshold_mm <= .2:
            raise ValueError("Repetition threshold must be between 0.005 and 0.2 mm")
        if not isinstance(adjacent_pages, int) or isinstance(adjacent_pages, bool) or not 0 <= adjacent_pages <= 2:
            raise ValueError("Repetition history must cover zero, one or two adjacent pages")
        self.threshold_mm = float(threshold_mm)
        self.adjacent_pages = adjacent_pages
        self.history = defaultdict(deque)
        self.page = 0
        self.comparison_count = 0

    def advance(self, page):
        self.page = max(self.page, page)
        for key in list(self.history):
            self.history[key] = deque(record for record in self.history[key] if record["page"] >= self.page-self.adjacent_pages)
            if not self.history[key]:
                del self.history[key]

    def collisions(self, item, page=None, *, descriptor=None, exclude_token_id=None):
        page = item.get("page", self.page) if page is None else page
        self.advance(page)
        descriptor = descriptor or shape_descriptor(item)
        pairs = []
        for previous in self.history[item.get("text")]:
            if abs(page-previous["page"]) > self.adjacent_pages or exclude_token_id is not None and previous["token_id"] == exclude_token_id:
                continue
            self.comparison_count += 1
            assessment = assess_pair(descriptor, previous["descriptor"], threshold_mm=self.threshold_mm)
            if assessment["category"] != "distinct":
                pairs.append({"first_token_id": previous["token_id"], "second_token_id": item.get("token_id"),
                              "first_page": previous["page"], "second_page": page,
                              "role": item.get("role"), "first_role": previous["descriptor"].role,
                              "second_role": item.get("role"), "text": item.get("text"), **assessment})
        return pairs

    def add(self, item, page=None, *, descriptor=None):
        page = item.get("page", self.page) if page is None else page
        self.advance(page)
        descriptor = descriptor or shape_descriptor(item)
        self.history[item.get("text")].append(
            {"page": page, "token_id": item.get("token_id"), "descriptor": descriptor})
        return descriptor


def audit_repetition(instances, *, policy=None):
    """Recompute geometry diagnostics; never trust stored variation/novelty IDs.

    ``policy`` accepts ``threshold_mm``, ``adjacent_pages`` and ``amount``. An
    explicit numeric zero amount is the only repetition opt-out. Optional
    ``strict_near`` (default true) rejects unresolved nontrivial near matches. Exact copies
    always fail except for the declared zero-variation opt-out.
    """
    policy = policy or {}
    if not isinstance(policy, dict):
        raise ValueError("Repetition policy must be an object")
    if "strict_near" in policy and not isinstance(policy["strict_near"], bool):
        raise ValueError("strict_near must be a boolean")
    index = RepetitionIndex(threshold_mm=policy.get("threshold_mm", NEAR_THRESHOLD_MM),
                            adjacent_pages=policy.get("adjacent_pages", 1))
    opted_out = _finite(policy.get("amount")) and policy["amount"] == 0
    report = {"version": "inkform-ink-repetition-1", "status": "pass", "scope": "same visible text across semantic roles on the same and adjacent pages",
              "grid_mm": GRID_MM, "threshold_mm": index.threshold_mm, "adjacent_pages": index.adjacent_pages,
              "opt_out": "explicit_zero_variation" if opted_out else None,
              "metric": "symmetric_mean_ink_boundary_distance_mm",
              "distance_scale": "3 mm nominal writing size when supplied; otherwise native print millimetres",
              "small_tail_criteria": {"symbols": ", ' ’ ‘ ;", "component_extent_mm": 1.2, "semicolon_height_mm": 2.4,
                                      "maximum_components": "one; two for semicolon", "enclosed_counters": False,
                                      "maximum_boundary_length": "3.75 times component extent plus two grid pixels"},
              "pairs": [], "pair_count": 0,
              "exact_count": 0, "near_count": 0, "limited_count": 0, "checked_instance_count": 0,
              "unchecked_instances": [], "comparison_count": 0, "truncated": False}
    report["excluded_graph_primitive_tokens"] = []
    def page_key(pair):
        page = pair[1].get("page") if isinstance(pair[1], dict) else None
        return (page if isinstance(page, int) and not isinstance(page, bool) else -1, pair[0])
    ordered = sorted(enumerate(instances), key=page_key)
    for instance_index, item in ordered:
        if is_graph_primitive(item):
            report["excluded_graph_primitive_tokens"].append(item.get("token_id"))
            continue
        if (not isinstance(item, dict) or not isinstance(item.get("page"), int) or isinstance(item.get("page"), bool)
                or item["page"] < 0 or not item.get("role") or not item.get("text")):
            report["unchecked_instances"].append({"instance": instance_index, "reason": "missing role, text or valid page"})
            continue
        try:
            descriptor = shape_descriptor(item)
            pairs = index.collisions(item, descriptor=descriptor)
            index.add(item, descriptor=descriptor)
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            report["unchecked_instances"].append({"instance": instance_index, "token_id": item.get("token_id"), "reason": str(exc)})
            continue
        report["checked_instance_count"] += 1
        for pair in pairs:
            report["pair_count"] += 1
            report[pair["category"]+"_count"] += 1
            if len(report["pairs"]) < MAX_REPORTED_PAIRS:
                report["pairs"].append(pair)
    report["comparison_count"] = index.comparison_count
    report["truncated"] = report["pair_count"] > len(report["pairs"])
    if opted_out:
        report["status"] = "opted_out"
    elif report["exact_count"] or policy.get("strict_near", True) and report["near_count"]:
        report["status"] = "fail"
    elif report["unchecked_instances"]:
        report["status"] = "partial" if report["checked_instance_count"] else "not_checked"
    elif report["near_count"] or report["limited_count"]:
        report["status"] = "review_needed"
    return report
