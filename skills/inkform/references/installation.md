# Install Inkform

The npm package bundles the Python engine, public synthetic examples, and this skill. It requires Node.js 20+. The engine uses Python 3.11 or newer. Installation does not execute setup scripts or upload handwriting.

```sh
npm install -g inkform
inkform setup
inkform skill install
```

`setup` explicitly installs Python dependencies in a private user cache. If `uv` is available, it selects Python 3.12 and may download it. Otherwise, it uses an installed Python 3.11+ with `venv` and `pip`. The first setup needs network access to fetch dependencies. To select another installed interpreter, use `inkform setup --python /path/to/python`. `INKFORM_CACHE_DIR` changes the runtime cache location; `INKFORM_DATA_DIR` independently changes private handwriting storage.

`skill install` copies a self-contained skill to `~/.agents/skills/inkform`. Use `--path /desired/skill/inkform` for another agent's skill directory. This path is the final skill directory. Existing directories are preserved; `--force` replaces only a previous installation bearing Inkform's installation marker. Reload the agent after installation. The skill launcher preserves the caller's working directory, so relative document and output paths work normally.

You can also use `npx --yes inkform setup` and `npx --yes inkform skill install`. The copied skill contains its own runtime, so it does not depend on npm's temporary download directory remaining available. To set it up later, run `node runtime/bin/inkform.mjs setup` from the installed skill directory.

If this skill was copied alone by a skill manager, install the engine with `npm install -g inkform` followed by `inkform setup`. Copying only `SKILL.md` does not install the engine. macOS and Linux are the primary supported environments; Windows has not been validated.

## Source checkout

From a cloned repository:

```sh
uv venv --python 3.12
uv pip install -e '.[dev]'
.venv/bin/inkform --help
```

Without uv, create `.venv` with `python3 -m venv .venv`, then run `.venv/bin/python -m pip install -e '.[dev]'`. The skill launcher uses this checkout's `.venv` when present. To use the skill in an agent, copy it with `node bin/inkform.mjs skill install` and set up its bundled runtime with `node bin/inkform.mjs setup`.

## Public engine demonstration

After npm setup, `inkform examples` prints the installed public example directory. Pass its `synthetic-profile.json` as `--profile` and its `math.json` as the document:

```sh
EXAMPLES="$(inkform examples)"
inkform render "$EXAMPLES/math.json" --profile "$EXAMPLES/synthetic-profile.json" --output demo.pdf
inkform check demo.pdf
```

The synthetic profile demonstrates the renderer; it does not imitate a real writer. Personal writing begins with `inkform train --id WRITER --samples FOLDER` and the observation review procedure in [training.md](training.md).
