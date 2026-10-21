"""Physical, role-aware layout from writer paths, with stable seeded variants."""
from __future__ import annotations

import copy
import hashlib
import json
import math
from collections import Counter
from dataclasses import dataclass, field

import regex

from .document import clusters, requirements, validate_document, visible_requirements
from .errors import InkformError
from .profiles import accepted_glyphs, coverage, geometry_bounds, resolve_render_mode, validate_profile
from .variation import OccurrenceVariation, stable_value


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


class Shaper:
    def __init__(self, profile, seed, mode, size):
        self.profile, self.seed, self.mode, self.size = profile, seed, mode, size
        self.forms, self.variant_history, self.variant_choices = {}, {}, {}
        self.variation = OccurrenceVariation(profile, seed)
        self.corrections = []
        representation = "centerline" if mode == "fresh_strokes" else "clean_outline"
        for glyph in accepted_glyphs(profile):
            if glyph["representation"] == representation or glyph["role"] == "mark.correction":
                self.forms.setdefault((glyph["role"], glyph["text"]), []).append(glyph)
        for forms in self.forms.values():
            forms.sort(key=lambda form: form["glyph_id"])

    def glyph(self, text, role, token_id, size=None, word_id=None, position=0.5):
        size = self.size if size is None else size
        forms = self.forms.get((role, text), [])
        if not forms:
            raise InkformError("missing_glyphs", "No reviewed geometry in the requested render mode.", {"missing_glyphs": [{"role": role, "text": text}], "mode": self.mode})
        if role == "mark.correction":
            # A reviewed pen mark is independent of the body representation.
            # Stretching a filled outline would also stretch its pen thickness.
            forms = [form for form in forms if form["representation"] == "centerline"]
            if not forms:
                raise InkformError("missing_correction_mark", "Cross-outs require a reviewed centerline mark from the writer's samples.", {"role": role, "text": text})
        if self.mode == "clean_outline" and role != "mark.correction":
            compatible_size = [form for form in forms if "outline_calibration" in form and abs(size - form["outline_calibration"]["body_x_height_mm"]) <= 0.01]
            if not compatible_size:
                raise InkformError("outline_scale_incompatible", "Filled outlines need a reviewed variant at the requested writing or script size.", {"role": role, "text": text, "requested_size_mm": size, "calibrated_sizes_mm": [form.get("outline_calibration", {}).get("body_x_height_mm") for form in forms]})
            forms = [form for form in compatible_size if abs(self.profile["pen"]["width_mm"] - form["outline_calibration"].get("pen_width_mm", form["outline_calibration"].get("effective_width_mm", -1))) <= 0.01]
            if not forms:
                raise InkformError("outline_pen_incompatible", "Changing the pen model cannot change a filled outline's thickness; select a reviewed compatible variant.", {"role": role, "text": text, "requested_pen_width_mm": self.profile["pen"]["width_mm"]})
        # History is scoped to the original stable content node. Inserting an
        # unrelated paragraph cannot change a surviving node's chosen forms.
        try:
            context = json.JSONDecoder().raw_decode(token_id)[0]
        except (ValueError, TypeError):
            context = word_id or token_id
        history_key = (str(context), role, text, size)
        choice_key = (token_id, role, text, size)
        index = min(len(forms) - 1, int(stable_value(self.seed, token_id, "variant") * len(forms)))
        previous = self.variant_history.get(history_key)
        if len(forms) > 1 and forms[index]["glyph_id"] == previous:
            index = (index + 1 + int(stable_value(self.seed, token_id, "alternate-variant") * (len(forms) - 1))) % len(forms)
        chosen = self.variant_choices.get(choice_key, forms[index]["glyph_id"])
        g = next(form for form in forms if form["glyph_id"] == chosen)
        self.variant_choices[choice_key] = g["glyph_id"]
        self.variant_history[history_key] = g["glyph_id"]
        word_id = word_id or token_id
        factors = self.variation.factors(g, token_id, word_id, position)
        strokes = map_strokes(g["strokes"], self.variation.transform(g, factors, size))
        item = {"token_id": token_id, "glyph_id": g["glyph_id"], "text": text, "role": role, "representation": g["representation"], "strokes": strokes, "pen_width_mm": self.profile["pen"]["width_mm"], "source_refs": copy.deepcopy(g["source_refs"]), "semantic": True, "variation": factors}
        box = Box(g["advance"] * size * factors["width"], [item])
        # Reserve complete ink, including terminal overhang and rounded caps.
        left = min(0, box.bounds[0])
        box = box.shifted(-left)
        box.advance = max(box.advance - left, box.bounds[2] + 0.035 * size)
        return box

    def text(self, text, role, token_id, size=None):
        letters = clusters(text)
        result = Box()
        for i, c in enumerate(letters):
            identity = f"{token_id}:{i}"
            box = self.glyph(c, role, identity, size, token_id, i / max(1, len(letters) - 1))
            result = join([result, box], gap=0.025 * (size or self.size) * self.variation.letter_gap(identity)) if result.items else box
        return result

    def phrase(self, text, token_id):
        """Single-line text for an explicit correction, with original spaces."""
        result, pending_gap = Box(), 0
        for match in regex.finditer(r"\s+|[^\s]+", text):
            identity = f"{token_id}:{match.start()}"
            if match.group().isspace():
                pending_gap += self.profile["metrics"]["word_gap"] * self.size * len(match.group()) * self.variation.word_gap(identity)
            else:
                result = join([Box(result.advance + pending_gap, result.items), self.text(match.group(), "text", identity)])
                pending_gap = 0
        result.advance += pending_gap
        return result

    def has_correction_mark(self):
        return any(form["representation"] == "centerline" for form in self.forms.get(("mark.correction", "crossout"), []))

    def correction(self, deleted, replacement, token_id, kind, replacement_box=None):
        if not self.has_correction_mark():
            raise InkformError("missing_correction_mark", "A correction needs a reviewed centerline cross-out mark from the writer's samples.", {"node": token_id, "missing_glyphs": [{"role": "mark.correction", "text": "crossout"}]})
        identity = token_id + ".correction"
        old = self.phrase(deleted, token_id + ".deleted")
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
        for box, part, semantic in ((old, "deleted", False), (mark, "mark", False), (new, "replacement", True)):
            for item in box.items:
                item.update(semantic=semantic, correction_id=identity, correction_part=part)
        crossed = Box(max(old.advance, mark.bounds[2]), old.items + mark.items)
        gap = self.profile["metrics"]["word_gap"] * self.size * self.variation.word_gap(token_id + ".restart-gap")
        result = join([crossed, new], gap) if new.items else crossed
        habit = self.profile.get("corrections", {}) if kind == "sampled_restart" else {}
        self.corrections.append({
            "id": identity, "kind": kind, "deleted": deleted, "replacement": replacement,
            "deleted_token_ids": [item["token_id"] for item in old.items],
            "mark_token_ids": [item["token_id"] for item in mark.items],
            "replacement_token_ids": [item["token_id"] for item in new.items],
            "glyph_id": mark.items[0]["glyph_id"], "source_refs": copy.deepcopy(mark.items[0]["source_refs"]),
            "basis": habit.get("basis", "explicit_document"), "sampled_rate": habit.get("rate"),
            "habit_observation_ids": copy.deepcopy(habit.get("observation_ids", [])),
            "habit_source_refs": copy.deepcopy(habit.get("source_refs", [])),
        })
        return result

    def bar(self, width, y, token_id, size):
        box = self.glyph("−", "math.operator", token_id, size)
        if box.items[0]["representation"] != "centerline":
            raise InkformError("unsupported_outline_primitive", "Stretchable mathematical marks require reviewed centerlines.")
        bounds = geometry_bounds(box.items[0]["strokes"])
        span = bounds[2] - bounds[0]
        if span < 1e-6:
            raise InkformError("invalid_primitive", "The reviewed fraction-bar form has no horizontal extent.")
        box.items[0]["strokes"] = map_strokes(box.items[0]["strokes"], lambda x, py: ((x - bounds[0]) * width / span, py - (bounds[1] + bounds[3]) / 2 + y))
        box.items[0]["construction"] = "extended_writer_minus"
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
            base, script = child("base"), child("script", 0.68)
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
            radical = self.glyph("√", "math.operator", token_id + ".radical", size)
            if radical.items[0]["representation"] != "centerline":
                raise InkformError("unsupported_outline_primitive", "Expandable roots require centerlines.")
            rb, bb = radical.bounds, body.bounds
            target_top = bb[1] - size * 0.30
            target_bottom = bb[3]
            radical.items[0]["strokes"] = map_strokes(radical.items[0]["strokes"], lambda x, y: (x, target_top + (y - rb[1]) * (target_bottom - target_top) / (rb[3] - rb[1])))
            x = radical.advance + size * 0.05
            bar = self.bar(body.advance + size * 0.15, target_top, token_id + ".bar", size).shifted(x)
            return Box(x + body.advance + size * 0.15, radical.items + body.shifted(x).items + bar.items)
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


