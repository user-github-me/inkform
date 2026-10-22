# Changelog

## 0.4.0 — 2026-10-22

- Propose alternate letter constructions from the writer's reviewed forms with `profile synthesize` and `train --synthesize`. Complete PDF/PNG review atlases and exact-hash decisions keep inferred variants separate from observed evidence.
- Audit repeated prose words after removing per-letter scale and shear; guide selection toward different approved constructions across the same and neighboring pages. Spacing changes cannot bypass this check.
- Remember clean or sample-based correction preferences while retaining multiple reviewed strike and scribble styles. Explicit supplied deletions remain intact.
- Draw explicit graphs with writer-shaped labels and arrowheads, reviewed source axis bow/ink width, preserved data anchors and ticks, and distinct open/filled markers. Verify emitted graph geometry against its declared content.
- Add a copyable agent prompt and source/output examples to the introduction website. Public examples use authored synthetic handwriting; personal samples and generated documents stay private.

## 0.3.0 — 2026-10-22

- Reject exact and near copies of ordinary letter and symbol shapes on the same and adjacent pages. Audit the actual emitted PDF paths, with separate limited-range reporting for simple dots and bars.
- Rotate distinct source families, blend compatible reviewed centerlines, and apply bounded smooth shape changes. Vary arrow shaft curvature and head proportions independently while preserving direction.
- Search a bounded candidate pool and fail with an actionable `repetition_exhausted` diagnostic when usable source variety is insufficient; changing IDs or tiny numeric values cannot bypass the check.
- Add `profile diversity`, `profile mine`, and `train --mine` for source-aware collection requests and private, reviewable candidate queues. Mining never automatically accepts labels or fitted geometry.
- Repeat known forms in the default calibration and teach the skill to collect, review, render, inspect, and refine using the actual-PDF report.

## 0.2.0 — 2026-10-22

- Vary individual letters and mathematical symbols, including repeated arrow lengths, while retaining smooth vector output and reproducible seeds.
- Learn bounded width and height variation from distinct reviewed examples of the same form, and word-gap variation from reviewed consecutive word boxes. Sparse evidence remains identified as an estimate.
- Learn correction frequency from reviewed representative passage counts and use the writer's reviewed strike or scribble geometry for crossed-out word restarts. Profiles without an observed habit receive no automatic cross-outs.
- Support explicit deleted-text/replacement correction runs and a document setting to disable sampled corrections while preserving semantic content and plain-text sidecars.
- Update the agent skill and calibration guidance to collect repeated forms, ordinary spacing, and correction evidence rather than applying arbitrary messiness.

## 0.1.2 — 2026-10-21

- Publish the npm distribution as `@nazifbd/inkform`; the executable remains `inkform`.
- Update the website, installation instructions, and portable skill for the scoped package.
- Support upgrading a verified earlier standalone skill installation.

The unscoped name was rejected by npm because it was too similar to an existing package. Version 0.1.1 remains available through GitHub release archives.

## 0.1.1 — 2026-10-21

First public release of Inkform, a local handwriting engine and agent skill.

- Sample intake, portable labeled observations, exact-evidence geometry review, and reusable private writer profiles.
- Training reports distinguish imported samples, pending review, calibration, and readiness for a requested document.
- Smooth cubic outlines preserve observed ink shapes; centerline rendering supports adjustable strokes and mathematical constructions.
- Literal text and semantic mathematics produce A4 vector PDFs, image previews, text sidecars, and verification reports.
- npm launcher with explicit Python environment setup and standalone skill installation.
- Public introduction site, synthetic examples, contribution guidance, and MIT licensing.

This release uses reviewed exemplar geometry, not neural model training. Automatic segmentation, connected cursive, learned word spacing, and verified physical resemblance remain development and calibration work.
