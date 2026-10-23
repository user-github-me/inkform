# Profiles and review

`profile.json` uses schema version `1.0`; the machine schema is `src/inkform/schemas/profile.schema.json` in the project. `profile import-json` validates it before creating an immutable private snapshot. `profile update` retains unrelated records and creates a new version.

Coordinates are x-height units, baseline y=0, positive y downward. Bounds always mean `[min_x, min_y, max_x, max_y]`; source crops separately use `bbox_xywh`. A glyph has a unique `glyph_id`, grapheme `text`, explicit `role`, positive `advance`, conservative `ink_bounds`, vector `strokes`, source references, and separate label/geometry review states. Roles are `text`, `math.variable`, `math.digit`, `math.operator`, `math.function`, and `mark.correction`. Correction marks use `text: "crossout"` and reviewed centerlines, with their own provenance and coverage.

Each stroke has `start`, `segments`, and optional `closed` and `width_profile`. A segment is a `line` with `end`, or a `cubic` with `control1`, `control2`, and `end`. Width-profile samples contain normalized arc-length `s` and a multiplier; positions increase from 0 through 1. Outlines are closed contours with even-odd fill and explicit physical calibration. A path is neither an embedded image nor an inferred time series.

Only forms with both `label_status: reviewed` and `geometry_status: reviewed` and a named `reviewed_by` enter coverage. In the default observed mode, derived forms are excluded even when reviewed. Personal forms retain references to stored source IDs. `proposed` and `rejected` candidates remain available for diagnosis. `dynamics` retains unknown timing, pressure, and stroke order for photographs.

The prototype adapter writes a private `candidates.json`, paired review PNGs, and `review.json`. Each review entry is keyed by `observation_id` and includes the matching `mask_sha256`, `geometry_sha256`, `label_status`, `geometry_status`, and `reviewed_by`. Inspect the pairs before recording decisions. Unmatched hashes invalidate acceptance; rerun `profile import-prototype --update` after editing the review ledger.

`profile create --id NAME --samples FOLDER` preserves photos/PDFs and orientation-normalized analysis images but creates no accepted glyphs. Prefer the coordinating `train` command and [portable observations](observations.md) to learn from labeled crops and review the fitted paths. The portable importer keys decisions by `candidate_id`, separately for each representation. Existing reviewed prototype records can also be reused. Never ask for an entire alphabet when only a few relevant forms are missing.

Initial scale and pen values are calibration choices, not measurements established by image dimensions. Review records must distinguish agent observations from user feedback. Missing role/context coverage is an explicit diagnostic. A reviewed form in another role needs a separate record with compatibility provenance; dictionary replacement is not a role transfer.

## Variation and correction evidence

Optional `source_context` on a glyph or its referenced observation records
`kind` (`body`, `capital`, `baseline_digit`, `superscript`, or `subscript`),
`review_status`, and `reviewed_by`. It requires actual reviewed source context;
small geometry alone does not establish that an occurrence was an exponent.
Object annotations bind to an identified reviewed observation and its matching
stored source hash; a glyph annotation must retain that observation reference.
Older string-valued `source_context` notes remain valid, but do not establish
reviewed context categories.
For script forms, `normalization: context_xheight` means the stored geometry
uses the script's own x-height and can use normal script layout scaling.
`body_xheight` or unknown script normalization needs calibration before use,
otherwise the engine would shrink an already-small source form again.

Selection first favors compatible source context and size, then balances
genuine variants in that pool. Literal case and word-initial position remain
separate: a first letter is not automatically capitalized. Text and mathematical
numbers use comparable selected neighbors, including repeated characters;
explicit superscript/subscript layout applies its reduction once.

Size comparisons are geometry estimates even for reviewed context annotations.
Latin lowercase bodies use ink above the baseline, excluding descenders and
detached i/j dots; ascenders and capitals use the writer's ordinary stem scale.
An i/j stem can start below the ordinary body zone, so it compares locally
only with another occurrence of the same letter; independent bank checks
still apply. Digits use digit ink height. Marks and operators do not inherit letter-height
requirements. A selected neighbor must have independent bank evidence before
it can establish a clear mismatch, and comparable neighbors must fit in both
directions. Sparse evidence remains disclosed; marginal fallback favors the
closest genuine variant, while clear incompatibility reports a source gap
before emission. The actual-PDF context-size audit checks emitted vector paths
against both bank and selected-neighbor references.

Calibrate source x-height from ordinary lowercase bodies in the same writing
context, retaining source boxes, baseline, hashes, and review evidence. A
capital's full height or a descender is not the x-height denominator. Correct
a mistaken denominator uniformly in a new reviewed profile version, retaining
the original source contour and history. Shape distortion does not repair a
mislabeled or poorly calibrated donor.

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
Optional `strokes` hold reviewed `stroke_id`, `role` (`curve`, `tick`,
`marker_open`, or `marker_filled`), source `trace_px`, and `closed`. Curves
retain `anchors_px` for their underlying shape; markers retain `center_px`.
Observed rendering transfers the measured residual shape of axes/curves and
the actual tick/marker contour. It keeps curve data anchors and axis endpoints
exact; ticks keep their data centers and must visibly intersect their axis.
The manifest carries the reviewed evidence so actual-PDF checks can rebuild
the expected paths. Missing trace roles use declared geometry and are reported
as fallbacks. Static trace transfer does not recover pen movement.

To retain an actual mode preference, create an immutable version. Omit the feedback field until feedback exists; record the user's words accurately rather than inventing approval. This example saves only the mode:

```python
from inkform.profiles import load_profile, merge_profile, save_profile

current = load_profile("my-writing")
updated = merge_profile(current, {"default_render_mode": "clean_outline"})
save_profile(updated)
```

The other mode is `fresh_strokes`. Rendering without `--mode` uses this stored preference. A command-line override applies only to that export. Mode selection is separate from glyph review and actual-size print calibration; it does not approve proposed geometry or establish physical similarity.
