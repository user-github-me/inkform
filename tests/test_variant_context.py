"""Context eligibility precedes source diversity and never changes ink shape."""
import copy
from pathlib import Path

import pytest

from inkform.errors import InkformError
from inkform.layout import Shaper, map_strokes
from inkform.profiles import geometry_bounds, load_profile
from inkform.variant_context import validate_source_context


@pytest.fixture
def profile():
    result=copy.deepcopy(load_profile(Path(__file__).parents[1]/"examples/synthetic-profile.json"))
    result["variation"]={"mode":"observed","amount":0}
    return result


def donor(profile,text="6",role="math.digit",scale=.5,name="small-six"):
    glyph=copy.deepcopy(next(g for g in profile["glyphs"] if g["text"]==text and g["role"]==role))
    glyph["glyph_id"]=name
    glyph["strokes"]=map_strokes(glyph["strokes"],lambda x,y:(x*scale,y*scale))
    glyph["ink_bounds"]=geometry_bounds(glyph["strokes"])
    glyph["advance"]*=scale
    profile["glyphs"].append(glyph)
    return glyph


def writer(profile,seed=72,mode="fresh_strokes"):
    return Shaper(profile,seed,mode,profile["physical_scale"]["body_x_height_mm"])


def relative(strokes):
    x,y=strokes[0]["start"]
    return map_strokes(strokes,lambda px,py:(round(px-x,8),round(py-y,8)))


def annotate(profile,glyph,kind,normalization=None):
    # Explicitly authored source-context fixture, not user evidence.
    source_id="context-test-source"; obs_id="context-"+glyph["glyph_id"]
    profile.update(synthetic=False,sources=[{"source_id":source_id,"sha256":"a"*64}])
    glyph["source_refs"]=[{"source_id":source_id,"observation_id":obs_id}]
    annotation={"kind":kind,"review_status":"reviewed","reviewed_by":"unit-test authored context fixture"}
    if normalization is not None: annotation["normalization"]=normalization
    observation={"observation_id":obs_id,"source_id":source_id,"source_sha256":"a"*64,"review_status":"reviewed",
                 "reviewed_by":"unit-test authored context fixture","source_context":annotation}
    profile.setdefault("observations",[]).append(observation)
    return observation


def test_baseline_digit_excludes_tiny_donor_before_balancing(profile):
    tiny=donor(profile)
    shape=writer(profile)
    items=[shape.glyph("6","math.digit",f"six-{i}").items[0] for i in range(8)]
    assert all(item["glyph_id"]!=tiny["glyph_id"] for item in items)
    diagnosis=items[0]["variation"]["context_selection"]
    assert diagnosis["requested"]=="baseline_digit"
    assert diagnosis["basis"]=="estimated_geometry_context"
    assert diagnosis["excluded"]==[{"glyph_id":"small-six","reason":"estimated_height_incompatible","height_xheight":tiny["ink_bounds"][3]-tiny["ink_bounds"][1]}]
    source=next(g for g in profile["glyphs"] if g["glyph_id"]==items[0]["glyph_id"])
    assert relative(items[0]["strokes"])==relative(map_strokes(source["strokes"],lambda x,y:(x*3,y*3)))


def test_capital_uses_peer_capital_height_without_changing_case_or_contour(profile):
    small=donor(profile,"G","text",.48,"tiny-capital-g")
    shape=writer(profile)
    for index in range(6):
        item=shape.glyph("G","text",f"capital-{index}").items[0]
        assert item["glyph_id"]!=small["glyph_id"]
        assert item["text"]=="G" and item["variation"]["source_shape_preserved"]
        assert item["variation"]["context_selection"]["requested"]=="capital"


def test_legitimate_observed_variants_remain_balanced_and_deterministic(profile):
    donor(profile,scale=.9,name="normal-six-b")
    donor(profile,scale=1.1,name="normal-six-c")
    first,second=writer(profile,12),writer(profile,12)
    one=[first.glyph("6","math.digit",f"six-{i}").items[0] for i in range(6)]
    two=[second.glyph("6","math.digit",f"six-{i}").items[0] for i in range(6)]
    assert one==two
    assert len({item["glyph_id"] for item in one[:3]})==3
    assert all(item["variation"]["context_selection"]["excluded"]==[] for item in one)


def test_script_ast_prefers_explicit_context_with_local_normalization(profile):
    script=donor(profile,scale=.98,name="reviewed-script-six")
    annotate(profile,script,"superscript","context_xheight")
    validate_source_context(profile)
    shape=writer(profile)
    expr={"type":"superscript","base":{"type":"number","value":"6"},"script":{"type":"number","value":"6"}}
    result=shape.math(expr,"power")
    base,sup=result.items
    assert base["glyph_id"]!=script["glyph_id"]
    assert sup["glyph_id"]==script["glyph_id"]
    assert sup["nominal_size_mm"]==pytest.approx(3*.68)
    assert sup["variation"]["context_selection"]["basis"]=="reviewed_source_context"
    assert sup["variation"]["context_selection"]["requested"]=="superscript"
    assert shape.glyph_context is None
    assert relative(sup["strokes"])==relative(map_strokes(script["strokes"],lambda x,y:(x*3*.68,y*3*.68)))


