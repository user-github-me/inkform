"""Reviewed graph evidence, semantic constraints and emitted-vector checks."""
import copy
import hashlib
import json
import math
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from inkform.graphs import graph_box, validate_graph_style
from inkform.graph_traces import path_samples, sampled_curve_protected_indices, trace_candidates
from inkform.layout import Shaper, plan_document
from inkform.profiles import load_profile
from inkform.rendering import export_pdf, render_document
from inkform.validation import check_pdf


def graph_profile():
    # Authored unit-test observations exercise the source contract. They are
    # not shipped personal data and never claim to be this user's handwriting.
    profile=copy.deepcopy(load_profile(Path(__file__).parents[1]/"examples/synthetic-profile.json"))
    observation={"observation_id":"reviewed-graph-test","source_id":"graph-test-source","kind":"graph_style",
                 "source_sha256":"a"*64,"bbox_xywh":[0,0,110,110],"review_status":"reviewed",
                 "reviewed_by":"unit-test explicitly authored evidence fixture","xheight_px":10,"pen_width_px":1,
                 "axes":[{"orientation":"horizontal","trace_px":[[0,2],[25,2.8],[50,.8],[75,3.2],[100,2]]},
                         {"orientation":"vertical","trace_px":[[0,0],[4,50],[0,100]]}],
                 "strokes":[{"stroke_id":"ramp","role":"curve","closed":False,
                             "trace_px":[[5,105],[25,84],[45,65],[65,44],[85,25]],"anchors_px":[[5,105],[85,25]]},
                            {"stroke_id":"tick","role":"tick","closed":False,"trace_px":[[30,30],[31,40],[30.5,50]]},
                            {"stroke_id":"open","role":"marker_open","closed":True,"center_px":[50,50],
                             "trace_px":[[61,50],[57,57],[50,59],[43,57],[40,50],[43,44],[50,40],[57,43],[61,50]]}]}
    profile.update(synthetic=False,glyphs=[],sources=[{"source_id":"graph-test-source","sha256":"a"*64}],
                   observations=[observation],variation={"mode":"observed","amount":.3},graph_style={
                       "version":"inkform-graph-style-1","basis":"reviewed_samples","observation_ids":[observation["observation_id"]],
                       "source_refs":[{"source_id":observation["source_id"],"observation_id":observation["observation_id"]}],
                       "pen_width_xheight":.1,"axis_bow_xheight":{"horizontal":.12,"vertical":.4}})
    return profile


def graph_spec():
    return {"width_mm":120,"height_mm":90,"x_range":[0,10],"y_range":[0,10],
            "axes":[{"id":"x","from":[0,0],"to":[10,0],"arrow":False,
                     "ticks":[{"id":"zero","at":[0,0]},{"id":"three","at":[3,0]}, {"id":"seven","at":[7,0]}]},
                    {"id":"y","from":[0,0],"to":[0,10],"arrow":False}],
            "curves":[{"id":"ramp","points":[[1,1],[5,6],[9,7]],"interpolation":"smooth"}],
            "markers":[{"id":"open","at":[5,6],"style":"open","radius_mm":.9},
                       {"id":"filled","at":[9,7],"style":"filled","radius_mm":.9}]}


def document():
    return {"schema_version":"1.0","document_id":"reviewed-graph","content_mode":"literal",
            "blocks":[{"id":"plot","type":"graph","graph":graph_spec()}],"unresolved_inputs":[]}


def box(seed=72,profile=None,spec=None):
    profile=profile or graph_profile()
    writer=Shaper(profile,seed,"fresh_strokes",profile["physical_scale"]["body_x_height_mm"])
    return graph_box(spec or graph_spec(),writer,"plot"),writer


