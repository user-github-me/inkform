"""The selected writer style and preview resolution survive the CLI/export path."""
import copy
import json
from pathlib import Path

from inkform.cli import main
from inkform.document import literal_document
from inkform.profiles import load_profile, validate_profile


def test_writer_outline_preference_controls_default_export_without_changing_explicit_strokes(tmp_path, capsys):
    profile = load_profile(Path(__file__).resolve().parents[1] / "examples/synthetic-profile.json")
    outline = copy.deepcopy(next(g for g in profile["glyphs"] if g["text"] == "a" and g["role"] == "text"))
    outline.update(glyph_id=outline["glyph_id"] + ".outline", representation="clean_outline",
                   ink_bounds=[0, -1, 0.5, 0], advance=0.7,
                   strokes=[{"start": [0, 0], "closed": True, "segments": [
                       {"type": "line", "end": [0, -1]}, {"type": "line", "end": [0.5, -1]},
                       {"type": "line", "end": [0.5, 0]}, {"type": "line", "end": [0, 0]},
                   ]}], outline_calibration={"body_x_height_mm": profile["physical_scale"]["body_x_height_mm"],
                                             "pen_width_mm": profile["pen"]["width_mm"]})
    profile["glyphs"].append(outline)
    profile["default_render_mode"] = "clean_outline"
    validate_profile(profile)
    pf, doc = tmp_path / "profile.json", tmp_path / "note.json"
    pf.write_text(json.dumps(profile))
    doc.write_text(json.dumps(literal_document("a")))
    before = pf.read_bytes()
    for name, extra, representation in (("preferred", [], "clean_outline"), ("explicit", ["--mode", "fresh_strokes"], "centerline")):
        assert main(["render", str(doc), "--profile", str(pf), "--output", str(tmp_path / f"{name}.pdf"), *extra]) == 0
        result = json.loads(capsys.readouterr().out)
        manifest = json.loads(Path(result["manifest"]).read_text())
        report = json.loads(Path(result["report"]).read_text())
        assert manifest["representations_used"] == [representation]
        assert report["handwriting_raster_objects"] == 0
        assert report["pages"][0]["preview_dpi"] == 300
        assert report["pages"][0]["preview_pixels"][0] > 2400
    assert pf.read_bytes() == before
