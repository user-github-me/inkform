"""Bounded, topology-preserving refinements of closed handwriting outlines.

This replaces pixel-scale straight facets with fitted periodic cubic splines.
The resulting geometry is a new proposal, never an automatic approval of a
source observation or a reconstruction of the writer's pen movement.
"""

from __future__ import annotations

import copy
import math
import warnings
from collections import defaultdict

import numpy as np
from scipy.interpolate import splev, splprep
from scipy.spatial import cKDTree

from .geometry import geometry_hash
from .profiles import geometry_bounds


SMOOTHING_VERSION = "inkform-periodic-outline-1"


def _cubic_flatten(p0, p1, p2, p3, tolerance, depth=0):
    chord = p3 - p0
    length = np.linalg.norm(chord)
    if length < 1e-12:
        error = max(np.linalg.norm(p1-p0), np.linalg.norm(p2-p0))
    else:
        # Distance to the finite chord also detects a collinear backtracking
        # curve. Distance to its infinite line alone would miss that cusp.
        def distance(point):
            projection = np.clip(np.dot(point-p0, chord) / (length*length), 0, 1)
            return np.linalg.norm(point - (p0+projection*chord))
        error = max(distance(p1), distance(p2))
    if error <= tolerance or depth >= 20:
        return [p3]
    a, b, c = (p0+p1)/2, (p1+p2)/2, (p2+p3)/2
    d, e = (a+b)/2, (b+c)/2
    m = (d+e)/2
    return (_cubic_flatten(p0, a, d, m, tolerance, depth+1)
            + _cubic_flatten(m, e, c, p3, tolerance, depth+1))


def flatten_outline(stroke: dict, tolerance: float = .0002) -> np.ndarray:
    """Return a closed polyline with cubic flatness bounded in x-height units."""
    current = np.asarray(stroke["start"], dtype=float)
    points = [current]
    for segment in stroke["segments"]:
        end = np.asarray(segment["end"], dtype=float)
        if segment["type"] == "line":
            points.append(end)
        elif segment["type"] == "cubic":
            points.extend(_cubic_flatten(current, np.asarray(segment["control1"], dtype=float),
                np.asarray(segment["control2"], dtype=float), end, tolerance))
        else:
            raise ValueError("Only line and cubic outline segments can be refined")
        current = end
    points = np.asarray(points)
    if not np.isfinite(points).all():
        raise ValueError("Outline coordinates must be finite")
    # Remove duplicate vertices so arc length is strictly increasing.
    keep = np.r_[True, np.linalg.norm(np.diff(points, axis=0), axis=1) > 1e-10]
    points = points[keep]
    if not np.allclose(points[-1], points[0], atol=1e-10, rtol=0):
        points = np.vstack([points, points[0]])
    else:
        points[-1] = points[0]
    return points


def _resample(points: np.ndarray, spacing: float, minimum: int = 16) -> np.ndarray:
    lengths = np.linalg.norm(np.diff(points, axis=0), axis=1)
    cumulative = np.r_[0, np.cumsum(lengths)]
    count = max(minimum, math.ceil(cumulative[-1] / spacing))
    distances = np.linspace(0, cumulative[-1], count+1)
    return np.column_stack([np.interp(distances, cumulative, points[:, axis]) for axis in (0, 1)])


def _area(points: np.ndarray) -> float:
    return float(np.sum(points[:-1, 0]*points[1:, 1] - points[1:, 0]*points[:-1, 1]) / 2)


def _centroid(points: np.ndarray) -> np.ndarray:
    cross = points[:-1, 0]*points[1:, 1] - points[1:, 0]*points[:-1, 1]
    return np.sum((points[:-1]+points[1:])*cross[:, None], axis=0) / (6*_area(points))


def _retain_ink_area(path: dict, original: np.ndarray, flatness: float) -> dict:
    """Constrain fitted contour area/centroid so smoothing does not thin the ink."""
    fitted = flatten_outline(path, flatness)
    ratio = _area(original) / _area(fitted)
    if ratio <= 0:
        return path  # Orientation guard below will reject this fit.
    factor = math.sqrt(ratio)
    source_center, fitted_center = _centroid(original), _centroid(fitted)
    def point(p):
        value = source_center+(np.asarray(p)-fitted_center)*factor
        return [round(float(v), 8) for v in value]
    result = copy.deepcopy(path)
    result["start"] = point(result["start"])
    for segment in result["segments"]:
        for key in ("control1", "control2", "end"):
            segment[key] = point(segment[key])
    return result


def _contains(points: np.ndarray, point: np.ndarray) -> bool:
    a, b = points[:-1], points[1:]
    crossing = (a[:, 1] > point[1]) != (b[:, 1] > point[1])
    with np.errstate(divide="ignore", invalid="ignore"):
        xs = a[:, 0] + (point[1]-a[:, 1]) * (b[:, 0]-a[:, 0]) / (b[:, 1]-a[:, 1])
    return bool(np.count_nonzero(crossing & (point[0] < xs)) % 2)


