"""Portable labeled-crop import with evidence-bound geometry review.

This adapter is deliberately independent of the private prototype's filenames.
An agent or person supplies labels and crop measurements; this module does not
recognize handwriting or treat a label review as an approval of new geometry.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path

import numpy as np
from PIL import Image
from scipy import ndimage

from .geometry import GEOMETRY_VERSION, reconstruct_geometry
from .ingest import _copy_evidence, _open_oriented, _private_root, _profile, _write_json, sha256_file, write_contact_sheets
from .profiles import ROLES, merge_profile, validate_profile
from .smoothing import SMOOTHING_VERSION, smooth_outline_glyph


OBSERVATION_IMPORT_VERSION = "inkform-observation-import-1"


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _resolve(manifest: Path, value: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError("Source and mask paths must be nonempty strings")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = manifest.parent / path
    path = path.resolve()
    if not path.is_file():
        raise ValueError(f"Observation evidence does not exist: {path}")
    return path


def _measurement(row: dict, name: str, positive=False) -> float:
    value = row.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"Observation {name} must be a finite number")
    if positive and value <= 0:
        raise ValueError(f"Observation {name} must be positive")
    return float(value)


def _bbox(row: dict, size: tuple[int, int]) -> list[int] | None:
    bbox = row.get("bbox_xywh")
    if bbox is None:
        return None
    if not isinstance(bbox, list) or len(bbox) != 4 or any(isinstance(v, bool) or not isinstance(v, int) for v in bbox):
        raise ValueError("bbox_xywh must contain four integer EXIF-oriented pixel coordinates")
    x, y, width, height = bbox
    if x < 0 or y < 0 or width <= 0 or height <= 0 or x+width > size[0] or y+height > size[1]:
        raise ValueError("Observation crop must lie inside the EXIF-oriented source image")
    return bbox


def _crop_alpha(image: Image.Image, bbox: list[int], xheight: float) -> np.ndarray:
    """Separate dark ink from smooth paper illumination; retain every component."""
    x, y, width, height = bbox
    padding = math.ceil(xheight * 2)
    left, top = max(0, x-padding), max(0, y-padding)
    context = np.asarray(image.crop((left, top, min(image.width, x+width+padding),
                                    min(image.height, y+height+padding))).convert("L"), dtype=float)
    background = ndimage.gaussian_filter(context, sigma=max(2.0, xheight*.6))
    contrast = background-context
    crop = contrast[y-top:y-top+height, x-left:x-left+width]
    positive = crop[crop > 6]
    if not positive.size:
        raise ValueError("No dark-ink contrast in observation crop; provide a reviewed isolated mask")
    upper = max(35.0, float(np.percentile(positive, 95)))
    return np.clip((crop-6) / (upper-6) * 255, 0, 255).astype(np.uint8)


def import_observations(manifest_path: Path, base_profile: dict | None = None,
                        profile_id: str = "personal-default") -> dict:
    """Import explicit labels/crops and propose both supported representations.

    Manifest contract and exact review procedure:
    skills/inkform/references/observations.md. Evidence stays private; original
    files are never edited. A supplied base profile is merged into a new version.
    """
    manifest_path = Path(manifest_path).expanduser().resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != "1.0" or not isinstance(manifest.get("observations"), list):
        raise ValueError("Observation manifest requires schema_version '1.0' and an observations array")
    profile = _profile(base_profile["profile_id"] if base_profile else profile_id)
    if base_profile:
        for field in ("physical_scale", "pen", "metrics"):
            profile[field] = copy.deepcopy(base_profile[field])
    profile["calibration_status"] = "unreviewed"
    sources = {}; observations = []; records = []; images = {}; identifiers = set()
    # Preserve the manifest itself as provenance, without treating its strings
    # as commands or labels recognized automatically from the photograph.
    manifest_sha, manifest_copy = _copy_evidence(manifest_path, "manifests")
    for row in manifest["observations"]:
        if not isinstance(row, dict):
            raise ValueError("Each observation must be an object")
        identity = row.get("id")
        if not isinstance(identity, str) or not identity or identity in identifiers:
            raise ValueError("Observation IDs must be nonempty and unique within a manifest")
        identifiers.add(identity)
        text, role = row.get("text"), row.get("role")
        if not isinstance(text, str) or len(text) != 1 or role not in ROLES:
            raise ValueError("A glyph observation requires one Unicode character and an explicit supported role")
        label_status = row.get("label_status", "proposed")
        if label_status not in {"reviewed", "proposed", "rejected"}:
            raise ValueError("label_status must be reviewed, proposed, or rejected")
        actor = row.get("reviewed_by")
        if label_status == "reviewed" and (not isinstance(actor, str) or not actor.strip()):
            raise ValueError("Reviewed labels must identify reviewed_by")
        baseline = _measurement(row, "baseline_px")
        xheight = _measurement(row, "xheight_px", positive=True)
        source = _resolve(manifest_path, row.get("source_path"))
        source_sha, preserved_source = _copy_evidence(source, "originals")
        source_id = "source-"+source_sha[:24]
        if source_sha not in images:
            images[source_sha] = _open_oriented(source)
        image, orientation = images[source_sha]
        bbox = _bbox(row, image.size)
        sources[source_id] = {"source_id": source_id, "sha256": source_sha, "kind": "photo",
            "source_path": str(preserved_source), "original_path": str(source),
            "oriented_size": list(image.size), "orientation": orientation,
            "preservation": "original bytes preserved; crop coordinates use EXIF-oriented image"}
        if row.get("mask_path"):
            mask_source = _resolve(manifest_path, row["mask_path"])
            mask_input_sha, _ = _copy_evidence(mask_source, "provided-masks")
            with Image.open(mask_source) as mask:
                if mask.mode not in {"L", "1"}:
                    raise ValueError("Isolated masks must be L-mode/1-bit white ink on black background")
                alpha = np.asarray(mask.convert("L"))
            if bbox and list(alpha.shape[::-1]) != bbox[2:]:
                raise ValueError("Isolated mask dimensions must match the provided source crop")
            method = "supplied isolated white-ink alpha mask; all components preserved"
        else:
            if bbox is None:
                raise ValueError("Each observation requires bbox_xywh or an isolated mask_path")
            alpha = _crop_alpha(image, bbox, xheight)
            mask_input_sha = None
            method = "local smooth-background subtraction; no OCR or component removal"
        if alpha.size == 0 or min(alpha.shape) < 1:
            raise ValueError("Observation masks must be nonempty")
        evidence_identity = _digest({"source_sha256": source_sha, "input_mask_sha256": mask_input_sha,
                                    "crop": bbox, "id": identity})
        crop_path = _private_root() / "observation-crops" / (evidence_identity+".png")
        crop_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not crop_path.exists():
            Image.fromarray(alpha).save(crop_path); crop_path.chmod(0o600)
        # A pipeline/decoder change must not reuse stale alpha silently.
        expected = _digest(alpha.tolist())
        with Image.open(crop_path) as stored:
            if _digest(np.asarray(stored).tolist()) != expected:
                crop_path = crop_path.with_name(evidence_identity+"-"+expected[:16]+".png")
                Image.fromarray(alpha).save(crop_path); crop_path.chmod(0o600)
        mask_sha = sha256_file(crop_path)
        observation_id = "obs-"+_digest({"source": source_sha, "mask": mask_sha, "id": identity})[:24]
        observation = {"observation_id": observation_id, "source_id": source_id, "kind": "glyph",
            "manifest_observation_id": identity, "proposed_label": text, "role": role,
            "label_status": label_status, "reviewed_by": actor if label_status == "reviewed" else None,
            "source_path": str(crop_path), "mask_sha256": mask_sha, "bbox_xywh": bbox,
            "baseline_px": baseline, "xheight_px": xheight, "source_oriented_size": list(image.size),
            "preprocessing": method, "manifest_sha256": manifest_sha,
            "measurement_status": "caller-supplied local baseline and x-height; not physical millimeters"}
        observations.append(observation)
        for representation in ("clean_outline", "centerline"):
            geometry = reconstruct_geometry(alpha, baseline, xheight, representation)
            glyph = {"glyph_id": f"{observation_id}.{role}.{ord(text):04x}.{representation}",
                "text": text, "role": role, **geometry,
                "source_refs": [{"source_id": source_id, "observation_id": observation_id}],
                "label_status": label_status, "geometry_status": "proposed",
                "reviewed_by": actor if label_status == "reviewed" else None,
                "stroke_order_status": "unknown", "observation_id": observation_id}
            if representation == "clean_outline":
                glyph["outline_calibration"] = {"body_x_height_mm": profile["physical_scale"]["body_x_height_mm"],
                    "pen_width_mm": profile["pen"]["width_mm"],
                    "effective_width_mm": geometry["reconstruction"]["estimated_ink_width_xheight"]*profile["physical_scale"]["body_x_height_mm"],
                    "status": "initial digital calibration; actual-size print proof required"}
                try:
                    glyph = smooth_outline_glyph(glyph)
                except ValueError as error:
                    glyph["refinement_warning"] = str(error)
            records.append({"candidate_id": glyph["glyph_id"], "observation_id": observation_id,
                "manifest_observation_id": identity, "text": text, "role": role,
                "mask_sha256": mask_sha, "geometry_sha256": glyph["geometry_sha256"],
                "source_path": str(crop_path), "baseline_px": baseline, "xheight_px": xheight,
                "geometry": glyph, "geometry_status": "proposed"})
    key = _digest({"importer": OBSERVATION_IMPORT_VERSION, "geometry": GEOMETRY_VERSION,
        "smoothing": SMOOTHING_VERSION, "candidates": [(r["candidate_id"], r["mask_sha256"], r["geometry_sha256"]) for r in records]})
    build = _private_root() / ("observations-"+key[:20])
    build.mkdir(parents=True, exist_ok=True, mode=0o700)
    review_path = build / "review.json"
    reviews = json.loads(review_path.read_text()).get("reviews", {}) if review_path.exists() else {}
    for record in records:
        glyph = record["geometry"]
        decision = reviews.get(record["candidate_id"], {})
        valid = decision.get("mask_sha256") == record["mask_sha256"] and decision.get("geometry_sha256") == record["geometry_sha256"]
        reviewer = decision.get("reviewed_by")
        if valid and isinstance(reviewer, str) and reviewer.strip():
            if decision.get("geometry_status") in {"reviewed", "rejected"}:
                glyph["geometry_status"] = decision["geometry_status"]
            if decision.get("label_status") == "reviewed" and glyph["label_status"] != "rejected":
                glyph["label_status"] = "reviewed"
            glyph["reviewed_by"] = reviewer
            if "refinement" in glyph:
                glyph["refinement"]["geometry_review_status"] = glyph["geometry_status"]
        record["geometry_status"] = glyph["geometry_status"]
        profile["glyphs"].append(glyph)
    profile["sources"] = list(sources.values()); profile["observations"] = observations
    reviewed = sum(g["label_status"] == g["geometry_status"] == "reviewed" for g in profile["glyphs"])
    profile["calibration_status"] = "pending_user_feedback" if reviewed else "unreviewed"
    profile["import_report"] = {"method": OBSERVATION_IMPORT_VERSION, "source_count": len(sources),
        "observation_count": len(observations), "glyph_count": len(records), "accepted_glyph_count": reviewed,
        "review_required": reviewed < len(records), "review_directory": str(build), "review_path": str(review_path),
        "manifest_sha256": manifest_sha, "source_path": str(manifest_copy),
        "smoothing_failures": [r["candidate_id"] for r in records if "refinement_warning" in r["geometry"]]}
    _write_json(build / "candidates.json", {"algorithm": OBSERVATION_IMPORT_VERSION, "candidates": records})
    if not review_path.exists():
        _write_json(review_path, {"instructions": "Approve only after comparing source and geometry; each decision requires both exact hashes and reviewed_by.", "reviews": {}})
    # Give each representation a distinct contact-sheet label while retaining
    # the real observation ID in the profile and source references.
    contacts = [dict(record, observation_id=record["manifest_observation_id"]+"."+record["geometry"]["representation"]) for record in records]
    write_contact_sheets(contacts, build)
    validate_profile(profile)
    if base_profile:
        return merge_profile(base_profile, {"sources": profile["sources"], "observations": observations,
            "glyphs": profile["glyphs"], "import_report": profile["import_report"]})
    return profile
