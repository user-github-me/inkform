# Profiles and review

`profile.json` uses schema version `1.0`; the machine schema is `src/inkform/schemas/profile.schema.json` in the project. `profile import-json` validates it before creating an immutable private snapshot. `profile update` retains unrelated records and creates a new version.

Coordinates are x-height units, baseline y=0, positive y downward. Bounds always mean `[min_x, min_y, max_x, max_y]`; source crops separately use `bbox_xywh`. A glyph has a unique `glyph_id`, grapheme `text`, explicit `role`, positive `advance`, conservative `ink_bounds`, vector `strokes`, source references, and separate label/geometry review states. Roles are `text`, `math.variable`, `math.digit`, `math.operator`, `math.function`, and `mark.correction`. Correction marks use `text: "crossout"` and reviewed centerlines, with their own provenance and coverage.

Each stroke has `start`, `segments`, and optional `closed` and `width_profile`. A segment is a `line` with `end`, or a `cubic` with `control1`, `control2`, and `end`. Width-profile samples contain normalized arc-length `s` and a multiplier; positions increase from 0 through 1. Outlines are closed contours with even-odd fill and explicit physical calibration. A path is neither an embedded image nor an inferred time series.

Only forms with both `label_status: reviewed` and `geometry_status: reviewed` and a named `reviewed_by` enter coverage. In the default observed mode, derived forms are excluded even when reviewed. Personal forms retain references to stored source IDs. `proposed` and `rejected` candidates remain available for diagnosis. `dynamics` retains unknown timing, pressure, and stroke order for photographs.

The prototype adapter writes a private `candidates.json`, paired review PNGs, and `review.json`. Each review entry is keyed by `observation_id` and includes the matching `mask_sha256`, `geometry_sha256`, `label_status`, `geometry_status`, and `reviewed_by`. Inspect the pairs before recording decisions. Unmatched hashes invalidate acceptance; rerun `profile import-prototype --update` after editing the review ledger.

`profile create --id NAME --samples FOLDER` preserves photos/PDFs and orientation-normalized analysis images but creates no accepted glyphs. Prefer the coordinating `train` command and [portable observations](observations.md) to learn from labeled crops and review the fitted paths. The portable importer keys decisions by `candidate_id`, separately for each representation. Existing reviewed prototype records can also be reused. Never ask for an entire alphabet when only a few relevant forms are missing.

Initial scale and pen values are calibration choices, not measurements established by image dimensions. Review records must distinguish agent observations from user feedback. Missing role/context coverage is an explicit diagnostic. A reviewed form in another role needs a separate record with compatibility provenance; dictionary replacement is not a role transfer.

## Variation and correction evidence

`variation.mode` is `observed` by default, including when absent from an older profile. It selects reviewed source forms, excludes `derivation` records, and preserves their internal geometry. Uniform scale and layout placement remain available; interpolation, shape warps, and invented letter or arrow curves are disabled. `experimental` explicitly enables the earlier deformation and derived-form behavior. Save the choice with `profile preferences my-writing --variation-mode observed|experimental`. The normal workflow uses observed mode.

`variation.learned` records the model with `version: "inkform-variation-1"` and `basis: "reviewed_samples"`. Its `by_glyph` rows contain `role`, `text`, relative `width` and `height` amplitudes, `source_count`, `glyph_ids`, and `observation_ids`. A row needs at least two distinct reviewed source occurrences of that form; repeated representations or aliases do not count twice. Width and height amplitudes are bounded at 0.15 and 0.12. Missing evidence stays a reported gap; observed rendering does not invent an internal shape distribution.

A glyph may set `variation_dimensions_observed: false` when its width/height reflects a crop choice or a context transfer rather than an observed standalone form. Training retains its reviewed shape but excludes it from learned dimensional variation, and reports the excluded IDs. Document the reason and role transfer; a cropped graph-axis segment does not measure the writer's natural tends-arrow length.

Generated style candidates have `derivation.version: inkform-style-family-1`,
`basis: writer_conditioned_estimate`, exact parent IDs/hashes, their own geometry
hash, and separate review status. Their `source_refs` equal the union of the
recorded parents; `independent_observation_count` is zero and
`variation_dimensions_observed` is false. Parents must be reviewed source forms
of the same character, role, and representation, never other generated forms.
Use the synthesis/review CLI to maintain these bindings only for an explicitly
requested experiment. Reviewed derived forms are available in experimental
mode, excluded from observed mode, and cannot inflate measured sample counts.

At least three reviewed adjacent word gaps across two lines inform `word_gap` (bounded at 0.25), `word_gap_sample_count`, and `word_gap_observation_ids`. A zero sample count means unavailable evidence, not measured uniform spacing. See [observations.md](observations.md) for source hashes and layout-review requirements. `variation.amount` is optional, from 0 through 0.3, with default 0.1. Zero disables per-occurrence transforms while genuine source selection remains available. Observed mode restricts transforms to uniform scale and layout placement regardless of amount. Experimental slant and shape-warp axes remain estimates even when width and height statistics come from samples.

`corrections` records `rate`, `basis`, `sampled_word_count`, `correction_count`, `observation_ids`, and `source_refs`. Personal learning uses `basis: "reviewed_samples"`; only authored public fixtures use `synthetic_demo`. The rate is bounded at 0.35 and is derived from reviewed representative passage counts, independently of the correction-mark geometry. No frequency evidence means no inferred habit. A reviewed mark alone can support an explicitly supplied document correction without establishing a rate.

`output_preferences: {"corrections":"sampled","selected_by":"writer"}` records
a correction choice separately from its measured habit. `none` disables sampled
restarts; an explicit document choice takes precedence. The preferences CLI
saves a new immutable version.

`graph_style` optionally stores `version: inkform-graph-style-1`,
`basis: reviewed_samples`, `observation_ids`, matching `source_refs`,
`pen_width_xheight` (0.02–0.5), and `axis_bow_xheight` with horizontal and vertical
values (0–2). Each referenced `kind: graph_style` observation retains a source
hash, reviewed actor/status, crop `bbox_xywh`, `xheight_px`, `pen_width_px`, and
`axes` with `orientation` and absolute source `trace_px` points. Validation
recomputes perpendicular deviation from each endpoint chord and aggregates
the reviewed measurements. Record static ink width only, not inferred pressure.
Observed graph rendering uses the reviewed pen width and explicit graph
geometry without added axis bow. Experimental rendering can use the reviewed
bow extent, with new direction/path choices still identified as estimates.
Both modes keep the supplied data anchors fixed.

To retain an actual mode preference, create an immutable version. Omit the feedback field until feedback exists; record the user's words accurately rather than inventing approval. This example saves only the mode:

```python
from inkform.profiles import load_profile, merge_profile, save_profile

current = load_profile("my-writing")
updated = merge_profile(current, {"default_render_mode": "clean_outline"})
save_profile(updated)
```

The other mode is `fresh_strokes`. Rendering without `--mode` uses this stored preference. A command-line override applies only to that export. Mode selection is separate from glyph review and actual-size print calibration; it does not approve proposed geometry or establish physical similarity.
