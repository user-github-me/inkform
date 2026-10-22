"""Writer-conditioned vector style proposals, with explicit inferred provenance.

This is geometric augmentation of a writer's reviewed forms, not new observed
handwriting or a recovered motor model. Smooth local changes move bowls, arches,
stems and endings together with their counters. Proposals require review of the
exact generated paths before the renderer may use them.
"""
from __future__ import annotations

import copy
import hashlib
import html
import json
import math
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

from .errors import InkformError
from .geometry import geometry_hash
from .profiles import accepted_glyphs, geometry_bounds, reviewed_actor, validate_profile
from .smoothing import outline_topology
from .variation import contour_topology, geometry_family, interpolate_forms, stable_value


SYNTHESIS_VERSION = "inkform-style-family-1"
MIN_STRUCTURAL_DISTANCE_MM = .075
_FINITE_SHAPES = {".", ",", ":", ";", "'", "’", "‘", "·", "•", "∙", "⋅", "…", "−", "-", "_", "|", "‖", "―", "—", "–", "="}

# These names describe spatial regions, not an OCR claim about anatomy. Applying
# the same continuous field to every contour preserves an outline's counters.
# Each entry is (centre_x, centre_y, displacement_x, displacement_y, radius).
# Coordinates are relative to the recorded ink box; displacement/radius are in
# x-height units. Several genuinely different local configurations are searched
# rather than one master path with a random global width or slant.
_RECIPES = {
    "upper_arch_left": [(.25, .15, -.17, -.09, .47), (.78, .75, .10, .04, .40)],
    "upper_arch_right": [(.72, .17, .17, -.08, .47), (.23, .72, -.08, .06, .40)],
    "lower_bowl_left": [(.18, .78, -.15, .10, .46), (.78, .18, .08, -.04, .38)],
    "lower_bowl_right": [(.78, .78, .15, .11, .46), (.22, .18, -.08, -.03, .38)],
    "high_waist": [(.52, .54, .01, -.18, .42), (.12, .10, -.07, .03, .38)],
    "low_waist": [(.50, .43, -.01, .18, .42), (.88, .86, .07, -.03, .38)],
    "left_stem_bow": [(.08, .52, -.18, .01, .40), (.80, .28, .03, -.09, .42)],
    "right_stem_bow": [(.90, .48, .18, -.01, .40), (.18, .76, -.03, .09, .42)],
    "upper_terminal_sweep": [(.08, .08, .17, -.12, .36), (.76, .76, -.06, .06, .44)],
    "lower_terminal_sweep": [(.90, .92, .17, -.12, .36), (.22, .22, -.06, .06, .44)],
    "diagonal_rounding": [(.22, .25, -.10, .13, .41), (.78, .75, .10, -.13, .41)],
    "opposite_rounding": [(.78, .22, .10, .13, .41), (.22, .78, -.10, -.13, .41)],
}


def _mapped_strokes(strokes, transform):
    result = copy.deepcopy(strokes)
    for stroke in result:
        stroke["start"] = list(transform(*stroke["start"]))
        for segment in stroke["segments"]:
            for key in ("control1", "control2", "end"):
                if key in segment:
                    segment[key] = list(transform(*segment[key]))
    return result


def _subdivide(strokes, maximum_span=.13):
    """Give a local field enough cubic spans to bend a formerly straight stem."""
    def point(value):
        return [float(v) for v in value]

    def spans(p0, p1, p2, p3, depth=0):
        # Control-polygon length bounds both a line and a curved span.
        length = sum(float(np.linalg.norm(b-a)) for a, b in zip((p0, p1, p2), (p1, p2, p3)))
        if length <= maximum_span or depth >= 9:
            return [{"type": "cubic", "control1": point(p1), "control2": point(p2), "end": point(p3)}]
        a, b, c = (p0+p1)/2, (p1+p2)/2, (p2+p3)/2
        d, e = (a+b)/2, (b+c)/2
        middle = (d+e)/2
        return spans(p0, a, d, middle, depth+1) + spans(middle, e, c, p3, depth+1)

    result = copy.deepcopy(strokes)
    for stroke in result:
        current = np.asarray(stroke["start"], dtype=float)
        original = stroke["segments"]
        if stroke.get("closed") and (not original or not np.allclose(original[-1]["end"], stroke["start"], atol=1e-12, rtol=0)):
            original = original + [{"type": "line", "end": stroke["start"]}]
        segments = []
        for segment in original:
            end = np.asarray(segment["end"], dtype=float)
            if segment["type"] == "line":
                p1, p2 = current+(end-current)/3, current+2*(end-current)/3
            else:
                p1, p2 = np.asarray(segment["control1"], dtype=float), np.asarray(segment["control2"], dtype=float)
            segments.extend(spans(current, p1, p2, end))
            current = end
        stroke["segments"] = segments
    return result


