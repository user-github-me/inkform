"""Private, provenance-preserving sample and prototype import.

OCR and traced paths are proposals. Only an explicit review record matching
both the evidence and the exact generated geometry can approve a new glyph.
"""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps

from .geometry import GEOMETRY_VERSION, geometry_hash, reconstruct_geometry


IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp", ".heic", ".heif"}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _private_root() -> Path:
    from .profiles import default_data_dir
    root = default_data_dir() / "imports"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
    path.chmod(0o600)


def _copy_evidence(path: Path, category: str) -> tuple[str, Path]:
    digest = sha256_file(path)
    dest = _private_root() / category / (digest + path.suffix.lower())
    dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not dest.exists():
        shutil.copyfile(path, dest)
        dest.chmod(0o600)
    elif sha256_file(dest) != digest:
        raise ValueError(f"Private evidence hash mismatch: {dest}")
    return digest, dest


def _profile(profile_id: str) -> dict:
    return {"schema_version": "1.0", "profile_id": profile_id, "profile_version": "0.1.0",
            "scripts": ["Latin"], "physical_scale": {"body_x_height_mm": 3.0,
                "status": "estimated", "evidence": "initial print calibration choice; not measured from photo pixels"},
            "pen": {"width_mm": .25, "color": "#20232a", "status": "estimated"},
            "metrics": {"word_gap": .72, "line_height": 2.8, "status": "estimated"},
            "glyphs": [], "sources": [], "observations": [],
            "coordinate_system": {"glyph_units": "x_height", "baseline_y": 0, "y_axis": "down", "page_units": "mm"},
            "dynamics": {"stroke_order": "unknown", "timing": "unknown", "pressure": "unknown"},
            "calibration_status": "pending_user_feedback", "synthetic": False,
            "limitations": ["physical scale and nominal pen width await an actual-size print proof",
                            "photographs do not determine stroke order, timing, or pressure",
                            "arbitrary connected cursive synthesis is not supported"]}


def _open_oriented(path: Path) -> tuple[Image.Image, dict]:
    adapter = "Pillow"
    if path.suffix.lower() in {".heic", ".heif"}:
        try:
            import pillow_heif
            pillow_heif.register_heif_opener()
            adapter = "pillow-heif"
        except ImportError:
            # sips is a local format adapter only; originals remain untouched.
            if not shutil.which("sips"):
                raise ValueError("HEIC import requires pillow-heif or macOS sips")
            digest = sha256_file(path)
            target = _private_root() / "normalized" / (digest + "-sips.png")
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            subprocess.run(["sips", "-s", "format", "png", str(path), "--out", str(target)],
                           check=True, capture_output=True)
            with Image.open(target) as image:
                original_size = list(image.size)
                result = ImageOps.exif_transpose(image).convert("RGB")
            target.chmod(0o600)
            return result, {"adapter": "macOS sips", "original_size": original_size,
                            "orientation_status": "adapter_normalized; verify visually", "exif_orientation": None}
    with Image.open(path) as image:
        original_size = list(image.size)
        orientation = image.getexif().get(274, 1)
        result = ImageOps.exif_transpose(image).convert("RGB")
    return result, {"adapter": adapter, "original_size": original_size,
                    "exif_orientation": orientation, "orientation_status": "EXIF applied"}


