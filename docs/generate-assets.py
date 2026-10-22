"""Rebuild the site's public vector specimens from the synthetic profile only.

Run with the project's Python environment. No private profile or sample lookup
is used here; assets are deterministically generated from authored coordinates.
"""

import copy
import html
import json
from pathlib import Path

from inkform.document import literal_document, load_document
from inkform.layout import plan_document, Box, join, map_strokes
from inkform.profiles import load_profile
from inkform.rendering import export_pdf
from inkform.validation import check_pdf
from inkform.repetition import audit_repetition


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "docs" / "assets"
profile = load_profile(ROOT / "examples" / "synthetic-varied-profile.json")
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


def authored_specimen():
    """Lay out reference coordinates directly, without pretending to train.

    Each row picks another authored source construction. This is a reference
    sheet, while the paired output below goes through the actual renderer and
    both independent repetition checks.
    """
    instances = []
    for row in range(3):
        line = Box()
        for position, char in enumerate("letter river minimum"):
            if char == " ":
                line.advance += 4.2
                continue
            forms = [glyph for glyph in profile["glyphs"] if glyph["role"] == "text" and glyph["text"] == char]
            glyph = forms[(row+position//3) % len(forms)]
            item = {"token_id": f"source-row-{row}:{position}", "glyph_id": glyph["glyph_id"],
                    "text": char, "role": "text", "representation": "centerline", "pen_width_mm": .38,
                    "strokes": map_strokes(glyph["strokes"], lambda x,y:(x*5.5,y*5.5))}
            line = join([line, Box(glyph["advance"]*5.5, [item])], gap=.18)
        instances.extend(line.shifted(10, 24+row*25).items)
    return {"page": {"width_mm": 180, "height_mm": 105}, "instances": instances}


note = literal_document("make room for\nsmall changes.\n\n", "inkform-site-specimen")
note["blocks"][0]["children"] += [
    {"id": "note-edit", "type": "correction", "deleted": "perfect", "text": "personal"},
    {"id": "note-ending", "type": "text", "text": "\nwrite something new.\n\none page at a time."},
]
note["page"] = {"width_mm": 120, "height_mm": 145, "margins_mm": 5, "body_x_height_mm": 5.0}
note_plan = plan_document(note, profile, seed=72, mode="fresh_strokes")
(ASSETS / "handwriting.svg").write_text(svg(note_plan, "Synthetic handwriting specimen", profile["pen"]["color"]))

demo = load_document(ROOT / "examples" / "corrections.json")
math_examples = [copy.deepcopy(block["expression"]) for block in demo["blocks"] if block["type"] == "display_math"]
expression = {"type": "row", "items": []}
for example in math_examples:
    if expression["items"]:
        expression["items"].append({"type": "symbol", "value": ";", "role": "math.operator"})
    expression["items"].append(example)
math_document = {"schema_version": "1.0", "document_id": "inkform-site-math", "content_mode": "literal", "page": {"width_mm": 180, "height_mm": 62, "margins_mm": 8, "body_x_height_mm": 5.8}, "blocks": [{"id": "formula", "type": "display_math", "expression": expression}], "unresolved_inputs": []}
math_plan = plan_document(math_document, profile, seed=72, mode="fresh_strokes")
(ASSETS / "mathematics.svg").write_text(svg(math_plan, "Three synthetic limits with varied arrows and preserved directions", "#f4f0e7"))

reference_plan = authored_specimen()
(ASSETS / "comparison-source.svg").write_text(svg(reference_plan, "Authored synthetic source constructions; not a real person's samples", "#19313a"))
comparison = literal_document("letter river minimum\nletter river minimum\nletter river minimum", "inkform-public-comparison")
comparison["page"] = {"width_mm": 180, "height_mm": 105, "margins_mm": 10, "body_x_height_mm": 5.5}
comparison_plan = plan_document(comparison, profile, seed=72, mode="fresh_strokes")
assert comparison_plan["word_patterns"]["status"] == "pass"
assert audit_repetition(comparison_plan["instances"])["near_count"] == 0
(ASSETS / "comparison-output.svg").write_text(svg(comparison_plan, "Generated synthetic words with checked alternate constructions", "#19313a"))

plan = plan_document(demo, profile, seed=72, mode="fresh_strokes")
assert note_plan["page_count"] == math_plan["page_count"] == plan["page_count"] == 1
export_pdf(plan, profile, ASSETS / "inkform-synthetic-demo.pdf", "Inkform — authored synthetic demonstration")
preview_dir = ROOT / "build" / "public-demo-preview"
report = check_pdf(ASSETS / "inkform-synthetic-demo.pdf", plan, preview_dir, preview_dpi=150)
assert report["ok"], report
(preview_dir / "report.json").write_text(json.dumps(report, indent=2) + "\n")
(ASSETS / "demo-provenance.json").write_text(json.dumps({"source": "examples/synthetic-varied-profile.json", "document": "examples/corrections.json", "synthetic": True, "personal_sources": 0, "seed": 72, "representation": "centerline", "pen_width_mm": .38, "width_profile": "constant", "variation": {"amount": .1, "basis": "authored alternate constructions plus bounded occurrence estimates"}, "word_pattern_audit": {"status": plan["word_patterns"]["status"], "compared_words": plan["word_patterns"]["comparison_count"], "repeated_word_count": plan["word_patterns"]["repeated_word_count"]}, "comparison": {"source": "direct layout of authored public reference constructions", "output": "actual renderer; no private profile or input", "word_pattern_status": comparison_plan["word_patterns"]["status"], "repeated_word_comparisons": comparison_plan["word_patterns"]["comparison_count"], "repeated_word_count": comparison_plan["word_patterns"]["repeated_word_count"], "geometry_hash": comparison_plan["geometry_hash"]}, "corrections": "explicit authored examples; no fabricated sample habit", "geometry_hash": plan["geometry_hash"], "artifacts": ["handwriting.svg", "mathematics.svg", "inkform-synthetic-demo.pdf", "comparison-source.svg", "comparison-output.svg"]}, indent=2) + "\n")
