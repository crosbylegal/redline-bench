"""Resolve LLM-supplied anchors to (paragraph_id, char_start, char_end) positions.

The LLM's anchor schema is::

    {
      "paragraph_id": "p-027",        # required when we can; primary key
      "text": "non-exclusive, royalty-free",  # exact text to find inside that paragraph
      "context_before": "...",        # optional, used to disambiguate when text appears twice
      "context_after": "...",         # optional, same purpose
      "occurrence": 1                 # optional, 1-indexed, defaults to 1
    }

We normalize before matching (NFC, smart-quote folding, dash folding, whitespace
collapse). Failures return a structured `AnchorError` so the runtime can report
them back to the LLM for retry rather than silently misapplying.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Optional

from .document import DocumentView


# Smart quote / dash / whitespace folding. LLMs love re-typing curly quotes
# slightly differently than the source; this is the most common false-mismatch.
_QUOTE_MAP = str.maketrans(
    {
        "‘": "'",  # left single quote
        "’": "'",  # right single quote
        "‚": "'",  # single low-9 quote
        "‛": "'",  # single high-reversed-9 quote
        "“": '"',  # left double quote
        "”": '"',  # right double quote
        "„": '"',  # double low-9 quote
        "‟": '"',  # double high-reversed-9 quote
        "–": "-",  # en dash
        "—": "-",  # em dash
        "−": "-",  # minus sign
        " ": " ",  # non-breaking space
    }
)

_WHITESPACE_RE = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Normalize text for robust matching. Idempotent."""
    if not text:
        return ""
    text = unicodedata.normalize("NFC", text)
    text = text.translate(_QUOTE_MAP)
    text = _WHITESPACE_RE.sub(" ", text)
    return text.strip()


def raw_span(raw_text: str, start: int, end: int) -> tuple[int, int]:
    """Map a [start, end) span of `normalize(raw_text)` back to offsets in `raw_text`."""
    offsets: list[int] = []
    prev_ws = True  # leading whitespace is stripped by normalize()
    for i, ch in enumerate(raw_text):
        if _WHITESPACE_RE.fullmatch(ch.translate(_QUOTE_MAP)):
            if not prev_ws:
                offsets.append(i)
            prev_ws = True
        else:
            offsets.append(i)
            prev_ws = False
    if prev_ws and offsets and _WHITESPACE_RE.fullmatch(raw_text[offsets[-1]].translate(_QUOTE_MAP)):
        offsets.pop()  # trailing whitespace is stripped by normalize()
    if len(offsets) != len(normalize(raw_text)):
        raise ValueError("cannot map normalized offsets back to the paragraph text")
    return offsets[start], offsets[end - 1] + 1


@dataclass
class ResolvedAnchor:
    paragraph_id: str
    normalized_text: str
    # Index into the *normalized* paragraph text. The caller will need to map
    # this back to the underlying runs when applying edits — see ops.py.
    start: int
    end: int


@dataclass
class AnchorError:
    """Structured failure that gets handed back to the LLM."""

    kind: str  # "paragraph_not_found" | "text_not_found" | "ambiguous" | "no_paragraph_id"
    message: str
    # For ambiguous matches, list the paragraph IDs that contained the text.
    candidates: list[str] | None = None


def resolve(view: DocumentView, anchor: dict) -> ResolvedAnchor | AnchorError:
    """Resolve an LLM anchor dict to a `ResolvedAnchor` or `AnchorError`."""
    paragraph_id: Optional[str] = anchor.get("paragraph_id")
    text: Optional[str] = anchor.get("text")
    # `occurrence` is explicit (None = let resolver decide; only works when unique).
    occurrence_raw = anchor.get("occurrence")
    occurrence: int | None = int(occurrence_raw) if occurrence_raw is not None else None
    context_before: str = normalize(anchor.get("context_before", "") or "")
    context_after: str = normalize(anchor.get("context_after", "") or "")

    if not text:
        return AnchorError("text_not_found", "anchor.text is required and must be non-empty")

    needle = normalize(text)
    if not needle:
        return AnchorError("text_not_found", "anchor.text normalizes to empty")

    # If paragraph_id is supplied, restrict the search to that paragraph.
    if paragraph_id:
        para = view.get(paragraph_id)
        if para is None:
            return AnchorError(
                "paragraph_not_found",
                f"no paragraph with id={paragraph_id!r}",
            )
        return _resolve_within_paragraph(
            paragraph_id, para.text, needle, context_before, context_after, occurrence
        )

    # No paragraph_id — search the whole doc.
    candidates: list[tuple[str, int, int]] = []
    for p in view.paragraphs:
        if p.is_empty:
            continue
        hay = normalize(p.text)
        for m in re.finditer(re.escape(needle), hay):
            if _context_matches(hay, m.start(), m.end(), context_before, context_after):
                candidates.append((p.id, m.start(), m.end()))

    if not candidates:
        return AnchorError(
            "text_not_found",
            f"text {text!r} not found in any paragraph (after normalization)",
        )
    if len(candidates) > 1:
        if occurrence is None or not (1 <= occurrence <= len(candidates)):
            return AnchorError(
                "ambiguous",
                f"text {text!r} appears {len(candidates)} times; "
                "provide paragraph_id, context_before/after, or occurrence",
                candidates=[c[0] for c in candidates],
            )
        pid, start, end = candidates[occurrence - 1]
    else:
        pid, start, end = candidates[0]
    return ResolvedAnchor(paragraph_id=pid, normalized_text=needle, start=start, end=end)


def _resolve_within_paragraph(
    paragraph_id: str,
    raw_text: str,
    needle: str,
    context_before: str,
    context_after: str,
    occurrence: int | None,
) -> ResolvedAnchor | AnchorError:
    hay = normalize(raw_text)
    matches = []
    for m in re.finditer(re.escape(needle), hay):
        if _context_matches(hay, m.start(), m.end(), context_before, context_after):
            matches.append((m.start(), m.end()))
    if not matches:
        return AnchorError(
            "text_not_found",
            f"text {needle!r} not found in paragraph {paragraph_id}",
        )
    if len(matches) > 1:
        if occurrence is None or not (1 <= occurrence <= len(matches)):
            return AnchorError(
                "ambiguous",
                f"text {needle!r} appears {len(matches)} times in {paragraph_id}; "
                "supply occurrence",
                candidates=[paragraph_id],
            )
        start, end = matches[occurrence - 1]
    else:
        start, end = matches[0]
    return ResolvedAnchor(
        paragraph_id=paragraph_id,
        normalized_text=needle,
        start=start,
        end=end,
    )


def _context_matches(hay: str, start: int, end: int, before: str, after: str) -> bool:
    """Both context fields are optional; absent → always matches.

    Boundaries are whitespace-trimmed before comparing: `normalize()` strips
    the context strings, so a context like "at least " must still match even
    though the paragraph has a space between it and the anchored text.
    """
    if before:
        # The chars immediately before `start` should end with `before`.
        if not hay[:start].rstrip().endswith(before):
            return False
    if after:
        if not hay[end:].lstrip().startswith(after):
            return False
    return True
