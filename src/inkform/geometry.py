"""Conservative static geometry proposals from already isolated ink evidence.

The graph paths describe the visible skeleton, never recovered pen chronology.
All geometry remains a proposal until reviewed against its source crop.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict

import numpy as np
from scipy import ndimage
from skimage.measure import approximate_polygon, find_contours
from skimage.morphology import skeletonize


GEOMETRY_VERSION = "inkform-isolated-ink-1"


def _point(x: float, y: float, baseline: float, xheight: float) -> list[float]:
    return [round(float(x) / xheight, 7), round((float(y) - baseline) / xheight, 7)]


def _rounded_path(points: np.ndarray, baseline: float, xheight: float, closed: bool) -> dict:
    """Round polygon corners inside their convex hull; preserve endpoints.

    Rounding is confined to 0.7 source pixels or 0.02 x-height. This removes
    pixel stairs without Catmull-Rom overshoot into a loop or neighboring mark.
    """
    pts = np.asarray(points, dtype=float)
    if closed and np.allclose(pts[0], pts[-1]):
        pts = pts[:-1]
    radius = max(0.25, min(0.7, xheight * 0.02))
    if len(pts) < 3:
        return {"start": _point(*pts[0], baseline, xheight),
                "segments": [{"type": "line", "end": _point(*p, baseline, xheight)} for p in pts[1:]],
                "closed": closed}
    entries, exits = [], []
    for i, p in enumerate(pts):
        if not closed and i in (0, len(pts) - 1):
            entries.append(p); exits.append(p)
            continue
        prev, nxt = pts[(i - 1) % len(pts)], pts[(i + 1) % len(pts)]
        before, after = prev - p, nxt - p
        d1, d2 = np.linalg.norm(before), np.linalg.norm(after)
        r = min(radius, d1 * 0.35, d2 * 0.35)
        entries.append(p + before * r / max(d1, 1e-12))
        exits.append(p + after * r / max(d2, 1e-12))
    start = exits[0] if closed else pts[0]
    segments = []
    indices = list(range(1, len(pts))) + ([0] if closed else [])
    for i in indices:
        p, entry, out = pts[i], entries[i], exits[i]
        segments.append({"type": "line", "end": _point(*entry, baseline, xheight)})
        if not np.allclose(entry, out):
            # Quadratic corner rounding represented as a cubic Bezier.
            c1 = entry + (p - entry) * (2 / 3)
            c2 = out + (p - out) * (2 / 3)
            segments.append({"type": "cubic", "control1": _point(*c1, baseline, xheight),
                             "control2": _point(*c2, baseline, xheight),
                             "end": _point(*out, baseline, xheight)})
    return {"start": _point(*start, baseline, xheight), "segments": segments, "closed": closed}


def skeleton_paths(binary: np.ndarray) -> list[tuple[np.ndarray, bool]]:
    """Decompose every component into edge-disjoint graph paths, including dots.

    Ignore a diagonal edge when an orthogonal route already joins the same
    pixels. This avoids graph triangles at ordinary ninety-degree bends.
    Paths are deterministically sorted; that order has no temporal meaning.
    """
    skeleton = skeletonize(np.asarray(binary, dtype=bool))
    pixels = {(int(x), int(y)) for y, x in np.argwhere(skeleton)}
    graph: dict[tuple, set] = defaultdict(set)
    for x, y in sorted(pixels):
        p = (x, y)
        graph[p]
        for dx, dy in ((1, 0), (0, 1), (1, 1), (1, -1)):
            q = (x + dx, y + dy)
            if q not in pixels:
                continue
            if dx and dy and ((x + dx, y) in pixels or (x, y + dy) in pixels):
                continue
            graph[p].add(q); graph[q].add(p)
    seen = set()
    paths = []

    def edge(a, b):
        return tuple(sorted((a, b)))

    def walk(a, b):
        path = [a, b]
        seen.add(edge(a, b))
        previous, current = a, b
        while len(graph[current]) == 2:
            other = next(p for p in sorted(graph[current]) if p != previous)
            key = edge(current, other)
            if key in seen:
                break
            seen.add(key); path.append(other)
            previous, current = current, other
            if current == path[0]:
                break
        return np.array(path, dtype=float), path[-1] == path[0]

    for node in sorted(graph):
        if not graph[node]:
            # A tiny ink dot is a visible round-cap stroke, never dust by fiat.
            x, y = node
            paths.append((np.array([[x - .05, y], [x + .05, y]]), False))
        elif len(graph[node]) != 2:
            for neighbor in sorted(graph[node]):
                if edge(node, neighbor) not in seen:
                    paths.append(walk(node, neighbor))
    for node in sorted(graph):
        for neighbor in sorted(graph[node]):
            if edge(node, neighbor) not in seen:
                paths.append(walk(node, neighbor))
    return paths


def reconstruct_geometry(alpha: np.ndarray, baseline: float, xheight: float,
                         representation: str = "centerline") -> dict:
    """Return normalized geometry and diagnostics; no review is implied.

    Input alpha is white ink on black paper/background, as in the prototype.
    y=0 is the supplied baseline; positive y is below it; x-height is one.
    """
    if representation not in {"centerline", "clean_outline"}:
        raise ValueError("representation must be centerline or clean_outline")
    if not math.isfinite(float(baseline)) or not math.isfinite(float(xheight)) or xheight <= 0:
        raise ValueError("baseline and positive xheight must be finite")
    values = np.asarray(alpha, dtype=float)
    if values.ndim != 2 or not values.size or not np.isfinite(values).all():
        raise ValueError("ink evidence must be a finite two-dimensional array")
    if values.max() > 1:
        values = values / 255
    values = np.clip(values, 0, 1)
    sigma = max(.35, min(.85, xheight * .016))
    clean = ndimage.gaussian_filter(values, sigma=sigma)
    binary = clean >= .38
    if not binary.any():
        raise ValueError("ink evidence is empty after segmentation")
    components, count = ndimage.label(binary, structure=np.ones((3, 3)))
    distances = ndimage.distance_transform_edt(binary)
    skeleton = skeletonize(binary)
    width_px = float(np.median(distances[skeleton]) * 2)
    paths = []
    if representation == "clean_outline":
        # Padding guarantees closed outer contours even for an edge-touching mark.
        for contour in find_contours(np.pad(clean, 2), .38, fully_connected="high"):
            points = contour[:, ::-1] - 2
            points = approximate_polygon(points, tolerance=max(.28, xheight * .009))
            if len(points) >= 4:
                paths.append(_rounded_path(points, baseline, xheight, True))
    else:
        for points, closed in skeleton_paths(binary):
            if len(points) >= 3:
                points = approximate_polygon(points, tolerance=max(.4, xheight * .012))
            paths.append(_rounded_path(points, baseline, xheight, closed))
    if not paths:
        raise ValueError("no finite geometry could be reconstructed")
    all_points = []
    for path in paths:
        all_points.append(path["start"])
        for segment in path["segments"]:
            all_points.append(segment["end"])
            if segment["type"] == "cubic":
                all_points.extend([segment["control1"], segment["control2"]])
    xy = np.array(all_points)
    # Conservative bounds enclose every control point, hence every Bezier curve.
    pad = width_px / xheight / 2 if representation == "centerline" else 0
    bounds = [round(float(xy[:, 0].min() - pad), 7), round(float(xy[:, 1].min() - pad), 7),
              round(float(xy[:, 0].max() + pad), 7), round(float(xy[:, 1].max() + pad), 7)]
    result = {"representation": representation, "strokes": paths, "ink_bounds": bounds,
              "advance": round(max(bounds[2] + .12, values.shape[1] / xheight + .07), 7),
              "reconstruction": {"algorithm": GEOMETRY_VERSION, "segmentation_threshold": .38,
                                 "smoothing_sigma_px": sigma, "component_count": int(count),
                                 "skeleton_path_count": len(paths), "estimated_ink_width_px": width_px,
                                 "estimated_ink_width_xheight": width_px / xheight,
                                 "movement_observed": False,
                                 "uncertainties": ["photo does not reveal stroke order, timing, or pressure",
                                                   "baseline and x-height are estimates from source neighbors"]}}
    result["geometry_sha256"] = geometry_hash(result)
    return result


def geometry_hash(geometry: dict) -> str:
    fields = {k: geometry[k] for k in ("representation", "strokes", "ink_bounds", "advance")}
    return hashlib.sha256(json.dumps(fields, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
