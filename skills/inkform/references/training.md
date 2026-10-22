# Training from user samples

The intended order is **samples → learned writer profile → calibration → requested PDF**. The local training model is a set of fitted vector forms, observed variants, and measured style statistics, retained privately and improved with more examples. This implementation does not train a neural network or recover motion/pressure from photographs.

```sh
inkform train --id my-writing --samples ./writing-samples
```

This preserves photographs/PDF pages, hashes, orientation, and available source observations. A new folder of unlabelled photographs returns `awaiting_observations`; importing files is not sufficient evidence to say the handwriting is trained.

Inventory the full supplied corpus, including preserved pages from earlier imports. Record which pages have been inspected, which observations are accepted or pending, and which characters or contexts remain unresolved. Create a portable observation manifest following [observations.md](observations.md), including its coordinate conventions and exact review procedure. Label and segment usable letter/symbol forms, retain detached dots, and identify ambiguous joins. Keep coordinate transforms, source hashes, baseline estimates, and x-height evidence. Reviewed labels do not automatically approve newly fitted geometry. The importer generates outlines/centerlines for comparison, with hashes that bind later geometry decisions to the specific source and fitted paths.

```sh
inkform train --id my-writing --samples ./writing-samples \
  --observations ./reviewed-observations.json
```

Review the candidate geometry against the originals. Keep correct characteristic forms and variants; leave doubtful forms proposed. Collect every usable distinct occurrence found during review, including letters, digits, punctuation, mathematical signs, arrows, and corrections. Do not stop at a complete alphabet or a quota for the next document. Include the normal range of widths, slants, endings, and tidy or hurried forms visible in the supplied pages. Inspect word context before deciding that an unusual curve is a real alternate construction: it may belong to a neighboring letter or crossing stroke. Duplicating one crop under several IDs is not evidence of variation. Use smooth outlines when they best preserve the user's ink weight and recognizable shapes; smoothing must not create extra flourishes or reinterpret the letter. Routine candidate review belongs to the agent; ask the user for extra evidence only when actual missing forms or ambiguous content cannot be resolved.

For an existing legacy collection, `--prototype ./output` reuses its reviewed evidence. A later run loads the current profile and preserves reviewed refinements; it must not replace them with a rougher automatic reconstruction.

```sh
inkform train --id my-writing --samples ./writing-samples \
  --calibration-document calibration.json --document requested-document.json --mine
```

Training reports separate stages:

- `awaiting_observations`: sources are imported, but accepted letter evidence is absent.
- `needs_geometry_review`: candidates exist, with insufficient accepted forms.
- `ready_for_calibration`: reviewed forms can be inspected; readiness is not a claim of universal coverage.
- `ready_for_document`: the supplied document passes role coverage and physical layout preflight with no declared unresolved inputs. A printable partial document reports `renderable: true`, `ready: false`, and `content_complete: false`.

A requested calibration document is rendered when the selected profile can cover it. Otherwise, use the diagnostic vector atlas and a targeted calibration passage drawn from known forms. Diagnostic printed labels are not substitutes in a handwritten document. Calibration and training artifacts stay in private storage.

The default specimen repeats each covered character or symbol four times, so calibration can reveal stamped forms. Training reports a `diversity` inventory: source families, independent observations, distinct path geometry, physically separated shapes, and prioritized collection requests. The logarithmic collection targets are review budgets, not proof that a certain sample count guarantees unique output. Aliases or multiple representations of one crop count once. Two genuinely separate captures with the same shape still do not enlarge the distinct shape pool.

## Collect, render, inspect, refine

```sh
inkform profile diversity my-writing --document requested-document.json
inkform profile mine my-writing --characters 'nemr→' --per-label 24 --output ./build/private-review
```

Training normally mines all available reviewed character templates across the preserved pages; `--no-mine` skips that optional search. The focused `profile mine` command above is useful when a report highlights particular forms. Mining compares whole ink components against the writer's reviewed masks. It preserves page hashes, context crops, masks, candidate scores, and an importable `observations.json`; every candidate remains proposed. The score measures shape similarity, not reliable recognition. It does not find an unrepresented alphabet automatically, cut arbitrary fragments from joined words, invent missing forms, or approve graph-axis arrows as limit operators. Inspect candidate context, the complete letter including detached marks, baseline/x-height estimates, and fitted vector paths. When compatible source forms serve another role, document the specific source and visual justification explicitly.

Detached dots and multi-part signs need context-aware manual crops that retain all their components; the component miner intentionally skips forms such as `i`, `j`, and `=`. For an arrow with a separated shaft or head, inspect the source and isolate the complete sign manually before import.

Use the observation importer to generate candidates, inspect the fitted geometry, and record exact mask/geometry hashes using [observations.md](observations.md). Retain distinct ordinary and hurried forms that actually occur in the samples, then resume training with the reviewed manifest. More independent evidence should widen the supported shape range only when that evidence shows a wider range; a large collection is not a reason to make a neat writer messy.

If a compatible arrow is isolated from a long graph axis, the visible head and shaft curvature can be reviewed, but the crop length is a choice rather than an observed independent operator length. Set `variation_dimensions_observed: false` on such glyphs and document the role transfer. They remain renderable donors, while training excludes their dimensions from learned width/height variation. Do not count crop choices as the writer's natural arrow-length distribution.