def _style_parameters(glyph, recipe, seed, attempt):
    left, top, right, bottom = geometry_bounds(glyph["strokes"])
    # A body-sized radius avoids extreme derivatives in narrow strokes such as
    # a digit 1. The derived source keeps the original writing scale.
    identity = f"{glyph['glyph_id']}:{recipe}:{attempt}"
    body_scale = min(1.5, max(1., bottom-top))
    scale = (.82 + .36*stable_value(seed, identity, "strength"))*body_scale
    fields = []
    for index, (cx, cy, dx, dy, radius) in enumerate(_RECIPES[recipe]):
        jitter_x = .10*(stable_value(seed, identity, f"cx-{index}")-.5)
        jitter_y = .10*(stable_value(seed, identity, f"cy-{index}")-.5)
        fields.append({"center": [left+(cx+jitter_x)*(right-left), top+(cy+jitter_y)*(bottom-top)],
                       "displacement": [dx*scale, dy*scale], "radius": radius*body_scale})
    # Gaussian gradient max = sqrt(2/e)/radius. A vector displacement has a
    # rank-one Jacobian; the summed spectral-norm upper bound being <1 makes the
    # entire ambient field injective and orientation preserving.
    derivative_bound = sum(math.sqrt(2/math.e)*math.hypot(*field["displacement"])/field["radius"] for field in fields)
    gain = min(1., .72/max(derivative_bound, 1e-12))
    for field in fields:
        field["displacement"] = [round(value*gain, 10) for value in field["displacement"]]
    return {"fields": fields, "composition_steps": 3, "step_strength": .65,
            "jacobian_residual_norm_upper_bound": round(derivative_bound*gain*.65, 10),
            "maximum_displacement_bound_xheight": round(1.95*sum(math.hypot(*f["displacement"]) for f in fields), 10),
            "cubic_span_xheight": .13}


def _one_parameters(glyph, seed, attempt):
    """Infer the existing digit's stem axis; change its own hook/foot around it."""
    left, top, right, bottom = geometry_bounds(glyph["strokes"])
    height = bottom-top
    points = []
    for stroke in _subdivide(glyph["strokes"]):
        points.extend([stroke["start"]]+[s["end"] for s in stroke["segments"]])
    middle = np.asarray([p for p in points if top+.28*height <= p[1] <= top+.72*height])
    if len(middle) < 3:
        slope, intercept = 0., (left+right)/2
    else:
        slope, intercept = np.linalg.lstsq(np.column_stack((middle[:, 1], np.ones(len(middle)))), middle[:, 0], rcond=None)[0]
    identity = glyph["glyph_id"]+f":one:{attempt}"
    signed = lambda key: 2*stable_value(seed, identity, key)-1
    return {"anatomy": "one_stem_hook_foot", "stem_axis": [float(slope), float(intercept)],
            "top": top, "bottom": bottom,
            "hook_width": 1+.70*signed("hook_width"), "foot_width": 1+.72*signed("foot_width"),
            "hook_angle": .32*signed("hook_angle"), "foot_angle": .22*signed("foot_angle"),
            "stem_bow_xheight": .016*signed("stem_bow"), "cubic_span_xheight": .13}


