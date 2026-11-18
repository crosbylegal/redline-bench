# Edit schema and anchor reference

## Edit object schema (`propose_edits.py`)

```json
{
  "op": "replace" | "delete" | "insert_after",
  "anchor": {
    "paragraph_id": "p-027",
    "text": "verbatim substring of the paragraph",
    "context_before": "optional text immediately before the anchor",
    "context_after": "optional text immediately after the anchor",
    "occurrence": 1
  },
  "new_text": "required for replace and insert_after",
  "comment": "required — the rationale margin comment"
}
```

| op | effect | new_text |
|---|---|---|
| `replace` | tracked-delete `anchor.text`, tracked-insert `new_text` in its place | required |
| `delete` | tracked-delete `anchor.text` | ignored |
| `insert_after` | tracked-insert `new_text` immediately after `anchor.text` | required |

## Anchor resolution rules

- **`text` is required** and must be a verbatim substring of the target
  paragraph's current text. Copy it from the `read_document.py` output, not
  from memory.
- **Tracked changes are not part of the matched text.** `read_document.py`
  shows pending changes inline as `++inserted++` and `~~deleted~~`, but
  matching runs against the paragraph with every such span removed — the
  markers and the text inside them, including your own earlier edits. For a
  line shown as `within ~~thirty (30)~~++ten (10)++ days’ written notice`,
  the anchor `days’ written notice` matches; `ten (10) days`,
  `thirty (30) days`, and anything containing `++` or `~~` do not.
- **Keep an anchor on one side of a tracked change.** An anchor that reaches
  across a `++…++`/`~~…~~` span (above, `within days’ written notice`) can
  resolve and still land the edit in the wrong place.
- One exception: a table row inserted or deleted as a whole is shown with
  each of its paragraphs wrapped in full (`~~Severity 3~~`), and that text
  is still matched.
- **Always supply `paragraph_id`** (the `p-NNN` from the document view). It
  restricts the search to one paragraph, which avoids nearly all ambiguity.
- Matching is **normalized on both sides**, so these differences never cause a
  mismatch: curly vs straight quotes (`’` ≡ `'`, `“”` ≡ `"`), en/em dashes vs
  hyphens, non-breaking spaces, and runs of whitespace (any amount of
  whitespace matches a single space).
- Anything else must match exactly — case, punctuation, spelling.
- If `text` appears more than once in the paragraph, disambiguate with either:
  - `occurrence`: 1-indexed position among the matches (1 = first), or
  - `context_before` / `context_after`: short verbatim snippets adjacent to
    the intended match (whitespace at the boundary is forgiven).

## Failure modes and fixes

Every edit returns a JSON result. Statuses:

| status | meaning | fix |
|---|---|---|
| `ok` | applied; `comment_id` is the margin comment's id | — |
| `anchor_failed` / `text_not_found` | `text` is not a substring of that paragraph | Re-read the paragraph; copy exact text from outside any `++…++`/`~~…~~` span (your earlier edits now sit inside such spans) |
| `anchor_failed` / `ambiguous` | matched multiple places; `candidates` lists paragraph ids | Add `paragraph_id`, then `occurrence` or context fields |
| `anchor_failed` / `paragraph_not_found` | bad `paragraph_id` | Use an id that appears in the current document view |
| `runtime_error` | the engine could not apply the edit | Read `error`; usually re-anchoring on a smaller span fixes it |

Two rules of thumb:

1. **After any successful batch, the paragraph text has changed.** If you need
   a second edit in the same paragraph, re-run `read_document.py` first and
   anchor on the *current* text — what the paragraph now shows outside its
   `++…++`/`~~…~~` spans.
2. **Resubmit only the failed edits.** Successful edits are already saved in
   the file; resubmitting them double-applies the change.

## Choosing good anchors

- Anchor on the **shortest span that is unique** within the paragraph —
  usually 4–10 words. Whole-sentence anchors break when any word in the
  sentence was already edited.
- For `replace`, anchor exactly the words being changed, not the surrounding
  sentence. The tracked change then reads as a surgical word-level edit, which
  is what reviewers expect.
- For `insert_after`, anchor the few words you want the insertion to follow
  (commonly the end of a sentence including its period).
- Text in a table is anchored like any other paragraph: each table-cell
  paragraph has its own `p-NNN`.