@pytest.mark.parametrize("normalization",[None,"body_xheight"])
def test_script_source_without_local_normalization_is_not_reduced_twice(profile,normalization):
    script=donor(profile,scale=.5,name="body-normalized-script")
    annotate(profile,script,"superscript",normalization)
    shape=writer(profile)
    item=shape.glyph("6","math.digit","script",size=2.04,context="superscript").items[0]
    assert item["glyph_id"]!=script["glyph_id"]
    diagnostic=item["variation"]["context_selection"]
    assert "supported_normalization" in diagnostic["fallback"]
    assert diagnostic["excluded"][0]["reason"]=="script_source_normalization_unsupported"


def test_fraction_reduction_does_not_infer_superscript_context(profile):
    script=donor(profile,scale=.98,name="script-six")
    annotate(profile,script,"superscript","context_xheight")
    shape=writer(profile)
    expr={"type":"fraction","numerator":{"type":"number","value":"6"},"denominator":{"type":"number","value":"6"}}
    items=[i for i in shape.math(expr,"fraction").items if i["role"]=="math.digit"]
    assert all(item["glyph_id"]!=script["glyph_id"] for item in items)
    assert all(item["variation"]["context_selection"]["requested"]=="baseline_digit" for item in items)


def test_unknown_tiny_form_is_not_claimed_as_a_source_observed_superscript(profile):
    tiny=donor(profile)
    item=writer(profile).glyph("6","math.digit","script",size=2.04,context="superscript").items[0]
    assert item["glyph_id"]!=tiny["glyph_id"]
    diagnostic=item["variation"]["context_selection"]
    assert diagnostic["basis"]=="estimated_geometry_context"
    assert diagnostic["fallback"] is not None


def test_sparse_legacy_profile_preserves_original_geometry_without_upscaling(profile):
    only=donor(profile,scale=.4)
    profile["glyphs"]=[only]
    item=writer(profile).glyph("6","math.digit","only-six").items[0]
    assert item["glyph_id"]==only["glyph_id"]
    assert relative(item["strokes"])==relative(map_strokes(only["strokes"],lambda x,y:(3*x,3*y)))
    assert "insufficient_comparable_geometry" in item["variation"]["context_selection"]["fallback"]


def test_source_context_requires_identified_review_and_real_source_binding(profile):
    glyph=donor(profile)
    observation=annotate(profile,glyph,"superscript","context_xheight")
    observation["source_context"]["reviewed_by"]=""
    with pytest.raises(InkformError): validate_source_context(profile)


def test_legacy_free_text_source_context_remains_a_note_without_review_claim(profile):
    glyph=donor(profile)
    glyph["source_context"]="IMG1893 graph; copied from source notes, possibly a small digit"
    profile["observations"]=[{"observation_id":"legacy-note","source_id":"unused-fixture-source",
                              "source_context":"superscript context was not classified"}]
    validate_source_context(profile)
    item=writer(profile).glyph("6","math.digit","baseline").items[0]
    assert item["glyph_id"]!=glyph["glyph_id"]
    assert item["variation"]["context_selection"]["basis"]=="estimated_geometry_context"


@pytest.mark.parametrize("change",[
    lambda p,g,o:o.update(source_sha256="b"*64),
    lambda p,g,o:o["source_context"].update(reviewed_by="unknown"),
    lambda p,g,o:g.update(source_context=o.pop("source_context"),source_refs=[{"source_id":"context-test-source"}]),
    lambda p,g,o:o.update(review_status="proposed"),
])
def test_context_annotation_cannot_replace_exact_reviewed_source_provenance(profile,change):
    glyph=donor(profile)
    observation=annotate(profile,glyph,"superscript","context_xheight")
    change(profile,glyph,observation)
    with pytest.raises(InkformError): validate_source_context(profile)


def test_known_script_only_bank_does_not_silently_render_wrong_baseline_or_reduce_again(profile):
    glyph=donor(profile)
    annotate(profile,glyph,"superscript","body_xheight")
    profile["glyphs"]=[glyph]
    shape=writer(profile)
    for context in (None,"superscript"):
        with pytest.raises(InkformError) as error:
            shape.glyph("6","math.digit","incompatible",context=context,size=2.04)
        assert error.value.code=="missing_context_compatible_glyph"