def _deform_one(glyph, parameters):
    result = copy.deepcopy(glyph)
    top, bottom = parameters["top"], parameters["bottom"]
    height = max(bottom-top, .1)
    slope, intercept = parameters["stem_axis"]
    def transform(x, y):
        u = (y-top)/height
        hook, foot = math.exp(-(u/.24)**2), math.exp(-((1-u)/.21)**2)
        offset = x-(slope*y+intercept)
        new_x = x+offset*((parameters["hook_width"]-1)*hook+(parameters["foot_width"]-1)*foot)
        new_x += parameters["stem_bow_xheight"]*math.sin(math.pi*u)
        new_y = y+offset*(parameters["hook_angle"]*hook+parameters["foot_angle"]*foot)
        return round(new_x, 9), round(new_y, 9)
    result["strokes"] = _mapped_strokes(_subdivide(glyph["strokes"]), transform)
    bounds = geometry_bounds(result["strokes"])
    result["ink_bounds"] = [bounds[0]-1e-8, bounds[1]-1e-8, bounds[2]+1e-8, bounds[3]+1e-8]
    old = geometry_bounds(glyph["strokes"])
    result["advance"] = max(.05, glyph["advance"]+bounds[2]-bounds[0]-(old[2]-old[0]))
    return result


def _deform(glyph, parameters):
    result = copy.deepcopy(glyph)
    def transform(x, y):
        for _ in range(parameters["composition_steps"]):
            dx = dy = 0.
            for field in parameters["fields"]:
                cx, cy = field["center"]
                weight = math.exp(-((x-cx)**2+(y-cy)**2)/field["radius"]**2)
                dx += weight*field["displacement"][0]*parameters["step_strength"]
                dy += weight*field["displacement"][1]*parameters["step_strength"]
            x, y = x+dx, y+dy
        return round(x, 9), round(y, 9)
    result["strokes"] = _mapped_strokes(_subdivide(glyph["strokes"]), transform)
    bounds = geometry_bounds(result["strokes"])
    result["ink_bounds"] = [bounds[0]-1e-8, bounds[1]-1e-8, bounds[2]+1e-8, bounds[3]+1e-8]
    # Preserve the recorded side bearing; the changed form receives its actual
    # width, without inventing a new measurement of writer letter spacing.
    old_bounds = geometry_bounds(glyph["strokes"])
    result["advance"] = max(.05, glyph["advance"]+(bounds[2]-bounds[0])-(old_bounds[2]-old_bounds[0]))
    return result


def glyph_instance(glyph, pen_width_mm=.28):
    """Represent profile-unit paths at 3 mm for the visible-ink shape audit."""
    return {"role": glyph["role"], "text": glyph["text"], "representation": glyph["representation"],
            "strokes": _mapped_strokes(glyph["strokes"], lambda x, y: (3*x, 3*y)),
            "pen_width_mm": pen_width_mm, "nominal_size_mm": 3.}


def _descriptor(glyph, pen_width_mm):
    from .word_patterns import structural_descriptor
    return structural_descriptor(glyph_instance(glyph, pen_width_mm))


def _distance(first, second):
    from .word_patterns import structural_distance
    return structural_distance(first, second)


def _topology_guard(original, candidate, original_topology):
    if original["representation"] != "clean_outline":
        return {"status": "ambient_field_guard", "stroke_count": len(candidate["strokes"]),
                "closed_strokes": sum(bool(stroke.get("closed")) for stroke in candidate["strokes"])}
    topology = outline_topology(candidate["strokes"], tolerance=.0008)
    if (topology["intersections"] or topology["parents"] != original_topology["parents"]
            or topology["holes"] != original_topology["holes"] or topology["components"] != original_topology["components"]):
        return None
    ratio = topology["ink_area"]/original_topology["ink_area"]
    if not .72 <= ratio <= 1.35:
        return None
    return {"status": "verified_outline_topology", "components": topology["components"], "holes": topology["holes"],
            "containment_parents": topology["parents"], "area_ratio": round(ratio, 7), "intersections": False}