def test_reviewed_source_knots_polarity_and_physical_scale_survive_transfer():
    result,_=box()
    horizontal=next(item for item in result.items if item["token_id"]=="plot.axis.x")
    stroke=horizontal["strokes"][0]
    points=[stroke["start"]]+[segment["end"] for segment in stroke["segments"]]
    assert points == pytest.approx(np.asarray([[12,78],[36,78.24],[60,77.64],[84,78.36],[108,78]]))
    assert horizontal["graph_trace"]["stroke_id"]=="axis-0"
    assert horizontal["variation"]["basis"]=="reviewed_source_trace_transfer"
    assert all(abs(p[1]-78)<=.45 for p in path_samples(stroke))


def test_reviewed_axis_choice_varies_only_between_actual_source_traces():
    profile=graph_profile()
    profile["observations"][0]["axes"].append({"orientation":"horizontal","trace_px":[[0,2],[50,2.5],[100,2]]})
    variants={}
    for seed in range(20):
        result,_=box(seed,profile)
        item=next(item for item in result.items if item["token_id"]=="plot.axis.x")
        variants.setdefault(item["graph_trace"]["stroke_id"],set()).add(json.dumps(item["strokes"],sort_keys=True))
    assert set(variants)=={"axis-0","axis-2"}
    assert all(len(geometries)==1 for geometries in variants.values())
    assert box(5,profile)[0].items==box(5,profile)[0].items


def test_reviewed_curves_keep_exact_anchors_and_no_new_extrema():
    result,_=box()
    curve=next(item for item in result.items if item["graph_kind"]=="curve")
    ends=[curve["strokes"][0]["start"]]+[s["end"] for s in curve["strokes"][0]["segments"]]
    assert all(point in ends for point in curve["graph_plot_anchors_mm"])
    samples=path_samples(curve["strokes"][0])
    assert all(a[0] <= b[0] and a[1] >= b[1] for a,b in zip(samples,samples[1:]))
    graph=graph_spec(); graph["curves"][0].update(points=[[1,1],[5,1],[5,6]],interpolation="linear")
    result,_=box(spec=graph)
    curve=next(item for item in result.items if item["graph_kind"]=="curve")
    samples=path_samples(curve["strokes"][0])
    assert all(abs(p[1]-71.4)<1e-9 or abs(p[0]-60)<1e-9 for p in samples)


def test_reviewed_open_contour_does_not_claim_unsupported_filled_marker_evidence():
    result,writer=box()
    markers={item["graph_element_id"]:item for item in result.items if item["graph_kind"]=="marker"}
    assert len(markers["open"]["strokes"][0]["segments"])==8
    assert markers["open"]["paper_knockout"]=="open_graph_marker"
    assert "graph_trace" not in markers["filled"]
    assert writer.graphs[0]["style"]["unsupported_primitives"]==[{
        "token_id":"plot.marker.filled","kind":"marker","basis":"explicit_graph_geometry",
        "reason":"no reviewed source stroke of the corresponding kind"}]


@pytest.mark.parametrize("mutate",[
    lambda o:o["strokes"][0].pop("anchors_px"),
    lambda o:o["strokes"][0].update(review_status="proposed"),
    lambda o:o["strokes"][1]["trace_px"].__setitem__(1,[500,500]),
    lambda o:o["strokes"][2].update(center_px=[0,0]),
    lambda o:o["strokes"][2].update(closed=False),
])
def test_reviewed_strokes_require_valid_role_specific_source_evidence(mutate):
    from inkform.errors import InkformError
    profile=graph_profile(); mutate(profile["observations"][0])
    with pytest.raises(InkformError): validate_graph_style(profile)


def test_export_reconstructs_reviewed_graph_and_rasterizes_visible_axis_bow(tmp_path):
    result=render_document(document(),graph_profile(),tmp_path/"observed.pdf",preview_dpi=150)
    manifest=json.loads(Path(result["manifest"]).read_text())
    report=json.loads(Path(result["report"]).read_text())
    assert report["ok"] and report["checks"]["emitted_graph_geometry"]=="pass"
    axis=next(item for item in manifest["instances"] if item["token_id"].endswith(".axis.y"))
    start,end=axis["graph_plot_anchors_mm"]
    midpoint=axis["strokes"][0]["segments"][0]["end"]
    assert midpoint[0]-start[0]==pytest.approx(1.2)
    pixels=np.asarray(Image.open(result["previews"][0]).convert("RGB"))
    def area(point):
        x,y=[round(v*150/25.4) for v in point]
        return pixels[y-1:y+2,x-1:x+2]
    assert area(midpoint).mean()<180
    assert area([(start[0]+end[0])/2,(start[1]+end[1])/2]).min()>240
    markers={item["graph_element_id"]:item for item in manifest["instances"] if item.get("graph_kind")=="marker"}
    assert area(markers["open"]["graph_plot_anchors_mm"][0]).min()>240
    assert area(markers["filled"]["graph_plot_anchors_mm"][0]).max()<180


