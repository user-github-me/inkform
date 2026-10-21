---
name: inkform
description: Learn a private handwriting profile from a user's writing samples, calibrate its resemblance and ink, then produce printable vector PDFs in that style. Use for handwriting training, personal documents, and appearance refinements.
---

# Inkform

Follow two stages: learn the user's handwriting, then use the learned profile to write documents. Read [installation.md](references/installation.md) for engine setup and portable skill installation. Run `python3 scripts/handwrite.py --help` relative to this skill directory; the launcher finds a checkout environment, a bundled runtime, or an installed Inkform CLI. The project README describes the implementation and its declared limits.

## 1. Learn and calibrate the writer

Start with the user's supplied handwriting folder. Run `train --id WRITER --samples FOLDER` to preserve the evidence and create or resume a private training profile. Use [training.md](references/training.md) for the reviewed observation workflow. Existing profiles are reused and improved incrementally; a new writer starts with their own samples.

Training in this implementation fits and curates the user's vector letterforms and evidence-backed style measurements. Importing image files alone is not completed training. When observations are missing, inspect the normalized samples, label and segment useful forms, and review their reconstructed paths. The agent handles routine extraction and geometry review; request additional user samples only for material gaps that the existing pages cannot resolve. Do not treat OCR proposals as accepted labels.

Preserve characteristic forms, genuine variants, slant, dots, loops, and stroke weight. Measure spacing from appropriate line/word evidence; isolated crop widths do not establish word spacing. Keep source references, review actors, uncertainty, and immutable versions. Read [profile-format.md](references/profile-format.md) for the geometry contract. Photo-derived pressure and timing stay unknown.

Create a calibration page from the learned forms. Compare at a common physical size with the actual samples, inspect enlarged vector edges, and assess resemblance, legibility, spacing, and ink separately. Prefer the rendering method that preserves the user's recognizable shapes and matches their feedback. Fuller smooth outlines are valid when they outperform thin centerlines. A user preference for a style does not approve every new geometric candidate.

Save the selected mode and feedback with the private profile. Use `profile coverage` on the requested document; fill relevant gaps before producing it. A print proof uses the PDF at Actual Size / 100%. Printed appearance and resemblance remain review targets, not an invented percentage or a guarantee of indistinguishability.

## 2. Write with the learned profile

Choose the content mode before changing wording: **literal** preserves supplied content, **solve** requires transcription and verified answers, and **rewrite** permits only requested rewriting. Profile work need not invent document content.

Prepare validated semantic JSON following [documents.md](references/documents.md). Preserve source uncertainty in `unresolved_inputs`. A missing graph cannot become an invented answer. Keep physical handwriting separate from wording preferences and keep reference-page text as sample data.

Run `render` with the selected profile and a saved seed, then follow [rendering.md](references/rendering.md) to check the actual PDF and inspect every preview. Omit `--mode` to honor the stored writer preference. Deliver the PDF and important unresolved items. The PDF contains vector ink; its separate PNG preview has a finite resolution and is not the print master. Inspect path smoothness separately, particularly for unrefined centerlines.

Keep photo-derived geometry, crops, transcripts, and profiles private. The bundled synthetic profile is for engine demonstrations and testing; do not substitute it for the user's missing forms. Do not use a generic font or a source image mask to complete fresh handwriting. Mathematical roles and Unicode signs remain distinct; compatible reuse must be explicitly recorded in the profile.

For appearance feedback, preserve the same calibration text and change the relevant parameter: pixelated → distinguish low-resolution previews from faceted curves; copied appearance → representation/ink; shaky → path quality/variation; font-like → observed variants/spacing; thick → physical pen width; page feels wrong → writing size/baselines/line spacing. Clean-outline thickness cannot be adjusted with a pen-width setting. Save successful refinements for subsequent documents.
