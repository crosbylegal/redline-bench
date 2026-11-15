"""Top-level redlining operations: replace, delete, insert, comment.

Each op takes a `DocumentView`, an anchor dict (resolved via `anchoring.resolve`),
and metadata (author, comment text, reply target). Each returns an `OpResult`
that the runtime hands back to the LLM — successes contain the assigned IDs;
failures contain a structured reason.

All edits are mutations to `view._docx` in place. Callers save the document by
calling `view._docx.save(path)` after applying a batch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

from docx_revisions import RevisionParagraph

from .anchoring import AnchorError, ResolvedAnchor, resolve, raw_span
from .comments import add_top_level_comment, add_comment_reply
from .document import DocumentView


@dataclass
class OpResult:
    op: str
    status: Literal["ok", "anchor_failed", "runtime_error"]
    paragraph_id: str | None = None
    comment_id: int | None = None
    revision_ids: list[int] = field(default_factory=list)
    error: str | None = None
    candidates: list[str] | None = None  # for ambiguous-anchor failures


# Default redline author — surfaces in Word's Review pane.
DEFAULT_AUTHOR = "Crosby AI (AgentCo Legal)"


def replace_text(
    view: DocumentView,
    anchor: dict,
    new_text: str,
    comment: str,
    author: str = DEFAULT_AUTHOR,
) -> OpResult:
    """Track-change replace `anchor.text` with `new_text` and add a comment."""
    resolved = resolve(view, anchor)
    if isinstance(resolved, AnchorError):
        return OpResult(
            op="replace",
            status="anchor_failed",
            error=f"{resolved.kind}: {resolved.message}",
            candidates=resolved.candidates,
        )

    para = view.runtime_paragraph(resolved.paragraph_id)
    try:
        rp = RevisionParagraph.from_paragraph(para)
        raw_start, raw_end = raw_span(rp.text, resolved.start, resolved.end)
        rp.replace_tracked_at(
            start=raw_start,
            end=raw_end,
            replace_text=new_text,
            author=author,
            index_mode="text",
        )
    except Exception as exc:  # noqa: BLE001 — convert to structured error
        return OpResult(
            op="replace",
            status="runtime_error",
            paragraph_id=resolved.paragraph_id,
            error=f"replace_tracked_at: {exc}",
        )

    comment_id = _attach_comment(view, para, comment, author)
    return OpResult(
        op="replace",
        status="ok",
        paragraph_id=resolved.paragraph_id,
        comment_id=comment_id,
    )


def delete_text(
    view: DocumentView,
    anchor: dict,
    comment: str,
    author: str = DEFAULT_AUTHOR,
) -> OpResult:
    """Track-change delete `anchor.text` and add a comment."""
    resolved = resolve(view, anchor)
    if isinstance(resolved, AnchorError):
        return OpResult(
            op="delete",
            status="anchor_failed",
            error=f"{resolved.kind}: {resolved.message}",
            candidates=resolved.candidates,
        )
    para = view.runtime_paragraph(resolved.paragraph_id)
    try:
        rp = RevisionParagraph.from_paragraph(para)
        raw_start, raw_end = raw_span(rp.text, resolved.start, resolved.end)
        rp.add_tracked_deletion(
            start=raw_start,
            end=raw_end,
            author=author,
            index_mode="text",
        )
    except Exception as exc:  # noqa: BLE001
        return OpResult(
            op="delete",
            status="runtime_error",
            paragraph_id=resolved.paragraph_id,
            error=f"add_tracked_deletion: {exc}",
        )
    comment_id = _attach_comment(view, para, comment, author)
    return OpResult(
        op="delete",
        status="ok",
        paragraph_id=resolved.paragraph_id,
        comment_id=comment_id,
    )


def insert_after_text(
    view: DocumentView,
    anchor: dict,
    new_text: str,
    comment: str,
    author: str = DEFAULT_AUTHOR,
) -> OpResult:
    """Track-change insert `new_text` immediately after the resolved anchor span.

    Implementation: docx-revisions doesn't expose an "insert at position" API
    cleanly (zero-width replacement rejects, append-only appends to end of
    paragraph). We achieve the right end state with a `replace` whose
    `replace_text` is `anchor_text + new_text` — the tracked diff visibly
    includes the anchor text in both the deletion and insertion, which is
    slightly noisier than ideal. Acceptable for V1; flagged for future
    refinement via lxml run-splitting.
    """
    resolved = resolve(view, anchor)
    if isinstance(resolved, AnchorError):
        return OpResult(
            op="insert_after",
            status="anchor_failed",
            error=f"{resolved.kind}: {resolved.message}",
            candidates=resolved.candidates,
        )
    para = view.runtime_paragraph(resolved.paragraph_id)
    try:
        rp = RevisionParagraph.from_paragraph(para)
        raw_start, raw_end = raw_span(rp.text, resolved.start, resolved.end)
        rp.replace_tracked_at(
            start=raw_start,
            end=raw_end,
            replace_text=rp.text[raw_start:raw_end] + new_text,
            author=author,
            index_mode="text",
        )
    except Exception as exc:  # noqa: BLE001
        return OpResult(
            op="insert_after",
            status="runtime_error",
            paragraph_id=resolved.paragraph_id,
            error=f"replace_tracked_at: {exc}",
        )
    comment_id = _attach_comment(view, para, comment, author)
    return OpResult(
        op="insert_after",
        status="ok",
        paragraph_id=resolved.paragraph_id,
        comment_id=comment_id,
    )


def add_standalone_comment(
    view: DocumentView,
    paragraph_id: str,
    comment: str,
    author: str = DEFAULT_AUTHOR,
    reply_to_comment_id: Optional[int] = None,
) -> OpResult:
    """Add a comment without an accompanying tracked change.

    Anchors on the whole paragraph. If `reply_to_comment_id` is set, this
    becomes a reply on the existing thread; otherwise it starts a new thread.
    """
    para = view.runtime_paragraph(paragraph_id)
    if para is None:
        return OpResult(
            op="comment",
            status="anchor_failed",
            error=f"paragraph_not_found: no paragraph with id={paragraph_id!r}",
        )
    if reply_to_comment_id is not None:
        try:
            cid = add_comment_reply(view, reply_to_comment_id, comment, author)
        except Exception as exc:  # noqa: BLE001
            return OpResult(
                op="reply",
                status="runtime_error",
                paragraph_id=paragraph_id,
                error=f"add_comment_reply: {exc}",
            )
        return OpResult(
            op="reply",
            status="ok",
            paragraph_id=paragraph_id,
            comment_id=cid,
        )
    try:
        cid = add_top_level_comment(view, para, comment, author)
    except Exception as exc:  # noqa: BLE001
        return OpResult(
            op="comment",
            status="runtime_error",
            paragraph_id=paragraph_id,
            error=f"add_top_level_comment: {exc}",
        )
    return OpResult(
        op="comment",
        status="ok",
        paragraph_id=paragraph_id,
        comment_id=cid,
    )


def _attach_comment(
    view: DocumentView,
    para,
    comment_text: str,
    author: str,
) -> int | None:
    """Helper: add a top-level comment to `para` anchored on its whole text."""
    if not comment_text:
        return None
    try:
        return add_top_level_comment(view, para, comment_text, author)
    except Exception:
        # Comments are non-load-bearing — silently degrade rather than fail
        # the whole tracked change. The caller still gets `status="ok"` and a
        # comment_id of None makes the absence visible.
        return None
