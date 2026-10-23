# Inkform

**Personal forms. Fresh ink.**

[Introduction](https://user-github-me.github.io/inkform/) · [npm](https://www.npmjs.com/package/@nazifbd/inkform) · [GitHub Packages](https://github.com/user-github-me/inkform/pkgs/npm/inkform) · [Releases](https://github.com/user-github-me/inkform/releases) · [Contribute](https://github.com/user-github-me/inkform/blob/main/CONTRIBUTING.md)

Inkform is an open-source Python engine and agent skill for learning reusable private handwriting profiles and producing printable vector PDFs. It fits the writer's own reviewed forms, preserves genuine variants, and keeps text and mathematical roles separate.

This is a working **0.4 implementation**, with a profile importer, reviewed vector geometry, semantic layout, a command-line interface, and actual PDF checks. Personal resemblance and print quality still need calibration. It is not a fully automatic photo-to-handwriting model.

Version 0.4.3 adds anatomy-aware source selection and checks character sizes against comparable neighboring ink in text and mathematics. Prose places letter bodies with complete ink collision checks, with visible word spaces calibrated from reviewed source lines. Graphs transfer the writer's reviewed axis, curve, tick, and marker strokes while retaining data anchors. Ordinary letters still use real reviewed forms without invented curves or synthesized constructions; source gaps and reuse remain visible in the report.

Inkform includes sample-first training, portable reviewed observations, smooth cubic outline refinement, and 300 dpi previews. A writer can choose `default_render_mode: clean_outline` to retain fuller, sample-derived forms. An explicit `--mode` overrides that preference. The PDF itself remains vector at every preview resolution.

The renderer checks the actual visible shapes of repeated letters and symbols on the same and adjacent pages. It selects distinct reviewed source occurrences before reusing them, preserving their internal construction. More source examples provide more real alternatives; a small bank may repeat, and the report makes that visible. Uniform size and layout placement can change without inventing new letter shapes. Reviewed correction habits can add crossed-out partial writing attempts using a rate learned from sample passages, using the writer's own strike or scribble geometry.

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
  --profile examples/synthetic-varied-profile.json --seed 72 \
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
  --calibration-document calibration.json --document document.json
inkform render document.json --profile my-writing --output build/notes.pdf
```

Training learns a private library of the writer's own fitted curves and variants, together with evidence-backed measurements. Raw photos begin an intake stage; they do not become an accepted alphabet without segmentation and review. The agent handles that review through the [training workflow](https://github.com/user-github-me/inkform/blob/main/skills/inkform/references/training.md). Training reports distinguish imported samples, pending geometry review, calibration readiness, and readiness for a specific document. This is local exemplar learning, not neural network training.

Once calibrated, the profile is reused for new documents. Additional samples improve it through immutable versions. If a requested symbol is missing, the workflow uses more evidence from the supplied pages or requests a targeted sample. It does not fill the gap with generic handwriting. Resemblance, spacing, and printed appearance are evaluated separately; export validity alone is not a handwriting-match score.

Use all supplied pages, including preserved samples from earlier imports. The agent inventories inspected pages and pending candidates, collects usable distinct occurrences of letters, numbers, signs, arrows, and corrections, and reviews their word context. Collection continues beyond a single example per character. Word spacing and representative correction counts are reviewed separately. This distinguishes the writer's actual range from one repeated crop or a collection containing only their messiest words. See [observation evidence](https://github.com/user-github-me/inkform/blob/main/skills/inkform/references/observations.md) for the manifest format.

`profile diversity` identifies overused forms and counts physically distinct reviewed shapes. Training normally searches all preserved pages using the available reviewed character templates; `--no-mine` skips that optional scan. `profile mine` runs a focused search and writes a private queue with source context. Candidates require label and geometry review; they are not automatically accepted. The miner does not recognize an entirely new alphabet or separate every joined letter, so agent inspection remains necessary. The skill repeats collection, calibration, actual-PDF checking, and visual inspection, and reports unreviewed material and evidence gaps honestly.

The default `observed` variation mode excludes previously synthesized forms and disables internal shape deformation. If existing samples cannot provide another real occurrence, it reuses the closest available form and reports repetition. Missing characters still need source evidence. Experimental synthesis remains available only as a separately requested experiment; it is not the fallback for sparse personal samples. Repeated-word checks remain useful diagnostics, and a low repetition count is not a resemblance score.

## Personal profiles

```sh
# Inspect accepted forms and preflight actual content.
inkform profile inspect my-writing --preview build/profile.pdf
inkform profile coverage my-writing --document document.json
inkform profile diversity my-writing --document document.json
inkform profile mine my-writing --characters 'nemr→' --output ./build/private-review
inkform profile preferences my-writing --variation-mode observed
inkform profile preferences my-writing --corrections sampled
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

Documents contain paragraphs with typed text, inline-math, or correction runs, display-math blocks, and explicit graphs. Supported mathematics includes numbers, symbols, rows, relations, function calls, fractions, square roots, superscripts, subscripts, and limits with explicit `left`, `right`, or `both` direction. Graphs preserve supplied data anchors, ticks, qualitative/quantitative coordinates, and open/filled markers while using the writer's labels and arrows. Coverage depends on the selected profile; a supported math structure does not imply its symbols have been learned. Oversized indivisible words/expressions produce an actionable overflow error. Prose wraps and paginates without compressing lines.

The renderer preserves supplied content; it does not solve exercises or verify their mathematical truth. An agent performs those tasks before rendering. Missing graphs belong in `unresolved_inputs`, which is retained and reported in a partial deliverable. Literal text is preserved exactly in the semantic and text sidecars; line wrapping and whitespace are physical layout decisions. Sampled cross-outs show a partial writing attempt before the intact word or mathematical expression, preserving the intended content. The attempt is an explicit rendering estimate, not a recovered original mistake. Explicit correction runs retain supplied deleted text in the semantic document and use the replacement in the plain-text sidecar. Set `"handwriting": {"corrections": "none"}` to disable sampled cross-outs.

See the [agent skill](https://github.com/user-github-me/inkform/blob/main/skills/inkform/SKILL.md), [profile contract](https://github.com/user-github-me/inkform/blob/main/skills/inkform/references/profile-format.md), [document contract](https://github.com/user-github-me/inkform/blob/main/skills/inkform/references/documents.md), and [rendering and validation notes](https://github.com/user-github-me/inkform/blob/main/skills/inkform/references/rendering.md).

## Current limits

- Both ink representations support observed source selection, with reproducible seeds and no invented internal letter geometry by default. A finite source bank can repeat; collecting more distinct actual occurrences improves its range. Experimental deformation and reviewed synthesis require an explicit experimental variation mode. Geometric checks cannot alone establish letter identity; visual review rejects inaccurate traces. Photo-derived paths do not recover real pen pressure, timing, or order.
- Cross-outs need a reviewed correction mark. Automatic placement additionally needs a correction rate counted from representative sample passages; importing photographs or isolated marks alone does not establish that habit. Corrections support words and mathematical expressions. Automatic attempts are incomplete prefixes or expression fragments; they preserve the final answer and are marked as estimates. Explicit supplied corrections can contain the actual deleted expression. Automatic graph deletions are not implemented.
- Clean outlines preserve the observed ink shape and can be the writer's default. Their thickness is baked into their geometry. Body and script sizes need compatible reviewed outlines; expandable bars and roots use separately reviewed centerlines alongside them.
- Outline refinement fits continuous cubic curves with bounded movement while preserving detached marks, holes, containment, and ink area. New candidates need geometric review; old profile snapshots remain available. Changing the PNG preview DPI does not smooth the PDF's geometry.
- Unrefined centerlines can remain angular. Reviewed word and letter boxes inform spacing medians; pair-specific kerning, connected cursive, automatic page/perspective measurement, universal OCR segmentation, advanced math, and tablet capture are future work. Graph styles need reviewed traces from the writer; missing trace types remain explicit geometry fallbacks.
- Actual PDF parsing checks physical page size, vector paths, image absence, safe bounds, every-page rasterization, emitted glyph/word constructions, comparable character sizes, and graph anchors. Same/adjacent-page duplicates and near copies remain reported in observed mode; they do not force deformation or block otherwise valid output. Experimental mode retains strict repetition failures. Similarity to the writer, transcription/answers, and printed readability still require review.
- Coverage is specific to each writer's reviewed samples. Missing forms fail coverage; Inkform never silently inserts a font.

## Contributing and license

Contributions are welcome: geometry quality, connected writing and spacing, input adapters, accessibility, documentation, and synthetic regression cases are useful starting points. Read [CONTRIBUTING.md](https://github.com/user-github-me/inkform/blob/main/CONTRIBUTING.md), open an [issue](https://github.com/user-github-me/inkform/issues), or send a pull request. Report security concerns through [SECURITY.md](https://github.com/user-github-me/inkform/blob/main/SECURITY.md).

Inkform's code, skill, documentation, and synthetic examples are released under the [MIT license](https://github.com/user-github-me/inkform/blob/main/LICENSE). Personal handwriting is not included in the project or covered by its public example data.
