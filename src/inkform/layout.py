"""Physical, role-aware layout from writer paths, with stable seeded variants."""
from __future__ import annotations

import copy
import hashlib
import json
import math
from collections import Counter
from dataclasses import dataclass, field

import regex

from .document import (clusters, requirements, validate_document, visible_requirements,
                       math_text, math_restart_fragments)
from .errors import InkformError
from .profiles import accepted_glyphs, coverage, geometry_bounds, resolve_render_mode, validate_profile
from .measurements import spacing_metrics
from .ink_clearance import InkClearance, GRID_MM, RASTER_UNCERTAINTY_MM
from .variation import (OccurrenceVariation, stable_value, source_family, source_evidence, geometry_family,
                        contour_topology, interpolate_forms, curved_strokes)
from .repetition import shape_descriptor, assess_pair, audit_repetition, NEAR_THRESHOLD_MM
from .word_patterns import (audit_word_patterns, word_descriptor, compare_words,
                            structural_descriptor, structural_distance, STRUCTURAL_THRESHOLD_MM)


def map_strokes(strokes, transform):
    result = copy.deepcopy(strokes)
    for stroke in result:
        stroke["start"] = list(transform(*stroke["start"]))
        for segment in stroke["segments"]:
            for key in ("end", "control1", "control2"):
                if key in segment:
                    segment[key] = list(transform(*segment[key]))
    return result


def item_bounds(item):
    bounds = geometry_bounds(item["strokes"])
    pad = item["pen_width_mm"] * max((p["multiplier"] for s in item["strokes"] for p in s.get("width_profile", [])), default=1) / 2 if item["representation"] == "centerline" else 0
    return [bounds[0] - pad, bounds[1] - pad, bounds[2] + pad, bounds[3] + pad]


@dataclass
class Box:
    advance: float = 0
    items: list = field(default_factory=list)

    @property
    def bounds(self):
        if not self.items:
            return [0, 0, self.advance, 0]
        bs = [item_bounds(item) for item in self.items]
        return [min(b[0] for b in bs), min(b[1] for b in bs), max(b[2] for b in bs), max(b[3] for b in bs)]

    def shifted(self, x=0, y=0):
        result = copy.deepcopy(self)
        for item in result.items:
            item["strokes"] = map_strokes(item["strokes"], lambda px, py: (px + x, py + y))
            if "graph_plot_anchors_mm" in item:
                item["graph_plot_anchors_mm"] = [[px+x, py+y] for px, py in item["graph_plot_anchors_mm"]]
            if "body_ink_bounds_mm" in item:
                left, top, right, bottom = item["body_ink_bounds_mm"]
                item["body_ink_bounds_mm"] = [left+x, top+y, right+x, bottom+y]
        return result


def join(boxes, gap=0):
    result = Box()
    for box in boxes:
        if result.items and box.items:
            result.advance = max(result.advance, result.bounds[2] + gap - box.bounds[0])
        result.items.extend(box.shifted(result.advance).items)
        result.advance += box.advance + gap
    if boxes:
        result.advance -= gap
    return result


def join_ink(left, right, gap):
    """Translate intact boxes to a visible clearance, independent of bearings.

    Isolated-source advances include crop padding, not observed letter/word
    spacing. Measure the complete emitted ink (including caps and overhang)
    so that this whitespace cannot be consumed by a terminal stroke.
    """
    if not left.items or not right.items:
        return join([left, right], gap)
    offset = left.bounds[2] + gap - right.bounds[0]
    return Box(offset + right.advance, left.items + right.shifted(offset).items)


