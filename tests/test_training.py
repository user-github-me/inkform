"""Training orchestration with invented samples and exact review ledgers."""

import copy
import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageOps

from inkform.document import literal_document
from inkform.errors import InkformError
from inkform.geometry import geometry_hash, reconstruct_geometry
from inkform.ingest import import_samples, sha256_file
from inkform.observations import import_observations
from inkform.profiles import accepted_glyphs, load_profile, next_profile_version, save_profile
from inkform.training import _merge_evidence, train_profile


@pytest.fixture
def inputs(tmp_path, monkeypatch):
    monkeypatch.setenv("INKFORM_DATA_DIR", str(tmp_path / "private"))
    samples = tmp_path / "samples"
    samples.mkdir()
    mask = Image.new("L", (64, 80))
    ImageDraw.Draw(mask).ellipse((17, 26, 45, 66), outline=255, width=5)
    mask.save(tmp_path / "letter-mask.png")
    ImageOps.invert(mask).convert("RGB").save(samples / "page.png")
    manifest = tmp_path / "observations.json"
    manifest.write_text(json.dumps({"schema_version": "1.0", "observations": [{
        "id": "invented-o", "text": "o", "role": "text", "source_path": "samples/page.png",
        "mask_path": "letter-mask.png", "bbox_xywh": [0, 0, 64, 80],
        "baseline_px": 68, "xheight_px": 40, "label_status": "reviewed", "reviewed_by": "synthetic-label-review",
    }]}))
    return {"root": tmp_path, "samples": samples, "manifest": manifest, "mask": np.asarray(mask)}


def approve(report, representation="clean_outline", geometry_digest=None):
    imported = next(item["import_report"] for item in report["imports"] if item["stage"] == "observations")
    review_path = Path(imported["review_path"])
    candidates = json.loads((review_path.parent / "candidates.json").read_text())["candidates"]
    reviews = {}
    for candidate in candidates:
        if candidate["geometry"]["representation"] == representation:
            reviews[candidate["candidate_id"]] = {"geometry_status": "reviewed", "label_status": "reviewed", "reviewed_by": "synthetic-geometry-review",
                "mask_sha256": candidate["mask_sha256"], "geometry_sha256": geometry_digest or candidate["geometry_sha256"]}
    review_path.write_text(json.dumps({"reviews": reviews}))


def approved_writer(inputs, profile_id="fixture-writer"):
    proposed = train_profile(inputs["samples"], profile_id, observations=inputs["manifest"])
    approve(proposed)
    return profile_id


def test_raw_samples_are_preserved_but_never_claimed_as_learned_forms(inputs):
    source = inputs["samples"] / "page.png"
    before = source.read_bytes()
    result = train_profile(inputs["samples"], "raw-writer")
    profile = load_profile(result["profile_path"])
    assert result["ok"]
    assert result["training_status"] == "awaiting_observations"
    assert result["sample_count"] == 1
    assert result["reviewed_glyph_count"] == 0
    assert not result["neural_model"]
    assert not accepted_glyphs(profile)
    assert "default_render_mode" not in profile
    assert result["calibration"]["kind"] == "unavailable"
    assert profile["sources"][0]["sha256"] == sha256_file(source)
    assert Path(profile["sources"][0]["source_path"]).read_bytes() == before
    assert source.read_bytes() == before


def test_training_mines_private_candidates_without_approving_them(inputs):
    writer = approved_writer(inputs, 'mining-writer')
    trained = train_profile(inputs['samples'], writer, observations=inputs['manifest'],
                            document=literal_document('oooo'), mine=True)
    assert trained['ok'], trained['diagnostics']
    assert trained['diversity']['collection_requests'][0]['text'] == 'o'
    assert trained['reviewed_glyph_count'] == 1
    assert trained['sample_mining']['candidate_count'] == 0  # Only existing evidence on this page.
    assert Path(trained['report_path']).is_file()