The default `observed` variation mode selects actual reviewed occurrences. It excludes synthesized forms even if an older review accepted them, and does not add shape warps, interpolation, or invented curves. An existing profile can explicitly select it with `profile preferences my-writing --variation-mode observed`. Uniform size and layout placement may change, but the internal letter construction comes from the selected source. Preserve the user's preferred source shapes when choosing between modes or traces.

If source variety is insufficient, inspect additional existing pages and pending candidates. Once the usable evidence is exhausted, reuse real forms and describe which characters or words repeat. Missing a character is a coverage gap; having only one valid occurrence is a variety limitation. Neither is permission to generate another writer's letter or embellish the user's. Collection budgets and automatic candidate limits are practical stopping points, not proof that every useful instance has been found. Report any unreviewed remainder explicitly.

Render the same calibration text with a saved seed, including repeated words and several arrows/signs across adjacent pages. Run `check PDF --manifest BUILD_JSON`, inspect the actual-PDF repetition and word-pattern reports and every preview, then address the named forms using additional source evidence. The word check removes per-letter scale and shear before comparing repeated constructions; spacing jitter cannot hide a repeated word. Check baseline calibration against source context too. Observed mode prefers unused or least-recently used source forms; repetitions remain review findings when the real bank is exhausted. Do not chase a zero-repeat score by changing the letter shapes.

Finish when content, geometry, and layout checks pass and visual inspection preserves letter identity, direction, spacing, and the writer's characteristic shapes. Disclose unresolved repetition and source gaps; a faithful repeated form is acceptable when no real alternative is available. Simple dots and bars can legitimately have little variety. Keep failures and iteration reports privately so the result is supported by evidence. Physical print calibration remains a separate, actual paper check.

The immutable profile records the calibration plan and its `execution_report_path`. Read that training report for actual export success, errors, and resulting PDF paths; a planned artifact is not proof of a successful export.

Measurements report sample counts, medians, spread, and evidence limits. Keep isolated-glyph advances and body bounds distinct from word spacing observed on real lines. A photo's pixel dimensions do not reveal physical writing size. Preserve a user-selected print size until proper physical-scale evidence is available. Do not relabel estimated pen darkness as pressure.

## Natural variation and corrections

Natural character variation comes from independently written, reviewed source occurrences. Learn measured dimensions and layout statistics from compatible source evidence, keeping unrelated characters separate. Observed rendering retains the selected form's construction; it does not manufacture a wider shape distribution when evidence is sparse. Check repeated letters and arrows in calibration, not only a passage where every symbol appears once. A fixed seed keeps the comparison repeatable while the selected source occurrences can differ.

For spacing, review complete word and letter ink boxes on real lines with the corresponding page region and local x-height. Do not treat isolated glyph advances as a spacing measurement. Inspect a full sentence at normal reading size: letters should group into words, with visible word boundaries. Review graph axes, curves, ticks, and marker shapes wherever the sample pages contain them; retain exact source traces and compare their transfer against the original drawing. For crossed-out words, review isolated strike or scribble centerlines separately from the deleted writing. Also inspect a representative passage and record both its total word count and its corrected-word count. Keep the counted region and reviewer with the evidence. A collection of deliberately selected crossed-out words would overstate the habit and must not be used as the denominator.

Automatic sampled corrections require both a reviewed habit and an accepted correction mark. A writer with no observed correction habit receives none. These additions are visible crossed-out partial words or mathematical expression fragments followed by the complete intended content. The draft fragment is an estimate, recorded separately from the observed strike style. It does not change the supplied answer. Applying a passage-level correction rate to math is a rendering choice, not evidence of a measured math-error rate. Explicitly supplied deletions use the document's correction run instead. Compare the amount and appearance of cross-outs against the original passages, then inspect wrapping and the intact replacement text at print size.

When the user's preference is unknown, ask whether to reproduce the observed cuts or produce a clean page. Save the choice with `profile preferences my-writing --corrections sampled` or `--corrections none`; do not ask again when already specified. A document-level choice overrides this remembered preference. Explicit text or math deletions remain content even when automatic corrections are disabled. Correction examples cut out of equations can provide strike geometry; that does not authorize adding wrong equations or altering numerical answers.

After calibration, save the chosen render mode and user feedback using the versioned update in [profile-format.md](profile-format.md). Run `render requested-document.json --profile my-writing --output notes.pdf` using the same learned profile. The engine checks every needed role and symbol; fill coverage gaps from the existing samples or a short targeted sample before completing that document. A successful PDF export does not itself establish likeness or printed quality: inspect enlarged vector edges and print the PDF at Actual Size / 100%.

## Experimental invented variants

Use synthesis only when the user explicitly requests invented styles as an experiment. It is not a sparse-sample fallback or part of normal personal-handwriting training. The retained `profile synthesize` and `train --synthesize` commands create proposals; `profile review-variants` records exact-hash decisions after inspecting the full PDF/PNG atlas against its parents. Proposed or reviewed derived forms are still excluded from observed output. Choosing `profile preferences my-writing --variation-mode experimental` deliberately enables the experimental renderer and its stronger novelty constraints.

Experimental forms remain identified as estimates with source parent references. Review letter identity, detached dots, counters, and the source writer's range. Reject extra curves or misleading constructions even when numeric novelty checks pass. Never count generated forms as observed samples, fit learned distributions from them, or use them as recursive synthesis parents. Experimental uniqueness checks do not establish resemblance. A user preference for source fidelity returns the profile to observed mode.