def import_samples(samples_dir: Path, existing_profile: dict | None = None,
                   profile_id: str = "personal-default") -> dict:
    """Preserve/hash raw inputs and create unlabeled page observations.

    Existing accepted glyphs survive an update. New photos/PDFs alone cannot
    manufacture accepted character coverage. No OCR labels are accepted here.
    """
    samples_dir = Path(samples_dir).expanduser().resolve()
    if not samples_dir.is_dir():
        raise ValueError(f"Sample directory does not exist: {samples_dir}")
    incoming = _profile(existing_profile["profile_id"] if existing_profile else profile_id)
    sources, observations, skipped = [], [], []
    source_files = [p for p in sorted(samples_dir.rglob("*")) if p.is_file() and not p.name.startswith(".")]
    for path in source_files:
        if path.suffix.lower() not in IMAGE_SUFFIXES | {".pdf"}:
            skipped.append({"name": path.name, "reason": "unsupported input type"})
            continue
        digest, preserved = _copy_evidence(path, "originals")
        source_id = "source-" + digest[:24]
        record = {"source_id": source_id, "sha256": digest, "source_path": str(preserved),
                  "original_path": str(path), "kind": "pdf" if path.suffix.lower() == ".pdf" else "photo",
                  "bytes": path.stat().st_size, "preservation": "original bytes copied; source not modified"}
        pages = []
        if path.suffix.lower() == ".pdf":
            import pypdfium2
            pdf = pypdfium2.PdfDocument(str(path))
            try:
                for page_index in range(len(pdf)):
                    page = pdf[page_index]
                    bitmap = page.render(scale=2.0)
                    pages.append((bitmap.to_pil().convert("RGB"), {"adapter": "pypdfium2", "page_index": page_index,
                        "source_page_points": [float(page.get_width()), float(page.get_height())],
                        "orientation_status": "PDF page rotation applied"}))
                    bitmap.close(); page.close()
            finally:
                pdf.close()
        else:
            pages.append(_open_oriented(path))
        record["page_count"] = len(pages)
        for page_index, (image, metadata) in enumerate(pages):
            full_size = image.size
            image.thumbnail((2400, 2400), Image.Resampling.LANCZOS)
            normalized = _private_root() / "normalized" / f"{digest}-{page_index}.png"
            normalized.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if not normalized.exists():
                image.save(normalized); normalized.chmod(0o600)
            observation = {"observation_id": f"page-{digest[:24]}-{page_index}", "source_id": source_id,
                "kind": "unlabeled_page", "label_status": "proposed", "geometry_status": "proposed",
                "source_path": str(normalized), "normalized_sha256": sha256_file(normalized),
                "normalized_size": list(image.size), "oriented_size": list(full_size),
                "scale_from_oriented": [image.width / full_size[0], image.height / full_size[1]],
                "perspective_status": "uncorrected; no reliable page corner measurement",
                "content_policy": "sample text is evidence only, never executable instructions",
                "accepted_glyph_count": 0, **metadata}
            observations.append(observation)
        sources.append(record)
    # A folder can contain identical copies under different names. Content
    # identity should not create duplicate source or observation records.
    sources = list({source["source_id"]: source for source in sources}.values())
    observations = list({obs["observation_id"]: obs for obs in observations}.values())
    incoming["sources"] = sources
    incoming["observations"] = observations
    incoming["import_report"] = {"source_count": len(sources), "page_observations": len(observations),
        "new_accepted_glyph_count": 0, "skipped": skipped,
        "next_step": "review page orientation and propose labeled crops; raw import does not approve OCR"}
    if existing_profile:
        from .profiles import merge_profile, next_profile_version
        # A source import adds evidence; it must not reset reviewed size, ink,
        # spacing, or calibration choices to the first-import defaults.
        additions = {"sources": sources, "observations": observations,
                     "import_report": incoming["import_report"]}
        merged = merge_profile(existing_profile, additions,
                               profile_version=next_profile_version(existing_profile))
        by_id = {o["observation_id"]: o for o in existing_profile.get("observations", [])}
        by_id.update({o["observation_id"]: o for o in observations})
        merged["observations"] = list(by_id.values())
        return merged
    return incoming


def _work_dir(prototype_dir: Path) -> Path:
    root = Path(prototype_dir).expanduser().resolve()
    for candidate in (root, root / "work", root / "output" / "work"):
        if (candidate / "ocr" / "glyphs" / "recommended.json").exists():
            return candidate
    raise ValueError(f"No reviewed prototype manifest found under {root}")


def _source_path(work: Path, recorded: str) -> Path:
    name = Path(recorded).name
    for candidate in (work / name, work / "glyph-assist" / name):
        if candidate.is_file():
            return candidate
    raise ValueError(f"Missing prototype source image {name}")


