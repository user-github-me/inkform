"""Correction input preserves the final text while accounting for deleted ink."""
import copy
from collections import Counter

import pytest

from inkform.document import literal_document, plain_text, requirements, validate_document, visible_requirements
from inkform.errors import InkformError


def corrected_document():
    document = literal_document("The answer is ")
    document["blocks"][0]["children"].extend([
        {"type": "correction", "id": "edit", "deleted": "nine", "text": "ten"},
        {"type": "text", "id": "ending", "text": "."},
    ])
    return document


def test_final_content_and_visible_deleted_text_are_distinct():
    document = corrected_document()
    original = copy.deepcopy(document)
    validate_document(document)
    assert plain_text(document) == "The answer is ten."
    extra = Counter(visible_requirements(document)) - Counter(requirements(document))
    assert extra == Counter(("text", c) for c in "nine")
    assert document == original


def test_explicit_deletion_and_auto_corrections_off_preserve_source_edit():
    document = corrected_document()
    document["handwriting"] = {"corrections": "none"}
    document["blocks"][0]["children"][1]["text"] = ""
    validate_document(document)
    assert plain_text(document) == "The answer is ."
    assert Counter(visible_requirements(document)) - Counter(requirements(document)) == Counter(("text", c) for c in "nine")


@pytest.mark.parametrize("value", [None, [], True, {"corrections": []}, {"corrections": "random"}, {"correction": "none"}])
def test_invalid_correction_policy_is_rejected(value):
    document = corrected_document()
    document["handwriting"] = value
    with pytest.raises(InkformError) as error:
        validate_document(document)
    assert error.value.code == "invalid_document"


@pytest.mark.parametrize("field,value", [("deleted", ""), ("deleted", "   "), ("deleted", "line\nbreak"),
                                          ("text", "line\rbreak"), ("text", "tab\there"), ("text", "line\u2028break"),
                                          ("deleted", []), ("text", None)])
def test_invalid_or_multiline_correction_is_rejected(field, value):
    document = corrected_document()
    document["blocks"][0]["children"][1][field] = value
    with pytest.raises(InkformError):
        validate_document(document)
