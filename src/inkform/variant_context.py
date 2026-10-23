"""Choose context-compatible observations before balancing source diversity.

These rules never resize, relabel or deform a glyph. Reviewed annotations are
source evidence; relative-height checks are explicitly geometry estimates.
Sparse legacy banks remain usable when a reliable comparison is unavailable.
"""
from __future__ import annotations

import math
from statistics import median

from .errors import InkformError


CONTEXTS = {"body", "capital", "baseline_digit", "superscript", "subscript"}
SCRIPT_CONTEXTS = {"superscript", "subscript"}
NORMALIZATIONS = {"body_xheight", "context_xheight"}
PREFERRED_HEIGHT_RATIO_BOUNDS = (.72, 1.5)
CLEAR_HEIGHT_RATIO_BOUNDS = (.65, 1.8)
# Comparable adjacent ink must fit in both reading directions. The bank
# bounds remain asymmetric because a bank median is not another occurrence.
NEIGHBOR_HEIGHT_RATIO_BOUNDS = (max(CLEAR_HEIGHT_RATIO_BOUNDS[0], 1/CLEAR_HEIGHT_RATIO_BOUNDS[1]),
                              min(CLEAR_HEIGHT_RATIO_BOUNDS[1], 1/CLEAR_HEIGHT_RATIO_BOUNDS[0]))
LATIN_CAPITALS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
BODY_LETTERS = frozenset("acegijmnopqrsuvwxyz")
DOTTED_BODY_LETTERS = frozenset("ij")
# After excluding the dot, an i/j stem can begin below the ordinary body
# zone. It measures another i/j occurrence, not a different letter's body.
BODY_REFERENCES = BODY_LETTERS-DOTTED_BODY_LETTERS
ASCENDER_LETTERS = frozenset("bdfhklt")
# f and t have particularly variable upper extensions; their own source
# forms remain usable, but they do not establish the capital/stem scale.
ASCENDER_REFERENCES = frozenset("bdhkl")


def natural_context(text, role):
    if role == "mark.correction" or role == "math.operator":
        return None
    if len(text) == 1 and text.isdecimal():
        return "baseline_digit"
    if len(text) == 1 and text.isupper():
        return "capital"
    if len(text) == 1 and text.islower():
        return "body"
    return None


def _valid_annotation(annotation):
    from .profiles import reviewed_actor
    return (isinstance(annotation, dict) and annotation.get("kind") in CONTEXTS
            and annotation.get("review_status") == "reviewed"
            and reviewed_actor(annotation.get("reviewed_by"))
            and ("normalization" not in annotation or annotation["normalization"] in NORMALIZATIONS))


def _bound_observation(observation, sources, source_id=None):
    from .profiles import reviewed_actor
    source=sources.get(observation.get("source_id"),{})
    return (bool(source.get("sha256")) and observation.get("source_sha256")==source["sha256"]
            and (source_id is None or observation.get("source_id")==source_id)
            and observation.get("review_status")=="reviewed" and reviewed_actor(observation.get("reviewed_by")))


def source_context(glyph, observations, sources):
    refs = glyph.get("source_refs", [])
    own = glyph.get("source_context")
    if _valid_annotation(own) and any(_bound_observation(observations.get(ref.get("observation_id"),{}),sources,ref.get("source_id")) for ref in refs):
        return own
    annotations = []
    for ref in refs:
        observation = observations.get(ref.get("observation_id"), {})
        annotation = observation.get("source_context")
        if (_valid_annotation(annotation) and _bound_observation(observation,sources,ref.get("source_id"))):
            annotations.append(annotation)
    if annotations and len({(a["kind"], a.get("normalization")) for a in annotations}) == 1:
        return annotations[0]
    return None


