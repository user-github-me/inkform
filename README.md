# Inkform

**Personal forms. Fresh ink.**

[Introduction](https://user-github-me.github.io/inkform/) · [npm](https://www.npmjs.com/package/@nazifbd/inkform) · [GitHub Packages](https://github.com/user-github-me/inkform/pkgs/npm/inkform) · [Releases](https://github.com/user-github-me/inkform/releases) · [Contribute](https://github.com/user-github-me/inkform/blob/main/CONTRIBUTING.md)

Inkform is an open-source Python engine and agent skill for learning reusable private handwriting profiles and producing printable vector PDFs. It fits the writer's own reviewed forms, preserves genuine variants, and keeps text and mathematical roles separate.

This is a working **0.3 implementation**, with a profile importer, reviewed vector geometry, semantic layout, a command-line interface, and actual PDF checks. Personal resemblance and print quality still need calibration. It is not a fully automatic photo-to-handwriting model.

Inkform includes sample-first training, portable reviewed observations, smooth cubic outline refinement, and 300 dpi previews. A writer can choose `default_render_mode: clean_outline` to retain fuller, sample-derived forms. An explicit `--mode` overrides that preference. The PDF itself remains vector at every preview resolution.

The renderer now checks the actual visible shapes of repeated letters and symbols on the same and adjacent pages. It rotates distinct reviewed forms, varies smooth geometry, and changes arrow shafts and heads separately. Exact and near copies of ordinary forms block export; the engine reports when the available shape range is insufficient. Simple dots and straight bars have a separately reported limited range. The profile learns from distinct reviewed examples where available; sparse evidence uses a conservative estimate. Reviewed correction habits can add crossed-out word restarts at a rate learned from sample passages, using the writer's own strike or scribble geometry.

## Install with npm

Node.js 20 or newer and Python 3.11 or newer are required. With `uv` installed, setup can provision a compatible Python automatically. Setup downloads dependencies once; handwriting processing runs locally without a cloud account or API key.

```sh
npm install -g @nazifbd/inkform
inkform setup
inkform --help

# Install the agent skill, including its bundled runtime.
inkform skill install
```

You can also use `npx @nazifbd/inkform setup` and `npx @nazifbd/inkform skill install`. The installer defaults to `~/.agents/skills/inkform`; use `--path` for your agent's skill directory. It preserves an existing installation unless you explicitly request replacement. See [installation details](https://github.com/user-github-me/inkform/blob/main/skills/inkform/references/installation.md).

The npm package contains the Python engine and skill. It is a launcher and installer, not a browser handwriting model. The Python environment is prepared only by the explicit `setup` command.

The repository also publishes a linked [GitHub Packages mirror](https://github.com/user-github-me/inkform/pkgs/npm/inkform), named `@user-github-me/inkform`. GitHub's npm registry requires [GitHub registry authentication](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-npm-registry#authenticating-to-github-packages), including for public packages. Once configured, install it with `npm install -g @user-github-me/inkform --registry=https://npm.pkg.github.com`. Both packages provide the same `inkform` command and handwriting engine.

## Run from source

```sh
git clone https://github.com/user-github-me/inkform.git
cd inkform
uv venv --python 3.12
uv pip install -e '.[dev]'
.venv/bin/inkform render examples/calibration.json \
  --profile examples/synthetic-profile.json --seed 72 \
  --output build/demo.pdf
.venv/bin/inkform check build/demo.pdf --manifest build/demo.build.json
.venv/bin/pytest -q
```

The bundled example profile is deliberately synthetic, made from authored coordinates. It contains no personal samples. `handwrite` is an alias for `inkform` after installation.

Every successful render writes a PDF, a PNG preview for each page, the unchanged semantic document, a plain-text sidecar, a build manifest, and a verification report. Print on A4 at **Actual Size / 100%**. Vector paths are not searchable text; use the sidecars for accessible text.

## Train, then write

```sh
inkform train --id my-writing --samples ./writing-samples
# The agent reviews sample forms and their fitted geometry, then resumes:
inkform train --id my-writing --samples ./writing-samples \
  --observations ./reviewed-observations.json \
  --calibration-document calibration.json --document document.json --mine
inkform render document.json --profile my-writing --output build/notes.pdf
```

Training learns a private library of the writer's own fitted curves and variants, together with evidence-backed measurements. Raw photos begin an intake stage; they do not become an accepted alphabet without segmentation and review. The agent handles that review through the [training workflow](https://github.com/user-github-me/inkform/blob/main/skills/inkform/references/training.md). Training reports distinguish imported samples, pending geometry review, calibration readiness, and readiness for a specific document. This is local exemplar learning, not neural network training.

Once calibrated, the profile is reused for new documents. Additional samples improve it through immutable versions. If a requested symbol is missing, the workflow uses more evidence from the supplied pages or requests a targeted sample. It does not fill the gap with generic handwriting. Resemblance, spacing, and printed appearance are evaluated separately; export validity alone is not a handwriting-match score.

Include multiple occurrences of frequent letters and symbols, ordinary word spacing, and representative passages containing any corrections. The agent reviews the forms, word boxes, and correction counts separately. This distinguishes the writer's actual range from one repeated crop or a collection containing only their messiest words. See [observation evidence](https://github.com/user-github-me/inkform/blob/main/skills/inkform/references/observations.md) for the manifest format.

`profile diversity` identifies overused forms and counts physically distinct reviewed shapes. `profile mine` searches preserved pages for more examples using the writer's own masks and writes a private queue with source context. Candidates require label and geometry review; they are not automatically accepted. The skill repeats collection, calibration, actual-PDF checking, and visual inspection until the requested document passes or an evidence gap remains. More samples help when they contain genuinely different forms.

## Personal profiles

```sh
# Inspect accepted forms and preflight actual content.
inkform profile inspect my-writing --preview build/profile.pdf
inkform profile coverage my-writing --document document.json
inkform profile diversity my-writing --document document.json
inkform profile mine my-writing --characters 'nemr→' --output ./build/private-review
inkform render document.json --profile my-writing --seed 72 --output build/personal.pdf

# Preserve additional samples, orientation, hashes, and import observations.
inkform profile update my-writing --samples ./additional-samples
```

Review ledgers bind each label and geometry decision to exact source-mask and geometry hashes. Later imports reuse matching decisions. Updates create new versions; existing snapshots are immutable. An optional `profile import-prototype` adapter supports legacy isolated-crop collections, but a prototype is not needed for new writers.

For a new writer, use `train` to coordinate intake, reviewed observations, measurements, and calibration. The lower-level `profile create --id writer --samples ./samples` imports photographs/PDFs and metadata. It does **not** automatically accept OCR labels or infer an alphabet. The [portable observation manifest](https://github.com/user-github-me/inkform/blob/main/skills/inkform/references/observations.md) accepts labeled image crops or isolated masks from any writer; the legacy prototype is optional. New observations need segmentation, label review, and geometric review before they can render. A complete manually curated vector profile can be validated and stored with `profile import-json profile.json`.

Private profiles and copied evidence default to `~/Library/Application Support/Inkform` on macOS. Use `INKFORM_DATA_DIR` or the global `--data-dir` option to choose another directory. Source files are preserved. Photographs, OCR, masks, derived paths, reviews, and rendered personal documents stay local. Public packages contain only code, documentation, and authored synthetic examples. Keep your own sample and output folders outside source control.

## Documents and mathematics

```sh
inkform document --text-file note.txt --id note --output build/note.json
inkform render build/note.json --profile my-writing --output build/note.pdf
# From a source checkout, try the bundled synthetic math profile:
inkform render examples/math.json --profile examples/synthetic-profile.json \
  --output build/math.pdf
inkform compare --reference build/baseline.pdf \
  --candidate build/personal.pdf --output build/comparison
```

Documents contain paragraphs with typed text, inline-math, or correction runs and display-math blocks. Supported mathematics includes numbers, symbols, rows, relations, function calls, fractions, square roots, superscripts, subscripts, and limits with explicit `left`, `right`, or `both` direction. Coverage depends on the selected profile; a supported math structure does not imply its symbols have been learned. Oversized indivisible words/expressions produce an actionable overflow error. Prose wraps and paginates without compressing lines.

The renderer preserves supplied content; it does not solve exercises or verify their mathematical truth. An agent performs those tasks before rendering. Missing graphs belong in `unresolved_inputs`, which is retained and reported in a partial deliverable. Literal text is preserved exactly in the semantic and text sidecars; line wrapping and whitespace are physical layout decisions. Sampled cross-outs duplicate a word before its intact version, preserving the intended wording. Explicit correction runs retain supplied deleted text in the semantic document and use the replacement in the plain-text sidecar. Set `"handwriting": {"corrections": "none"}` to disable sampled cross-outs.

See the [agent skill](https://github.com/user-github-me/inkform/blob/main/skills/inkform/SKILL.md), [profile contract](https://github.com/user-github-me/inkform/blob/main/skills/inkform/references/profile-format.md), [document contract](https://github.com/user-github-me/inkform/blob/main/skills/inkform/references/documents.md), and [rendering and validation notes](https://github.com/user-github-me/inkform/blob/main/skills/inkform/references/rendering.md).

## Current limits

- Both modes use deterministic source-family rotation and bounded shape variation. Repeated-form measurements inform width and height; estimated variation remains labeled. Compatible centerline forms can interpolate; outlines retain their reviewed contours and topology. Photo-derived paths do not recover real pen pressure, timing, or order.
- Cross-outs need a reviewed correction mark. Automatic placement additionally needs a correction rate counted from representative sample passages; importing photographs or isolated marks alone does not establish that habit. Corrections are text restarts, not generated wrong answers.
- Clean outlines preserve the observed ink shape and can be the writer's default. Their thickness is baked into their geometry. Uncalibrated size changes, scripts, and scalable outline primitives are rejected.
- Outline refinement fits continuous cubic curves with bounded movement while preserving detached marks, holes, containment, and ink area. New candidates need geometric review; old profile snapshots remain available. Changing the PNG preview DPI does not smooth the PDF's geometry.
- Unrefined centerlines can remain angular. Reviewed consecutive word boxes can inform word-gap variation; connected cursive, learned within-word kerning, automatic page/perspective measurement, universal OCR segmentation, advanced math, and tablet capture are future work.
- Actual PDF parsing checks physical page size, vector paths, image absence, safe bounds, every-page rasterization, and emitted glyph shapes. Same/adjacent-page duplicates and near copies fail for ordinary forms; simple punctuation has a separate limited-range report. A bounded candidate search can return `repetition_exhausted` on dense content with too little source variety. Similarity to the writer, transcription/answers, and printed readability still require review.
- Coverage is specific to each writer's reviewed samples. Missing forms fail coverage; Inkform never silently inserts a font.

## Contributing and license

Contributions are welcome: geometry quality, connected writing and spacing, input adapters, accessibility, documentation, and synthetic regression cases are useful starting points. Read [CONTRIBUTING.md](https://github.com/user-github-me/inkform/blob/main/CONTRIBUTING.md), open an [issue](https://github.com/user-github-me/inkform/issues), or send a pull request. Report security concerns through [SECURITY.md](https://github.com/user-github-me/inkform/blob/main/SECURITY.md).

Inkform's code, skill, documentation, and synthetic examples are released under the [MIT license](https://github.com/user-github-me/inkform/blob/main/LICENSE). Personal handwriting is not included in the project or covered by its public example data.
