"""LLM-facing view of a .docx contract.

A `DocumentView` is a frozen snapshot of a document for the LLM to reason
about: it carries paragraph IDs the model can cite, a Markdown serialization
that fits in a prompt, and enumerations of any pre-existing comments and
tracked changes (so the model can reply on threads and accept prior edits).

The view is built fresh each time the document is loaded; paragraph IDs are
assigned by load-order (`p-000`, `p-001`, ...) and are stable for the duration
of one redlining session. After we apply edits and save, reloading produces a
new view — the LLM must re-read in that case.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from docx import Document
from docx.document import Document as DocxDocument
from docx.text.paragraph import Paragraph
from lxml import etree

from .ooxml_utils import NSMAP, qn


_PARAGRAPH_ID_FMT = "p-{:03d}"


@dataclass
class ParagraphInfo:
    """A paragraph in the document, with the metadata the LLM and engine need."""

    id: str
    index: int
    text: str
    style: str
    is_empty: bool
    has_revisions: bool
    has_comments: bool
    # Word's auto-numbering is driven by w:numPr; we record the abstract
    # level (0 = top-level) when present so the serializer can indent.
    list_level: int | None = None


@dataclass
class ExistingComment:
    """A pre-existing comment in the document the LLM may need to reply to."""

    id: int
    author: str
    initials: str
    text: str
    paragraph_ids: list[str] = field(default_factory=list)
    parent_id: int | None = None  # if this comment is itself a reply


@dataclass
class ExistingRevision:
    """A pre-existing tracked change the LLM may need to accept/reject."""

    id: int
    type: str  # "ins" | "del" | "moveFrom" | "moveTo"
    author: str
    text: str
    paragraph_ids: list[str] = field(default_factory=list)


@dataclass
class DocumentView:
    docx_path: Path
    paragraphs: list[ParagraphInfo]
    comments: list[ExistingComment]
    revisions: list[ExistingRevision]
    # Map paragraph ID → underlying python-docx Paragraph object (not serialized
    # for the LLM; used internally when applying edits).
    _paragraph_objects: dict[str, Paragraph] = field(default_factory=dict, repr=False)
    _docx: DocxDocument | None = field(default=None, repr=False)

    @classmethod
    def load(cls, path: str | Path) -> "DocumentView":
        path = Path(path)
        doc = Document(str(path))
        paragraphs: list[ParagraphInfo] = []
        paragraph_objects: dict[str, Paragraph] = {}

        for i, p in enumerate(_iter_all_paragraphs(doc)):
            pid = _PARAGRAPH_ID_FMT.format(i)
            info = ParagraphInfo(
                id=pid,
                index=i,
                text=p.text,
                style=p.style.name if p.style else "",
                is_empty=not p.text.strip(),
                has_revisions=_paragraph_has_revisions(p)
                or _row_revision(p) is not None,
                has_comments=_paragraph_has_comments(p),
                list_level=_paragraph_list_level(p),
            )
            paragraphs.append(info)
            paragraph_objects[pid] = p

        comments = _enumerate_comments(doc, paragraphs, paragraph_objects)
        revisions = _enumerate_revisions(doc, paragraphs, paragraph_objects)

        return cls(
            docx_path=path,
            paragraphs=paragraphs,
            comments=comments,
            revisions=revisions,
            _paragraph_objects=paragraph_objects,
            _docx=doc,
        )

    def get(self, paragraph_id: str) -> ParagraphInfo | None:
        for p in self.paragraphs:
            if p.id == paragraph_id:
                return p
        return None

    def runtime_paragraph(self, paragraph_id: str) -> Paragraph | None:
        """Return the underlying python-docx `Paragraph` object for the given id."""
        return self._paragraph_objects.get(paragraph_id)

    def to_markdown(self, max_chars: int | None = None) -> str:
        """Serialize the document into an LLM-friendly Markdown view.

        Blank paragraphs are omitted from the visible output but their IDs are
        preserved (skipped) so paragraph IDs match the underlying document.

        Paragraphs carrying tracked changes show them inline: inserted text as
        `++inserted++` and deleted text as `~~deleted~~`, so the body reads as
        the running document. A paragraph whose only text is a pending
        insertion or deletion is listed too.

        If `max_chars` is set, truncates with a sentinel — useful for prompt
        budgeting. The LargeCo template is ~90 KB; most current frontier models
        comfortably fit the whole thing, but smaller models or budget runs may
        need windowing.
        """
        lines: list[str] = [f"# {self.docx_path.name}", ""]
        for p in self.paragraphs:
            text = self._body_text(p)
            if not text.strip():
                continue
            flags = []
            if p.has_revisions:
                flags.append("REVS")
            if p.has_comments:
                flags.append("CMTS")
            flag_str = f" {{{','.join(flags)}}}" if flags else ""
            indent = "  " * (p.list_level or 0)
            lines.append(f"[{p.id}] ({p.style}){flag_str} {indent}{text}")

        if self.comments:
            lines.append("")
            lines.append("## Existing comments")
            for c in self.comments:
                head = f"[cmt-{c.id}] {c.author!r}"
                if c.parent_id is not None:
                    head += f" (reply to cmt-{c.parent_id})"
                spans = ",".join(c.paragraph_ids) or "?"
                lines.append(f"{head} on [{spans}]: {c.text}")

        if self.revisions:
            lines.append("")
            lines.append("## Existing tracked changes")
            for r in self.revisions:
                spans = ",".join(r.paragraph_ids) or "?"
                lines.append(
                    f"[rev-{r.id}] {r.type} by {r.author!r} on [{spans}]: {r.text!r}"
                )

        out = "\n".join(lines)
        if max_chars is not None and len(out) > max_chars:
            out = out[: max_chars - 100] + "\n\n[... document truncated ...]"
        return out

    def _body_text(self, p: ParagraphInfo) -> str:
        """Text `to_markdown` lists for `p`, with tracked changes inline."""
        if p.has_revisions:
            p_obj = self._paragraph_objects[p.id]
            row_mark = _row_revision(p_obj)
            if row_mark is not None and not _paragraph_has_revisions(p_obj):
                deleted = etree.QName(row_mark).localname == "del"
                text = _row_paragraph_text(p_obj._p, deleted)
                if not text:
                    return ""
                marker = "~~" if deleted else "++"
                return f"{marker}{text}{marker}"
            return _render_with_revisions(p_obj._p)
        return p.text

    def stats(self) -> dict[str, int]:
        return {
            "paragraphs": len(self.paragraphs),
            "non_empty_paragraphs": sum(
                1 for p in self.paragraphs if self._body_text(p).strip()
            ),
            "existing_comments": len(self.comments),
            "existing_revisions": len(self.revisions),
        }


# --- Paragraph metadata helpers -------------------------------------------- #


def _iter_all_paragraphs(doc: DocxDocument) -> Iterable[Paragraph]:
    """Yield body paragraphs in document order, including table-cell paragraphs.

    `doc.paragraphs` returns only the direct children of the body, so text and
    tracked changes inside `<w:tbl>` (SLA tables, credit tables, signature
    grids) never reach the reader, the anchoring code or the judge view.
    Paragraphs nested inside another paragraph (text boxes) are skipped so
    their text is not emitted twice.
    """
    p_tag = qn("w:p")
    for p_el in doc.element.body.iter(p_tag):
        if any(a.tag == p_tag for a in p_el.iterancestors()):
            continue
        yield Paragraph(p_el, doc._body)  # noqa: SLF001


def _paragraph_has_revisions(p: Paragraph) -> bool:
    for tag in ("w:ins", "w:del", "w:moveFrom", "w:moveTo"):
        if next(p._p.iter(qn(tag)), None) is not None:
            return True
    return False


def _row_revision(p: Paragraph) -> etree._Element | None:
    """Return the `w:ins` or `w:del` mark of a table row that contains `p`.

    Word records an inserted or deleted table row in the row properties
    (`w:tr/w:trPr/w:ins` or `w:trPr/w:del`). The text in that row need not
    carry a run-level mark of its own, so without this check a deleted row
    would be listed as live text. A paragraph in such a row that has no
    run-level mark is shown wrapped in full; one that has run-level marks is
    rendered from them, as in any other paragraph. Rows of enclosing tables
    are checked too.
    """
    for anc in p._p.iterancestors(qn("w:tr")):
        tr_pr = anc.find(qn("w:trPr"))
        if tr_pr is None:
            continue
        for tag in ("w:del", "w:ins"):
            mark = tr_pr.find(qn(tag))
            if mark is not None:
                return mark
    return None


def _row_paragraph_text(p_el: etree._Element, deleted: bool) -> str:
    """Text of a paragraph in an inserted or deleted row.

    For a deleted row, all text (including `w:delText`) is returned. For an
    inserted row, text inside run-level deletions is left out.
    """
    parts: list[str] = []
    skip = (qn("w:del"), qn("w:moveFrom"))
    for r in p_el.iter(qn("w:r")):
        if not deleted and any(a.tag in skip for a in r.iterancestors()):
            continue
        parts.append(_run_text(r))
    return "".join(parts)


def _render_with_revisions(el: etree._Element) -> str:
    """Return the text of `el` with tracked changes marked inline.

    Covers the same content as `Paragraph.text` (runs and hyperlinks) and adds
    the runs inside `w:ins`/`w:moveTo` as `++text++` and inside
    `w:del`/`w:moveFrom` as `~~text~~`. Adjacent changes of the same kind are
    shown as one span. Nested revisions (a deletion of another author's
    insertion) are rendered recursively.
    """
    segments: list[list[str]] = []  # [marker, text]; marker "" for plain text

    def add(marker: str, text: str) -> None:
        if not text:
            return
        if segments and segments[-1][0] == marker:
            segments[-1][1] += text
        else:
            segments.append([marker, text])

    for child in el.iterchildren():
        tag = child.tag
        if tag == qn("w:r"):
            add("", _run_text(child))
        elif tag == qn("w:hyperlink"):
            add("", _render_with_revisions(child))
        elif tag in (qn("w:ins"), qn("w:moveTo")):
            add("++", _render_with_revisions(child))
        elif tag in (qn("w:del"), qn("w:moveFrom")):
            add("~~", _render_with_revisions(child))
    return "".join(f"{m}{t}{m}" for m, t in segments)


def _run_text(r: etree._Element) -> str:
    """Text of one `w:r`, as python-docx renders it, plus any `w:delText`."""
    parts: list[str] = []
    for e in r.iterchildren():
        if e.tag in (qn("w:t"), qn("w:delText")):
            parts.append(e.text or "")
        elif e.tag in (
            qn("w:br"),
            qn("w:cr"),
            qn("w:noBreakHyphen"),
            qn("w:ptab"),
            qn("w:tab"),
        ):
            parts.append(str(e))
    return "".join(parts)


def _paragraph_has_comments(p: Paragraph) -> bool:
    for tag in ("w:commentRangeStart", "w:commentRangeEnd", "w:commentReference"):
        if next(p._p.iter(qn(tag)), None) is not None:
            return True
    return False


def _paragraph_list_level(p: Paragraph) -> int | None:
    """Return the list nesting level if the paragraph is part of a numbered/bulleted list."""
    numPr = p._p.find(f"{qn('w:pPr')}/{qn('w:numPr')}")
    if numPr is None:
        return None
    ilvl = numPr.find(qn("w:ilvl"))
    if ilvl is None:
        return 0
    val = ilvl.get(qn("w:val"))
    try:
        return int(val) if val is not None else 0
    except ValueError:
        return 0


# --- Comment & revision enumeration ---------------------------------------- #


def _paragraph_ids_for_element_range(
    start_id_attr: str,
    end_id_attr: str,
    el_id: int,
    paragraphs: list[ParagraphInfo],
    paragraph_objects: dict[str, Paragraph],
) -> list[str]:
    """Find paragraph IDs that the given range marker pair spans.

    Walks paragraphs in document order looking for `w:commentRangeStart`/`End`
    (or analogous markers) with matching IDs. Returns paragraph IDs from start
    to end inclusive.
    """
    started = False
    spans: list[str] = []
    target = str(el_id)
    for pi in paragraphs:
        p = paragraph_objects[pi.id]
        # Did this paragraph contain start?
        for el in p._p.iter(qn(start_id_attr)):
            if el.get(qn("w:id")) == target:
                started = True
                break
        if started:
            spans.append(pi.id)
        # Did this paragraph contain end?
        for el in p._p.iter(qn(end_id_attr)):
            if el.get(qn("w:id")) == target:
                return spans
    return spans


def _enumerate_comments(
    doc: DocxDocument,
    paragraphs: list[ParagraphInfo],
    paragraph_objects: dict[str, Paragraph],
) -> list[ExistingComment]:
    out: list[ExistingComment] = []
    # python-docx exposes `Document.comments` (CommentCollection) in 1.2.
    try:
        collection = doc.comments
    except AttributeError:
        return out

    # Build a paraId → parent_id lookup from commentsExtended.xml so we can
    # mark replies. The commentsExtended part is optional; absence just means
    # no thread metadata available.
    paraid_parent = _read_comments_extended(doc)

    for c in collection:
        cid = int(c._element.get(qn("w:id"), -1))
        if cid < 0:
            continue
        spans = _paragraph_ids_for_element_range(
            "w:commentRangeStart",
            "w:commentRangeEnd",
            cid,
            paragraphs,
            paragraph_objects,
        )
        # Text — join all w:t descendants of the w:comment element.
        text = "".join(t.text or "" for t in c._element.iter(qn("w:t")))
        # Determine if this is a reply: look up our first paragraph's paraId.
        parent_id = None
        first_para = c._element.find(qn("w:p"))
        if first_para is not None:
            pid_attr = first_para.get(qn("w14:paraId"))
            if pid_attr and pid_attr in paraid_parent:
                parent_id = paraid_parent[pid_attr]

        out.append(
            ExistingComment(
                id=cid,
                author=c._element.get(qn("w:author"), ""),
                initials=c._element.get(qn("w:initials"), ""),
                text=text,
                paragraph_ids=spans,
                parent_id=parent_id,
            )
        )
    return out


def _read_comments_extended(doc: DocxDocument) -> dict[str, int]:
    """Return a paraId → parent_comment_id map from commentsExtended.xml.

    Word stores comment-thread parentage by paraId (a w14 attribute on the
    paragraph inside the comment body) rather than by comment IDs directly.
    We resolve that mapping here.
    """
    out: dict[str, int] = {}
    package_part = doc.part.package
    # Walk for any part whose URI ends with /word/commentsExtended.xml
    for rel in doc.part.rels.values():
        if rel.target_ref.endswith("commentsExtended.xml"):
            blob = rel.target_part.blob
            root = etree.fromstring(blob)
            # Build paraId → parent_paraId from <w15:commentEx>
            paraid_to_parent = {}
            for ex in root.iter(qn("w15:commentEx")):
                pid = ex.get(qn("w15:paraId"))
                ppid = ex.get(qn("w15:paraIdParent"))
                if pid:
                    paraid_to_parent[pid] = ppid
            if not paraid_to_parent:
                return out
            # Now we need to resolve parent paraId → comment id by walking
            # comments.xml.
            paraid_to_commentid = {}
            for crel in doc.part.rels.values():
                if crel.target_ref.endswith("comments.xml"):
                    cblob = crel.target_part.blob
                    croot = etree.fromstring(cblob)
                    for c in croot.iter(qn("w:comment")):
                        first_p = c.find(qn("w:p"))
                        if first_p is None:
                            continue
                        pid = first_p.get(qn("w14:paraId"))
                        cid = c.get(qn("w:id"))
                        if pid and cid is not None:
                            try:
                                paraid_to_commentid[pid] = int(cid)
                            except ValueError:
                                continue
            for pid, ppid in paraid_to_parent.items():
                if ppid in paraid_to_commentid:
                    out[pid] = paraid_to_commentid[ppid]
            break
    return out


def _enumerate_revisions(
    doc: DocxDocument,
    paragraphs: list[ParagraphInfo],
    paragraph_objects: dict[str, Paragraph],
) -> list[ExistingRevision]:
    out: list[ExistingRevision] = []
    seen_ids: set[int] = set()
    for pi in paragraphs:
        p = paragraph_objects[pi.id]
        for tag in ("w:ins", "w:del", "w:moveFrom", "w:moveTo"):
            for el in p._p.iter(qn(tag)):
                raw = el.get(qn("w:id"))
                if raw is None:
                    continue
                try:
                    rid = int(raw)
                except ValueError:
                    continue
                if rid in seen_ids:
                    continue
                seen_ids.add(rid)
                # Extract text — w:t (insertion) or w:delText (deletion)
                text_parts = []
                for t in el.iter(qn("w:t")):
                    text_parts.append(t.text or "")
                for t in el.iter(qn("w:delText")):
                    text_parts.append(t.text or "")
                out.append(
                    ExistingRevision(
                        id=rid,
                        type=etree.QName(el).localname,
                        author=el.get(qn("w:author"), ""),
                        text="".join(text_parts),
                        paragraph_ids=[pi.id],
                    )
                )
    # Row-level insertions and deletions (`w:trPr/w:ins`, `w:trPr/w:del`),
    # listed once per row with the text of every paragraph in the row.
    row_revisions: dict[int, ExistingRevision] = {}
    for pi in paragraphs:
        p = paragraph_objects[pi.id]
        mark = _row_revision(p)
        if mark is None:
            continue
        try:
            rid = int(mark.get(qn("w:id")))
        except (TypeError, ValueError):
            continue
        deleted = etree.QName(mark).localname == "del"
        text = _row_paragraph_text(p._p, deleted)
        rev = row_revisions.get(rid)
        if rev is None:
            if rid in seen_ids:
                continue
            seen_ids.add(rid)
            rev = ExistingRevision(
                id=rid,
                type=etree.QName(mark).localname,
                author=mark.get(qn("w:author"), ""),
                text=text,
                paragraph_ids=[pi.id],
            )
            row_revisions[rid] = rev
            out.append(rev)
        else:
            if text:
                rev.text = f"{rev.text} | {text}" if rev.text else text
            rev.paragraph_ids.append(pi.id)
    return out
