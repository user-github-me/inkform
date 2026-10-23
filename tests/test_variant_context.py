"""Context eligibility precedes source diversity and never changes ink shape."""
import copy
from pathlib import Path

import pytest

from inkform.errors import InkformError
from inkform.layout import Shaper, map_strokes
from inkform.profiles import geometry_bounds, load_profile
from inkform.variant_context import audit_context_sizes, instance_scale_metric, validate_source_context


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


@pytest.mark.parametrize("text,role,kind",[("6","math.digit","baseline_digit"),("G","text","capital")])
@pytest.mark.parametrize("annotation_location",["observation","glyph"])
def test_reviewed_ordinary_context_does_not_exempt_a_tiny_form_from_size_selection(profile,text,role,kind,annotation_location):
    tiny=donor(profile,text,role,.5,"reviewed-tiny-form")
    observation=annotate(profile,tiny,kind,"body_xheight")
    if annotation_location=="glyph":
        tiny["source_context"]=observation.pop("source_context")
    validate_source_context(profile)
    before=copy.deepcopy(profile)
    shape=writer(profile)
    for index in range(6):
        item=shape.glyph(text,role,f"reviewed-{index}").items[0]
        assert item["glyph_id"]!=tiny["glyph_id"]
        diagnostic=item["variation"]["context_selection"]
        assert diagnostic["basis"]=="estimated_geometry_context"
        assert diagnostic["excluded"][0]["reason"]=="estimated_height_incompatible"
        assert diagnostic["excluded"][0]["source_context"]==kind
    assert profile==before


@pytest.mark.parametrize("reviewed",[False,True])
@pytest.mark.parametrize("context",[None,"superscript","subscript"])
def test_tiny_only_digit_bank_cannot_restore_incompatible_forms_when_peers_establish_scale(profile,reviewed,context):
    # Reproduce the concise assignment's three small sixes: each is around
    # 57–62% of the other digits, so their own median is not a useful reference.
    small=[donor(profile,scale=scale,name=f"small-six-{index}")
           for index,scale in enumerate((.569,.62,.571))]
    profile["glyphs"]=[g for g in profile["glyphs"] if g["text"]!="6" or g in small]
    if reviewed:
        for glyph in small:
            annotate(profile,glyph,"baseline_digit","body_xheight")
    shape=writer(profile)
    with pytest.raises(InkformError) as error:
        shape.glyph("6","math.digit","incompatible-six",context=context)
    assert error.value.code=="missing_context_compatible_glyph"
    diagnostic=error.value.details["context_selection"]
    assert diagnostic["height_reference_basis"]=="peer_character_median"
    assert diagnostic["height_reference_count"]>=3
    assert diagnostic["clear_height_ratio_bounds"]==[.65,1.8]
    assert diagnostic["eligible_count"]==0
    assert {entry["glyph_id"] for entry in diagnostic["excluded"]}=={glyph["glyph_id"] for glyph in small}
    assert all(entry["reason"]=="estimated_height_clearly_incompatible" for entry in diagnostic["excluded"])


@pytest.mark.parametrize("text,role,kind,ratio",[("9","math.digit","baseline_digit",.706),("V","text","capital",.568)])
def test_outside_preferred_height_range_does_not_make_marginal_digits_or_unusual_capitals_unusable(profile,text,role,kind,ratio):
    if kind=="capital":
        # A capital-only bank cannot establish the ordinary ascender scale;
        # this sparse legacy case still has no independent anatomy evidence.
        profile["glyphs"]=[g for g in profile["glyphs"] if g["text"] not in "bdhkl"]
    shape=writer(profile)
    forms=shape.forms[(role,text)]
    reference,_,_=shape.variant_context._reference(forms,text,kind)
    scale=ratio*reference/shape.variant_context.scale_heights[forms[0]["glyph_id"]]
    only=donor(profile,text,role,scale,"unusual-source-form")
    profile["glyphs"]=[g for g in profile["glyphs"] if g["text"]!=text or g is only]
    item=writer(profile).glyph(text,role,"unusual-form").items[0]
    diagnostic=item["variation"]["context_selection"]
    assert item["glyph_id"]==only["glyph_id"]
    assert "no_candidate_in_preferred_height_range" in diagnostic["fallback"]
    assert diagnostic["excluded"]==[]
    assert diagnostic["warnings"]==[{"code":"estimated_height_outside_preferred_range",
                                    "glyph_id":only["glyph_id"],"height_ratio":pytest.approx(ratio)}]
    assert relative(item["strokes"])==relative(map_strokes(only["strokes"],lambda x,y:(x*3,y*3)))