def _parents(contours: list[np.ndarray]) -> list[int | None]:
    areas = [abs(_area(points)) for points in contours]
    parents = []
    for i, points in enumerate(contours):
        enclosing = [j for j, other in enumerate(contours)
                     if areas[j] > areas[i] and _contains(other, points[0])]
        parents.append(min(enclosing, key=lambda j: areas[j]) if enclosing else None)
    return parents


def _cross(a, b):
    return a[0] * b[1] - a[1] * b[0]


def _segments_intersect(a, b, c, d, epsilon=1e-10):
    # Includes nonadjacent tangencies, which could pinch an ink component apart.
    if (max(a[0], b[0]) < min(c[0], d[0])-epsilon or
        max(c[0], d[0]) < min(a[0], b[0])-epsilon or
        max(a[1], b[1]) < min(c[1], d[1])-epsilon or
        max(c[1], d[1]) < min(a[1], b[1])-epsilon):
        return False
    u, v = _cross(b-a, c-a), _cross(b-a, d-a)
    w, z = _cross(d-c, a-c), _cross(d-c, b-c)
    return ((u >= -epsilon and v <= epsilon) or (v >= -epsilon and u <= epsilon)) and \
           ((w >= -epsilon and z <= epsilon) or (z >= -epsilon and w <= epsilon))


def _intersections(contours: list[np.ndarray]) -> bool:
    """Grid-index polyline segments to catch self/contour crossings efficiently."""
    segments = []
    for contour_index, points in enumerate(contours):
        for index in range(len(points)-1):
            segments.append((contour_index, index, len(points)-1, points[index], points[index+1]))
    all_points = np.vstack(contours)
    diagonal = np.linalg.norm(np.ptp(all_points, axis=0))
    cell_width = max(.015, diagonal / max(8, math.sqrt(len(segments))))
    grid = defaultdict(list)
    checked = set()
    for index, (contour, offset, count, a, b) in enumerate(segments):
        lo = np.floor(np.minimum(a, b) / cell_width).astype(int)
        hi = np.floor(np.maximum(a, b) / cell_width).astype(int)
        for x in range(lo[0], hi[0]+1):
            for y in range(lo[1], hi[1]+1):
                key = (x, y)
                for other_index in grid[key]:
                    pair = (other_index, index)
                    if pair in checked:
                        continue
                    checked.add(pair)
                    other_contour, other_offset, _, c, d = segments[other_index]
                    if contour == other_contour and (abs(offset-other_offset) == 1 or {offset, other_offset} == {0, count-1}):
                        continue
                    if _segments_intersect(a, b, c, d):
                        return True
                grid[key].append(index)
    return False


def _ink_area(contours: list[np.ndarray], parents: list[int | None]) -> float:
    total = 0
    for index, points in enumerate(contours):
        depth, ancestor = 0, parents[index]
        while ancestor is not None:
            depth += 1; ancestor = parents[ancestor]
        total += abs(_area(points)) * (-1 if depth % 2 else 1)
    return total


def outline_topology(strokes: list[dict], tolerance: float = .0002) -> dict:
    """Return geometric contour topology for diagnostics and synthetic tests."""
    contours = [flatten_outline(stroke, tolerance) for stroke in strokes]
    parents = _parents(contours)
    depths = []
    for parent in parents:
        depth = 0
        while parent is not None:
            depth += 1; parent = parents[parent]
        depths.append(depth)
    return {"contour_count": len(contours), "parents": parents,
            "components": sum(depth % 2 == 0 for depth in depths),
            "holes": sum(depth % 2 == 1 for depth in depths),
            "intersections": _intersections(contours),
            "ink_area": _ink_area(contours, parents)}


def _spline_path(points: np.ndarray, tolerance: float, strength: float) -> dict:
    samples = _resample(points, spacing=max(.001, tolerance*.20), minimum=32)
    lengths = np.linalg.norm(np.diff(samples, axis=0), axis=1)
    parameters = np.r_[0, np.cumsum(lengths)]
    parameters /= parameters[-1]
    smoothing = len(samples) * (tolerance * .65)**2 * strength
    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        tck, _ = splprep(samples.T, u=parameters, s=smoothing, per=True, k=3)
    knots = sorted(set(float(knot) for knot in tck[0] if 0 <= knot <= 1))
    segments = []
    def point(values):
        return [round(float(v), 8) for v in values]
    first = np.asarray(splev(0, tck))
    for left, right in zip(knots, knots[1:]):
        start, end = np.asarray(splev(left, tck)), np.asarray(splev(right, tck))
        derivative0, derivative1 = np.asarray(splev(left, tck, der=1)), np.asarray(splev(right, tck, der=1))
        span = (right-left) / 3
        segments.append({"type": "cubic", "control1": point(start+span*derivative0),
                         "control2": point(end-span*derivative1), "end": point(end)})
    segments[-1]["end"] = point(first)
    return {"start": point(first), "segments": segments, "closed": True}


