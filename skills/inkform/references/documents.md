# Semantic document contract

Use `inkform document --text-file note.txt --output note.json` for literal text. It preserves UTF-8 content in one typed paragraph. The engine consumes JSON, not arbitrary prompts, Markdown, or TeX. Grapheme clusters are matched without silently normalizing the source spelling.

```json
{
  "schema_version": "1.0",
  "document_id": "note",
  "content_mode": "literal",
  "page": {"width_mm": 210, "height_mm": 297, "margins_mm": 20},
  "blocks": [{"id": "body", "type": "paragraph", "children": [
    {"id": "text-1", "type": "text", "text": "The answer is "},
    {"id": "math-1", "type": "inline_math", "expression": {"type": "number", "value": "2"}},
    {"id": "text-2", "type": "text", "text": "."}
  ]}],
  "unresolved_inputs": []
}
```

Keep block/run IDs unique and stable across edits. A display equation has `type: display_math`, an `id`, and `expression`. Page margins can instead be `{top, right, bottom, left}` in millimeters. Optional `body_x_height_mm` chooses one size for the whole document.

## Crossed-out text

An explicit paragraph run can preserve a supplied deletion and its replacement:

```json
{"id": "revision-1", "type": "correction", "deleted": "Tuesday", "text": "Thursday"}
```

The PDF draws `deleted` with the profile's reviewed strike or scribble and then
writes `text` intact. Both strings remain in the semantic document; the
plain-text sidecar contains the replacement. Include a deletion only when it
is part of the supplied content or an authorized edit, not as an invented wrong
answer. The selected profile needs the relevant letterforms and a reviewed
`mark.correction` / `crossout` centerline.

Correction runs are indivisible single-line phrases. Split multiline edits
into separate runs; a phrase too wide for the printable area produces an
overflow error. An empty replacement `text` preserves a deletion without
adding new words.

Optional top-level `handwriting` controls automatic sampled corrections:

```json
"handwriting": {"corrections": "sampled"}
```

The document setting overrides the profile's saved correction preference;
without either, `sampled` is the default. Save a writer's choice with
`profile preferences WRITER --corrections sampled` or `--corrections none`.
A profile with a reviewed nonzero correction habit and
accepted correction marks can add crossed-out restarts of ordinary text words,
then write those same words intact. These physical restarts leave the input
document and intended plain text unchanged. The rate comes from representative
sample passages; absent habit evidence produces none. Use `"corrections":
"none"` to suppress sampled restarts. This setting does not erase explicit
correction runs supplied as content.

Sampling applies to ordinary text words, not math nodes. It is deterministic
for a saved seed, not a promise of an exact number of marks on each short page.
A sampled restart is skipped when its doubled word would exceed the printable
width.

## Mathematics

Supported expression fields:

| Type | Fields |
|---|---|
| `number` | decimal string `value`, optional leading sign |
| `symbol` | `value`, optional explicit math `role` (default `math.variable`) |
| `row` | nonempty `items` expressions |
| `relation` | `left`, `operator`, `right` |
| `function_call` | `name`, nonempty `arguments` |
| `fraction` | `numerator`, `denominator` |
| `root` | `body` (square roots only) |
| `superscript`, `subscript` | `base`, `script` |
| `limit` | `variable`, `approaches`, `direction` (`left`, `right`, `both`), `body` |

Single-letter function names use math-variable forms; multi-letter names use math-function forms. Fraction/roof bars use a reviewed Unicode minus `−` centerline, expanded horizontally without increasing pen width. Roots additionally require `√`; limits require `lim`, `→`, and the direction's sign. Individual arrows select from real reviewed source occurrences, while fraction and root bars still span their content. Unsupported constructs fail before export.

Read the project's `examples/math.json` for nested examples. For an absent graph add `{"source_item":"q5","reason":"required_graph_missing"}` to `unresolved_inputs`. Complete independent content and identify the deliverable as partial. Solve and check source answers outside the renderer; optional `verification` metadata records what was actually checked. Rendering success proves no mathematical answer.

## Graphs

Graphs are explicit semantic blocks, with coordinates supplied by the user or
transcribed from a visible source. This example is authored demonstration data:

```json
{"id":"graph-1","type":"graph","graph":{
  "coordinate_kind":"quantitative","width_mm":120,"height_mm":85,
  "x_range":[-1,5],"y_range":[-1,5],
  "axes":[
    {"id":"x-axis","from":[-1,0],"to":[5,0],"arrow":true,
     "ticks":[{"id":"one","at":[1,0],"label":"1","offset_mm":[0,4]}]},
    {"id":"y-axis","from":[0,-1],"to":[0,5],"arrow":true}
  ],
  "curves":[{"id":"curve","points":[[1,1],[2,2],[4,3]],"interpolation":"smooth"}],
  "markers":[{"id":"start","at":[1,1],"style":"open","radius_mm":0.8}],
  "labels":[{"id":"x-label","text":"x","at":[5,0],"offset_mm":[1,5]}]
}}
```

Coordinates use x-right/y-up; label offsets use millimeters with positive y
downward. `padding_mm` defaults to 12 and is included in the graph's width and
height. Graph element IDs are unique within each block. Axes and tick anchors
must agree. Curves accept `linear` or monotone `smooth` interpolation; smooth
curves require increasing x. Open and filled markers remain distinct.

Use `coordinate_kind: qualitative` when the source establishes only a shape,
not numerical values. Keep those placement coordinates out of asserted answers.
Labels use the writer's text forms; axes use their reviewed right-arrow forms
rotated to the declared direction. Their heads retain normal writing scale.
The profile's source-reviewed `graph_style` supplies static pen width. In
observed mode, axes and curves follow the explicit graph specification without
added bow, wobble, or marker deformation. A traced curve needs enough supplied
points to preserve its visible shape. Experimental mode can use reviewed axis
bow extents and estimated curve deviations; those are not source-recovered pen
motion. The actual PDF check verifies anchors, marker fill, and every visible
graph path. Repeated geometric data are legitimate; graph labels and arrows
still undergo handwriting audits. Report source limitations instead of adding
generic irregularity to make the graph look handwritten.

Do not infer an equation, redraw a missing problem graph, change an open point
to a filled point, or shift data to make the picture appear more handwritten.
