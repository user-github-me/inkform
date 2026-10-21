#!/usr/bin/env python3
"""Build Inkform's public, hand-authored demonstration path library.

The compact coordinates below were authored for this project. They are not a
font conversion, a traced handwriting sample, or a model of a real person.
Positive input y points up; the exported profile uses baseline-zero y-down.
Run from the project root: python scripts/make_synthetic_profile.py
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path


# M starts a pen stroke; L draws a line; C draws a cubic curve; Z closes it.
# Deliberately simple, open, lightly slanted forms make failures easy to see.
LOWER = {
    "a": "M .70 .91 C .49 1.12 .12 .99 .09 .51 C .04 .04 .48 -.12 .69 .23 C .79 .48 .74 .76 .70 .91 M .74 .97 L .72 .11 C .73 -.02 .87 .02 .91 .07",
    "b": "M .12 .04 C .13 .51 .26 1.17 .26 1.55 M .17 .72 C .43 1.21 .83 1.00 .81 .53 C .79 .13 .42 -.11 .15 .08",
    "c": "M .80 .88 C .55 1.16 .12 .96 .10 .48 C .07 .06 .47 -.09 .78 .15",
    "d": "M .71 .87 C .44 1.15 .11 .94 .09 .45 C .07 .07 .45 -.11 .69 .23 M .85 1.55 C .80 1.08 .67 .36 .71 .06 L .89 .03",
    "e": "M .12 .49 C .35 .40 .67 .47 .77 .70 C .87 .94 .54 1.11 .29 .91 C -.08 .60 .09 -.16 .74 .09",
    "f": "M .02 -.44 C .33 .09 .31 .86 .46 1.31 C .53 1.59 .74 1.60 .83 1.39 M .10 .87 L .70 .92",
    "g": "M .73 .89 C .45 1.11 .11 .92 .10 .44 C .08 .01 .51 -.05 .70 .23 M .77 .97 C .70 .31 .76 -.31 .44 -.49 C .25 -.60 .11 -.47 .17 -.29",
    "h": "M .13 .01 C .18 .53 .28 1.08 .29 1.55 M .18 .67 C .35 1.00 .67 1.08 .74 .76 C .78 .55 .68 .14 .79 .02",
    "i": "M .23 .92 C .24 .65 .12 .19 .20 .04 L .35 .06 M .27 1.34 L .28 1.36",
    "j": "M .36 .93 C .29 .40 .38 -.17 .15 -.44 C .04 -.55 -.11 -.48 -.14 -.35 M .40 1.32 L .41 1.35",
    "k": "M .13 .01 C .17 .56 .27 1.08 .27 1.53 M .80 1.00 L .22 .48 L .79 .01",
    "l": "M .26 1.53 C .20 1.13 .10 .36 .16 .10 C .20 -.04 .35 .01 .42 .07",
    "m": "M .12 .03 L .16 .97 M .17 .70 C .37 1.07 .57 1.08 .62 .78 L .60 .03 M .63 .70 C .89 1.10 1.13 1.08 1.13 .71 L 1.09 .04",
    "n": "M .12 .01 L .17 .97 M .18 .69 C .39 1.08 .74 1.09 .77 .70 L .72 .04",
    "o": "M .54 .99 C .17 1.12 .04 .73 .09 .37 C .13 -.01 .49 -.12 .74 .16 C .97 .47 .85 1.03 .54 .99 Z",
    "p": "M .09 -.46 L .21 .97 M .17 .72 C .41 1.11 .83 1.04 .81 .60 C .79 .17 .46 .06 .18 .28",
    "q": "M .69 .91 C .40 1.15 .08 .85 .10 .43 C .13 .02 .49 -.02 .70 .29 M .76 .96 L .66 -.47 L .88 -.28",
    "r": "M .13 .02 L .18 .98 M .18 .68 C .39 1.08 .62 1.10 .74 .82",
    "s": "M .77 .86 C .52 1.16 .09 .93 .18 .67 C .23 .46 .74 .49 .76 .23 C .78 -.03 .31 -.11 .10 .10",
    "t": "M .42 1.38 C .33 .98 .20 .25 .30 .08 C .38 -.04 .55 .01 .65 .09 M .08 .91 L .73 .96",
    "u": "M .17 .96 C .14 .59 .02 .08 .35 .02 C .58 -.03 .72 .34 .77 .93 M .77 .96 L .72 .09 L .90 .04",
    "v": "M .09 .95 C .15 .61 .24 .21 .39 .01 C .63 .35 .78 .71 .88 .99",
    "w": "M .09 .97 L .29 .03 L .65 .69 L .86 .02 C 1.01 .29 1.19 .69 1.23 .99",
    "x": "M .12 .98 C .35 .77 .64 .31 .84 .01 M .89 .98 C .59 .73 .30 .26 .08 .00",
    "y": "M .12 .97 C .15 .56 .32 .22 .44 .15 M .84 .99 C .60 .41 .38 -.22 .15 -.49",
    "z": "M .10 .96 L .83 .98 L .09 .02 L .82 .06",
}

UPPER = {
    "A": "M .04 .01 L .65 1.49 L 1.05 .02 M .25 .53 L .88 .56",
    "B": "M .13 .01 L .24 1.46 C 1.08 1.62 1.13 .80 .29 .77 C 1.25 .91 1.19 -.09 .13 .01",
    "C": "M 1.04 1.28 C .76 1.64 .16 1.52 .10 .76 C .03 .09 .59 -.17 1.00 .20",
    "D": "M .12 .01 L .25 1.47 C 1.25 1.68 1.41 .08 .12 .01",
    "E": "M 1.00 1.45 L .22 1.44 L .11 .05 L .94 .08 M .17 .76 L .79 .77",
    "F": "M .11 .00 L .22 1.45 L 1.01 1.47 M .18 .79 L .84 .80",
    "G": "M 1.05 1.28 C .77 1.64 .16 1.52 .10 .76 C .03 .09 .68 -.17 1.00 .19 L 1.03 .72 L .64 .70",
    "H": "M .12 .01 L .25 1.48 M 1.05 1.48 L .94 .01 M .20 .77 L 1.01 .79",
    "I": "M .19 1.47 L .88 1.48 M .59 1.47 L .47 .01 M .07 .01 L .79 .03",
    "J": "M .28 1.47 L .99 1.48 M .83 1.47 L .74 .34 C .68 -.13 .19 -.12 .10 .27",
    "K": "M .12 .02 L .25 1.48 M 1.07 1.46 L .20 .67 L 1.05 .01",
    "L": "M .25 1.49 L .12 .03 L .98 .06",
    "M": "M .08 .01 L .23 1.47 L .69 .60 L 1.29 1.48 L 1.23 .02",
    "N": "M .11 .01 L .23 1.48 L 1.02 .06 L 1.13 1.49",
    "O": "M .70 1.49 C .19 1.55 .04 .89 .13 .43 C .25 -.16 .97 -.09 1.13 .54 C 1.26 1.10 1.09 1.51 .70 1.49 Z",
    "P": "M .11 .01 L .24 1.47 C 1.39 1.69 1.32 .61 .20 .76",
    "Q": "M .70 1.49 C .19 1.55 .04 .89 .13 .43 C .25 -.16 .97 -.09 1.13 .54 C 1.26 1.10 1.09 1.51 .70 1.49 Z M .67 .38 L 1.15 -.15",
    "R": "M .11 .01 L .24 1.47 C 1.39 1.69 1.32 .61 .20 .76 M .60 .76 L 1.13 .01",
    "S": "M 1.01 1.28 C .65 1.65 .13 1.43 .21 1.03 C .29 .68 1.03 .84 1.05 .38 C 1.07 -.08 .43 -.16 .10 .15",
    "T": "M .09 1.47 L 1.21 1.49 M .68 1.47 L .54 .01",
    "U": "M .24 1.48 L .15 .48 C .04 -.17 .92 -.16 1.01 .47 L 1.09 1.50",
    "V": "M .09 1.47 L .50 .02 L 1.18 1.49",
    "W": "M .09 1.48 L .31 .02 L .84 1.07 L 1.05 .03 L 1.61 1.50",
    "X": "M .14 1.48 L 1.08 .00 M 1.18 1.48 L .08 .00",
    "Y": "M .09 1.48 L .59 .73 L 1.16 1.49 M .59 .73 L .52 .01",
    "Z": "M .13 1.47 L 1.19 1.49 L .08 .02 L 1.10 .06",
}

DIGITS = {
    "0": "M .56 1.36 C .12 1.42 .03 .88 .10 .39 C .19 -.16 .76 -.12 .89 .45 C 1.01 1.05 .88 1.39 .56 1.36 Z",
    "1": "M .14 1.05 L .52 1.36 L .40 .02 M .11 .00 L .74 .03",
    "2": "M .12 1.07 C .29 1.50 .89 1.45 .86 1.03 C .83 .68 .33 .41 .10 .02 L .89 .06",
    "3": "M .16 1.19 C .62 1.55 1.05 1.20 .73 .87 L .44 .69 C 1.18 .93 1.08 -.09 .44 .00 C .27 .02 .17 .08 .09 .16",
    "4": "M .68 1.38 L .08 .42 L .98 .46 M .80 1.36 L .69 .00",
    "5": "M .94 1.35 L .27 1.32 L .19 .72 C .57 .91 .97 .73 .87 .30 C .77 -.12 .27 -.10 .10 .12",
    "6": "M .87 1.33 C .32 1.65 .03 .54 .18 .18 C .36 -.21 .91 -.02 .91 .44 C .91 .83 .39 .86 .16 .42",
    "7": "M .12 1.33 L .98 1.38 L .34 .00 M .28 .68 L .79 .70",
    "8": "M .56 .72 C -.13 1.10 .34 1.63 .78 1.30 C 1.15 1.01 .61 .77 .56 .72 C -.18 .35 .17 -.13 .65 .03 C 1.19 .20 .91 .61 .56 .72 Z",
    "9": "M .88 .98 C .85 1.59 .14 1.47 .12 .94 C .10 .49 .69 .45 .88 .98 C .85 .55 .63 .20 .31 .00",
}

MARKS = {
    ".": "M .18 .03 L .20 .04", ",": "M .26 .08 C .30 -.03 .19 -.20 .11 -.25",
    ":": "M .20 .84 L .21 .85 M .16 .08 L .17 .09",
    ";": "M .22 .84 L .23 .85 M .23 .08 C .26 -.07 .18 -.20 .09 -.25",
    "!": "M .28 1.38 L .21 .40 M .18 .05 L .19 .07",
    "?": "M .12 1.13 C .28 1.55 .88 1.41 .83 1.08 C .78 .77 .37 .79 .38 .43 M .36 .07 L .37 .09",
    "'": "M .27 1.44 L .18 1.11", '"': "M .20 1.44 L .12 1.13 M .49 1.44 L .41 1.13",
    "-": "M .12 .48 L .67 .50", "−": "M .10 .49 L 1.02 .50", "–": "M .10 .49 L 1.04 .50", "—": "M .10 .49 L 1.70 .51",
    "+": "M .10 .50 L 1.02 .50 M .57 .97 L .54 .04",
    "=": "M .10 .70 L 1.03 .72 M .08 .29 L 1.02 .32",
    "<": "M .88 .98 L .12 .49 L .87 .03", ">": "M .13 .97 L .90 .49 L .12 .02",
    "/": "M .06 -.13 L .76 1.47", "\\": "M .12 1.47 L .69 -.13",
    "(": "M .55 1.49 C .06 1.12 .01 .27 .42 -.22",
    ")": "M .16 1.49 C .59 1.09 .59 .28 .06 -.22",
    "[": "M .59 1.47 L .20 1.46 L .10 -.20 L .51 -.18",
    "]": "M .13 1.47 L .52 1.46 L .42 -.20 L .01 -.18",
    "{": "M .74 1.49 C .17 1.57 .46 .76 .12 .63 C .48 .42 .08 -.25 .63 -.20",
    "}": "M .13 1.49 C .70 1.57 .41 .76 .75 .63 C .39 .42 .79 -.25 .24 -.20",
    "|": "M .26 1.46 L .14 -.20", "_": "M .06 -.17 L .98 -.15",
    "×": "M .13 .94 L .94 .06 M .97 .96 L .10 .05",
    "·": "M .29 .54 L .31 .56", "÷": "M .10 .50 L 1.02 .52 M .55 .93 L .57 .95 M .50 .06 L .52 .08",
    "*": "M .51 1.05 L .46 .13 M .11 .84 L .87 .32 M .88 .87 L .12 .28",
    "%": "M .12 .00 L 1.11 1.44 M .32 1.42 C -.05 1.43 .04 .86 .28 .91 C .62 .92 .62 1.39 .32 1.42 Z M .93 .53 C .57 .56 .61 -.03 .93 .01 C 1.23 .00 1.25 .54 .93 .53 Z",
    "&": "M 1.11 .03 C .79 .30 .18 .93 .20 1.19 C .23 1.49 .62 1.50 .69 1.21 C .78 .89 .07 .62 .11 .27 C .16 -.12 .84 -.12 1.07 .60",
    "@": "M .85 .37 C .38 .11 .35 .92 .76 .93 C 1.01 .97 1.00 .35 .89 .34 C 1.44 -.09 1.75 1.47 .80 1.48 C -.18 1.51 -.26 .01 .66 -.13 C .96 -.19 1.21 -.10 1.38 .04",
    "#": "M .41 1.42 L .23 -.08 M .91 1.44 L .73 -.06 M .09 .91 L 1.15 .94 M .03 .39 L 1.09 .42",
    "$": "M .92 1.20 C .45 1.54 .09 1.14 .24 .90 C .44 .62 1.01 .82 .94 .31 C .88 -.06 .35 -.11 .12 .17 M .65 1.59 L .48 -.20",
    "~": "M .10 .43 C .38 .94 .72 .06 1.05 .57", "^": "M .13 .82 L .52 1.38 L .87 .83", "`": "M .18 1.46 L .39 1.17",
    "∞": "M .66 .51 C .28 1.13 -.12 .64 .14 .32 C .39 -.01 .79 .91 1.08 .89 C 1.45 .83 1.43 .24 1.07 .24 C .89 .24 .77 .41 .66 .51 Z",
    "→": "M .07 .50 L 1.43 .53 M .99 .98 L 1.46 .53 L .99 .09",
    "←": "M .12 .50 L 1.48 .53 M .55 .98 L .09 .50 L .54 .08",
    "↔": "M .12 .50 L 1.48 .53 M .55 .98 L .09 .50 L .54 .08 M 1.02 .98 L 1.49 .53 L 1.02 .09",
    "⇒": "M .09 .72 L 1.13 .74 M .07 .31 L 1.12 .33 M .95 1.01 L 1.46 .53 L .95 .03",
    "↦": "M .13 .50 L 1.48 .53 M .13 .92 L .12 .09 M 1.01 .98 L 1.49 .53 L 1.01 .09",
    "√": "M .09 .53 L .29 .19 L .62 1.25 L 1.28 1.28",
}

GREEK = {
    "π": "M .06 .94 C .40 1.04 .81 .93 1.03 .98 M .31 .94 L .22 .01 M .80 .96 C .68 .45 .67 .00 .91 .08",
    "α": "M .83 .84 C .45 1.23 .01 .71 .13 .28 C .33 -.24 .77 .28 .85 .88 M .92 .98 C .76 .30 .86 .08 1.07 .04",
    "β": "M .07 -.45 L .29 1.20 C .53 1.86 1.11 1.45 .80 1.06 L .43 .79 C 1.42 1.17 1.15 -.21 .18 .10",
    "θ": "M .54 1.36 C .12 1.42 .03 .88 .10 .39 C .19 -.16 .76 -.12 .89 .45 C 1.01 1.05 .88 1.39 .54 1.36 Z M .14 .64 L .90 .70",
    "λ": "M .24 1.54 C .62 1.45 .54 .40 .94 .02 M .52 .84 L .04 .00",
    "μ": "M .08 -.43 L .24 .98 C .01 .01 .42 -.19 .79 .43 M .86 .98 C .73 .51 .65 .02 .95 .07",
    "σ": "M 1.01 .99 L .52 .96 C .12 1.01 .02 .42 .21 .13 C .50 -.21 .97 .24 .74 .76 L .52 .96",
    "φ": "M .62 1.42 L .43 -.45 M .64 .95 C .20 1.15 -.06 .52 .21 .17 C .67 -.30 1.14 .40 .86 .78 C .81 .89 .71 .92 .64 .95 Z",
    "ω": "M .21 .96 C -.15 .39 .11 -.19 .49 .14 L .64 .60 C .49 -.15 1.12 -.22 1.24 .40 C 1.30 .64 1.24 .84 1.14 .99",
}

# Two original correction gestures, independently authored for the public demo.
# These describe marks, not character shapes, and carry no personal evidence.
CORRECTIONS = {
    "single": "M .02 .45 C .78 .55 1.15 .31 1.94 .47 C 2.61 .62 3.21 .43 3.93 .58",
    "double": "M .05 .66 C 1.02 .53 1.84 .75 2.62 .52 L 3.84 .61 M 3.94 .27 C 2.99 .41 2.02 .22 1.27 .41 L .01 .33",
}


def parse_path(description: str) -> list[dict]:
    tokens = re.findall(r"[MLCZ]|[-+]?(?:\d+(?:\.\d*)?|\.\d+)", description)
    paths, current, index = [], None, 0

    def point(offset):
        return [float(tokens[offset]), -float(tokens[offset + 1])]

    while index < len(tokens):
        op = tokens[index]
        index += 1
        if op == "M":
            current = {"start": point(index), "segments": [], "closed": False,
                       "dynamics_source": "not_applicable"}
            paths.append(current)
            index += 2
        elif op == "L":
            current["segments"].append({"type": "line", "end": point(index)})
            index += 2
        elif op == "C":
            current["segments"].append({"type": "cubic", "control1": point(index),
                                         "control2": point(index + 2), "end": point(index + 4)})
            index += 6
        elif op == "Z":
            current["closed"] = True
        else:
            raise ValueError(f"Unknown coordinate command: {op}")
    return paths


def glyph(character: str, role: str, description: str) -> dict:
    paths = parse_path(description)
    points = []
    for path in paths:
        points.append(path["start"])
        for segment in path["segments"]:
            points.extend(value for key, value in segment.items() if key in {"end", "control1", "control2"})
    # Including control points deliberately overestimates the curve bounds.
    pad = .055
    bounds = [round(min(p[0] for p in points)-pad, 5), round(min(p[1] for p in points)-pad, 5),
              round(max(p[0] for p in points)+pad, 5), round(max(p[1] for p in points)+pad, 5)]
    return {"glyph_id": f"synthetic.{role}.u{ord(character):04x}", "text": character, "role": role,
            "representation": "centerline", "advance": round(bounds[2]+.20, 5), "ink_bounds": bounds,
            "strokes": paths, "source_refs": [], "label_status": "reviewed", "geometry_status": "reviewed",
            "reviewed_by": "inkform-synthetic-fixture-author", "stroke_order_status": "not_applicable"}


def build_profile() -> dict:
    marks = copy.deepcopy(MARKS)
    marks["≠"] = marks["="] + " M .30 -.06 L .91 1.08"
    marks["≤"] = marks["<"] + " M .12 -.17 L .86 -.15"
    marks["≥"] = marks[">"] + " M .12 -.17 L .86 -.15"
    marks["±"] = marks["+"] + " M .11 -.20 L 1.02 -.18"
    marks["∓"] = "M .10 1.19 L 1.02 1.20 " + marks["+"]
    marks["⇐"] = "M .42 .72 L 1.47 .74 M .42 .31 L 1.47 .33 M .61 1.01 L .09 .53 L .61 .03"
    marks["’"], marks["‘"] = marks["'"], marks["`"]
    marks["“"] = "M .31 1.46 L .11 1.19 M .60 1.46 L .40 1.19"
    marks["”"] = marks['"']
    marks["…"] = "M .16 .03 L .18 .04 M .49 .03 L .51 .04 M .82 .03 L .84 .04"
    letters = {**LOWER, **UPPER}
    glyphs = [glyph(char, "text", path) for char, path in {**letters, **DIGITS, **marks, **GREEK}.items()]
    glyphs += [glyph(char, "math.variable", path) for char, path in {**letters, **GREEK}.items()]
    glyphs += [glyph(char, "math.function", path) for char, path in letters.items()]
    glyphs += [glyph(char, "math.digit", path) for char, path in DIGITS.items()]
    glyphs += [glyph(char, "math.operator", path) for char, path in marks.items()]
    for name, path in CORRECTIONS.items():
        correction = glyph("−", "mark.correction", path)
        correction.update(glyph_id=f"synthetic.mark.correction.crossout.{name}", text="crossout")
        glyphs.append(correction)
    return {
        "schema_version": "1.0", "profile_id": "synthetic-demo", "profile_version": "1.0.0",
        "display_name": "Inkform synthetic educational strokes", "synthetic": True,
        "description": "Public demonstration geometry, authored from scratch; not learned from any person's handwriting or converted from a font.",
        "license": "MIT", "scripts": ["Latin", "Greek-math-subset"],
        "coordinate_system": {"glyph_units": "x_height", "baseline_y": 0, "y_axis": "down", "page_units": "mm"},
        "physical_scale": {"body_x_height_mm": 3.0, "source": "chosen demonstration scale; no physical sample measurement"},
        "pen": {"width_mm": .26, "color": "#182A44"},
        "metrics": {"word_gap": .72, "line_height": 2.65},
        "dynamics": {"stroke_order": "not_applicable", "timing": "not_applicable", "pressure": "not_applicable"},
        "calibration_status": "agent_reviewed", "sources": [], "glyphs": glyphs,
    }


def main():
    destination = Path(__file__).resolve().parents[1] / "examples" / "synthetic-profile.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    profile = build_profile()
    destination.write_text(json.dumps(profile, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(f"Wrote {len(profile['glyphs'])} synthetic vector glyphs to {destination}")


if __name__ == "__main__":
    main()