def _one_jacobian_guard(glyph, parameters):
    """Keep inferred serif changes away from folds and pinched ink."""
    left, top, right, bottom = geometry_bounds(glyph["strokes"])
    height = max(bottom-top, .1)
    slope, intercept = parameters["stem_axis"]
    minimum, maximum = math.inf, 0.
    for y in np.linspace(top, bottom, 25):
        u = (y-top)/height
        hook, foot = math.exp(-(u/.24)**2), math.exp(-((1-u)/.21)**2)
        dh = -2*u/(.24**2*height)*hook
        df = 2*(1-u)/(.21**2*height)*foot
        width = (parameters["hook_width"]-1)*hook+(parameters["foot_width"]-1)*foot
        angle = parameters["hook_angle"]*hook+parameters["foot_angle"]*foot
        dw = (parameters["hook_width"]-1)*dh+(parameters["foot_width"]-1)*df
        da = parameters["hook_angle"]*dh+parameters["foot_angle"]*df
        bow = parameters["stem_bow_xheight"]*math.pi/height*math.cos(math.pi*u)
        for x in np.linspace(left, right, 17):
            offset = x-(slope*y+intercept)
            jacobian = np.array([[1+width, -slope*width+offset*dw+bow], [angle, 1-slope*angle+offset*da]])
            values = np.linalg.svd(jacobian, compute_uv=False)
            if np.linalg.det(jacobian) <= 0 or values.min() < .30 or values.max() > 2.3:
                return None
            minimum, maximum = min(minimum, values.min()), max(maximum, values.max())
    return {"minimum_sampled_singular_value": round(float(minimum), 7),
            "maximum_sampled_singular_value": round(float(maximum), 7), "grid": [17, 25]}


def _parents(profile, characters, role):
    groups = defaultdict(list)
    selected = set(characters) if characters is not None else None
    for glyph in accepted_glyphs(profile):
        if glyph.get("derivation") or glyph["role"] == "mark.correction":
            continue
        if (selected is not None and glyph["text"] not in selected) or (role is not None and glyph["role"] != role):
            continue
        groups[(glyph["role"], glyph["text"], glyph["representation"])].append(glyph)
    # A copied or renamed geometry is not an additional parent opportunity.
    for key, values in groups.items():
        unique = {}
        for glyph in sorted(values, key=lambda value: value["glyph_id"]):
            unique.setdefault(geometry_family(glyph), glyph)
        groups[key] = list(unique.values())
    return groups


