"""Direct vector PDF export with independently rasterized preview artifacts."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import tempfile

from reportlab.lib.colors import HexColor
from reportlab.pdfgen import canvas

from . import __version__
from .document import plain_text
from .errors import InkformError
from .layout import plan_document
from .profiles import accepted_glyphs, resolve_render_mode
from .validation import check_pdf

PT = 72 / 25.4


def _path(c, stroke, height, p=None):
    p = c.beginPath() if p is None else p
    def xy(point):
        return point[0] * PT, (height - point[1]) * PT
    p.moveTo(*xy(stroke["start"]))
    for seg in stroke["segments"]:
        if seg["type"] == "line":
            p.lineTo(*xy(seg["end"]))
        else:
            p.curveTo(*xy(seg["control1"]), *xy(seg["control2"]), *xy(seg["end"]))
    if stroke.get("closed"):
        p.close()
    return p


def _sample_stroke(stroke):
    points = [stroke["start"]]
    start = stroke["start"]
    for seg in stroke["segments"]:
        controls = [start, seg["end"]] if seg["type"] == "line" else [start, seg["control1"], seg["control2"], seg["end"]]
        length = sum(math.dist(a, b) for a, b in zip(controls, controls[1:]))
        n = max(1, min(5000, math.ceil(length / 0.09)))
        for i in range(1, n + 1):
            t, u = i / n, 1 - i / n
            if len(controls) == 2:
                points.append([u * start[k] + t * seg["end"][k] for k in range(2)])
            else:
                points.append([u**3 * controls[0][k] + 3*u*u*t * controls[1][k] + 3*u*t*t * controls[2][k] + t**3 * controls[3][k] for k in range(2)])
        start = seg["end"]
    if stroke.get("closed") and points[-1] != points[0]:
        points.append(points[0])
    return points


def _width_at(samples, s):
    for a, b in zip(samples, samples[1:]):
        if s <= b["s"]:
            t = (s - a["s"]) / (b["s"] - a["s"])
            return a["multiplier"] * (1-t) + b["multiplier"] * t
    return samples[-1]["multiplier"]


def draw_instance(c, item, height):
    c.setLineCap(1); c.setLineJoin(1)
    if "paper_knockout" in item:
        paths = item.get("strokes", [])
        if (item["paper_knockout"] != "open_graph_marker" or item.get("role") != "graph.marker"
                or item.get("content_kind") != "graph_primitive" or item.get("semantic") is not False
                or item.get("representation") != "centerline" or len(paths) != 1
                or not paths[0].get("closed") or len(paths[0]["segments"]) != 4
                or any(segment["type"] != "cubic" for segment in paths[0]["segments"])):
            raise InkformError("invalid_paper_knockout", "Paper knockouts are reserved for explicit closed open-graph markers.")
        # A single B* paints the paper interior before the same path's pen
        # outline. PDF validation checks this operation and its bounded marker
        # geometry, so a curve cannot silently show through the open point.
        c.saveState()
        c.setFillColorRGB(1, 1, 1)
        c.setLineWidth(item["pen_width_mm"] * PT)
        c.drawPath(_path(c, paths[0], height), fill=1, stroke=1, fillMode=0)
        c.restoreState()
        return
    if item["representation"] == "clean_outline":
        combined = c.beginPath()
        for stroke in item["strokes"]:
            _path(c, stroke, height, combined)
        c.drawPath(combined, fill=1, stroke=0, fillMode=0)
        return
    for stroke in item["strokes"]:
        samples = stroke.get("width_profile", [{"s": 0, "multiplier": 1}, {"s": 1, "multiplier": 1}])
        if len({p["multiplier"] for p in samples}) == 1:
            c.setLineWidth(item["pen_width_mm"] * samples[0]["multiplier"] * PT)
            if not stroke["segments"]:
                x, y = stroke["start"]
                c.circle(x * PT, (height-y) * PT, item["pen_width_mm"] * samples[0]["multiplier"] * PT / 2, fill=1, stroke=0)
            else:
                c.drawPath(_path(c, stroke, height), fill=0, stroke=1)
            continue
        points = _sample_stroke(stroke)
        lengths = [math.dist(a, b) for a, b in zip(points, points[1:])]
        total, pos = sum(lengths), 0
        if total < 1e-8:
            x, y = points[0]
            c.circle(x*PT, (height-y)*PT, item["pen_width_mm"] * samples[0]["multiplier"] * PT / 2, stroke=0, fill=1)
            continue
        for a, b, length in zip(points, points[1:], lengths):
            multiplier = _width_at(samples, (pos + length/2) / total)
            c.setLineWidth(item["pen_width_mm"] * multiplier * PT)
            c.line(a[0]*PT, (height-a[1])*PT, b[0]*PT, (height-b[1])*PT)
            pos += length


def export_pdf(plan, profile, path, title):
    page = plan["page"]
    c = canvas.Canvas(str(path), pagesize=(page["width_mm"] * PT, page["height_mm"] * PT), invariant=1, pageCompression=1)
    c.setTitle(title); c.setAuthor("Inkform")
    c.setCreator(f"Inkform {__version__}")
    c.setViewerPreference("PrintScaling", "None")
    for page_index in range(plan["page_count"]):
        color = HexColor(profile["pen"]["color"])
        c.setStrokeColor(color); c.setFillColor(color)
        for item in plan["instances"]:
            if item["page"] == page_index:
                # Ordinal marked-content groups let validation inspect the
                # emitted paths without embedding text or private provenance.
                c._code.append("/InkformGlyph BMC")
                draw_instance(c, item, page["height_mm"])
                c._code.append("EMC")
        c.showPage()
    c.save()


def _json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")


def render_document(document, profile, output, seed=72, mode=None, *, preview_dpi=300):
    output = Path(output).expanduser().resolve()
    if output.suffix.lower() != ".pdf":
        raise InkformError("invalid_output", "Output must have a .pdf extension.")
    mode = resolve_render_mode(profile, mode)
    plan = plan_document(document, profile, seed, mode)
    output.parent.mkdir(parents=True, exist_ok=True)
    stem = output.stem
    with tempfile.TemporaryDirectory(prefix=".inkform-", dir=output.parent) as tmp:
        stage = Path(tmp)
        pdf = stage / output.name
        export_pdf(plan, profile, pdf, document["document_id"])
        manifest = {
            "schema_version": "1.0", "document_id": document["document_id"],
            "profile_id": profile["profile_id"], "profile_version": profile["profile_version"],
            "profile_sha256": hashlib.sha256(json.dumps(profile, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            "document_sha256": hashlib.sha256(json.dumps(document, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            "renderer_version": __version__, "seed": seed, "render_mode": mode,
            "emitted_glyph_groups": {"version": "inkform-ordinal-paths-1", "count": len(plan["instances"])},
            "synthetic_profile": profile["synthetic"], "pdf_sha256": hashlib.sha256(pdf.read_bytes()).hexdigest(),
            "representations_used": sorted({i["representation"] for i in plan["instances"]}),
            "unreported_substitutions": 0, "missing_glyphs": [],
            "unresolved_inputs": document.get("unresolved_inputs", []),
            "content_mode": document["content_mode"],
            "content_verification": document.get("verification", {"status": "not_supplied", "note": "Renderer preserves the semantic input; it does not solve or independently verify answers."}),
            **plan,
        }
        report = check_pdf(pdf, manifest, stage / "previews", preview_dpi=preview_dpi)
        report["checks"]["content_accounted_for"] = "pass_with_declared_missing_input" if manifest["unresolved_inputs"] else "pass"
        report["checks"]["semantic_sidecar"] = "exact_input_preserved"
        report["checks"]["style_review"] = "pending_user_feedback"
        report["checks"]["print_proof"] = "pending_actual_size_print"
        report["pdf_path"] = str(output)
        if not report["ok"]:
            raise InkformError("export_validation_failed", "Exported PDF failed validation; no deliverable was replaced.", report)
        previews = []
        for index, preview in enumerate(sorted((stage / "previews").glob("*.png")), 1):
            target = output.parent / f"{stem}-page-{index:03d}.png"
            os.replace(preview, target)
            previews.append(str(target))
        report["previews"] = previews
        for page_report, preview in zip(report.get("pages", []), previews):
            if "preview" in page_report: page_report["preview"] = preview
            if "preview_path" in page_report: page_report["preview_path"] = preview
        manifest["checks"] = report["checks"]
        _json(stage / f"{stem}.build.json", manifest)
        _json(stage / f"{stem}.document.json", document)
        _json(stage / f"{stem}.report.json", report)
        (stage / f"{stem}.txt").write_text(plain_text(document), encoding="utf-8")
        for artifact in stage.iterdir():
            if artifact.is_file():
                os.replace(artifact, output.parent / artifact.name)
    return {"ok": True, "pdf": str(output), "pages": plan["page_count"], "previews": previews,
            "manifest": str(output.with_suffix(".build.json")), "report": str(output.with_suffix(".report.json")),
            "geometry_hash": plan["geometry_hash"], "unresolved_inputs": manifest["unresolved_inputs"], "synthetic_profile": profile["synthetic"]}


def profile_preview(profile, output):
    """A labeled diagnostic atlas, separate from the document renderer."""
    from .layout import map_strokes
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(output), pagesize=(210*PT, 297*PT), invariant=1)
    glyphs = accepted_glyphs(profile)
    for i, glyph in enumerate(glyphs):
        cell = i % 30
        if cell == 0:
            if i: c.showPage()
            c.setFont("Helvetica", 11)
            c.drawString(18*PT, 283*PT, f"Inkform profile: {profile['profile_id']} / {profile['profile_version']}")
            c.setFont("Helvetica", 8)
            c.drawString(18*PT, 277*PT, "Diagnostic labels use a print font; forms below are stored vector geometry.")
        col, row = cell % 5, cell // 5
        x, y = 20 + col*35, 46 + row*38
        c.setFont("Helvetica", 6)
        c.drawString(x*PT, (297-y+15)*PT, glyph["glyph_id"][:30])
        size = min(7, 26 / max(glyph["advance"], 1))
        strokes = map_strokes(glyph["strokes"], lambda px, py: (x + px*size, y + py*size))
        draw_instance(c, {"representation": glyph["representation"], "strokes": strokes, "pen_width_mm": profile["pen"]["width_mm"]}, 297)
    if not glyphs:
        c.setFont("Helvetica", 12); c.drawString(20*PT, 270*PT, "No reviewed renderable glyphs. Review imported observations first.")
    c.save()
    return str(output.resolve())
