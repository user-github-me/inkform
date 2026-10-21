"""Validated semantic documents. No OCR, solving, or implicit rewriting here."""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

import regex

from .errors import InkformError

MATH_TYPES = {"symbol", "number", "row", "relation", "fraction", "root", "superscript", "subscript", "limit", "function_call"}


def clusters(text):
    return regex.findall(r"\X", text)


def fail(message, path="", code="invalid_document"):
    raise InkformError(code, message, {"path": path})


def _string(value, path, nonempty=True):
    if not isinstance(value, str) or (nonempty and not value):
        fail("Expected a nonempty string." if nonempty else "Expected a string.", path)


def validate_expression(expr, path="expression", depth=0):
    if depth > 40:
        fail("Math nesting exceeds 40 levels.", path)
    if not isinstance(expr, dict) or not isinstance(expr.get("type"), str) or expr["type"] not in MATH_TYPES:
        fail("Unsupported math construct; supported: " + ", ".join(sorted(MATH_TYPES)), path, "unsupported_math")
    typ = expr["type"]
    def child(key):
        validate_expression(expr.get(key), f"{path}.{key}", depth + 1)
    if typ in {"symbol", "number"}:
        _string(expr.get("value"), path + ".value")
        if typ == "number" and not re.fullmatch(r"[+−-]?\d+(?:\.\d+)?", expr["value"], re.ASCII):
            fail("Numbers use decimal digits and an optional sign; use a structured expression for other notation.", path)
        if typ == "symbol" and (not isinstance(expr.get("role", "math.variable"), str) or expr.get("role", "math.variable") not in {"math.variable", "math.operator", "math.function", "math.digit"}):
            fail("Invalid math symbol role.", path)
    elif typ == "row":
        items = expr.get("items")
        if not isinstance(items, list) or not items:
            fail("A math row requires items.", path)
        for i, item in enumerate(items):
            validate_expression(item, f"{path}.items[{i}]", depth + 1)
    elif typ == "relation":
        child("left"); child("right")
        if not isinstance(expr.get("operator"), str) or expr["operator"] not in {"=", "≠", "<", ">", "≤", "≥", "+", "-", "−", "×", "÷", "→"}:
            fail("Unsupported relation/operator.", path, "unsupported_math")
    elif typ == "fraction":
        child("numerator"); child("denominator")
    elif typ == "root":
        child("body")
        if "index" in expr:
            fail("Indexed roots are not supported in this version.", path, "unsupported_math")
    elif typ in {"superscript", "subscript"}:
        child("base"); child("script")
    elif typ == "function_call":
        _string(expr.get("name"), path + ".name")
        if not isinstance(expr.get("arguments"), list) or not expr["arguments"]:
            fail("Function calls require at least one argument.", path)
        for i, arg in enumerate(expr["arguments"]):
            validate_expression(arg, f"{path}.arguments[{i}]", depth + 1)
    elif typ == "limit":
        _string(expr.get("variable"), path + ".variable")
        if not isinstance(expr.get("direction"), str) or expr["direction"] not in {"left", "right", "both"}:
            fail("A limit needs explicit direction: left, right, or both.", path)
        child("approaches"); child("body")


def validate_document(document):
    if not isinstance(document, dict) or document.get("schema_version") != "1.0":
        fail("Expected document schema_version 1.0.")
    _string(document.get("document_id"), "document_id")
    if not isinstance(document.get("content_mode"), str) or document["content_mode"] not in {"literal", "solve", "rewrite"}:
        fail("content_mode must be literal, solve, or rewrite.")
    page = document.get("page", {})
    if not isinstance(page, dict):
        fail("page must be an object.")
    for name, default in (("width_mm", 210), ("height_mm", 297), ("body_x_height_mm", 2.8)):
        val = page.get(name, default)
        if isinstance(val, bool) or not isinstance(val, (int, float)) or not math.isfinite(val) or val <= 0:
            fail("Page dimensions and writing size must be finite positive numbers.", f"page.{name}")
    margins = page.get("margins_mm", 20)
    if isinstance(margins, (int, float)) and not isinstance(margins, bool):
        margins = dict.fromkeys(("left", "right", "top", "bottom"), margins)
    if not isinstance(margins, dict) or set(margins) != {"left", "right", "top", "bottom"}:
        fail("margins_mm must be a number or contain top, right, bottom, left.")
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0 for v in margins.values()):
        fail("Margins must be finite nonnegative numbers.")
    if margins["left"] + margins["right"] >= page.get("width_mm", 210) or margins["top"] + margins["bottom"] >= page.get("height_mm", 297):
        fail("Margins leave no writable area.")
    ids = set()
    blocks = document.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        fail("A document needs at least one block.")
    def identifier(node, path):
        _string(node.get("id"), path + ".id")
        if node["id"] in ids:
            fail("Block and run IDs must be globally unique for stable variation.", path)
        ids.add(node["id"])
    for i, block in enumerate(blocks):
        path = f"blocks[{i}]"
        if not isinstance(block, dict):
            fail("Expected a block object.", path)
        identifier(block, path)
        if block.get("type") == "paragraph":
            if not isinstance(block.get("children"), list) or not block["children"]:
                fail("Paragraphs need typed text/inline_math children.", path)
            for j, run in enumerate(block["children"]):
                if not isinstance(run, dict):
                    fail("Expected a run object.", path)
                identifier(run, f"{path}.children[{j}]")
                if run.get("type") == "text":
                    _string(run.get("text"), path + ".text", False)
                    if any(ord(ch) < 32 and ch not in "\n\t\r" for ch in run["text"]):
                        fail("Text contains unsupported control characters.", path)
                elif run.get("type") == "inline_math":
                    validate_expression(run.get("expression"), path)
                else:
                    fail("Unsupported paragraph child.", path)
        elif block.get("type") == "display_math":
            validate_expression(block.get("expression"), path)
        else:
            fail("Only paragraph and display_math blocks are supported.", path)
    unresolved = document.get("unresolved_inputs", [])
    if not isinstance(unresolved, list):
        fail("unresolved_inputs must be a list.")
    for item in unresolved:
        if not isinstance(item, dict) or not isinstance(item.get("source_item"), str) or not item["source_item"] or not isinstance(item.get("reason"), str) or not item["reason"]:
            fail("Unresolved input needs source_item and reason.")
    return document


