# Rendering and checking

```sh
inkform profile coverage personal-default --document document.json
inkform render document.json --profile personal-default --seed 72 --paper A4 --output build/notes.pdf
inkform check build/notes.pdf --manifest build/notes.build.json
```

`fresh_strokes` requires reviewed centerlines. `clean_outline` requires reviewed closed outlines at a compatible physical size and nominal pen calibration, including separately calibrated script sizes. Expandable bars and radicals use reviewed `−` and `√` centerlines alongside the outline body. Rendering never reads a source bitmap. Missing coverage and layout overflow return structured errors with exit code 2, before replacing the deliverable.

Omitting `--mode` honors the profile's `default_render_mode` (older profiles default to `fresh_strokes`). Explicit modes remain strict: choosing fuller outlines does not silently substitute centerlines for missing script sizes. Retain both representations under different glyph IDs when adding refined outlines to a profile.

Previews default to 300 dpi. `--preview-dpi` on render/check/compare changes only raster inspection artifacts, never the PDF paths. A pixelated preview and a faceted path are different issues: compare an actual high-resolution PDF raster with the enlarged preview before choosing the fix. For rough outline edges, `inkform.smoothing.smooth_outline_glyph` fits continuous cubic curves with bounded boundary movement and topology/ink-area guards. Inspect the new forms, keep their provenance, and explicitly review them before use. Raising preview resolution alone cannot fix rough geometry.

The manifest records the frozen profile/document hashes, profile and renderer versions, seed, selected glyph IDs, source links, absolute path geometry, ink bounds, and geometry hash. Repeated identical inputs and seeds produce identical geometry; PDF metadata is also deterministic. Stable node/token identifiers seed source selection, but inserting nearby writing can change the selected occurrence. The default `variation.mode: observed` rotates real reviewed source forms, preferring unused or least-recently used examples before reuse. It excludes all derived forms and preserves the selected construction under uniform scaling and placement. It does not blend letters, warp their contours, or redraw arrow heads and shafts to force novelty. Experimental mode retains those older behaviors for an explicitly requested experiment; its estimates remain separate from observed evidence.

Each instance's `variation` records its transform factors and basis, including
source glyph/observation IDs and any estimated axes. Observed output has no
shear or shape warp; inspect source selection as well as the rendered ink.
Correction records distinguish `sampled_restart` from
explicit document changes and retain the mark, deleted-text, and replacement
token IDs. The deleted ink and strike are nonsemantic; the intact replacement
accounts for the intended content. All correction marks require reviewed
centerlines, including when the body uses clean outlines.

Read the actual report. The checker parses physical page boxes, PDF path operators and recursive resources, actual painted bounds, page rasterization, and visible ink; it verifies the PDF hash against the manifest and checks planned safe margins. The ordinary document path contains no image objects. Previews are separate raster artifacts. Diagnostic profile atlases have printed labels explicitly separate from the handwritten forms.

New exports mark glyph boundaries inside the PDF without adding visible text. The checker reconstructs the emitted ink and verifies it against the manifest, then compares occurrences of the same character on the same and adjacent pages, including across prose and math roles. Exact copies include translated, reordered, reversed, and cyclically shifted paths. Near copies use visible-ink boundary distance on a 0.025 mm grid, normalized to a nominal 3 mm writing size; ordinary pairs below 0.035 mm are reported. Repeated prose words are also compared after per-letter scale/shear normalization. The final check uses the emitted PDF, not only planned coordinates or variant IDs. Legacy unmarked PDFs can only provide a manifest-based repetition result, which is labeled accordingly.

In observed mode, exact and near repeats are review findings, not an export failure. The finite source bank may legitimately repeat; collect more usable source occurrences when available and disclose remaining reuse. Content, vector integrity, bounds, and other structural failures still block delivery. In experimental mode, ordinary exact and near repeats retain the stricter failure behavior and may produce `repetition_exhausted`. Do not switch to experimental deformation merely to remove a repeat finding.

Simple dots and straight bars have a separate limited-range report. A comma or apostrophe qualifies only when its painted ink is at most 1.2 mm in each dimension at the normalized 3 mm writing size, has one connected component, no enclosed counter, and a compact boundary. A semicolon may have two such components within 1.2 × 2.4 mm. Larger flourishes or intricate punctuation shapes receive the ordinary comparison. The comparison window includes adjacent pages, not an unlimited prohibition across a whole book. Inspect the report's `geometry_source`, `exact_count`, `near_count`, `limited_count`, and per-pair references. A valid PDF with repeat findings is not evidence of unique handwriting throughout the document.

Open every preview and inspect repeated letter shapes, arrow lengths, letter identity, collisions, dots, scripts, limit directions, and prose/math scale. For corrections, inspect the strike or scribble and the intact replacement, including line and page boundaries. Check the frequency against the representative sample evidence; arbitrary extra messiness is not a resemblance improvement. Automated path checks do not certify correctness or personal resemblance. `content_accounted_for` concerns preserved semantic tokens; `math_correctness` remains unchecked unless verified outside the renderer. Print-proof and user calibration stay pending until actually performed.

`inkform compare --reference baseline.pdf --candidate notes.pdf --output comparison` makes a common-scale contact sheet, a vector `comparison.pdf`, and separate structural reports. The PDF places original pages side by side at actual physical size, with a separate diagnostic label strip; it does not flatten them into images. Different content or layout weakens stylistic comparison. Use the same calibration text and seed for outline refinements; preserve glyph advances. The earlier raster PDF remains a historical reference. Image differences are not a handwriting identity percentage.
