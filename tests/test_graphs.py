"""Graph data survives handwritten rendering without invented plot values."""
import copy
import hashlib
import json
import math
from pathlib import Path

import pytest
import numpy as np
from PIL import Image

from inkform.errors import InkformError
from inkform.graphs import graph_box, graph_requirements, graph_text, validate_graph, validate_graph_style
from inkform.layout import Shaper
from inkform.profiles import load_profile
from inkform.rendering import render_document


def spec():
    return {"width_mm": 120, "height_mm": 90, "x_range": [0, 10], "y_range": [0, 20],
            "axes": [{"id": "x", "from": [0, 0], "to": [10, 0], "arrow": True,
                      "ticks": [{"id": "two", "at": [2, 0], "label": "2"}]},
                     {"id": "y", "from": [0, 0], "to": [0, 20], "arrow": True}],
            "curves": [{"id": "data", "points": [[0, 4], [3, 8], [8, 12]], "interpolation": "smooth"}],
            "markers": [{"id": "start", "at": [0, 4], "style": "open"},
                        {"id": "end", "at": [8, 12], "style": "filled"}],
            "labels": [{"id": "cost", "at": [0, 20], "text": "Cost", "offset_mm": [0, -2]}]}


def shaper(seed=72, *, variation=0, variation_mode="observed"):
    profile = load_profile(Path(__file__).parents[1] / "examples/synthetic-profile.json")
    profile = copy.deepcopy(profile)
    # These tests establish plotting semantics. Repetition policy is exercised
    # by its own tests; turn it on explicitly for the graph-variation test.
    profile["variation"] = {"amount": variation, "mode": variation_mode}
    return Shaper(profile, seed, "fresh_strokes", profile["physical_scale"]["body_x_height_mm"])


def test_graph_preserves_explicit_curve_anchors_and_ledger():
    graph = spec(); writer = shaper()
    box = graph_box(graph, writer, "chart")
    curve = next(item for item in box.items if item["role"] == "graph.curve")
    path = curve["strokes"][0]
    actual = [path["start"]] + [segment["end"] for segment in path["segments"]]
    assert actual == curve["graph_plot_anchors_mm"]
    assert curve["graph_data_anchors"] == graph["curves"][0]["points"]
    assert writer.graphs[0]["spec"] == graph
    assert len(writer.graphs[0]["primitive_token_ids"]) == 6
    assert len(writer.graphs[0]["arrow_token_ids"]) == 2
    assert all(item["semantic"] is False for item in box.items if item["role"].startswith("graph."))


def test_only_labels_and_sampled_arrows_require_glyphs():
    assert graph_requirements(spec()) == [("math.operator", "→"), ("text", "2"), ("math.operator", "→"),
                                           ("text", "C"), ("text", "o"), ("text", "s"), ("text", "t")]


def test_arrowheads_keep_writer_pen_weight_and_are_rotated_not_stretched():
    writer = shaper(); box = graph_box(spec(), writer, "chart")
    arrows = [item for item in box.items if item.get("graph_kind") == "arrow"]
    assert len(arrows) == 2
    assert [item["graph_axis_direction"] for item in arrows] == [[1, 0], [0, -1]]
    assert all(item["pen_width_mm"] == pytest.approx(writer.profile["pen"]["width_mm"]) for item in arrows)
    assert all(item["source_refs"] == [] for item in arrows)  # The fixture is explicitly synthetic.
    assert all(item["text"] == "→" and item["role"] == "math.operator" for item in arrows)


def test_varying_a_graph_keeps_data_and_monotone_envelopes():
    graph = spec(); graph["axes"] = []; graph["labels"] = []; graph["markers"] = []
    first = graph_box(graph, shaper(1, variation=.1, variation_mode="experimental"), "chart")
    second = graph_box(graph, shaper(2, variation=.1, variation_mode="experimental"), "chart")
    a, b = first.items[0], second.items[0]
    assert a["strokes"] != b["strokes"]
    assert a["graph_plot_anchors_mm"] == b["graph_plot_anchors_mm"]
    for item in (a, b):
        current = item["strokes"][0]["start"]
        for segment in item["strokes"][0]["segments"]:
            end = segment["end"]
            for key in ("control1", "control2"):
                for axis in (0, 1):
                    assert min(current[axis], end[axis]) <= segment[key][axis] <= max(current[axis], end[axis])
            for axis in (0, 1):
                assert (segment["control2"][axis] - segment["control1"][axis]) * (end[axis] - current[axis]) >= 0
            current = end


def test_observed_mode_does_not_invent_axis_bow_curve_deviations_or_marker_shapes():
    graph = spec()
    one, two = shaper(1, variation=.3), shaper(2, variation=.3)
    one.profile.update(observed_style()); two.profile.update(observed_style())
    first, second = graph_box(graph, one, "chart"), graph_box(graph, two, "chart")
    a = {item["token_id"]: item for item in first.items if item.get("content_kind") == "graph_primitive"}
    b = {item["token_id"]: item for item in second.items if item.get("content_kind") == "graph_primitive"}
    assert {key: value["strokes"] for key, value in a.items()} == {key: value["strokes"] for key, value in b.items()}
    assert one.graphs[0]["style"]["estimated_axes"] == []
    assert one.graphs[0]["style"]["maximum_control_deviation_mm"] == 0
    assert one.graphs[0]["style"]["pen_basis"] == "reviewed_graph_ink_width"
    assert one.graphs[0]["style"]["axis_bow_basis"] == "explicit_graph_geometry"