def plan_document(document, profile, seed=72, mode=None):
    validate_document(document); validate_profile(profile)
    mode = resolve_render_mode(profile, mode)
    representation = "centerline" if mode == "fresh_strokes" else "clean_outline"
    eligible = {**profile, "glyphs": [g for g in profile["glyphs"] if g["representation"] == representation]}
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
    shaper = Shaper(profile, seed, mode, size)
    habit = profile.get("corrections", {})
    sampled_rate = habit.get("rate", 0) if document.get("handwriting", {}).get("corrections", "sampled") == "sampled" and shaper.has_correction_mark() else 0
    line_height = profile["metrics"]["line_height"] * size
    word_gap = profile["metrics"]["word_gap"] * size
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
        baseline = cursor - b[1]
        placed = line.shifted(margins["left"] - min(0, b[0]), baseline)
        for item in placed.items:
            item["page"] = page_index
            item["ink_bounds"] = item_bounds(item)
            instances.append(item)
        lines.append({"id": line_id, "page": page_index, "baseline_mm": baseline, "ink_bounds": [margins["left"], cursor, margins["left"] + span, cursor + ink_height]})
        cursor += max(line_height, ink_height + size * 0.65)

    for block in document["blocks"]:
        if block["type"] == "display_math":
            box = shaper.math(block["expression"], json.dumps(block["id"], ensure_ascii=True))
            for item in box.items:
                item.update(content_node_id=block["id"], block_id=block["id"])
            place(box, block["id"])
        else:
            line, n, pending_gap = Box(), 0, 0
            for run in block["children"]:
                run_token_id = json.dumps(run["id"], ensure_ascii=True)
                if run["type"] == "inline_math":
                    chunks = [(None, shaper.math(run["expression"], run_token_id))]
                elif run["type"] == "correction":
                    chunks = [(None, shaper.correction(run["deleted"], run["text"], run_token_id, "explicit"))]
                else:
                    chunks = [(m.group(), m.start()) for m in regex.finditer(r"\r\n|\n|\r|[^\S\r\n]+|[^\s]+", run["text"])]
                for text, box_or_offset in chunks:
                    if text is not None and text.isspace():
                        if "\n" in text or "\r" in text:
                            place(line, f"{block['id']}.line{n}"); line = Box(); n += 1; pending_gap = 0
                        else:
                            pending_gap += word_gap * sum(4 if ch == "\t" else 1 for ch in text) * shaper.variation.word_gap(f"{run_token_id}:{box_or_offset}")
                        continue
                    box = box_or_offset if text is None else shaper.text(text, "text", f"{run_token_id}:{box_or_offset}")
                    if text is not None and sampled_rate and len(clusters(text)) > 1 and regex.search(r"\p{L}", text) and stable_value(seed, f"{run_token_id}:{box_or_offset}", "sampled-correction") < sampled_rate:
                        original = box
                        box = shaper.correction(text, text, f"{run_token_id}:{box_or_offset}", "sampled_restart", replacement_box=original)
                        if max(box.advance, box.bounds[2]) - min(0, box.bounds[0]) > available:
                            # Optional sample-derived texture must not make a
                            # previously printable word become unbreakable.
                            shaper.corrections.pop()
                            box = original
                    for item in box.items:
                        item.update(content_node_id=run["id"], block_id=block["id"])
                    candidate = join([Box(line.advance + pending_gap, line.items), box])
                    if line.items and max(candidate.advance, candidate.bounds[2]) > available:
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
    return {"document_id": document["document_id"], "content_mode": document["content_mode"], "unresolved_inputs": copy.deepcopy(document.get("unresolved_inputs", [])), "content_status": "partial" if document.get("unresolved_inputs") else "complete", "page": {"width_mm": width, "height_mm": height, "margins_mm": margins, "body_x_height_mm": size}, "page_count": page_index + 1, "instances": instances, "lines": lines, "geometry_hash": geometry_hash, "required_symbol_count": len(needed), "rendered_symbol_count": sum(rendered.values()), "visible_symbol_count": len(instances), "coverage": cov, "visible_coverage": visible_cov, "corrections": shaper.corrections, "variation": {"version": "inkform-occurrence-1", "amount": shaper.variation.amount, "estimated_axes_are_not_learned_dynamics": True}}
