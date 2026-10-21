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

`sampled` is the default. A profile with a reviewed nonzero correction habit and
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

Single-letter function names use math-variable forms; multi-letter names use math-function forms. Fraction/roof bars use a reviewed Unicode minus `−` centerline, expanded horizontally without increasing pen width. Roots additionally require `√`; limits require `lim`, `→`, and the direction's sign. Individual arrows can vary, while fraction and root bars still span their content. Unsupported constructs fail before export.

Read the project's `examples/math.json` for nested examples. For an absent graph add `{"source_item":"q5","reason":"required_graph_missing"}` to `unresolved_inputs`. Complete independent content and identify the deliverable as partial. Solve and check source answers outside the renderer; optional `verification` metadata records what was actually checked. Rendering success proves no mathematical answer.
