"""Train a private writer profile from local, reviewed handwriting exemplars.

Training here means preserving evidence, fitting/curating vector exemplars,
learning supported measurements, and checking a calibration document. Copying
photographs alone never produces an accepted writer model or neural weights.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections import Counter
from pathlib import Path

from .document import clusters, load_document, validate_document, visible_requirements
from .errors import InkformError
from .geometry import geometry_hash
from .ingest import import_prototype, import_samples
from .layout import plan_document
from .measurements import measure_style
from .profiles import (
    accepted_glyphs, coverage, default_data_dir, load_profile, merge_profile,
    next_profile_version, resolve_render_mode, save_profile, validate_profile,
)


TRAINING_VERSION = "inkform-exemplar-training-1"


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _new_id(identity: str, kind: str, digest: str) -> str:
    suffix = f".{kind}.{digest[:12]}"
    return identity[:128-len(suffix)] + suffix


def _refs(glyph):
    return tuple(sorted((ref["source_id"], ref.get("observation_id", "")) for ref in glyph.get("source_refs", [])))


def _merge_evidence(existing: dict, incoming: dict) -> tuple[dict, dict]:
    """Keep representations and revised observations under distinct stable IDs."""
    validate_profile(incoming)
    if existing["synthetic"] != incoming["synthetic"]:
        raise InkformError("invalid_profile", "Synthetic demonstration profiles cannot be mixed with personal training evidence.")
    update = {key: copy.deepcopy(incoming.get(key, [])) for key in ("sources", "observations", "glyphs")}
    previous_sources = {item["source_id"]: item for item in existing["sources"]}
    source_aliases = {}
    for source in update["sources"]:
        old = previous_sources.get(source["source_id"])
        if old and old.get("sha256") and source.get("sha256") and old["sha256"] != source["sha256"]:
            original = source["source_id"]
            source["source_id"] = _new_id(original, "evidence", source["sha256"])
            source_aliases[original] = source["source_id"]
    previous_observations = {item["observation_id"]: item for item in existing.get("observations", [])}
    observation_aliases = {}
    measurement_fields = ("source_id", "mask_sha256", "baseline_px", "xheight_px", "bbox_xywh", "source_oriented_size", "normalized_size", "oriented_size", "scale_from_oriented",
                          "kind", "page_index", "line_id", "word_index", "column_id", "scope", "representative", "word_count", "correction_count", "source_sha256")
    for observation in update["observations"]:
        observation["source_id"] = source_aliases.get(observation["source_id"], observation["source_id"])
        old = previous_observations.get(observation["observation_id"])
        changed = old and any(old.get(field) != observation.get(field) for field in measurement_fields)
        if changed:
            original = observation["observation_id"]
            observation["observation_id"] = _new_id(original, "evidence", _digest({field: observation.get(field) for field in measurement_fields}))
            observation_aliases[original] = observation["observation_id"]
    previous_glyphs = {item["glyph_id"]: item for item in existing["glyphs"]}
    refined = [g for g in accepted_glyphs(existing) if g["representation"] == "clean_outline" and g.get("refinement")]
    retained, superseded, renamed = [], [], []
    for glyph in update["glyphs"]:
        for ref in glyph["source_refs"]:
            ref["source_id"] = source_aliases.get(ref["source_id"], ref["source_id"])
            if "observation_id" in ref:
                ref["observation_id"] = observation_aliases.get(ref["observation_id"], ref["observation_id"])
        if "observation_id" in glyph:
            glyph["observation_id"] = observation_aliases.get(glyph["observation_id"], glyph["observation_id"])
        fingerprint = geometry_hash(glyph)
        preferred = next((g for g in refined if (g["role"], g["text"], _refs(g)) == (glyph["role"], glyph["text"], _refs(glyph)) and g["refinement"].get("previous_geometry_sha256") == fingerprint), None)
        if preferred:
            superseded.append({"glyph_id": glyph["glyph_id"], "geometry_sha256": fingerprint, "preferred_glyph_id": preferred["glyph_id"], "reason": "reviewed_refinement_already_preserves_this_exact_evidence"})
            continue
        old = previous_glyphs.get(glyph["glyph_id"])
        if old and (geometry_hash(old) != fingerprint or _refs(old) != _refs(glyph)):
            original = glyph["glyph_id"]
            glyph["glyph_id"] = _new_id(original, glyph["representation"].replace("_", "-"), _digest([fingerprint, _refs(glyph)]))
            glyph.setdefault("origin_glyph_id", original)
            renamed.append({"original_id": original, "stored_id": glyph["glyph_id"]})
            old = previous_glyphs.get(glyph["glyph_id"])
        if old:
            # Re-importing proposed data cannot erase prior reviewed geometry.
            combined = copy.deepcopy(glyph)
            for status in ("label_status", "geometry_status"):
                if combined[status] == "proposed" and old[status] in {"reviewed", "rejected"}:
                    combined[status] = old[status]
                    combined["reviewed_by"] = old["reviewed_by"]
            glyph = {**copy.deepcopy(old), **combined}
        retained.append(glyph)
    update["glyphs"] = retained
    # Derived models are rebuilt after evidence merging; stale rates must not
    # survive an updated review/count or prevent a legitimate evidence revision.
    working = copy.deepcopy(existing)
    working.get("variation", {}).pop("learned", None)
    if working.get("corrections", {}).get("basis") == "reviewed_samples":
        working.pop("corrections")
    result = merge_profile(working, update, profile_version=next_profile_version(existing))
    return result, {"incoming_glyph_count": len(incoming["glyphs"]), "retained_glyph_count": len(retained), "renamed_glyphs": renamed, "superseded_candidates": superseded}


def _inventory(profile) -> dict:
    result = {}
    for representation in ("centerline", "clean_outline"):
        glyphs = [g for g in accepted_glyphs(profile) if g["representation"] == representation]
        forms = Counter((g["role"], g["text"]) for g in glyphs)
        result[representation] = {"reviewed_glyph_count": len(glyphs), "unique_form_count": len(forms),
            "roles": {role: sorted({text for candidate_role, text in forms if candidate_role == role}) for role in sorted({role for role, _ in forms})},
            "variants": [{"role": role, "text": text, "count": count} for (role, text), count in sorted(forms.items())]}
    return result


def _document(value):
    return load_document(value) if isinstance(value, (str, Path)) else validate_document(copy.deepcopy(value))


def _readiness(profile, document) -> dict:
    if document is None:
        return {"status": "not_checked", "ready": False, "renderable": False, "content_complete": False, "reason": "Supply an actual document to check its exact forms and mathematical layout."}
    mode = resolve_render_mode(profile)
    representation = "centerline" if mode == "fresh_strokes" else "clean_outline"
    eligible = {**profile, "glyphs": [g for g in profile["glyphs"] if g["representation"] == representation]}
    cov = coverage(eligible, visible_requirements(document))
    unresolved = copy.deepcopy(document.get("unresolved_inputs", []))
    result = {"status": "missing_glyphs" if not cov["complete"] else "coverage_complete", "ready": False, "renderable": False, "content_complete": not unresolved,
              "unresolved_inputs": unresolved, "mode": mode, "coverage": cov,
              "missing_form_requests": [{"role": item["role"], "text": item["text"], "occurrences": item["count"], "suggested_sample": " ".join([item["text"]] * 3), "context": item["role"]} for item in cov["missing_glyphs"]]}
    if not cov["complete"]:
        return result
    try:
        plan = plan_document(document, profile)
        result.update(status="unresolved_inputs" if unresolved else "ready", ready=not unresolved, renderable=True,
                      page_count=plan["page_count"], required_symbol_count=plan["required_symbol_count"])
    except InkformError as exc:
        result.update(status=exc.code, diagnostic=exc.as_dict())
    return result


def _specimen_document(profile):
    """A coverage-matched specimen, never an invented transcription of samples."""
    representation = "centerline" if resolve_render_mode(profile) == "fresh_strokes" else "clean_outline"
    glyphs = [g for g in accepted_glyphs(profile) if g["role"] != "mark.correction" and g["representation"] == representation and len(clusters(g["text"])) == 1 and not g["text"].isspace()]
    blocks = []
    text_forms = sorted({g["text"] for g in glyphs if g["role"] == "text"})
    for offset in range(0, len(text_forms), 20):
        blocks.append({"id": f"text-{offset}", "type": "paragraph", "children": [{"id": f"forms-{offset}", "type": "text", "text": " ".join(text_forms[offset:offset+20])}]})
    math_forms = sorted({(g["role"], g["text"]) for g in glyphs if g["role"] != "text"})
    for offset in range(0, len(math_forms), 12):
        blocks.append({"id": f"math-{offset}", "type": "display_math", "expression": {"type": "row", "items": [{"type": "symbol", "role": role, "value": text} for role, text in math_forms[offset:offset+12]]}})
    if not blocks:
        return None
    return validate_document({"schema_version": "1.0", "document_id": profile["profile_id"] + "-training-calibration", "content_mode": "literal", "blocks": blocks, "unresolved_inputs": []})


def _error(stage, exc):
    return {"stage": stage, **(exc.as_dict() if isinstance(exc, InkformError) else {"error": "training_stage_failed", "message": str(exc)})}


def train_profile(samples: Path, profile_id: str, prototype: Path | None = None,
                  observations: Path | None = None, calibration_document: Path | dict | None = None,
                  document: Path | dict | None = None) -> dict:
    """Import, learn from reviewed observations, persist, and calibrate locally.

    Existing current profiles are resumed. Raw photos alone result in
    ``awaiting_observations``. No candidate geometry is approved by this function.
    A target document is ready only after role coverage and full layout pass;
    physical print/style calibration remains explicitly separate.
    """
    if not isinstance(profile_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", profile_id):
        raise InkformError("invalid_training_input", "Choose a safe writer profile ID.")
    samples = Path(samples).expanduser().resolve()
    if not samples.is_dir():
        raise InkformError("invalid_training_input", "Handwriting samples must be a local directory.", {"path": str(samples)})
    target = _document(document) if document is not None else None
    calibration_input = _document(calibration_document) if calibration_document is not None else None
    pointer = default_data_dir() / "profiles" / profile_id / "current.json"
    existing = load_profile(profile_id) if pointer.exists() else None
    imported = import_samples(samples, profile_id=profile_id)
    if not imported["sources"]:
        raise InkformError("empty_training_samples", "No supported handwriting photographs, scans, or PDFs were found.", {"path": str(samples), "skipped": imported.get("import_report", {}).get("skipped", [])})
    sample_count = len(imported["sources"])
    current = _merge_evidence(existing, imported)[0] if existing else imported
    stages = [{"stage": "imported", "sample_count": sample_count, "page_observation_count": len(imported.get("observations", []))}]
    diagnostics, imports = [], []
    current["training"] = {"version": TRAINING_VERSION, "method": "local_reviewed_exemplar_learning", "neural_model": False,
                           "status": "imported", "sample_count": sample_count, "stages": copy.deepcopy(stages)}
    profile_path = save_profile(current)
    import_jobs = []
    if prototype is not None:
        import_jobs.extend([(f"prototype_{representation}", lambda rep=representation: import_prototype(Path(prototype), profile_id, rep)) for representation in ("centerline", "clean_outline")])
    if observations is not None:
        def portable_observations():
            from .observations import import_observations
            # Pass writing calibration, not existing evidence: the adapter's
            # merged return must not present old derived forms as new input and
            # remap them to a remeasured observation during our own merge.
            context = {key: copy.deepcopy(current[key]) for key in (
                "schema_version", "profile_id", "profile_version", "scripts", "physical_scale", "pen",
                "metrics", "dynamics", "calibration_status", "synthetic",
            )}
            context.update(glyphs=[], sources=[], observations=[])
            return import_observations(Path(observations), base_profile=context, profile_id=profile_id)
        import_jobs.append(("observations", portable_observations))
    for stage, importer in import_jobs:
        try:
            incoming = importer()
            current, merge_report = _merge_evidence(current, incoming)
            imports.append({"stage": stage, "import_report": incoming.get("import_report", {}), **merge_report})
            stages.append({"stage": stage, "reviewed_glyph_count": len(accepted_glyphs(incoming))})
            current["training"]["stages"] = copy.deepcopy(stages)
            profile_path = save_profile(current)
        except (InkformError, OSError, ValueError, ImportError) as exc:
            diagnostics.append(_error(stage, exc))
    learned = accepted_glyphs(current)
    if "default_render_mode" not in current and learned:
        current["default_render_mode"] = "clean_outline" if any(g["representation"] == "clean_outline" for g in learned) else "fresh_strokes"
    measurements = measure_style(current)
    if measurements["metric_updates"]:
        current["metrics"].update(measurements["metric_updates"])
        current["metrics"]["measurement_evidence"] = copy.deepcopy(measurements["metric_update_evidence"])
    if measurements["variation_update"] is not None:
        current.setdefault("variation", {})["learned"] = copy.deepcopy(measurements["variation_update"])
    if measurements["correction_update"] is not None:
        current["corrections"] = copy.deepcopy(measurements["correction_update"])
    readiness = _readiness(current, target)
    mode = resolve_render_mode(current)
    chosen_representation = "centerline" if mode == "fresh_strokes" else "clean_outline"
    usable = [g for g in learned if g["representation"] == chosen_representation]
    if not current["glyphs"]:
        status = "awaiting_observations"
    elif not usable:
        status = "needs_geometry_review"
    else:
        status = "ready_for_document" if readiness["ready"] else "ready_for_calibration"
    stages.append({"stage": status, "reviewed_glyph_count": len(learned), "usable_glyph_count": len(usable)})
    current["profile_version"] = next_profile_version(current)
    run_dir = default_data_dir() / "training" / profile_id / current["profile_version"]
    run_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    calibration_source = calibration_input or _specimen_document(current)
    calibration_readiness = _readiness(current, calibration_source) if calibration_source else {"ready": False, "status": "no_reviewed_forms"}
    calibration_kind = "document" if calibration_readiness.get("renderable") else "atlas" if learned else "unavailable"
    calibration = {"kind": calibration_kind, "status": "planned" if learned else "unavailable", "planned_pdf_path": str(run_dir / ("calibration.pdf" if calibration_kind == "document" else "profile-atlas.pdf")) if learned else None,
                   "export_status": "see_run_report", "execution_report_path": str(run_dir / "training-report.json"),
                   "document_preflight": calibration_readiness, "physical_scale": copy.deepcopy(current["physical_scale"]),
                   "print_status": "pending_actual_size_print", "style_status": current["calibration_status"],
                   "note": "Digital sample review cannot establish the appearance of an actual-size paper print."}
    current["training"] = {"version": TRAINING_VERSION, "method": "local_reviewed_exemplar_learning", "neural_model": False,
        "status": status, "sample_count": sample_count, "source_count": len(current["sources"]),
        "reviewed_glyph_count": len(learned), "geometry_status_counts": dict(Counter(g["geometry_status"] for g in current["glyphs"])),
        "stages": copy.deepcopy(stages), "imports": imports, "measurements": measurements, "coverage": _inventory(current),
        "document_readiness": readiness, "calibration": copy.deepcopy(calibration), "report_path": str(run_dir / "training-report.json"),
        "uncertainties": list(measurements["uncertainties"]),
        "next_action": "Inspect the samples and supply labeled, reviewed vector observations; importing photos alone does not train forms." if status == "awaiting_observations" else "Compare proposed geometry with its exact source evidence and record label/geometry review." if status == "needs_geometry_review" else "Inspect the calibration PDF and an actual-size print; preflight each requested document before rendering."}
    profile_path = save_profile(current)
    if learned:
        try:
            from .rendering import profile_preview, render_document
            if calibration_kind == "document":
                rendered = render_document(calibration_source, current, Path(calibration["planned_pdf_path"]))
                calibration.update(status="rendered_pending_review", export_status="complete", pdf_path=rendered["pdf"], previews=rendered["previews"], document_path=str(Path(rendered["pdf"]).with_suffix(".document.json")), report_path=rendered["report"])
            else:
                profile_preview(current, Path(calibration["planned_pdf_path"]))
                calibration.update(status="atlas_only_pending_review", export_status="complete", pdf_path=calibration["planned_pdf_path"], note="Diagnostic atlas contains printed labels; this is not a finished handwriting document.")
        except (InkformError, OSError, ValueError) as exc:
            diagnostics.append(_error("calibration", exc))
            calibration.update(status="export_failed", export_status="failed", pdf_path=None)
    report = {"ok": not diagnostics, "profile_id": profile_id, "profile_version": current["profile_version"], "profile_path": str(profile_path),
        "training_status": status, "method": "local_reviewed_exemplar_learning", "neural_model": False,
        "sample_count": sample_count, "source_count": len(current["sources"]), "reviewed_glyph_count": len(learned),
        "default_render_mode": mode, "stages": stages, "coverage": current["training"]["coverage"],
        "document_readiness": readiness, "calibration": calibration, "measurements": measurements,
        "diagnostics": diagnostics, "imports": imports, "next_action": current["training"]["next_action"], "report_path": str(run_dir / "training-report.json")}
    report_path = Path(report["report_path"])
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    report_path.chmod(0o600)
    return report
