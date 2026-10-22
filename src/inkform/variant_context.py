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


class VariantContext:
    def __init__(self, forms, observations, sources):
        self.observations = observations
        self.sources = {source["source_id"]:source for source in sources}
        self.heights = {}
        self.by_category = {"capital": {}, "baseline_digit": {}}
        seen = set()
        for candidates in forms.values():
            for glyph in candidates:
                identity = glyph["glyph_id"]
                if identity in seen:
                    continue
                seen.add(identity)
                self.heights[identity] = _height(glyph)
                if glyph.get("derivation"):
                    continue
                category = natural_context(glyph["text"],glyph["role"])
                context = source_context(glyph,observations,self.sources)
                if category not in self.by_category or (context and context["kind"] != category):
                    continue
                height = self.heights[identity]
                if height is not None:
                    self.by_category[category].setdefault(glyph["text"], []).append(height)

    def _reference(self, forms, text, natural):
        # Give each different character one vote; plentiful aliases of one
        # label do not establish the writer's capital/digit scale.
        peers = [median(heights) for label,heights in self.by_category.get(natural,{}).items() if label != text]
        if len(peers) >= 3:
            return median(peers), "peer_character_median", len(peers)
        heights = [self.heights.get(glyph["glyph_id"]) for glyph in forms]
        heights = [height for height in heights if height is not None]
        if len(heights) >= 3:
            return median(heights), "same_character_median", len(heights)
        return None, "insufficient_comparable_geometry", len(heights)

    def eligible(self, forms, text, role, requested=None):
        natural = natural_context(text,role)
        requested = requested or natural
        diagnostic = {"version":"inkform-variant-context-1", "requested":requested,
                      "basis":"legacy_unclassified", "candidate_count":len(forms),
                      "eligible_count":len(forms), "excluded":[], "fallback":None,
                      "geometry_action":"selection_only"}
        if requested not in CONTEXTS or natural is None:
            return forms,diagnostic
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
            reference,basis,count=self._reference(compatible,text,natural)
            if reference is not None:
                acceptable=[g for g in compatible if annotations[g["glyph_id"]] or (
                    self.heights.get(g["glyph_id"]) is not None and .72*reference <= self.heights[g["glyph_id"]] <= 1.5*reference)]
                diagnostic.update(height_reference_xheight=reference,height_reference_basis=basis,
                                  height_reference_count=count,height_ratio_bounds=[.72,1.5])
                if acceptable:
                    for glyph in compatible:
                        if glyph not in acceptable:
                            excluded.append({"glyph_id":glyph["glyph_id"],"reason":"estimated_height_incompatible",
                                             "height_xheight":self.heights.get(glyph["glyph_id"])})
                    selected=acceptable
                    if len(acceptable)!=len(compatible) or diagnostic["basis"]!="reviewed_source_context":
                        diagnostic["basis"]="estimated_geometry_context"
                else:
                    diagnostic["fallback"]="no_comparable_height_candidate; preserve existing source forms"
            elif diagnostic["basis"]!="reviewed_source_context":
                diagnostic["fallback"]=diagnostic["fallback"] or "insufficient_comparable_geometry; preserve existing source forms"
        if not selected:
            raise InkformError("missing_context_compatible_glyph",
                "The available reviewed forms belong to another writing context or need an unsupported script normalization.",
                {"role":role,"text":text,"requested_context":requested,"excluded":excluded,
                 "needed":"a reviewed ordinary form or a matching script form normalized to context_xheight"})
        diagnostic["excluded"]=excluded
        diagnostic["eligible_count"]=len(selected)
        return selected,diagnostic