@pytest.mark.parametrize("mutation",["axis_handle","curve_handle","tick_handle","marker_handle","source_binding","remove_reference"])
def test_pdf_and_matching_manifest_cannot_replace_reviewed_source_geometry(tmp_path,mutation):
    profile=graph_profile(); plan=plan_document(document(),profile)
    kind=mutation.split("_")[0] if mutation.endswith("_handle") else "axis"
    item=next(item for item in plan["instances"] if item["graph_kind"]==kind)
    if mutation.endswith("_handle"):
        item["strokes"][0]["segments"][0]["control1"][0]+=.1
    elif mutation=="source_binding":
        item["graph_trace"]["trace_sha256"]="f"*64
    else:
        item.pop("graph_trace")
    from inkform.layout import item_bounds
    item["ink_bounds"]=item_bounds(item)
    plan["geometry_hash"]=hashlib.sha256(json.dumps(plan["instances"],sort_keys=True,separators=(",",":")).encode()).hexdigest()
    pdf=tmp_path/f"{mutation}.pdf"; export_pdf(plan,profile,pdf,"changed source-bound path")
    plan["pdf_sha256"]=hashlib.sha256(pdf.read_bytes()).hexdigest()
    report=check_pdf(pdf,plan,preview_dpi=36)
    assert not report["ok"]
    assert report["checks"]["emitted_graph_geometry"]=="fail"
    assert any(error["code"]=="invalid_graph_inventory" for error in report["errors"])


def sampled_graph(count=101, *, anchors=None, interpolation="linear"):
    points = [[1+8*i/(count-1), 1+8*i/(count-1)] for i in range(count)]
    return {"width_mm": 120, "height_mm": 90, "x_range": [0, 10], "y_range": [0, 10],
            "curves": [{"id": "sampled-ramp", "points": points, "point_mode": "samples",
                        "anchor_indices": anchors or [0, count-1], "interpolation": interpolation}]}


@pytest.mark.parametrize("points", [
    [[0, 0], [1e-12, 0]],
    [[0, 0], [1e-12, 0], [1, 1]],
    [[-1, -1], [0, 0], [1e-12, 0]],
])
def test_sampled_distinct_data_points_can_collapse_to_one_page_point(points):
    graph = sampled_graph(2)
    graph.update(x_range=[-1e8, 1e8], y_range=[-1e8, 1e8])
    graph["curves"][0].update(points=points, anchor_indices=list(range(len(points))))
    result, _ = box(spec=graph)
    curve = result.items[0]
    samples = path_samples(curve["strokes"][0])
    assert all(math.isfinite(value) for point in samples for value in point)
    assert all(anchor in samples for anchor in curve["graph_plot_anchors_mm"])


def ordinate(path, x):
    """Independent evaluation of an exported monotone cubic at page x."""
    from scipy.optimize import brentq
    start = path["start"]
    for segment in path["segments"]:
        end = segment["end"]
        if start[0]-1e-9 <= x <= end[0]+1e-9:
            def value(t, axis):
                q = 1-t
                return (q**3*start[axis]+3*q*q*t*segment["control1"][axis]
                        +3*q*t*t*segment["control2"][axis]+t**3*end[axis])
            t = 0. if x <= start[0] else 1. if x >= end[0] else brentq(lambda t: value(t, 0)-x, 0, 1)
            return value(t, 1)
        start = end
    raise AssertionError("x outside curve")