def test_word_initial_capital_uses_ascenders_when_capital_bank_is_undersized(profile):
    ordinary=donor(profile,"H","text",1.,"ordinary-h")
    for glyph in profile["glyphs"]:
        if glyph["text"].isupper() and glyph is not ordinary:
            glyph["strokes"]=map_strokes(glyph["strokes"],lambda x,y:(x*.6,y*.6))
            glyph["ink_bounds"]=geometry_bounds(glyph["strokes"])
    annotate(profile,ordinary,"capital","body_xheight")
    item=writer(profile).text("Half","text","half").items[0]
    assert item["glyph_id"]==ordinary["glyph_id"]
    diagnostic=item["variation"]["context_selection"]
    assert diagnostic["height_reference_basis"]=="ascender_character_median"
    assert diagnostic["height_metric"]=="baseline_to_upper_ink"
    assert diagnostic["local_context"]["word_initial"] is True
    assert diagnostic["local_context"]["next_text"]=="a"
    assert diagnostic["local_context"]["role"]=="text"


def test_missing_normal_capital_is_reported_before_word_emission(profile):
    small=donor(profile,"H","text",.6,"only-short-h")
    profile["glyphs"]=[g for g in profile["glyphs"] if g["text"]!="H" or g is small]
    with pytest.raises(InkformError) as error:
        writer(profile).text("Half","text","half")
    diagnostic=error.value.details["context_selection"]
    assert error.value.code=="missing_context_compatible_glyph"
    assert diagnostic["anatomy"]=="capital"
    assert diagnostic["height_reference_basis"]=="ascender_character_median"
    assert diagnostic["local_context"]["word_initial"] is True
    assert diagnostic["eligible_count"]==0


def test_local_lowercase_neighbors_filter_a_form_that_global_body_range_allows(profile):
    small=donor(profile,"a","text",.75,"short-a")
    for text in "ce":
        source=donor(profile,text,"text",1.2,"large-"+text)
        profile["glyphs"]=[g for g in profile["glyphs"] if g["text"]!=text or g is source]
    shape=writer(profile)
    global_forms,_=shape.variant_context.eligible(shape.forms[("text","a")],"a","text")
    assert small in global_forms
    for index in range(3):
        item=shape.text("ace","text",f"local-{index}").items[0]
        assert item["glyph_id"]!=small["glyph_id"]
        diagnostic=item["variation"]["context_selection"]
        assert diagnostic["height_reference_basis"]=="neighbor_character_median"
        assert diagnostic["height_reference_count"]==2
        assert diagnostic["height_reference_mm"]==pytest.approx(3*diagnostic["height_reference_xheight"])
        assert item["text"]=="a" and diagnostic["local_context"]["word_initial"]


def test_descenders_and_detached_dots_do_not_define_lowercase_body_size(profile):
    for text in "gij":
        glyph=next(g for g in profile["glyphs"] if g["text"]==text and g["role"]=="text")
        if text=="g":
            glyph["strokes"]=map_strokes(glyph["strokes"],lambda x,y:(x,y*4 if y>0 else y))
        else:
            # An authored detached dot far above the body must not make the
            # stem ineligible or cause a smaller stem to be selected.
            glyph["strokes"].append({"start":[.2,-3.],"segments":[{"type":"line","end":[.2,-3.05]}]})
        glyph["ink_bounds"]=geometry_bounds(glyph["strokes"])
    shape=writer(profile)
    for text in "gij":
        item=shape.glyph(text,"text",text).items[0]
        diagnostic=item["variation"]["context_selection"]
        assert diagnostic["anatomy"]=="body_xheight"
        assert diagnostic["excluded"]==[]
        assert item["variation"]["source_shape_preserved"]