def test_default_training_calibration_exercises_repeated_forms(inputs):
    writer = approved_writer(inputs, 'repeat-calibration')
    trained = train_profile(inputs['samples'], writer, observations=inputs['manifest'])
    assert trained['ok'], trained['diagnostics']
    document = json.loads(Path(trained['calibration']['document_path']).read_text())
    assert document['blocks'][0]['children'][0]['text'] == 'o o o o'
    checked = json.loads(Path(trained['calibration']['report_path']).read_text())
    assert checked['repetition']['geometry_source'] == 'emitted_pdf_vector_paths'
    assert checked['repetition']['exact_count'] == checked['repetition']['near_count'] == 0


def test_raw_to_reviewed_outlines_selects_writer_default_and_renders_real_pdf(inputs):
    raw = train_profile(inputs["samples"], "first-writer")
    original_path = Path(raw["profile_path"])
    original_bytes = original_path.read_bytes()
    proposed = train_profile(inputs["samples"], "first-writer", observations=inputs["manifest"])
    assert proposed["training_status"] == "needs_geometry_review"
    assert proposed["reviewed_glyph_count"] == 0
    assert "default_render_mode" not in load_profile(proposed["profile_path"])
    # Label approval and a forged geometry hash cannot train an accepted form.
    approve(proposed, geometry_digest="0" * 64)
    rejected_hash = train_profile(inputs["samples"], "first-writer", observations=inputs["manifest"])
    assert rejected_hash["training_status"] == "needs_geometry_review"
    approve(rejected_hash)
    note = literal_document("o o")
    trained = train_profile(inputs["samples"], "first-writer", observations=inputs["manifest"], calibration_document=note, document=note)
    assert trained["ok"], trained["diagnostics"]
    assert trained["training_status"] == "ready_for_document"
    assert trained["default_render_mode"] == "clean_outline"
    assert trained["document_readiness"]["ready"]
    assert trained["reviewed_glyph_count"] == 1
    assert trained["calibration"]["status"] == "rendered_pending_review"
    assert Path(trained["calibration"]["pdf_path"]).read_bytes().startswith(b"%PDF")
    assert json.loads(Path(trained["calibration"]["document_path"]).read_text()) == note
    assert original_path.read_bytes() == original_bytes
    profile = load_profile(trained["profile_path"])
    assert profile["training"]["method"] == "local_reviewed_exemplar_learning"
    assert profile["training"]["calibration"]["status"] == "planned"
    assert profile["training"]["calibration"]["export_status"] == "see_run_report"
    assert "pdf_path" not in profile["training"]["calibration"]
    assert trained["calibration"]["print_status"] == "pending_actual_size_print"


def test_actual_document_missing_forms_are_requested_without_font_or_role_fallback(inputs):
    writer = approved_writer(inputs)
    note = literal_document("oz")
    trained = train_profile(inputs["samples"], writer, observations=inputs["manifest"], calibration_document=note, document=note)
    assert trained["ok"]
    assert trained["training_status"] == "ready_for_calibration"
    assert not trained["document_readiness"]["ready"]
    assert trained["document_readiness"]["coverage"]["missing_glyphs"] == [{"role": "text", "text": "z", "count": 1}]
    assert trained["document_readiness"]["missing_form_requests"] == [{"role": "text", "text": "z", "occurrences": 1, "suggested_sample": "z z z", "context": "text"}]
    assert trained["calibration"]["kind"] == "atlas"
    assert trained["calibration"]["status"] == "atlas_only_pending_review"
    assert {g["text"] for g in load_profile(trained["profile_path"])["glyphs"]} == {"o"}


