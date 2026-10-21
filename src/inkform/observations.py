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
from .profiles import ROLES, merge_profile, reviewed_actor, validate_profile
from .smoothing import SMOOTHING_VERSION, smooth_outline_glyph


OBSERVATION_IMPORT_VERSION = "inkform-observation-import-2"


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


def _source(manifest, row, sources, images):
    source = _resolve(manifest, row.get("source_path"))
    source_sha, preserved = _copy_evidence(source, "originals")
    source_id = "source-"+source_sha[:24]
    if source_sha not in images:
        images[source_sha] = _open_oriented(source)
    image, orientation = images[source_sha]
    sources[source_id] = {"source_id": source_id, "sha256": source_sha, "kind": "photo",
        "source_path": str(preserved), "original_path": str(source), "synthetic": False,
        "oriented_size": list(image.size), "orientation": orientation,
        "preservation": "original bytes preserved; crop coordinates use EXIF-oriented image"}
    return source_id, source_sha, image


def _integer(row, field, minimum=0):
    value = row.get(field)
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ValueError(f"Observation {field} must be an integer >= {minimum}")
    return value


def _retain_dimension_exclusion(glyph, observation, source_sha, previous):
    """Keep a reviewed source-context restriction, never a geometry approval.

    Regenerating the same cropped axis arrow does not turn its chosen shaft
    length into observed operator dimensions. A different mask, source, crop,
    label or role must establish its own restriction and review.
    """
    old_glyphs, old_observations, old_sources = previous
    old = old_glyphs.get(glyph["glyph_id"])
    if (not old or old.get("variation_dimensions_observed") is not False
            or old.get("source_refs") != glyph["source_refs"]
            or any(old.get(key) != glyph.get(key) for key in ("text", "role", "representation"))):
        return
    old_observation = old_observations.get(observation["observation_id"], {})
    old_source = old_sources.get(old_observation.get("source_id"), {})
    if (old_source.get("sha256") != source_sha
            or any(old_observation.get(key) != observation.get(key)
                   for key in ("source_id", "mask_sha256", "bbox_xywh", "manifest_observation_id"))):
        return
    # This conservative exclusion stays meaningful while replacement
    # geometry is awaiting review, including repeated regeneration passes.
    glyph["variation_dimensions_observed"] = False
    for key in ("role_transfer", "natural_length_status", "source_context"):
        if isinstance(old.get(key), str) and old[key].strip():
            glyph[key] = old[key]