def validate_source_context(profile):
    """Validate optional annotations as reviewed, source-bound information."""
    sources = {source["source_id"]:source for source in profile.get("sources", [])}
    observations = {observation["observation_id"]:observation for observation in profile.get("observations", [])}
    for section in ("glyphs", "observations"):
        for index, entry in enumerate(profile.get(section, [])):
            annotation = entry.get("source_context")
            # Earlier profiles used this field for free-form source notes.
            # Such notes remain valid and never become reviewed class labels.
            if annotation is None or isinstance(annotation,str):
                continue
            path = f"/{section}/{index}/source_context"
            if not _valid_annotation(annotation):
                raise InkformError("invalid_profile", "Source context needs a supported kind and an identified explicit review.", {"path": path})
            bound = _bound_observation(entry,sources) if section == "observations" else any(
                _bound_observation(observations.get(ref.get("observation_id"),{}),sources,ref.get("source_id")) for ref in entry.get("source_refs",[]))
            if not bound or profile.get("synthetic") is True:
                raise InkformError("invalid_profile", "Reviewed source context must identify personal source evidence.", {"path": path})


def _height(glyph):
    from .profiles import geometry_bounds
    bounds = geometry_bounds(glyph["strokes"])
    value = bounds[3]-bounds[1]
    return value if math.isfinite(value) and value > 1e-8 else None


def anatomy_context(text, natural):
    if natural == "body":
        if text in BODY_LETTERS:
            return "body_xheight"
        if text in ASCENDER_LETTERS:
            return "ascender"
    return natural


def _upper_extent(glyph, baseline=0., size=1.):
    """Ink above the source baseline, without counting a descender or dot.

    This is an estimate of the vertical writing zone, not a glyph bounding
    box or a requirement that different anatomical classes be equally tall.
    The baseline-connected stem of i/j is measured separately from its dot.
    """
    from .profiles import geometry_bounds
    bounds = [geometry_bounds([stroke]) for stroke in glyph["strokes"]]
    if glyph["text"] in {"i", "j"}:
        connected={index for index,box in enumerate(bounds) if box[3] >= baseline-.25*size}
        if connected:
            # Export can split one variable-width stem into short path paints.
            # Follow touching endpoints so this representation change does
            # not turn its upper fragments into a purported detached dot.
            endpoints=[[stroke["start"],*[segment["end"] for segment in stroke["segments"]]] for stroke in glyph["strokes"]]
            remaining=set(range(len(bounds)))-connected
            while remaining:
                points=[point for index in connected for point in endpoints[index]]
                found={index for index in remaining if any(math.dist(point,other)<=.03*size
                                                          for point in endpoints[index] for other in points)}
                if not found:
                    break
                connected.update(found)
                remaining.difference_update(found)
            bounds=[bounds[index] for index in connected]
    value = baseline-min((box[1] for box in bounds), default=baseline)
    return value if math.isfinite(value) and value > 1e-8 else None


def instance_scale_metric(item):
    """Measure emitted ink in its semantic size and translated baseline."""
    category=anatomy_context(item["text"],natural_context(item["text"],item["role"]))
    size=item.get("nominal_size_mm")
    if not size or category not in {"capital","body_xheight","ascender","baseline_digit"}:
        return None
    if category=="baseline_digit":
        height=_height(item)
    else:
        baseline=item.get("baseline_y_mm")
        if baseline is None:
            return None
        height=_upper_extent(item,baseline,size)
    if height is None:
        return None
    return {"token_id":item.get("token_id"),"text":item["text"],"anatomy":category,"height_mm":height,"height_xheight":height/size}


def _strong_reference(category, basis, count):
    if category=="capital" and basis=="peer_character_median":
        return False
    return ((basis in {"peer_character_median","ascender_character_median"} and count>=3)
            or (basis in {"neighbor_character_median","neighbor_ascender_median"} and count>=2))