class Shaper:
    def __init__(self, profile, seed, mode, size, nonces=None, page_hints=None):
        self.profile, self.seed, self.mode, self.size = profile, seed, mode, size
        self.forms, self.variant_history, self.variant_choices = {}, {}, {}
        self.primitive_forms = {}
        self.glyph_context = None
        self.history, self.word_history, self.current_page, self.nonces = {}, {}, 0, dict(nonces or {})
        self.used_families = {}
        self.observed_source_uses, self.observed_source_recency = Counter(), {}
        self.observed_sequence = 0
        self.excluded_derived_count = 0
        self.word_targets = {}
        self.page_hints = page_hints or {}
        self.observations = {entry["observation_id"]: entry for entry in profile.get("observations", [])}
        self.variation = OccurrenceVariation(profile, seed)
        self.spacing = spacing_metrics(profile)
        self.ink_clearance = InkClearance()
        self.corrections = []
        representation = "centerline" if mode == "fresh_strokes" else "clean_outline"
        for glyph in accepted_glyphs(profile):
            if (glyph["representation"] == "centerline" and glyph["role"] == "math.operator"
                    and glyph["text"] in {"−", "√"}
                    and not (self.variation.observed_only and glyph.get("derivation"))):
                self.primitive_forms.setdefault((glyph["role"], glyph["text"]), []).append(glyph)
            if glyph["representation"] == representation or glyph["role"] == "mark.correction":
                if self.variation.observed_only and glyph.get("derivation"):
                    self.excluded_derived_count += 1
                    continue
                self.forms.setdefault((glyph["role"], glyph["text"]), []).append(glyph)
        for key, forms in self.forms.items():
            # One crop renamed or smoothed three times is still one example.
            families, shapes, claimed_evidence, derived = {}, set(), {}, []
            ordered = sorted(forms, key=lambda form: form["glyph_id"], reverse=True)
            from .variant_context import source_context
            context_sources = {source["source_id"]:source for source in profile.get("sources", [])}
            contexts = {form["glyph_id"]:source_context(form,self.observations,context_sources) for form in forms}
            ordered.sort(key=lambda form: contexts[form["glyph_id"]] is None)
            # Prefer individual observed occurrences over a derivative claiming
            # several of them. A+B is not a third independent sample beside A/B.
            ordered.sort(key=lambda form: len(source_evidence(form, self.observations)))
            for form in ordered:
                calibration = json.dumps(form.get("outline_calibration", {}), sort_keys=True)
                context = contexts[form["glyph_id"]]
                context_key = (context["kind"],context.get("normalization")) if context else None
                family, shape = (source_family(form, self.observations), calibration), (geometry_family(form), calibration, context_key)
                evidence = source_evidence(form, self.observations)
                # An approved synthesis is a usable shape, never a new source
                # observation. Keep its geometry bank beside the independent
                # source pool instead of dropping it as a provenance alias.
                if form.get("derivation"):
                    if shape not in shapes:
                        derived.append(form)
                        shapes.add(shape)
                    continue
                claimed = claimed_evidence.setdefault(calibration, set())
                if family not in families and shape not in shapes and not evidence.intersection(claimed):
                    families[family] = form
                    shapes.add(shape)
                    claimed.update(evidence)
            self.forms[key] = sorted([*families.values(), *derived], key=lambda form: form["glyph_id"])
        from .variant_context import VariantContext
        for key,forms in self.primitive_forms.items():
            seen_families,seen_shapes,unique=set(),set(),[]
            for form in sorted(forms,key=lambda value:value["glyph_id"],reverse=True):
                family,shape=source_family(form,self.observations),geometry_family(form)
                if family not in seen_families and shape not in seen_shapes:
                    unique.append(form); seen_families.add(family); seen_shapes.add(shape)
            self.primitive_forms[key]=sorted(unique,key=lambda value:value["glyph_id"])
        self.variant_context = VariantContext(self.forms, self.observations, profile.get("sources", []))

    def commit_page(self, page, items):
        """Correct provisional occurrence pages after physical line placement."""
        tokens = {item["token_id"] for item in items}
        self.current_page = page
        for key in list(self.history):
            records = self.history[key]
            for record in records:
                if record["token_id"] in tokens:
                    record["page"] = page
            self.history[key] = [record for record in records if record["page"] >= page-1]
            if not self.history[key]:
                del self.history[key]
        for text in list(self.word_history):
            for record in self.word_history[text]:
                if any(token in tokens for token in record["token_ids"]):
                    record["page"] = page
            self.word_history[text] = [record for record in self.word_history[text] if record["page"] >= page-1]
            if not self.word_history[text]:
                del self.word_history[text]

    @staticmethod
    def compatible(first, second):
        # A derived bank entry is already an explicitly reviewed construction.
        # Blending it back toward a parent can erase the very local differences
        # that its synthesis/review established. Ordinary occurrence variation
        # still applies, with its separate measured ink audit.
        if first.get("derivation") or second.get("derivation"):
            return False
        # Outlines need an explicitly reviewed point correspondence before
        # morphing. Matching contour counts alone cannot protect small counters.
        if first["representation"] != "centerline" or contour_topology(first) != contour_topology(second):
            return False
        # A coincident segment count does not establish matching stroke order.
        # Only small corresponding displacements permit source interpolation.
        deltas = []
        for stroke, peer in zip(first["strokes"], second["strokes"]):
            pairs = [(stroke["start"], peer["start"])]
            pairs += [(segment[key], other[key]) for segment, other in zip(stroke["segments"], peer["segments"])
                      for key in ("end", "control1", "control2") if key in segment]
            deltas.extend(math.hypot(a[0]-b[0], a[1]-b[1]) for a,b in pairs)
        return max(deltas, default=0) < .42 and sum(deltas)/max(1,len(deltas)) < .24

    def glyph(self, text, role, token_id, size=None, word_id=None, position=0.5, *, context=None, primitive=False):
        if token_id in self.page_hints:
            self.commit_page(self.page_hints[token_id], [])
        size = self.size if size is None else size
        forms = self.primitive_forms.get((role,text), []) if primitive else self.forms.get((role, text), [])
        if not forms:
            if primitive:
                raise InkformError("unsupported_outline_primitive", "Expandable mathematical marks need a reviewed centerline source alongside the body forms.",
                                   {"role":role,"text":text,"mode":self.mode})
            raise InkformError("missing_glyphs", "No reviewed geometry in the requested render mode.", {"missing_glyphs": [{"role": role, "text": text}], "mode": self.mode})
        if role == "mark.correction":
            # A reviewed pen mark is independent of the body representation.
            # Stretching a filled outline would also stretch its pen thickness.
            forms = [form for form in forms if form["representation"] == "centerline"]
            if not forms:
                raise InkformError("missing_correction_mark", "Cross-outs require a reviewed centerline mark from the writer's samples.", {"role": role, "text": text})
        if self.mode == "clean_outline" and role != "mark.correction" and not primitive:
            compatible_size = [form for form in forms if "outline_calibration" in form and abs(size - form["outline_calibration"]["body_x_height_mm"]) <= 0.01]
            if not compatible_size:
                raise InkformError("outline_scale_incompatible", "Filled outlines need a reviewed variant at the requested writing or script size.", {"role": role, "text": text, "requested_size_mm": size, "calibrated_sizes_mm": [form.get("outline_calibration", {}).get("body_x_height_mm") for form in forms]})
            forms = [form for form in compatible_size if abs(self.profile["pen"]["width_mm"] - form["outline_calibration"].get("pen_width_mm", form["outline_calibration"].get("effective_width_mm", -1))) <= 0.01]
            if not forms:
                raise InkformError("outline_pen_incompatible", "Changing the pen model cannot change a filled outline's thickness; select a reviewed compatible variant.", {"role": role, "text": text, "requested_pen_width_mm": self.profile["pen"]["width_mm"]})
        requested_context = context or self.glyph_context
        forms,context_selection = self.variant_context.eligible(forms,text,role,requested_context)
        if self.variation.observed_only:
            return self.observed_glyph(forms, text, role, token_id, size, word_id or token_id, position, context_selection)
        # Ink identity crosses semantic roles: text x and mathematical x
        # still must not reuse the same visible form on neighboring pages.
        history_key = text
        choice_key = (role, text, size, requested_context, primitive)
        used = self.used_families.setdefault(choice_key, set())
        def selection_family(form):
            return "derived:"+geometry_family(form) if form.get("derivation") else source_family(form, self.observations)
        candidates = [form for form in forms if selection_family(form) not in used]
        if not candidates:
            used.clear()
            previous = self.variant_history.get(choice_key)
            candidates = [form for form in forms if form["glyph_id"] != previous] if len(forms) > 1 else list(forms)
        selection_id = token_id if not self.nonces.get(token_id) else f"{token_id}\0retry-{self.nonces[token_id]}"
        start = min(len(candidates)-1, int(stable_value(self.seed, selection_id, "variant")*len(candidates)))
        candidates = candidates[start:] + candidates[:start]
        targets = self.word_targets.get(token_id, [])
        if targets:
            selected_ids = {form["glyph_id"] for form in candidates}
            candidates += [form for form in forms if form["glyph_id"] not in selected_ids]
        word_id = word_id or token_id
        records = [record for record in self.history.get(history_key, []) if record["token_id"] != token_id]
        best, best_distance, best_score = None, -1, None
        # A finite maximin search keeps every attempt inside the approved style
        # envelope. The final placed-page audit decides whether it is enough.
        attempts = 1 if self.variation.amount == 0 or not (records or targets) else 48
        for trial in range(attempts):
            source = candidates[trial % len(candidates)]
            attempt = self.nonces.get(token_id, 0)*48 + trial
            observed_count = len({source_family(form, self.observations) for form in forms if not form.get("derivation")})
            factors = self.variation.factors(source, token_id, word_id, position,
                                             attempt=attempt, independent_forms=observed_count)
            factors["derived_form_count"] = sum(bool(form.get("derivation")) for form in forms)
            if source.get("derivation"):
                factors["selected_derivation"] = copy.deepcopy(source["derivation"])
            g = source
            compatible = [peer for peer in forms if peer["glyph_id"] != source["glyph_id"] and self.compatible(source, peer)]
            refs = copy.deepcopy(source["source_refs"])
            if compatible and self.variation.amount:
                peer = compatible[min(len(compatible)-1, int(stable_value(self.seed, f"{token_id}:{attempt}", "blend-form")*len(compatible)))]
                ratio = .12 + .38*stable_value(self.seed, f"{token_id}:{attempt}", "blend-ratio")
                g = interpolate_forms(source, peer, ratio)
                factors["interpolation"] = {"glyph_ids": [source["glyph_id"], peer["glyph_id"]], "ratio": ratio,
                                            "basis": "reviewed_writer_conditioned_estimates" if source.get("derivation") or peer.get("derivation") else "compatible_reviewed_geometry"}
                for ref in peer["source_refs"]:
                    if ref not in refs:
                        refs.append(copy.deepcopy(ref))
            paths = curved_strokes(g["strokes"]) if self.variation.amount else g["strokes"]
            strokes = map_strokes(paths, self.variation.transform(g, factors, size))
            item = {"token_id": token_id, "glyph_id": source["glyph_id"], "text": text, "role": role,
                    "representation": source["representation"], "strokes": strokes,
                    "pen_width_mm": self.profile["pen"]["width_mm"]*factors["pen_width"], "nominal_size_mm": size,
                    "source_refs": refs, "semantic": True, "variation": factors}
            item["variation"]["context_selection"] = copy.deepcopy(context_selection)
            descriptor = shape_descriptor(item) if self.variation.amount else None
            assessments = [assess_pair(descriptor, record["descriptor"], threshold_mm=NEAR_THRESHOLD_MM+.003) for record in records] if descriptor else []
            conflicts = [pair for pair in assessments if pair["category"] in {"exact", "near"}]
            distance = min((pair["distance_mm"] for pair in assessments), default=1.)
            construction = structural_descriptor(descriptor) if descriptor and targets else None
            structural_minimum = min((structural_distance(construction, target) for target in targets), default=1.)
            score = (not bool(conflicts), -len(conflicts), structural_minimum if targets else distance, distance)
            item["variation"]["word_structure_distance_mm"] = round(structural_minimum, 7) if targets else None
            item["variation"]["word_structure_guidance_reference_count"] = len(targets)
            if best is None or score > best_score:
                best_score = score
                best, best_distance = (g, item, descriptor), distance
            if not conflicts and (not targets or structural_minimum >= STRUCTURAL_THRESHOLD_MM+.003):
                best, best_distance = (g, item, descriptor), distance
                break
        g, item, descriptor = best
        factors = item["variation"]
        self.variant_history[choice_key] = item["glyph_id"]
        used.add(selection_family(next(form for form in forms if form["glyph_id"] == item["glyph_id"])))
        if descriptor is not None:
            self.history.setdefault(history_key, []).append({"token_id": token_id, "page": self.current_page,
                                                            "descriptor": descriptor})
        item["variation"]["candidate_count"] = trial+1
        item["variation"]["minimum_recent_distance_mm"] = round(best_distance, 7) if records else None
        box = Box(g["advance"] * size * factors["width"], [item])
        # Reserve complete ink, including terminal overhang and rounded caps.
        left = min(0, box.bounds[0])
        box = box.shifted(-left)
        box.advance = max(box.advance - left, box.bounds[2] + 0.035 * size)
        return box

    def observed_glyph(self, forms, text, role, token_id, size, word_id, position, context_selection=None):
        """Use the full real bank before returning to an earlier source form.

        No contour candidate search is involved: an exhausted source bank is
        finite evidence, not permission to manufacture a new shape. Recency
        and counts cross semantic roles that share the same visible character.
        """
        families = {form["glyph_id"]: source_family(form, self.observations) for form in forms}
        def usage(form):
            return self.observed_source_uses[(text, families[form["glyph_id"]])]
        least_usage = min(usage(form) for form in forms)
        candidates = [form for form in forms if usage(form) == least_usage]
        if len(candidates) > 1:
            last = max((self.observed_source_recency.get((text, families[form["glyph_id"]]), -1)
                        for form in forms), default=-1)
            if last >= 0:
                alternatives = [form for form in candidates if self.observed_source_recency.get(
                    (text, families[form["glyph_id"]]), -1) != last]
                # Several aliases can still describe the same source family
                # (for example an externally supplied primitive pool). Reuse
                # that finite source when no different family is available.
                if alternatives:
                    candidates = alternatives
        index = min(len(candidates)-1, int(stable_value(self.seed, token_id, "observed-source") * len(candidates)))
        source = candidates[index]
        family = families[source["glyph_id"]]
        key = (text, family)
        prior_uses = self.observed_source_uses[key]
        self.observed_source_uses[key] += 1
        self.observed_source_recency[key] = self.observed_sequence
        self.observed_sequence += 1
        factors = self.variation.factors(source, token_id, word_id, position,
                                         independent_forms=len(set(families.values())))
        factors.update(source_family=family, source_geometry_hash=geometry_family(source),
                       source_use_index=prior_uses+1, source_reused=bool(prior_uses),
                       derived_form_count=0, candidate_count=len(candidates),
                       selection="balanced_observations_without_immediate_reuse")
        if context_selection is not None:
            factors["context_selection"] = copy.deepcopy(context_selection)
        item = {"token_id": token_id, "glyph_id": source["glyph_id"], "text": text, "role": role,
                "representation": source["representation"],
                "strokes": map_strokes(source["strokes"], self.variation.transform(source, factors, size)),
                "pen_width_mm": self.profile["pen"]["width_mm"], "nominal_size_mm": size,
                "source_refs": copy.deepcopy(source["source_refs"]), "semantic": True, "variation": factors}
        # Keep the same history contract as experimental rendering. It supports
        # page-aware diagnostics, but never causes source paths to be altered.
        self.history.setdefault(text, []).append({"token_id": token_id, "page": self.current_page,
                                                  "descriptor": shape_descriptor(item)})
        box = Box(source["advance"] * size, [item])
        left = min(0, box.bounds[0])
        box = box.shifted(-left)
        box.advance = max(box.advance-left, box.bounds[2]+.035*size)
        return box

    def text(self, text, role, token_id, size=None):
        if f"{token_id}:0" in self.page_hints:
            self.commit_page(self.page_hints[f"{token_id}:0"], [])
        letters = clusters(text)
        words, current = [], []
        for index, char in enumerate(letters+[""]):
            if char and all(c.isalnum() for c in char):
                current.append(index)
            else:
                if len(current) >= 3:
                    words.append(("".join(letters[i] for i in current), current))
                current = []
        comparable = role == "text" and self.variation.amount and not self.variation.observed_only
        repeated = comparable and any(self.word_history.get(word) for word, _ in words)
        previous_targets = self.word_targets
        self.word_targets = {}
        # Candidate words are transactional: rejected alternatives must not
        # consume source families or poison the glyph/page history.
        initial = ({key: list(value) for key, value in self.history.items()},
                   self.variant_history.copy(), {key: set(value) for key, value in self.used_families.items()},
                   self.nonces.copy())
        best, best_score, hard_targets = None, None, {}
        for attempt in range(8 if repeated else 1):
            self.word_targets = {}
            if repeated:
                for word, indexes in words:
                    references = hard_targets.get(word) or self.word_history.get(word, [])
                    if references:
                        for position, index in enumerate(indexes):
                            # Guidance is bounded, while final comparison still
                            # checks every recent word. Requiring each letter to
                            # differ from every previous position would forbid
                            # new combinations of an otherwise useful bank.
                            chosen = len(references)-1 if attempt == 0 else min(len(references)-1,
                                int(stable_value(self.seed, f"{token_id}:{index}:{attempt}", "word-reference")*len(references)))
                            self.word_targets[f"{token_id}:{index}"] = [references[chosen]["descriptor"]["letters"][position]]
            if attempt:
                self.history = {key: list(value) for key, value in initial[0].items()}
                self.variant_history = initial[1].copy()
                self.used_families = {key: set(value) for key, value in initial[2].items()}
                self.nonces = initial[3].copy()
                for i in range(len(letters)):
                    identity = f"{token_id}:{i}"
                    self.nonces[identity] = self.nonces.get(identity, 0)+attempt
                    # A nearly exhausted source cycle must not lock every
                    # retry to its last, structurally similar family. Retry
                    # against the full approved bank; visible-ink history and
                    # the word audit still prevent repetition.
                    self.used_families[(role, letters[i], size or self.size)] = set()
            result = Box()
            for i, c in enumerate(letters):
                identity = f"{token_id}:{i}"
                box = self.glyph(c, role, identity, size, token_id, i / max(1, len(letters) - 1))
                for item in box.items:
                    item.update(word_id=token_id, word_position=i)
                if role == "text":
                    gap = self.letter_clearance(identity, size)
                    for item in box.items:
                        item["body_ink_bounds_mm"] = self.ink_clearance.body_bounds(item)
                    if result.items:
                        result = self.join_letters(result, box, gap)
                    else:
                        result = box.shifted(-box.bounds[0])
                    # A run with no following whitespace can continue the same
                    # word. Only its trailing letter clearance is reserved.
                    result.advance = result.bounds[2] + gap
                else:
                    result = join([result, box], gap=0.025 * (size or self.size) * self.variation.letter_gap(identity)) if result.items else box
            descriptors, conflicts, scores, residuals, next_targets = [], [], [], [], {}
            if comparable:
                for word, indexes in words:
                    desc = word_descriptor([result.items[i] for i in indexes])
                    descriptors.append((word, indexes, desc))
                    for record in self.word_history.get(word, []):
                        comparison = compare_words(record["descriptor"], desc)
                        scores.append(comparison["changed_letter_count"]/comparison["required_changed_letter_count"])
                        residuals.extend(comparison["letter_distances_mm"])
                        if comparison["category"] == "repeated_template":
                            conflicts.append(comparison)
                            next_targets.setdefault(word, []).append(record)
            score = (min(scores, default=1), -len(conflicts), sum(residuals)/max(1, len(residuals)))
            if best is None or score > best_score:
                best_score = score
                best = (result, descriptors,
                        {key: list(value) for key, value in self.history.items()}, self.variant_history.copy(),
                        {key: set(value) for key, value in self.used_families.items()}, self.nonces.copy())
            if not conflicts:
                break
            hard_targets = next_targets
        result, descriptors, self.history, self.variant_history, self.used_families, self.nonces = best
        for item in result.items:
            item["variation"]["word_candidate_count"] = attempt+1
        for word, indexes, desc in descriptors:
            self.word_history.setdefault(word, []).append({"page": self.current_page, "word_id": token_id,
                "token_ids": [result.items[i]["token_id"] for i in indexes], "descriptor": desc})
        self.word_targets = previous_targets
        return result

    def letter_clearance(self, token_id, size=None):
        size = self.size if size is None else size
        observed = self.spacing.get("letter_gap_xheight")
        gap = .12 if observed is None else observed
        # This is a layout clearance, never a deformation of observed ink.
        return max(.04 * size, .5 * self.profile["pen"]["width_mm"],
                   gap * size * self.variation.letter_gap(token_id))

    def whitespace(self, text, token_id):
        observed = self.spacing.get("word_gap_xheight")
        # An old estimated .72-xheight advance was not an ink clearance.
        # Keep an explicitly larger metric, and make the fallback readable.
        gap = max(1.2, self.profile["metrics"]["word_gap"]) if observed is None else observed
        count = sum(4 if char == "\t" else 1 for char in text)
        return count * gap * self.size * self.variation.word_gap(token_id)

    def join_letters(self, left, right, gap):
        """Attach body bands; let intact swashes occupy safely empty space."""
        first, last = right.items[0], left.items[-1]
        desired = last["body_ink_bounds_mm"][2] + gap - first["body_ink_bounds_mm"][0]
        size = first.get("nominal_size_mm", self.size)
        minimum = max(.04 * size, .5 * self.profile["pen"]["width_mm"])
        offset = desired
        if not self.ink_clearance.separated(left.items, right.items, offset, minimum):
            # The full-ink position is always safe. Refine toward the desired
            # body position, retaining a verified-safe upper endpoint even for
            # nonmonotonic shapes with loops and detached dots.
            low = desired
            offset = max(desired, left.bounds[2] + minimum - right.bounds[0])
            while offset-low > GRID_MM:
                middle = (offset+low)/2
                if self.ink_clearance.separated(left.items, right.items, middle, minimum):
                    offset = middle
                else:
                    low = middle
        right = right.shifted(offset)
        right.items[0]["spacing_before"] = {
            "kind": "letter", "placement": "body_band_with_2d_ink_clearance",
            "requested_body_gap_mm": gap,
            "body_gap_mm": right.items[0]["body_ink_bounds_mm"][0]-last["body_ink_bounds_mm"][2],
            "minimum_ink_clearance_mm": minimum, "ink_clearance_verified": True,
            "clearance_method": "complete_ink_masks_with_conservative_raster_margin",
            "raster_grid_mm": GRID_MM, "raster_uncertainty_mm": RASTER_UNCERTAINTY_MM,
            "horizontal_full_ink_gap_mm": item_bounds(right.items[0])[0]-item_bounds(last)[2],
            "left_token_id": last["token_id"],
        }
        return Box(offset+right.advance, left.items+right.items)

    def join_words(self, left, right, gap):
        if not gap:
            if (left.items and right.items and left.items[-1].get("body_ink_bounds_mm")
                    and right.items[0].get("body_ink_bounds_mm")):
                return self.join_letters(left, right, self.letter_clearance(right.items[0]["token_id"],
                                                                          right.items[0].get("nominal_size_mm")))
            return join([left, right])
        if not left.items:
            # Explicit indentation is retained; whitespace at an automatic
            # line break is discarded by the paragraph's wrapping code.
            return join([Box(left.advance + gap), right])
        inner_gaps = []
        for box in (left, right):
            for first, second in zip(box.items, box.items[1:]):
                if (first.get("word_id") and first.get("word_id") == second.get("word_id")
                        and first["role"] == second["role"] == "text"):
                    inner_gaps.append(max(0., item_bounds(second)[0] - item_bounds(first)[2]))
        # The word boundary must remain distinguishable even when a measured
        # source gap is smaller than this rendering's within-word clearances.
        # Using the actual placed ink also covers different sizes and overhang.
        minimum = max(inner_gaps, default=self.letter_clearance("word-boundary"))
        clearance = max(gap, .9 * self.size, minimum * 3, minimum + .2 * self.size)
        if right.items:
            right = copy.deepcopy(right)
            right.items[0]["spacing_before"] = {"kind": "word", "minimum_ink_gap_mm": clearance,
                "requested_ink_gap_mm": gap, "left_token_id": max(left.items, key=lambda item: item_bounds(item)[2])["token_id"]}
        return join_ink(left, right, clearance)

    def phrase(self, text, token_id):
        """Single-line text for an explicit correction, with original spaces."""
        result, pending_gap = Box(), 0
        for match in regex.finditer(r"\s+|[^\s]+", text):
            identity = f"{token_id}:{match.start()}"
            if match.group().isspace():
                pending_gap += self.whitespace(match.group(), identity)
            else:
                result = self.join_words(result, self.text(match.group(), "text", identity), pending_gap)
                pending_gap = 0
        result.advance += pending_gap
        return result

    def has_correction_mark(self):
        return any(form["representation"] == "centerline" for form in self.forms.get(("mark.correction", "crossout"), []))

    def restart_attempt(self, text, token_id):
        """A partial textual start using only already-covered source letters.

        This is explicitly an estimated writing event, not an observed error
        or a changed mathematical answer. Supplied document deletions do not
        pass through this sampler and retain their exact text.
        """
        letters = clusters(text)
        positions = [i for i, char in enumerate(letters) if regex.search(r"\p{L}", char)]
        if len(positions) < 2:
            return None
        count = 1 + int(stable_value(self.seed, token_id, "restart-prefix") * (len(positions)-1))
        return "".join(letters[:positions[count-1]+1])

    def correction(self, deleted, replacement, token_id, kind, replacement_box=None, *,
                   deleted_box=None, deleted_expression=None, replacement_expression=None):
        if not self.has_correction_mark():
            raise InkformError("missing_correction_mark", "A correction needs a reviewed centerline cross-out mark from the writer's samples.", {"node": token_id, "missing_glyphs": [{"role": "mark.correction", "text": "crossout"}]})
        identity = token_id + ".correction"
        old = self.phrase(deleted, token_id + ".deleted") if deleted_box is None else copy.deepcopy(deleted_box)
        new = self.phrase(replacement, token_id + ".replacement") if replacement_box is None else copy.deepcopy(replacement_box)
        mark = self.glyph("crossout", "mark.correction", token_id + ".crossout")
        left, top, right, bottom = old.bounds
        bounds = geometry_bounds(mark.items[0]["strokes"])
        span = bounds[2] - bounds[0]
        if span <= 1e-6:
            raise InkformError("invalid_correction_mark", "The reviewed cross-out mark has no horizontal extent.", {"glyph_id": mark.items[0]["glyph_id"]})
        overhang = self.size * 0.07
        center_y = top + (bottom - top) * 0.53
        mark_height = bounds[3] - bounds[1]
        vertical_scale = min(1, max(self.size * 0.08, (bottom - top) * 0.70) / max(mark_height, 1e-9))
        mark.items[0]["strokes"] = map_strokes(mark.items[0]["strokes"], lambda x, y: (left - overhang + (x - bounds[0]) * (right - left + 2 * overhang) / span, center_y + (y - (bounds[1] + bounds[3]) / 2) * vertical_scale))
        mark.items[0]["construction"] = "extended_reviewed_crossout"
        mark.items[0]["variation"].update(source_shape_preserved=False,
                                          source_geometry_transform="fit_reviewed_crossout_to_deleted_text")
        for box, part, semantic in ((old, "deleted", False), (mark, "mark", False), (new, "replacement", True)):
            for item in box.items:
                item.update(semantic=semantic, correction_id=identity, correction_part=part)
        crossed = Box(max(old.advance, mark.bounds[2]), old.items + mark.items)
        gap = self.whitespace(" ", token_id + ".restart-gap")
        result = self.join_words(crossed, new, gap) if new.items else crossed
        habit = self.profile.get("corrections", {}) if kind == "sampled_restart" else {}
        self.corrections.append({
            "id": identity, "kind": kind, "deleted": deleted, "replacement": replacement,
            "content_type": "math" if deleted_expression is not None else "text",
            "deleted_token_ids": [item["token_id"] for item in old.items],
            "mark_token_ids": [item["token_id"] for item in mark.items],
            "replacement_token_ids": [item["token_id"] for item in new.items],
            "glyph_id": mark.items[0]["glyph_id"], "source_refs": copy.deepcopy(mark.items[0]["source_refs"]),
            "basis": habit.get("basis", "explicit_document"), "sampled_rate": habit.get("rate"),
            "habit_observation_ids": copy.deepcopy(habit.get("observation_ids", [])),
            "habit_source_refs": copy.deepcopy(habit.get("source_refs", [])),
            **({"deleted_expression": copy.deepcopy(deleted_expression),
                "replacement_expression": copy.deepcopy(replacement_expression)} if deleted_expression is not None else {}),
            **({"attempt_basis": "estimated_partial_fragment" if deleted_expression is not None else "estimated_partial_word_restart", "attempt_observed": False}
               if kind == "sampled_restart" else {}),
        })
        return result

    def math_run(self, expression, token_id, *, deleted_expression=None, sampled_rate=0, available=None):
        """An intact intended expression beside an explicit or sampled cut."""
        explicit = deleted_expression is not None
        original = self.math(expression, token_id + ".replacement" if explicit else token_id)
        if not explicit:
            if not sampled_rate or stable_value(self.seed, token_id, "sampled-correction") >= sampled_rate:
                return original
            fragments = math_restart_fragments(expression)
            if not fragments:
                return original
            index = min(len(fragments)-1, int(stable_value(self.seed, token_id, "restart-fragment") * len(fragments)))
            deleted_expression = fragments[index]
        # An optional restart must not consume source selections if its extra
        # ink makes an otherwise printable expression exceed the line width.
        tracked = ("observed_source_uses", "observed_source_recency", "observed_sequence", "history",
                   "variant_history", "used_families", "word_history", "nonces")
        state = {key: copy.deepcopy(getattr(self, key)) for key in tracked} if not explicit else None
        deleted = self.math(deleted_expression, token_id + ".deleted")
        result = self.correction(math_text(deleted_expression), math_text(expression), token_id,
                                 "explicit" if explicit else "sampled_restart", replacement_box=original,
                                 deleted_box=deleted, deleted_expression=deleted_expression,
                                 replacement_expression=expression)
        if not explicit and available is not None and max(result.advance, result.bounds[2])-min(0, result.bounds[0]) > available:
            self.corrections.pop()
            for key, value in state.items():
                setattr(self, key, value)
            return original
        return result

    def bar(self, width, y, token_id, size):
        box = self.glyph("−", "math.operator", token_id, size, primitive=True)
        if box.items[0]["representation"] != "centerline":
            raise InkformError("unsupported_outline_primitive", "Stretchable mathematical marks require reviewed centerlines.")
        bounds = geometry_bounds(box.items[0]["strokes"])
        span = bounds[2] - bounds[0]
        if span < 1e-6:
            raise InkformError("invalid_primitive", "The reviewed fraction-bar form has no horizontal extent.")
        box.items[0]["strokes"] = map_strokes(box.items[0]["strokes"], lambda x, py: ((x - bounds[0]) * width / span, py - (bounds[1] + bounds[3]) / 2 + y))
        box.items[0]["construction"] = "extended_writer_minus"
        box.items[0]["variation"].update(source_shape_preserved=False,
                                         source_geometry_transform="extend_writer_bar_to_expression_width")
        box.advance = width
        return box

    def math(self, expr, token_id, size=None):
        size = self.size if size is None else size
        typ = expr["type"]
        def child(key, scale=1):
            return self.math(expr[key], token_id + "." + key, size * scale)
        if typ == "symbol":
            return self.text(expr["value"], expr.get("role", "math.variable"), token_id, size)
        if typ == "number":
            return join([self.glyph(c, "math.digit" if c.isdigit() else "math.operator", f"{token_id}:{i}", size, token_id) for i, c in enumerate(clusters(expr["value"]))])
        if typ == "row":
            return join([self.math(item, f"{token_id}.items[{i}]", size) for i, item in enumerate(expr["items"])], 0.22 * size)
        if typ == "relation":
            return join([child("left"), self.glyph(expr["operator"], "math.operator", token_id + ".operator", size), child("right")], 0.3 * size)
        if typ == "function_call":
            role = "math.variable" if len(clusters(expr["name"])) == 1 else "math.function"
            boxes = [self.text(expr["name"], role, token_id + ".name", size), self.glyph("(", "math.operator", token_id + ".open", size)]
            for i, arg in enumerate(expr["arguments"]):
                if i:
                    boxes.append(self.glyph(",", "math.operator", f"{token_id}.comma{i}", size))
                boxes.append(self.math(arg, f"{token_id}.arguments[{i}]", size))
            boxes.append(self.glyph(")", "math.operator", token_id + ".close", size))
            return join(boxes, 0.10 * size)
        if typ in {"superscript", "subscript"}:
            base = child("base")
            previous_context = self.glyph_context
            self.glyph_context = typ
            try:
                script = child("script", 0.68)
            finally:
                self.glyph_context = previous_context
            sx = max(base.advance, base.bounds[2]) + size * 0.1
            sy = base.bounds[1] + 0.28 * size if typ == "superscript" else base.bounds[3] - script.bounds[1] + size * 0.12
            result = Box(sx + script.advance, base.items + script.shifted(sx, sy).items)
            return result
        if typ == "fraction":
            numerator, denominator = child("numerator", 0.86), child("denominator", 0.86)
            numerator_span = max(numerator.advance, numerator.bounds[2]) - min(0, numerator.bounds[0])
            denominator_span = max(denominator.advance, denominator.bounds[2]) - min(0, denominator.bounds[0])
            width = max(numerator_span, denominator_span) + size * 0.55
            y = -size * 0.48
            bar = self.bar(width, y, token_id + ".bar", size)
            clearance = max(size * 0.22, self.profile["pen"]["width_mm"] * 0.35)
            num = numerator.shifted((width - numerator_span) / 2 - min(0, numerator.bounds[0]), bar.bounds[1] - clearance - numerator.bounds[3])
            den = denominator.shifted((width - denominator_span) / 2 - min(0, denominator.bounds[0]), bar.bounds[3] + clearance - denominator.bounds[1])
            return Box(width + 0.12 * size, num.items + den.items + bar.items)
        if typ == "root":
            body = child("body")
            radical = self.glyph("√", "math.operator", token_id + ".radical", size, primitive=True)
            if radical.items[0]["representation"] != "centerline":
                raise InkformError("unsupported_outline_primitive", "Expandable roots require centerlines.")
            rb, bb = geometry_bounds(radical.items[0]["strokes"]), body.bounds
            target_top = bb[1] - size * 0.30
            target_bottom = bb[3]
            radical.items[0]["strokes"] = map_strokes(radical.items[0]["strokes"], lambda x, y: (x, target_top + (y - rb[1]) * (target_bottom - target_top) / (rb[3] - rb[1])))
            radical.items[0]["variation"].update(source_shape_preserved=False,
                                                 source_geometry_transform="fit_radical_to_radicand_height")
            # Join actual pen endpoints. The source advance includes crop
            # padding, and its ink bounds include round caps; neither names the
            # end of the radical's upper arm. Starting the roof at that advance
            # left a visible break in every expanded radical.
            endpoints = [point for stroke in radical.items[0]["strokes"] if not stroke.get("closed")
                         for point in (stroke["start"], stroke["segments"][-1]["end"])]
            if not endpoints:
                raise InkformError("invalid_primitive", "The reviewed radical needs an open upper-arm endpoint.")
            upper = [point for point in endpoints if point[1] <= target_top + .4 * (target_bottom-target_top)]
            attachment = max(upper or endpoints, key=lambda point: point[0])
            x = radical.bounds[2] + size * .12 - bb[0]
            roof_end = x + bb[2] + size * .15
            bar = self.bar(roof_end - attachment[0], target_top, token_id + ".bar", size)
            bar_endpoints = [point for stroke in bar.items[0]["strokes"] if not stroke.get("closed")
                             for point in (stroke["start"], stroke["segments"][-1]["end"])]
            if not bar_endpoints:
                raise InkformError("invalid_primitive", "The reviewed root bar needs an open endpoint.")
            start = min(bar_endpoints, key=lambda point: point[0])
            bar = bar.shifted(attachment[0]-start[0], attachment[1]-start[1])
            bar.items[0]["construction"] = "joined_writer_root_bar"
            bar.items[0]["root_radical_token_id"] = radical.items[0]["token_id"]
            return Box(max(roof_end, bar.bounds[2]), radical.items + body.shifted(x).items + bar.items)
        if typ == "limit":
            label = self.text("lim", "math.function", token_id + ".label", size)
            approach = child("approaches", 0.60)
            if expr["direction"] != "both":
                sign = self.glyph("−" if expr["direction"] == "left" else "+", "math.operator", token_id + ".direction", size * 0.43)
                approach = Box(approach.advance + sign.advance, approach.items + sign.shifted(approach.advance + 0.1 * size, approach.bounds[1] + 0.05 * size).items)
            under = join([self.text(expr["variable"], "math.variable", token_id + ".variable", size * 0.60), self.glyph("→", "math.operator", token_id + ".arrow", size * 0.60), approach], 0.12 * size)
            width = max(label.advance, under.advance)
            under_y = label.bounds[3] - under.bounds[1] + size * 0.30
            op = Box(width, label.shifted((width - label.advance) / 2).items + under.shifted((width - under.advance) / 2, under_y).items)
            result = join([op, child("body")], size * 0.35)
            for item in result.items:
                item.setdefault("math_context", []).append({"node": token_id, "type": "limit", "direction": expr["direction"]})
            return result
        raise InkformError("unsupported_math", f"Unsupported math type: {typ}")


