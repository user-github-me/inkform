"""Local-only CLI. Every operation returns JSON diagnostics."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from . import __version__
from .document import literal_document, load_document, visible_requirements
from .errors import InkformError
from .profiles import accepted_glyphs, renderable_glyphs, coverage, default_data_dir, load_profile, merge_profile, next_profile_version, resolve_render_mode, save_profile


def parser():
    p = argparse.ArgumentParser(prog="inkform", description="Private handwriting profiles and fresh vector ink.")
    p.add_argument("--version", action="version", version=f"Inkform {__version__}")
    p.add_argument("--data-dir", type=Path, help="Private data directory (default: platform user data directory)")
    sub = p.add_subparsers(dest="command", required=True)
    train = sub.add_parser("train", help="Learn and calibrate a private writer profile from samples and reviewed observations")
    train.add_argument("--id", required=True, help="Private writer profile to create or improve")
    train.add_argument("--samples", required=True, type=Path)
    train.add_argument("--prototype", type=Path, help="Optional legacy reviewed observation collection")
    train.add_argument("--observations", type=Path, help="Portable labeled observation manifest from sample review")
    train.add_argument("--calibration-document", type=Path, help="Semantic document to use for the calibration PDF")
    train.add_argument("--document", type=Path, help="Check whether the learned profile is ready for this requested document")
    mining = train.add_mutually_exclusive_group()
    mining.add_argument("--mine", action="store_true", dest="mine", default=True, help="Search every preserved page for all candidate source forms (default)")
    mining.add_argument("--no-mine", action="store_false", dest="mine", help="Skip the source collection scan for this training run")
    train.add_argument("--synthesize", action="store_true", help="Experimental: propose invented forms for review; excluded from observed rendering")
    profile = sub.add_parser("profile", help="Create, inspect, update, and check private profiles")
    ps = profile.add_subparsers(dest="operation", required=True)
    create = ps.add_parser("create")
    create.add_argument("--id", required=True)
    create.add_argument("--samples", required=True, type=Path)
    imp = ps.add_parser("import-prototype", help="Import the reviewed raster prototype as new geometry proposals")
    imp.add_argument("--id", default="personal-default")
    imp.add_argument("--prototype", required=True, type=Path)
    imp.add_argument("--representation", choices=["centerline", "clean_outline"], default="centerline")
    imp.add_argument("--update", action="store_true", help="Merge into an existing profile as a new version")
    ji = ps.add_parser("import-json", help="Validate and store a complete vector profile")
    ji.add_argument("file", type=Path)
    upd = ps.add_parser("update")
    upd.add_argument("profile")
    upd.add_argument("--samples", required=True, type=Path)
    inspect = ps.add_parser("inspect")
    inspect.add_argument("profile")
    inspect.add_argument("--preview", type=Path)
    cov = ps.add_parser("coverage")
    cov.add_argument("profile")
    cov.add_argument("--document", type=Path, required=True)
    cov.add_argument("--mode", choices=["fresh_strokes", "clean_outline"], help="Default: the writer profile's preferred mode")
    mine = ps.add_parser("mine", help="Find more candidate letter and symbol forms in the writer's sample pages")
    mine.add_argument("profile")
    mine.add_argument("--samples", type=Path, help="Default: originals already preserved with this profile")
    mine.add_argument("--characters", help="Characters to collect; default: every reviewed template")
    mine.add_argument("--role", help="Restrict candidates to an existing semantic role")
    mine.add_argument("--per-label", type=int, default=24)
    mine.add_argument("--all-candidates", action="store_true", help="Collect all qualifying unique source candidates without a per-character quota")
    mine.add_argument("--min-score", type=float, default=.58)
    mine.add_argument("--output", required=True, type=Path, help="Private review directory for crops and proposed observations")
    diversity = ps.add_parser("diversity", help="Count distinct reviewed shapes and identify where more samples would help")
    diversity.add_argument("profile")
    diversity.add_argument("--document", type=Path)
    diversity.add_argument("--mode", choices=["fresh_strokes", "clean_outline"])
    synth = ps.add_parser("synthesize", help="Experimental invented variants; excluded from normal observed rendering")
    synth.add_argument("profile")
    synth.add_argument("--characters")
    synth.add_argument("--role")
    synth.add_argument("--per-glyph", type=int, default=6)
    synth.add_argument("--seed", type=int, default=72)
    synth.add_argument("--output", required=True, type=Path)
    review = ps.add_parser("review-variants", help="Apply exact-geometry review decisions and save a new private profile version")
    review.add_argument("profile", help="Generated proposal-profile.json")
    review.add_argument("--review", required=True, type=Path)
    prefs = ps.add_parser("preferences", help="Remember source fidelity and observed correction preferences")
    prefs.add_argument("profile")
    prefs.add_argument("--corrections", choices=["sampled", "none"])
    prefs.add_argument("--variation-mode", choices=["observed", "experimental"])
    prefs.add_argument("--selected-by", default="cli-user")
    ps.add_parser("list")
    text = sub.add_parser("document", help="Convert a UTF-8 text file to literal document IR")
    text.add_argument("--text-file", required=True, type=Path)
    text.add_argument("--id", default="note")
    text.add_argument("--output", required=True, type=Path)
    render = sub.add_parser("render")
    render.add_argument("document", type=Path)
    render.add_argument("--profile", required=True)
    render.add_argument("--mode", choices=["fresh_strokes", "clean_outline"], help="Default: the writer profile's preferred mode")
    render.add_argument("--seed", type=int, default=72)
    render.add_argument("--paper", choices=["A4"], help="Enforce A4 page size")
    render.add_argument("--output", required=True, type=Path)
    render.add_argument("--preview-dpi", type=int, default=300, help="PNG preview resolution; the PDF remains vector (default: 300)")
    check = sub.add_parser("check")
    check.add_argument("pdf", type=Path)
    check.add_argument("--manifest", type=Path)
    check.add_argument("--previews", type=Path)
    check.add_argument("--preview-dpi", type=int, default=300)
    compare = sub.add_parser("compare")
    compare.add_argument("--reference", type=Path, required=True)
    compare.add_argument("--candidate", type=Path, required=True)
    compare.add_argument("--output", type=Path, required=True)
    compare.add_argument("--preview-dpi", type=int, default=300)
    return p


def _read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InkformError("invalid_json_file", str(exc), {"path": str(path)}) from exc


def _summary(profile, path=None):
    glyphs = accepted_glyphs(profile)
    by_role = {}
    for glyph in glyphs:
        by_role.setdefault(glyph["role"], set()).add(glyph["text"])
    return {"profile_id": profile["profile_id"], "profile_version": profile["profile_version"],
            "path": str(path) if path else None, "synthetic": profile["synthetic"],
            "glyph_count": len(profile["glyphs"]), "reviewed_glyph_count": len(glyphs),
            "renderable_glyph_count": len(renderable_glyphs(profile)),
            "variation_mode": profile.get("variation", {}).get("mode", "observed"),
            "default_render_mode": resolve_render_mode(profile),
            "coverage": {role: sorted(chars) for role, chars in by_role.items()},
            "calibration_status": profile["calibration_status"], "import_report": profile.get("import_report")}


def run(args):
    if args.data_dir:
        os.environ["INKFORM_DATA_DIR"] = str(args.data_dir.expanduser().resolve())
    if args.command == "train":
        from .training import train_profile
        return train_profile(args.samples, args.id, prototype=args.prototype, observations=args.observations,
                             calibration_document=args.calibration_document, document=args.document, mine=args.mine,
                             synthesize=args.synthesize)
    if args.command == "profile":
        op = args.operation
        if op == "list":
            base = default_data_dir() / "profiles"
            return {"data_directory": str(default_data_dir()), "profiles": [_summary(load_profile(p.name)) for p in sorted(base.glob("*")) if p.is_dir() and (p / "current.json").exists()]}
        if op in {"create", "update", "import-prototype"}:
            from .ingest import import_prototype, import_samples
            if op == "import-prototype":
                profile = import_prototype(args.prototype, args.id, args.representation)
                if args.update:
                    profile = merge_profile(load_profile(args.id), profile)
            elif op == "update":
                profile = import_samples(args.samples, existing_profile=load_profile(args.profile))
            else:
                profile = import_samples(args.samples, profile_id=args.id)
            path = save_profile(profile)
            return _summary(profile, path)
        if op == "import-json":
            profile = load_profile(args.file)
            return _summary(profile, save_profile(profile))
        profile = load_profile(args.profile)
        if op == "synthesize":
            from .synthesis import synthesize_variants, write_synthesis_review
            result = synthesize_variants(profile, args.characters, args.per_glyph, args.seed, role=args.role)
            return {"ok": True, **result["summary"], **write_synthesis_review(result, args.output)}
        if op == "review-variants":
            from .synthesis import approve_variants
            review = _read_json(args.review)
            if not isinstance(review, dict) or not isinstance(review.get("decisions"), list) or not review["decisions"]:
                raise InkformError("invalid_variant_review", "Supply a nonempty decisions list and an identified reviewer.")
            reviewed = approve_variants(profile, review.get("decisions", []), review.get("reviewed_by"))
            current_path = default_data_dir() / "profiles" / profile["profile_id"] / "current.json"
            current = load_profile(profile["profile_id"]) if current_path.exists() else profile
            # Retain concurrent evidence/profile preferences; merge only the
            # requested derived decisions, never an old snapshot wholesale.
            decisions = {row["glyph_id"] for row in review.get("decisions", [])}
            updated = merge_profile(current, {"glyphs": [g for g in reviewed["glyphs"] if g["glyph_id"] in decisions]})
            return _summary(updated, save_profile(updated))
        if op == "preferences":
            if args.corrections is None and args.variation_mode is None:
                raise InkformError("missing_preference", "Choose --corrections or --variation-mode.")
            if args.corrections is not None:
                profile.setdefault("output_preferences", {}).update(corrections=args.corrections, selected_by=args.selected_by)
            if args.variation_mode is not None:
                profile.setdefault("variation", {})["mode"] = args.variation_mode
            profile["profile_version"] = next_profile_version(profile)
            return _summary(profile, save_profile(profile))
        if op == "mine":
            from .mining import mine_candidates
            return mine_candidates(profile, args.samples, args.output, characters=args.characters,
                                   role=args.role, per_label=None if args.all_candidates else args.per_label, min_score=args.min_score)
        if op == "diversity":
            from .diversity import profile_diversity
            return profile_diversity(profile, load_document(args.document) if args.document else None, args.mode)
        if op == "coverage":
            mode = resolve_render_mode(profile, args.mode)
            rep = "centerline" if mode == "fresh_strokes" else "clean_outline"
            eligible = {**profile, "glyphs": [g for g in profile["glyphs"] if
                (g["role"] != "mark.correction" and g["representation"] == rep)
                or (g["role"] == "mark.correction" and g["representation"] == "centerline")
                or (g["representation"] == "centerline" and g["role"] == "math.operator" and g["text"] in {"−", "√"})]}
            document = load_document(args.document)
            needed = visible_requirements(document)
            needed.extend(("mark.correction", "crossout") for block in document["blocks"]
                          for run in block.get("children", []) if run["type"] in {"correction", "math_correction"})
            needed.extend(("mark.correction", "crossout") for block in document["blocks"]
                          if block["type"] == "display_math" and "deleted_expression" in block)
            result = coverage(eligible, needed)
            return {"ok": result["complete"], "mode": mode, **result}
        result = _summary(profile)
        if args.preview:
            from .rendering import profile_preview
            result["preview"] = profile_preview(profile, args.preview)
        return result
    if args.command == "document":
        if args.text_file.resolve() == args.output.resolve():
            raise InkformError("input_output_conflict", "Document output must differ from the source text file.")
        with args.text_file.open("r", encoding="utf-8", newline="") as f:
            document = literal_document(f.read(), args.id)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return {"document": str(args.output.resolve()), "content_mode": "literal"}
    if args.command == "render":
        from .rendering import render_document
        document = load_document(args.document)
        if args.paper:
            declared = document.get("page", {})
            if declared.get("width_mm", 210) != 210 or declared.get("height_mm", 297) != 297:
                raise InkformError("paper_conflict", "--paper A4 conflicts with the document page dimensions.")
        return render_document(document, load_profile(args.profile), args.output, args.seed, args.mode, preview_dpi=args.preview_dpi)
    if args.command == "check":
        from .validation import check_pdf
        return check_pdf(args.pdf, _read_json(args.manifest) if args.manifest else None, args.previews, preview_dpi=args.preview_dpi)
    if args.command == "compare":
        from .validation import compare
        return compare(args.reference, args.candidate, args.output, preview_dpi=args.preview_dpi)


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        result = run(args)
        print(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False))
        return 0 if result.get("ok", True) else 2
    except InkformError as exc:
        print(json.dumps(exc.as_dict(), indent=2, ensure_ascii=False), file=sys.stderr)
        return 2
    except (OSError, ValueError, KeyError) as exc:
        print(json.dumps({"error": "operation_failed", "message": str(exc)}, indent=2), file=sys.stderr)
        return 2
