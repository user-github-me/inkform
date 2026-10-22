"""A radical's roof must meet real path endpoints, independent of crop padding."""
import copy
import math
from pathlib import Path

import pytest

from inkform.layout import plan_document
from inkform.profiles import load_profile
from inkform.rendering import render_document


def ends(item):
    return [p for stroke in item["strokes"] for p in
            (stroke["start"], stroke["segments"][-1]["end"])]


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("body", [
    {"type": "number", "value": "1296"},
    {"type": "fraction", "numerator": {"type": "number", "value": "123"},
     "denominator": {"type": "number", "value": "4"}},
    {"type": "root", "body": {"type": "number", "value": "16"}},
])
def test_root_roof_meets_hook_across_sizes_and_source_directions(tmp_path, reverse, body):
    profile = load_profile(Path(__file__).resolve().parents[1]/"examples/synthetic-profile.json")
    for glyph in profile["glyphs"]:
        if glyph["role"] == "math.operator" and glyph["text"] in {"√", "−"}:
            glyph["advance"] += 4  # isolated crop padding must not open a gap
            if reverse:
                for stroke in glyph["strokes"]:
                    points = [stroke["start"], *[seg["end"] for seg in stroke["segments"]]]
                    if all(seg["type"] == "line" for seg in stroke["segments"]):
                        points.reverse()
                        stroke["start"] = points[0]
                        stroke["segments"] = [{"type": "line", "end": p} for p in points[1:]]
    document = {"schema_version": "1.0", "document_id": "joined-root", "content_mode": "literal",
                "blocks": [{"id": "root", "type": "display_math",
                            "expression": {"type": "root", "body": copy.deepcopy(body)}}],
                "unresolved_inputs": []}
    for size in (2., 3., 4.):
        document["page"] = {"body_x_height_mm": size}
        plan = plan_document(document, profile, seed=31, mode="fresh_strokes")
        items = {item["token_id"]: item for item in plan["instances"]}
        for bar in items.values():
            if bar.get("construction") != "joined_writer_root_bar":
                continue
            radical = items[bar["root_radical_token_id"]]
            assert min(math.dist(a, b) for a in ends(radical) for b in ends(bar)) < 1e-8
    result = render_document(document, profile, tmp_path/"roots.pdf", seed=31, mode="fresh_strokes")
    assert result["pages"] == 1