def load_document(path):
    def reject_constant(value):
        raise ValueError(f"Non-finite JSON value: {value}")

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Duplicate JSON object key: {key}")
            result[key] = value
        return result

    try:
        return validate_document(json.loads(Path(path).read_text(encoding="utf-8"), parse_constant=reject_constant, object_pairs_hook=unique_object))
    except (OSError, ValueError) as exc:
        raise InkformError("document_read_error", str(exc)) from exc


def literal_document(text, document_id="note"):
    return validate_document({"schema_version": "1.0", "document_id": document_id, "content_mode": "literal", "blocks": [{"id": "body", "type": "paragraph", "children": [{"id": "body-text", "type": "text", "text": text}]}], "unresolved_inputs": []})


def math_requirements(expr):
    t = expr["type"]
    if t == "symbol":
        return [(expr.get("role", "math.variable"), c) for c in clusters(expr["value"])]
    if t == "number":
        return [("math.digit" if c.isdigit() else "math.operator", c) for c in clusters(expr["value"])]
    if t == "row":
        return [r for item in expr["items"] for r in math_requirements(item)]
    if t == "relation":
        return math_requirements(expr["left"]) + [("math.operator", expr["operator"])] + math_requirements(expr["right"])
    if t == "fraction":
        return [("math.operator", "−")] + math_requirements(expr["numerator"]) + math_requirements(expr["denominator"])
    if t == "root":
        return [("math.operator", "√"), ("math.operator", "−")] + math_requirements(expr["body"])
    if t in {"superscript", "subscript"}:
        return math_requirements(expr["base"]) + math_requirements(expr["script"])
    if t == "function_call":
        role = "math.variable" if len(clusters(expr["name"])) == 1 else "math.function"
        return [(role, c) for c in clusters(expr["name"])] + [("math.operator", "("), ("math.operator", ")")] + [("math.operator", ",")] * (len(expr["arguments"]) - 1) + [r for a in expr["arguments"] for r in math_requirements(a)]
    if t == "limit":
        req = [("math.function", c) for c in "lim"] + [("math.variable", c) for c in clusters(expr["variable"])] + [("math.operator", "→")] + math_requirements(expr["approaches"]) + math_requirements(expr["body"])
        if expr["direction"] != "both":
            req += [("math.operator", "−" if expr["direction"] == "left" else "+")]
        return req
    raise AssertionError(t)


def requirements(document):
    validate_document(document)
    result = []
    for block in document["blocks"]:
        if block["type"] == "display_math":
            result.extend(math_requirements(block["expression"]))
        else:
            for run in block["children"]:
                result.extend(math_requirements(run["expression"]) if run["type"] == "inline_math" else [("text", c) for c in clusters(run["text"]) if not c.isspace()])
    return result


def math_text(expr):
    t = expr["type"]
    if t in {"symbol", "number"}: return expr["value"]
    if t == "row": return " ".join(map(math_text, expr["items"]))
    if t == "relation": return f"{math_text(expr['left'])} {expr['operator']} {math_text(expr['right'])}"
    if t == "fraction": return f"({math_text(expr['numerator'])})/({math_text(expr['denominator'])})"
    if t == "root": return f"sqrt({math_text(expr['body'])})"
    if t in {"superscript", "subscript"}: return math_text(expr["base"]) + ("^" if t == "superscript" else "_") + "(" + math_text(expr["script"]) + ")"
    if t == "function_call": return expr["name"] + "(" + ", ".join(map(math_text, expr["arguments"])) + ")"
    if t == "limit":
        sign = {"left": "-", "right": "+", "both": ""}[expr["direction"]]
        return f"lim_({expr['variable']}->{math_text(expr['approaches'])}{sign}) {math_text(expr['body'])}"


def plain_text(document):
    return "\n\n".join(math_text(b["expression"]) if b["type"] == "display_math" else "".join(r["text"] if r["type"] == "text" else math_text(r["expression"]) for r in b["children"]) for b in document["blocks"])