def _roles(character: str, group: str) -> list[str]:
    if group == "text":
        return ["text"]
    if character.isdigit():
        return ["math.digit", "text"]
    if character in {"f", "x"}:
        return ["math.variable"]
    if character in {".", ",", ":"}:
        return ["text", "math.operator"]
    if character.isupper():
        return ["text"]
    return ["math.operator"]


def import_prototype(prototype_dir: Path, profile_id: str = "personal-default",
                     representation: str = "centerline") -> dict:
    """Import curated masks as geometry proposals with a private review ledger.

    An exact geometry hash plus evidence hash must match in review.json for an
    accepted result. Changing reconstruction or evidence invalidates approval.
    """
    work = _work_dir(prototype_dir)
    profile = _profile(profile_id)
    texts = json.loads((work / "ocr/glyphs/recommended.json").read_text())
    math = json.loads((work / "glyph-assist/manifest.json").read_text())
    records = []
    for group, manifest, folder in (("text", texts, work / "ocr/glyphs"), ("math", math, work / "glyph-assist")):
        for character, entries in manifest["glyphs"].items():
            for row in entries:
                records.append((group, character, row, folder / row["file"]))
    input_key = hashlib.sha256((GEOMETRY_VERSION + representation + "".join(sha256_file(p) for *_, p in records)).encode()).hexdigest()
    build = _private_root() / ("prototype-" + input_key[:20])
    build.mkdir(parents=True, exist_ok=True, mode=0o700)
    review_path = build / "review.json"
    reviews = json.loads(review_path.read_text()).get("reviews", {}) if review_path.exists() else {}
    source_map = {}; observations = []; candidates = []; ignored = []
    project = work.parent.parent
    original_names = {p.stem: p for p in (project / "my writtings").glob("*") if p.is_file()}

    def add_source(path: Path, kind: str) -> str:
        key = str(path.resolve())
        if key in source_map:
            return source_map[key]["source_id"]
        digest, preserved = _copy_evidence(path, "originals" if kind == "photo" else "prototype-sources")
        record = {"source_id": "source-" + digest[:24], "sha256": digest,
                  "source_path": str(preserved), "original_path": str(path.resolve()), "kind": kind}
        if path.suffix.lower() not in {".heic", ".heif"}:
            with Image.open(path) as image:
                record["dimensions_px"] = list(image.size)
        source_map[key] = record
        stem = path.stem.replace("-full", "").replace("-upright", "")
        if kind != "photo" and stem in original_names:
            record["derived_from"] = [add_source(original_names[stem], "photo")]
            record["normalization_status"] = "legacy prototype transform; crop coordinates refer to this derived image"
        return record["source_id"]

    for group, character, row, mask_path in records:
        observation_id = f"prototype-{group}-{row['id']}"
        digest, preserved = _copy_evidence(mask_path, "crops")
        source_id = add_source(_source_path(work, row["source"]), "prototype_normalized_photo")
        label_reviewed = bool(texts.get("visually_reviewed")) if group == "text" else False
        observation = {"observation_id": observation_id, "source_id": source_id,
            "proposed_label": character, "source_path": str(preserved), "mask_sha256": digest,
            "bbox_xywh": row.get("source_bbox", row.get("bbox")),
            "baseline_px": row["baseline"], "xheight_px": row["xheight"],
            "label_status": "reviewed" if label_reviewed else "proposed",
            "label_evidence": "legacy visually_reviewed recommendation" if label_reviewed else "legacy manual crop selection; new review required",
            "word_context": row.get("word"), "preprocessing": "legacy local-background removal and component isolation"}
        observations.append(observation)
        # Whole formulas stay observations: composing them would bypass semantic layout.
        if len(character) != 1:
            ignored.append({"observation_id": observation_id, "text": character,
                            "reason": "whole formula retained as evidence; individual semantic layout required"})
            continue
        with Image.open(mask_path) as image:
            alpha = np.asarray(image.convert("L"))
        geometry = reconstruct_geometry(alpha, row["baseline"], row["xheight"], representation)
        decision = reviews.get(observation_id, {})
        review_valid = decision.get("mask_sha256") == digest and decision.get("geometry_sha256") == geometry["geometry_sha256"]
        accepted = review_valid and decision.get("geometry_status") == "reviewed" and bool(decision.get("reviewed_by"))
        rejected = review_valid and decision.get("geometry_status") == "rejected"
        label_accepted = label_reviewed or (review_valid and decision.get("label_status") == "reviewed")
        for role in _roles(character, group):
            glyph = {"glyph_id": f"{role}.{ord(character):04x}.{row['id']}", "text": character,
                "role": role, **copy.deepcopy(geometry),
                "source_refs": [{"source_id": source_id, "observation_id": observation_id}],
                "label_status": "reviewed" if label_accepted else "proposed",
                "geometry_status": "reviewed" if accepted else "rejected" if rejected else "proposed",
                "reviewed_by": decision.get("reviewed_by") if review_valid and decision.get("reviewed_by") else "legacy prototype visual review" if label_reviewed else None,
                "stroke_order_status": "unknown", "observation_id": observation_id,
                "compatibility": "isolated glyph; joins not modeled"}
            if role == "text" and group == "math" and character.isdigit():
                glyph["role_transfer"] = "same numeric identity; digit form reused in prose"
            if representation == "clean_outline":
                glyph["outline_calibration"] = {"body_x_height_mm": 3.0, "pen_width_mm": .25,
                    "effective_width_mm": geometry["reconstruction"]["estimated_ink_width_xheight"] * 3.0,
                    "compatible_scale_range": [1.0, 1.0], "status": "initial; no script optical correction reviewed"}
            profile["glyphs"].append(glyph)
        candidates.append({"observation_id": observation_id, "text": character, "group": group,
            "mask_sha256": digest, "geometry_sha256": geometry["geometry_sha256"],
            "source_path": str(preserved), "baseline_px": row["baseline"], "xheight_px": row["xheight"],
            "geometry": geometry, "geometry_status": "reviewed" if accepted else "rejected" if rejected else "proposed"})
    # lim uses separately written upright letters. Reuse accepted personal forms
    # explicitly; do not overwrite the text namespace or claim a new observation.
    for glyph in list(profile["glyphs"]):
        if glyph["role"] == "text" and glyph["text"] in "lim":
            compatible = copy.deepcopy(glyph)
            compatible["role"] = "math.function"
            compatible["glyph_id"] = glyph["glyph_id"].replace("text.", "math.function.", 1)
            compatible["role_transfer"] = "upright function-name letter reused from personal text; allowed context lim"
            compatible["allowed_functions"] = ["lim"]
            profile["glyphs"].append(compatible)
        elif glyph["role"] == "math.operator" and glyph["text"] == "-":
            minus = copy.deepcopy(glyph)
            minus["text"] = "−"
            minus["glyph_id"] = glyph["glyph_id"].replace("002d", "2212")
            minus["semantic_alias"] = "Unicode mathematical minus and ASCII subtraction share this reviewed observed sign"
            profile["glyphs"].append(minus)
    unique_sources = {source["source_id"]: source for source in source_map.values()}
    profile["sources"] = sorted(unique_sources.values(), key=lambda s: s["source_id"])
    profile["observations"] = observations
    accepted_glyphs = [g for g in profile["glyphs"] if g["label_status"] == g["geometry_status"] == "reviewed"]
    coverage = {}
    for glyph in accepted_glyphs:
        coverage.setdefault(glyph["role"], set()).add(glyph["text"])
    profile["import_report"] = {"method": GEOMETRY_VERSION, "representation": representation,
        "review_directory": str(build), "review_path": str(review_path), "source_count": len(profile["sources"]),
        "observation_count": len(observations), "glyph_count": len(profile["glyphs"]),
        "accepted_glyph_count": len(accepted_glyphs), "accepted_coverage": {k: sorted(v) for k, v in coverage.items()},
        "missing_lowercase": sorted(set("abcdefghijklmnopqrstuvwxyz") - coverage.get("text", set())),
        "missing_digits": sorted(set("0123456789") - coverage.get("math.digit", set())),
        "excluded_observations": ignored, "review_required": len(accepted_glyphs) < len(profile["glyphs"])}
    profile["calibration_status"] = "pending_user_feedback" if accepted_glyphs else "unreviewed"
    _write_json(build / "candidates.json", {"algorithm": GEOMETRY_VERSION, "candidates": candidates})
    if not review_path.exists():
        _write_json(review_path, {"instructions": "Visual review must compare evidence and newly rendered geometry. Copy both hashes and identify the reviewer. No auto-approval.", "reviews": {}})
    write_contact_sheets(candidates, build)
    return profile