def test_observed_graph_rejects_extra_bend_even_when_pdf_matches_manifest(tmp_path):
    from inkform.layout import plan_document
    from inkform.rendering import export_pdf
    from inkform.validation import check_pdf

    profile = shaper(variation=.3).profile
    plan = plan_document(_marker_crossing_document(), profile)
    axis = next(item for item in plan["instances"] if item["role"] == "graph.axis")
    axis["strokes"][0]["segments"][0]["control1"][0] += .3
    pdf = tmp_path / "invented-bend.pdf"
    export_pdf(plan, profile, pdf, "invented bend")
    plan["pdf_sha256"] = hashlib.sha256(pdf.read_bytes()).hexdigest()
    report = check_pdf(pdf, plan, preview_dpi=36)
    assert not report["ok"]
    assert report["checks"]["emitted_graph_geometry"] == "fail"
    assert any("invented bends" in error.get("reason", "") for error in report["errors"])


def test_plateau_and_vertical_data_segments_do_not_gain_fake_extrema():
    graph = spec(); graph.update(axes=[], markers=[], labels=[], curves=[
        {"id": "piecewise", "points": [[1, 4], [5, 4], [5, 12]], "interpolation": "linear"}])
    curve = graph_box(graph, shaper(variation=.3), "chart").items[0]["strokes"][0]
    horizontal, vertical = curve["segments"]
    assert horizontal["control1"][1] == horizontal["control2"][1] == horizontal["end"][1]
    assert vertical["control1"][0] == vertical["control2"][0] == vertical["end"][0]


def test_open_and_filled_markers_remain_distinct_and_centered():
    graph = spec(); graph.update(axes=[], curves=[], labels=[])
    box = graph_box(graph, shaper(variation=.1), "chart")
    assert [item["representation"] for item in box.items] == ["centerline", "clean_outline"]
    assert box.items[0]["paper_knockout"] == "open_graph_marker"
    assert "paper_knockout" not in box.items[1]
    for item in box.items:
        stroke = item["strokes"][0]; center = item["graph_plot_anchors_mm"][0]
        opposing = stroke["segments"][1]["end"]
        assert [(a + b) / 2 for a, b in zip(stroke["start"], opposing)] == pytest.approx(center)
        assert stroke["closed"] is True


@pytest.mark.parametrize("change", [
    lambda g: g.update(equation="invented"),
    lambda g: g.update(x_range=[0, 0]),
    lambda g: g.update(y_range=[0, math.nan]),
    lambda g: g["curves"][0].update(points=[[0, 1], [11, 2]]),
    lambda g: g["curves"][0].update(points=[[1, 1], [1, 2]]),
    lambda g: g["axes"][0]["ticks"][0].update(at=[2, 3]),
    lambda g: g["markers"][0].update(style="maybe"),
    lambda g: g["labels"][0].update(id="data"),
])
def test_graph_rejects_ambiguous_or_invalid_semantics(change):
    graph = spec(); change(graph)
    with pytest.raises(InkformError) as error:
        validate_graph(graph)
    assert error.value.code == "invalid_graph"


def test_qualitative_graph_is_explicit_in_semantic_text():
    graph = spec(); graph["coordinate_kind"] = "qualitative"
    assert graph_text(graph).startswith("[qualitative graph:")
    assert '"points":[[0,4],[3,8],[8,12]]' in graph_text(graph)


def observed_style():
    observation = {"observation_id": "graph-observation", "kind": "graph_style", "source_id": "source-graph",
                   "source_sha256": "a" * 64, "bbox_xywh": [0, 0, 110, 110], "review_status": "reviewed",
                   "reviewed_by": "unit-test explicit reviewed trace", "xheight_px": 10, "pen_width_px": 2,
                   "axes": [{"orientation": "horizontal", "trace_px": [[0, 0], [50, 1], [100, 0]]},
                            {"orientation": "vertical", "trace_px": [[0, 0], [4, 50], [0, 100]]}]}
    return {"synthetic": False, "sources": [{"source_id": "source-graph", "sha256": "a" * 64}],
            "observations": [observation], "graph_style": {
                "version": "inkform-graph-style-1", "basis": "reviewed_samples", "observation_ids": ["graph-observation"],
                "source_refs": [{"source_id": "source-graph", "observation_id": "graph-observation"}],
                "pen_width_xheight": .2, "axis_bow_xheight": {"horizontal": .1, "vertical": .4}}}


def test_graph_style_model_is_derived_from_reviewed_static_measurements():
    profile = observed_style()
    assert validate_graph_style(profile) == profile["graph_style"]


