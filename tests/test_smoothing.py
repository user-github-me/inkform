"""Public synthetic fixtures for smooth, bounded outline refinement."""

import copy
import json
import math
import unittest

import numpy as np

from inkform.geometry import geometry_hash
from inkform.smoothing import flatten_outline, outline_topology, smooth_outline_glyph


def polygon(points):
    values = [list(map(float, point)) for point in points]
    return {"start": values[0], "segments": [{"type": "line", "end": p} for p in values[1:] + values[:1]], "closed": True}


def ellipse(center, radii, count=80, wobble=.003, reverse=False):
    ts = np.arange(count)*2*np.pi/count
    ripple = 1+wobble*np.sin(ts*19)
    points = np.column_stack([center[0]+radii[0]*np.cos(ts)*ripple, center[1]+radii[1]*np.sin(ts)*ripple])
    return polygon(points[::-1] if reverse else points)


def fixture():
    strokes = [ellipse((.65, -.5), (.55, .50), wobble=.012),
               ellipse((.65, -.5), (.34, .30), wobble=.008, reverse=True),
               ellipse((1.55, -.88), (.06, .06), count=32, wobble=.02)]
    result = {"glyph_id": "synthetic.outline", "text": "o", "role": "text", "representation": "clean_outline",
              "advance": 1.8, "ink_bounds": [.05, -1.05, 1.65, .05], "strokes": strokes,
              "label_status": "reviewed", "geometry_status": "reviewed", "reviewed_by": "synthetic fixture review",
              "source_refs": [{"source_id": "synthetic-source"}], "stroke_order_status": "unknown",
              "entry_anchor": [.1, 0], "exit_anchor": [1.2, 0]}
    result["geometry_sha256"] = geometry_hash(result)
    return result


class SmoothingTests(unittest.TestCase):
    def test_smooth_periodic_curves_remove_facets_and_preserve_topology(self):
        original = fixture()
        candidate = smooth_outline_glyph(original)
        old_topology = outline_topology(original["strokes"])
        topology = outline_topology(candidate["strokes"])
        self.assertEqual(topology["components"], 2)
        self.assertEqual(topology["holes"], 1)
        self.assertEqual(topology["parents"], old_topology["parents"])
        self.assertFalse(topology["intersections"])
        self.assertTrue(all(segment["type"] == "cubic" for path in candidate["strokes"] for segment in path["segments"]))
        self.assertLess(sum(len(p["segments"]) for p in candidate["strokes"]), sum(len(p["segments"]) for p in original["strokes"]))
        for path in candidate["strokes"]:
            self.assertTrue(path["closed"])
            self.assertEqual(path["start"], path["segments"][-1]["end"])
            # Adjacent cubic tangents align, including the periodic seam.
            for previous, following in zip(path["segments"], path["segments"][1:]+path["segments"][:1]):
                tangent1 = np.array(previous["end"])-previous["control2"]
                tangent2 = np.array(following["control1"])-previous["end"]
                norm = np.linalg.norm(tangent1)*np.linalg.norm(tangent2)
                self.assertGreater(norm, 1e-12)
                cosine = np.dot(tangent1, tangent2)/norm
                self.assertGreater(cosine, .99999)

    def test_candidate_is_deterministic_immutable_and_needs_geometry_review(self):
        original = fixture(); before = copy.deepcopy(original)
        a = smooth_outline_glyph(original)
        b = smooth_outline_glyph(original)
        self.assertEqual(original, before)
        self.assertEqual(a, b)
        self.assertEqual(a["geometry_status"], "proposed")
        self.assertEqual(a["label_status"], "reviewed")
        self.assertEqual(a["reviewed_by"], original["reviewed_by"])
        self.assertEqual(a["refinement"]["geometry_review_status"], "proposed")
        self.assertEqual(a["refinement"]["previous_geometry_sha256"], geometry_hash(original))
        self.assertNotEqual(a["geometry_sha256"], geometry_hash(original))
        self.assertEqual(a["geometry_sha256"], geometry_hash(a))
        for field in ("advance", "entry_anchor", "exit_anchor", "source_refs", "stroke_order_status"):
            self.assertEqual(a[field], original[field])
        json.dumps(a, allow_nan=False)

    def test_bounds_and_ink_weight_constrained(self):
        candidate = smooth_outline_glyph(fixture(), tolerance_xheight=.025)
        self.assertAlmostEqual(candidate["refinement"]["area_ratio"], 1, delta=.001)
        for record in candidate["refinement"]["contours"]:
            self.assertLessEqual(record["deviation_upper_bound_xheight"], record["tolerance_xheight"])
        bounds = candidate["ink_bounds"]
        for stroke in candidate["strokes"]:
            points = flatten_outline(stroke, .00001)
            self.assertGreaterEqual(points[:, 0].min(), bounds[0]-1e-7)
            self.assertGreaterEqual(points[:, 1].min(), bounds[1]-1e-7)
            self.assertLessEqual(points[:, 0].max(), bounds[2]+1e-7)
            self.assertLessEqual(points[:, 1].max(), bounds[3]+1e-7)

    def test_tiny_hole_and_detached_dot_are_not_collapsed(self):
        glyph = fixture()
        glyph["strokes"] = [ellipse((.5, -.5), (.12, .15), count=40),
                            ellipse((.5, -.5), (.012, .014), count=32, reverse=True),
                            ellipse((1, -.7), (.015, .015), count=24)]
        candidate = smooth_outline_glyph(glyph)
        topology = outline_topology(candidate["strokes"], .00002)
        self.assertEqual((topology["components"], topology["holes"]), (2, 1))
        self.assertLess(candidate["refinement"]["contours"][1]["tolerance_xheight"], .005)

    def test_arrow_tip_and_tail_stay_within_deviation_envelope(self):
        glyph = fixture()
        glyph["strokes"] = [polygon([(0, -.06), (1.5, -.06), (1.25, -.4), (1.42, -.47),
                                     (1.98, 0), (1.42, .47), (1.25, .4), (1.5, .06), (0, .06)])]
        candidate = smooth_outline_glyph(glyph)
        points = flatten_outline(candidate["strokes"][0])
        self.assertAlmostEqual(points[:, 0].max(), 1.98, delta=.025)
        self.assertAlmostEqual(points[:, 0].min(), 0, delta=.025)
        self.assertFalse(outline_topology(candidate["strokes"])["intersections"])

    def test_invalid_or_self_intersecting_input_is_rejected(self):
        glyph = fixture()
        for tolerance in (0, -.1, math.nan, math.inf, .2):
            with self.assertRaises(ValueError):
                smooth_outline_glyph(glyph, tolerance)
        centerline = copy.deepcopy(glyph); centerline["representation"] = "centerline"
        with self.assertRaises(ValueError): smooth_outline_glyph(centerline)
        open_path = copy.deepcopy(glyph); open_path["strokes"][0]["closed"] = False
        with self.assertRaises(ValueError): smooth_outline_glyph(open_path)
        crossed = copy.deepcopy(glyph)
        crossed["strokes"] = [polygon([(0, 0), (1, 1), (1, 0), (0, 1)])]
        with self.assertRaises(ValueError): smooth_outline_glyph(crossed)
        bad = copy.deepcopy(glyph); bad["strokes"][0]["start"][0] = math.nan
        with self.assertRaises(ValueError): smooth_outline_glyph(bad)


if __name__ == "__main__":
    unittest.main()