def _draw_geometry(canvas: Image.Image, geometry: dict, x: float, baseline: float, scale: float) -> None:
    """Small review renderer. Production output uses the vector exporter."""
    draw = ImageDraw.Draw(canvas)
    def transform(p):
        return (x + p[0] * scale, baseline + p[1] * scale)
    contours = []
    for path in geometry["strokes"]:
        current = np.array(path["start"]); points = [transform(current)]
        for segment in path["segments"]:
            end = np.array(segment["end"])
            if segment["type"] == "line":
                points.append(transform(end))
            else:
                c1, c2 = np.array(segment["control1"]), np.array(segment["control2"])
                for t in np.linspace(0, 1, 9)[1:]:
                    p = (1-t)**3 * current + 3*(1-t)**2*t*c1 + 3*(1-t)*t*t*c2 + t**3*end
                    points.append(transform(p))
            current = end
        if path["closed"]:
            points.append(points[0])
        if geometry["representation"] == "clean_outline":
            contours.append(points)
        else:
            width = max(1, round(scale * .25 / 3.0))
            draw.line(points, fill="#20232a", width=width, joint="curve")
            for px, py in [points[0], points[-1]]:
                draw.ellipse((px-width/2, py-width/2, px+width/2, py+width/2), fill="#20232a")
    if contours:
        # XOR contour masks reproduces even-odd holes for isolated closed outlines.
        from PIL import ImageChops
        combined = Image.new("1", canvas.size)
        for points in contours:
            layer = Image.new("1", canvas.size)
            ImageDraw.Draw(layer).polygon(points, fill=1)
            combined = ImageChops.logical_xor(combined, layer)
        canvas.paste("#20232a", (0, 0), combined)