def test_renderable_partial_document_is_not_claimed_ready_when_graph_is_missing(inputs):
    writer = approved_writer(inputs)
    note = literal_document("o")
    note["unresolved_inputs"] = [{"source_item": "q5", "reason": "required_graph_missing"}]
    original = copy.deepcopy(note)
    trained = train_profile(inputs["samples"], writer, observations=inputs["manifest"], calibration_document=note, document=note)
    assert trained["ok"], trained["diagnostics"]
    assert trained["training_status"] == "ready_for_calibration"
    readiness = trained["document_readiness"]
    assert readiness["status"] == "unresolved_inputs"
    assert readiness["renderable"] is True
    assert readiness["content_complete"] is False
    assert readiness["ready"] is False
    assert readiness["unresolved_inputs"] == original["unresolved_inputs"]
    assert readiness["coverage"]["complete"] is True
    assert trained["calibration"]["kind"] == "document"
    assert Path(trained["calibration"]["pdf_path"]).is_file()
    report = json.loads(Path(trained["calibration"]["report_path"]).read_text())
    assert report["checks"]["content_accounted_for"] == "pass_with_declared_missing_input"
    assert load_profile(trained["profile_path"])["training"]["document_readiness"]["unresolved_inputs"] == original["unresolved_inputs"]
    assert note == original


def test_new_observations_keep_custom_writer_calibration_and_explicit_preference(inputs):
    base = import_samples(inputs["samples"], profile_id="custom-writer")
    base["physical_scale"]["body_x_height_mm"] = 4.0
    base["pen"]["width_mm"] = 0.30
    base["metrics"]["word_gap"] = 1.2
    base["default_render_mode"] = "clean_outline"
    save_profile(base)
    proposals = train_profile(inputs["samples"], "custom-writer", observations=inputs["manifest"])
    candidate_profile = load_profile(proposals["profile_path"])
    for glyph in candidate_profile["glyphs"]:
        if glyph["representation"] == "clean_outline":
            assert glyph["outline_calibration"]["body_x_height_mm"] == 4.0
            assert glyph["outline_calibration"]["pen_width_mm"] == 0.30
    approve(proposals)
    trained = train_profile(inputs["samples"], "custom-writer", observations=inputs["manifest"], document=literal_document("o"))
    assert trained["ok"], trained["diagnostics"]
    assert trained["training_status"] == "ready_for_document"
    final = load_profile(trained["profile_path"])
    assert final["physical_scale"] == base["physical_scale"]
    assert final["pen"] == base["pen"]
    assert final["metrics"] == base["metrics"]
    assert trained["measurements"]["metric_updates"] == {}


def test_explicit_fresh_preference_is_not_replaced_by_reviewed_outlines(inputs):
    base = import_samples(inputs["samples"], profile_id="explicit-writer")
    base["default_render_mode"] = "fresh_strokes"
    save_profile(base)
    proposed = train_profile(inputs["samples"], "explicit-writer", observations=inputs["manifest"])
    approve(proposed)
    trained = train_profile(inputs["samples"], "explicit-writer", observations=inputs["manifest"])
    assert trained["default_render_mode"] == "fresh_strokes"
    assert trained["training_status"] == "needs_geometry_review"
    assert trained["reviewed_glyph_count"] == 1


@pytest.mark.parametrize("measurement", ["baseline_px", "xheight_px"])
def test_geometry_and_observation_updates_preserve_old_records(inputs, measurement):
    profile = import_observations(inputs["manifest"], profile_id="merge-writer")
    for glyph in profile["glyphs"]:
        glyph["geometry_status"] = "reviewed"
    old = copy.deepcopy(profile)
    changed = copy.deepcopy(profile)
    changed["observations"][0][measurement] += 1
    for glyph in changed["glyphs"]:
        glyph["geometry_status"] = "proposed"
    merged, report = _merge_evidence(profile, changed)
    assert profile == old
    assert len(merged["observations"]) == 2
    assert merged["observations"][0] == old["observations"][0]
    assert merged["observations"][1][measurement] == old["observations"][0][measurement] + 1
    assert len(merged["glyphs"]) == 4
    assert merged["glyphs"][:2] == old["glyphs"]
    assert all(g["source_refs"][0]["observation_id"] != old["observations"][0]["observation_id"] for g in merged["glyphs"][2:])
    assert len(report["renamed_glyphs"]) == 2


