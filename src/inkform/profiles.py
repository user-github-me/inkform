"""Validated, private writer profiles with immutable version snapshots.

All renderable geometry is inline vector data. Source paths are provenance only:
loading a profile never opens a source photograph or an external geometry file.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import sys
import tempfile
from collections import Counter
from functools import lru_cache
from importlib.resources import files
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Iterable

from jsonschema import Draft202012Validator

from .errors import InkformError


ROLES = frozenset({"text", "math.variable", "math.digit", "math.operator", "math.function", "mark.correction"})
RENDER_MODES = frozenset({"fresh_strokes", "clean_outline"})
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_RASTER_SHAPE_KEYS = frozenset({
    "image", "image_data", "image_file", "image_url", "bitmap", "bitmap_data",
    "raster", "raster_data", "raster_shape", "raster_file", "mask", "mask_file",
    "png_base64", "jpeg_base64", "data_url",
})


def default_data_dir() -> Path:
    """Return the private data location without creating it."""
    configured = os.environ.get("INKFORM_DATA_DIR")
    if configured:
        return Path(configured).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Inkform"
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "Inkform"
    return Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))).expanduser() / "inkform"


def resolve_render_mode(profile: dict, mode: str | None = None) -> str:
    """Honor the writer's preferred mode unless the caller explicitly selects one.

    Older profiles keep the fresh-strokes default. Resolution never changes
    geometry, relaxes outline calibration, or enables representation fallback.
    """
    selected = profile.get("default_render_mode", "fresh_strokes") if mode is None else mode
    if not isinstance(selected, str) or selected not in RENDER_MODES:
        raise InkformError("invalid_mode", "Use fresh_strokes or clean_outline.", {"mode": selected})
    return selected


@lru_cache(maxsize=1)
def _validator() -> Draft202012Validator:
    schema = json.loads(files("inkform").joinpath("schemas/profile.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _invalid(message: str, path: str = "", **details) -> None:
    raise InkformError("invalid_profile", message, {"path": path, **details})


def _safe_relative_path(value: str) -> bool:
    """Only portable relative paths may be used for profile-owned assets."""
    if not value or value == "." or "\x00" in value or "\\" in value:
        return False
    posix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    return not posix.is_absolute() and not windows.drive and all(part not in {"", ".."} for part in posix.parts)


def _check_json(value, path: str = "", ancestors: set[int] | None = None) -> None:
    """Reject values JSON Schema does not reliably exclude (notably NaN)."""
    ancestors = set() if ancestors is None else ancestors
    if value is None or isinstance(value, (str, bool)):
        return
    if isinstance(value, (int, float)):
        try:
            finite = math.isfinite(value)
        except OverflowError:
            finite = False
        if not finite:
            _invalid("All profile numbers must be finite.", path)
        return
    if not isinstance(value, (dict, list)):
        _invalid("Profiles may contain only JSON values.", path, value_type=type(value).__name__)
    if id(value) in ancestors:
        _invalid("Profile data must not contain circular references.", path)
    ancestors.add(id(value))
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                _invalid("Profile object keys must be strings.", path)
            child_path = f"{path}/{key}"
            if key.endswith("_file") and isinstance(child, str) and not _safe_relative_path(child):
                _invalid("Profile asset files must use safe relative paths.", child_path)
            _check_json(child, child_path, ancestors)
    else:
        for index, child in enumerate(value):
            _check_json(child, f"{path}/{index}", ancestors)
    ancestors.remove(id(value))


def _cubic_extrema(p0: float, p1: float, p2: float, p3: float) -> list[float]:
    """Curve extrema, rather than the wider control-point bounding box."""
    # The derivative is 3 * (a*t*t + b*t + c).
    scale = max(1.0, abs(p0), abs(p1), abs(p2), abs(p3))
    q0, q1, q2, q3 = (value / scale for value in (p0, p1, p2, p3))
    a = -q0 + 3 * q1 - 3 * q2 + q3
    b = 2 * (q0 - 2 * q1 + q2)
    c = q1 - q0
    roots: list[float] = []
    if abs(a) < 1e-12:
        if abs(b) >= 1e-12:
            roots.append(-c / b)
    else:
        discriminant = b * b - 4 * a * c
        if discriminant >= 0:
            radical = math.sqrt(discriminant)
            roots.extend(((-b + radical) / (2 * a), (-b - radical) / (2 * a)))
    values = [p0, p3]
    for t in roots:
        if 0 < t < 1:
            u = 1 - t
            values.append(u ** 3 * p0 + 3 * u * u * t * p1 + 3 * u * t * t * p2 + t ** 3 * p3)
    return values


def geometry_bounds(strokes: list[dict]) -> list[float]:
    """Return [min_x, min_y, max_x, max_y] of path geometry, excluding pen width."""
    xs: list[float] = []
    ys: list[float] = []
    for stroke in strokes:
        current = stroke["start"]
        xs.append(current[0])
        ys.append(current[1])
        for segment in stroke["segments"]:
            end = segment["end"]
            if segment["type"] == "cubic":
                xs.extend(_cubic_extrema(current[0], segment["control1"][0], segment["control2"][0], end[0]))
                ys.extend(_cubic_extrema(current[1], segment["control1"][1], segment["control2"][1], end[1]))
            else:
                xs.append(end[0])
                ys.append(end[1])
            current = end
    return [min(xs), min(ys), max(xs), max(ys)]


def reviewed_actor(value) -> bool:
    """A review must identify an actor, rather than an unknown placeholder."""
    return isinstance(value, str) and bool(value.strip()) and value.strip().lower() not in {"unknown", "none", "unreviewed"}


def glyph_evidence_key(glyph: dict, observations: dict) -> tuple:
    """One source mask remains one sample across aliases/representations."""
    identities = []
    for ref in glyph.get("source_refs", []):
        identity = ref.get("observation_id")
        if identity:
            observation = observations.get(identity, {})
            identities.append(("mask", observation["mask_sha256"]) if observation.get("mask_sha256")
                              else (ref["source_id"], identity))
    if identities:
        return tuple(sorted(set(identities)))
    digest = hashlib.sha256(json.dumps(glyph["strokes"], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return ("geometry_record", digest)


def independent_frequency_observations(profile: dict, candidates: list[dict] | None = None) -> tuple[list[dict], dict]:
    """Keep reviewed representative, nonoverlapping correction counts once."""
    sources = {source["source_id"]: source for source in profile["sources"]}
    groups, excluded = {}, Counter()
    for observation in profile.get("observations", []) if candidates is None else candidates:
        if observation.get("kind") != "correction_frequency":
            continue
        if (observation.get("review_status") != "reviewed" or not reviewed_actor(observation.get("reviewed_by"))
                or observation.get("representative") is not True):
            excluded["not_reviewed_representative_frequency"] += 1
            continue
        source = sources[observation["source_id"]]
        key = (source.get("sha256", source["source_id"]), observation["page_index"], observation["scope"],
               tuple(observation["bbox_xywh"]) if observation["scope"] == "region" else ())
        groups.setdefault(key, []).append(observation)
    unique = []
    for key, records in groups.items():
        if len({(item["word_count"], item["correction_count"]) for item in records}) > 1:
            excluded["conflicting_counts_for_same_region"] += len(records)
            continue
        unique.append((key, min(records, key=lambda item: item["observation_id"])))
        excluded["duplicate_frequency_region"] += len(records)-1
    selected = []
    for key, observation in sorted(unique, key=lambda pair: (pair[0][0], pair[0][1], pair[0][2] != "page", pair[0][3])):
        overlap = False
        for previous_key, _ in selected:
            if previous_key[:2] != key[:2]:
                continue
            if previous_key[2] == "page" or key[2] == "page":
                overlap = True
                break
            x, y, w, h = key[3]
            px, py, pw, ph = previous_key[3]
            if max(x, px) < min(x+w, px+pw) and max(y, py) < min(y+h, py+ph):
                overlap = True
                break
        if overlap:
            excluded["overlapping_frequency_region"] += 1
        else:
            selected.append((key, observation))
    return [observation for _, observation in selected], {key: count for key, count in excluded.items() if count}


def _validate_style_models(profile: dict, observations: dict) -> None:
    learned = profile.get("variation", {}).get("learned")
    if learned is not None:
        if profile["synthetic"]:
            _invalid("Synthetic profiles cannot claim learned personal variation.", "/variation/learned")
        glyphs = {glyph["glyph_id"]: glyph for glyph in accepted_glyphs(profile)}
        forms = set()
        for index, model in enumerate(learned["by_glyph"]):
            path = f"/variation/learned/by_glyph/{index}"
            form = (model["role"], model["text"])
            if form in forms:
                _invalid("Learned variation must have one entry per role and text.", path)
            forms.add(form)
            evidence, observation_ids = set(), set()
            for identity in model["glyph_ids"]:
                glyph = glyphs.get(identity)
                if glyph is None or (glyph["role"], glyph["text"]) != form:
                    _invalid("Variation evidence must name reviewed glyphs of the same form.", path)
                if glyph.get("derivation") or glyph.get("variation_dimensions_observed") is False:
                    _invalid("Derived shapes and crop-chosen dimensions are not independent observed variation evidence.", path)
                refs = glyph["source_refs"]
                if not refs or any(ref.get("observation_id") not in observations for ref in refs):
                    _invalid("Learned variation requires observation provenance for every sample.", path)
                observation_ids.update(ref["observation_id"] for ref in refs)
                evidence.add(glyph_evidence_key(glyph, observations))
            if len(evidence) != model["source_count"] or len(evidence) < 2:
                _invalid("Variation source_count must count independent source observations, not aliases.", path)
            if observation_ids != set(model["observation_ids"]):
                _invalid("Variation observation IDs must match the reviewed glyph evidence.", path)
        word_groups = {}
        for identity in learned["word_gap_observation_ids"]:
            observation = observations.get(identity, {})
            if (observation.get("kind") != "word" or observation.get("label_status") != "reviewed"
                    or observation.get("layout_status") != "reviewed" or not reviewed_actor(observation.get("reviewed_by"))):
                _invalid("Word-gap variation requires reviewed word layout evidence.", "/variation/learned/word_gap_observation_ids")
            box, xheight = observation.get("bbox_xywh"), observation.get("xheight_px")
            if (not isinstance(box, list) or len(box) != 4 or any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in box)
                    or min(box[2:]) <= 0 or isinstance(xheight, bool) or not isinstance(xheight, (int, float)) or xheight <= 0):
                _invalid("Learned word-gap evidence requires valid pixel boxes and local x-height.", "/variation/learned/word_gap_observation_ids")
            for field in ("page_index", "word_index"):
                value = observation.get(field)
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    _invalid("Learned word-gap evidence needs explicit page and word indices.", "/variation/learned/word_gap_observation_ids")
            if not all(isinstance(observation.get(field, default), str) and observation.get(field, default).strip()
                       for field, default in (("line_id", None), ("column_id", "body"))):
                _invalid("Learned word-gap evidence needs explicit line identities.", "/variation/learned/word_gap_observation_ids")
            key = (observation["source_id"], observation["page_index"], observation.get("column_id", "body"), observation["line_id"])
            word_groups.setdefault(key, []).append(observation)
        gap_count = learned["word_gap_sample_count"]
        actual_gaps, gap_lines = 0, 0
        for words in word_groups.values():
            words.sort(key=lambda word: word["word_index"])
            if len({word["word_index"] for word in words}) != len(words):
                _invalid("Learned word-gap evidence cannot include ambiguous duplicate word indices.", "/variation/learned/word_gap_observation_ids")
            line_gaps = 0
            for left, right in zip(words, words[1:]):
                lb, rb = left["bbox_xywh"], right["bbox_xywh"]
                if (right["word_index"] == left["word_index"]+1 and rb[0] > lb[0]+lb[2]
                        and max(left["xheight_px"], right["xheight_px"])/min(left["xheight_px"], right["xheight_px"]) <= 1.25
                        and max(lb[1], rb[1]) < min(lb[1]+lb[3], rb[1]+rb[3])):
                    line_gaps += 1
            actual_gaps += line_gaps
            gap_lines += line_gaps > 0
        if (gap_count != actual_gaps or (gap_count and (gap_count < 3 or gap_lines < 2))
                or (learned["word_gap"] > 0 and not gap_count)):
            _invalid("Word-gap variation must retain its supporting adjacent-word observations.", "/variation/learned/word_gap_sample_count")
        if not forms and not gap_count:
            _invalid("A learned variation model must retain supported form or word-gap evidence.", "/variation/learned")
    corrections = profile.get("corrections")
    if corrections is None:
        return
    path = "/corrections"
    if corrections["correction_count"] > corrections["sampled_word_count"]:
        _invalid("Correction counts cannot exceed the number of sampled words.", path)
    expected_rate = min(.35, corrections["correction_count"] / corrections["sampled_word_count"])
    if not math.isclose(corrections["rate"], expected_rate, abs_tol=1e-12):
        _invalid("Correction rate must match the bounded reviewed word frequency.", path)
    if corrections["basis"] == "synthetic_demo":
        if not profile["synthetic"] or corrections["observation_ids"] or corrections["source_refs"]:
            _invalid("Authored demo correction rates belong only to synthetic profiles without personal evidence.", path)
        return
    if profile["synthetic"] or not corrections["observation_ids"]:
        _invalid("Learned correction rates require nonsynthetic reviewed frequency evidence.", path)
    count, words, expected_refs = 0, 0, set()
    for identity in corrections["observation_ids"]:
        observation = observations.get(identity, {})
        if (observation.get("kind") != "correction_frequency" or observation.get("review_status") != "reviewed"
                or observation.get("representative") is not True or not reviewed_actor(observation.get("reviewed_by"))):
            _invalid("Correction rates require reviewed representative word-frequency observations.", path)
        count += observation["correction_count"]
        words += observation["word_count"]
        expected_refs.add((observation["source_id"], identity))
    selected, _ = independent_frequency_observations(profile, [observations[identity] for identity in corrections["observation_ids"]])
    if len(selected) != len(corrections["observation_ids"]):
        _invalid("Correction frequencies must not double-count duplicate or overlapping sample regions.", path)
    if count != corrections["correction_count"] or words != corrections["sampled_word_count"]:
        _invalid("Correction totals must match their reviewed frequency observations.", path)
    refs = {(ref["source_id"], ref.get("observation_id")) for ref in corrections["source_refs"]}
    if refs != expected_refs:
        _invalid("Correction source references must match their frequency observations.", path)


def validate_profile(profile: dict) -> dict:
    """Validate shape, units, provenance, and vector geometry; return the input."""
    _check_json(profile)
    error = next(_validator().iter_errors(profile), None)
    if error is not None:
        _invalid(error.message, "/" + "/".join(map(str, error.absolute_path)))
    source_ids: set[str] = set()
    sources = {}
    for index, source in enumerate(profile["sources"]):
        source_id = source["source_id"]
        if source_id in source_ids:
            _invalid("Source IDs must be unique.", f"/sources/{index}/source_id", source_id=source_id)
        source_ids.add(source_id)
        sources[source_id] = source
        if source.get("synthetic", profile["synthetic"]) != profile["synthetic"]:
            _invalid("Synthetic and personal source evidence cannot be mixed.", f"/sources/{index}/synthetic")
    observations: dict[str, dict] = {}
    for index, observation in enumerate(profile.get("observations", [])):
        identity = observation["observation_id"]
        if identity in observations:
            _invalid("Observation IDs must be unique.", f"/observations/{index}/observation_id")
        if observation["source_id"] not in source_ids:
            _invalid("Observation source reference does not name a stored source.", f"/observations/{index}/source_id")
        path = f"/observations/{index}"
        if observation.get("synthetic", profile["synthetic"]) != profile["synthetic"]:
            _invalid("Synthetic and personal observations cannot be mixed.", f"{path}/synthetic")
        source_hash = sources[observation["source_id"]].get("sha256")
        if "source_sha256" in observation and observation["source_sha256"] != source_hash:
            _invalid("An observation review must refer to its exact stored source bytes.", f"{path}/source_sha256")
        if observation.get("kind") == "correction_frequency":
            if observation["correction_count"] > observation["word_count"]:
                _invalid("Count corrected words once within the complete sampled word count.", path)
            bbox = observation.get("bbox_xywh")
            if bbox is not None and (min(bbox[:2]) < 0 or min(bbox[2:]) <= 0):
                _invalid("Frequency regions require a nonempty source-pixel rectangle.", f"{path}/bbox_xywh")
            if observation["review_status"] == "reviewed" and (not reviewed_actor(observation.get("reviewed_by")) or not observation.get("source_sha256")):
                _invalid("Reviewed frequency counts need an identified actor and the exact source hash.", path)
        observations[identity] = observation
    glyph_ids: set[str] = set()
    for index, glyph in enumerate(profile["glyphs"]):
        path = f"/glyphs/{index}"
        glyph_id = glyph["glyph_id"]
        if glyph_id in glyph_ids:
            _invalid("Glyph IDs must be unique; retain variants under separate IDs.", f"{path}/glyph_id", glyph_id=glyph_id)
        glyph_ids.add(glyph_id)
        if glyph.get("synthetic", profile["synthetic"]) != profile["synthetic"]:
            _invalid("Synthetic and personal glyph evidence cannot be mixed.", f"{path}/synthetic")
        for key in _RASTER_SHAPE_KEYS.intersection(glyph):
            _invalid("Renderable glyphs must contain vector paths, never raster shapes.", f"{path}/{key}")
        reviewed = glyph["label_status"] == "reviewed" or glyph["geometry_status"] == "reviewed"
        if reviewed and not reviewed_actor(glyph["reviewed_by"]):
            _invalid("Reviewed glyphs must identify their review actor.", f"{path}/reviewed_by")
        if reviewed and not profile["synthetic"] and not glyph["source_refs"]:
            _invalid("Reviewed personal glyphs must retain source provenance.", f"{path}/source_refs")
        for ref_index, ref in enumerate(glyph["source_refs"]):
            if ref["source_id"] not in source_ids:
                _invalid("Glyph source reference does not name a stored source.", f"{path}/source_refs/{ref_index}", source_id=ref["source_id"])
            if "observations" in profile and "observation_id" in ref:
                observation = observations.get(ref["observation_id"])
                if observation is None or observation["source_id"] != ref["source_id"]:
                    _invalid("Glyph observation reference must name an observation from the same source.", f"{path}/source_refs/{ref_index}")
        bounds = glyph["ink_bounds"]
        if bounds[0] > bounds[2] or bounds[1] > bounds[3]:
            _invalid("Ink bounds must be ordered [min_x, min_y, max_x, max_y].", f"{path}/ink_bounds")
        actual = geometry_bounds(glyph["strokes"])
        if not all(math.isfinite(value) for value in actual):
            _invalid("Vector geometry exceeds finite numeric precision.", f"{path}/strokes")
        tolerance = 1e-6 * max(1, *(abs(v) for v in actual + bounds))
        if actual[0] < bounds[0] - tolerance or actual[1] < bounds[1] - tolerance or actual[2] > bounds[2] + tolerance or actual[3] > bounds[3] + tolerance:
            _invalid("Declared ink bounds must enclose the complete vector path.", f"{path}/ink_bounds", geometry_bounds=actual)
        stroke_ids: set[str] = set()
        for stroke_index, stroke in enumerate(glyph["strokes"]):
            stroke_path = f"{path}/strokes/{stroke_index}"
            if "stroke_id" in stroke:
                if stroke["stroke_id"] in stroke_ids:
                    _invalid("Stroke IDs must be unique within a glyph.", f"{stroke_path}/stroke_id")
                stroke_ids.add(stroke["stroke_id"])
            samples = stroke.get("width_profile")
            if samples:
                positions = [sample["s"] for sample in samples]
                if positions[0] != 0 or positions[-1] != 1 or any(a >= b for a, b in zip(positions, positions[1:])):
                    _invalid("Width profiles must run from 0 to 1 with strictly increasing positions.", f"{stroke_path}/width_profile")
    _validate_derivations(profile)
    _validate_style_models(profile, observations)
    if "graph_style" in profile:
        from .graphs import validate_graph_style
        validate_graph_style(profile)
    return profile


def _validate_derivations(profile):
    """Bind estimated style variants to immutable parents and exact review ink."""
    from .geometry import geometry_hash
    glyphs = {g["glyph_id"]: g for g in profile["glyphs"]}
    for index, glyph in enumerate(profile["glyphs"]):
        derived = glyph.get("derivation")
        if derived is None:
            continue
        path = f"/glyphs/{index}/derivation"
        parents = derived["parent_glyph_ids"]
        hashes = derived["parent_geometry_sha256"]
        if not parents or len(parents) != len(set(parents)) or len(parents) != len(hashes):
            _invalid("Derived forms require distinct parent IDs with corresponding geometry hashes.", path)
        refs = set()
        for parent_id, expected in zip(parents, hashes):
            parent = glyphs.get(parent_id)
            if (not parent or parent.get("derivation") or parent["label_status"] != "reviewed"
                    or parent["geometry_status"] != "reviewed" or geometry_hash(parent) != expected):
                _invalid("Derived forms require unchanged reviewed, non-derived parent geometry.", path)
            if any(parent[key] != glyph[key] for key in ("text", "role", "representation")):
                _invalid("Derived variants must retain the parent character, role and representation.", path)
            refs.update(json.dumps(ref, sort_keys=True) for ref in parent["source_refs"])
        if refs != {json.dumps(ref, sort_keys=True) for ref in glyph["source_refs"]}:
            _invalid("Derived forms must retain exactly their parent source provenance.", path)
        if derived["geometry_sha256"] != geometry_hash(glyph):
            _invalid("Derived review must bind the exact current geometry.", path)
        if glyph.get("variation_dimensions_observed") is not False:
            _invalid("Synthesized dimensions cannot claim new observed evidence.", path)
        if derived["review_status"] != glyph["geometry_status"]:
            _invalid("Derived form review status must match its geometry review.", path)
        if derived["review_status"] in {"reviewed", "rejected"}:
            if not reviewed_actor(derived["reviewed_by"]) or derived["reviewed_by"] != glyph["reviewed_by"]:
                _invalid("A derived geometry decision must name its reviewer.", path)


def accepted_glyphs(profile: dict) -> list[dict]:
    """Return reviewed forms only; proposed/rejected evidence remains stored."""
    return [glyph for glyph in profile["glyphs"] if glyph["label_status"] == "reviewed" and glyph["geometry_status"] == "reviewed" and reviewed_actor(glyph.get("reviewed_by"))]


def renderable_glyphs(profile: dict) -> list[dict]:
    """Use observed forms by default; keep experimental geometry in storage.

    Reviewing an invented form does not make it an observation of the writer.
    Historical profiles therefore gain the faithful default without deleting
    either their source evidence or their previous review decisions.
    """
    glyphs = accepted_glyphs(profile)
    if profile.get("variation", {}).get("mode", "observed") == "experimental":
        return glyphs
    return [glyph for glyph in glyphs if not glyph.get("derivation")]


def coverage(profile: dict, requirements: Iterable[tuple[str, str]]) -> dict:
    """Count exact (role, text) coverage with no text/math role fallback."""
    required = Counter()
    for requirement in requirements:
        if not isinstance(requirement, (tuple, list)) or len(requirement) != 2:
            raise InkformError("invalid_requirements", "Each glyph requirement must be a (role, text) pair.")
        role, text = requirement
        if role not in ROLES or not isinstance(text, str) or not text:
            raise InkformError("invalid_requirements", "Glyph requirements need a supported role and nonempty text.")
        required[(role, text)] += 1
    available = Counter((glyph["role"], glyph["text"]) for glyph in renderable_glyphs(profile))
    missing = [{"role": role, "text": text, "count": count} for (role, text), count in required.items() if not available[(role, text)]]
    return {
        "complete": not missing,
        "required_count": len(required),
        "covered_count": len(required) - len(missing),
        "missing_count": len(missing),
        "accepted_glyph_count": sum(available.values()),
        "missing_glyphs": missing,
        "glyph_counts": [{"role": role, "text": text, "required_count": count, "available_variants": available[(role, text)]} for (role, text), count in required.items()],
    }


def _reject_constant(value: str):
    raise ValueError(f"Non-finite JSON value: {value}")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON object key: {key}")
        result[key] = value
    return result


def _read_json(path: Path) -> dict:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle, parse_constant=_reject_constant, object_pairs_hook=_unique_object)
    except FileNotFoundError as exc:
        raise InkformError("profile_not_found", f"Profile does not exist: {path}", {"path": str(path)}) from exc
    except (ValueError, UnicodeError) as exc:
        raise InkformError("invalid_profile_json", "Profile JSON is invalid.", {"path": str(path), "reason": str(exc)}) from exc
    except OSError as exc:
        raise InkformError("profile_io_error", "Could not read profile.", {"path": str(path), "reason": str(exc)}) from exc


def _profile_from_pointer(pointer_path: Path) -> dict:
    pointer = _read_json(pointer_path)
    if not isinstance(pointer, dict):
        raise InkformError("invalid_profile_pointer", "Current profile pointer must be an object.")
    profile_id = pointer.get("profile_id")
    version = pointer.get("profile_version")
    if not isinstance(profile_id, str) or not _SAFE_ID.fullmatch(profile_id) or not isinstance(version, str) or not _SAFE_ID.fullmatch(version):
        raise InkformError("invalid_profile_pointer", "Current profile pointer has an invalid ID or version.")
    relative = pointer.get("snapshot")
    if not isinstance(relative, str) or not _safe_relative_path(relative) or relative != f"{version}/profile.json":
        raise InkformError("invalid_profile_pointer", "Current profile pointer must identify its local version snapshot.")
    snapshot = pointer_path.parent / relative
    if not snapshot.resolve().is_relative_to(pointer_path.parent.resolve()):
        raise InkformError("invalid_profile_pointer", "Current profile pointer escapes the profile directory.")
    profile = validate_profile(_read_json(snapshot))
    if profile["profile_id"] != profile_id or profile["profile_version"] != version:
        raise InkformError("invalid_profile_pointer", "Current profile pointer and snapshot identity disagree.")
    return profile


def load_profile(path_or_id: str | Path, data_dir: Path | None = None) -> dict:
    """Load an ID, profile JSON, current pointer, or snapshot/profile directory."""
    if not isinstance(path_or_id, (str, Path)) or not str(path_or_id):
        raise InkformError("profile_not_found", "A profile ID or JSON path is required.")
    path = Path(path_or_id).expanduser()
    explicit_path = isinstance(path_or_id, Path) or path.is_absolute() or len(path.parts) > 1 or path.suffix == ".json" or path.exists()
    if not explicit_path:
        if not _SAFE_ID.fullmatch(str(path_or_id)):
            raise InkformError("invalid_profile", "Profile ID contains unsafe characters.")
        path = (Path(data_dir).expanduser() if data_dir is not None else default_data_dir()) / "profiles" / str(path_or_id) / "current.json"
    if path.is_dir():
        path = path / ("profile.json" if (path / "profile.json").is_file() else "current.json")
    return _profile_from_pointer(path) if path.name == "current.json" else validate_profile(_read_json(path))


def _json_bytes(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")


def _write_temp(directory: Path, data: bytes) -> Path:
    fd, name = tempfile.mkstemp(prefix=".inkform-", suffix=".tmp", dir=directory)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    return Path(name)


def save_profile(profile: dict, data_dir: Path | None = None) -> Path:
    """Create an immutable snapshot and atomically point current.json to it.

    Saving identical data for the same ID/version is idempotent. A changed
    snapshot requires a new version; existing snapshots are never overwritten.
    """
    validate_profile(profile)
    base = (Path(data_dir).expanduser() if data_dir is not None else default_data_dir()).resolve()
    profile_dir = base / "profiles" / profile["profile_id"]
    version_dir = profile_dir / profile["profile_version"]
    snapshot = version_dir / "profile.json"
    temporary: Path | None = None
    try:
        base.mkdir(mode=0o700, parents=True, exist_ok=True)
        for directory in (base / "profiles", profile_dir, version_dir):
            if directory.is_symlink():
                raise InkformError("invalid_profile", "Profile storage directories must not be symbolic links.", {"path": str(directory)})
            directory.mkdir(mode=0o700, exist_ok=True)
        if snapshot.is_symlink():
            raise InkformError("invalid_profile", "Profile snapshot must not be a symbolic link.")
        payload = _json_bytes(profile)
        temporary = _write_temp(version_dir, payload)
        try:
            # Link is an atomic, exclusive publication of the completed file.
            os.link(temporary, snapshot)
        except FileExistsError:
            if _json_bytes(_read_json(snapshot)) != payload:
                raise InkformError("profile_version_exists", "This profile version is immutable; save changes under a new version.", {"profile_id": profile["profile_id"], "profile_version": profile["profile_version"], "path": str(snapshot)})
        temporary.unlink()
        temporary = None
        pointer = {"schema_version": "1.0", "profile_id": profile["profile_id"], "profile_version": profile["profile_version"], "snapshot": f"{profile['profile_version']}/profile.json"}
        temporary = _write_temp(profile_dir, _json_bytes(pointer))
        os.replace(temporary, profile_dir / "current.json")
        temporary = None
    except OSError as exc:
        raise InkformError("profile_io_error", "Could not save private profile.", {"path": str(snapshot), "reason": str(exc)}) from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return snapshot


def next_profile_version(profile: dict) -> str:
    """Increment a numeric patch version, or append a safe revision suffix."""
    version = profile["profile_version"]
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version)
    if match:
        return f"{match[1]}.{match[2]}.{int(match[3]) + 1}"
    suffix = re.fullmatch(r"(.+)\.revision-(\d+)", version)
    return f"{suffix[1]}.revision-{int(suffix[2]) + 1}" if suffix else f"{version}.revision-1"


def _merge_dict(existing: dict, incoming: dict) -> dict:
    result = copy.deepcopy(existing)
    for key, value in incoming.items():
        result[key] = _merge_dict(result[key], value) if isinstance(result.get(key), dict) and isinstance(value, dict) else copy.deepcopy(value)
    return result


def merge_profile(existing: dict, incoming: dict, profile_version: str | None = None) -> dict:
    """Apply a partial update while retaining unrelated glyphs and source records.

    An incoming record replaces the matching glyph ID. Source records merge
    metadata, but a source ID cannot be reassigned to different hashed evidence.
    Neither input is modified; the result is validated before return.
    """
    validate_profile(existing)
    _check_json(incoming)
    if not isinstance(incoming, dict):
        _invalid("A profile update must be an object.")
    if incoming.get("profile_id", existing["profile_id"]) != existing["profile_id"]:
        _invalid("An update cannot change the profile ID.", "/profile_id")
    if incoming.get("synthetic", existing["synthetic"]) != existing["synthetic"]:
        _invalid("An update cannot mix a synthetic profile with personal handwriting evidence.", "/synthetic")
    result = _merge_dict(existing, {key: value for key, value in incoming.items() if key not in {"glyphs", "sources", "observations", "profile_version"}})
    for key, id_key in (("sources", "source_id"), ("observations", "observation_id"), ("glyphs", "glyph_id")):
        if key not in existing and key not in incoming:
            continue
        records = {record[id_key]: copy.deepcopy(record) for record in existing.get(key, [])}
        updates = incoming.get(key, [])
        if not isinstance(updates, list):
            _invalid(f"Updated {key} must be a list.", f"/{key}")
        updated_ids: set[str] = set()
        for index, record in enumerate(updates):
            if not isinstance(record, dict) or not isinstance(record.get(id_key), str):
                _invalid(f"Updated {key} need {id_key}.", f"/{key}/{index}")
            identity = record[id_key]
            if identity in updated_ids:
                _invalid(f"Updated {key} contain a duplicate ID.", f"/{key}/{index}/{id_key}")
            updated_ids.add(identity)
            old = records.get(identity)
            if key in {"sources", "observations"} and old:
                hash_key = "sha256" if key == "sources" else "mask_sha256"
                if old.get(hash_key) and record.get(hash_key) and old[hash_key].lower() != record[hash_key].lower():
                    raise InkformError("profile_source_conflict", "An evidence ID cannot be reassigned to different evidence.", {id_key: identity})
                if key == "observations" and record.get("source_id", old["source_id"]) != old["source_id"]:
                    raise InkformError("profile_source_conflict", "An observation ID cannot be reassigned to another source.", {id_key: identity})
                records[identity] = _merge_dict(old, record)
            else:
                records[identity] = copy.deepcopy(record)
        result[key] = list(records.values())
    result["profile_version"] = profile_version or incoming.get("profile_version") or next_profile_version(existing)
    if result["profile_version"] == existing["profile_version"]:
        _invalid("Profile updates require a new version.", "/profile_version")
    return validate_profile(result)