def test_body_and_ascender_anatomy_keep_script_scale_and_literal_case(profile):
    shape=writer(profile)
    ordinary=shape.text("Halfgij","text","body").items
    previous=shape.glyph_context
    shape.glyph_context="superscript"
    script=shape.text("Halfgij","text","script",size=2.04).items
    shape.glyph_context=previous
    assert "".join(item["text"] for item in ordinary)=="Halfgij"
    assert "".join(item["text"] for item in script)=="Halfgij"
    assert all(item["nominal_size_mm"]==2.04 for item in script)
    assert all(item["variation"]["context_selection"]["requested"]=="superscript" for item in script)
    for base,sup in zip(ordinary,script):
        b=geometry_bounds(base["strokes"]);s=geometry_bounds(sup["strokes"])
        assert s[3]-s[1]==pytest.approx((b[3]-b[1])*.68)


def test_actual_selected_neighbors_keep_body_sizes_coherent(profile):
    for text in "ac":
        small=donor(profile,text,"text",.85,"small-"+text)
        large=donor(profile,text,"text",1.35,"large-"+text)
        profile["glyphs"]=[g for g in profile["glyphs"] if g["text"]!=text or g in (small,large)]
    shape=writer(profile)
    for index in range(6):
        first,second=shape.text("ac","text",f"pair-{index}").items
        a,b=instance_scale_metric(first),instance_scale_metric(second)
        assert .72 <= b["height_mm"]/a["height_mm"] <= 1.5
        assert second["variation"]["context_selection"]["height_reference_basis"]=="selected_neighbor_median"


def test_small_neighbor_cannot_override_clear_independent_digit_scale(profile):
    small=donor(profile,scale=.5,name="only-tiny-six")
    profile["glyphs"]=[g for g in profile["glyphs"] if g["text"]!="6" or g is small]
    shape=writer(profile)
    with pytest.raises(InkformError) as error:
        shape.variant_context.eligible(shape.forms[("math.digit","6")],"6","math.digit",
            neighbor_metrics=[{"text":"1","anatomy":"baseline_digit","height_xheight":.6,"height_mm":1.8}])
    diagnostic=error.value.details["context_selection"]
    assert diagnostic["height_reference_basis"]=="selected_neighbor_median"
    assert diagnostic["clear_height_reference_basis"]=="peer_character_median"
    assert diagnostic["eligible_count"]==0


def test_emitted_size_metrics_follow_baseline_translation(profile):
    box=writer(profile).text("Agij","text","metrics",size=2.04)
    moved=box.shifted(15,120)
    for first,second in zip(box.items,moved.items):
        a,b=instance_scale_metric(first),instance_scale_metric(second)
        assert b["height_mm"]==pytest.approx(a["height_mm"])
        assert b["height_xheight"]==pytest.approx(a["height_xheight"])
        assert second["baseline_y_mm"]==pytest.approx(first["baseline_y_mm"]+120)


def test_placed_ink_audit_checks_actual_size_and_respects_scripts_and_marks(profile):
    shape=writer(profile)
    box=shape.text("Halfgij.","text","audit",size=2.04).shifted(10,75)
    before=audit_context_sizes(box.items)
    assert before["checked_glyph_count"]==7
    assert before["marks_and_symbols_skipped"]==1
    assert before["errors"]==[]
    capital=box.items[0]
    baseline=capital["baseline_y_mm"]
    capital["strokes"]=map_strokes(capital["strokes"],lambda x,y:(x,baseline+(y-baseline)*.4))
    after=audit_context_sizes(box.items)
    assert len(after["errors"])==1
    assert after["errors"][0]["token_id"]==capital["token_id"]
    assert after["errors"][0]["code"]=="placed_ink_context_size_mismatch"


