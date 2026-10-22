"""Deterministic transfer of reviewed graph strokes, with no generated wobble.

Source points remain in their source pixel coordinate system in the evidence
ledger. Only longitudinal placement and physical pen scale change. The PDF
auditor runs these same transfers from the evidence, never from output handles.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math

from scipy.interpolate import CubicSpline, PchipInterpolator

from .variation import stable_value


TRANSFER_VERSION = "reviewed-graph-trace-1"
ROLES = {"axis_horizontal", "axis_vertical", "curve", "tick", "marker_open", "marker_filled"}


def trace_hash(candidate):
    return hashlib.sha256(json.dumps(candidate, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def graph_evidence(profile):
    """A minimal, portable copy of the reviewed evidence used by a graph."""
    model = profile.get("graph_style")
    if model is None:
        return None
    observations = [copy.deepcopy(o) for o in profile.get("observations", []) if o["observation_id"] in model["observation_ids"]]
    source_ids = {o["source_id"] for o in observations}
    return {"synthetic": False, "graph_style": copy.deepcopy(model), "observations": observations,
            "sources": [{key: copy.deepcopy(value) for key, value in s.items()
                         if key in {"source_id", "sha256", "oriented_size", "dimensions_px"}}
                        for s in profile.get("sources", []) if s["source_id"] in source_ids]}


def trace_candidates(evidence, role):
    if not evidence or role not in ROLES:
        return []
    result = []
    for observation in evidence["observations"]:
        common = {"source_id": observation["source_id"], "observation_id": observation["observation_id"],
                  "source_sha256": observation["source_sha256"], "xheight_px": observation["xheight_px"]}
        if role.startswith("axis_"):
            for index, axis in enumerate(observation.get("axes", [])):
                if "axis_" + axis["orientation"] == role:
                    result.append({**common, "stroke_id": f"axis-{index}", "role": role,
                                   "trace_px": copy.deepcopy(axis["trace_px"]), "closed": False})
        for stroke in observation.get("strokes", []):
            if stroke["role"] == role:
                result.append({**common, **{key: copy.deepcopy(stroke[key]) for key in
                    ("stroke_id", "role", "trace_px", "closed", "anchors_px", "center_px") if key in stroke}})
    # Exact aliases cannot gain selection weight merely by being listed twice.
    unique = {}
    for entry in sorted(result, key=lambda c: (c["observation_id"], c["stroke_id"])):
        unique.setdefault((entry["source_sha256"], json.dumps(entry["trace_px"])), entry)
    return list(unique.values())


def select_trace(evidence, role, seed, identity):
    candidates = trace_candidates(evidence, role)
    if not candidates:
        return None
    index = min(len(candidates)-1, int(stable_value(seed, identity, "reviewed-graph-trace") * len(candidates)))
    return candidates[index]


def trace_reference(candidate, size):
    return {"version": TRANSFER_VERSION, "source_id": candidate["source_id"],
            "observation_id": candidate["observation_id"], "stroke_id": candidate["stroke_id"],
            "role": candidate["role"], "trace_sha256": trace_hash(candidate), "xheight_mm": size}


def resolve_trace(evidence, reference):
    if not isinstance(reference, dict) or reference.get("version") != TRANSFER_VERSION:
        raise ValueError("graph trace transfer needs its supported evidence reference")
    size = reference.get("xheight_mm")
    if not isinstance(size, (int, float)) or isinstance(size, bool) or not math.isfinite(size) or not 0 < size <= 30:
        raise ValueError("graph trace transfer needs a finite physical x-height")
    for candidate in trace_candidates(evidence, reference.get("role", "")):
        if all(candidate[key] == reference.get(key) for key in ("source_id", "observation_id", "stroke_id")):
            if trace_hash(candidate) != reference.get("trace_sha256"):
                raise ValueError("graph trace transfer differs from its exact reviewed source points")
            return candidate, size
    raise ValueError("graph trace transfer has no corresponding reviewed source stroke")


def _chord_profile(trace, xheight, *, reverse=False):
    trace = list(reversed(trace)) if reverse else trace
    a, b = trace[0], trace[-1]
    dx, dy = b[0]-a[0], b[1]-a[1]
    length = math.hypot(dx, dy)
    if length == 0:
        raise ValueError("reviewed open graph trace needs distinct endpoints")
    u = [((p[0]-a[0])*dx + (p[1]-a[1])*dy)/(length*length) for p in trace]
    residual = [(-(p[0]-a[0])*dy + (p[1]-a[1])*dx)/length/xheight for p in trace]
    u[0], u[-1], residual[0], residual[-1] = 0., 1., 0., 0.
    if any(a >= b for a, b in zip(u, u[1:])):
        raise ValueError("reviewed graph stroke must advance along its source baseline")
    return u, residual


def validate_stroke_trace(stroke, bbox, xheight):
    """Validate optional role-specific observations, without claiming review."""
    role, trace = stroke.get("role"), stroke.get("trace_px")
    if role not in ROLES or not isinstance(stroke.get("stroke_id"), str) or not stroke["stroke_id"]:
        raise ValueError("reviewed graph stroke needs a unique ID and a supported role")
    if not isinstance(trace, list) or not 3 <= len(trace) <= 2048:
        raise ValueError("reviewed graph stroke needs 3 to 2048 ordered source points")
    for point in trace:
        if (not isinstance(point, list) or len(point) != 2 or any(not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v) for v in point)
                or not bbox[0] <= point[0] <= bbox[0]+bbox[2] or not bbox[1] <= point[1] <= bbox[1]+bbox[3]):
            raise ValueError("reviewed graph stroke points must lie inside their source region")
    if stroke.get("review_status", "reviewed") != "reviewed":
        raise ValueError("unreviewed graph strokes cannot supply personal geometry")
    if role.startswith("marker_"):
        if stroke.get("closed") is not True or trace[0] != trace[-1]:
            raise ValueError("reviewed marker contours must explicitly close")
        center = stroke.get("center_px")
        if not isinstance(center, list) or len(center) != 2 or not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in center):
            raise ValueError("reviewed markers need an explicit source centre")
        radial = [math.dist(p, center) for p in trace[:-1]]
        if not radial or min(radial) <= 0 or max(radial)/min(radial) > 3:
            raise ValueError("reviewed marker contour must enclose a bounded central interior")
        angles = [math.atan2(p[1]-center[1], p[0]-center[0]) for p in trace]
        turns = [(b-a+math.pi)%(2*math.pi)-math.pi for a,b in zip(angles, angles[1:])]
        if (any(abs(t) >= math.pi*.75 or abs(t) < 1e-9 for t in turns)
                or not (all(t > 0 for t in turns) or all(t < 0 for t in turns))
                or abs(abs(sum(turns))-2*math.pi) > 1e-6):
            raise ValueError("reviewed marker points must wind once around their centre")
    else:
        if stroke.get("closed") is not False:
            raise ValueError("reviewed axes, curves and ticks must be open strokes")
        if role == "curve":
            anchors = stroke.get("anchors_px")
            if (not isinstance(anchors, list) or len(anchors) < 2 or anchors[0] != trace[0] or anchors[-1] != trace[-1]
                    or any(anchor not in trace for anchor in anchors)):
                raise ValueError("reviewed curve residuals need explicit source baseline anchors on the trace")
            indices = [trace.index(anchor) for anchor in anchors]
            if any(a >= b for a,b in zip(indices, indices[1:])):
                raise ValueError("reviewed curve anchors must follow source trace order")
            for a,b in zip(indices, indices[1:]):
                _chord_profile(trace[a:b+1], xheight)
        else:
            _chord_profile(trace, xheight)
            if role.startswith("axis_"):
                dx,dy=abs(trace[-1][0]-trace[0][0]),abs(trace[-1][1]-trace[0][1])
                if (role=="axis_horizontal" and dx<dy) or (role=="axis_vertical" and dy<dx):
                    raise ValueError("reviewed graph axis role conflicts with its source orientation")
            if role=="tick":
                axis=0 if abs(trace[-1][0]-trace[0][0])>=abs(trace[-1][1]-trace[0][1]) else 1
                direction=trace[-1][axis]-trace[0][axis]
                if any((b[axis]-a[axis])*direction<=0 for a,b in zip(trace,trace[1:])):
                    raise ValueError("reviewed tick must advance along its principal source direction")


def _hermite_stroke(knots, values, derivatives):
    segments = []
    for i in range(len(knots)-1):
        span = (knots[i+1]-knots[i])/3
        segments.append({"type": "cubic",
                         "control1": [values[i][j]+derivatives[i][j]*span for j in (0,1)],
                         "control2": [values[i+1][j]-derivatives[i+1][j]*span for j in (0,1)],
                         "end": list(values[i+1])})
    return {"start": list(values[0]), "segments": segments, "closed": False}


def axis_trace_path(anchors, candidate, size, *, has_ticks=False):
    trace = candidate["trace_px"]
    role = candidate["role"]
    reverse = trace[-1][0] < trace[0][0] if role == "axis_horizontal" else trace[-1][1] > trace[0][1]
    knots, residual = _chord_profile(trace, candidate["xheight_px"], reverse=reverse)
    a, b = anchors[0], anchors[-1]
    dx, dy = b[0]-a[0], b[1]-a[1]
    length = math.hypot(dx, dy)
    # Preserve screen-space polarity even when an explicit axis points left/down.
    target_reverse = dx < 0 if role == "axis_horizontal" else dy > 0
    if target_reverse:
        knots, residual = [1-u for u in reversed(knots)], [-v for v in reversed(residual)]
    cap = min(.45 if has_ticks else 1.2, length*.025)
    scale = min(size, cap/max(max(abs(v) for v in residual), 1e-12))
    curve = PchipInterpolator(knots, residual)
    values, derivatives = [], []
    for u in knots:
        shift, slope = float(curve(u))*scale, float(curve.derivative()(u))*scale
        values.append([a[0]+dx*u-dy/length*shift, a[1]+dy*u+dx/length*shift])
        derivatives.append([dx-dy/length*slope, dy+dx/length*slope])
    values[0], values[-1] = list(a), list(b)
    return _hermite_stroke(knots, values, derivatives)


def curve_trace_path(anchors, interpolation, candidate, size):
    trace, source_anchors = candidate["trace_px"], candidate["anchors_px"]
    indices = [trace.index(point) for point in source_anchors]
    profiles = [_chord_profile(trace[a:b+1], candidate["xheight_px"]) for a,b in zip(indices, indices[1:])]
    baseline = PchipInterpolator([p[0] for p in anchors], [p[1] for p in anchors]) if interpolation == "smooth" else None
    result = {"start": list(anchors[0]), "segments": [], "closed": False}
    for index, (a,b) in enumerate(zip(anchors, anchors[1:])):
        knots, residual = profiles[index % len(profiles)]
        dx, dy = b[0]-a[0], b[1]-a[1]
        length = math.hypot(dx,dy)
        # A declared plateau or vertical jump must remain exactly that.
        cap = min(.45, length*.015) if dx and dy else 0
        scale = min(size, cap/max(max(abs(v) for v in residual), 1e-12))
        shape = PchipInterpolator(knots, residual)
        values, derivatives = [], []
        for u in knots:
            x = a[0]+dx*u
            y = float(baseline(x)) if baseline is not None else a[1]+dy*u
            slope = float(baseline.derivative()(x))*dx if baseline is not None else dy
            bend, bend_slope = float(shape(u))*scale, float(shape.derivative()(u))*scale
            values.append([x-dy/length*bend, y+dx/length*bend])
            derivatives.append([dx-dy/length*bend_slope, slope+dx/length*bend_slope])
        values[0], values[-1] = list(a), list(b)
        # Preserve each specified monotone interval, its extrema and anchors.
        for axis in (0,1):
            low, high = sorted((a[axis], b[axis]))
            for i in range(1,len(values)-1):
                values[i][axis] = min(high,max(low,values[i][axis]))
                values[i][axis] = max(values[i-1][axis],values[i][axis]) if b[axis]>=a[axis] else min(values[i-1][axis],values[i][axis])
        stroke = _hermite_stroke(knots, values, derivatives)
        start = stroke["start"]
        for segment in stroke["segments"]:
            end = segment["end"]
            for axis in (0,1):
                low, high = sorted((start[axis],end[axis]))
                for key in ("control1","control2"):
                    segment[key][axis] = min(high,max(low,segment[key][axis]))
                if (segment["control2"][axis]-segment["control1"][axis])*(end[axis]-start[axis]) < 0:
                    segment["control1"][axis] = segment["control2"][axis] = (segment["control1"][axis]+segment["control2"][axis])/2
            start = end
        result["segments"].extend(stroke["segments"])
    return result


def tick_trace_path(center, direction, candidate, size):
    trace = candidate["trace_px"]
    a,b = trace[0],trace[-1]
    source_axis = 0 if abs(b[0]-a[0]) >= abs(b[1]-a[1]) else 1
    trace = list(reversed(trace)) if b[source_axis] < a[source_axis] else trace
    a,b = trace[0],trace[-1]
    source_center = [(a[i]+b[i])/2 for i in (0,1)]
    half_length = (b[source_axis]-a[source_axis])/2
    u = [(p[source_axis]-a[source_axis])/(2*half_length) for p in trace]
    # The observed lean remains relative to the tick's ideal direction.
    polarity = -1 if source_axis==1 else 1
    residual = [polarity*(p[1-source_axis]-source_center[1-source_axis])/candidate["xheight_px"] for p in trace]
    curve = PchipInterpolator(u,residual)
    centre_residual = float(curve(.5))
    scale = min(size,.25/max(max(abs(v-centre_residual) for v in residual),1e-12))
    dx,dy = direction
    values,derivatives = [],[]
    for t in u:
        along = (2*t-1)*.8
        across = (float(curve(t))-centre_residual)*scale
        slope = float(curve.derivative()(t))*scale
        values.append([center[0]+dx*along-dy*across,center[1]+dy*along+dx*across])
        derivatives.append([dx*1.6-dy*slope,dy*1.6+dx*slope])
    return _hermite_stroke(u,values,derivatives)


def marker_trace_path(center, radius, candidate):
    source_center, trace = candidate["center_px"], candidate["trace_px"]
    points = [[p[i]-source_center[i] for i in (0,1)] for p in trace]
    knots = [0.]
    for a,b in zip(points,points[1:]):
        knots.append(knots[-1]+math.dist(a,b))
    splines = [CubicSpline(knots,[p[i] for p in points],bc_type="periodic") for i in (0,1)]
    path = _hermite_stroke(knots,points,[[float(s.derivative()(u)) for s in splines] for u in knots])
    all_points = [path["start"]]+[p for s in path["segments"] for k,p in s.items() if k in {"control1","control2","end"}]
    scale = radius/max(math.hypot(*p) for p in all_points)
    from .layout import map_strokes
    path = map_strokes([path],lambda x,y:(center[0]+x*scale,center[1]+y*scale))[0]
    path["closed"] = True
    return path


def path_distance(path, expected):
    if bool(path.get("closed")) != bool(expected.get("closed")) or len(path["segments"]) != len(expected["segments"]):
        return math.inf
    distance = math.dist(path["start"],expected["start"])
    for a,b in zip(path["segments"],expected["segments"]):
        if a["type"] != b["type"]:
            return math.inf
        distance = max(distance,*(math.dist(a[key],b[key]) for key in ("end","control1","control2") if key in b))
    return distance


def path_samples(path, divisions=24):
    """Points used for contact/interior checks, not to produce output geometry."""
    result, start = [path["start"]], path["start"]
    for segment in path["segments"]:
        end = segment["end"]
        if segment["type"] == "line":
            result.append(end)
        else:
            a,b = segment["control1"],segment["control2"]
            for index in range(1,divisions+1):
                t=index/divisions; q=1-t
                result.append([q**3*start[i]+3*q*q*t*a[i]+3*q*t*t*b[i]+t**3*end[i] for i in (0,1)])
        start=end
    return result


def paths_touch(axis_path, tick_path, tolerance=.008):
    axis, tick = path_samples(axis_path), path_samples(tick_path)
    bounds = [min(p[i] for p in tick)-tolerance for i in (0,1)]+[max(p[i] for p in tick)+tolerance for i in (0,1)]
    def point_segment(p,a,b):
        delta=[b[i]-a[i] for i in (0,1)]; length=sum(v*v for v in delta)
        u=max(0,min(1,sum((p[i]-a[i])*delta[i] for i in (0,1))/length)) if length else 0
        return math.dist(p,[a[i]+u*delta[i] for i in (0,1)])
    def cross(a,b,c):
        return (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])
    for a,b in zip(axis,axis[1:]):
        if any(max(a[i],b[i]) < bounds[i] or min(a[i],b[i]) > bounds[i+2] for i in (0,1)):
            continue
        for c,d in zip(tick,tick[1:]):
            if (all(max(min(a[i],b[i]),min(c[i],d[i])) <= min(max(a[i],b[i]),max(c[i],d[i])) for i in (0,1))
                    and cross(a,b,c)*cross(a,b,d) <= 0 and cross(c,d,a)*cross(c,d,b) <= 0):
                return True
            if min(point_segment(a,c,d),point_segment(b,c,d),point_segment(c,a,b),point_segment(d,a,b)) <= tolerance:
                return True
    return False
