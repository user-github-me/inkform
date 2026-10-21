"""Portable sample learning uses synthetic writers, never private photographs."""

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw

from inkform.ingest import sha256_file
from inkform.observations import import_observations
from inkform.profiles import accepted_glyphs, validate_profile


class ObservationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {"INKFORM_DATA_DIR": str(self.root / "private")})
        self.env.start()

    def tearDown(self):
        self.env.stop(); self.tmp.cleanup()

    def writer(self, name, horizontal_span):
        folder = self.root / name; folder.mkdir()
        source = folder / "page.png"
        image = Image.new("RGB", (220, 180), "white")
        ImageDraw.Draw(image).ellipse((40, 35, 40+horizontal_span, 130), outline="black", width=9)
        image.save(source)
        manifest = folder / "observations.json"
        manifest.write_text(json.dumps({"schema_version": "1.0", "observations": [{
            "id": "o-first", "text": "o", "role": "text", "source_path": "page.png",
            "bbox_xywh": [20, 15, 170, 140], "baseline_px": 115, "xheight_px": 95,
            "label_status": "reviewed", "reviewed_by": "Synthetic author label review"}]}))
        return source, manifest

    def approve(self, profile, candidate_id=None, bad_hash=False):
        path = Path(profile["import_report"]["review_path"])
        candidates = json.loads((path.parent / "candidates.json").read_text())["candidates"]
        candidate = next(c for c in candidates if c["candidate_id"] == candidate_id) if candidate_id else candidates[0]
        path.write_text(json.dumps({"reviews": {candidate["candidate_id"]: {
            "mask_sha256": candidate["mask_sha256"],
            "geometry_sha256": "0"*64 if bad_hash else candidate["geometry_sha256"],
            "geometry_status": "reviewed", "label_status": "reviewed", "reviewed_by": "Synthetic geometry review"}}}))
        return candidate

    def test_two_writers_keep_distinct_observed_shapes_without_automatic_approval(self):
        source_a, manifest_a = self.writer("writer-a", 55)
        source_b, manifest_b = self.writer("writer-b", 125)
        fingerprints = {path: sha256_file(path) for path in (source_a, source_b, manifest_a, manifest_b)}
        a = import_observations(manifest_a, profile_id="writer-a")
        b = import_observations(manifest_b, profile_id="writer-b")
        validate_profile(a); validate_profile(b)
        self.assertFalse(accepted_glyphs(a)); self.assertFalse(accepted_glyphs(b))
        self.assertEqual({g["representation"] for g in a["glyphs"]}, {"centerline", "clean_outline"})
        outline_a = next(g for g in a["glyphs"] if g["representation"] == "clean_outline")
        outline_b = next(g for g in b["glyphs"] if g["representation"] == "clean_outline")
        width_a = outline_a["ink_bounds"][2]-outline_a["ink_bounds"][0]
        width_b = outline_b["ink_bounds"][2]-outline_b["ink_bounds"][0]
        self.assertGreater(width_b, width_a*1.9)
        self.assertNotEqual(outline_a["geometry_sha256"], outline_b["geometry_sha256"])
        self.assertNotEqual(a["observations"][0]["observation_id"], b["observations"][0]["observation_id"])
        self.assertEqual(fingerprints, {path: sha256_file(path) for path in fingerprints})
        self.assertTrue(Path(a["import_report"]["review_directory"], "review-01.png").is_file())

    def test_exact_hash_review_accepts_only_selected_representation(self):
        _, manifest = self.writer("writer", 80)
        proposed = import_observations(manifest)
        self.approve(proposed, bad_hash=True)
        self.assertFalse(accepted_glyphs(import_observations(manifest)))
        chosen = self.approve(proposed)
        reviewed = import_observations(manifest)
        self.assertEqual([g["glyph_id"] for g in accepted_glyphs(reviewed)], [chosen["candidate_id"]])
        self.assertEqual(accepted_glyphs(reviewed)[0]["representation"], "clean_outline")
        self.assertEqual(accepted_glyphs(reviewed)[0]["refinement"]["geometry_review_status"], "reviewed")
        self.assertEqual(next(g for g in reviewed["glyphs"] if g["representation"] == "centerline")["geometry_status"], "proposed")

    def test_changed_source_pixels_cannot_reuse_old_geometry_approval(self):
        source, manifest = self.writer("writer", 80)
        original = import_observations(manifest); self.approve(original)
        with Image.open(source) as image:
            updated = image.copy()
        ImageDraw.Draw(updated).line((95, 90, 140, 140), fill="black", width=8); updated.save(source)
        changed = import_observations(manifest)
        self.assertFalse(accepted_glyphs(changed))
        self.assertNotEqual(changed["import_report"]["review_directory"], original["import_report"]["review_directory"])

    def test_base_profile_preserves_other_writer_records_and_calibration(self):
        _, manifest_a = self.writer("writer-a", 60)
        _, manifest_b = self.writer("writer-b", 120)
        base = import_observations(manifest_a, profile_id="combined-test")
        base["physical_scale"]["body_x_height_mm"] = 3.6
        base["pen"]["width_mm"] = .30
        before = copy.deepcopy(base)
        merged = import_observations(manifest_b, base_profile=base)
        validate_profile(merged)
        self.assertEqual(base, before)
        self.assertEqual(merged["profile_version"], "0.1.1")
        self.assertEqual(len(merged["sources"]), 2)
        self.assertEqual(len(merged["glyphs"]), 4)
        new_ids = {g["glyph_id"] for g in merged["glyphs"]} - {g["glyph_id"] for g in base["glyphs"]}
        new_outline = next(g for g in merged["glyphs"] if g["glyph_id"] in new_ids and g["representation"] == "clean_outline")
        self.assertEqual(new_outline["outline_calibration"]["body_x_height_mm"], 3.6)
        self.assertEqual(new_outline["outline_calibration"]["pen_width_mm"], .30)

    def test_isolated_alpha_keeps_detached_dot_and_original_provenance(self):
        source, manifest = self.writer("writer", 80)
        mask = Image.new("L", (60, 110))
        draw = ImageDraw.Draw(mask); draw.line((27, 45, 27, 98), fill=255, width=7); draw.ellipse((24, 10, 31, 17), fill=255)
        mask.save(source.parent / "i-mask.png")
        data = {"schema_version": "1.0", "observations": [{"id": "i-mask", "text": "i", "role": "text",
            "source_path": "page.png", "mask_path": "i-mask.png", "baseline_px": 98, "xheight_px": 53}]}
        manifest.write_text(json.dumps(data))
        profile = import_observations(manifest)
        self.assertEqual(len(profile["sources"]), 1)
        self.assertEqual(profile["glyphs"][0]["reconstruction"]["component_count"], 2)
        self.assertEqual(profile["glyphs"][0]["label_status"], "proposed")
        self.assertEqual(profile["sources"][0]["sha256"], sha256_file(source))
        self.assertIsNone(profile["observations"][0]["bbox_xywh"])

    def test_invalid_label_review_and_outside_crop_rejected(self):
        _, manifest = self.writer("writer", 80)
        data = json.loads(manifest.read_text())
        del data["observations"][0]["reviewed_by"]
        manifest.write_text(json.dumps(data))
        with self.assertRaises(ValueError): import_observations(manifest)
        data["observations"][0]["label_status"] = "proposed"
        data["observations"][0]["bbox_xywh"] = [100, 100, 999, 999]
        manifest.write_text(json.dumps(data))
        with self.assertRaises(ValueError): import_observations(manifest)


if __name__ == "__main__":
    unittest.main()
