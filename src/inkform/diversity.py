"""Inventory independent source forms and prioritize further sample review.

Collection targets are review hints, not a promise of unlimited unique writing.
Normal rendering uses real reviewed occurrences and reports remaining reuse.
"""
from __future__ import annotations

import copy
import math
from collections import Counter, defaultdict

from .document import visible_requirements
from .profiles import accepted_glyphs, resolve_render_mode, validate_profile
from .repetition import NEAR_THRESHOLD_MM, item_distance, shape_descriptor
from .variation import source_evidence


def _descriptor(glyph, profile):
    size = 3.0
    strokes = copy.deepcopy(glyph['strokes'])
    for stroke in strokes:
        stroke['start'] = [p * size for p in stroke['start']]
        for segment in stroke['segments']:
            for key in ('end', 'control1', 'control2'):
                if key in segment:
                    segment[key] = [p * size for p in segment[key]]
    return shape_descriptor({'strokes': strokes, 'representation': glyph['representation'],
                             'role': glyph['role'], 'text': glyph['text'],
                             'pen_width_mm': profile['pen']['width_mm'], 'nominal_size_mm': size})


def profile_diversity(profile: dict, document: dict | None = None, mode: str | None = None) -> dict:
    validate_profile(profile)
    mode = resolve_render_mode(profile, mode)
    representation = 'centerline' if mode == 'fresh_strokes' else 'clean_outline'
    variation_mode = profile.get('variation', {}).get('mode', 'observed')
    observations = {o['observation_id']: o for o in profile.get('observations', [])}
    groups = defaultdict(list)
    for glyph in accepted_glyphs(profile):
        if glyph['representation'] == representation or (glyph['role'] == 'mark.correction' and glyph['representation'] == 'centerline'):
            groups[(glyph['role'], glyph['text'])].append(glyph)
    needed = Counter(visible_requirements(document)) if document is not None else Counter()
    records, requests = [], []
    for role, text in sorted(set(groups) | set(needed)):
        glyphs = sorted(groups.get((role, text), []), key=lambda g: g['glyph_id'])
        observed = [g for g in glyphs if not g.get('derivation')]
        derived = [g for g in glyphs if g.get('derivation')]
        independent_forms, used_evidence = [], set()
        for glyph in sorted(observed, key=lambda g: (len(source_evidence(g, observations)), g['glyph_id'])):
            evidence = source_evidence(glyph, observations)
            if used_evidence.intersection(evidence):
                continue
            independent_forms.append(glyph)
            used_evidence.update(evidence)
        descriptors, fingerprints, representatives = [], set(), []
        for glyph in independent_forms:
            desc = _descriptor(glyph, profile)
            fingerprints.add(desc.fingerprint)
            if all(item_distance(desc, other) >= NEAR_THRESHOLD_MM for other in descriptors):
                descriptors.append(desc)
                representatives.append(glyph['glyph_id'])
        independent = sum(bool(g['source_refs']) and all(ref.get('observation_id') in observations for ref in g['source_refs'])
                          for g in independent_forms)
        occurrences = needed[(role, text)]
        target = min(8, max(2, math.ceil(math.log2(occurrences + 1)))) if occurrences > 1 else 1
        # A target is a bounded collection budget; repeated source copies do not
        # satisfy it and more examples need not imply a messier writer.
        need_more = occurrences > 0 and len(representatives) < target
        record = {'role': role, 'text': text, 'render_mode': mode,
                  'reviewed_records': len(glyphs), 'independent_source_observations': independent,
                  'reviewed_derived_forms': len(derived),
                  'derived_forms_enabled': variation_mode == 'experimental',
                  'source_families': len(independent_forms), 'distinct_path_geometry': len(fingerprints),
                  'separated_shape_count': len(representatives), 'representative_glyph_ids': representatives,
                  'requested_occurrences': occurrences, 'collection_target_hint': target,
                  'needs_more_reviewed_shapes': need_more,
                  'evidence': 'authored_synthetic' if profile['synthetic'] else 'reviewed_source_forms',
                  'limited_shape_space': bool(descriptors) and all(d.limited for d in descriptors)}
        records.append(record)
        if need_more:
            requests.append({'role': role, 'text': text, 'occurrences': occurrences,
                             'separated_shape_count': len(representatives), 'collection_target_hint': target,
                             'action': 'Collect and review more distinct occurrences from the writer\'s original samples. Preserve their shapes and report any remaining reuse.'})
    requests.sort(key=lambda r: (-r['occurrences'], r['role'], r['text']))
    return {'profile_id': profile['profile_id'], 'profile_version': profile['profile_version'],
            'mode': mode, 'variation_mode': variation_mode, 'synthetic': profile['synthetic'], 'threshold_mm_at_3mm_xheight': NEAR_THRESHOLD_MM,
            'groups': records, 'collection_requests': requests,
            'note': 'Shape separation and collection targets guide review; only an audit of the actual rendered pages can establish whether repeats remain.'}
