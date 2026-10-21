"""Useful collection requests must not mistake aliases for new source forms."""
import copy
from pathlib import Path

from inkform.diversity import profile_diversity
from inkform.document import literal_document
from inkform.layout import map_strokes
from inkform.profiles import load_profile


def writer():
    return load_profile(Path(__file__).resolve().parents[1] / "examples/synthetic-profile.json")


def group(report, role, text):
    return next(g for g in report['groups'] if (g['role'], g['text']) == (role, text))


def test_repeated_geometry_alias_does_not_inflate_diversity_or_change_input():
    profile = writer()
    original = next(g for g in profile['glyphs'] if (g['role'], g['text']) == ('text', 'n'))
    alias = copy.deepcopy(original)
    alias['glyph_id'] = 'renamed-n'
    alias['strokes'] = map_strokes(alias['strokes'], lambda x,y: (x+.1, y+.1))
    alias['ink_bounds'] = [v+.1 for v in alias['ink_bounds']]
    profile['glyphs'].append(alias)
    before = copy.deepcopy(profile)
    report = profile_diversity(profile, literal_document('n '*20))
    record = group(report, 'text', 'n')
    assert record['reviewed_records'] == 2
    assert record['source_families'] == record['separated_shape_count'] == 1
    assert record['needs_more_reviewed_shapes']
    assert record['requested_occurrences'] == 20
    assert record['independent_source_observations'] == 0
    assert profile == before


def test_missing_form_remains_a_collection_gap_and_roles_are_distinct():
    report = profile_diversity(writer(), literal_document('漢 nnn'))
    missing = group(report, 'text', '漢')
    assert missing['separated_shape_count'] == missing['source_families'] == 0
    assert missing['needs_more_reviewed_shapes']
    assert missing['requested_occurrences'] == 1
    assert group(report, 'math.variable', 'x')['requested_occurrences'] == 0


def test_distinct_actual_geometry_expands_bank_but_near_copy_does_not():
    profile = writer()
    original = next(g for g in profile['glyphs'] if (g['role'], g['text']) == ('text', 'n'))
    for index, factor in enumerate((1.00001, .65, 1.35)):
        form = copy.deepcopy(original)
        form['glyph_id'] = f'new-n-{index}'
        form['strokes'] = map_strokes(form['strokes'], lambda x,y: (x*factor, y))
        form['ink_bounds'] = [form['ink_bounds'][0]*factor, form['ink_bounds'][1],
                              form['ink_bounds'][2]*factor, form['ink_bounds'][3]]
        form['advance'] *= factor
        profile['glyphs'].append(form)
    record = group(profile_diversity(profile, literal_document('nnnn')), 'text', 'n')
    assert record['source_families'] == 4
    assert record['separated_shape_count'] == 3
    assert not record['needs_more_reviewed_shapes']
