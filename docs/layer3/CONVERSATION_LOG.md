# Log a conversation (agent terminal)

After talking to a customer (call, chat, text, email, branch visit), open the customer in the terminal and click
**+ LOG CONVERSATION** (or type `LOG CS-2026-614370`). Paste the transcript or your own notes, click **EXTRACT DATA**,
check what the system found, then **SAVE TO CUSTOMER FILE**. Spec: `SPEC_L3_CONVERSATION_EXTRACT.md` (same folder).

## Two ways to paste

- **TRANSCRIPT:** lines that start with `Agent:` / `Customer:`, with optional `[mm:ss]` times. Any other label, such as
  `Spouse:`, counts as a third party. It runs the same rules as the call review, including the call-quality score.
- **MY NOTES:** free text such as "Spoke w/ cust. Lost job last month. Promised $200 on Oct 14." Each sentence is read
  as the customer's words. Agent conduct and call quality are not assessed from notes.

**LOAD EXAMPLE** fills in a sample transcript. `.txt` and `.json` files can be loaded too.

## What gets created when you save

| Found in the conversation | Created | Who approves | Can it be skipped? |
|---|---|---|---|
| Asked us to stop contact | stop-contact request | team lead | **No**: contact is held straight away |
| Bankruptcy / consumer proposal mentioned | bankruptcy check | team lead | **No**: contact is held until checked |
| Possible vulnerability | extra-care flag | team lead | **No** |
| Agent threatened legal action (transcripts only) | conduct review | team lead | **No** |
| Promise with an amount and a date | promise, recorded | you | Yes |
| Hardship (job loss, reduced hours, illness …) | hardship request | hardship specialist | Yes (ticked when clear) |
| Dispute | dispute request | you | Yes |
| "Don't call me at work", "only text" … | contact wish | you | Yes |

- **Corrections:** you can correct any field. Changing a protective finding from yes to no needs a written reason,
  and the change is stored next to what was found automatically.
- **The server re-checks on save:** it reads the text again when you save and never trusts what the browser sends.
- **Promise checks:** the same rules as the customer app apply. The date must be after today, within 21 days
  (PRC-COLL-010 §3.1), and not a statutory holiday; the amount must not be above the balance. A failing check blocks
  the save until you fix the value or untick the promise.

## What the customer page shows afterwards

A banner **FROM A CONVERSATION LOGGED IN THIS APP**, the conversation under **CONVERSATIONS LOGGED HERE** (click to
open it read-only, `CONV <id>`), logged promises, and a recomputed next best action.

Protective effects apply at once: a pending stop-contact request moves the next best action to "letters only" and
blocks approving a call. Rejecting the request in Approvals (F6) removes the effect. Nothing that makes collection
more aggressive takes effect without approval.

## Privacy

- **Masking:** card numbers (last 4 digits kept), SINs, phone numbers, emails and bank account numbers are masked in
  stored text and quotes. Amounts and dates are kept.
- **Local store:** the full masked text stays in the local action store (`outputs/layer3/actions.sqlite`).
- **MongoDB:** with `C360_L3_WRITEBACK=mongo`, MongoDB receives the summary, fields, corrections and follow-ups, but
  **not** the text.
- **Browser:** drafts live in the browser's memory only, never in browser storage.
- **No AI model:** extraction is rules-based and repeatable.

## Limits

- **Notes wording:** notes must be fairly plain. Dates like `10/14` are ambiguous and must be typed in.
- **Risk score:** the promise-risk score is not recomputed from logged promises.
- **Append-only:** a saved log cannot be edited; log a correction as a new conversation.
- **Not re-mirrored:** later approvals and rejections are not re-sent to MongoDB (same as other requests today).
- **Not built yet:**
  - the optional AI fill-in for notes (spec Appendix C);
  - the "AI next best step" buttons (`SPEC_L3_AI_NBA.md`);
  - the call-review screen's rendering was not refactored to share code with this one.

API: `POST /api/l3/cases/{id}/conversations/extract` (writes nothing), `POST /api/l3/cases/{id}/conversations`
(needs `confirm: true`), `GET /api/l3/cases/{id}/conversations`, `GET /api/l3/conversations/{id}`.
Tests: `tests/layer3/test_conversation.py`.