def write_contact_sheets(candidates: list[dict], destination: Path) -> list[Path]:
    """Source/geometry pairs with identical scale and baseline for visual review."""
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 17)
        small = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 12)
    except OSError:
        font = small = ImageFont.load_default()
    outputs = []
    for page_no, start in enumerate(range(0, len(candidates), 24), 1):
        section = candidates[start:start+24]
        canvas = Image.new("RGB", (1440, 1060), "white")
        draw = ImageDraw.Draw(canvas)
        draw.text((18, 10), "Inkform geometry review — observed ink | new geometry — nominal 0.25 mm / x-height 3 mm", fill="black", font=font)
        for i, candidate in enumerate(section):
            x, y = (i % 6) * 240, 50 + (i // 6) * 250
            draw.text((x+8, y+4), f"{candidate['text']}  {candidate['observation_id'].replace('prototype-', '')}", fill="black", font=font)
            baseline = y + 162
            scale = 54.0
            draw.line((x+5, baseline, x+235, baseline), fill="#d7dce4")
            with Image.open(candidate["source_path"]) as source:
                ratio = scale / candidate["xheight_px"]
                resized = source.resize((max(1, round(source.width * ratio)), max(1, round(source.height * ratio))), Image.Resampling.LANCZOS)
                canvas.paste("#20232a", (x+12, round(baseline-candidate["baseline_px"]*ratio)), resized)
            _draw_geometry(canvas, candidate["geometry"], x+128, baseline, scale)
            draw.text((x+8, y+218), candidate["geometry_status"], fill="#606570", font=small)
            draw.line((x, y+244, x+239, y+244), fill="#e2e5ea")
        path = destination / f"review-{page_no:02}.png"
        canvas.save(path); path.chmod(0o600); outputs.append(path)
    return outputs