def _style_observation(manifest, row, identity, sources, images, manifest_sha):
    """Preserve explicit source layout/count review; never infer it from crops."""
    kind = row.get("kind")
    if row.get("synthetic", False) is not False:
        raise ValueError("Synthetic observations cannot be imported as personal sample evidence")
    if kind not in {"word", "correction_frequency"}:
        raise ValueError("Style observations must have kind word or correction_frequency")
    source_id, source_sha, image = _source(manifest, row, sources, images)
    bbox = _bbox(row, image.size)
    page = _integer(row, "page_index")
    status_fields = ("label_status", "layout_status") if kind == "word" else ("review_status",)
    statuses = {field: row.get(field, "proposed") for field in status_fields}
    if any(status not in {"reviewed", "proposed", "rejected"} for status in statuses.values()):
        raise ValueError("Style review statuses must be reviewed, proposed, or rejected")
    reviewed = "reviewed" in statuses.values()
    actor = row.get("reviewed_by")
    if reviewed and not reviewed_actor(actor):
        raise ValueError("Reviewed style observations must identify reviewed_by")
    if (reviewed or "source_sha256" in row) and row.get("source_sha256") != source_sha:
        raise ValueError("Reviewed style observations require source_sha256 matching the exact original image bytes")
    observation = {"kind": kind, "source_id": source_id, "source_sha256": source_sha,
        "manifest_observation_id": identity, "manifest_sha256": manifest_sha, "page_index": page,
        "reviewed_by": actor if reviewed else None, "synthetic": False, **statuses}
    if bbox is not None:
        observation["bbox_xywh"] = bbox
    if kind == "word":
        if bbox is None:
            raise ValueError("Word layout observations require bbox_xywh")
        observation["xheight_px"] = _measurement(row, "xheight_px", positive=True)
        observation["word_index"] = _integer(row, "word_index")
        for field, default in (("line_id", None), ("column_id", "body")):
            value = row.get(field, default)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Word layout observations require a nonempty {field}")
            observation[field] = value
    else:
        scope = row.get("scope")
        if scope not in {"page", "region"}:
            raise ValueError("Correction frequency scope must be page or region")
        if scope == "region" and bbox is None:
            raise ValueError("Correction frequency regions require bbox_xywh")
        if scope == "page" and bbox is not None and bbox != [0, 0, image.width, image.height]:
            raise ValueError("Page frequency counts cover the complete page; use region for a cropped area")
        if not isinstance(row.get("representative"), bool):
            raise ValueError("Correction frequency observations must explicitly declare representative")
        words, corrections = _integer(row, "word_count", 1), _integer(row, "correction_count")
        if corrections > words:
            raise ValueError("Count corrected words once within the complete sampled word count")
        observation.update(scope=scope, representative=row["representative"], word_count=words, correction_count=corrections)
    # Source pixels and measurements identify the evidence; changing either
    # cannot silently overwrite an earlier count under the same stored ID.
    observation["observation_id"] = "style-"+_digest(observation)[:24]
    return observation


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
    if not isinstance(manifest.get("style_observations", []), list):
        raise ValueError("style_observations must be an array")
    if manifest.get("synthetic", False) is not False:
        raise ValueError("Synthetic manifests cannot be imported as personal training evidence")
    if base_profile and base_profile["synthetic"]:
        raise ValueError("Personal sample imports cannot be merged into a synthetic demonstration profile")
    profile = _profile(base_profile["profile_id"] if base_profile else profile_id)
    if base_profile:
        for field in ("physical_scale", "pen", "metrics"):
            profile[field] = copy.deepcopy(base_profile[field])
    profile["calibration_status"] = "unreviewed"
    sources = {}; observations = []; records = []; images = {}; identifiers = set()
    previous = tuple({row[key]: row for row in (base_profile or {}).get(collection, [])}
                     for collection, key in (("glyphs", "glyph_id"), ("observations", "observation_id"),
                                             ("sources", "source_id")))
    # Preserve the manifest itself as provenance, without treating its strings
    # as commands or labels recognized automatically from the photograph.
    manifest_sha, manifest_copy = _copy_evidence(manifest_path, "manifests")
    for row in manifest["observations"]:
        if not isinstance(row, dict):
            raise ValueError("Each observation must be an object")
        if row.get("synthetic", False) is not False:
            raise ValueError("Synthetic observations cannot be imported as personal sample evidence")
        identity = row.get("id")
        if not isinstance(identity, str) or not identity or identity in identifiers:
            raise ValueError("Observation IDs must be nonempty and unique within a manifest")
        identifiers.add(identity)
        text, role = row.get("text"), row.get("role")
        correction_mark = role == "mark.correction" and text == "crossout"
        if not isinstance(text, str) or (len(text) != 1 and not correction_mark) or role not in ROLES or (role == "mark.correction" and not correction_mark):
            raise ValueError("A glyph observation requires one Unicode character and an explicit supported role")
        label_status = row.get("label_status", "proposed")
        if label_status not in {"reviewed", "proposed", "rejected"}:
            raise ValueError("label_status must be reviewed, proposed, or rejected")
        actor = row.get("reviewed_by")
        if label_status == "reviewed" and not reviewed_actor(actor):
            raise ValueError("Reviewed labels must identify reviewed_by")
        baseline = _measurement(row, "baseline_px")
        xheight = _measurement(row, "xheight_px", positive=True)
        source_id, source_sha, image = _source(manifest_path, row, sources, images)
        bbox = _bbox(row, image.size)
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
        for representation in (("centerline",) if correction_mark else ("clean_outline", "centerline")):
            geometry = reconstruct_geometry(alpha, baseline, xheight, representation)
            label_key = "crossout" if correction_mark else f"{ord(text):04x}"
            glyph = {"glyph_id": f"{observation_id}.{role}.{label_key}.{representation}",
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
            _retain_dimension_exclusion(glyph, observation, source_sha, previous)
            records.append({"candidate_id": glyph["glyph_id"], "observation_id": observation_id,
                "manifest_observation_id": identity, "text": text, "role": role,
                "mask_sha256": mask_sha, "geometry_sha256": glyph["geometry_sha256"],
                "source_path": str(crop_path), "baseline_px": baseline, "xheight_px": xheight,
                "geometry": glyph, "geometry_status": "proposed"})
    for row in manifest.get("style_observations", []):
        if not isinstance(row, dict):
            raise ValueError("Each style observation must be an object")
        identity = row.get("id")
        if not isinstance(identity, str) or not identity or identity in identifiers:
            raise ValueError("Observation IDs must be nonempty and unique within a manifest")
        identifiers.add(identity)
        observations.append(_style_observation(manifest_path, row, identity, sources, images, manifest_sha))
    key = _digest({"importer": OBSERVATION_IMPORT_VERSION, "geometry": GEOMETRY_VERSION,
        "smoothing": SMOOTHING_VERSION, "candidates": [(r["candidate_id"], r["mask_sha256"], r["geometry_sha256"]) for r in records],
        "style_only": [observation["observation_id"] for observation in observations] if not records else []})
    build = _private_root() / ("observations-"+key[:20])
    build.mkdir(parents=True, exist_ok=True, mode=0o700)
    review_path = build / "review.json"
    reviews = json.loads(review_path.read_text()).get("reviews", {}) if review_path.exists() else {}
    for record in records:
        glyph = record["geometry"]
        decision = reviews.get(record["candidate_id"], {})
        valid = decision.get("mask_sha256") == record["mask_sha256"] and decision.get("geometry_sha256") == record["geometry_sha256"]
        reviewer = decision.get("reviewed_by")
        if valid and reviewed_actor(reviewer):
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
        "style_observation_count": sum(observation["kind"] != "glyph" for observation in observations),
        "review_required": reviewed < len(records), "review_directory": str(build), "review_path": str(review_path),
        "manifest_sha256": manifest_sha, "source_path": str(manifest_copy),
        "smoothing_failures": [r["candidate_id"] for r in records if "refinement_warning" in r["geometry"]]}
    for index, record in enumerate(records, 1):
        # Compact atlas IDs refer unambiguously to full evidence/geometry IDs in
        # candidates.json, even when a caller supplies a very long source ID.
        record["contact_sheet_id"] = f"C{index:04d}"
    _write_json(build / "candidates.json", {"algorithm": OBSERVATION_IMPORT_VERSION, "candidates": records})
    _write_json(build / "style-observations.json", {"observations": [observation for observation in observations if observation["kind"] != "glyph"]})
    if not review_path.exists():
        _write_json(review_path, {"instructions": "Approve only after comparing source and geometry; each decision requires both exact hashes and reviewed_by.", "reviews": {}})
    # Contact-sheet labels fit their cells; printed code points also keep signs
    # unambiguous when the host's small diagnostic font lacks a Unicode glyph.
    contacts = []
    for record in records:
        form = "outline" if record["geometry"]["representation"] == "clean_outline" else "stroke"
        text = record["text"] if record["text"].isascii() else " ".join(f"U+{ord(c):04X}" for c in record["text"])
        contacts.append(dict(record, text=text, observation_id=record["contact_sheet_id"]+" "+form,
                             geometry_status=record["geometry_status"]+" | candidates.json:"+record["contact_sheet_id"]))
    write_contact_sheets(contacts, build)
    validate_profile(profile)
    if base_profile:
        return merge_profile(base_profile, {"sources": profile["sources"], "observations": observations,
            "glyphs": profile["glyphs"], "import_report": profile["import_report"]})
    return profile
