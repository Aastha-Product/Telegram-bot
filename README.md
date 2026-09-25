# Skinstinct Content Engine

A note-to-draft assistant for Meera Pillai (founder, Skinstinct). She drops a voice note (or text) into a private Telegram channel. The bot transcribes it, scores its publishability against a 10-parameter rubric, and sends her a scorecard. If the note scores **strictly above 8.0** and passes every guardrail, it drafts a LinkedIn post in her voice, with a credible news hook only if one genuinely fits, fact-checks the draft twice, and sends it for **Approve / Edit / Reject / Regenerate**. She posts to LinkedIn herself. **This system never publishes anything, anywhere.**

See `PLAN.md` for the design, `docs/publishability_rubric.md` for the scoring rubric (with sources), and `CLAUDE.md` for working rules.

## How it works (Components Map)

| Actor | Step | Module |
|---|---|---|
| Meera | Drops a voice note into the capture channel | - |
| Telegram | Receives it; Gemini transcribes it verbatim and reports clarity. Silent or noisy audio: Meera is asked to re-record | `ingest.py` |
| Gemini (triage) | Scores 10 parameters, each with verbatim evidence, a gap and a guardrail status; flags hard guardrail issues | `triage.py` |
| Code | Verifies evidence against the transcript, applies guardrail caps, computes the weighted score, detects personal data, decides: **> 8.0 and no flags = draft** | `triage.py` |
| Telegram | Scorecard to Meera for every note: rejected (with what would make it stronger), human review (with the flags), or qualified | `review.py` |
| Gemini + Google News | For a note scoring 8 or below: 3 topics that suit Meera specifically, from her content areas and current credible headlines, each phrased as a question about her own experience | `triage.py` |
| Google News | A recent, relevant hook from an allowlisted credible publisher, or none | `news.py` |
| Gemini (AI) | Drafts in her voice (`prompts/voice_skill.md` + corpus examples), preserving her core idea | `draft.py` |
| QA | Code validators (invented numbers, studies or timing; emoji, hashtags, links, CTAs), then a model fact check for anything the note doesn't support. One redraft, then drop | `draft.py` |
| Review gate | Approve (copy-ready text + "I've posted it"), Edit (verbatim rewrite or one-line instruction), Reject, Regenerate | `review.py` |

Notes are processed **the moment they arrive** (`pipeline.process_note`). A sweep every `SWEEP_INTERVAL_MINUTES` retries anything that failed, for example while Gemini or Telegram was down. Every assessment, draft revision, and approved final text (kept separately from the AI draft) is stored in SQLite for audit.

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
- Add notes by posting in the capture channel, or by sending a voice message straight to the bot chat (Meera only). Text becomes `new`, voice is transcribed and becomes `new`, stickers and bare photos are stored as `unsupported`.
- Each note gets a scorecard in the bot chat within seconds; qualifying notes also get a draft.
- `/run` in the bot chat processes anything pending now (Meera only); `/start` confirms the bot is listening and delivers any waiting drafts.
- Tap **Approve** to get copy-ready text, then **I've posted it** once it's live. Tap **Edit**, then reply with a full rewrite (kept verbatim) or a short instruction (one checked redraft). **Reject** shelves the note. **Regenerate** makes a fresh checked draft (up to `MAX_REGENERATIONS`).

Stop with Ctrl+C.

## Tests

```bash
.venv/Scripts/python -m pytest
```

All external services are mocked. Live checks against real Gemini, Google News and RSS are kept separate:

```bash
.venv/Scripts/python smoke_gemini.py [audio_file]      # one JSON call (+ optional transcription)
.venv/Scripts/python tests/eval_live.py triage         # 10-parameter scorecards: 5 samples + test cases
.venv/Scripts/python tests/eval_live.py draft          # news + draft + QA for qualifying samples; --save updates the baseline
.venv/Scripts/python tests/eval_live.py qa             # TEST 9: injected hallucination must be caught by QA
.venv/Scripts/python tests/eval_live.py probe 10       # hallucination probe
.venv/Scripts/python tests/eval_live.py news           # real Google News fetch (credible + relevant only)
```

When you change `prompts/draft.md` or `prompts/voice_skill.md`, re-run `eval_live.py draft` and compare against `tests/fixtures/last_good_drafts.json` before shipping.

## Deploy on Vercel (serverless) with Supabase

Vercel imports `app.py` and serves its ASGI `app`:
- `POST /telegram/<WEBHOOK_SECRET>` accepts only requests carrying Telegram's matching secret header. It processes the note fully (transcribe, score, draft, review) before replying.
- `GET /cron/sweep` is the retry job, protected by `CRON_SECRET`. `vercel.json` schedules it daily at 03:30 UTC (09:00 IST); Hobby allows one cron run per day.
- `GET /healthz` returns `ok`.

Data lives in **Supabase Postgres**, because Vercel's file system is temporary.

1. **Supabase:** create a project, then go to **Connect** and choose **Transaction pooler** (port **6543**). Copy the URI and put your database password into it.
2. **Vercel:** set these under Project, then Settings, then Environment Variables (Production):

   | Variable | Value |
   |---|---|
   | `TELEGRAM_BOT_TOKEN` | from @BotFather (rotate it first if it was ever shared) |
   | `TELEGRAM_CHAT_ID` | capture channel id (`-100...`) |
   | `TELEGRAM_REVIEW_CHAT_ID` | Meera's chat id |
   | `MEERA_USER_ID` | Meera's Telegram user id |
   | `GEMINI_API_KEY` | from Google AI Studio (rotate it first if it was ever shared) |
   | `DATABASE_URL` | the Supabase transaction-pooler URI from step 1 |
   | `WEBHOOK_SECRET` | output of `python -c "import secrets; print(secrets.token_urlsafe(32))"` |
   | `CRON_SECRET` | another output of that command |

   The app refuses to start on Vercel if `DATABASE_URL`, `WEBHOOK_SECRET` or `CRON_SECRET` is missing.
3. **Redeploy.** Push to GitHub, or use Deployments, then Redeploy. The tables are created automatically on first run.
4. **Stop any local `python app.py`.** Local polling can't run while the webhook is set.
5. **Point Telegram at Vercel (once).** Add `PUBLIC_URL=https://<your-project>.vercel.app` and the same `WEBHOOK_SECRET` to your local `.env`, then run:
   ```bash
   .venv/Scripts/python app.py set-webhook
   ```
6. **Check it:**
   - `https://<your-project>.vercel.app/healthz` should return `ok`.
   - Send a voice note to the bot.
   - In Vercel's logs, `app.update_received` shows every incoming message, and `review.scorecard_sent` shows the reply.

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
