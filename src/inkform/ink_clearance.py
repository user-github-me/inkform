"""Place intact letter bodies while checking the complete two-dimensional ink."""
from __future__ import annotations

import copy
from collections import OrderedDict
import math

import numpy as np
from scipy.spatial import cKDTree

from .measurements import _body_path_bounds
from .profiles import geometry_bounds
from .repetition import _canonical, raster_ink


GRID_MM = .025
# Covers polygon rasterization, round-cap/width rounding and curve flattening
# on both masks. This is subtracted from measured pixel-centre separation.
RASTER_UNCERTAINTY_MM = 6 * GRID_MM


def _paint_key(paint, origin):
    # Keep each width profile attached to its own path. A geometry multiset and
    # a separate width multiset could alias two differently painted glyphs.
    return tuple(sorted(_canonical([stroke], 1, paint["representation"],
                                   paint["pen_width_mm"], origin) for stroke in paint["strokes"]))


def ink_bounds(item):
    paints = item.get("_ink_paints", [item])
    bounds = []
    for paint in paints:
        box = geometry_bounds(paint["strokes"])
        pad = 0 if paint["representation"] == "clean_outline" else paint["pen_width_mm"] * max(
            (p["multiplier"] for stroke in paint["strokes"] for p in stroke.get("width_profile", [])), default=1) / 2
        bounds.append([box[0]-pad, box[1]-pad, box[2]+pad, box[3]+pad])
    return [min(b[0] for b in bounds), min(b[1] for b in bounds),
            max(b[2] for b in bounds), max(b[3] for b in bounds)]


def body_ink_bounds(item):
    """Actual path extents in the baseline/x-height band, with pen padding.

    Dots, ascenders and descenders stay in the collision mask. They simply do
    not determine the horizontal attachment of the letter's main body.
    """
    size = item["nominal_size_mm"]
    baseline = item.get("variation", {}).get("baseline_x_height", 0) * size
    strokes = copy.deepcopy(item["strokes"])
    for stroke in strokes:
        stroke["start"][1] = (stroke["start"][1]-baseline)/size
        for segment in stroke["segments"]:
            for key in ("end", "control1", "control2"):
                if key in segment:
                    segment[key][1] = (segment[key][1]-baseline)/size
    body = _body_path_bounds(strokes)
    if body is None:
        return ink_bounds(item)
    pad = 0 if item["representation"] == "clean_outline" else item["pen_width_mm"] * max(
        (p["multiplier"] for stroke in item["strokes"] for p in stroke.get("width_profile", [])), default=1) / 2
    return [body[0]-pad, body[1]*size+baseline-pad,
            body[2]+pad, body[3]*size+baseline+pad]


class InkClearance:
    """Bounded translation-invariant masks for conservative separation tests."""

    def __init__(self):
        self.cache = OrderedDict()
        self.body_cache = OrderedDict()

    def body_bounds(self, item):
        anchor = item["strokes"][0]["start"]
        baseline = item.get("variation", {}).get("baseline_x_height", 0) * item["nominal_size_mm"]
        key = (_paint_key(item, anchor),
               item["nominal_size_mm"], round(baseline-anchor[1], 8))
        if key not in self.body_cache:
            left, top, right, bottom = body_ink_bounds(item)
            self.body_cache[key] = [left-anchor[0], top-anchor[1], right-anchor[0], bottom-anchor[1]]
            if len(self.body_cache) > 256:
                self.body_cache.popitem(last=False)
        self.body_cache.move_to_end(key)
        left, top, right, bottom = self.body_cache[key]
        return [left+anchor[0], top+anchor[1], right+anchor[0], bottom+anchor[1]]

    def _mask(self, item):
        paints = item.get("_ink_paints", [item])
        bounds = geometry_bounds([stroke for paint in paints for stroke in paint["strokes"]])
        center = np.array([(bounds[0]+bounds[2])/2, (bounds[1]+bounds[3])/2])
        key = tuple(sorted(_paint_key(paint, center) for paint in paints))
        if key not in self.cache:
            raster = raster_ink(item, grid_mm=GRID_MM, normalize=False)
            yy, xx = np.nonzero(raster["mask"])
            points = np.column_stack((xx*GRID_MM+raster["origin_mm"][0],
                                      yy*GRID_MM+raster["origin_mm"][1]))
            self.cache[key] = points, cKDTree(points)
            if len(self.cache) > 256:
                self.cache.popitem(last=False)
        self.cache.move_to_end(key)
        return self.cache[key], center

    def separated(self, left, right, dx, minimum):
        """Certify clearance for every actual ink pair after translating right.

        Disjoint bounding rectangles can certify an easy pair. Overlapping
        rectangles always reach the complete masks, never a bounding-box veto.
        Testing every filled pixel also catches containment inside painted ink;
        testing only path boundaries would miss it. Outline holes remain empty.
        """
        for first in left:
            a = ink_bounds(first)
            for second in right:
                b = ink_bounds(second)
                horizontal = max(a[0]-(b[2]+dx), b[0]+dx-a[2], 0.)
                vertical = max(a[1]-b[3], b[1]-a[3], 0.)
                if math.hypot(horizontal, vertical) >= minimum-1e-10:
                    continue
                (_, tree), ac = self._mask(first)
                (points, _), bc = self._mask(second)
                # A finite upper bound stops each nearest-neighbour search as
                # soon as it can certify that this pixel is sufficiently far.
                threshold = minimum + RASTER_UNCERTAINTY_MM
                distance, _ = tree.query(points+bc+np.array([dx, 0.])-ac,
                                         distance_upper_bound=threshold)
                if np.any(np.isfinite(distance)):
                    return False
        return True
