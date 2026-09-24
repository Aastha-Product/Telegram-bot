# Skinstinct Content Engine

Telegram notes → Gemini triage → Gemini draft in Meera's voice → review in Telegram (Approve/Edit/Discard). Meera posts to LinkedIn herself; this system never publishes anywhere public.

See `PLAN.md` for the design and `CLAUDE.md` for working rules.

## Local setup

Requires Python 3.12+.

```bash
uv venv --python 3.13 .venv
uv pip install --python .venv -r requirements.txt
cp .env.example .env   # then fill in the required values
```

Check config loads (secrets are masked):

```bash
.venv/Scripts/python config.py
```

Run tests:

```bash
.venv/Scripts/python -m pytest
```

## Run the bot locally (long-polling)

```bash
.venv/Scripts/python app.py
```

Post in the capture channel; each post becomes a row in `notes` (text → `new`, voice → `pending_transcription`, stickers/bare photos → `unsupported`/`shelved`). Stop with Ctrl+C.

## Gemini smoke test (real API call, not part of pytest)

```bash
.venv/Scripts/python smoke_gemini.py            # JSON call only
.venv/Scripts/python smoke_gemini.py note.ogg   # also transcribe an audio file
```
