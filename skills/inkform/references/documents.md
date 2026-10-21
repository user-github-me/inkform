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

Single-letter function names use math-variable forms; multi-letter names use math-function forms. Fraction/roof bars use a reviewed Unicode minus `−` centerline, expanded horizontally without increasing pen width. Roots additionally require `√`; limits require `lim`, `→`, and the direction's sign. Unsupported constructs fail before export.

Read the project's `examples/math.json` for nested examples. For an absent graph add `{"source_item":"q5","reason":"required_graph_missing"}` to `unresolved_inputs`. Complete independent content and identify the deliverable as partial. Solve and check source answers outside the renderer; optional `verification` metadata records what was actually checked. Rendering success proves no mathematical answer.