@pytest.mark.parametrize("interpolation", ["linear", "smooth"])
def test_sample_density_does_not_restart_or_shrink_reviewed_residual(interpolation):
    sparse, _ = box(spec=sampled_graph(2, interpolation=interpolation))
    dense, _ = box(spec=sampled_graph(401, interpolation=interpolation))
    first, second = sparse.items[0], dense.items[0]
    assert first["graph_data_anchors"] == second["graph_data_anchors"] == [[1, 1], [9, 9]]
    a, b = first["graph_plot_anchors_mm"]
    positions = np.linspace(a[0], b[0], 101)
    assert [ordinate(first["strokes"][0], x) for x in positions] == pytest.approx(
        [ordinate(second["strokes"][0], x) for x in positions], abs=1e-8)
    errors = [abs(ordinate(second["strokes"][0], x)-(a[1]+(b[1]-a[1])*(x-a[0])/(b[0]-a[0]))) for x in positions]
    assert max(errors) > .15  # Visible source residual survives 400 sampling intervals.
    # Opting out retains the legacy promise that every supplied point is exact.
    legacy = sampled_graph(401)
    legacy["curves"][0].pop("point_mode"); legacy["curves"][0].pop("anchor_indices")
    old, _ = box(spec=legacy)
    assert len(old.items[0]["graph_data_anchors"]) == 401
    ends = [old.items[0]["strokes"][0]["start"]]+[s["end"] for s in old.items[0]["strokes"][0]["segments"]]
    assert all(p in ends for p in old.items[0]["graph_plot_anchors_mm"])


def test_sampled_curve_pins_declared_anchor_extremum_plateau_and_vertical_segment():
    graph = sampled_graph()
    points = [[0, 0], [1, 1], [2, 2], [3, 1], [4, 0], [5, 0], [5, 1], [5, 2]]
    graph["curves"][0].update(points=points, anchor_indices=[0, 1, 7])
    assert sampled_curve_protected_indices(points, [0, 1, 7]) == [0, 1, 2, 4, 5, 7]
    result, _ = box(spec=graph)
    item = result.items[0]; path = item["strokes"][0]
    ends = [path["start"]]+[s["end"] for s in path["segments"]]
    mapped = lambda p: [12+p[0]*9.6, 78-p[1]*6.6]
    for i in [0, 1, 2, 4, 5, 7]:
        assert any(math.dist(mapped(points[i]), p) < 1e-9 for p in ends)
    samples = path_samples(path)
    assert all(12-.45 <= p[0] <= 60+.45 and 64.8-.45 <= p[1] <= 78+.45 for p in samples)
    for first, last, along, across in [(4, 5, 0, 1), (5, 7, 1, 0)]:
        start = next(i for i, p in enumerate(ends) if math.dist(mapped(points[first]), p) < 1e-9)
        stop = next(i for i, p in enumerate(ends) if math.dist(mapped(points[last]), p) < 1e-9)
        section = {"start": ends[start], "segments": path["segments"][start:stop], "closed": False}
        traced = path_samples(section)
        errors = [abs(p[across]-ends[start][across]) for p in traced]
        assert .005 < max(errors) <= .45
        direction = 1 if ends[stop][along] > ends[start][along] else -1
        assert all((b[along]-a[along])*direction > 0 for a, b in zip(traced, traced[1:]))
    assert graph["curves"][0]["points"] == points  # Constant mathematical values stay explicit.


@pytest.mark.parametrize("change", [
    lambda c: c.pop("anchor_indices"),
    lambda c: c.update(anchor_indices=[1, 100]),
    lambda c: c.update(anchor_indices=[0, 99]),
    lambda c: c.update(anchor_indices=[0, 50, 50, 100]),
    lambda c: c.update(anchor_indices=[0, True, 100]),
    lambda c: c.update(anchor_indices=[0, 100.0]),
    lambda c: c.update(anchor_indices=[0, 101]),
    lambda c: c.update(point_mode="anchors"),
    lambda c: c.update(point_mode="implicit"),
])
def test_sampled_curve_requires_an_explicit_valid_anchor_contract(change):
    from inkform.errors import InkformError
    from inkform.graphs import validate_graph
    graph = sampled_graph(); change(graph["curves"][0])
    with pytest.raises(InkformError) as error:
        validate_graph(graph)
    assert error.value.code == "invalid_graph"


