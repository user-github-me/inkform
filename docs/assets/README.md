# Public demonstration assets

The handwriting SVGs and PDF are generated exclusively from
`examples/synthetic-varied-profile.json`, an authored, shareable coordinate set with several reference constructions. They
contain no personal samples, learned writer profiles, photographs, or raster ink.

Run `.venv/bin/python docs/generate-assets.py` from the checkout to regenerate
them. The demonstration uses a constant 0.38 mm pen width and seed 72. The SVG
paths and PDF come from the actual Inkform layout engine.

The PDF follows `examples/corrections.json` in observed mode: repeated letters
select from the authored reference forms without contour warps or invented
curves. Reuse of the finite bank remains reported in `demo-provenance.json`;
the examples do not claim every shape or word is unique. An explicit crossed-out
word retains its replacement, and three limit arrows keep their mathematical
directions. Both correction
gestures are original authored paths. The synthetic profile has no automatic
correction frequency and makes no claim to learned personal habits.
Regeneration also checks the PDF and saves a preview under
`build/public-demo-preview/` for inspection.

`mark.svg` is an original, code-authored project mark. Site assets are covered by
the repository's MIT license. `demo-provenance.json` records their source.
