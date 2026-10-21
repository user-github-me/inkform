# Training from user samples

The intended order is **samples → learned writer profile → calibration → requested PDF**. The local training model is a set of fitted vector forms, observed variants, and measured style statistics, retained privately and improved with more examples. This implementation does not train a neural network or recover motion/pressure from photographs.

```sh
inkform train --id my-writing --samples ./writing-samples
```

This preserves photographs/PDF pages, hashes, orientation, and available source observations. A new folder of unlabelled photographs returns `awaiting_observations`; importing files is not sufficient evidence to say the handwriting is trained.

Inspect existing samples and create a portable observation manifest following [observations.md](observations.md), including its coordinate conventions and exact review procedure. The agent should label and segment useful letter/symbol forms, retain detached dots, and identify ambiguous joins. Keep coordinate transforms, source hashes, baseline estimates, and x-height evidence. Reviewed labels do not automatically approve newly fitted geometry. The importer generates outlines/centerlines for comparison, with hashes that bind later geometry decisions to the specific source and fitted paths.

```sh
inkform train --id my-writing --samples ./writing-samples \
  --observations ./reviewed-observations.json
```

Review the candidate geometry against the originals. Keep correct characteristic forms and variants; leave doubtful forms proposed. Retain several independently written instances of common letters and recurring symbols such as limit arrows. Include the normal range of widths, slants, endings, and tidy or hurried forms visible in the supplied pages. Duplicating one crop under several IDs is not evidence of variation. Use smooth outlines when they best preserve the user's ink weight and recognizable shapes. Routine candidate review belongs to the agent; ask the user for extra evidence only when actual missing forms or ambiguous content cannot be resolved.

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

Mining compares whole ink components against the writer's reviewed masks. It preserves page hashes, context crops, masks, candidate scores, and an importable `observations.json`; every candidate remains proposed. The score measures shape similarity, not reliable recognition. It does not cut arbitrary fragments from joined words, invent missing forms, or approve graph-axis arrows as limit operators. Inspect candidate context, the complete letter including detached marks, baseline/x-height estimates, and fitted vector paths. When compatible source forms serve another role, document the specific source and visual justification explicitly.

Detached dots and multi-part signs need context-aware manual crops that retain all their components; the component miner intentionally skips forms such as `i`, `j`, and `=`. For an arrow with a separated shaft or head, inspect the source and isolate the complete sign manually before import.

Use the observation importer to generate candidates, inspect the fitted geometry, and record exact mask/geometry hashes using [observations.md](observations.md). Retain distinct ordinary and hurried forms that actually occur in the samples, then resume training with the reviewed manifest. More independent evidence should widen the supported shape range only when that evidence shows a wider range; a large collection is not a reason to make a neat writer messy.

If a compatible arrow is isolated from a long graph axis, the visible head and shaft curvature can be reviewed, but the crop length is a choice rather than an observed independent operator length. Set `variation_dimensions_observed: false` on such glyphs and document the role transfer. They remain renderable donors, while training excludes their dimensions from learned width/height variation. Do not count crop choices as the writer's natural arrow-length distribution.

Render the same calibration text with a saved seed, including dense repeated letters and several arrows/signs across adjacent pages. Run `check PDF --manifest BUILD_JSON`, inspect the actual-PDF repetition report and every preview, then address the named forms and render again. The renderer searches a bounded pool automatically and checks the final page placements. A `repetition_exhausted` result means the safe range was insufficient: mine additional source examples or identify the remaining evidence gap. Never fix this by reducing the threshold or setting `variation.amount` to zero. That opt-out exists only for an explicitly requested faithful diagnostic reproduction.

Finish only when the ordinary forms pass the actual emitted-path audit and visual inspection confirms that identity, direction, spacing, and the writer's characteristic shapes remain intact. Simple dots and bars can legitimately have a limited range; inspect their separate report and describe that limitation honestly. Keep failures and iteration reports privately so the final result is supported by evidence. Physical print calibration remains a separate, actual paper check.

The immutable profile records the calibration plan and its `execution_report_path`. Read that training report for actual export success, errors, and resulting PDF paths; a planned artifact is not proof of a successful export.

Measurements report sample counts, medians, spread, and evidence limits. Keep isolated-glyph advances and body bounds distinct from word spacing observed on real lines. A photo's pixel dimensions do not reveal physical writing size. Preserve a user-selected print size until proper physical-scale evidence is available. Do not relabel estimated pen darkness as pressure.

## Natural variation and corrections

Variation has two sources: choosing genuine reviewed forms, and applying bounded per-occurrence changes around them. Fit the latter from distinct same-character evidence, without pooling unrelated characters into a claimed personal distribution. When evidence is sparse, the renderer uses a conservative estimate and reports that limitation. Check repeated letters and arrows in calibration, not only a passage where every symbol appears once. A fixed seed keeps the comparison repeatable while occurrences within the page can differ.

For spacing, review real word gaps with the corresponding page region and x-height. For crossed-out words, review isolated strike or scribble centerlines separately from the deleted writing. Also inspect a representative passage and record both its total word count and its corrected-word count. Keep the counted region and reviewer with the evidence. A collection of deliberately selected crossed-out words would overstate the habit and must not be used as the denominator.

Automatic sampled corrections require both a reviewed habit and an accepted correction mark. A writer with no observed correction habit receives none. These additions are visible crossed-out restarts of an existing word followed by its intact text; they do not invent incorrect facts or change the supplied answer. Explicitly supplied deletions use the document's correction run instead. Compare the amount and appearance of cross-outs against the original passages, then inspect wrapping and the intact replacement text at print size.

After calibration, save the chosen render mode and user feedback using the versioned update in [profile-format.md](profile-format.md). Run `render requested-document.json --profile my-writing --output notes.pdf` using the same learned profile. The engine checks every needed role and symbol; fill coverage gaps from the existing samples or a short targeted sample before completing that document. A successful PDF export does not itself establish likeness or printed quality: inspect enlarged vector edges and print the PDF at Actual Size / 100%.
