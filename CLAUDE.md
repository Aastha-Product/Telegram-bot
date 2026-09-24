# CLAUDE.md — Skinstinct Content Engine

Rules for Claude Code working in this repo. Read this **and** `PLAN.md` before writing code. If anything here conflicts with a later instruction from me, ask.

## What this project is
A note-to-draft assistant for one founder (Meera). Telegram notes → Gemini triage → Gemini draft in her voice → she reviews in Telegram (Approve/Edit/Discard) → she posts to LinkedIn **herself**. The system **never publishes anywhere public.** The review gate is the terminal step. Preserve that boundary in every change.

## How we work
- **One phase at a time** (see `PLAN.md` §12). Never build ahead. After each change, show the diff and how to verify it. Wait for my go-ahead before the next phase.
- **Test after every phase.** Don't tell me something works until a test or a real run proves it.
- **Ask before adding a dependency** that isn't already in `requirements.txt`.
- If a library's real API differs from your memory, check the **installed version's** docs/`--help`/source before writing against it. Pin versions.

## Architecture guardrails (do not overengineer)
- One process, one small service. No microservices, no message queue, no vector DB, no agent framework, no web framework beyond what `python-telegram-bot` already runs.
- The model **writes and classifies**; plain code **decides and gates** (routing, dedup, scheduling, length checks, source verification, authorization). Never put a safety decision inside a prompt when code can enforce it.
- Keep all DB access behind `db.py` and all model calls behind `gemini_client.py`, so provider/storage swaps are one file.
- Model names, chat ids, schedule, and thresholds live in `config.py` — never hard-coded in logic.

## Coding standards
- **Python 3.12+**, type hints on every function, small single-purpose functions.
- **Clean structure:** the module layout in `PLAN.md` §12 is the contract; don't invent new top-level modules without asking.
- **Naming:** clear and boring. `add_note`, `pick_best`, `make_draft`, `send_for_review`.
- **No duplicate logic.** Shared helpers over copy-paste.
- **Comments** only where the *why* isn't obvious (e.g. "Telegram redelivers updates, so this insert must be idempotent"). No narrating the obvious.
- **Async:** the bot is async; never block the event loop. Run blocking SDK/HTTP calls in a thread executor.

## Errors & reliability
- Wrap every external call (Telegram, Gemini, RSS). Failures are **logged with context and never crash the process.**
- Degrade gracefully: RSS down → draft without news; Gemini down → skip the run, quiet notice to the review chat, retry next slot.
- Idempotency is mandatory: `notes.tg_message_id` UNIQUE, `runs.slot` UNIQUE.
- Retries with exponential backoff (max 3) on 5xx/429/timeout; honor `retry-after`. No infinite loops.
- Fail **safe/closed**: if a draft can't be validated, don't send it. Silence beats a broken post.

## Security & secrets
- **No secrets in code, tests, fixtures, logs, or committed files.** Secrets come from env only. `.env` and `*.db` are git-ignored.
- Never log the bot token or API key. Redact in any debug output.
- **All SQL parameterised** — no f-string/format SQL, ever.
- Only `MEERA_USER_ID` may press review buttons or run admin commands — verify `from_user.id` server-side.
- Webhook: secret path segment + verify Telegram's `secret_token` header. Reject anything else.
- Error text sent to Telegram must never contain stack traces or secrets.

## Input validation
- Ingest only `channel_post` from the configured capture chat id; reject empty; non-text → `unsupported`.
- Validate every model output in code before use: JSON shape (via response schema), draft length 200–3000 chars, and the **fact/source checks** in `PLAN.md` §10.3. A cited `source_url` must be one actually fetched this run, or the reference is stripped.

## Testing
- `pytest`. Mock external services (Telegram, Gemini, RSS) in unit tests; keep a couple of real-call smoke scripts separate.
- Every phase adds/updates tests. Critical coverage: DB idempotency, gemini retry/repair, triage ranking on the sample notes, **draft validators (especially fabricated-stat rejection)**, review authorization, pipeline idempotency and graceful degradation.
- Keep the 5 sample notes + last-known-good drafts as a **prompt-regression** fixture; re-run when a prompt changes.

## Voice is the product
The hired human writer failed because his posts were correct but didn't sound like her. A generic, on-trend LinkedIn post is a **defect**, not a pass. Ground drafts in retrieved corpus exemplars; enforce the negative rules (no emoji, no hashtags, no CTA, no invented numbers; British spelling; concrete opening; understated close). When in doubt about tone, look at `corpus/`, not general LinkedIn conventions.

## Git
- Commit after each green phase with a short, specific message. Small commits.
- Never commit `.env`, `*.db`, or anything with a real token/key.
