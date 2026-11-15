"""Regression: anchors resolve in normalized text, edits must land in the stored text.

A paragraph with doubled spaces before the anchor used to shift every tracked
change one character left per collapsed whitespace character.
"""

import sys
from pathlib import Path

import pytest

pytest.importorskip("docx")
pytest.importorskip("docx_revisions")

from docx import Document  # noqa: E402
from docx_revisions import RevisionParagraph  # noqa: E402

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "contract-redliner" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from redline_engine.anchoring import normalize, raw_span  # noqa: E402
from redline_engine.document import DocumentView  # noqa: E402
from redline_engine.ops import delete_text, insert_after_text, replace_text  # noqa: E402

TEXT = "Test clause.  Fees are due.  Customer shall be paid within ninety (90) days of invoice."


def _apply(tmp_path, op, **kwargs):
    path = tmp_path / "contract.docx"
    doc = Document()
    doc.add_paragraph(TEXT)
    doc.save(path)
    view = DocumentView.load(path)
    result = op(view, comment="test", author="Tester", **kwargs)
    assert result.status == "ok", result
    view._docx.save(str(path))  # noqa: SLF001 - same save call as propose_edits.py
    para = Document(path).paragraphs[0]
    return RevisionParagraph.from_paragraph(para).accepted_text


@pytest.mark.parametrize(
    "raw, needle",
    [
        (TEXT, "ninety (90) days"),
        ("  Leading.  Two  gaps here.", "Two gaps"),
        ("No extra whitespace at all.", "whitespace"),
        ("Curly “quotes”  and  spaces.", '"quotes" and'),
    ],
)
def test_raw_span_round_trips(raw, needle):
    hay = normalize(raw)
    start = hay.index(needle)
    raw_start, raw_end = raw_span(raw, start, start + len(needle))
    assert normalize(raw[raw_start:raw_end]) == needle


def test_replace_after_double_spaces(tmp_path):
    accepted = _apply(tmp_path, replace_text, anchor={"text": "ninety (90) days"}, new_text="sixty (60) days")
    assert accepted == TEXT.replace("ninety (90) days", "sixty (60) days")


def test_delete_after_double_spaces(tmp_path):
    accepted = _apply(tmp_path, delete_text, anchor={"text": "ninety (90) days"})
    assert accepted == TEXT.replace("ninety (90) days", "")


def test_insert_after_double_spaces(tmp_path):
    accepted = _apply(tmp_path, insert_after_text, anchor={"text": "days of invoice."}, new_text=" Late fees apply.")
    assert accepted == TEXT + " Late fees apply."


def test_insert_after_keeps_stored_anchor_text(tmp_path):
    # The anchor spans a doubled space; the re-inserted anchor must keep it.
    accepted = _apply(tmp_path, insert_after_text, anchor={"text": "Fees are due. Customer"}, new_text=" (the payee)")
    assert accepted == TEXT.replace("Customer", "Customer (the payee)")