def test_actual_remeasurement_cannot_reassign_an_older_derived_glyph(inputs):
    writer = approved_writer(inputs)
    trained = train_profile(inputs["samples"], writer, observations=inputs["manifest"])
    profile = load_profile(trained["profile_path"])
    derived = copy.deepcopy(accepted_glyphs(profile)[0])
    derived["glyph_id"] += ".math-context"
    derived["role"] = "math.variable"
    profile["glyphs"].append(derived)
    profile["profile_version"] = next_profile_version(profile)
    save_profile(profile)
    previous_observation_id = derived["source_refs"][0]["observation_id"]
    previous_observation = next(o for o in profile["observations"] if o["observation_id"] == previous_observation_id)
    manifest = json.loads(inputs["manifest"].read_text())
    manifest["observations"][0]["baseline_px"] += 1
    inputs["manifest"].write_text(json.dumps(manifest))
    remeasured = train_profile(inputs["samples"], writer, observations=inputs["manifest"])
    final = load_profile(remeasured["profile_path"])
    assert next(g for g in final["glyphs"] if g["glyph_id"] == derived["glyph_id"]) == derived
    assert next(o for o in final["observations"] if o["observation_id"] == previous_observation_id) == previous_observation
    new_observation = next(o for o in final["observations"] if o.get("baseline_px") == previous_observation["baseline_px"] + 1)
    assert new_observation["observation_id"] != previous_observation_id
    remapped = [g for g in final["glyphs"] if any(ref.get("observation_id") == new_observation["observation_id"] for ref in g["source_refs"])]
    assert len(remapped) == 2
    assert all(g["role"] == "text" and g["geometry_status"] == "proposed" for g in remapped)
    assert not any(g.get("origin_glyph_id") == derived["glyph_id"] for g in final["glyphs"])


def test_collision_between_representations_cannot_replace_reviewed_centerline(inputs):
    profile = import_observations(inputs["manifest"], profile_id="merge-writer")
    centerline = next(g for g in profile["glyphs"] if g["representation"] == "centerline")
    outline = next(g for g in profile["glyphs"] if g["representation"] == "clean_outline")
    centerline["geometry_status"] = "reviewed"
    base = {**profile, "glyphs": [copy.deepcopy(centerline)]}
    incoming = {**profile, "glyphs": [copy.deepcopy(outline)]}
    incoming["glyphs"][0]["glyph_id"] = centerline["glyph_id"]
    merged, report = _merge_evidence(base, incoming)
    assert merged["glyphs"][0] == centerline
    assert len({g["glyph_id"] for g in merged["glyphs"]}) == 2
    assert {g["representation"] for g in merged["glyphs"]} == {"centerline", "clean_outline"}
    assert len(report["renamed_glyphs"]) == 1


def test_rough_outlines_do_not_reenter_selection_when_exact_refinement_is_reviewed(inputs):
    profile = import_observations(inputs["manifest"], profile_id="polished-writer")
    smooth = next(g for g in profile["glyphs"] if g["representation"] == "clean_outline")
    smooth["geometry_status"] = "reviewed"
    smooth["refinement"]["geometry_review_status"] = "reviewed"
    original = copy.deepcopy(profile)
    rough = {**copy.deepcopy(smooth), **reconstruct_geometry(inputs["mask"], 68, 40, "clean_outline")}
    rough.pop("refinement")
    rough["glyph_id"] += ".rough-original"
    assert geometry_hash(rough) == smooth["refinement"]["previous_geometry_sha256"]
    incoming = {**copy.deepcopy(profile), "glyphs": [rough]}
    merged, report = _merge_evidence(profile, incoming)
    assert merged["glyphs"] == original["glyphs"]
    assert report["retained_glyph_count"] == 0
    assert len(report["superseded_candidates"]) == 1


def test_later_import_failure_keeps_preserved_samples_and_actionable_report(inputs):
    result = train_profile(inputs["samples"], "partial-writer", observations=inputs["root"] / "missing.json")
    assert not result["ok"]
    assert result["training_status"] == "awaiting_observations"
    assert result["diagnostics"][0]["stage"] == "observations"
    profile = load_profile(result["profile_path"])
    assert len(profile["sources"]) == 1
    assert not profile["glyphs"]