def test_sampled_curve_pdf_audits_baseline_and_exact_semantic_anchor(tmp_path):
    graph = sampled_graph(101, anchors=[0, 50, 100])
    doc = document(); doc["blocks"][0]["graph"] = graph
    result = render_document(doc, graph_profile(), tmp_path/"sampled.pdf", preview_dpi=150)
    manifest = json.loads(Path(result["manifest"]).read_text())
    report = json.loads(Path(result["report"]).read_text())
    assert report["ok"] and report["checks"]["emitted_graph_geometry"] == "pass"
    item = manifest["instances"][0]
    assert item["graph_data_anchors"] == [[1, 1], [5, 5], [9, 9]]
    ends = [item["strokes"][0]["start"]]+[s["end"] for s in item["strokes"][0]["segments"]]
    assert all(p in ends for p in item["graph_plot_anchors_mm"])
    # The PDF audit reconstructs samples from the original graph specification;
    # changing both the emitted path and manifest handles cannot replace them.
    item["strokes"][0]["segments"][1]["control1"][1] += .1
    from inkform.layout import item_bounds
    item["ink_bounds"] = item_bounds(item)
    manifest["geometry_hash"] = hashlib.sha256(json.dumps(manifest["instances"], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    altered = tmp_path/"changed-sampled.pdf"
    export_pdf(manifest, graph_profile(), altered, "changed sampled path")
    manifest["pdf_sha256"] = hashlib.sha256(altered.read_bytes()).hexdigest()
    changed = check_pdf(altered, manifest, preview_dpi=36)
    assert changed["checks"]["emitted_graph_geometry"] == "fail"


def test_sampled_curve_without_source_keeps_explicit_baseline(tmp_path):
    profile = graph_profile()
    profile["observations"][0]["strokes"] = []
    doc = document(); doc["blocks"][0]["graph"] = sampled_graph()
    result = render_document(doc, profile, tmp_path/"no-source.pdf", preview_dpi=36)
    report = json.loads(Path(result["report"]).read_text())
    assert report["ok"] and report["checks"]["emitted_graph_geometry"] == "pass"


def test_sample_limit_allows_inserted_intersections_without_changing_legacy_limit():
    from inkform.errors import InkformError
    from inkform.graphs import validate_graph
    assert validate_graph(sampled_graph(2048))
    with pytest.raises(InkformError):
        validate_graph(sampled_graph(2049))
    graph = sampled_graph(513)
    graph["curves"][0].pop("point_mode"); graph["curves"][0].pop("anchor_indices")
    with pytest.raises(InkformError):
        validate_graph(graph)


def test_shallow_sampled_curve_reduces_source_scale_without_manufacturing_flat_tails():
    results = []
    for count in [2, 101, 401]:
        graph = sampled_graph(count)
        for i, point in enumerate(graph["curves"][0]["points"]):
            point[1] = 1+.1*i/(count-1)
        result, _ = box(spec=graph)
        path = result.items[0]["strokes"][0]
        samples = path_samples(path)
        assert all(a[1] > b[1] for a, b in zip(samples, samples[1:]))
        a, b = result.items[0]["graph_plot_anchors_mm"]
        errors = [abs(p[1]-(a[1]+(b[1]-a[1])*(p[0]-a[0])/(b[0]-a[0]))) for p in samples]
        assert max(errors) > .04  # Attenuation retains visible source variation.
        results.append([ordinate(path, x) for x in np.linspace(a[0], b[0], 101)])
    assert results[0] == pytest.approx(results[1], abs=1e-8)
    assert results[0] == pytest.approx(results[2], abs=1e-8)