def audit_context_sizes(instances, *, tolerance_mm=.0002):
    """Check placed ink against the context used to select each source form."""
    result={"version":"inkform-context-size-audit-1","tolerance_mm":tolerance_mm,"checked_glyph_count":0,
            "without_comparable_reference":0,"marks_and_symbols_skipped":0,
            "errors":[],"warnings":[]}
    metrics={item.get("token_id"):instance_scale_metric(item) for item in instances}
    for item in instances:
        metric=metrics[item.get("token_id")]
        if metric is None:
            result["marks_and_symbols_skipped"]+=1
            continue
        context=item.get("variation",{}).get("context_selection",{})
        reference=context.get("height_reference_xheight")
        neighbor_reference=context.get("selected_neighbor_clear_height_reference_xheight")
        neighbor_tokens=context.get("selected_neighbor_token_ids",[])
        if neighbor_tokens:
            neighbors={}
            for token in neighbor_tokens:
                neighbor=metrics.get(token)
                if neighbor is not None:
                    neighbors.setdefault(neighbor["text"],[]).append(neighbor["height_xheight"])
            if any(metrics.get(token) is None for token in neighbor_tokens):
                result["errors"].append({"code":"missing_selected_neighbor_ink","token_id":item.get("token_id"),
                                         "neighbor_token_ids":neighbor_tokens})
                continue
            neighbor_reference=median(median(values) for values in neighbors.values())
            if context.get("height_reference_basis")=="selected_neighbor_median":
                reference=neighbor_reference
        if not reference:
            result["without_comparable_reference"]+=1
            continue
        result["checked_glyph_count"]+=1
        ratio=metric["height_xheight"]/reference
        record={"token_id":item.get("token_id"),"glyph_id":item.get("glyph_id"),
                "text":item["text"],"role":item["role"],"page":item.get("page"),
                "anatomy":metric["anatomy"],"requested_context":context.get("requested"),
                "height_mm":metric["height_mm"],"reference_height_mm":reference*item["nominal_size_mm"],
                "height_ratio":ratio,"reference_basis":context.get("height_reference_basis")}
        clear_reference=context.get("clear_height_reference_xheight")
        if clear_reference:
            clear_ratio=metric["height_xheight"]/clear_reference
            low,high=context["clear_height_ratio_bounds"]
            epsilon=tolerance_mm/(clear_reference*item["nominal_size_mm"])
            if not low-epsilon <= clear_ratio <= high+epsilon:
                result["errors"].append({**record,"code":"placed_ink_context_size_mismatch",
                                         "clear_height_ratio":clear_ratio})
                continue
        if neighbor_reference:
            neighbor_ratio=metric["height_xheight"]/neighbor_reference
            low,high=context["selected_neighbor_clear_height_ratio_bounds"]
            epsilon=tolerance_mm/(neighbor_reference*item["nominal_size_mm"])
            if not low-epsilon <= neighbor_ratio <= high+epsilon:
                result["errors"].append({**record,"code":"placed_ink_selected_neighbor_size_mismatch",
                                         "selected_neighbor_height_ratio":neighbor_ratio,
                                         "selected_neighbor_reference_height_mm":neighbor_reference*item["nominal_size_mm"]})
                continue
        low,high=context.get("height_ratio_bounds",PREFERRED_HEIGHT_RATIO_BOUNDS)
        epsilon=tolerance_mm/(reference*item["nominal_size_mm"])
        if not low-epsilon <= ratio <= high+epsilon:
            result["warnings"].append({**record,"code":"placed_ink_outside_preferred_height_range"})
    return result


