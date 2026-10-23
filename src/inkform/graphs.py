"""Explicit graph data drawn with a writer's labels, arrows, and pen.

The graph specification is the semantic authority. This module does not infer a
function, read a graph photograph, or manufacture numerical coordinates.
Reviewed graph traces supply the writer's actual departures from a baseline;
missing kinds retain explicit geometry instead of acquiring invented wobble.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from statistics import median

import regex
from scipy.interpolate import PchipInterpolator

from .errors import InkformError
from .variation import stable_value


def _fail(message, path):
    raise InkformError("invalid_graph", message, {"path": path})


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _keys(value, allowed, required, path):
    if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
        _fail("Graph fields must be explicit and use the supported graph specification.", path)


def _pair(value, path):
    if not isinstance(value, list) or len(value) != 2 or not all(_finite(v) for v in value):
        _fail("Expected two finite coordinates.", path)


def validate_graph(spec, path="graph"):
    """Validate a bounded, explicit graph without interpreting its meaning."""
    _keys(spec, ("width_mm", "height_mm", "padding_mm", "x_range", "y_range", "coordinate_kind",
                 "axes", "curves", "markers", "labels"),
          ("width_mm", "height_mm", "x_range", "y_range"), path)
    for key in ("width_mm", "height_mm"):
        if not _finite(spec[key]) or not 25 <= spec[key] <= 500:
            _fail("Graph dimensions must be between 25 and 500 millimetres.", path + "." + key)
    padding = spec.get("padding_mm", 12)
    if not _finite(padding) or padding < 2 or 2 * padding >= min(spec["width_mm"], spec["height_mm"]):
        _fail("Graph padding must leave a positive plotting area.", path + ".padding_mm")
    for key in ("x_range", "y_range"):
        _pair(spec[key], path + "." + key)
        if spec[key][0] >= spec[key][1]:
            _fail("Graph ranges must increase.", path + "." + key)
    if spec.get("coordinate_kind", "quantitative") not in ("qualitative", "quantitative"):
        _fail("Coordinates must be declared qualitative or quantitative.", path + ".coordinate_kind")
    identifiers = set()

    def identity(item, item_path):
        name = item.get("id")
        if not isinstance(name, str) or not name or name in identifiers:
            _fail("Graph element IDs must be nonempty and unique within the graph.", item_path + ".id")
        identifiers.add(name)

    def point(value, item_path):
        _pair(value, item_path)
        if not all(spec[key][0] <= v <= spec[key][1] for v, key in zip(value, ("x_range", "y_range"))):
            _fail("Graph data anchors must fall inside the declared ranges.", item_path)

    def text(value, item_path):
        if not isinstance(value, str) or not value.strip() or len(value) > 500 or any(ord(c) < 32 for c in value):
            _fail("Graph labels must be nonempty single-line text, up to 500 characters.", item_path)

    total = 0
    for category in ("axes", "curves", "markers", "labels"):
        entries = spec.get(category, [])
        if not isinstance(entries, list) or len(entries) > 100:
            _fail("A graph category must be an array of at most 100 elements.", path + "." + category)
        total += len(entries)
        for i, entry in enumerate(entries):
            item_path = f"{path}.{category}[{i}]"
            allowed = {"axes": ("id", "from", "to", "arrow", "ticks"),
                       "curves": ("id", "points", "interpolation", "point_mode", "anchor_indices"),
                       "markers": ("id", "at", "style", "radius_mm"),
                       "labels": ("id", "at", "text", "offset_mm")}[category]
            required = {"axes": ("id", "from", "to"), "curves": ("id", "points"),
                        "markers": ("id", "at", "style"), "labels": ("id", "at", "text")}[category]
            _keys(entry, allowed, required, item_path)
            identity(entry, item_path)
            if category == "axes":
                point(entry["from"], item_path + ".from"); point(entry["to"], item_path + ".to")
                if entry["from"] == entry["to"]:
                    _fail("An axis requires distinct endpoints.", item_path)
                if not isinstance(entry.get("arrow", True), bool):
                    _fail("Axis arrow must be true or false.", item_path + ".arrow")
                ticks = entry.get("ticks", [])
                if not isinstance(ticks, list) or len(ticks) > 100:
                    _fail("Axis ticks must be an array of at most 100 entries.", item_path + ".ticks")
                a, b = entry["from"], entry["to"]
                dx, dy = b[0] - a[0], b[1] - a[1]
                for j, tick in enumerate(ticks):
                    tp = f"{item_path}.ticks[{j}]"
                    _keys(tick, ("id", "at", "label", "offset_mm"), ("id", "at"), tp)
                    identity(tick, tp); point(tick["at"], tp + ".at")
                    tx, ty = tick["at"][0] - a[0], tick["at"][1] - a[1]
                    if abs(tx * dy - ty * dx) > 1e-8 * max(1, abs(dx), abs(dy)) or not -1e-8 <= tx * dx + ty * dy <= dx * dx + dy * dy + 1e-8:
                        _fail("A tick must lie on its supplied axis segment.", tp + ".at")
                    if "label" in tick: text(tick["label"], tp + ".label")
                    if "offset_mm" in tick: _pair(tick["offset_mm"], tp + ".offset_mm")
            elif category == "curves":
                points = entry["points"]
                maximum = 2048 if entry.get("point_mode") == "samples" else 512
                if not isinstance(points, list) or not 2 <= len(points) <= maximum:
                    _fail(f"A curve requires between 2 and {maximum} supplied points.", item_path + ".points")
                for j, p in enumerate(points): point(p, f"{item_path}.points[{j}]")
                if any(a == b for a, b in zip(points, points[1:])):
                    _fail("Consecutive graph anchors must be distinct.", item_path + ".points")
                interpolation = entry.get("interpolation", "linear")
                if interpolation not in ("linear", "smooth"):
                    _fail("Graph interpolation must be linear or smooth.", item_path + ".interpolation")
                if interpolation == "smooth" and any(a[0] >= b[0] for a, b in zip(points, points[1:])):
                    _fail("Smooth graph anchors need strictly increasing x; use explicit linear segments for vertical paths.", item_path + ".points")
                point_mode = entry.get("point_mode", "anchors")
                if point_mode not in ("anchors", "samples"):
                    _fail("Curve point_mode must be anchors or samples.", item_path + ".point_mode")
                if point_mode == "samples":
                    indices = entry.get("anchor_indices")
                    if (not isinstance(indices, list) or len(indices) < 2
                            or any(not isinstance(i, int) or isinstance(i, bool) or not 0 <= i < len(points) for i in indices)
                            or indices[0] != 0 or indices[-1] != len(points)-1
                            or any(a >= b for a, b in zip(indices, indices[1:]))):
                        _fail("Sampled curves require increasing anchor_indices including both endpoints.", item_path + ".anchor_indices")
                elif "anchor_indices" in entry:
                    _fail("anchor_indices applies only to an explicitly sampled curve.", item_path + ".anchor_indices")
            else:
                point(entry["at"], item_path + ".at")
                if category == "markers":
                    if entry["style"] not in ("open", "filled"):
                        _fail("Graph markers must be open or filled.", item_path + ".style")
                    radius = entry.get("radius_mm", .8)
                    if not _finite(radius) or not .3 <= radius <= 3:
                        _fail("Graph marker radius must be between 0.3 and 3 millimetres.", item_path + ".radius_mm")
                else:
                    text(entry["text"], item_path + ".text")
                    if "offset_mm" in entry: _pair(entry["offset_mm"], item_path + ".offset_mm")
    if not total:
        _fail("A graph needs at least one explicit element.", path)
    return spec


def graph_requirements(spec):
    """Only writer-shaped labels and arrows need glyph coverage."""
    validate_graph(spec)
    result = []
    for axis in spec.get("axes", []):
        if axis.get("arrow", True): result.append(("math.operator", "→"))
        for tick in axis.get("ticks", []):
            result.extend(("text", c) for c in regex.findall(r"\X", tick.get("label", "")) if not c.isspace())
    for label in spec.get("labels", []):
        result.extend(("text", c) for c in regex.findall(r"\X", label["text"]) if not c.isspace())
    return result


def graph_text(spec):
    validate_graph(spec)
    return "[" + spec.get("coordinate_kind", "quantitative") + " graph: " + json.dumps(spec, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "]"


def graph_axis_anchors(axis):
    """Endpoints constrain the axis; exact tick centres are separate data.

