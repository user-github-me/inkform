"""Detect repeated word templates after removing easy geometric disguises.

The physical ink audit answers whether two printed glyphs are close. This
complementary check asks whether a repeated word still uses the same recognizable
letter constructions. Per-letter translation, width, height and slant are removed
before comparing actual ink boundaries. Spacing changes alone cannot turn a
copied sequence of letter templates into a new word form.

These conservative engineering thresholds are not a learned handwriting score.
They apply to repeated lexical words of at least three letters/digits on the
same or adjacent page; punctuation and mathematical operators are not distorted
to satisfy a word test.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
import math

import numpy as np
from scipy.spatial import cKDTree

from .repetition import ShapeDescriptor, shape_descriptor


STRUCTURAL_THRESHOLD_MM = .065
MINIMUM_CHANGED_FRACTION = .4
MAX_REPORTED_WORD_PAIRS = 500


@dataclass
class StructuralDescriptor:
    boundary: np.ndarray = field(repr=False)
    tree: object = field(repr=False)
    text: str


def structural_descriptor(item) -> StructuralDescriptor:
    """Describe visible ink with width/height/slant removed at a 3 mm scale.

    This accepts a rendered item, including one reconstructed from PDF paints,
    or an already calculated physical-ink descriptor. IDs and declared bounds
    are deliberately unused. Degenerate one-dimensional ink is left measurable
    with a lower normalization bound rather than magnifying it without limit.
    """
    desc = item if isinstance(item, ShapeDescriptor) else shape_descriptor(item)
    points = np.asarray(desc.boundary, dtype=np.float64)
    if len(points) < 3:
        raise ValueError("Structural audit needs a visible ink boundary")
    points = points-points.mean(axis=0)
    yy = float(np.mean(points[:, 1]**2))
    xy = float(np.mean(points[:, 0]*points[:, 1]))
    shear = xy/max(yy, .0025)
    points[:, 0] -= shear*points[:, 1]
    # A .75 mm standard deviation gives ordinary letters a roughly 3 mm
    # normalized extent. Independently scaling the axes removes font-like
    # stretching as evidence of a new construction.
    scales = np.maximum(np.std(points, axis=0), .05)
    points = points*(.75/scales)
    if len(points) > 1800:
        # The input boundary is raster ordered. Even sampling is stable under
        # reordering of PDF paths and bounded independently of path complexity.
        points = points[np.linspace(0, len(points)-1, 1800, dtype=int)]
    return StructuralDescriptor(points, cKDTree(points), desc.text)


def _aligned_distance(first, second):
    source = first.boundary
    target = second.boundary
    moved = source
    # A short affine ICP fit removes residual sampling-induced slant/scale.
    # Fitting the whole letter cannot independently move a bowl, terminal or
    # crossbar, so local construction differences survive this alignment.
    for _ in range(5):
        _, nearest = second.tree.query(moved, workers=1)
        design = np.column_stack((source, np.ones(len(source))))
        transform, _, _, _ = np.linalg.lstsq(design, target[nearest], rcond=None)
        linear = transform[:2]
        singular = np.linalg.svd(linear, compute_uv=False)
        if np.linalg.det(linear) <= 0 or singular.min() < .65 or singular.max() > 1.5:
            break
        candidate = design@transform
        if np.max(np.abs(candidate-moved)) < 1e-5:
            moved = candidate
            break
        moved = candidate
    forward = float(np.mean(second.tree.query(moved, workers=1)[0]))
    backward = float(np.mean(cKDTree(moved).query(target, workers=1)[0]))
    return (forward+backward)/2


def structural_distance(first, second) -> float:
    """Symmetric residual ink distance after affine normalization, in mm."""
    first = first if isinstance(first, StructuralDescriptor) else structural_descriptor(first)
    second = second if isinstance(second, StructuralDescriptor) else structural_descriptor(second)
    return (_aligned_distance(first, second)+_aligned_distance(second, first))/2


def _word_groups(instances):
    """Use lexical boundaries, but reconstruct spelling from actual labels.

    Word IDs associate letters only; comparing their values never establishes
    novelty. Punctuation ends a lexical group, so ``exist`` and ``exist.`` are
    compared even when their surrounding runs differ.
    """
    groups, ungrouped = defaultdict(list), []
    for index, item in enumerate(instances):
        if item.get("role") != "text":
            continue
        if not isinstance(item.get("word_id"), str) or not isinstance(item.get("word_position"), int):
            if any(char.isalnum() for char in item.get("text", "")):
                ungrouped.append(item.get("token_id", index))
            continue
        page = item.get("page")
        if not isinstance(page, int) or isinstance(page, bool) or page < 0:
            ungrouped.append(item.get("token_id", index))
            continue
        groups[(page, item["word_id"])].append(item)
    words = []
    for (page, identity), items in groups.items():
        items.sort(key=lambda item: item["word_position"])
        if [item["word_position"] for item in items] != list(range(len(items))):
            # Splitting one word into arbitrary differently named groups must
            # not turn a partial lexical audit into a successful one.
            ungrouped.extend(item.get("token_id") for item in items)
            continue
        current = []
        for item in items+[None]:
            if item is not None and item.get("text") and all(char.isalnum() for char in item["text"]):
                current.append(item)
                continue
            if len(current) >= 3:
                words.append({"page": page, "word_id": identity, "text": "".join(i["text"] for i in current),
                              "items": current})
            current = []
    return sorted(words, key=lambda word: word["page"]), ungrouped


def word_descriptor(items):
    descriptors = [structural_descriptor(item) for item in items]
    # Spacing is reported independently. It never rescues a copied allograph
    # sequence, nor is a new source ID itself counted as a shape difference.
    centers = []
    for item in items:
        from .profiles import geometry_bounds
        paths = item.get("strokes")
        if paths is None:
            paths = [path for paint in item["_ink_paints"] for path in paint["strokes"]]
        left, top, right, bottom = geometry_bounds(paths)
        centers.append(((left+right)/2, (top+bottom)/2))
    centers = np.asarray(centers)
    gap = np.diff(centers[:, 0])
    span = max(float(np.mean(np.abs(gap))), .1)
    rhythm = gap/span
    # Remove whole-word drift; retain the alternating rises/falls of letters.
    positions = np.arange(len(centers), dtype=float)
    design = np.column_stack((positions, np.ones(len(positions))))
    trend = design@np.linalg.lstsq(design, centers[:, 1], rcond=None)[0]
    baseline = (centers[:, 1]-trend)/span
    return {"letters": descriptors, "rhythm": rhythm, "baseline": baseline}


def compare_words(first, second, *, threshold_mm=STRUCTURAL_THRESHOLD_MM):
    distances = [structural_distance(a, b) for a, b in zip(first["letters"], second["letters"])]
    if len(first["letters"]) != len(second["letters"]) or len(distances) < 3:
        raise ValueError("Word comparison needs matching lexical words of at least three letters")
    changed = sum(distance >= threshold_mm for distance in distances)
    required = max(1, math.ceil(len(distances)*MINIMUM_CHANGED_FRACTION))
    return {"category": "repeated_template" if changed < required else "distinct",
            "letter_distances_mm": [round(distance, 7) for distance in distances],
            "changed_letter_count": changed, "required_changed_letter_count": required,
            "threshold_mm": threshold_mm,
            "spacing_rhythm_distance": round(float(np.mean(np.abs(first["rhythm"]-second["rhythm"]))), 7),
            "baseline_rhythm_distance": round(float(np.mean(np.abs(first["baseline"]-second["baseline"]))), 7)}


def audit_word_patterns(instances, *, amount=None, adjacent_pages=1):
    """Recalculate repeated lexical word constructions from visible paths."""
    if not isinstance(adjacent_pages, int) or isinstance(adjacent_pages, bool) or not 0 <= adjacent_pages <= 2:
        raise ValueError("Word history must cover zero, one or two adjacent pages")
    words, ungrouped = _word_groups(instances)
    report = {"version": "inkform-word-patterns-1", "status": "pass", "adjacent_pages": adjacent_pages,
              "metric": "affine_normalized_letter_ink_boundary_distance_mm",
              "threshold_mm": STRUCTURAL_THRESHOLD_MM, "minimum_changed_fraction": MINIMUM_CHANGED_FRACTION,
              "scope": "repeated prose words of at least three letters/digits on the same and adjacent pages; mathematical functions retain the physical glyph audit",
              "checked_word_count": len(words), "comparison_count": 0, "repeated_word_count": 0,
              "ungrouped_tokens": ungrouped, "pairs": [], "truncated": False}
    counts = defaultdict(int)
    for word in words:
        counts[word["text"]] += 1
    history = defaultdict(list)
    for word in words:
        if counts[word["text"]] < 2:
            continue
        descriptor = word_descriptor(word["items"])
        previous = [entry for entry in history[word["text"]] if word["page"]-entry[0]["page"] <= adjacent_pages]
        for older, old_descriptor in previous:
            report["comparison_count"] += 1
            comparison = compare_words(old_descriptor, descriptor)
            if comparison["category"] == "repeated_template":
                report["repeated_word_count"] += 1
                if len(report["pairs"]) < MAX_REPORTED_WORD_PAIRS:
                    report["pairs"].append({"text": word["text"], "first_word_id": older["word_id"],
                                            "second_word_id": word["word_id"], "first_page": older["page"],
                                            "second_page": word["page"],
                                            "first_token_ids": [i["token_id"] for i in older["items"]],
                                            "second_token_ids": [i["token_id"] for i in word["items"]], **comparison})
        history[word["text"]] = previous+[(word, descriptor)]
    report["truncated"] = report["repeated_word_count"] > len(report["pairs"])
    if isinstance(amount, (int, float)) and not isinstance(amount, bool) and amount == 0:
        report["status"] = "opted_out"
    elif report["repeated_word_count"]:
        report["status"] = "fail"
    elif ungrouped:
        report["status"] = "partial" if words else "not_checked"
    return report
