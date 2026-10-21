# Public demonstration assets

The handwriting SVGs and PDF are generated exclusively from
`examples/synthetic-profile.json`, an authored, shareable coordinate set. They
contain no personal samples, learned writer profiles, photographs, or raster ink.

Run `.venv/bin/python docs/generate-assets.py` from the checkout to regenerate
them. The demonstration uses a constant 0.38 mm pen width and seed 72. The SVG
paths and PDF come from the actual Inkform layout engine.

`mark.svg` is an original, code-authored project mark. Site assets are covered by
the repository's MIT license. `demo-provenance.json` records their source.
