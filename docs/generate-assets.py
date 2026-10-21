"""Rebuild the site's public vector specimens from the synthetic profile only.

Run with the project's Python environment. No private profile or sample lookup
is used here; assets are deterministically generated from authored coordinates.
"""

import copy
import html
import json
from pathlib import Path

from inkform.document import literal_document
from inkform.layout import plan_document
from inkform.profiles import load_profile
from inkform.rendering import export_pdf


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "docs" / "assets"
profile = load_profile(ROOT / "examples" / "synthetic-profile.json")
assert profile["synthetic"] is True
assert not profile["sources"]
profile["pen"]["width_mm"] = .38
profile["pen"]["color"] = "#19313a"
for glyph in profile["glyphs"]:
    assert not glyph["source_refs"]
    for stroke in glyph["strokes"]:
        stroke["width_profile"] = [{"s": 0, "multiplier": 1}, {"s": 1, "multiplier": 1}]


def svg(plan, title, color):
    width, height = plan["page"]["width_mm"], plan["page"]["height_mm"]
    output = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}">',
              f"<title>{html.escape(title)}</title>",
              "<desc>Generated only from Inkform's authored synthetic demo profile. No personal handwriting samples.</desc>",
              f'<g fill="none" stroke="{color}" stroke-linecap="round" stroke-linejoin="round">']
    def xy(point):
        return " ".join(f"{value:.5f}" for value in point)
    for item in plan["instances"]:
        for stroke in item["strokes"]:
            commands = ["M " + xy(stroke["start"])]
            for segment in stroke["segments"]:
                commands.append("L " + xy(segment["end"]) if segment["type"] == "line" else "C " + " ".join(xy(segment[key]) for key in ("control1", "control2", "end")))
            if stroke.get("closed"):
                commands.append("Z")
            output.append(f'<path d="{" ".join(commands)}" stroke-width="{item["pen_width_mm"]}"/>')
    return "\n".join(output + ["</g>", "</svg>", ""])


note = literal_document("make room for\nsmall thoughts.\n\nkeep the shape.\nwrite something new.\n\none page at a time.", "inkform-site-specimen")
note["page"] = {"width_mm": 120, "height_mm": 145, "margins_mm": 5, "body_x_height_mm": 5.0}
note_plan = plan_document(note, profile, seed=72, mode="fresh_strokes")
(ASSETS / "handwriting.svg").write_text(svg(note_plan, "Synthetic handwriting specimen", profile["pen"]["color"]))

expression = {"type": "relation", "operator": "=", "left": {"type": "limit", "variable": "x", "approaches": {"type": "number", "value": "2"}, "direction": "left", "body": {"type": "function_call", "name": "f", "arguments": [{"type": "symbol", "value": "x"}]}}, "right": {"type": "fraction", "numerator": {"type": "number", "value": "1"}, "denominator": {"type": "number", "value": "2"}}}
math_document = {"schema_version": "1.0", "document_id": "inkform-site-math", "content_mode": "literal", "page": {"width_mm": 150, "height_mm": 62, "margins_mm": 8, "body_x_height_mm": 8}, "blocks": [{"id": "formula", "type": "display_math", "expression": expression}], "unresolved_inputs": []}
math_plan = plan_document(math_document, profile, seed=72, mode="fresh_strokes")
(ASSETS / "mathematics.svg").write_text(svg(math_plan, "Synthetic limit and fraction specimen", "#f4f0e7"))

demo = literal_document("Inkform\nPersonal forms. Fresh ink.\n\nThis is an authored synthetic demonstration.\nA real writer profile begins with your own samples.\n\nKeep the shape. Write something new.\n\n", "inkform-open-source-demo")
demo["blocks"].append({"id": "math-example", "type": "display_math", "expression": copy.deepcopy(expression)})
demo["blocks"].append({"id": "footer", "type": "paragraph", "children": [{"id": "footer-text", "type": "text", "text": "\nPrint this vector PDF at Actual Size / 100%.\nThe public demonstration contains no personal handwriting."}]})
demo["page"] = {"width_mm": 210, "height_mm": 297, "margins_mm": 22, "body_x_height_mm": 3.4}
plan = plan_document(demo, profile, seed=72, mode="fresh_strokes")
export_pdf(plan, profile, ASSETS / "inkform-synthetic-demo.pdf", "Inkform — authored synthetic demonstration")
(ASSETS / "demo-provenance.json").write_text(json.dumps({"source": "examples/synthetic-profile.json", "synthetic": True, "personal_sources": 0, "seed": 72, "representation": "centerline", "pen_width_mm": .38, "width_profile": "constant", "artifacts": ["handwriting.svg", "mathematics.svg", "inkform-synthetic-demo.pdf"]}, indent=2) + "\n")