def test_geometrically_equal_independent_sources_keep_distinct_reviewed_contexts(profile):
    script=donor(profile,scale=1,name="same-contour-script")
    annotate(profile,script,"superscript","context_xheight")
    ordinary=next(g for g in profile["glyphs"] if g["text"]=="6" and g["role"]=="math.digit" and g["glyph_id"]!=script["glyph_id"])
    annotate(profile,ordinary,"baseline_digit")
    shape=writer(profile)
    assert shape.glyph("6","math.digit","base").items[0]["glyph_id"]==ordinary["glyph_id"]
    assert shape.glyph("6","math.digit","power",context="superscript").items[0]["glyph_id"]==script["glyph_id"]


def test_clean_outline_body_can_select_separate_reviewed_centerline_primitives(profile):
    glyph=next(g for g in profile["glyphs"] if g["role"]=="text" and g["text"]=="a")
    glyph.update(representation="clean_outline",outline_calibration={"body_x_height_mm":3,"pen_width_mm":profile["pen"]["width_mm"]})
    shape=writer(profile,mode="clean_outline")
    body=shape.glyph("a","text","body").items[0]
    primitive=shape.glyph("√","math.operator","root",primitive=True).items[0]
    assert body["representation"]=="clean_outline"
    assert primitive["representation"]=="centerline"
    assert primitive["pen_width_mm"]==profile["pen"]["width_mm"]
    assert primitive["variation"]["source_shape_preserved"]


def test_training_readiness_and_diversity_include_scalable_marks_beside_outline_body(profile):
    from inkform.diversity import profile_diversity
    from inkform.training import _readiness
    outline=copy.deepcopy(next(g for g in profile["glyphs"] if g["role"]=="math.variable" and g["text"]=="x"))
    outline.update(glyph_id="authored-outline-x",representation="clean_outline",ink_bounds=[0,-1,.5,0],advance=.7,
                   strokes=[{"start":[0,0],"closed":True,"segments":[{"type":"line","end":[0,-1]},
                            {"type":"line","end":[.5,-1]},{"type":"line","end":[.5,0]},{"type":"line","end":[0,0]}]}],
                   outline_calibration={"body_x_height_mm":3,"pen_width_mm":profile["pen"]["width_mm"]})
    profile["glyphs"].append(outline)
    profile["default_render_mode"]="clean_outline"
    document={"schema_version":"1.0","document_id":"mixed-root-readiness","content_mode":"literal",
              "blocks":[{"id":"root","type":"display_math","expression":{"type":"root","body":{"type":"symbol","value":"x"}}}],
              "unresolved_inputs":[]}
    readiness=_readiness(profile,document)
    assert readiness["ready"] and readiness["coverage"]["complete"]
    groups={(group["role"],group["text"]):group for group in profile_diversity(profile,document)["groups"]}
    assert groups[("math.operator","√")]["reviewed_records"]>0
    assert groups[("math.operator","−")]["reviewed_records"]>0
    assert not groups[("math.operator","√")]["needs_more_reviewed_shapes"]


@pytest.mark.parametrize("raw_alias_pool",[False,True])
def test_repeated_root_primitives_with_same_observation_aliases_keep_one_source_family(profile,raw_alias_pool):
    profile["sources"]=[{"source_id":"authored-primitives","sha256":"a"*64,"synthetic":True}]
    raw={}
    for text,name in (("√","root"),("−","bar")):
        source=next(g for g in profile["glyphs"] if g["role"]=="math.operator" and g["text"]==text)
        source["source_refs"]=[{"source_id":"authored-primitives","observation_id":name+"-source"}]
        profile.setdefault("observations",[]).append({"observation_id":name+"-source","source_id":"authored-primitives",
                                                      "source_sha256":"a"*64,"synthetic":True})
        alias=copy.deepcopy(source)
        alias["glyph_id"]=name+"-same-source-alias"
        alias["strokes"]=map_strokes(alias["strokes"],lambda x,y:(x*1.02,y))
        alias["ink_bounds"]=geometry_bounds(alias["strokes"])
        profile["glyphs"].append(alias)
        raw[("math.operator",text)]=[source,alias]
    shape=writer(profile)
    assert all(len(shape.primitive_forms[key])==1 for key in raw)
    if raw_alias_pool:
        # Exercise the selection boundary as well as the standard dedup pass:
        # a future primitive provider cannot empty the pool via shared recency.
        shape.primitive_forms.update(raw)
    for index in range(4):
        result=shape.math({"type":"root","body":{"type":"number","value":"1"}},f"root-{index}")
        primitives=[item for item in result.items if item["text"] in {"√","−"}]
        assert len(primitives)==2
        assert all(item["variation"]["independent_form_count"]==1 for item in primitives)
        assert all(item["variation"]["source_use_index"]==index+1 for item in primitives)