class VariantContext:
    def __init__(self, forms, observations, sources):
        self.observations = observations
        self.sources = {source["source_id"]:source for source in sources}
        self.heights = {}
        self.scale_heights = {}
        self.by_category = {"capital": {}, "baseline_digit": {}, "body_xheight": {}, "ascender": {}}
        seen = set()
        for candidates in forms.values():
            for glyph in candidates:
                identity = glyph["glyph_id"]
                if identity in seen:
                    continue
                seen.add(identity)
                self.heights[identity] = _height(glyph)
                natural = natural_context(glyph["text"],glyph["role"])
                category = anatomy_context(glyph["text"],natural)
                self.scale_heights[identity] = (_upper_extent(glyph) if category in {"capital","body_xheight","ascender"}
                                                else self.heights[identity])
                if glyph.get("derivation"):
                    continue
                context = source_context(glyph,observations,self.sources)
                if category not in self.by_category or (context and context["kind"] != natural):
                    continue
                height = self.scale_heights[identity]
                if height is not None:
                    self.by_category[category].setdefault(glyph["text"], []).append(height)

    def _reference(self, forms, text, natural, neighbors=None, neighbor_metrics=None):
        # Give each different character one vote; plentiful aliases of one
        # label do not establish the writer's capital/digit scale.
        category = anatomy_context(text,natural)
        reference_category = "ascender" if category=="ascender" or (category=="capital" and text in LATIN_CAPITALS) else category
        selected_neighbors={}
        for item in neighbor_metrics or []:
            if self._comparable_neighbor(text,category,reference_category,item):
                selected_neighbors.setdefault(item["text"],[]).append(item["height_xheight"])
        if selected_neighbors:
            return median(median(values) for values in selected_neighbors.values()), "selected_neighbor_median", len(selected_neighbors)
        pool = {label:heights for label,heights in self.by_category.get(reference_category,{}).items()
                if label != text and (reference_category != "ascender" or label in ASCENDER_REFERENCES)
                and (reference_category != "body_xheight" or label in BODY_REFERENCES)}
        local = [median(heights) for label,heights in pool.items() if label in set(neighbors or [])]
        if len(local) >= 2:
            basis = "neighbor_ascender_median" if reference_category=="ascender" else "neighbor_character_median"
            return median(local), basis, len(local)
        peers = [median(heights) for heights in pool.values()]
        if len(peers) >= 3:
            basis = "ascender_character_median" if reference_category=="ascender" else "peer_character_median"
            return median(peers), basis, len(peers)
        if category=="capital":
            peers = [median(heights) for label,heights in self.by_category["capital"].items() if label != text]
            if len(peers) >= 3:
                return median(peers), "peer_character_median", len(peers)
        heights = [self.scale_heights.get(glyph["glyph_id"]) for glyph in forms]
        heights = [height for height in heights if height is not None]
        if len(heights) >= 3:
            return median(heights), "same_character_median", len(heights)
        return None, "insufficient_comparable_geometry", len(heights)

    def _credible_neighbor(self, item):
        natural="body" if item["anatomy"] in {"body_xheight","ascender"} else item["anatomy"]
        reference,basis,count=self._reference([],item["text"],natural)
        if reference is None or not _strong_reference(item["anatomy"],basis,count):
            return False
        low,high=CLEAR_HEIGHT_RATIO_BOUNDS
        return low*reference <= item["height_xheight"] <= high*reference

    def _comparable_neighbor(self, text, category, expected, item):
        if item["text"]==text:
            # Repeated i/j/f/t/capitals can compare to their own anatomy even
            # though they do not establish the general ascender scale. An
            # independent bank reference must first validate that occurrence.
            return item["anatomy"]==category and self._credible_neighbor(item)
        if expected=="body_xheight" and (text in DOTTED_BODY_LETTERS or item["text"] in DOTTED_BODY_LETTERS):
            return False
        return (item["anatomy"]==expected
                and (expected!="ascender" or item["text"] in ASCENDER_REFERENCES))

    def _verified_neighbor_reference(self, text, natural, neighbor_metrics):
        """Use selected neighbors only when their own bank scale is credible."""
        category=anatomy_context(text,natural)
        expected="ascender" if category=="ascender" or (category=="capital" and text in LATIN_CAPITALS) else category
        values={}
        tokens=[]
        for item in neighbor_metrics or []:
            label=item["text"]
            if not self._comparable_neighbor(text,category,expected,item):
                continue
            if self._credible_neighbor(item):
                values.setdefault(label,[]).append(item["height_xheight"])
                if item.get("token_id") is not None:
                    tokens.append(item["token_id"])
        if values:
            return median(median(heights) for heights in values.values()),len(values),tokens
        return None,0,[]

    def eligible(self, forms, text, role, requested=None, *, neighbors=None, neighbor_metrics=None, local_context=None, size=None):
        natural = natural_context(text,role)
        category = anatomy_context(text,natural)
        requested = requested or natural
        diagnostic = {"version":"inkform-variant-context-1", "requested":requested,
                      "basis":"legacy_unclassified", "candidate_count":len(forms),
                      "eligible_count":len(forms), "excluded":[], "fallback":None,
                      "geometry_action":"selection_only"}
        if local_context is not None:
            diagnostic["local_context"] = dict(local_context)
        if requested not in CONTEXTS or natural is None:
            return forms,diagnostic
        diagnostic["anatomy"] = category
        diagnostic["height_metric"] = "baseline_to_upper_ink" if category in {"capital","body_xheight","ascender"} else "ink_height"
        annotations = {glyph["glyph_id"]:source_context(glyph,self.observations,self.sources) for glyph in forms}
        selected, excluded = [], []
        if requested in SCRIPT_CONTEXTS:
            # Existing script layout applies its semantic size reduction.
            # A source already normalized to body x-height cannot be reduced
            # again safely; only an explicitly local normalization is eligible.
            selected = [g for g in forms if annotations[g["glyph_id"]]
                        and annotations[g["glyph_id"]]["kind"] == requested
                        and annotations[g["glyph_id"]].get("normalization") == "context_xheight"]
            if selected:
                diagnostic["basis"]="reviewed_source_context"
                for glyph in forms:
                    if glyph not in selected:
                        annotation=annotations[glyph["glyph_id"]]
                        excluded.append({"glyph_id":glyph["glyph_id"],"reason":"reviewed_context_preferred",
                                         "source_context":annotation["kind"] if annotation else None})
            else:
                diagnostic["fallback"]="no_reviewed_script_with_supported_normalization; use ordinary normalized forms at explicit script layout size"
        if not selected:
            compatible=[]
            for glyph in forms:
                annotation=annotations[glyph["glyph_id"]]
                if annotation and annotation["kind"] != natural:
                    reason = "script_source_normalization_unsupported" if annotation["kind"]==requested else "reviewed_context_mismatch"
                    excluded.append({"glyph_id":glyph["glyph_id"],"reason":reason,"source_context":annotation["kind"]})
                else:
                    compatible.append(glyph)
            reviewed=[g for g in compatible if annotations[g["glyph_id"]] and annotations[g["glyph_id"]]["kind"]==natural]
            # Reviewed ordinary-context samples and unclassified forms remain
            # candidates together; geometry may rule out clear scale outliers.
            selected=compatible
            if reviewed:
                diagnostic["basis"]="reviewed_source_context"
            reference,basis,count=self._reference(compatible,text,natural,neighbors,neighbor_metrics)
            if reference is not None:
                # A reviewed context identifies where the sample was written;
                # it does not certify that its normalized size matches this
                # bank. Supported letter anatomy also needs the comparison.
                require_height = category in self.by_category
                low,high=PREFERRED_HEIGHT_RATIO_BOUNDS
                acceptable=[g for g in compatible if (annotations[g["glyph_id"]] and not require_height) or (
                    self.scale_heights.get(g["glyph_id"]) is not None and low*reference <= self.scale_heights[g["glyph_id"]] <= high*reference)]
                # A chosen neighbor improves local coherence, but cannot make
                # a clearly miscalibrated source pass an independent bank check.
                clear_reference,clear_basis,clear_count=self._reference(compatible,text,natural)
                if not _strong_reference(category,clear_basis,clear_count):
                    clear_reference,clear_basis,clear_count=reference,basis,count
                clear_supported=require_height and _strong_reference(category,clear_basis,clear_count)
                clear_candidates=compatible
                if clear_supported:
                    low,high=CLEAR_HEIGHT_RATIO_BOUNDS
                    clear_candidates=[g for g in compatible if self.scale_heights.get(g["glyph_id"]) is not None
                                      and low*clear_reference <= self.scale_heights[g["glyph_id"]] <= high*clear_reference]
                    acceptable=[g for g in acceptable if g in clear_candidates]
                neighbor_reference,neighbor_count,neighbor_tokens=self._verified_neighbor_reference(text,natural,neighbor_metrics)
                neighbor_candidates=compatible
                if neighbor_reference is not None:
                    low,high=NEIGHBOR_HEIGHT_RATIO_BOUNDS
                    neighbor_candidates=[g for g in compatible if self.scale_heights.get(g["glyph_id"]) is not None
                                         and low*neighbor_reference <= self.scale_heights[g["glyph_id"]] <= high*neighbor_reference]
                    acceptable=[g for g in acceptable if g in neighbor_candidates]
                diagnostic.update(height_reference_xheight=reference,height_reference_basis=basis,
                                  height_reference_count=count,height_ratio_bounds=list(PREFERRED_HEIGHT_RATIO_BOUNDS))
                if size is not None:
                    diagnostic["height_reference_mm"] = reference*size
                if clear_supported:
                    diagnostic.update(clear_height_ratio_bounds=list(CLEAR_HEIGHT_RATIO_BOUNDS),
                                      clear_height_reference_xheight=clear_reference,
                                      clear_height_reference_basis=clear_basis,
                                      clear_height_reference_count=clear_count)
                if neighbor_reference is not None:
                    diagnostic.update(selected_neighbor_clear_height_reference_xheight=neighbor_reference,
                                      selected_neighbor_clear_height_reference_count=neighbor_count,
                                      selected_neighbor_clear_height_ratio_bounds=list(NEIGHBOR_HEIGHT_RATIO_BOUNDS))
                    if neighbor_tokens:
                        diagnostic["selected_neighbor_token_ids"]=neighbor_tokens
                selected=acceptable
                reason="estimated_height_incompatible"
                if not acceptable:
                    selected=compatible
                    # The preferred band is an estimate, not ground truth.
                    # Marginal differences do not make a bank unusable. Clear
                    # outliers need evidence from distinct comparable letters
                    # or digits, rather than another alias of the same crop.
                    if clear_supported:
                        selected=clear_candidates
                        reason="estimated_height_clearly_incompatible"
                    if neighbor_reference is not None:
                        selected=[g for g in selected if g in neighbor_candidates]
                        if selected:
                            distances={g["glyph_id"]:abs(math.log(self.scale_heights[g["glyph_id"]]/neighbor_reference))
                                       for g in selected}
                            nearest=min(distances.values())
                            selected=[g for g in selected if distances[g["glyph_id"]]<=nearest+1e-7]
                            diagnostic["fallback_preference"]="closest_actual_neighbor_height"
                    if selected:
                        diagnostic["fallback"]="no_candidate_in_preferred_height_range; preserve source forms within the available evidence"
                        diagnostic["warnings"]=[{"code":"estimated_height_outside_preferred_range",
                                                 "glyph_id":g["glyph_id"],
                                                 "height_ratio":self.scale_heights[g["glyph_id"]]/reference
                                                     if self.scale_heights.get(g["glyph_id"]) is not None else None}
                                                for g in selected]
                for glyph in compatible:
                    if glyph not in selected:
                        entry={"glyph_id":glyph["glyph_id"],"reason":reason,
                               "height_xheight":self.scale_heights.get(glyph["glyph_id"])}
                        if not acceptable and glyph in clear_candidates and glyph not in neighbor_candidates:
                            entry["reason"]="estimated_height_clearly_incompatible_with_selected_neighbor"
                        elif not acceptable and glyph in clear_candidates and glyph in neighbor_candidates and neighbor_reference is not None:
                            entry["reason"]="farther_from_selected_neighbor_height"
                        annotation=annotations[glyph["glyph_id"]]
                        if annotation:
                            entry["source_context"]=annotation["kind"]
                        excluded.append(entry)
                if len(selected)!=len(compatible) or diagnostic["basis"]!="reviewed_source_context":
                    diagnostic["basis"]="estimated_geometry_context"
            elif diagnostic["basis"]!="reviewed_source_context":
                diagnostic["fallback"]=diagnostic["fallback"] or "insufficient_comparable_geometry; preserve existing source forms"
        diagnostic["excluded"]=excluded
        diagnostic["eligible_count"]=len(selected)
        if not selected:
            raise InkformError("missing_context_compatible_glyph",
                "No source form has a compatible writing context, normalization and comparable size.",
                {"role":role,"text":text,"requested_context":requested,"excluded":excluded,
                 "context_selection":diagnostic,
                 "needed":"an ordinary source form at a comparable size or a matching script form normalized to context_xheight"})
        return selected,diagnostic