@pytest.mark.parametrize("change", [
    lambda p: p["graph_style"].update(pen_width_xheight=.25),
    lambda p: p["graph_style"]["axis_bow_xheight"].update(vertical=.5),
    lambda p: p["observations"][0].update(source_sha256="b" * 64),
    lambda p: p["observations"][0].update(review_status="proposed"),
    lambda p: p["graph_style"].update(source_refs=None),
    lambda p: p.update(synthetic=True),
])
def test_graph_style_rejects_changed_measurements_or_unreviewed_evidence(change):
    profile = observed_style(); change(profile)
    with pytest.raises(InkformError) as error:
        validate_graph_style(profile)
    assert error.value.code == "invalid_profile"


def test_measured_bow_keeps_every_tick_on_the_visible_axis():
    writer = shaper(variation=.1); writer.profile.update(observed_style())
    graph = spec(); graph.update(curves=[], markers=[], labels=[])
    graph["axes"][0]["ticks"][0].pop("label")
    for axis in graph["axes"]: axis["arrow"] = False
    box = graph_box(graph, writer, "chart")
    for item in box.items:
        if item["graph_kind"] != "axis": continue
        stroke = item["strokes"][0]
        assert [stroke["start"]] + [segment["end"] for segment in stroke["segments"]] == item["graph_plot_anchors_mm"]
        assert item["pen_width_mm"] == pytest.approx(.2 * writer.size)
        assert item["source_refs"] == writer.profile["graph_style"]["source_refs"]


def _marker_crossing_document():
    graph = spec()
    graph.update(axes=[{"id": "vertical", "from": [5, 0], "to": [5, 20], "arrow": False}],
                 curves=[{"id": "horizontal", "points": [[0, 10], [10, 10]], "interpolation": "linear"}],
                 markers=[{"id": "excluded", "at": [5, 10], "style": "open", "radius_mm": .9},
                          {"id": "included", "at": [8, 10], "style": "filled", "radius_mm": .9}], labels=[])
    return {"schema_version": "1.0", "document_id": "crossed-open-marker", "content_mode": "literal",
            "blocks": [{"id": "plot", "type": "graph", "graph": graph}], "unresolved_inputs": []}


def test_export_open_marker_covers_curve_and_axis_with_paper(tmp_path):
    result = render_document(_marker_crossing_document(), shaper().profile, tmp_path / "marker.pdf", mode="fresh_strokes")
    manifest = json.loads(Path(result["manifest"]).read_text())
    report = json.loads(Path(result["report"]).read_text())
    assert report["checks"]["emitted_graph_geometry"] == "pass"
    pixels = np.asarray(Image.open(result["previews"][0]).convert("RGB"))
    scale = report["pages"][0]["preview_dpi"] / 25.4
    markers = {item["graph_element_id"]: item for item in manifest["instances"] if item.get("graph_kind") == "marker"}

    def neighborhood(point):
        x, y = [round(v * scale) for v in point]
        return pixels[y - 1:y + 2, x - 1:x + 2]

    center = markers["excluded"]["graph_plot_anchors_mm"][0]
    assert neighborhood(center).min() >= 250  # Paper survives both crossing paths.
    assert neighborhood([center[0] + .9, center[1]]).mean() < 180  # The open outline remains.
    assert neighborhood([center[0] - 2, center[1]]).mean() < 180  # Curve outside the point remains.
    assert neighborhood([center[0], center[1] - 2]).mean() < 180  # Axis outside the point remains.
    assert neighborhood(markers["included"]["graph_plot_anchors_mm"][0]).max() < 180


def test_export_rejects_an_open_marker_without_its_white_paper_fill(tmp_path):
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import ContentStream
    from inkform.validation import check_pdf

    result = render_document(_marker_crossing_document(), shaper().profile, tmp_path / "marker.pdf", mode="fresh_strokes")
    reader = PdfReader(result["pdf"])
    writer = PdfWriter(clone_from=reader)
    stream = ContentStream(writer.pages[0].get_contents(), writer)
    # Replace B* (paper fill followed by stroke) with S (stroke only). Both
    # files have the same outline geometry; only the visible exclusion changes.
    changed = False
    for index, (operands, operator) in enumerate(stream.operations):
        if operator == b"B*":
            stream.operations[index] = (operands, b"S"); changed = True; break
    assert changed
    writer.pages[0].replace_contents(stream)
    altered = tmp_path / "no-paper.pdf"
    with altered.open("wb") as handle: writer.write(handle)
    manifest = json.loads(Path(result["manifest"]).read_text())
    manifest["pdf_sha256"] = hashlib.sha256(altered.read_bytes()).hexdigest()
    report = check_pdf(altered, manifest)
    assert report["ok"] is False
    assert report["checks"]["emitted_glyph_geometry"] == "fail" or report["checks"].get("emitted_graph_geometry") == "fail"


def test_open_marker_rejects_radius_that_its_pen_would_fill():
    graph = spec(); graph.update(axes=[], curves=[], labels=[], markers=[{"id": "too-small", "at": [5, 10], "style": "open", "radius_mm": .3}])
    writer = shaper(); writer.profile["pen"]["width_mm"] = .7
    with pytest.raises(InkformError) as error:
        graph_box(graph, writer, "plot")
    assert error.value.code == "graph_marker_too_small"
