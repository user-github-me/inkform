"""Graph data and emitted vector coordinates stay bound through PDF export."""
import copy
import hashlib
import json
from pathlib import Path

import pytest

from inkform.layout import map_strokes, plan_document
from inkform.profiles import load_profile
from inkform.rendering import export_pdf, render_document
from inkform.validation import check_pdf


@pytest.fixture
def profile():
    return load_profile(Path(__file__).resolve().parents[1]/"examples"/"synthetic-profile.json")


def document():
    return {"schema_version": "1.0", "document_id": "graph-proof", "content_mode": "literal",
            "unresolved_inputs": [], "blocks": [{"id": "plot", "type": "graph", "graph": {
                "width_mm": 120, "height_mm": 90, "x_range": [-1, 3], "y_range": [0, 4],
                "axes": [{"id": "x", "from": [-1, 0], "to": [3, 0]},
                         {"id": "y", "from": [0, 0], "to": [0, 4]}],
                "curves": [{"id": "values", "points": [[-1, 1], [1, 3], [3, 2]], "interpolation": "linear"}],
                "markers": [{"id": "open", "at": [1, 3], "style": "open"},
                            {"id": "filled", "at": [1, 2], "style": "filled"}],
                "labels": [{"id": "x-name", "text": "x", "at": [3, 0], "offset_mm": [0, 5]},
                           {"id": "y-name", "text": "y", "at": [0, 4], "offset_mm": [3, 0]}]
            }}]}


def test_graph_export_accounts_for_data_primitives_and_actual_pdf_paths(profile, tmp_path):
    result = render_document(document(), profile, tmp_path/"graph.pdf", preview_dpi=36)
    manifest = json.loads(Path(result["manifest"]).read_text())
    report = json.loads(Path(result["report"]).read_text())
    assert report["ok"]
    for check in ("graph_data_accounted_for", "emitted_graph_geometry", "emitted_glyph_geometry", "vector_handwriting"):
        assert report["checks"][check] == "pass"
    assert manifest["required_symbol_count"] == manifest["rendered_symbol_count"] == 4
    assert manifest["visible_symbol_count"] == 9
    assert len(report["repetition"]["excluded_graph_primitive_tokens"]) == 5
    assert report["repetition"]["checked_instance_count"] == 4  # Labels and sampled arrows remain audited.
    assert manifest["graphs"][0]["spec"] == document()["blocks"][0]["graph"]
    assert report["handwriting_raster_objects"] == report["font_text_operators"] == 0


@pytest.mark.parametrize("mutation", ["translate_curve", "move_anchor", "fill_open_marker"])
def test_emitted_graph_cannot_move_data_or_change_marker_semantics(profile, tmp_path, mutation):
    original = plan_document(document(), profile)
    changed = copy.deepcopy(original)
    curve = next(item for item in changed["instances"] if item["role"] == "graph.curve")
    if mutation == "translate_curve":
        # Translation alone is invisible to the glyph novelty metric. The
        # independent graph check must use physical PDF coordinates.
        curve["strokes"] = map_strokes(curve["strokes"], lambda x,y:(x+1,y))
    elif mutation == "move_anchor":
        curve["strokes"][0]["segments"][0]["end"][1] += 1
    else:
        marker = next(item for item in changed["instances"] if item["token_id"].endswith(".marker.open"))
        marker["representation"] = "clean_outline"
        marker.pop("paper_knockout")
    path = tmp_path/f"{mutation}.pdf"
    export_pdf(changed, profile, path, "graph-test")
    manifest = {**original, "pdf_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    report = check_pdf(path, manifest, preview_dpi=36)
    assert report["checks"]["emitted_graph_geometry"] == "fail"
    assert report["checks"]["emitted_glyph_geometry"] == "fail"
    assert any(error["code"] == "invalid_graph_inventory" for error in report["errors"])


def test_undeclared_graph_flags_cannot_bypass_ink_accounting(profile, tmp_path):
    plan = plan_document(document(), profile)
    path = tmp_path/"missing-ledger.pdf"
    export_pdf(plan, profile, path, "graph-test")
    plan["graphs"] = []
    plan["pdf_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    report = check_pdf(path, plan, preview_dpi=36)
    assert not report["ok"]
    assert report["checks"]["graph_data_accounted_for"] == "fail"
    assert report["checks"]["emitted_graph_geometry"] == "fail"
    assert any(error["code"] == "invalid_graph_inventory" for error in report["errors"])


def test_open_marker_cannot_hide_extra_graph_area_even_with_matching_manifest(profile, tmp_path):
    plan = plan_document(document(), profile)
    marker = next(item for item in plan["instances"] if item["token_id"].endswith(".marker.open"))
    cx, cy = marker["graph_plot_anchors_mm"][0]
    marker["strokes"] = map_strokes(marker["strokes"], lambda x,y:(cx+4*(x-cx), cy+4*(y-cy)))
    from inkform.layout import item_bounds
    marker["ink_bounds"] = item_bounds(marker)
    plan["geometry_hash"] = hashlib.sha256(json.dumps(plan["instances"], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    path = tmp_path/"oversized-knockout.pdf"
    export_pdf(plan, profile, path, "graph-test")
    plan["pdf_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    report = check_pdf(path, plan, preview_dpi=36)
    assert not report["ok"]
    assert report["checks"]["graph_data_accounted_for"] == "fail"
    assert report["checks"]["emitted_graph_geometry"] == "fail"
    assert any("radii" in error.get("reason", "") for error in report["errors"])