def test_export_failure_is_reported_without_false_persisted_completion(inputs, monkeypatch):
    writer = approved_writer(inputs)
    def fail_export(*args, **kwargs):
        raise InkformError("export_validation_failed", "Synthetic export failure")
    monkeypatch.setattr("inkform.rendering.render_document", fail_export)
    result = train_profile(inputs["samples"], writer, observations=inputs["manifest"])
    assert not result["ok"]
    assert result["calibration"]["status"] == "export_failed"
    assert result["calibration"]["pdf_path"] is None
    profile = load_profile(result["profile_path"])
    assert len(accepted_glyphs(profile)) == 1
    stored = profile["training"]["calibration"]
    assert stored["status"] == "planned" and "pdf_path" not in stored
    assert not Path(stored["planned_pdf_path"]).exists()
    actual = json.loads(Path(stored["execution_report_path"]).read_text())
    assert actual["calibration"]["status"] == "export_failed"


def test_empty_sample_folder_does_not_create_a_trained_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("INKFORM_DATA_DIR", str(tmp_path / "private"))
    with pytest.raises(InkformError) as error:
        train_profile(tmp_path, "empty-writer")
    assert error.value.code == "empty_training_samples"
    assert not (tmp_path / "private" / "profiles").exists()


def test_training_persists_sampled_variation_and_correction_counts_and_retains_amount_zero(inputs):
    base = import_samples(inputs["samples"], profile_id="variable-writer")
    base["variation"] = {"amount": 0}
    original_path = save_profile(base)
    original_bytes = original_path.read_bytes()
    second = Image.new("L", (64, 80))
    ImageDraw.Draw(second).ellipse((9, 24, 54, 66), outline=255, width=5)
    second.save(inputs["root"] / "second-mask.png")
    data = json.loads(inputs["manifest"].read_text())
    row = copy.deepcopy(data["observations"][0])
    row.update(id="second-o", mask_path="second-mask.png")
    data["observations"].append(row)
    data["style_observations"] = [{"id": "counted-page", "kind": "correction_frequency", "scope": "page",
        "source_path": "samples/page.png", "source_sha256": sha256_file(inputs["samples"] / "page.png"),
        "page_index": 0, "representative": True, "word_count": 100, "correction_count": 6,
        "review_status": "reviewed", "reviewed_by": "fixture-author"}]
    inputs["manifest"].write_text(json.dumps(data))
    proposed = train_profile(inputs["samples"], "variable-writer", observations=inputs["manifest"])
    approve(proposed)
    trained = train_profile(inputs["samples"], "variable-writer", observations=inputs["manifest"])
    assert trained["ok"], trained["diagnostics"]
    profile = load_profile(trained["profile_path"])
    assert profile["variation"]["amount"] == 0
    model = profile["variation"]["learned"]["by_glyph"][0]
    assert model["text"] == "o" and model["source_count"] == 2
    assert model["width"] > 0
    assert profile["corrections"]["rate"] == .06
    assert profile["corrections"]["sampled_word_count"] == 100
    # Training can resume without reimporting: no duplicate aliases inflate it.
    resumed = train_profile(inputs["samples"], "variable-writer")
    again = load_profile(resumed["profile_path"])
    assert again["variation"] == profile["variation"]
    assert again["corrections"] == profile["corrections"]
    assert original_path.read_bytes() == original_bytes


def test_training_preflight_requests_deleted_text_and_explicit_correction_marks(inputs):
    writer = approved_writer(inputs)
    note = literal_document("o")
    note["blocks"][0]["children"][0].update(type="correction", deleted="z")
    missing_letter = train_profile(inputs["samples"], writer, observations=inputs["manifest"], document=note)
    requests = missing_letter["document_readiness"]["missing_form_requests"]
    assert [(item["role"], item["text"]) for item in requests] == [("text", "z")]
    note["blocks"][0]["children"][0]["deleted"] = "o"
    missing_mark = train_profile(inputs["samples"], writer, document=note)
    assert missing_mark["document_readiness"]["status"] == "missing_correction_mark"
    assert not missing_mark["document_readiness"]["ready"]
