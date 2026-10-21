# Rendering and checking

```sh
inkform profile coverage personal-default --document document.json
inkform render document.json --profile personal-default --seed 72 --paper A4 --output build/notes.pdf
inkform check build/notes.pdf --manifest build/notes.build.json
```

`fresh_strokes` requires reviewed centerlines. `clean_outline` requires reviewed closed outlines at a compatible physical size and nominal pen calibration; scripts and extended primitives need suitable centerlines or explicitly reviewed variants. Rendering never reads a source bitmap. Missing coverage and layout overflow return structured errors with exit code 2, before replacing the deliverable.

Omitting `--mode` honors the profile's `default_render_mode` (older profiles default to `fresh_strokes`). Explicit modes remain strict: choosing fuller outlines does not silently substitute centerlines for missing script sizes. Retain both representations under different glyph IDs when adding refined outlines to a profile.

Previews default to 300 dpi. `--preview-dpi` on render/check/compare changes only raster inspection artifacts, never the PDF paths. A pixelated preview and a faceted path are different issues: compare an actual high-resolution PDF raster with the enlarged preview before choosing the fix. For rough outline edges, `inkform.smoothing.smooth_outline_glyph` fits continuous cubic curves with bounded boundary movement and topology/ink-area guards. Inspect the new forms, keep their provenance, and explicitly review them before use. Raising preview resolution alone cannot fix rough geometry.

The manifest records the frozen profile/document hashes, profile and renderer versions, seed, selected glyph IDs, source links, absolute path geometry, ink bounds, and geometry hash. Stable node/token identifiers isolate variant choices. Repeated identical inputs and seeds produce identical geometry; PDF metadata is also deterministic. Per-occurrence variation prevents one selected form from becoming an identical stamp throughout the page, including in mathematical symbols. Coherent word transforms preserve neighboring letter relationships. Learned variation and conservative estimates are distinguished in the export record.

Each instance's `variation` records the chosen width, height, shear, baseline,
and smooth shape-warp factors, their `basis`, `estimated_axes`, and any source
glyph/observation IDs. Correction records distinguish `sampled_restart` from
explicit document changes and retain the mark, deleted-text, and replacement
token IDs. The deleted ink and strike are nonsemantic; the intact replacement
accounts for the intended content. All correction marks require reviewed
centerlines, including when the body uses clean outlines.

Read the actual report. The checker parses physical page boxes, PDF path operators and recursive resources, actual painted bounds, page rasterization, and visible ink; it verifies the PDF hash against the manifest and checks planned safe margins. The ordinary document path contains no image objects. Previews are separate raster artifacts. Diagnostic profile atlases have printed labels explicitly separate from the handwritten forms.

Open every preview and inspect repeated letter shapes, arrow lengths, letter identity, collisions, dots, scripts, limit directions, and prose/math scale. For corrections, inspect the strike or scribble and the intact replacement, including line and page boundaries. Check the frequency against the representative sample evidence; arbitrary extra messiness is not a resemblance improvement. Automated path checks do not certify correctness or personal resemblance. `content_accounted_for` concerns preserved semantic tokens; `math_correctness` remains unchecked unless verified outside the renderer. Print-proof and user calibration stay pending until actually performed.

`inkform compare --reference baseline.pdf --candidate notes.pdf --output comparison` makes a common-scale contact sheet, a vector `comparison.pdf`, and separate structural reports. The PDF places original pages side by side at actual physical size, with a separate diagnostic label strip; it does not flatten them into images. Different content or layout weakens stylistic comparison. Use the same calibration text and seed for outline refinements; preserve glyph advances. The earlier raster PDF remains a historical reference. Image differences are not a handwriting identity percentage.
