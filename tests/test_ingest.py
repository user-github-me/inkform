"""Synthetic ingestion tests: never depend on private handwriting fixtures."""

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage

from inkform.geometry import geometry_hash, reconstruct_geometry, skeleton_paths
from inkform.ingest import import_prototype, import_samples, sha256_file
from inkform.profiles import accepted_glyphs, validate_profile


class GeometryTests(unittest.TestCase):
    def test_detached_dot_is_a_separate_path_and_not_discarded(self):
        mask = np.zeros((100, 70), dtype=np.uint8)
        mask[12:19, 32:40] = 255
        mask[35:83, 31:38] = 255
        before = mask.copy()
        result = reconstruct_geometry(mask, baseline=84, xheight=50)
        self.assertEqual(result["reconstruction"]["component_count"], 2)
        self.assertGreaterEqual(len(result["strokes"]), 2)
        self.assertTrue(any(stroke["start"][1] < -1.2 for stroke in result["strokes"]))
        self.assertTrue(any(stroke["start"][1] > -1.1 for stroke in result["strokes"]))
        np.testing.assert_array_equal(mask, before)

    def test_normalization_y_down_and_scale_invariant(self):
        mask = np.zeros((80, 60), dtype=np.uint8)
        mask[20:65, 21:28] = 255
        a = reconstruct_geometry(mask, baseline=60, xheight=40)
        b = reconstruct_geometry(mask, baseline=60, xheight=80)
        for pa, pb in zip(a["strokes"], b["strokes"]):
            # Small differences from scale-relative smoothing are bounded.
            self.assertLess(abs(pa["start"][0] / 2 - pb["start"][0]), .03)
            self.assertLess(abs(pa["start"][1] / 2 - pb["start"][1]), .03)
        self.assertLess(a["ink_bounds"][1], 0)
        self.assertGreater(a["ink_bounds"][3], 0)

    def test_crossing_loop_and_isolated_pixel_graph_preserves_components(self):
        mask = np.zeros((60, 90), dtype=bool)
        mask[10:41, 20] = True; mask[25, 5:36] = True
        mask[10:35, 55] = True; mask[10:35, 75] = True
        mask[10, 55:76] = True; mask[34, 55:76] = True
        mask[50, 85] = True
        paths = skeleton_paths(mask)
        self.assertTrue(any(closed for _, closed in paths))
        self.assertTrue(any(np.allclose(points[:, 1], 50) for points, _ in paths))
        labels, _ = ndimage.label(mask, structure=np.ones((3, 3)))
        covered = set()
        for points, _ in paths:
            x, y = np.rint(points[0]).astype(int)
            covered.add(labels[y, x])
        self.assertEqual(covered, {1, 2, 3})

    def test_outlines_are_closed_and_holes_retained(self):
        image = Image.new("L", (100, 100)); draw = ImageDraw.Draw(image)
        draw.ellipse((15, 15, 80, 80), outline=255, width=8)
        result = reconstruct_geometry(np.array(image), 83, 60, "clean_outline")
        self.assertEqual(len(result["strokes"]), 2)
        self.assertTrue(all(p["closed"] for p in result["strokes"]))
        self.assertEqual(geometry_hash(result), result["geometry_sha256"])
        json.dumps(result, allow_nan=False)

    def test_invalid_empty_nonfinite_or_negative_scale_rejected(self):
        for mask, baseline, xheight in ((np.zeros((3, 3)), 2, 2), (np.ones((3, 3)), 2, -1),
                                       (np.array([[float("nan")]]), 1, 1), (np.ones((3, 3)), float("inf"), 2)):
            with self.assertRaises(ValueError):
                reconstruct_geometry(mask, baseline, xheight)


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {"INKFORM_DATA_DIR": str(self.root / "private")})
        self.env.start()

    def tearDown(self):
        self.env.stop(); self.tmp.cleanup()

    def image(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        image = Image.new("L", (60, 80)); draw = ImageDraw.Draw(image)
        draw.line((28, 35, 28, 70), fill=255, width=6)
        draw.ellipse((26, 12, 32, 18), fill=255)
        image.save(path)

    def fixture(self):
        work = self.root / "prototype" / "output" / "work"
        self.image(work / "synthetic-full.png")
        self.image(work / "ocr/glyphs/i.png")
        (work / "glyph-assist").mkdir(parents=True)
        record = {"id": "i01", "file": "i.png", "source": "old/location/synthetic-full.png",
                  "baseline": 70, "xheight": 40, "bbox": [0, 0, 60, 80], "word": "if"}
        (work / "ocr/glyphs/recommended.json").write_text(json.dumps({"visually_reviewed": True, "glyphs": {"i": [record]}}))
        (work / "glyph-assist/manifest.json").write_text(json.dumps({"glyphs": {}}))
        return work

    def test_source_files_immutable_and_new_geometry_needs_hash_bound_review(self):
        work = self.fixture()
        before = {p: sha256_file(p) for p in work.rglob("*") if p.is_file()}
        profile = import_prototype(work)
        validate_profile(profile)
        self.assertFalse(accepted_glyphs(profile))
        glyph = profile["glyphs"][0]
        self.assertEqual(glyph["label_status"], "reviewed")
        self.assertEqual(glyph["geometry_status"], "proposed")
        self.assertEqual(glyph["stroke_order_status"], "unknown")
        review_path = Path(profile["import_report"]["review_path"])
        candidate = json.loads((review_path.parent / "candidates.json").read_text())["candidates"][0]
        decision = {"geometry_status": "reviewed", "label_status": "reviewed", "reviewed_by": "Synthetic test review",
                    "mask_sha256": candidate["mask_sha256"], "geometry_sha256": "0" * 64}
        review_path.write_text(json.dumps({"reviews": {candidate["observation_id"]: decision}}))
        self.assertFalse(accepted_glyphs(import_prototype(work)))
        decision["geometry_sha256"] = candidate["geometry_sha256"]
        review_path.write_text(json.dumps({"reviews": {candidate["observation_id"]: decision}}))
        approved = import_prototype(work)
        validate_profile(approved)
        self.assertEqual(len(accepted_glyphs(approved)), 2)  # text i + explicit lim role reuse
        self.assertEqual(before, {p: sha256_file(p) for p in work.rglob("*") if p.is_file()})

    def test_raw_photo_import_preserves_bytes_corrects_exif_and_accepts_no_glyphs(self):
        samples = self.root / "samples"; samples.mkdir()
        source = samples / "rotated.jpg"
        image = Image.new("RGB", (40, 70), "white")
        exif = image.getexif(); exif[274] = 6
        image.save(source, exif=exif)
        digest = sha256_file(source)
        profile = import_samples(samples)
        validate_profile(profile)
        self.assertEqual(sha256_file(source), digest)
        self.assertEqual(profile["sources"][0]["sha256"], digest)
        self.assertEqual(profile["observations"][0]["oriented_size"], [70, 40])
        self.assertFalse(profile["glyphs"])
        self.assertEqual(profile["observations"][0]["label_status"], "proposed")
        self.assertTrue(Path(profile["sources"][0]["source_path"]).is_relative_to(self.root / "private"))

    def test_incremental_import_preserves_existing_calibration_and_geometry(self):
        original = import_prototype(self.fixture())
        original["pen"]["width_mm"] = .31
        original["physical_scale"]["body_x_height_mm"] = 3.4
        before = copy.deepcopy(original)
        samples = self.root / "new"; self.image(samples / "new.png")
        updated = import_samples(samples, existing_profile=original)
        validate_profile(updated)
        self.assertEqual(original, before)
        self.assertEqual(updated["glyphs"], original["glyphs"])
        self.assertEqual(updated["pen"], original["pen"])
        self.assertEqual(updated["physical_scale"], original["physical_scale"])
        self.assertEqual(updated["profile_version"], "0.1.1")
        self.assertGreater(len(updated["observations"]), len(original["observations"]))

    def test_duplicate_raw_sources_deduplicated_by_content(self):
        samples = self.root / "copies"
        self.image(samples / "a.png")
        (samples / "b.png").write_bytes((samples / "a.png").read_bytes())
        profile = import_samples(samples)
        validate_profile(profile)
        self.assertEqual(len(profile["sources"]), 1)
        self.assertEqual(len(profile["observations"]), 1)


if __name__ == "__main__":
    unittest.main()