def _plan_document_once(document, profile, seed=72, mode=None, nonces=None, page_hints=None):
    validate_document(document); validate_profile(profile)
    mode = resolve_render_mode(profile, mode)
    representation = "centerline" if mode == "fresh_strokes" else "clean_outline"
    eligible = {**profile, "glyphs": [g for g in profile["glyphs"] if g["representation"] == representation
                                    or (g["representation"] == "centerline" and g["role"] == "math.operator"
                                        and g["text"] in {"−", "√"})]}
    needed = requirements(document)
    cov = coverage(eligible, needed)
    visible_cov = coverage(eligible, visible_requirements(document))
    if not visible_cov["complete"]:
        raise InkformError("missing_glyphs", "The profile lacks reviewed forms required by this document, including its visible deleted text.", {**visible_cov, "mode": mode})
    page = document.get("page", {})
    width, height = page.get("width_mm", 210), page.get("height_mm", 297)
    size = page.get("body_x_height_mm", profile["physical_scale"]["body_x_height_mm"])
    margins = page.get("margins_mm", 20)
    if isinstance(margins, (int, float)):
        margins = dict.fromkeys(("top", "right", "bottom", "left"), margins)
    available = width - margins["left"] - margins["right"]
    shaper = Shaper(profile, seed, mode, size, nonces, page_hints)
    habit = profile.get("corrections", {})
    correction_mode = document.get("handwriting", {}).get("corrections", profile.get("output_preferences", {}).get("corrections", "sampled"))
    sampled_rate = habit.get("rate", 0) if correction_mode == "sampled" and shaper.has_correction_mark() else 0
    line_height = profile["metrics"]["line_height"] * size
    instances, lines = [], []
    page_index, cursor = 0, margins["top"]

    def place(line, line_id):
        nonlocal cursor, page_index
        b = line.bounds
        span = max(line.advance, b[2]) - min(0, b[0])
        if span > available + 1e-6:
            raise InkformError("layout_overflow", "A word or equation exceeds the writable width. Use shorter text, split the expression, or choose an overall writing size.", {"node": line_id, "width_mm": span, "available_mm": available})
        ink_height = b[3] - b[1]
        if ink_height > height - margins["top"] - margins["bottom"]:
            raise InkformError("layout_overflow", "A single expression is taller than the page.", {"node": line_id})
        if cursor + ink_height > height - margins["bottom"]:
            page_index += 1
            cursor = margins["top"]
        shaper.commit_page(page_index, line.items)
        baseline = cursor - b[1]
        placed = line.shifted(margins["left"] - min(0, b[0]), baseline)
        for item in placed.items:
            item["page"] = page_index
            item["ink_bounds"] = item_bounds(item)
            instances.append(item)
        lines.append({"id": line_id, "page": page_index, "baseline_mm": baseline, "ink_bounds": [margins["left"], cursor, margins["left"] + span, cursor + ink_height]})
        cursor += max(line_height, ink_height + size * 0.65)

    for block in document["blocks"]:
        if block["type"] in {"display_math", "graph"}:
            if block["type"] == "graph":
                from .graphs import graph_box
                box = graph_box(block["graph"], shaper, json.dumps(block["id"], ensure_ascii=True))
            else:
                box = shaper.math_run(block["expression"], json.dumps(block["id"], ensure_ascii=True),
                                      deleted_expression=block.get("deleted_expression"),
                                      sampled_rate=sampled_rate, available=available)
            for item in box.items:
                item.update(content_node_id=block["id"], block_id=block["id"])
            place(box, block["id"])
        else:
            line, n, pending_gap = Box(), 0, 0
            for run in block["children"]:
                run_token_id = json.dumps(run["id"], ensure_ascii=True)
                if run["type"] in {"inline_math", "math_correction"}:
                    chunks = [(None, shaper.math_run(run["expression"], run_token_id,
                              deleted_expression=run.get("deleted_expression") if run["type"] == "math_correction" else None,
                              sampled_rate=sampled_rate, available=available))]
                elif run["type"] == "correction":
                    chunks = [(None, shaper.correction(run["deleted"], run["text"], run_token_id, "explicit"))]
                else:
                    chunks = [(m.group(), m.start()) for m in regex.finditer(r"\r\n|\n|\r|[^\S\r\n]+|[^\s]+", run["text"])]
                for text, box_or_offset in chunks:
                    if text is not None and text.isspace():
                        if "\n" in text or "\r" in text:
                            place(line, f"{block['id']}.line{n}"); line = Box(); n += 1; pending_gap = 0
                        else:
                            pending_gap += shaper.whitespace(text, f"{run_token_id}:{box_or_offset}")
                        continue
                    box = box_or_offset if text is None else shaper.text(text, "text", f"{run_token_id}:{box_or_offset}")
                    attempted = shaper.restart_attempt(text, f"{run_token_id}:{box_or_offset}") if text is not None and sampled_rate else None
                    if attempted and stable_value(seed, f"{run_token_id}:{box_or_offset}", "sampled-correction") < sampled_rate:
                        original = box
                        source_state = (shaper.observed_source_uses.copy(), shaper.observed_source_recency.copy(),
                                        shaper.observed_sequence, {key: list(value) for key, value in shaper.history.items()})
                        box = shaper.correction(attempted, text, f"{run_token_id}:{box_or_offset}", "sampled_restart", replacement_box=original)
                        if max(box.advance, box.bounds[2]) - min(0, box.bounds[0]) > available:
                            # Optional sample-derived texture must not make a
                            # previously printable word become unbreakable.
                            shaper.corrections.pop()
                            if shaper.variation.observed_only:
                                (shaper.observed_source_uses, shaper.observed_source_recency,
                                 shaper.observed_sequence, shaper.history) = source_state
                            box = original
                    for item in box.items:
                        item.update(content_node_id=run["id"], block_id=block["id"])
                    candidate = shaper.join_words(line, box, pending_gap)
                    if line.items and max(candidate.advance, candidate.bounds[2])-min(0, candidate.bounds[0]) > available:
                        place(line, f"{block['id']}.line{n}"); line = box; n += 1
                    else:
                        line = candidate
                    pending_gap = 0
            if line.items:
                place(line, f"{block['id']}.line{n}")
        cursor += size * 0.65
    if not instances:
        raise InkformError("empty_document", "The document contains no visible writing.")
    token_ids = [item["token_id"] for item in instances]
    expected = Counter(needed)
    rendered = Counter((item["role"], item["text"]) for item in instances if item.get("semantic", True))
    if len(set(token_ids)) != len(token_ids) or rendered != expected:
        raise InkformError("content_accounting_failed", "The shaped geometry must account for every required symbol in its exact role.", {"required": len(needed), "rendered": len(instances), "missing": [{"role": role, "text": text, "count": count} for (role, text), count in (expected - rendered).items()], "unexpected": [{"role": role, "text": text, "count": count} for (role, text), count in (rendered - expected).items()]})
    geometry_hash = hashlib.sha256(json.dumps(instances, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    spacing = {**copy.deepcopy(shaper.spacing), "version": "inkform-ink-clearance-2",
               "letter_placement": "body_band_with_verified_complete_2d_ink_clearance",
               "word_measurement": "complete_ink_bounds_including_pen_and_overhang",
               "collision_raster_grid_mm": GRID_MM, "collision_raster_uncertainty_mm": RASTER_UNCERTAINTY_MM,
               "fallback_letter_gap_xheight": .12, "fallback_word_gap_xheight": 1.2,
               "minimum_word_gap_xheight": .9, "minimum_word_to_letter_gap_ratio": 3,
               "source_paths_changed": False}
    return {"document_id": document["document_id"], "content_mode": document["content_mode"], "unresolved_inputs": copy.deepcopy(document.get("unresolved_inputs", [])), "content_status": "partial" if document.get("unresolved_inputs") else "complete", "page": {"width_mm": width, "height_mm": height, "margins_mm": margins, "body_x_height_mm": size}, "page_count": page_index + 1, "instances": instances, "lines": lines, "geometry_hash": geometry_hash, "required_symbol_count": len(needed), "rendered_symbol_count": sum(rendered.values()), "visible_symbol_count": len(instances), "coverage": cov, "visible_coverage": visible_cov, "corrections": shaper.corrections, "graphs": getattr(shaper, "graphs", []), "spacing": spacing, "variation": {"version": "inkform-occurrence-3", "mode": shaper.variation.mode, "amount": shaper.variation.amount, "estimated_axes_are_not_learned_dynamics": True}, "source_usage": _source_usage(instances, shaper)}


def _source_usage(instances, shaper):
    """Collection guidance from actual visible selections, not novelty claims."""
    groups = {}
    for item in instances:
        factors = item.get("variation", {})
        if not factors.get("source_family"):
            continue
        key = (item["role"], item["text"])
        record = groups.setdefault(key, {"role": key[0], "text": key[1], "occurrences": 0,
                                        "available_source_forms": 0, "families": Counter()})
        record["occurrences"] += 1
        record["available_source_forms"] = max(record["available_source_forms"], factors["independent_form_count"])
        record["families"][factors["source_family"]] += 1
    records = []
    for key in sorted(groups):
        record = groups[key]
        families = record.pop("families")
        record.update(selected_source_forms=len(families), reused_occurrences=sum(count-1 for count in families.values()),
                      selected_glyph_ids=sorted({item["glyph_id"] for item in instances if (item["role"], item["text"]) == key}))
        records.append(record)
    requests = [{"role": row["role"], "text": row["text"], "available_source_forms": row["available_source_forms"],
                 "occurrences": row["occurrences"], "reused_occurrences": row["reused_occurrences"],
                 "action": "Collect and review more distinct occurrences from the writer's original samples."}
                for row in records if row["reused_occurrences"]]
    return {"mode": shaper.variation.mode, "excluded_derived_forms": shaper.excluded_derived_count,
            "selection": "balanced_observations_without_immediate_reuse" if shaper.variation.observed_only else "experimental_novelty_search",
            "observed_form_usage_evaluated": shaper.variation.observed_only,
            "reused_occurrences": sum(row["reused_occurrences"] for row in records) if shaper.variation.observed_only else None,
            "groups": records, "collection_requests": requests,
            "note": "Source inventory counts are not a uniqueness claim. The independent ink and word audits measure the actual output."}


def plan_document(document, profile, seed=72, mode=None):
    """Shape, audit visible ink, and retry conflicting occurrences finitely."""
    nonces, page_hints = {}, {}
    for attempt in range(3):
        plan = _plan_document_once(document, profile, seed, mode, nonces, page_hints)
        variation_mode = plan["variation"]["mode"]
        report = audit_repetition(plan["instances"], policy={"amount": plan["variation"]["amount"], "strict_near": True, "mode": variation_mode})
        plan["repetition"] = report
        words = audit_word_patterns(plan["instances"], amount=plan["variation"]["amount"], mode=variation_mode)
        plan["word_patterns"] = words
        plan["variation"]["audit_passes"] = attempt+1
        failures = [pair for pair in report.get("pairs", []) if pair["category"] in {"exact", "near"}]
        if report["unchecked_instances"] and (variation_mode == "observed" or plan["variation"]["amount"]):
            raise InkformError("repetition_audit_incomplete", "Some rendered ink could not be measured; strict non-repetition has not been verified.", {"repetition": report})
        if variation_mode == "observed" or plan["variation"]["amount"] == 0 or not (report["exact_count"] or report["near_count"] or report["unchecked_instances"] or words["repeated_word_count"]):
            return plan
        # A word is provisionally shaped before we know whether its line spills
        # onto another page. Reuse actual positions on the next pass so stale
        # two-pages-old words cannot crowd out valid adjacent-page choices.
        page_hints = {item["token_id"]: item["page"] for item in plan["instances"]}
        for pair in failures:
            token = pair["second_token_id"]
            nonces[token] = nonces.get(token, 0)+1
        for pair in words["pairs"]:
            for token in pair["second_token_ids"]:
                nonces[token] = nonces.get(token, 0)+1
    missing = Counter((pair["role"], pair["text"]) for pair in failures)
    by_token = {item["token_id"]: item for item in plan["instances"]}
    for pair in words["pairs"]:
        for token, distance in zip(pair["second_token_ids"], pair["letter_distances_mm"]):
            if distance < pair["threshold_mm"]:
                item = by_token[token]
                missing[(item["role"], item["text"])] += 1
    raise InkformError("repetition_exhausted", "Experimental novelty checks exhausted the reviewed bank. Collect more real samples of the listed letters and symbols, or use observed mode to preserve source geometry and report reuse honestly.",
                       {"repetition": report, "word_patterns": words, "attempted_passes": 3,
                        "repeated_words": sorted({pair["text"] for pair in words["pairs"]}),
                        "needed_samples": [{"role": role, "text": text, "conflicting_pairs": count} for (role,text),count in missing.items()]})