The reviewed line may meet a tick anywhere within its visible cross-stroke,
rather than being flattened at every tick. The PDF audit verifies contact.
"""
    return [list(axis["from"]), list(axis["to"])]


def graph_curve_anchors(curve):
    """Only an explicit samples declaration relaxes the all-points contract."""
    indices = curve["anchor_indices"] if curve.get("point_mode") == "samples" else range(len(curve["points"]))
    return [list(curve["points"][i]) for i in indices]


def _trace_deviation(trace):
    a, b = trace[0], trace[-1]
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = math.hypot(dx, dy)
    return max(abs((p[0] - a[0]) * dy - (p[1] - a[1]) * dx) / length for p in trace)


def validate_graph_style(profile):
    """Bind measured static graph style to exact, reviewed source observations."""
    model = profile.get("graph_style")
    if model is None:
        return None

    def fail(message):
        raise InkformError("invalid_profile", message, {"path": "/graph_style"})

    if profile.get("synthetic") is True:
        fail("Reviewed personal graph style cannot be attributed to a synthetic profile.")
    required = {"version", "basis", "observation_ids", "source_refs", "pen_width_xheight", "axis_bow_xheight"}
    if not isinstance(model, dict) or set(model) != required or model["version"] != "inkform-graph-style-1" or model["basis"] != "reviewed_samples":
        fail("Graph style must declare the supported reviewed static-source model.")
    identities = model["observation_ids"]
    if not isinstance(identities, list) or not identities or not all(isinstance(i, str) for i in identities) or len(set(identities)) != len(identities):
        fail("Graph style requires unique reviewed observation IDs.")
    width = model["pen_width_xheight"]
    bows = model["axis_bow_xheight"]
    if not _finite(width) or not .02 <= width <= .5 or not isinstance(bows, dict) or set(bows) != {"horizontal", "vertical"} or any(not _finite(v) or not 0 <= v <= 2 for v in bows.values()):
        fail("Graph pen width or axis bow exceeds the supported measured range.")
    observations = {o["observation_id"]: o for o in profile.get("observations", [])}
    sources = {s["source_id"]: s for s in profile.get("sources", [])}
    measured_widths, measured_bows, refs = [], {"horizontal": [], "vertical": []}, []
    for identity in identities:
        observation = observations.get(identity, {})
        source = sources.get(observation.get("source_id"), {})
        actor = observation.get("reviewed_by")
        if observation.get("kind") != "graph_style" or observation.get("review_status") != "reviewed" or not isinstance(actor, str) or not actor.strip():
            fail("Graph style may use only identified, reviewed graph observations.")
        if not source.get("sha256") or observation.get("source_sha256") != source["sha256"]:
            fail("Graph style source hashes must match the stored source bytes.")
        bbox = observation.get("bbox_xywh")
        if not isinstance(bbox, list) or len(bbox) != 4 or not all(_finite(v) for v in bbox) or min(bbox[:2]) < 0 or min(bbox[2:]) <= 0:
            fail("Graph style observations need a valid source-pixel region.")
        source_size = source.get("oriented_size", source.get("dimensions_px"))
        if isinstance(source_size, list) and len(source_size) == 2 and all(_finite(v) for v in source_size):
            if bbox[0] + bbox[2] > source_size[0] or bbox[1] + bbox[3] > source_size[1]:
                fail("Graph style review regions must lie inside the stored source image.")
        xheight, pen_width = observation.get("xheight_px"), observation.get("pen_width_px")
        if not _finite(xheight) or xheight <= 0 or not _finite(pen_width) or pen_width <= 0:
            fail("Graph style measurements need a positive source x-height and ink width.")
        measured_widths.append(pen_width / xheight)
        axes = observation.get("axes", [])
        strokes = observation.get("strokes", [])
        if not isinstance(axes, list) or not isinstance(strokes, list) or not (axes or strokes):
            fail("Graph style observations need explicit reviewed graph traces.")
        for axis in axes:
            orientation = axis.get("orientation") if isinstance(axis, dict) else None
            trace = axis.get("trace_px") if isinstance(axis, dict) else None
            if orientation not in measured_bows or not isinstance(trace, list) or len(trace) < 3:
                fail("Graph axis observations need at least three source trace points and an orientation.")
            if any(not isinstance(p, list) or len(p) != 2 or not all(_finite(v) for v in p)
                   or not bbox[0] <= p[0] <= bbox[0] + bbox[2] or not bbox[1] <= p[1] <= bbox[1] + bbox[3] for p in trace):
                fail("Graph axis trace points must lie in the reviewed source region.")
            if trace[0] == trace[-1]:
                fail("Graph axis traces need distinct endpoints.")
            dx, dy = abs(trace[-1][0] - trace[0][0]), abs(trace[-1][1] - trace[0][1])
            if (orientation == "horizontal" and dx < dy) or (orientation == "vertical" and dy < dx):
                fail("Graph axis orientation conflicts with its source trace.")
            from .graph_traces import _chord_profile
            try:
                _chord_profile(trace, xheight)
            except ValueError as exc:
                fail(str(exc))
            measured_bows[orientation].append(_trace_deviation(trace) / xheight)
        from .graph_traces import validate_stroke_trace
        stroke_ids = set()
        for stroke in strokes:
            if not isinstance(stroke, dict):
                fail("Reviewed graph strokes must be explicit objects.")
            if stroke.get("stroke_id") in stroke_ids:
                fail("Reviewed graph stroke IDs must be unique within an observation.")
            stroke_ids.add(stroke.get("stroke_id"))
            try:
                validate_stroke_trace(stroke, bbox, xheight)
            except ValueError as exc:
                fail(str(exc))
            if stroke["role"].startswith("axis_"):
                measured_bows[stroke["role"].removeprefix("axis_")].append(_trace_deviation(stroke["trace_px"])/xheight)
        refs.append({"source_id": observation["source_id"], "observation_id": identity})
    declared_refs = model["source_refs"]
    if not isinstance(declared_refs, list) or any(not isinstance(r, dict) or set(r) != {"source_id", "observation_id"}
                                                  or not all(isinstance(v, str) for v in r.values()) for r in declared_refs):
        fail("Graph style source references must name exact observations.")
    if sorted(declared_refs, key=lambda r: (r["source_id"], r["observation_id"])) != sorted(refs, key=lambda r: (r["source_id"], r["observation_id"])):
        fail("Graph style source references must exactly identify its reviewed observations.")
    if abs(width - median(measured_widths)) > 1e-7 or any(not measured_bows[key] or abs(bows[key] - max(measured_bows[key])) > 1e-7 for key in bows):
        fail("Graph style amplitudes must match the reviewed source measurements.")
    return model


def _curve_stroke(points, interpolation, signed, amplitude):
    """Return anchored cubic spans; no change to the supplied graph values."""
    tangents = None
    if interpolation == "smooth":
        # x increases in page space; y is inverted but PCHIP remains monotone.
        tangents = PchipInterpolator([p[0] for p in points], [p[1] for p in points]).derivative()
    segments = []
    for index, (a, b) in enumerate(zip(points, points[1:])):
        dx, dy = b[0] - a[0], b[1] - a[1]
        c1 = [a[0] + dx / 3, a[1] + dy / 3]
        c2 = [b[0] - dx / 3, b[1] - dy / 3]
        if tangents is not None:
            c1[1] = a[1] + float(tangents(a[0])) * dx / 3
            c2[1] = b[1] - float(tangents(b[0])) * dx / 3
        length = math.hypot(dx, dy)
        # One broad bend, no independently jittered points. All controls stay
        # inside the interval's coordinate envelope, protecting monotonicity,
        # extrema, plateaus and vertical segments.
        bend = min(amplitude, length * .035) * signed(index)
        for control, weight in ((c1, 1), (c2, .65)):
            control[0] = min(max(a[0], b[0]), max(min(a[0], b[0]), control[0] - dy / length * bend * weight))
            control[1] = min(max(a[1], b[1]), max(min(a[1], b[1]), control[1] + dx / length * bend * weight))
        for axis in (0, 1):
            if (c2[axis] - c1[axis]) * (b[axis] - a[axis]) < 0:
                c1[axis] = c2[axis] = (c1[axis] + c2[axis]) / 2
        segments.append({"type": "cubic", "control1": c1, "control2": c2, "end": list(b)})
    return {"start": list(points[0]), "segments": segments, "closed": False}


def graph_box(spec, shaper, token_id):
    """Make a graph Box; append a separate ledger to ``shaper.graphs``."""
    from .layout import Box, map_strokes
    from .graph_traces import (axis_trace_path, curve_trace_path, graph_evidence, marker_trace_path, sampled_curve_trace_path,
                               select_trace, tick_trace_path, trace_candidates, trace_reference)
    validate_graph(spec)
    width, height = spec["width_mm"], spec["height_mm"]
    padding = spec.get("padding_mm", 12)
    xmin, xmax = spec["x_range"]; ymin, ymax = spec["y_range"]

    def mapped(point):
        return [padding + (point[0] - xmin) / (xmax - xmin) * (width - 2 * padding),
                height - padding - (point[1] - ymin) / (ymax - ymin) * (height - 2 * padding)]

    observed_only = shaper.variation.observed_only
    strength = 0 if observed_only else shaper.variation.strength
    amplitude = min(.14, .085 * strength)
    learned_style = validate_graph_style(shaper.profile)
    evidence = graph_evidence(shaper.profile)
    pen = learned_style["pen_width_xheight"] * shaper.size if learned_style else shaper.profile["pen"]["width_mm"]
    style_refs = copy.deepcopy(learned_style["source_refs"]) if learned_style else []
    result = Box(width)
    ledger = {"graph_id": token_id, "spec": copy.deepcopy(spec), "primitive_token_ids": [],
              "label_token_ids": [], "arrow_token_ids": [],
              "style": {"version": "inkform-graph-1", "pen_width_mm": pen,
                        "pen_basis": "reviewed_graph_ink_width" if learned_style else "writer_profile",
                        "source_refs": style_refs, "arrow_basis": "reviewed_writer_glyphs",
                        "axis_bow_basis": "reviewed_source_trace_transfer" if learned_style else "explicit_graph_geometry" if observed_only else "bounded_estimate",
                        "axis_bow_xheight": copy.deepcopy(learned_style["axis_bow_xheight"]) if learned_style else None,
                        "estimated_axes": [],
                        "mode": shaper.variation.mode,
                        "curve_deviation_basis": "reviewed_source_trace_transfer" if trace_candidates(evidence, "curve") else "explicit_graph_geometry" if observed_only else "bounded_estimate",
                        "maximum_control_deviation_mm": amplitude,
                        "reviewed_transfer_limits_mm": {"axis":1.2,"axis_with_ticks":.45,"curve":.45,"tick":.25},
                        "source_evidence": evidence, "unsupported_primitives": [],
                        "source_fidelity": "reviewed source trace knots and polarity where available; explicit graph geometry for unsupported stroke kinds",
                        "axis_tick_contact": "exact data centres; reviewed axis may cross within 0.45 mm of centre",
                        "data_anchors": "preserved_exactly", "curve_interpolation": "explicit_specification",
                        "sampled_curve_transfer": "source residual along baseline arc length between explicit anchors and coordinate extrema"}}

    def signed(identity, domain):
        return 2 * stable_value(shaper.seed, identity, domain) - 1

    def primitive(kind, name, strokes, data_anchors, *, filled=False, candidate=None):
        identity = f"{token_id}.{kind}.{name}"
        geometry = {"strokes": strokes, "representation": "clean_outline" if filled else "centerline", "pen_width_mm": pen}
        item = {**geometry, "token_id": identity, "glyph_id": "graph." + kind,
                "role": "graph." + kind, "text": f"graph-{kind}:{name}", "semantic": False,
                "content_kind": "graph_primitive", "graph_id": token_id, "graph_kind": kind, "graph_element_id": name,
                "graph_data_anchors": copy.deepcopy(data_anchors), "graph_plot_anchors_mm": [mapped(p) for p in data_anchors],
                "source_refs": [{"source_id": candidate["source_id"], "observation_id": candidate["observation_id"]}] if candidate else [],
                "source_geometry_hash": hashlib.sha256(json.dumps(geometry, sort_keys=True).encode()).hexdigest(),
                "variation": {"basis": "reviewed_source_trace_transfer" if candidate else "explicit_graph_geometry" if observed_only else "bounded_estimate",
                              "mode": shaper.variation.mode, "amount": shaper.variation.amount}}
        if candidate:
            item["graph_trace"] = trace_reference(candidate, shaper.size)
        else:
            ledger["style"]["unsupported_primitives"].append({"token_id": identity, "kind": kind,
                "basis": item["variation"]["basis"], "reason": "no reviewed source stroke of the corresponding kind"})
            if not observed_only and kind not in ledger["style"]["estimated_axes"]:
                ledger["style"]["estimated_axes"].append(kind)
        result.items.append(item); ledger["primitive_token_ids"].append(identity)
        return item

    def label(text, at, offset, identity):
        box = shaper.phrase(text, identity)
        x, y = mapped(at)
        placed = box.shifted(x + offset[0], y + offset[1])
        for item in placed.items:
            item["graph_id"] = token_id; item["graph_kind"] = "label"
            item["graph_data_anchors"] = [list(at)]; item["graph_plot_anchors_mm"] = [[x + offset[0], y + offset[1]]]
            ledger["label_token_ids"].append(item["token_id"])
        result.items.extend(placed.items)

    for axis in spec.get("axes", []):
        a, b = mapped(axis["from"]), mapped(axis["to"])
        dx, dy = b[0] - a[0], b[1] - a[1]
        length = math.hypot(dx, dy)
        anchors = graph_axis_anchors(axis); plot_anchors = [mapped(p) for p in anchors]
        role = "axis_horizontal" if abs(dx) >= abs(dy) else "axis_vertical"
        candidate = select_trace(evidence, role, shaper.seed, f"{token_id}.axis.{axis['id']}")
        if candidate:
            stroke = axis_trace_path(plot_anchors, candidate, shaper.size, has_ticks=bool(axis.get("ticks")))
        else:
            bow = min(amplitude, length*.055)*signed(axis["id"], token_id+"-axis")
            stroke = {"start": a, "segments": [{"type": "cubic",
                "control1": [a[0]+dx/3-dy/length*bow,a[1]+dy/3+dx/length*bow],
                "control2": [a[0]+2*dx/3-dy/length*bow*.6,a[1]+2*dy/3+dx/length*bow*.6], "end": b}], "closed": False}
        primitive("axis", axis["id"], [stroke], anchors, candidate=candidate)
        if axis.get("arrow", True):
            arrow = shaper.glyph("→", "math.operator", f"{token_id}.arrow.{axis['id']}")
            bounds = arrow.bounds
            tip_x, tip_y = bounds[2], (bounds[1] + bounds[3]) / 2
            ux, uy = dx / length, dy / length
            for item in arrow.items:
                item["strokes"] = map_strokes(item["strokes"], lambda x, y: (b[0] + (x - tip_x) * ux - (y - tip_y) * uy,
                                                                            b[1] + (x - tip_x) * uy + (y - tip_y) * ux))
                item["graph_id"] = token_id; item["graph_kind"] = "arrow"
                item["graph_element_id"] = axis["id"]
                item["graph_data_anchors"] = [list(axis["to"])]; item["graph_plot_anchors_mm"] = [list(b)]
                item["graph_axis_direction"] = [ux, uy]
                ledger["arrow_token_ids"].append(item["token_id"])
            result.items.extend(arrow.items)
        nx, ny = -dy / length, dx / length
        for tick in axis.get("ticks", []):
            x, y = mapped(tick["at"])
            half = .8 + .10 * strength * signed(tick["id"], token_id + "-tick")
            candidate = select_trace(evidence, "tick", shaper.seed, f"{token_id}.tick.{tick['id']}")
            stroke = tick_trace_path([x,y], [nx,ny], candidate, shaper.size) if candidate else {
                "start": [x-nx*half,y-ny*half], "segments": [{"type": "line", "end": [x+nx*half,y+ny*half]}], "closed": False}
            primitive("tick", tick["id"], [stroke], [tick["at"]], candidate=candidate)
            if "label" in tick:
                label(tick["label"], tick["at"], tick.get("offset_mm", [-1, 5]), f"{token_id}.tick-label.{tick['id']}")
    for curve in spec.get("curves", []):
        points = [mapped(p) for p in curve["points"]]
        candidate = select_trace(evidence, "curve", shaper.seed, f"{token_id}.curve.{curve['id']}")
        if candidate and curve.get("point_mode") == "samples":
            stroke = sampled_curve_trace_path(points, curve.get("interpolation", "linear"),
                                              curve["anchor_indices"], candidate, shaper.size)
        elif candidate:
            stroke = curve_trace_path(points, curve.get("interpolation", "linear"), candidate, shaper.size)
        else:
            stroke = _curve_stroke(points, curve.get("interpolation", "linear"),
                                   lambda i: signed(curve["id"], f"{token_id}-curve-{i}"), amplitude)
        primitive("curve", curve["id"], [stroke], graph_curve_anchors(curve), candidate=candidate)
    for marker in spec.get("markers", []):
        x, y = mapped(marker["at"]); radius = marker.get("radius_mm", .8)
        # Symmetric opposing handles keep the actual marker centre exact.
        rx = radius * (1 + .025 * strength * signed(marker["id"], token_id + "-marker-width"))
        ry = radius * (1 + .025 * strength * signed(marker["id"], token_id + "-marker-height"))
        k = .5522847498307936
        stroke = {"start": [x + rx, y], "segments": [
            {"type": "cubic", "control1": [x + rx, y + k * ry], "control2": [x + k * rx, y + ry], "end": [x, y + ry]},
            {"type": "cubic", "control1": [x - k * rx, y + ry], "control2": [x - rx, y + k * ry], "end": [x - rx, y]},
            {"type": "cubic", "control1": [x - rx, y - k * ry], "control2": [x - k * rx, y - ry], "end": [x, y - ry]},
            {"type": "cubic", "control1": [x + k * rx, y - ry], "control2": [x + rx, y - k * ry], "end": [x + rx, y]}], "closed": True}
        candidate = select_trace(evidence, "marker_"+marker["style"], shaper.seed, f"{token_id}.marker.{marker['id']}")
        if candidate:
            stroke = marker_trace_path([x,y],radius,candidate)
            # Source contours need enough interior after normalization for the
            # actual pen, just as an explicit ellipse does.
            rx = ry = min(math.dist(p,[x,y]) for p in [stroke["start"]]+[s["end"] for s in stroke["segments"]])
        item = primitive("marker", marker["id"], [stroke], [marker["at"]], filled=marker["style"] == "filled", candidate=candidate)
        if marker["style"] == "open":
            if min(rx, ry) - pen / 2 < .15:
                raise InkformError("graph_marker_too_small", "An open graph marker needs a visible paper interior; increase its radius for this pen width.",
                                   {"graph": token_id, "marker": marker["id"], "radius_mm": radius, "pen_width_mm": pen})
            item["paper_knockout"] = "open_graph_marker"
    for entry in spec.get("labels", []):
        label(entry["text"], entry["at"], entry.get("offset_mm", [0, 0]), f"{token_id}.label.{entry['id']}")
    shaper.graphs = getattr(shaper, "graphs", []) + [ledger]
    return result
