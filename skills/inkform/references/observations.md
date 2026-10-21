# Learning from reviewed glyph observations

Use this format to learn from any writer's supplied images. The legacy
prototype is optional. An agent or person inspects the writing and supplies
labels and crop measurements; Inkform does not automatically accept OCR labels.
Here, training means building a private, reviewed geometry profile. It does not
mean a neural model was fitted or that the writer's pen movements were captured.

## Coordinate and mask conventions

`source_path` points to an image, including HEIC where supported. Relative paths
resolve beside the manifest. Image EXIF orientation is applied **before** crop
coordinates. Keep the original image unchanged.

`bbox_xywh` is `[left, top, width, height]` in that oriented image's pixels.
`baseline_px` is the local baseline measured **down from the crop's top**;
`xheight_px` is the lowercase body height in pixels. These values do not measure
physical millimeters. Geometry uses baseline zero, positive y downward, and one
x-height per unit.

For a dark-ink crop on light paper, provide `bbox_xywh`; the importer removes
smooth illumination and preserves every disconnected component. Inspect the
result for neighboring strokes, dust, clipped dots, or shadows.

For an already isolated glyph, provide `mask_path`: a PNG in L mode or 1-bit
mode, **white ink on black background**. `source_path` is still required for
provenance. A supplied bounding box must have the mask's dimensions. If the
original crop location is unavailable, omit the box and retain that limitation
in the review; baseline and x-height then refer to the mask.

## Manifest

The importer is `inkform.observations.import_observations(manifest_path,
base_profile=None, profile_id="personal-default")`. It returns a validated
profile mapping. With a base profile, it merges into a new version while
preserving unrelated records and calibration. Training normally calls it and
handles version persistence.

Each letter or symbol entry is one Unicode character with one explicit role:
`text`, `math.variable`, `math.digit`, `math.operator`, or `math.function`.
Correction marks instead use `role: "mark.correction"` and `text: "crossout"`.
Multiple entries can provide variants. IDs must be unique within the manifest.
There is no automatic text-to-mathematics role substitution.

This synthetic example assumes an inspected letter inside a 300 × 200 image:

```json
{
  "schema_version": "1.0",
  "observations": [
    {
      "id": "lowercase-o-1",
      "text": "o",
      "role": "text",
      "source_path": "samples/page.png",
      "bbox_xywh": [40, 20, 140, 150],
      "baseline_px": 130,
      "xheight_px": 105,
      "label_status": "reviewed",
      "reviewed_by": "Agent visual review of synthetic example"
    }
  ]
}
```

`label_status` is `proposed` by default, or `reviewed` / `rejected`. A reviewed
label requires a nonempty `reviewed_by`. This approves the character label
only; newly reconstructed geometry still requires a separate review.

## Repeated forms, word spacing, and correction habits

Use distinct independently written occurrences to capture a form's range.
The profile learns width and height spread when at least two reviewed source
occurrences of the same role and character are available. Aliases and two
representations of one crop remain one observation; they do not add evidence.

Optional top-level `style_observations` hold reviewed layout or frequency
evidence. These rows refer to original image bytes, including an exported page
image when the original intake was a PDF. Portable `source_path` does not itself
decode a PDF. Coordinates use the oriented image convention above, and
`page_index` and `word_index` are zero-based.

Every explicitly reviewed style row requires `source_path`, its exact
`source_sha256`, and a named `reviewed_by`. Compute the hash from the unmodified
image file, for example with
`hashlib.sha256(Path("samples/page.png").read_bytes()).hexdigest()` in Python.
Changing the source requires renewed review.

For word spacing, record each consecutive word box with these fields:

```json
{
  "id": "page-0-line-1-word-0",
  "kind": "word",
  "source_path": "samples/page.png",
  "source_sha256": "REPLACE_WITH_ACTUAL_IMAGE_SHA256",
  "page_index": 0,
  "line_id": "line-1",
  "word_index": 0,
  "bbox_xywh": [40, 20, 140, 45],
  "xheight_px": 30,
  "label_status": "reviewed",
  "layout_status": "reviewed",
  "reviewed_by": "Agent review against original page"
}
```

