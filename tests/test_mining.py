"""Only drawn test fixtures; never package or fetch a person's handwriting."""

import copy
import json
import shutil
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from inkform.ingest import import_samples, sha256_file
from inkform.mining import mine_candidates
from inkform.observations import import_observations
from inkform.profiles import accepted_glyphs


def draw_n(image, x, y, width=37, height=47, peak=12):
    draw = ImageDraw.Draw(image)
    draw.line([(x, y+height), (x, y), (x+3, y+height//3),
               (x+width//3, y+peak), (x+width*2//3, y+2),
               (x+width-2, y+height//3), (x+width, y+height)], fill=0, width=5)


@pytest.fixture
def writer(tmp_path, monkeypatch):
    monkeypatch.setenv("INKFORM_DATA_DIR", str(tmp_path/"private"))
    page = Image.new("L", (580, 220), 255)
    draw_n(page, 40, 70)
    source = tmp_path/"original.png"; page.save(source)
    mask = Image.eval(page.crop((33, 63, 85, 126)), lambda value: 255-value)
    mask_path = tmp_path/"template.png"; mask.save(mask_path)
    manifest = tmp_path/"template.json"
    manifest.write_text(json.dumps({"schema_version": "1.0", "observations": [{
        "id": "reviewed-n", "text": "n", "role": "text", "source_path": str(source),
        "mask_path": str(mask_path), "bbox_xywh": [33, 63, 52, 63],
        "baseline_px": 54, "xheight_px": 47}]}))
    proposed = import_observations(manifest)
    review_path = Path(proposed["import_report"]["review_path"])
    candidates = json.loads((review_path.parent/"candidates.json").read_text())["candidates"]
    review_path.write_text(json.dumps({"reviews": {row["candidate_id"]: {
        "mask_sha256": row["mask_sha256"], "geometry_sha256": row["geometry_sha256"],
        "label_status": "reviewed", "geometry_status": "reviewed", "reviewed_by": "drawn-fixture-review"}
        for row in candidates}}))
    profile = import_observations(manifest)
    return profile, source


def new_samples(tmp_path):
    samples = tmp_path/"samples"; samples.mkdir()
    page = Image.new("L", (580, 220), 255)
    for x, width, height, peak in ((40, 36, 47, 12), (135, 40, 43, 8), (242, 33, 52, 18), (345, 48, 46, 13)):
        draw_n(page, x, 70, width, height, peak)
    source = samples/"fresh.png"; page.save(source)
    return samples, source


def test_mining_retains_distinct_observed_ink_and_requires_exact_review(writer, tmp_path):
    profile, _ = writer
    samples, source = new_samples(tmp_path)
    before = copy.deepcopy(profile); source_hash = sha256_file(source)
    report = mine_candidates(profile, samples, tmp_path/"mined", characters="n", min_score=.72)
    assert report["candidate_count"] == 4
    assert report["accepted_glyph_count"] == 0
    assert profile == before and sha256_file(source) == source_hash
    rows = json.loads(Path(report["manifest"]).read_text())["observations"]
    assert {row["label_status"] for row in rows} == {"proposed"}
    assert len({row["bbox_xywh"][0] for row in rows}) == 4
    assert all(row["source_sha256"] == source_hash for row in rows)
    assert len({sha256_file(Path(report["manifest"]).parent/row["mask_path"]) for row in rows}) == 4
    imported = import_observations(Path(report["manifest"]))
    assert not accepted_glyphs(imported)
    assert Path(report["contact_sheets"][0]).is_file()


def test_duplicate_pages_and_existing_observation_are_not_extra_evidence(writer, tmp_path):
    profile, original = writer
    samples, source = new_samples(tmp_path)
    shutil.copyfile(source, samples/"duplicate.png")
    shutil.copyfile(original, samples/"old.png")
    report = mine_candidates(profile, samples, tmp_path/"mined", characters="n", min_score=.72)
    assert report["source_count"] == 2
    assert report["counters"]["duplicate_pages"] == 1
    assert report["counters"]["existing_observations"] == 1
    assert report["candidate_count"] == 4


def test_representations_do_not_inflate_template_bank(writer, tmp_path):
    profile, _ = writer
    samples, _ = new_samples(tmp_path)
    assert len(accepted_glyphs(profile)) == 2
    report = mine_candidates(profile, samples, tmp_path/"mined", characters="nq", per_label=2)
    assert report["template_count"] == 1
    assert report["candidate_count"] == 2
    assert report["unsupported_characters"] == ["q"]


def test_full_collection_retains_all_matches_without_weakening_review(writer, tmp_path):
    profile, _ = writer
    samples, _ = new_samples(tmp_path)
    bounded = mine_candidates(profile, samples, tmp_path/"bounded", characters="n", per_label=2, min_score=.72)
    full = mine_candidates(profile, samples, tmp_path/"all", characters="n", per_label=None, min_score=.72)
    assert bounded["candidate_count"] == 2
    assert full["candidate_count"] == full["matched_component_count"] == 4
    assert full["scanned_page_count"] == full["source_count"] == 1
    assert full["collection_mode"] == "all_qualified" and full["per_label"] is None
    candidates = json.loads(Path(full["candidates"]).read_text())["candidates"]
    assert all(row["score"] >= .72 and row["label_status"] == "proposed" for row in candidates)
    assert full["accepted_glyph_count"] == 0
    assert not accepted_glyphs(import_observations(Path(full["manifest"])))
    expected = Path(full["candidates"]).read_bytes()
    repeated = mine_candidates(profile, samples, tmp_path/"all", characters="n", per_label=None, min_score=.72)
    assert expected == Path(repeated["candidates"]).read_bytes()


def test_full_collection_counts_unmatched_connected_ink(writer, tmp_path):
    profile, _ = writer
    samples = tmp_path/"samples"; samples.mkdir()
    page = Image.new("L", (400, 160), 255)
    draw_n(page, 40, 40)
    draw = ImageDraw.Draw(page)
    draw.ellipse((190, 40, 260, 70), outline=0, width=5)
    page.save(samples/"mixed.png")
    report = mine_candidates(profile, samples, tmp_path/"all", characters="n", per_label=None, min_score=.90)
    assert report["matched_component_count"] == 1
    assert report["unmatched_component_count"] == 1
    assert report["counters"]["whole_components"] == 2
    assert any("whole-word" in note for note in report["limitations"])


def test_mining_uses_preserved_source_paths_and_reports_zero_new_forms(writer, tmp_path):
    profile, original = writer
    original.unlink()
    report = mine_candidates(profile, None, tmp_path/"mined", characters="n")
    assert report["source_count"] == 1
    assert report["candidate_count"] == 0
    assert report["counters"]["existing_observations"] == 1


def test_tampered_reviewed_mask_is_rejected(writer, tmp_path):
    profile, _ = writer
    Path(profile["observations"][0]["source_path"]).write_bytes(b"changed")
    with pytest.raises(ValueError, match="hash changed"):
        mine_candidates(profile, None, tmp_path/"mined", characters="n")


@pytest.mark.parametrize("kwargs", [{"per_label": 0}, {"per_label": True}, {"min_score": float("nan")},
                                    {"min_score": 0}, {"characters": ""}, {"role": "other"}])
def test_invalid_options_fail_before_mining(writer, tmp_path, kwargs):
    profile, _ = writer
    with pytest.raises(ValueError):
        mine_candidates(profile, None, tmp_path/"mined", **kwargs)


def test_connected_word_is_never_cut_into_matching_letter_windows(writer, tmp_path):
    profile, _ = writer
    samples = tmp_path/"samples"; samples.mkdir()
    page = Image.new("L", (400, 160), 255)
    for x in (40, 90, 140):
        draw_n(page, x, 40)
    ImageDraw.Draw(page).line((40, 87, 177, 87), fill=0, width=5)
    page.save(samples/"joined.png")
    report = mine_candidates(profile, samples, tmp_path/"mined", characters="n")
    assert report["candidate_count"] == 0


def test_repeated_runs_are_deterministic(writer, tmp_path):
    profile, _ = writer
    samples, _ = new_samples(tmp_path)
    destination = tmp_path/"mined"
    first = mine_candidates(profile, samples, destination, characters="n", per_label=3)
    rows = Path(first["candidates"]).read_bytes()
    second = mine_candidates(profile, samples, destination, characters="n", per_label=3)
    assert Path(second["candidates"]).read_bytes() == rows
    assert first == second


def test_missing_templates_returns_actionable_intake_status(writer, tmp_path):
    profile, _ = writer
    report = mine_candidates(profile, None, tmp_path/"mined", characters="→")
    assert report["status"] == "awaiting_templates"
    assert report["accepted_glyph_count"] == report["candidate_count"] == 0
    assert "Review at least one" in report["next_step"]
    assert json.loads(Path(report["manifest"]).read_text())["observations"] == []


def test_preserved_source_hash_is_verified(writer, tmp_path):
    profile, _ = writer
    Path(profile["sources"][0]["source_path"]).write_bytes(b"corrupt original")
    with pytest.raises(ValueError, match="Preserved source evidence hash changed"):
        mine_candidates(profile, None, tmp_path/"mined", characters="n")


def test_pdf_pages_are_mined_with_original_document_and_page_provenance(writer, tmp_path):
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen.canvas import Canvas
    profile, _ = writer
    pdf_dir = tmp_path/"pdfs"; pdf_dir.mkdir()
    page = Image.new("L", (580, 220), 255)
    draw_n(page, 160, 70, width=39, height=48, peak=10)
    pdf_path = pdf_dir/"sample.pdf"
    canvas = Canvas(str(pdf_path), pagesize=(290, 110))
    canvas.drawImage(ImageReader(page), 0, 0, width=290, height=110)
    canvas.showPage(); canvas.save()
    imported = import_samples(pdf_dir, existing_profile=profile)
    report = mine_candidates(imported, None, tmp_path/"mined", characters="n", min_score=.72)
    assert report["candidate_count"] == 1
    rendered = next(source for source in report["sources"] if "derived_from_pdf" in source)
    assert rendered["derived_from_pdf"]["original_source_sha256"] == sha256_file(pdf_path)
    assert rendered["derived_from_pdf"]["page_index"] == 0
    manifest = json.loads(Path(report["manifest"]).read_text())
    assert manifest["mining"]["derived_sources"][rendered["source_path"]] == rendered["derived_from_pdf"]
    assert not accepted_glyphs(import_observations(Path(report["manifest"])))
