# Skinstinct Content Engine

A note-to-draft assistant for Meera Pillai (founder, Skinstinct). She drops raw notes (voice or text) into a private Telegram channel; three times a week the bot picks the best one, drafts a LinkedIn post in her voice, and sends it to her in Telegram with **Approve / Edit / Discard**. She posts to LinkedIn herself. **This system never publishes anything, anywhere.**

See `PLAN.md` for the design and `CLAUDE.md` for working rules.

## How it works (Components Map)

| Actor | Step | Module |
|---|---|---|
| Meera | Drops a voice note or text into the capture channel | - |
| Telegram | Receives it; voice is transcribed to text with Gemini | `ingest.py` |
| Gemini (triage) | Scores each note 0-10; code rejects anything below 6 | `triage.py` |
| Google News | Fetches a recent, relevant India news hook (optional) | `news.py` |
| Gemini (AI) | Drafts in Meera's voice using `prompts/voice_skill.md` + corpus examples | `draft.py` |
| Code | Validates every draft: no invented numbers, studies, timing or sources; no emoji, hashtags, links or CTAs | `draft.py` |
| Review gate | Meera approves, edits or discards in Telegram, then posts it herself | `review.py` |

`pipeline.py` runs the chain on a schedule (Mon/Wed/Fri 07:30 IST by default), one draft per slot. `db.py` (SQLite) and `gemini_client.py` are the only storage and model boundaries.

## Local setup

Requires Python 3.12+.

```bash
uv venv --python 3.13 .venv
uv pip install --python .venv -r requirements.txt
cp .env.example .env   # fill in the five required values
.venv/Scripts/python config.py   # prints settings with secrets masked, or names what's missing
```

Required values: `TELEGRAM_BOT_TOKEN` (from @BotFather), `TELEGRAM_CHAT_ID` (capture channel, starts with `-100`), `TELEGRAM_REVIEW_CHAT_ID` (Meera's private chat with the bot, which is her user id), `MEERA_USER_ID`, `GEMINI_API_KEY`. The bot must be an admin of the capture channel, and Meera must press **Start** in her chat with the bot before it can message her.

## Run locally (long-polling)

```bash
.venv/Scripts/python app.py
```

In Telegram:
- Post in the capture channel to add notes: text becomes `new`, voice is transcribed and becomes `new`, stickers and bare photos are stored as `unsupported`.
- In the bot chat, `/run` drafts one post now (Meera only), and `/start` confirms the bot is listening.
- Tap **Approve** to get copy-ready text. Tap **Edit**, then reply with a full rewrite (kept verbatim) or a short instruction (one validated redraft). **Discard** shelves the note.

Stop with Ctrl+C.

## Tests

```bash
.venv/Scripts/python -m pytest
```

All external services are mocked. Live checks against real Gemini, Google News and RSS are kept separate:

```bash
.venv/Scripts/python smoke_gemini.py [audio_file]      # one JSON call (+ optional transcription)
.venv/Scripts/python tests/eval_live.py triage         # score the 5 sample notes
.venv/Scripts/python tests/eval_live.py news           # real Google News fetch
.venv/Scripts/python tests/eval_live.py draft          # draft all 5 samples; add --save to update the baseline
.venv/Scripts/python tests/eval_live.py probe 10       # hallucination probe
```

When you change `prompts/draft.md` or `prompts/voice_skill.md`, re-run `eval_live.py draft` and compare against `tests/fixtures/last_good_drafts.json` before shipping.

## Deploy (Railway; Render is the same shape)

In production the bot runs in **webhook mode**. It switches automatically when `PUBLIC_URL` is set, registers the webhook with Telegram on startup, and serves:
- `POST /telegram/<WEBHOOK_SECRET>`, which accepts only requests carrying Telegram's matching `X-Telegram-Bot-Api-Secret-Token` header
- `GET /healthz`, which returns `ok`

1. Push this repo to a **private** GitHub repository. `.env` and `*.db` are git-ignored.
2. In Railway, create a project from the repo. The `Procfile` start command is `python app.py`, and `.python-version` pins Python 3.13.
3. Add a **volume** mounted at `/data`, so SQLite survives redeploys.
4. Under Settings, then Networking, generate a public domain, for example `https://skinstinct.up.railway.app`.
5. Set these variables in the Railway dashboard (never in code):

   | Variable | Value |
   |---|---|
   | `TELEGRAM_BOT_TOKEN` | from @BotFather (rotate it with `/revoke` first if it was ever shared) |
   | `TELEGRAM_CHAT_ID` | capture channel id (`-100...`) |
   | `TELEGRAM_REVIEW_CHAT_ID` | Meera's chat id |
   | `MEERA_USER_ID` | Meera's Telegram user id |
   | `GEMINI_API_KEY` | from Google AI Studio (rotate it if it was ever shared) |
   | `PUBLIC_URL` | the domain from step 4 |
   | `WEBHOOK_SECRET` | output of `python -c "import secrets; print(secrets.token_urlsafe(32))"` |
   | `DB_PATH` | `/data/skinstinct.db` |

   `PORT` is set by the platform. Model names, schedule and thresholds are optional (defaults are in `.env.example`).
6. Deploy. The logs should show `app.starting mode=webhook`, `app.self_check assets=ok`, `app.self_check bot=@...`, `app.self_check review_chat=ok` and `app.webhook_listening`.
7. Verify the webhook. The URL should end in `/telegram/<secret>`, and `last_error_message` should be empty:
   ```bash
   curl "https://api.telegram.org/bot<TOKEN>/getWebhookInfo"
   ```
8. Post a test note in the capture channel, send `/run` in the bot chat, and confirm a draft arrives with buttons.

**Important:** don't run the bot locally in polling mode while production is live. Polling deletes the webhook. To go back to local development, stop the deployment first; the next production start re-registers the webhook.

**Rollback:** redeploy the previous commit from the Railway dashboard. The webhook is re-registered on startup, and the DB schema only ever moves forward, with additive migrations.

**Monitoring:**
- Every run writes a `runs` row: slot, outcome and detail.
- Failures send Meera a short notice with no error details.
- `/healthz` is available for platform health checks.
- Set a billing alert on the Gemini key.
