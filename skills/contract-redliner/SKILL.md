---
name: contract-redliner
description: Redline Word (.docx) contracts with native tracked changes and margin comments, the way an attorney works in Word's Review pane. Use when asked to redline, mark up, revise, or respond to a contract or other .docx legal document — every edit must be a real tracked change (insertion/deletion) with a rationale comment, applied via the bundled scripts, never by rewriting the file.
compatibility: Requires Python 3.10+ with python-docx==1.2.0, docx-revisions, and lxml installed.
---

# Contract Redliner

Redline a `.docx` contract with real Word tracked changes (`<w:ins>`/`<w:del>`)
and threaded margin comments. The deliverable is the same file, edited in
place, that opens cleanly in Microsoft Word's Review pane.

Never edit the `.docx` by other means (unzipping it, rewriting XML, or
regenerating the file) — that destroys tracked-change attribution. All edits
go through the scripts below. Always pass the same `--author` string on every
call in a session — it is the name that appears in Word's Review pane and how
your work is attributed.

## Scripts

1. **Read the document:**

   ```bash
   python scripts/read_document.py CONTRACT.docx
   ```

   Prints the contract as Markdown. Every paragraph has a stable ID like
   `[p-027]`, including paragraphs inside tables: each table-cell paragraph
   is its own line, in document order, without row or column labels.
   Paragraphs carrying existing tracked changes or comments are flagged
   `{REVS,CMTS}`, and their tracked changes show inline as `++inserted++`
   and `~~deleted~~`. An appendix lists each existing comment (`cmt-N`, with
   author and thread parent) and tracked change (`rev-N`, with author and
   text).

   The `++…++` and `~~…~~` spans are display only. An anchor matches the
   paragraph's text with those spans removed, so text inside one cannot be
   anchored, and an anchor should not reach across one (details:
   [references/anchors.md](references/anchors.md)).

2. **Apply a batch of tracked changes:**

   ```bash
   python scripts/propose_edits.py CONTRACT.docx edits.json --author "Jane (AgentCo Legal)"
   ```

   `edits.json` is an array of edit objects (full schema and anchor rules:
   [references/anchors.md](references/anchors.md)):

   ```json
   [
     {
       "op": "replace",
       "anchor": {"paragraph_id": "p-032", "text": "thirty (30) days’ written notice"},
       "new_text": "ten (10) business days’ written notice",
       "comment": "We shortened the notice window because our subprocessors ship on faster cycles than 30 days."
     },
     {
       "op": "delete",
       "anchor": {"paragraph_id": "p-033", "text": "(i) will render accurate and reliable results,"},
       "comment": "We can't warrant that AI outputs are accurate in every case — a breach claim would attach to any imperfect output."
     }
   ]
   ```

   Ops: `replace`, `delete`, `insert_after` (`new_text` required for
   `replace`/`insert_after`). `comment` is required on every edit. Results
   print as JSON per edit; exit code 1 means at least one edit failed.

3. **If an edit fails, fix only that edit and resubmit it.** A failed anchor
   returns `status: "anchor_failed"` with the reason — `text_not_found` (your
   text isn't a verbatim substring of that paragraph, or it includes text
   from inside a `++…++`/`~~…~~` span) or `ambiguous` (it
   matches more than once; candidates are listed). Re-read the paragraph in
   the `read_document.py` output, tighten the anchor (add `paragraph_id`,
   `context_before`/`context_after`, or `occurrence`), and resubmit a batch
   containing only the failed edits — successful ones are already saved in
   the file.

4. **Standalone comments and thread replies:**

   ```bash
   # New thread on a paragraph
   python scripts/add_comment.py CONTRACT.docx --paragraph-id p-035 \
       --comment "Can you confirm whether any Agents are offshore?" --author "Jane (AgentCo Legal)"

   # Reply to existing comment cmt-24 (use the paragraph the thread sits on)
   python scripts/add_comment.py CONTRACT.docx --paragraph-id p-035 --reply-to 24 \
       --comment "Agreed — we accepted this change." --author "Jane (AgentCo Legal)"
   ```

5. **Whole-section removal** (preserves downstream numbering):

   ```bash
   python scripts/mark_reserved.py CONTRACT.docx --start p-036 --end p-037 \
       --comment "We removed this section because ..." --author "Jane (AgentCo Legal)"
   ```

   The range covers every paragraph from `--start` to `--end`, including
   table-cell paragraphs that fall between them.

6. **Verify before finishing.** Re-run `read_document.py` and confirm the
   appendix shows your tracked changes under your author name and your
   comments where you expect them. The saved file is the deliverable.

## Known limitation

`insert_after` shows the anchor text in both the deletion and insertion halves
of the tracked change (it is implemented as replace anchor → anchor+new text).
This is cosmetically noisy but legally equivalent; prefer `replace` with the
full revised phrase when inserting mid-sentence.
