#!/usr/bin/env python3
"""Launch a checkout, a self-contained npm skill, or an installed Inkform CLI."""
import os
from pathlib import Path
import shutil
import sys

project = Path(__file__).resolve().parent.parent.parent.parent
interpreter = project / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
if interpreter.exists() and (project / "src" / "inkform").is_dir():
    os.execv(str(interpreter), [str(interpreter), "-m", "inkform", *sys.argv[1:]])

bundled = Path(__file__).resolve().parents[1] / "runtime" / "bin" / "inkform.mjs"
if bundled.is_file():
    node = shutil.which("node")
    if node:
        os.execv(node, [node, str(bundled), *sys.argv[1:]])
    sys.stderr.write("The installed Inkform skill requires Node.js 20+. Install Node, then run its bundled runtime with: node runtime/bin/inkform.mjs setup\n")
    raise SystemExit(2)

installed = shutil.which("inkform")
if installed:
    os.execv(installed, [installed, *sys.argv[1:]])
sys.stderr.write("Inkform needs its engine. Run: npm install -g inkform\nThen: inkform setup\nFor a source checkout, create .venv with Python 3.11+ and run: python -m pip install -e .\nSee references/installation.md.\n")
raise SystemExit(2)
