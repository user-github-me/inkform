"""Seeded variation of reviewed forms without independent point noise.

Whole-contour fields preserve shared boundaries and counters. Distinct reviewed
forms can be blended only when their contour topology agrees. That interpolation
is sample backed; the small residual shape field is explicitly an estimate.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math


def stable_value(seed, token_id, domain):
    digest = hashlib.sha256(f"{seed}\0{token_id}\0{domain}".encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


def source_evidence(glyph, observations=None):
    """Atomic source identities, so overlapping provenance is not new data."""
    observations = observations or {}
    evidence = set()
    for ref in glyph.get("source_refs", []):
        observation = observations.get(ref.get("observation_id"), {})
        if observation.get("mask_sha256"):
            evidence.add("mask:"+observation["mask_sha256"])
        else:
            evidence.add("observation:"+json.dumps([ref.get("source_id"), ref.get("observation_id")]))
    return evidence or {"geometry:"+geometry_family(glyph)}


def source_family(glyph, observations=None):
    """Aliases/refinements of one recorded occurrence are one exemplar family."""
    return hashlib.sha256(json.dumps(sorted(source_evidence(glyph, observations))).encode()).hexdigest()


def geometry_family(glyph):
    # Canonical segment inventory also recognizes translated, reordered and
    # reversed copies. Renaming or retracing a path does not create evidence.
    segments = []
    for stroke in glyph["strokes"]:
        start = current = stroke["start"]
        if not stroke["segments"]:
            segments.append((current,))
        for segment in stroke["segments"]:
            end = segment["end"]
            segments.append((current, end) if segment["type"] == "line" else
                            (current, segment["control1"], segment["control2"], end))
            current = end
        if stroke.get("closed") and current != start:
            segments.append((current, start))
    points = [point for segment in segments for point in segment]
    left, top = min(p[0] for p in points), min(p[1] for p in points)
    paths = []
    for segment in segments:
        path = tuple((round(x-left, 6), round(y-top, 6)) for x,y in segment)
        paths.append(min(path, path[::-1]))
    return hashlib.sha256(json.dumps(sorted(paths), separators=(",", ":")).encode()).hexdigest()


def contour_topology(glyph):
    return tuple((bool(s.get("closed")), tuple(seg["type"] for seg in s["segments"])) for s in glyph["strokes"])


def interpolate_forms(first, second, ratio):
    """Blend corresponding reviewed contours, never pair incompatible outlines."""
    if contour_topology(first) != contour_topology(second):
        return copy.deepcopy(first)
    result = copy.deepcopy(first)
    for target, other in zip(result["strokes"], second["strokes"]):
        target["start"] = [(1-ratio)*a + ratio*b for a, b in zip(target["start"], other["start"])]
        for segment, peer in zip(target["segments"], other["segments"]):
            for key in ("end", "control1", "control2"):
                if key in segment:
                    segment[key] = [(1-ratio)*a + ratio*b for a, b in zip(segment[key], peer[key])]
    result["advance"] = (1-ratio)*first["advance"] + ratio*second["advance"]
    result["ink_bounds"] = [(1-ratio)*a + ratio*b for a, b in zip(first["ink_bounds"], second["ink_bounds"])]
    return result


def curved_strokes(strokes):
    """Promote straight spans before a smooth field so a shaft can really bend."""
    result = copy.deepcopy(strokes)
    for stroke in result:
        start = stroke["start"]
        for segment in stroke["segments"]:
            if segment["type"] == "line":
                end = segment["end"]
                segment.update(type="cubic", control1=[a+(b-a)/3 for a,b in zip(start,end)],
                               control2=[a+2*(b-a)/3 for a,b in zip(start,end)])
            start = segment["end"]
    return result


class OccurrenceVariation:
    def __init__(self, profile, seed):
        self.seed = seed
        settings = profile.get("variation", {})
        self.amount = float(settings.get("amount", 0.1))
        self.strength = max(0.0, min(self.amount / 0.1, 3.0))
        self.learned = settings.get("learned", {})
        self.by_glyph = {(entry["role"], entry["text"]): entry for entry in self.learned.get("by_glyph", [])}

    def signed(self, identity, domain):
        return 2 * stable_value(self.seed, identity, domain) - 1

    def factors(self, glyph, token_id, word_id, position=0.5, *, attempt=0, independent_forms=1):
        role, text = glyph["role"], glyph["text"]
        math_form = role.startswith("math.")
        entry = self.by_glyph.get((role, text))
        identity = token_id if attempt == 0 else f"{token_id}\0candidate-{attempt}"
        width_amplitude = min(0.18, self.strength * (entry["width"] if entry else (0.11 if math_form else 0.085)))
        height_amplitude = min(0.14, self.strength * (entry["height"] if entry else (0.05 if math_form else 0.065)))
        slant_amplitude = min(0.10, self.strength * (0.028 if math_form else 0.055))
        baseline_amplitude = min(0.075, self.strength * (0.009 if math_form else 0.040))
        # More observations supply real candidate forms; they do not justify
        # arbitrary extra mess. Residual deformation remains equally bounded.
        warp_amplitude = min(0.080, self.strength * (0.044 if math_form else 0.058))
        blend = lambda domain: 0.20 * self.signed(word_id, "word-" + domain) + 0.80 * self.signed(identity, "occurrence-" + domain)
        width = 1 + width_amplitude * blend("width")
        height = 1 + height_amplitude * blend("height")
        shear = slant_amplitude * (0.35 * self.signed(word_id, "word-slant") + 0.65 * self.signed(identity, "occurrence-slant"))
        baseline = baseline_amplitude * (0.25 * self.signed(word_id, "word-baseline") + 0.5 * self.signed(identity, "occurrence-baseline") + 0.5 * (position - 0.5) * self.signed(word_id, "word-drift"))
        # Six low-frequency modes change arches, bowls, entry/exit slopes and
        # stroke bend. Shared fields avoid noisy edges and disconnected holes.
        field = [warp_amplitude * self.signed(identity, f"shape-field-{i}") for i in range(6)]
        arrow = role == "math.operator" and text in {"→", "←", "↔", "⇒", "⇐", "⇔", "↑", "↓"}
        arrow_shape = {key: 1 + min(limit, self.strength * amount) * self.signed(identity, "arrow-"+key)
                       for key, limit, amount in (("head_length", .24, .15), ("upper_opening", .25, .18), ("lower_opening", .25, .18))} if arrow else {}
        pen_width = 1 + min(.04, self.strength*.025)*self.signed(identity, "pen-width") if glyph["representation"] == "centerline" else 1.
        return {
            "version": "inkform-occurrence-2", "amount": self.amount,
            "basis": "reviewed_samples" if entry else "conservative_estimate",
            "estimated_axes": ["slant", "baseline", "shape_warp"] if entry else ["width", "height", "slant", "baseline", "shape_warp"],
            "width": width, "height": height, "shear": shear, "pen_width": pen_width,
            "pen_width_basis": "bounded_estimate" if glyph["representation"] == "centerline" else "reviewed_outline",
            "baseline_x_height": baseline, "bow_x_height": field[0], "bend_x_height": field[3],
            "shape_field_x_height": field, "arrow_shape": arrow_shape,
            "independent_form_count": independent_forms, "candidate_attempt": attempt,
            "sample_glyph_ids": list(entry.get("glyph_ids", [])) if entry else [],
            "sample_observation_ids": list(entry.get("observation_ids", [])) if entry else [],
        }

    @staticmethod
    def transform(glyph, factors, size):
        left, top, right, bottom = glyph["ink_bounds"]
        cx, cy = (left + right) / 2, (top + bottom) / 2
        # A common, bounded spatial scale avoids huge derivatives in thin
        # strokes. These field amplitudes keep the Jacobian orientation positive.
        span = max(0.8, right - left, bottom - top)
        field = factors.get("shape_field_x_height", [factors["bow_x_height"], 0, 0, factors["bend_x_height"], 0, 0])
        # Bound each row of the residual Jacobian by 0.42. I + D(field)
        # therefore remains orientation preserving even at maximum amount.
        derivative = max(abs(field[0])+2*abs(field[1])+2*abs(field[2]),
                         abs(field[3])+2*abs(field[4])+2*abs(field[5]))*math.pi/span
        gain = min(1., .42/max(derivative, 1e-12))
        field = [value*gain for value in field]
        arrow = factors.get("arrow_shape", {})

        def transform(x, y):
            px, py = x, y
            if arrow and glyph["text"] in {"→", "←", "⇒", "⇐"}:
                forward = glyph["text"] in {"→", "⇒"}
                u = ((x-left) if forward else (right-x)) / max(right-left, 0.01)
                # Blend the head transform into the shaft. The tip stays put;
                # asymmetric opening can change without reversing direction.
                weight = max(0.0, min(1.0, (u-.38)/.28))
                weight = weight*weight*(3-2*weight)
                tip = right if forward else left
                px += (x-tip)*(arrow["head_length"]-1)*weight
                opening = arrow["upper_opening"] if y < cy else arrow["lower_opening"]
                py += (y-cy)*(opening-1)*weight
            u, v = (px-cx)/span, (py-cy)/span
            pu, pv = math.pi*u, math.pi*v
            dx = field[0]*math.sin(pv) + field[1]*math.sin(pu)*math.cos(pv) + field[2]*math.cos(pu)*math.cos(pv)
            dy = field[3]*math.sin(pu) + field[4]*math.sin(pv)*math.cos(pu) + field[5]*math.cos(pu)*math.cos(pv)
            wx, wy = px+dx, py+dy
            return (size * (factors["width"] * wx - factors["shear"] * wy),
                    size * (factors["height"] * wy + factors["baseline_x_height"]))
        return transform

    def word_gap(self, token_id):
        observed = self.learned.get("word_gap") if self.learned.get("word_gap_sample_count", 0) > 0 else None
        amplitude = min(0.25, self.strength * (observed if observed is not None else 0.10))
        return 1 + amplitude * self.signed(token_id, "word-gap")

    def letter_gap(self, token_id):
        return 1 + min(0.45, self.strength * 0.20) * self.signed(token_id, "letter-gap")
