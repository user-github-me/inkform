"""Find additional *observed* forms, retaining a source-bound review queue.

This is deliberately a proposal generator, not handwriting recognition. It
compares complete ink components with the writer's reviewed crop evidence.
It never cuts a connected word into invented letters, accepts a label, or
silently transfers a graph arrow into a mathematical operator. A person or
agent must inspect the surrounding source and review the resulting geometry.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from scipy import ndimage

from .ingest import IMAGE_SUFFIXES, _open_oriented, _write_json, sha256_file
from .observations import _crop_alpha
from .profiles import ROLES, accepted_glyphs, validate_profile


MINING_VERSION = "inkform-observed-form-mining-1"
_SIDE = 24


def _signature(alpha: np.ndarray) -> tuple[np.ndarray, float, tuple[int, int, int, int]]:
    positions = np.argwhere(alpha > 32)
    if positions.size == 0:
        raise ValueError("Template evidence has no ink")
    y0, x0 = positions.min(axis=0); y1, x1 = positions.max(axis=0) + 1
    ink = Image.fromarray(np.asarray(alpha[y0:y1, x0:x1], dtype=np.uint8))
    values = np.asarray(ink.resize((_SIDE, _SIDE), Image.Resampling.BILINEAR), dtype=np.float32) / 255
    # A slight blur tolerates photographic threshold noise without erasing
    # the topology, loops, or relative stroke positions within the glyph.
    values = ndimage.gaussian_filter(values, .65).ravel()
    values /= max(float(np.linalg.norm(values)), 1e-8)
    return values, float((x1-x0)/(y1-y0)), (int(x0), int(y0), int(x1), int(y1))


def _templates(profile: dict, role: str | None) -> tuple[list[dict], list[dict]]:
    observations = {o["observation_id"]: o for o in profile.get("observations", [])}
    sources = {s["source_id"]: s for s in profile["sources"]}
    bank, missing, seen = [], [], set()
    for glyph in accepted_glyphs(profile):
        if role is not None and glyph["role"] != role:
            continue
        if len(glyph["text"]) != 1:
            continue
        for ref in glyph.get("source_refs", []):
            obs = observations.get(ref.get("observation_id"), {})
            digest = obs.get("mask_sha256")
            key = (glyph["text"], glyph["role"], digest)
            if key in seen:
                continue
            seen.add(key)
            path = Path(obs.get("source_path", ""))
            if not digest or not path.is_file():
                missing.append({"glyph_id": glyph["glyph_id"], "reason": "reviewed isolated mask unavailable"})
                continue
            if sha256_file(path) != digest:
                raise ValueError(f"Reviewed template evidence hash changed: {obs['observation_id']}")
            with Image.open(path) as image:
                alpha = np.asarray(image.convert("L"))
            signature, aspect, bounds = _signature(alpha)
            x0, y0, x1, y1 = bounds
            baseline, xheight = obs.get("baseline_px"), obs.get("xheight_px")
            if not isinstance(xheight, (float, int)) or xheight <= 0 or not isinstance(baseline, (float, int)):
                missing.append({"glyph_id": glyph["glyph_id"], "reason": "local baseline or x-height unavailable"})
                continue
            source = sources.get(ref["source_id"], {})
            bank.append({"text": glyph["text"], "role": glyph["role"], "glyph_id": glyph["glyph_id"],
                         "observation_id": obs["observation_id"], "signature": signature, "aspect": aspect,
                         "height_per_xheight": (y1-y0)/xheight, "baseline_fraction": (baseline-y0)/(y1-y0),
                         "mask_sha256": digest, "source_sha256": source.get("sha256"),
                         "bbox_xywh": obs.get("bbox_xywh")})
    return bank, missing


def _page_paths(profile: dict, samples_dir: Path | None) -> list[Path]:
    if samples_dir is None:
        photos = [Path(s["source_path"]).expanduser().resolve() for s in profile["sources"]
                  if s.get("kind") == "photo" and s.get("source_path")]
        pdf_ids = {s["source_id"] for s in profile["sources"] if s.get("kind") == "pdf"}
        pages = [Path(o["source_path"]).expanduser().resolve() for o in profile.get("observations", [])
                 if o.get("kind") == "unlabeled_page" and o.get("source_id") in pdf_ids and o.get("source_path")]
        return photos + pages
    folder = Path(samples_dir).expanduser().resolve()
    if not folder.is_dir():
        raise ValueError(f"Sample directory does not exist: {folder}")
    return [path for path in sorted(folder.rglob("*")) if path.is_file()
            and not path.name.startswith(".") and path.suffix.lower() in IMAGE_SUFFIXES]


def _overlap(a, b) -> float:
    ax, ay, aw, ah = a; bx, by, bw, bh = b
    area = max(0, min(ax+aw, bx+bw)-max(ax, bx)) * max(0, min(ay+ah, by+bh)-max(ay, by))
    return area / max(1, min(aw*ah, bw*bh))


def _aligned_derived_boxes(image: Image.Image, source_sha: str, sources: dict,
                           existing: dict) -> list[list[int]]:
    """Deduplicate a legacy normalized crop only after checking alignment.

    Merely sharing an ancestor or dimensions is insufficient: that photograph
    may have been rotated or cropped. Compare its decoded page pixels first.
    """
    parents = {key for key, source in sources.items() if source.get("sha256") == source_sha}
    boxes = []
    original = np.asarray(image.convert("L").resize((192, 256)), dtype=float)
    for source in sources.values():
        if not parents.intersection(source.get("derived_from", [])) or not existing.get(source.get("sha256")):
            continue
        path = Path(source.get("source_path", ""))
        if not path.is_file():
            continue
        decoded, _ = _open_oriented(path)
        if decoded.size != image.size:
            continue
        candidate = np.asarray(decoded.convert("L").resize((192, 256)), dtype=float)
        spread = float(np.std(original)*np.std(candidate))
        correlation = float(np.mean((original-original.mean())*(candidate-candidate.mean()))/spread) if spread else 0
        if correlation > .995 and np.mean(np.abs(original-candidate)) < 12:
            boxes.extend(existing[source["sha256"]])
    return boxes


def _whole_components(image: Image.Image):
    """Return complete components; no sliding windows through connected ink."""
    small = image.copy(); small.thumbnail((2600, 2600), Image.Resampling.LANCZOS)
    gray = np.asarray(small.convert("L"), dtype=np.float32)
    background = ndimage.gaussian_filter(gray, sigma=max(5, small.height/160))
    contrast = background - gray
    binary = contrast > 18
    labels, _ = ndimage.label(binary, structure=np.ones((3, 3)))
    objects = ndimage.find_objects(labels)
    sx, sy = image.width/small.width, image.height/small.height
    for component, region in enumerate(objects, 1):
        if region is None:
            continue
        ys, xs = region
        width, height = xs.stop-xs.start, ys.stop-ys.start
        if min(width, height) < 4 or max(width, height) > 650 or width/height > 12 or height/width > 9:
            continue
        membership = labels[region] == component
        if membership.sum() < 18:
            continue
        alpha = np.clip((contrast[region]-5)*255/65, 0, 255).astype(np.uint8) * membership
        signature, aspect, _ = _signature(alpha)
        x0, y0 = max(0, math.floor(xs.start*sx)-3), max(0, math.floor(ys.start*sy)-3)
        x1, y1 = min(image.width, math.ceil(xs.stop*sx)+3), min(image.height, math.ceil(ys.stop*sy)+3)
        yield {"signature": signature, "aspect": aspect, "bbox_xywh": [x0, y0, x1-x0, y1-y0],
               "small_bbox": [xs.start, ys.start, width, height], "membership": membership,
               "ink_height_px": height*sy, "scale": (sx, sy), "component_id": component}


def _isolate(image: Image.Image, component: dict, xheight: float) -> np.ndarray:
    bbox = component["bbox_xywh"]
    alpha = _crop_alpha(image, bbox, xheight)
    x, y, width, height = bbox
    bx, by, bw, bh = component["small_bbox"]
    sx, sy = component["scale"]
    # Preserve full-resolution photographic ink only near this complete
    # component, excluding adjacent letters and paper texture in the box.
    mask = Image.fromarray(component["membership"].astype(np.uint8)*255)
    left, top = round(bx*sx)-x, round(by*sy)-y
    support = Image.new("L", (width, height))
    support.paste(mask.resize((max(1, round(bw*sx)), max(1, round(bh*sy))), Image.Resampling.NEAREST), (left, top))
    support = ndimage.binary_dilation(np.asarray(support) > 0, iterations=max(1, math.ceil(max(sx, sy))))
    return alpha * support


def _select_diverse(rows: list[dict], maximum: int) -> list[dict]:
    """Quality-gated farthest sampling favors new shapes and source pages."""
    if not rows:
        return []
    remaining = sorted(rows, key=lambda row: (-row["score"], row["id"]))
    chosen = [remaining.pop(0)]
    while remaining and len(chosen) < maximum:
        def utility(row):
            distance = min(1-float(np.dot(row["_signature"], other["_signature"])) for other in chosen)
            new_page = all(row["source_sha256"] != other["source_sha256"] for other in chosen)
            return row["score"] + .5*distance + (.045 if new_page else 0)
        best = max(range(len(remaining)), key=lambda i: (utility(remaining[i]), remaining[i]["id"]))
        candidate = remaining.pop(best)
        candidate["diversity_distance"] = round(min(1-float(np.dot(candidate["_signature"], other["_signature"]))
                                                    for other in chosen), 6)
        chosen.append(candidate)
    chosen[0]["diversity_distance"] = None
    return chosen


def _contact_sheets(rows: list[dict], destination: Path) -> list[str]:
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 16)
        small = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 12)
    except OSError:
        font = small = ImageFont.load_default()
    outputs = []
    for page, start in enumerate(range(0, len(rows), 15), 1):
        canvas = Image.new("RGB", (1500, 1190), "white"); draw = ImageDraw.Draw(canvas)
        draw.text((12, 10), "PROPOSED observed forms: inspect source context, complete ink, label, role and baseline before import", fill="black", font=font)
        for index, row in enumerate(rows[start:start+15]):
            x, y = (index % 5)*300, 48+(index//5)*380
            draw.text((x+8, y), f"{row['text']} / {row['role']}   score {row['score']:.3f}", fill="black", font=font)
            draw.text((x+8, y+24), row["id"], fill="#555555", font=small)
            with Image.open(destination / row["context_path"]) as original:
                context = original.convert("RGB"); context.thumbnail((282, 170), Image.Resampling.LANCZOS)
                canvas.paste(context, (x+8, y+47))
            with Image.open(destination / row["mask_path"]) as original:
                mask = original.convert("L"); mask.thumbnail((210, 100), Image.Resampling.LANCZOS)
                canvas.paste("#20232a", (x+8, y+225), mask)
            draw.text((x+8, y+332), " / ".join(row["flags"])[:43], fill="#8c392d", font=small)
            draw.line((x, y+370, x+298, y+370), fill="#cccccc")
        path = destination / f"review-{page:02d}.jpg"
        canvas.save(path, quality=94); path.chmod(0o600); outputs.append(str(path))
    return outputs


def mine_candidates(profile: dict, samples_dir: Path | None, destination: Path, *,
                    characters: str | None = None, role: str | None = None,
                    per_label: int = 24, min_score: float = .58) -> dict:
    """Mine private page images into an importable, *unreviewed* crop manifest.

    With ``samples_dir=None``, use original photos preserved in the profile.
    Character labels come from reviewed mask templates, never generic fonts.
    Scores are geometric similarity, not probabilities or recognition claims.
    ``observations.json`` can be imported immediately for geometry proposals;
    review its source context and estimated measurements before acceptance.
    """
    validate_profile(profile)
    if profile["synthetic"]:
        raise ValueError("Mining personal writing requires a personal profile with reviewed source evidence")
    if role is not None and role not in ROLES:
        raise ValueError("Unknown glyph role")
    if not isinstance(per_label, int) or isinstance(per_label, bool) or not 1 <= per_label <= 500:
        raise ValueError("per_label must be an integer from 1 to 500")
    if isinstance(min_score, bool) or not isinstance(min_score, (int, float)) or not math.isfinite(min_score) or not 0 < min_score <= 1:
        raise ValueError("min_score must lie in (0, 1]")
    if characters is not None and (not isinstance(characters, str) or not characters):
        raise ValueError("characters must be a nonempty string")
    destination = Path(destination).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    for subfolder in ("masks", "context"):
        (destination/subfolder).mkdir(exist_ok=True, mode=0o700)
    bank, missing = _templates(profile, role)
    targets = [t for t in bank if characters is None or t["text"] in characters]
    if not targets:
        report = {"algorithm": MINING_VERSION, "status": "awaiting_templates", "source_count": 0,
            "template_count": len(bank), "candidate_count": 0, "candidate_counts": {}, "accepted_glyph_count": 0,
            "manifest": str(destination/"observations.json"), "candidates": str(destination/"candidates.json"),
            "contact_sheets": [], "sources": [], "missing_template_evidence": missing,
            "unsupported_characters": sorted(set(characters or "")), "skipped_sources": [], "counters": {},
            "next_step": "Review at least one isolated source crop and its exact geometry for each requested character and role, then mine again."}
        _write_json(destination/"observations.json", {"schema_version": "1.0", "observations": []})
        _write_json(destination/"candidates.json", {"algorithm": MINING_VERSION, "candidates": []})
        _write_json(destination/"report.json", report)
        return report
    matrix = np.stack([t["signature"] for t in bank])
    template_aspects = np.asarray([t["aspect"] for t in bank])
    target_keys = {(t["text"], t["role"]) for t in targets}
    source_hashes, scanned, skipped, groups, counters = set(), [], [], defaultdict(list), Counter()
    # Existing observations from the exact same image cannot become fresh
    # evidence merely by recropping a few pixels around their recorded box.
    source_map = {s["source_id"]: s for s in profile["sources"]}
    expected_sources = {str(Path(s["source_path"]).expanduser().resolve()): s.get("sha256")
                        for s in profile["sources"] if s.get("kind") == "photo" and s.get("source_path")}
    derived_sources = {}
    for observation in profile.get("observations", []):
        parent = source_map.get(observation.get("source_id"), {})
        if observation.get("kind") != "unlabeled_page" or parent.get("kind") != "pdf" or not observation.get("source_path"):
            continue
        path = str(Path(observation["source_path"]).expanduser().resolve())
        expected_sources[path] = observation.get("normalized_sha256")
        derived_sources[path] = {"original_source_path": parent.get("source_path"),
            "original_source_sha256": parent.get("sha256"), "page_index": observation.get("page_index", 0),
            "coordinate_system": "rendered normalized PNG pixels; not PDF points",
            "page_observation_id": observation["observation_id"]}
    existing = defaultdict(list)
    for obs in profile.get("observations", []):
        if obs.get("bbox_xywh") and obs.get("mask_sha256"):
            source = source_map.get(obs.get("source_id"), {})
            if source.get("sha256"):
                existing[source["sha256"]].append(obs["bbox_xywh"])
    for source_path in _page_paths(profile, samples_dir):
        if not source_path.is_file():
            skipped.append({"source_path": str(source_path), "reason": "preserved source unavailable"}); continue
        source_sha = sha256_file(source_path)
        if samples_dir is None and expected_sources.get(str(source_path)) != source_sha:
            raise ValueError(f"Preserved source evidence hash changed: {source_path}")
        if source_sha in source_hashes:
            counters["duplicate_pages"] += 1; continue
        source_hashes.add(source_sha)
        image, orientation = _open_oriented(source_path)
        already_observed = existing[source_sha] + _aligned_derived_boxes(image, source_sha, source_map, existing)
        scanned.append({"source_path": str(source_path), "source_sha256": source_sha,
                        "oriented_size": list(image.size), "orientation": orientation,
                        **({"derived_from_pdf": derived_sources[str(source_path)]} if str(source_path) in derived_sources else {})})
        page_rows = []
        for component in _whole_components(image):
            counters["whole_components"] += 1
            bbox = component["bbox_xywh"]
            if any(_overlap(bbox, previous) > .65 for previous in already_observed):
                counters["existing_observations"] += 1; continue
            similarity = matrix @ component["signature"]
            # Shape and aspect jointly reject partial letters, most connected
            # words and differently proportioned signs. No label is accepted.
            ratios = np.abs(np.log(component["aspect"] / template_aspects))
            scores = similarity - .20*np.minimum(ratios, 3)
            best = int(np.argmax(scores)); template = bank[best]; score = float(scores[best])
            key = (template["text"], template["role"])
            if key not in target_keys or score < min_score or ratios[best] > .85:
                continue
            rivals = [float(scores[i]) for i, other in enumerate(bank) if other["text"] != template["text"]]
            margin = score-max(rivals) if rivals else None
            flags = ["label and baseline unreviewed"]
            if margin is not None and margin < .035:
                flags.append("ambiguous shape")
            if template["role"].startswith("math.") or not template["text"].isalnum():
                flags.append("verify symbol context and role")
            if template["text"] in "ij:;=≠":
                # Complete components cannot by themselves establish a
                # detached dot or second sign stroke. Do not propose half.
                counters["detached_component_role_skipped"] += 1; continue
            ink_height = component["ink_height_px"]
            xheight = ink_height / template["height_per_xheight"]
            baseline = 3 + ink_height * template["baseline_fraction"]
            identity = "mine-" + hashlib.sha256(json.dumps([source_sha, bbox, key], ensure_ascii=False).encode()).hexdigest()[:20]
            row = {"id": identity, "text": template["text"], "role": template["role"],
                   "source_path": str(source_path), "source_sha256": source_sha, "bbox_xywh": bbox,
                   "baseline_px": round(baseline, 4), "xheight_px": round(xheight, 4),
                   "label_status": "proposed", "score": round(score, 6),
                   "alternative_label_margin": None if margin is None else round(margin, 6),
                   "template_observation_id": template["observation_id"], "template_mask_sha256": template["mask_sha256"],
                   "flags": flags, "measurement_status": "estimated from matched local template; review before acceptance",
                   "component_id": component["component_id"], "_signature": component["signature"], "_component": component}
            page_rows.append(row)
        # Bound work and disk use before writing full-resolution candidate
        # evidence. Selection per page keeps all pages represented later.
        by_key = defaultdict(list)
        for row in page_rows:
            by_key[(row["text"], row["role"])].append(row)
        for key, options in by_key.items():
            counters["similar_components"] += len(options)
            for row in _select_diverse(options, per_label):
                component = row.pop("_component")
                alpha = _isolate(image, component, row["xheight_px"])
                mask_path = destination / "masks" / (row["id"]+".png")
                Image.fromarray(alpha).save(mask_path); mask_path.chmod(0o600)
                row["mask_path"] = str(mask_path.relative_to(destination))
                row["mask_sha256"] = sha256_file(mask_path)
                x, y, w, h = row["bbox_xywh"]
                pad_x, pad_y = max(w, round(row["xheight_px"]*3)), max(h, round(row["xheight_px"]*1.5))
                box = [max(0, x-pad_x), max(0, y-pad_y), min(image.width, x+w+pad_x), min(image.height, y+h+pad_y)]
                context = image.crop(box); draw = ImageDraw.Draw(context)
                draw.rectangle((x-box[0]-1, y-box[1]-1, x+w-box[0]+1, y+h-box[1]+1), outline="#bc382f", width=2)
                context_path = destination / "context" / (row["id"]+".jpg")
                context.save(context_path, quality=95); context_path.chmod(0o600)
                row["context_path"] = str(context_path.relative_to(destination))
                row["context_bbox_xyxy"] = box
                groups[key].append(row)
    chosen = []
    for key in sorted(target_keys):
        chosen.extend(_select_diverse(groups[key], per_label))
    # A byte-identical isolated mask is one piece of evidence, even if the
    # same source image was recompressed or submitted under another name.
    distinct, hashes = [], set()
    for row in chosen:
        if row["mask_sha256"] in hashes:
            counters["duplicate_masks"] += 1; continue
        hashes.add(row["mask_sha256"]); row.pop("_signature", None); distinct.append(row)
    manifest_rows = [{key: row[key] for key in ("id", "text", "role", "source_path", "source_sha256", "bbox_xywh",
                    "mask_path", "baseline_px", "xheight_px", "label_status")} for row in distinct]
    _write_json(destination/"observations.json", {"schema_version": "1.0", "observations": manifest_rows,
        "mining": {"version": MINING_VERSION, "status": "all labels and geometry require review",
                   "derived_sources": derived_sources}})
    _write_json(destination/"candidates.json", {"algorithm": MINING_VERSION, "candidates": distinct})
    sheets = _contact_sheets(distinct, destination)
    counts = Counter(f"{row['role']}:{row['text']}" for row in distinct)
    report = {"algorithm": MINING_VERSION, "status": "proposals_only", "source_count": len(scanned),
        "template_count": len(bank), "candidate_count": len(distinct), "candidate_counts": dict(counts),
        "accepted_glyph_count": 0, "manifest": str(destination/"observations.json"),
        "candidates": str(destination/"candidates.json"), "contact_sheets": sheets, "sources": scanned,
        "missing_template_evidence": missing, "skipped_sources": skipped, "counters": dict(counters),
        "unsupported_characters": sorted(set(characters or "")-{t["text"] for t in targets}),
        "unmatched_labels": sorted(f"{role}:{text}" for text, role in target_keys if f"{role}:{text}" not in counts),
        "limitations": ["Shape matching cannot establish a character or its mathematical role.",
            "Connected words, detached marks and ambiguous components require explicit crop review.",
            "Baselines and x-heights are estimates transferred from the closest reviewed example.",
            "Source orientation and scan perspective remain uncalibrated.",
            "Visually inspect for copies submitted in differently transformed photographs."]}
    _write_json(destination/"report.json", report)
    return report
