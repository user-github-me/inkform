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

Review the candidate geometry against the originals. Keep correct characteristic forms and variants; leave doubtful forms proposed. Use smooth outlines when they best preserve the user's ink weight and recognizable shapes. Routine candidate review belongs to the agent; ask the user for extra evidence only when actual missing forms or ambiguous content cannot be resolved.

For an existing legacy collection, `--prototype ./output` reuses its reviewed evidence. A later run loads the current profile and preserves reviewed refinements; it must not replace them with a rougher automatic reconstruction.

```sh
inkform train --id my-writing --samples ./writing-samples \
  --calibration-document calibration.json --document requested-document.json
```

Training reports separate stages:

- `awaiting_observations`: sources are imported, but accepted letter evidence is absent.
- `needs_geometry_review`: candidates exist, with insufficient accepted forms.
- `ready_for_calibration`: reviewed forms can be inspected; readiness is not a claim of universal coverage.
- `ready_for_document`: the supplied document passes role coverage and physical layout preflight with no declared unresolved inputs. A printable partial document reports `renderable: true`, `ready: false`, and `content_complete: false`.

A requested calibration document is rendered when the selected profile can cover it. Otherwise, use the diagnostic vector atlas and a targeted calibration passage drawn from known forms. Diagnostic printed labels are not substitutes in a handwritten document. Calibration and training artifacts stay in private storage.

The immutable profile records the calibration plan and its `execution_report_path`. Read that training report for actual export success, errors, and resulting PDF paths; a planned artifact is not proof of a successful export.

Measurements report sample counts, medians, spread, and evidence limits. Keep isolated-glyph advances and body bounds distinct from word spacing observed on real lines. A photo's pixel dimensions do not reveal physical writing size. Preserve a user-selected print size until proper physical-scale evidence is available. Do not relabel estimated pen darkness as pressure.

After calibration, save the chosen render mode and user feedback using the versioned update in [profile-format.md](profile-format.md). Run `render requested-document.json --profile my-writing --output notes.pdf` using the same learned profile. The engine checks every needed role and symbol; fill coverage gaps from the existing samples or a short targeted sample before completing that document. A successful PDF export does not itself establish likeness or printed quality: inspect enlarged vector edges and print the PDF at Actual Size / 100%.