def synthesize_variants(profile: dict, characters: str | None = None, per_glyph: int = 6,
                        seed: int = 72, role: str | None = None) -> dict:
    """Return a profile containing reviewable writer-conditioned style families.

    ``per_glyph`` is the requested number per (role, text, representation), not
    per photograph. Source parents rotate through all distinct reviewed forms.
    Derived forms never become parents: repeated training cannot amplify its own
    guesses as if they were more examples of the writer's actual handwriting.
    """
    validate_profile(profile)
    if isinstance(per_glyph, bool) or not isinstance(per_glyph, int) or not 1 <= per_glyph <= 24:
        raise InkformError("invalid_synthesis", "per_glyph must be an integer from 1 to 24.")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise InkformError("invalid_synthesis", "The synthesis seed must be an integer.")
    if characters is not None and (not isinstance(characters, str) or not characters):
        raise InkformError("invalid_synthesis", "characters must be nonempty text when supplied.")
    if role is not None and (not isinstance(role, str) or role not in {"text", "math.variable", "math.digit", "math.operator", "math.function"}):
        raise InkformError("invalid_synthesis", "Choose an ordinary glyph role for style proposals.")
    result = copy.deepcopy(profile)
    groups = _parents(profile, characters, role)
    proposals, groups_report = [], []
    existing_ids = {glyph["glyph_id"] for glyph in result["glyphs"]}
    pen_width = profile["pen"]["width_mm"]
    for key, parents in sorted(groups.items()):
        role_name, text, representation = key
        row = {"role": role_name, "text": text, "representation": representation, "requested": per_glyph,
               "parent_glyph_ids": [g["glyph_id"] for g in parents], "proposed": 0, "rejections": {}}
        groups_report.append(row)
        if text in _FINITE_SHAPES:
            row["status"] = "limited_shape_space"
            continue
        # Keep accepted derived forms in the diversity check, while excluding
        # them as source data. This also makes a repeated identical job idempotent.
        existing = [g for g in accepted_glyphs(profile) if (g["role"], g["text"], g["representation"]) == key]
        descriptors = [_descriptor(g, pen_width) for g in existing]
        original_topologies = {}
        valid_parents = []
        for parent in parents:
            if representation == "clean_outline":
                topology = outline_topology(parent["strokes"], tolerance=.0008)
                if topology["intersections"] or topology["ink_area"] <= 0:
                    continue
                original_topologies[parent["glyph_id"]] = topology
            valid_parents.append(parent)
        if not valid_parents:
            row["status"] = "source_geometry_needs_review"
            continue
        recipes = list(_RECIPES)
        candidates = []
        rejected = Counter()
        # A bounded queue avoids an endless search for impossible uniqueness.
        # A few distinct source shapes receive all spatial recipes. Large pools
        # are distributed across the same bounded candidate budget.
        budget = min(144, max(36, per_glyph*12, len(valid_parents)*6))
        for attempt in range(budget):
            parent = valid_parents[attempt % len(valid_parents)]
            recipe = recipes[(attempt // len(valid_parents)) % len(recipes)]
            base, parents_used = parent, [parent]
            # Geometry interpolation is permitted only for corresponding
            # contours. It remains an estimate even though both endpoints are
            # observed. Outline guards below reject incompatible interpolation.
            if attempt >= len(recipes)*len(valid_parents) and len(valid_parents) > 1:
                peers = [p for p in valid_parents if p["glyph_id"] != parent["glyph_id"] and contour_topology(p) == contour_topology(parent)]
                if peers:
                    peer = peers[(attempt//len(valid_parents)) % len(peers)]
                    ratio = .25+.5*stable_value(seed, parent["glyph_id"], f"interpolation-{attempt}")
                    base, parents_used = interpolate_forms(parent, peer, ratio), [parent, peer]
            parameters = _one_parameters(base, seed, attempt) if text == "1" else _style_parameters(base, recipe, seed, attempt)
            if text == "1":
                recipe = "recorded_hook_and_foot"
            if len(parents_used) > 1:
                parameters["interpolation_ratio"] = ratio
            candidate = _deform_one(base, parameters) if text == "1" else _deform(base, parameters)
            old_box, new_box = geometry_bounds(parent["strokes"]), geometry_bounds(candidate["strokes"])
            width_ratio = (new_box[2]-new_box[0])/max(old_box[2]-old_box[0], .01)
            height_ratio = (new_box[3]-new_box[1])/max(old_box[3]-old_box[1], .01)
            # A bowl can be rebalanced, but an ascender must not collapse into
            # a different character. Broad affine size changes are neither the
            # purpose of these families nor proof of structural novelty.
            # Digit1 serif width intentionally varies more, with its measured
            # straight stem protected by the dedicated transformation below.
            width_limits = (.5, 1.65) if text == "1" else (.74, 1.35)
            if not width_limits[0] <= width_ratio <= width_limits[1] or not .82 <= height_ratio <= 1.22:
                rejected["writer_shape_envelope"] += 1
                continue
            parameters["source_extent_ratios"] = {"width": round(width_ratio, 7), "height": round(height_ratio, 7)}
            if text == "1":
                jacobian = _one_jacobian_guard(base, parameters)
                if jacobian is None:
                    rejected["stem_or_serif_geometry"] += 1
                    continue
                parameters["jacobian_review"] = jacobian
            guard = _topology_guard(parent, candidate, original_topologies.get(parent["glyph_id"]))
            if guard is None:
                rejected["topology_or_ink_area"] += 1
                continue
            descriptor = _descriptor(candidate, pen_width)
            distance = min((_distance(descriptor, other) for other in descriptors), default=math.inf)
            if distance < MIN_STRUCTURAL_DISTANCE_MM:
                rejected["same_structural_family"] += 1
                continue
            candidates.append((candidate, parents_used, recipe, parameters, guard, descriptor, distance))
        # Maximin selection covers the useful candidate shape space instead of
        # accepting the first six nearly identical variations of one recipe.
        selected = 0
        while candidates and selected < per_glyph:
            index = max(range(len(candidates)), key=lambda i: candidates[i][-1])
            candidate, parents_used, recipe, parameters, guard, descriptor, distance = candidates.pop(index)
            if distance < MIN_STRUCTURAL_DISTANCE_MM:
                rejected["near_existing_proposal"] += 1+len(candidates)
                break
            candidate.pop("derivation", None)
            candidate.pop("refinement", None)
            candidate.pop("extraction", None)
            candidate.pop("refinement_warning", None)
            candidate["variation_dimensions_observed"] = False
            digest = geometry_hash(candidate)
            identity = hashlib.sha256((SYNTHESIS_VERSION+"\0"+str(seed)+"\0"+"\0".join(p["glyph_id"] for p in parents_used)+"\0"+digest).encode()).hexdigest()[:24]
            candidate.update(glyph_id="derived-"+identity, geometry_status="proposed", label_status="proposed", reviewed_by=None,
                             geometry_sha256=digest, stroke_order_status="estimated")
            refs = {(ref["source_id"], ref.get("observation_id")): copy.deepcopy(ref) for parent in parents_used for ref in parent["source_refs"]}
            candidate["source_refs"] = list(refs.values())
            candidate["derivation"] = {"version": SYNTHESIS_VERSION, "basis": "writer_conditioned_estimate",
                "parent_glyph_ids": [parent["glyph_id"] for parent in parents_used],
                "parent_geometry_sha256": [geometry_hash(parent) for parent in parents_used],
                "seed": seed, "recipe": recipe, "parameters": parameters, "geometry_sha256": digest,
                "structural_distance_mm": round(distance, 7), "structural_threshold_mm": MIN_STRUCTURAL_DISTANCE_MM,
                "topology": guard, "independent_observation_count": 0, "review_status": "proposed", "reviewed_by": None}
            if len(parents_used) > 1:
                candidate["derivation"]["interpolation_ratio"] = parameters["interpolation_ratio"]
            if candidate["glyph_id"] not in existing_ids:
                existing_ids.add(candidate["glyph_id"])
                result["glyphs"].append(candidate)
            proposals.append({"glyph_id": candidate["glyph_id"], "role": role_name, "text": text, "representation": representation,
                              "geometry_sha256": digest, "parent_glyph_ids": candidate["derivation"]["parent_glyph_ids"],
                              "recipe": recipe, "structural_distance_mm": round(distance, 7),
                              "geometry_status": "proposed", "decision": None})
            selected += 1
            descriptors.append(descriptor)
            candidates = [(*value[:-1], min(value[-1], _distance(value[-2], descriptor))) for value in candidates]
        row.update(proposed=selected, status="review_required" if selected == per_glyph else "bounded_diversity_limit",
                   rejections=dict(rejected))
    validate_profile(result)
    return {"version": SYNTHESIS_VERSION, "profile": result, "proposals": proposals,
            "summary": {"proposed_count": len(proposals), "groups": groups_report,
                        "basis": "writer_conditioned_estimate", "independent_observations_added": 0,
                        "note": "Generated style families require review of the exact vector geometry; they are not additional observed handwriting."}}


def approve_variants(profile: dict, decisions: list[dict], reviewed_by: str) -> dict:
    """Apply exact-geometry review decisions without relabelling guesses as data."""
    validate_profile(profile)
    if not reviewed_actor(reviewed_by):
        raise InkformError("invalid_synthesis_review", "A style-family review must identify its review actor.")
    if not isinstance(decisions, list) or not decisions or any(not isinstance(decision, dict) for decision in decisions):
        raise InkformError("invalid_synthesis_review", "Style-family decisions must be a nonempty list of review objects.")
    result = copy.deepcopy(profile)
    glyphs = {glyph["glyph_id"]: glyph for glyph in result["glyphs"]}
    seen = set()
    for decision in decisions:
        identity = decision.get("glyph_id")
        if not isinstance(identity, str):
            raise InkformError("invalid_synthesis_review", "A style-family review must name a glyph ID.")
        glyph = glyphs.get(identity)
        if not glyph or not glyph.get("derivation") or identity in seen:
            raise InkformError("invalid_synthesis_review", "Each review must name one existing derived glyph exactly once.", {"glyph_id": identity})
        seen.add(identity)
        derivation = glyph["derivation"]
        digest = geometry_hash(glyph)
        if decision.get("geometry_sha256") != digest or derivation.get("geometry_sha256") != digest:
            raise InkformError("invalid_synthesis_review", "The decision must match the exact generated vector geometry.", {"glyph_id": identity})
        for parent_id, parent_hash in zip(derivation["parent_glyph_ids"], derivation["parent_geometry_sha256"]):
            parent = glyphs.get(parent_id)
            if not parent or parent.get("derivation") or parent not in accepted_glyphs(result) or geometry_hash(parent) != parent_hash:
                raise InkformError("invalid_synthesis_review", "A derived form must retain its exact reviewed source parents.", {"glyph_id": identity, "parent_glyph_id": parent_id})
        status = decision.get("decision")
        if not isinstance(status, str) or status not in {"reviewed", "rejected"}:
            raise InkformError("invalid_synthesis_review", "Choose reviewed or rejected for a style proposal.")
        glyph.update(label_status=status, geometry_status=status, reviewed_by=reviewed_by)
        derivation.update(review_status=status, reviewed_by=reviewed_by)
    return validate_profile(result)


def _svg_paths(glyph):
    paths = []
    for stroke in glyph["strokes"]:
        x, y = stroke["start"]
        pieces = [f"M{x:.6f},{y:.6f}"]
        for segment in stroke["segments"]:
            if segment["type"] == "line":
                pieces.append("L"+",".join(f"{value:.6f}" for value in segment["end"]))
            else:
                pieces.append("C"+" ".join(",".join(f"{value:.6f}" for value in segment[key]) for key in ("control1", "control2", "end")))
        if stroke.get("closed"):
            pieces.append("Z")
        paths.append(" ".join(pieces))
    return " ".join(paths)


def write_synthesis_review(result: dict, destination: Path) -> dict:
    """Write private, vector review sheets and hash-bound decision templates."""
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    profiles = result["profile"]
    glyphs = {glyph["glyph_id"]: glyph for glyph in profiles["glyphs"]}
    files = {"profile": destination/"proposal-profile.json", "review": destination/"review.json", "summary": destination/"synthesis.json"}
    for name, payload in (("profile", profiles), ("review", {"version": SYNTHESIS_VERSION, "reviewed_by": None, "decisions": result["proposals"]}),
                          ("summary", {key: value for key, value in result.items() if key != "profile"})):
        files[name].write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False)+"\n", encoding="utf-8")
        files[name].chmod(0o600)
    sheets = []
    for offset in range(0, len(result["proposals"]), 24):
        rows = result["proposals"][offset:offset+24]
        height = 82+len(rows)*104
        svg = [f'<svg xmlns="http://www.w3.org/2000/svg" width="1100" height="{height}" viewBox="0 0 1100 {height}">',
               '<rect width="100%" height="100%" fill="white"/>',
               '<g font-family="sans-serif" fill="#1d2632"><text x="24" y="30" font-size="22">Inkform: source and inferred style review</text>',
               '<text x="24" y="55" font-size="13">Left: recorded parent(s). Right: generated estimate. Review identity, counters, endpoints and writer style.</text></g>']
        for index, record in enumerate(rows):
            glyph = glyphs[record["glyph_id"]]
            y = 82+index*104
            svg.extend([f'<path d="M20,{y+96} H1080" stroke="#dde2e9"/>',
                        f'<text x="24" y="{y+19}" font-family="sans-serif" font-size="15">{html.escape(glyph["role"]+" "+glyph["text"])}</text>',
                        f'<text x="24" y="{y+42}" font-family="monospace" font-size="11">{html.escape(glyph["glyph_id"])}</text>',
                        f'<text x="24" y="{y+65}" font-family="sans-serif" font-size="12">{html.escape(record["recipe"])}; structural distance {record["structural_distance_mm"]:.3f} mm</text>'])
            displayed = [glyphs[identity] for identity in glyph["derivation"]["parent_glyph_ids"]]+[glyph]
            for column, item in enumerate(displayed):
                x = (440+column*130) if column < len(displayed)-1 else 835
                left, top, right, bottom = item["ink_bounds"]
                scale = min(60/max(bottom-top, .2), 100/max(right-left, .2), 65)
                tx, ty = x-left*scale, y+80-bottom*scale
                paint = 'fill="#202937" fill-rule="evenodd"' if item["representation"] == "clean_outline" else f'fill="none" stroke="#202937" stroke-width="{profiles["pen"]["width_mm"]/3}" stroke-linecap="round" stroke-linejoin="round"'
                svg.append(f'<path d="{_svg_paths(item)}" transform="translate({tx},{ty}) scale({scale})" {paint}/>')
        svg.append("</svg>")
        path = destination/f"review-{offset//24+1:03d}.svg"
        path.write_text("\n".join(svg)+"\n", encoding="utf-8")
        path.chmod(0o600)
        sheets.append(str(path))
    pdf, previews = _write_pdf_review(result, destination)
    return {**{key: str(value) for key, value in files.items()}, "review_sheets": sheets,
            "review_pdf": str(pdf), "review_previews": previews}


def _write_pdf_review(result, destination):
    """Paginate the complete atlas; OS SVG thumbnails can crop tall sheets."""
    from reportlab.lib.colors import HexColor
    from reportlab.pdfgen import canvas
    import pypdfium2 as pdfium

    from .rendering import PT, draw_instance

    profile = result["profile"]
    glyphs = {glyph["glyph_id"]: glyph for glyph in profile["glyphs"]}
    pdf = destination/"review.pdf"
    width, height = 1100, 940
    page = canvas.Canvas(str(pdf), pagesize=(width, height), invariant=1, pageCompression=1)
    page.setTitle("Inkform source and inferred style review")
    rows = result["proposals"]
    for offset in range(0, max(1, len(rows)), 8):
        page.setFillColor(HexColor("#1d2632"))
        page.setFont("Helvetica-Bold", 22)
        page.drawString(24, height-30, "Inkform: source and inferred style review")
        page.setFont("Helvetica", 13)
        page.drawString(24, height-55, "Left: recorded parent(s). Right: generated estimate. Review identity, counters, endpoints and writer style.")
        page.setFont("Helvetica", 10)
        page.drawRightString(width-24, height-76, f"Candidates {offset+1}-{min(offset+8, len(rows))} of {len(rows)}")
        for index, record in enumerate(rows[offset:offset+8]):
            glyph = glyphs[record["glyph_id"]]
            y = 94+index*104
            page.setStrokeColor(HexColor("#dde2e9"))
            page.setLineWidth(.7)
            page.line(20, height-y-96, width-20, height-y-96)
            page.setFillColor(HexColor("#1d2632"))
            page.setFont("Helvetica-Bold", 15)
            label = (glyph["role"]+" "+glyph["text"]).encode("ascii", "backslashreplace").decode()
            page.drawString(24, height-y-19, label)
            page.setFont("Courier", 11)
            page.drawString(24, height-y-42, glyph["glyph_id"])
            page.setFont("Helvetica", 12)
            page.drawString(24, height-y-65, f"{record['recipe']}; structural distance {record['structural_distance_mm']:.3f} mm")
            displayed = [glyphs[identity] for identity in glyph["derivation"]["parent_glyph_ids"]]+[glyph]
            for column, item in enumerate(displayed):
                x = (440+column*130) if column < len(displayed)-1 else 835
                left, top, right, bottom = item["ink_bounds"]
                scale = min(60/max(bottom-top, .2), 100/max(right-left, .2), 65)
                tx, ty = x-left*scale, y+80-bottom*scale
                instance = {"representation": item["representation"],
                            "pen_width_mm": profile["pen"]["width_mm"]/3*scale/PT,
                            "strokes": _mapped_strokes(item["strokes"], lambda px, py: ((tx+px*scale)/PT, (ty+py*scale)/PT))}
                page.setStrokeColor(HexColor("#202937"))
                page.setFillColor(HexColor("#202937"))
                draw_instance(page, instance, height/PT)
        page.showPage()
    page.save()
    pdf.chmod(0o600)
    previews = []
    with pdfium.PdfDocument(pdf) as document:
        for index in range(len(document)):
            path = destination/f"review-page-{index+1:03d}.png"
            current = document[index]
            try:
                bitmap = current.render(scale=1.5)
                try:
                    bitmap.to_pil().save(path)
                finally:
                    bitmap.close()
            finally:
                current.close()
            path.chmod(0o600)
            previews.append(str(path))
    return pdf, previews
