"""Seeded, bounded variation of whole forms, never independent point noise.

Reviewed repetitions determine width and height spread when available. Other
axes remain explicitly estimated: a photograph does not reveal pen dynamics.
The same smooth field transforms every contour and control in a form, including
its holes. Positive scales and small shears retain the form's orientation.
"""
from __future__ import annotations

import hashlib
import math


def stable_value(seed, token_id, domain):
    digest = hashlib.sha256(f"{seed}\0{token_id}\0{domain}".encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


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

    def factors(self, glyph, token_id, word_id, position=0.5):
        role, text = glyph["role"], glyph["text"]
        math_form = role.startswith("math.")
        entry = self.by_glyph.get((role, text))
        # Fallback spreads deliberately fit existing single-exemplar profiles.
        width_amplitude = min(0.15, self.strength * (entry["width"] if entry else (0.085 if math_form else 0.065)))
        height_amplitude = min(0.12, self.strength * (entry["height"] if entry else (0.035 if math_form else 0.045)))
        slant_amplitude = min(0.08, self.strength * (0.018 if math_form else 0.045))
        baseline_amplitude = min(0.075, self.strength * (0.006 if math_form else 0.040))
        warp_amplitude = min(0.035, self.strength * (0.007 if math_form else 0.020))
        # Word-level rhythm plus independent occurrence variation prevents nnn
        # from becoming three copies, while preserving a coherent word slant.
        blend = lambda domain: 0.28 * self.signed(word_id, "word-" + domain) + 0.72 * self.signed(token_id, "occurrence-" + domain)
        width = 1 + width_amplitude * blend("width")
        height = 1 + height_amplitude * blend("height")
        shear = slant_amplitude * (0.55 * self.signed(word_id, "word-slant") + 0.45 * self.signed(token_id, "occurrence-slant"))
        baseline = baseline_amplitude * (0.25 * self.signed(word_id, "word-baseline") + 0.5 * self.signed(token_id, "occurrence-baseline") + 0.5 * (position - 0.5) * self.signed(word_id, "word-drift"))
        bow = warp_amplitude * self.signed(token_id, "shape-bow")
        bend = warp_amplitude * self.signed(token_id, "shape-bend")
        return {
            "version": "inkform-occurrence-1", "amount": self.amount,
            "basis": "reviewed_samples" if entry else "conservative_estimate",
            "estimated_axes": ["slant", "baseline", "shape_warp"] if entry else ["width", "height", "slant", "baseline", "shape_warp"],
            "width": width, "height": height, "shear": shear,
            "baseline_x_height": baseline, "bow_x_height": bow, "bend_x_height": bend,
            "sample_glyph_ids": list(entry.get("glyph_ids", [])) if entry else [],
            "sample_observation_ids": list(entry.get("observation_ids", [])) if entry else [],
        }

    @staticmethod
    def transform(glyph, factors, size):
        left, top, right, bottom = glyph["ink_bounds"]
        cx, cy = (left + right) / 2, (top + bottom) / 2
        # Spans >= 1 keep the warp derivative bounded even for tiny dots and
        # very thin outline holes; no point receives an independent random kick.
        sx, sy = max(1.0, right - left), max(1.0, bottom - top)

        def transform(x, y):
            wx = x + factors["bow_x_height"] * math.tanh((y - cy) / sy)
            wy = y + factors["bend_x_height"] * math.tanh((x - cx) / sx)
            return (size * (factors["width"] * wx - factors["shear"] * wy),
                    size * (factors["height"] * wy + factors["baseline_x_height"]))
        return transform

    def word_gap(self, token_id):
        observed = self.learned.get("word_gap") if self.learned.get("word_gap_sample_count", 0) > 0 else None
        amplitude = min(0.25, self.strength * (observed if observed is not None else 0.10))
        return 1 + amplitude * self.signed(token_id, "word-gap")

    def letter_gap(self, token_id):
        return 1 + min(0.45, self.strength * 0.20) * self.signed(token_id, "letter-gap")