def _boundary_deviation(original: np.ndarray, candidate: np.ndarray, spacing: float) -> float:
    original_samples = _resample(original, spacing)
    candidate_samples = _resample(candidate, spacing)
    forward = cKDTree(original_samples).query(candidate_samples)[0].max()
    reverse = cKDTree(candidate_samples).query(original_samples)[0].max()
    # A sampled estimate is reported as such. The caller leaves a spacing-sized
    # margin to cover the distance between samples and flattening error.
    return float(max(forward, reverse))


def smooth_outline_glyph(glyph: dict, tolerance_xheight: float = .025) -> dict:
    """Fit periodic cubic contours within a guarded displacement envelope.

    Advance, baseline convention, anchors, role, and source references survive.
    Every successful output is marked proposed pending comparison review.
    Raises ValueError for invalid/self-intersecting evidence or if no bounded
    refinement can preserve its topology; no unreviewed fallback is hidden.
    """
    if glyph.get("representation") != "clean_outline":
        raise ValueError("Outline smoothing requires clean_outline geometry")
    if not math.isfinite(tolerance_xheight) or not 0 < tolerance_xheight <= .1:
        raise ValueError("tolerance_xheight must be finite and within (0, 0.1]")
    if not glyph.get("strokes") or not all(path.get("closed") for path in glyph["strokes"]):
        raise ValueError("Every outline contour must be explicitly closed")
    flatness = min(.0002, tolerance_xheight / 60)
    original = [flatten_outline(stroke, flatness) for stroke in glyph["strokes"]]
    if any(len(points) < 4 or abs(_area(points)) <= 1e-10 for points in original):
        raise ValueError("Outline contours must have positive area and at least three vertices")
    if _intersections(original):
        raise ValueError("Source outline has intersecting contours; manual repair is required")
    parents = _parents(original)
    area = _ink_area(original, parents)
    # Protect detached dots and small holes from the body-sized tolerance.
    local_tolerances = [min(tolerance_xheight, float(np.ptp(points, axis=0).min()) * .18) for points in original]
    diagnostics = []
    result_paths = None
    for retry in range(12):
        strength = 2.0 ** -retry
        try:
            paths = [_spline_path(points, local_tolerance, strength)
                     for points, local_tolerance in zip(original, local_tolerances)]
            paths = [_retain_ink_area(path, points, flatness) for path, points in zip(paths, original)]
        except (ValueError, RuntimeWarning):
            continue
        contours = [flatten_outline(path, flatness) for path in paths]
        # The margin covers sampling along both outlines plus curve flattening.
        spacing = min(.001, min(local_tolerances)/16)
        deviations = [_boundary_deviation(old, new, spacing) for old, new in zip(original, contours)]
        within_bounds = all(deviation+spacing+2*flatness <= tolerance
                            for deviation, tolerance in zip(deviations, local_tolerances))
        directions = all(_area(old)*_area(new) > 0 for old, new in zip(original, contours))
        contour_areas = all(.78 <= abs(_area(new)/_area(old)) <= 1.22 for old, new in zip(original, contours))
        ratio = _ink_area(contours, parents) / area
        if not within_bounds or not directions or not contour_areas or not .95 <= ratio <= 1.05:
            continue
        if _parents(contours) != parents or _intersections(contours):
            continue
        result_paths = paths
        diagnostics = [{"contour": index, "tolerance_xheight": round(tolerance, 8),
                        "sampled_bidirectional_deviation_xheight": round(deviation, 8),
                        "deviation_upper_bound_xheight": round(deviation+spacing+2*flatness, 8),
                        "old_segments": len(glyph["strokes"][index]["segments"]),
                        "new_cubic_segments": len(paths[index]["segments"])}
                       for index, (tolerance, deviation) in enumerate(zip(local_tolerances, deviations))]
        break
    if result_paths is None:
        raise ValueError("No smooth outline satisfied deviation, area, and topology guards")
    candidate = copy.deepcopy(glyph)
    candidate["strokes"] = result_paths
    bounds = geometry_bounds(result_paths)
    candidate["ink_bounds"] = [round(bounds[0]-1e-8, 8), round(bounds[1]-1e-8, 8),
                               round(bounds[2]+1e-8, 8), round(bounds[3]+1e-8, 8)]
    candidate["geometry_status"] = "proposed"
    candidate["geometry_sha256"] = geometry_hash(candidate)
    previous_actor = glyph.get("reviewed_by")
    candidate["refinement"] = {"algorithm": SMOOTHING_VERSION,
        "previous_geometry_sha256": geometry_hash(glyph),
        "previous_geometry_status": glyph.get("geometry_status", "proposed"),
        "previous_geometry_reviewed_by": previous_actor,
        "label_review_retained": True, "geometry_review_status": "proposed",
        "tolerance_xheight": tolerance_xheight, "smoothing_strength": strength,
        "area_ratio": round(ratio, 8), "contour_count": len(original),
        "containment_parents": parents, "topology_preserved": True,
        "curve_flatness_xheight": flatness, "distance_sample_spacing_xheight": spacing,
        "contours": diagnostics,
        "method_note": "arc-length samples; periodic smoothing B-spline converted exactly to cubic Bezier spans; contour area and centroid retained; no photograph opacity or pen dynamics"}
    return candidate