Add the following word with its own ID, `word_index: 1`, and inspected box.
The gap comes from consecutive words on the same line, normalized by x-height;
isolated glyph advances cannot establish it. Learning the word-gap median and
spread needs at least three trusted adjacent gaps across at least two lines.

For corrections, count **all** words and corrected words in a representative
page or region, including words without a cross-out. Add a frequency row:

```json
{
  "id": "page-0-correction-count",
  "kind": "correction_frequency",
  "source_path": "samples/page.png",
  "source_sha256": "REPLACE_WITH_ACTUAL_IMAGE_SHA256",
  "page_index": 0,
  "scope": "page",
  "representative": true,
  "word_count": 100,
  "correction_count": 4,
  "review_status": "reviewed",
  "reviewed_by": "Agent count of the full original page"
}
```

The numbers illustrate the format; replace them with inspected counts. A
`scope: "region"` row also requires `bbox_xywh` locating that region. Selected
correction crops are not representative frequency evidence. Count each
corrected word once. Keep regions separate; duplicate or overlapping regions
cannot establish additional independent frequency evidence.

Import isolated strikes or scribbles through `observations` with
`role: "mark.correction"`, `text: "crossout"`, and the normal source, crop or
mask, baseline, x-height, and review fields. Separate the mark from the letters
underneath rather than tracing the whole deleted word as the mark. The importer
produces centerline candidates for correction marks. Review the exact source
and geometry hashes just as for letters. Frequency review and mark review are
independent: both are needed for automatic sampled corrections.

Training fits these measurements automatically after evidence review. With no
representative correction count it does not infer a correction habit. Further
sample evidence updates the private profile through a new version.

## Import, inspect, then approve exact geometry

From an installed checkout, run the training command documented in
[training.md](training.md) with the sample folder and `--observations`. For a
direct importer invocation, this executable Python snippet prints the private
review directory without saving an approved profile:

```python
from pathlib import Path
from inkform.observations import import_observations

profile = import_observations(Path("observations.json"), profile_id="my-writing")
print(profile["import_report"]["review_directory"])
```

The private directory contains `candidates.json`, `review-01.png` and any
additional numbered sheets, and `review.json`. Each sheet pairs observed ink
with the new geometry at a shared scale and baseline. Each observation has
separate `clean_outline` and `centerline` candidates. Clean outlines use smooth
cubic curves with guarded contour, hole, and ink-area preservation. If that
refinement fails, `refinement_warning` identifies the unsmoothed proposal; it
still requires review.

Read each proposed label in its original page context. Inspect loops, detached
dots, endings, character identity, baseline, and pen weight on the sheets.
Approve only the candidate IDs actually inspected. The following snippet
records one explicitly selected candidate after that review; replace the two
strings with the printed private directory and the inspected `candidate_id`:

```python
import json
from pathlib import Path

review_dir = Path("/absolute/private/review/directory")
approved_ids = {"the-inspected-candidate-id"}
records = json.loads((review_dir / "candidates.json").read_text())["candidates"]
ledger_path = review_dir / "review.json"
ledger = json.loads(ledger_path.read_text())
for candidate in records:
    if candidate["candidate_id"] in approved_ids:
        ledger["reviews"][candidate["candidate_id"]] = {
            "mask_sha256": candidate["mask_sha256"],
            "geometry_sha256": candidate["geometry_sha256"],
            "label_status": "reviewed",
            "geometry_status": "reviewed",
            "reviewed_by": "Agent visual review; user print calibration pending"
        }
ledger_path.write_text(json.dumps(ledger, indent=2) + "\n")
```

Run training again with the same manifest. The importer only accepts decisions
whose evidence and geometry hashes both match. Changed source pixels or changed
geometry invalidate old approvals. Unlisted candidates remain proposed and
cannot provide rendering coverage. The source files, masks, manifest, and
review ledger stay in private storage; none belongs in a public skill package.

Before rendering requested content, check exact role-aware coverage. A reviewed
`o` does not establish a full alphabet. Missing forms need targeted observations.
Print calibration remains separate from label and geometry review: use an A4
proof at actual size to choose physical writing size and ink thickness.