def test_pdf_context_size_audit_measures_emitted_paths_and_rejects_a_shrunken_capital(profile,tmp_path):
    import hashlib
    from inkform.document import literal_document
    from inkform.layout import plan_document
    from inkform.rendering import export_pdf
    from inkform.validation import check_pdf
    for glyph in profile["glyphs"]:
        if glyph["text"]=="i":
            for stroke in glyph["strokes"]:
                stroke["width_profile"]=[{"s":0,"multiplier":.8},{"s":.5,"multiplier":1.2},{"s":1,"multiplier":.8}]
    plan=plan_document(literal_document("Half-life"),profile)
    pdf=tmp_path/"normal.pdf"
    export_pdf(plan,profile,pdf,"normal")
    plan["pdf_sha256"]=hashlib.sha256(pdf.read_bytes()).hexdigest()
    normal=check_pdf(pdf,plan,preview_dpi=36)
    assert normal["ok"],normal["errors"]
    assert normal["checks"]["context_sizes"]=="pass"
    assert normal["context_size_audit"]["geometry_source"]=="emitted_pdf_vector_paths"
    assert normal["context_size_audit"]["checked_glyph_count"]==8
    changed=copy.deepcopy(plan)
    capital=next(item for item in changed["instances"] if item["text"]=="H")
    baseline=capital["baseline_y_mm"]
    capital["strokes"]=map_strokes(capital["strokes"],lambda x,y:(x,baseline+(y-baseline)*.4))
    tampered=tmp_path/"tiny-h.pdf"
    export_pdf(changed,profile,tampered,"tiny h")
    plan["pdf_sha256"]=hashlib.sha256(tampered.read_bytes()).hexdigest()
    # Retain the original passing sidecar audit while changing actual PDF ink.
    report=check_pdf(tampered,plan,preview_dpi=36)
    assert not report["ok"]
    assert plan["context_size_audit"]["errors"]==[]
    assert report["checks"]["context_sizes"]=="fail"
    assert report["context_size_audit"]["errors"][0]["text"]=="H"
    assert "context_size_mismatch" in {entry["code"] for entry in report["errors"]}


@pytest.mark.parametrize("text",["a","l","g","i"])
def test_body_and_ascender_selection_excludes_tiny_sources_with_only_two_forms(profile,text):
    small=donor(profile,text,"text",.45,"small-"+text)
    shape=writer(profile)
    for index in range(3):
        item=shape.glyph(text,"text",f"letter-{index}").items[0]
        assert item["glyph_id"]!=small["glyph_id"]
        assert item["variation"]["context_selection"]["excluded"][0]["glyph_id"]==small["glyph_id"]


def test_marginal_digit_fallback_does_not_restore_a_clearly_tiny_alternative(profile):
    tiny=donor(profile,scale=.5,name="clearly-tiny-six")
    marginal=donor(profile,scale=.7,name="marginal-six")
    profile["glyphs"]=[g for g in profile["glyphs"] if g["text"]!="6" or g in (tiny,marginal)]
    item=writer(profile).glyph("6","math.digit","fallback-six").items[0]
    assert item["glyph_id"]==marginal["glyph_id"]
    diagnostic=item["variation"]["context_selection"]
    assert diagnostic["excluded"][0]["glyph_id"]==tiny["glyph_id"]
    assert diagnostic["excluded"][0]["reason"]=="estimated_height_clearly_incompatible"
    assert diagnostic["warnings"][0]["glyph_id"]==marginal["glyph_id"]


def test_script_fallback_checks_ordinary_source_size_before_one_layout_reduction(profile):
    tiny=donor(profile,scale=.5,name="reviewed-tiny-six")
    annotate(profile,tiny,"baseline_digit","body_xheight")
    shape=writer(profile)
    for context in ("superscript","subscript"):
        item=shape.glyph("6","math.digit",context,size=2.04,context=context).items[0]
        assert item["glyph_id"]!=tiny["glyph_id"]
        source=next(g for g in profile["glyphs"] if g["glyph_id"]==item["glyph_id"])
        assert relative(item["strokes"])==relative(map_strokes(source["strokes"],lambda x,y:(x*2.04,y*2.04)))
        assert item["nominal_size_mm"]==2.04


@pytest.mark.parametrize("reviewed",[False,True])
def test_legitimate_observed_variants_remain_balanced_and_deterministic(profile,reviewed):
    donor(profile,scale=.9,name="normal-six-b")
    donor(profile,scale=1.1,name="normal-six-c")
    if reviewed:
        for glyph in profile["glyphs"]:
            if glyph["text"]=="6" and glyph["role"]=="math.digit":
                annotate(profile,glyph,"baseline_digit","body_xheight")
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
