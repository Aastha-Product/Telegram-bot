# Skinstinct Content Engine — Implementation Plan (Case 1 / Meera)

**Prepared for:** Aastha Pandey · MESA AI-Native Track, Founder's Office, Cohort C4
**Date:** 23 September 2026
**Purpose:** A realistic, executable path from the Case 1 documents to a working, tested, deployed system built with Claude Code.
**How to read this:** Sections 1–11 define *what* to build and *why*. Sections 12–17 are the *build*: sequential Claude Code phases, copy‑paste prompts, tests, deployment, and your exact first action.

> **Legend used throughout.** `[DOC]` = stated or directly implied by the provided documents (source of truth). `[REC]` = my engineering recommendation. `[ASSUME]` = an assumption I made to keep moving; verify with Meera/the session. `[RESEARCH]` = current external fact I verified on 23 Sep 2026. `[GAP]` = "Not specified in the provided material."

---

## 0. Alignment with the Components Map Answer Key (amended 24 Sep 2026)

The session's **Components Map answer key** is authoritative. Where any later section differs, this table wins.

| Actor (row) | Column | Answer key | How we build it |
|---|---|---|---|
| Meera (Founder) | Trigger | Drops **voice note** into Telegram | Capture channel accepts **voice notes (primary) and text**. |
| Telegram | Input | Receives note, **transcribes to text** | `ingest.py` receives the `channel_post`; voice audio is downloaded and transcribed to text with Gemini (the Bot API gives no transcript). Transcript stored as the note's `content`. |
| Gemini API (Triage) | Processing | Scores note **0–10**, publishability triage, **rejects low-score notes** | `triage.py` scores **0–10**; code rejects anything below `TRIAGE_THRESHOLD` (default **6**). |
| Google News (Context) | Context | Fetches relevant industry news hook | `news.py` (Google News RSS) — **MUST-have**, feeds the draft step; still degrades to no-news on failure and is source-verified in code. |
| Gemini API | AI | Drafts post in **Meera's voice (Skill)** + news hook | `draft.py` using a **voice skill** (`prompts/voice_skill.md`: her rules distilled from `corpus/`) + retrieved corpus exemplars + the news hook. |
| Review Gate (Meera) | Output | Reviews draft, edits if needed → publishes to LinkedIn | Approve / Edit / Discard in Telegram; **Meera publishes to LinkedIn herself** — the system never does. |

Consequences: voice capture (was S2) and news grounding (was S1) move into the **MUST-HAVE** set; triage scale is 0–10 everywhere.

---

## 1. Executive Understanding

We are building a **content drafting pipeline, not a publishing tool.** Meera Pillai already captures raw thoughts as notes into a private Telegram channel and will not change that habit `[DOC]`. The system's job is to pick up those notes, decide which ones are worth developing, draft a LinkedIn post *in her voice* from the good ones, optionally ground it against something current, and hand the finished draft back to her to review and post herself `[DOC]`.

The single most important design fact is a boundary, and it comes straight from the case: two no‑code consultants offered her tools that did "the whole thing end to end" and **she passed on both** `[DOC]`. She does not want automation that publishes. She wants automation that *removes the stall between having something to say and having a draft in front of her.* The human stays on the trigger for anything that reaches the public.

So the product is a **note‑to‑draft assistant** with a hard **human review gate** before anything is published, targeting **three posts a week without consuming her time** `[DOC]`.

---

## 2. What I Found in the Documents

### Document A — `1_voice.pdf` — "Meera and the Content Backlog" (the case brief)
This is the **primary source of truth.** Key extracted facts:

- **Founder / brand:** Meera Pillai runs **Skinstinct**, a D2C skincare brand launched ~18 months ago in Mumbai; ₹14–16 lakh/month revenue; two years of prior pharma formulation experience; audience is 28–40 year old urban women "tired of being sold to" who respond to founder-led science `[DOC]`.
- **The traction that matters:** her 4 published LinkedIn posts in 8 months got 47,000 impressions combined; her last post drove 340 profile visits in 48 hours and 3 wholesale enquiries. She has 6,200 followers and **has not posted in 11 weeks** `[DOC]`.
- **The problem is stall, not capture.** She drops 2–3 notes/week into a personal Telegram channel (~60 fragments over 8 months). None became posts. She has ~40 abandoned Google Drive drafts. Her words: *"I open the doc, I write two lines, I decide it's not good enough, I close it. By the time I come back two weeks later, the moment has passed."* `[DOC]`
- **A hired writer failed** — grammatically clean, factually accurate, but she spent more time rewriting than writing from scratch, and stopped after 4 posts `[DOC]`. **Implication `[REC]`:** voice fidelity is the make-or-break requirement, not grammar. A generic "good post" is a failure.
- **What she wants (verbatim scope):** something that (1) picks up the Telegram notes, (2) figures out which are worth developing, (3) drafts a LinkedIn post from the ones that are, (4) has it ready for her to look at, and (5) can reference "something current — a news angle, an industry data point — so the post doesn't feel like it was written in a vacuum." Target: **three posts a week, without it consuming her time** `[DOC]`.
- **Explicitly out of scope by her own choice:** end-to-end tools that do everything including publishing `[DOC]`.
- **Provided assets:** `notes/` = 60 raw fragments (voice-note transcriptions, half-finished observations, two-liners; mixed quality, "some not publishable in any form"); `published/` = 15 pieces she wrote herself (4 LinkedIn posts + 11 email newsletters) — *"the only voice reference you have"* `[DOC]`.
- **Class deliverables named:** Automation Brief (Pain/User/Outcome/Journey), the Nine Checks + "the Cut", a Components Map (Trigger → Input → Context → Processing → AI → Output), and a Telegram bot account.

### Document B — `2_Voice.pdf` — Telegram + BotFather setup guide
A step-by-step operational guide. Extracted technical constraints (these are **hard integration facts** `[DOC]`):

- The **trigger point** is a **private Telegram channel** (broadcast-only; only admins post). Meera posts notes; the bot reads them.
- A **bot** is created via **@BotFather** → yields a **Bot Token** (format `7412938475:AAG…`).
- The bot must be added as a **channel administrator** with **only the "Post Messages" permission**; all others off. The guide frames the bot as a "silent admin… it can only listen. Posting is always Meera's action." `[DOC]`
- The channel's **Chat ID** is a **negative number starting with `-100`**, retrieved via @userinfobot and stored as `TELEGRAM_CHAT_ID` `[DOC]`.
- Pre-session checklist confirms: Telegram installed, account verified, private channel created, bot created + username confirmed, bot added as admin (Post Messages only), Chat ID saved.

> **Important nuance `[REC]`:** for a bot to receive channel posts it must be an admin of that channel, and updates arrive as `channel_post` (not `message`). The build must filter on `channel_post` from the configured `-100…` chat ID. This is a common failure point — flagged again in the build plan.

### Document C — `3_Voice.pdf` — Seed data (`published/`, 15 pieces)
The **voice corpus** — the ground truth for tone. 4 LinkedIn posts + 11 newsletters. Observable, reusable voice characteristics `[DOC]`:

- **Structure:** opens with a concrete scene or specific claim, not a hook cliché ("In 2021 I was sitting in a stability review meeting…"; "The niacinamide serum you're using probably has the ingredient listed at 5% or 10%…"). Long, patient paragraphs. Ends on a takeaway that is understated, not a CTA.
- **Stance:** teaches, never sells. Repeated move: *"I'm not saying X. I'm saying Y."* Explicitly names uncertainty ("When I don't know something, I'll say so"). Concedes the other side before making her point.
- **Substance:** heavy on formulation chemistry, pH, concentration thresholds, stability, CoA/documentation, India-specific climate context (humidity, returns data). Uses real numbers she owns (23% of returns, 71% from >70% humidity cities, 67% repeat rate).
- **Register:** plain, precise, no wellness language, no emoji, no hype, British-ish spelling ("maximise", "oestrogenic"). First person, direct address to reader.
- **Categories present:** Ingredient Deep-Dive, Founder Story, India-Specific Context, Industry Transparency, Formulation Science, Brand Philosophy, Consumer Education. These are natural **content buckets** the system can reuse `[REC]`.

### Document D — `…af.docx` — Automation Brief / Nine Checks / Components Map templates
The course's decision framework `[DOC]`. Three artifacts:

1. **Automation Brief:** Why (Pain / User / Outcome) + Journey Today.
2. **The Nine Checks** (each Pass/Fail with evidence), grouped as:
   - *Kill Switches (any No = do not build):* 01 Problem Real, 02 Workflow Repeated, 03 Input Available.
   - *Sizing (any No = build smaller):* 04 Output Valuable, 05 Impact Measurable, 08 ROI Worth It.
   - *Boundary (any No = build the tool, human stays here):* 06 Failure Risk OK, 07 Judgment Protected, 09 Owner Clear.
   - **"The Cut": which check killed the scope.**
3. **Components Map** with named actors/columns: **Meera/Founder, Telegram, Gemini, Google News RSS, Review Gate**, across Trigger → Input → Context → Processing → AI → Output, with a **visible human gate** `[DOC]`.

**This document fixes the intended architecture.** The named components are not my invention — the case expects **Telegram (trigger/input) → Google News RSS (context) → Gemini (AI) → Review Gate (human) → Meera**. My job is to make that real and robust.

### Document E — `note.docx` — sample raw notes (from `notes/`)
Five representative raw fragments (clean-beauty musing, skin-barrier metaphor, a cold-pressed sourcing incident, a customer layering-order diagnosis, a batch-14 pH/preservative-change story). Characteristics that drive processing design `[DOC]`:

- Written in **first-person stream of consciousness**, longer and rawer than a post, often ending with *"I want to write about this but I'm not sure what the new angle is"* / *"I need to write about this because…"* — i.e., the note often already contains its own thesis and her doubt.
- **Mixed readiness:** some are a complete argument minus shaping (batch-14, cold-pressed, layering-order); one (clean beauty) she flags as *already-said, no new angle* — a good example of a note the system should **rank lower**.
- These map cleanly onto the published categories, which supports a **classify-then-draft** approach `[REC]`.

---

## 3. Requirements Consolidation

### Functional requirements (what it must do)
- **FR1** Ingest text notes Meera posts to her private Telegram channel, automatically, without her doing anything extra. `[DOC]`
- **FR2** Store every note durably with its timestamp and a processing status. `[REC, enables FR3–FR6]`
- **FR3** Score/triage notes to decide which are "worth developing," and *not* draft from weak ones. `[DOC]`
- **FR4** Draft a LinkedIn post from a selected note, **in Meera's voice**, grounded in the 15-piece corpus. `[DOC]`
- **FR5** Optionally attach a *current* reference (news angle / data point) from Google News RSS when a relevant, credible one exists. `[DOC]`
- **FR6** Deliver the finished draft back to Meera **inside Telegram** with an explicit **Approve / Edit / Discard** action. `[DOC + user decision]`
- **FR7** Never publish anywhere public. Posting to LinkedIn stays a manual human action. `[DOC]`
- **FR8** Run on a cadence that yields ~3 review-ready drafts/week without her intervention. `[DOC]`

### Non-functional requirements
- **NFR1 Voice fidelity first.** A draft that reads generic is a defect (the hired-writer lesson). `[REC from DOC]`
- **NFR2 Low effort for Meera.** Review must be phone-friendly and take seconds, not minutes. `[DOC]`
- **NFR3 Reliability over cleverness.** A missed note or a crash mid-week breaks trust; the pipeline must be idempotent and recover cleanly. `[REC]`
- **NFR4 Cheap to run.** MVP should sit near/at free tier. `[user decision: cheapest always-on]`
- **NFR5 Factual safety.** Meera's whole brand is "don't claim what you can't support." Any AI-added "current" fact must be traceable to a real source or omitted. `[REC from DOC — this is a brand-existential constraint]`
- **NFR6 Secrets server-side.** Bot token and API keys never in code or client. `[REC]`
- **NFR7 Single owner.** One accountable person (Meera) for every output — satisfies Check 09. `[DOC]`

### Business rules
- **BR1** Publishing is always Meera's action. `[DOC]`
- **BR2** A note she flags (or the model scores) as "nothing new to say" should not become a draft. `[DOC — clean-beauty note]`
- **BR3** Drafts must not invent statistics, studies, or news. Numbers come from her own corpus/notes; external facts come from a cited RSS item or are left out. `[REC from DOC]`
- **BR4** The system references current material only when it genuinely fits the note's topic; a forced news angle is worse than none. `[REC from DOC]`

### The Nine Checks — worked against the case `[DOC evidence]`
| # | Check | Evidence from the case | Verdict |
|---|-------|------------------------|---------|
| 01 | Problem Real | 60 notes, 40 dead drafts, 11 weeks silent, quantified stall | **Pass** |
| 02 | Workflow Repeated | 2–3 notes/week for 8 months; target 3 posts/week | **Pass** |
| 03 | Input Available | Notes in Telegram + 15 published pieces provided now | **Pass** |
| 04 | Output Valuable | 47k impressions, 340 visits, 3 wholesale enquiries from prior posts | **Pass** |
| 05 | Impact Measurable | Posts/week, impressions, profile visits, enquiries; time-to-draft | **Pass** |
| 08 | ROI Worth It | Near-free to run; replaces a paid writer who failed | **Pass** |
| 06 | Failure Risk OK | A bad draft is caught at review; **a bad *published* post is not** | **Pass only if review gate exists** |
| 07 | Judgment Protected | Requires a human to review before anything public | **Pass only if review gate exists** |
| 09 | Owner Clear | Meera is the sole accountable owner | **Pass** |

### The Cut `[REC — interpretation, flag for the session]`
**The scope that gets cut is auto‑publishing to LinkedIn.** Checks **07 (Judgment Protected)** and **06 (Failure Risk OK)** are the boundary checks: an AI post published unreviewed to the account of a founder whose entire brand equity is "I only claim what I can support" is an unacceptable failure mode. That is exactly why she rejected the two end‑to‑end tools. So the build stops at a review‑ready draft; the human stays on the publish action. Everything upstream of the review gate is automated; nothing downstream is.

> If the session's intended "Cut" is instead the *news-grounding* step (a defensible alternative reading, since live news is the most hallucination-prone part), the plan still holds: news becomes a clearly-postponable SHOULD-HAVE (see §8), and the MUST-HAVE is note → voice draft → review in Telegram.

---

## 4. Gaps / Contradictions / Assumptions

**Genuine gaps (Not specified in the provided material):**
- **G1 — Cadence mechanism.** How often the pipeline runs and how it picks *which* 3 notes/week. `[GAP]` → `[REC]` a scheduled batch (e.g. Mon/Wed/Fri mornings) that drafts the single best un-drafted note each run; §7 details this.
- **G2 — Voice grounding method.** Whether to fine-tune, few-shot, or retrieve from the corpus. `[GAP]` → `[REC]` few-shot with retrieved exemplars (no fine-tuning for MVP); §10.
- **G3 — Voice notes / audio.** The case says `notes/` includes "voice note transcriptions," implying some capture is audio, but the live channel notes she'll post are `[GAP]` on format. → **Resolved by the answer key (§0):** voice notes are the primary capture; Telegram delivers voice as a file, which we transcribe with Gemini. Text posts are also accepted.
- **G4 — Draft length/format target.** No stated length. `[GAP]` → `[REC]` match corpus norms (~150–450 words, no hashtags, no emoji).
- **G5 — Where approved drafts go.** `[GAP]` → with review-in-Telegram, "Approve" marks it approved and gives her clean copy-paste text; LinkedIn API auto-post is LATER (§8).
- **G6 — Data residency / privacy expectations.** `[GAP]` → `[REC]` store only note text + drafts; keep on a single region host; no third-party analytics.

**Contradiction / nuance to resolve:**
- **C1** The BotFather guide says the bot "can only listen… posting is always Meera's action," yet grants the bot "Post Messages." These are not actually in conflict: "Post Messages" is the channel permission that lets an admin bot *see* channel posts; the "posting is Meera's action" line is about **LinkedIn** publishing, not Telegram. **However**, to deliver drafts back to Meera *inside Telegram* (our chosen review surface), the bot sends messages to **a separate private chat with Meera (or a second "Drafts" channel/group), not necessarily the capture channel.** Design around this in §5/§11. `[REC]`

**Assumptions I'm proceeding on (verify):**
- `[ASSUME]` One user (Meera). No multi-tenant, no auth system needed for MVP.
- `[ASSUME]` English-language notes and posts.
- `[ASSUME]` "Current" reference means last ~14 days of India/skincare/beauty-industry news via Google News RSS.
- `[ASSUME]` You (Aastha) will supply the Gemini API key and any other keys, and will run the Telegram setup from Doc B.

---

## 5. Recommended Architecture

**Shape: a single small always‑on service + a scheduler + one database.** No microservices, no queue broker, no vector DB, no frontend. This is the smallest thing that satisfies every requirement and is the easiest to debug. `[REC]`

```
                    ┌──────────────────────────────────────────────────────┐
                    │  Meera's phone (Telegram)                            │
                    │  • Capture channel  → she drops raw notes            │
                    │  • Review chat      ← bot sends drafts + buttons     │
                    └───────────────┬───────────────────────▲──────────────┘
                                    │ channel_post          │ draft + Approve/Edit/Discard
                            webhook │                       │ sendMessage + inline keyboard
                    ┌───────────────▼───────────────────────┴──────────────┐
                    │  ONE SERVICE  (Python, always-on host)               │
                    │                                                      │
                    │  Ingest handler ──► SQLite/Postgres (notes, drafts)  │
                    │                                                      │
                    │  Scheduler (Mon/Wed/Fri) ──► Draft pipeline:         │
                    │     1. pick best un-drafted note (classifier)        │
                    │     2. fetch Google News RSS for topic (context)     │
                    │     3. Gemini: draft in voice (few-shot from corpus)  │
                    │     4. validate output (JSON, no invented facts)     │
                    │     5. send draft to Review chat                     │
                    │                                                      │
                    │  Callback handler ◄── Approve/Edit/Discard presses   │
                    └───────────────┬──────────────────────────────────────┘
                                    │
                    ┌───────────────▼───────────────┐   ┌──────────────────┐
                    │ Gemini API (google-genai SDK) │   │ Google News RSS   │
                    └───────────────────────────────┘   └──────────────────┘
```

### Frontend
**None for MVP.** Telegram *is* the UI — capture in one chat, review in another, buttons for the gate. `[user decision]` This removes an entire build/host/security surface. A web dashboard is a LATER nice-to-have (§8).

### Backend
- **Runtime:** **Python 3.12+.** `[REC]` Rationale in §6.
- **Framework:** `python-telegram-bot` v22.x (async), which bundles a webhook server; add a tiny scheduler via its built-in `JobQueue` (APScheduler under the hood) so there is **no separate cron service**. `[RESEARCH: PTB is at v22.8]`
- **Three responsibilities, one process:** (a) webhook ingest of `channel_post`, (b) scheduled draft jobs, (c) callback handling for button presses.

### Database
- **MVP: SQLite** (a single file on the host's persistent disk). One user, low volume, zero ops. `[REC]`
- **Upgrade path: Postgres** (Railway/Render managed, or Supabase) when you want durability across redeploys or a dashboard. Keep all DB access behind a thin `db.py` so the swap is one file. `[REC]`
- Tables: `notes`, `drafts`, `runs` (see §9).

### AI layer
- **Model:** **Gemini** (mandated by the Components Map). `[DOC]`
  - Drafting: **`gemini-3.5-flash`** — strong quality for voice matching at low cost. `[RESEARCH/REC]`
  - Triage/classification: **`gemini-3.5-flash-lite`** — cheapest, plenty for scoring. `[RESEARCH: $0.30/$2.50 per 1M tokens; free tier covers MVP]`
- **Grounding:** few-shot with 2–3 *retrieved* corpus exemplars from the same category (not the whole corpus every call). `[REC]`
- **Structured output:** Gemini JSON mode / `response_schema` for both triage and draft. `[REC]`
- Full detail in §10.

### Automation layer
- **Trigger 1 (event):** Telegram webhook fires on each `channel_post` → store note. `[DOC]`
- **Trigger 2 (schedule):** `JobQueue` runs the draft pipeline Mon/Wed/Fri 07:30 IST. `[REC for G1]`
- **Trigger 3 (event):** button callback → update draft status. `[REC]`
- Failure handling: each stage try/caught, errors logged and surfaced to Meera's review chat as a short "couldn't draft today, will retry" note rather than silent failure. §11.

### External integrations
1. **Telegram Bot API** — ingest + delivery. Auth: bot token. §11.
2. **Gemini API** — classify + draft. Auth: API key. §10/§11.
3. **Google News RSS** — current context. No auth (public RSS). §11.

### Infrastructure
- **Local dev:** run with long-polling (`Application.run_polling()`), SQLite file, `.env` for secrets.
- **Prod:** one always-on service on **Railway or Render** (webhook mode + HTTPS provided by the platform), env vars in the platform's secret store, SQLite on a mounted volume (or managed Postgres). `[RESEARCH/REC]`
- **Logging:** structured stdout logs (platform captures them). **Monitoring:** a daily heartbeat log + optional error ping to Meera's review chat. §13.

---

## 6. Technology Decisions

For each decision: options → pick → why → tradeoff → fit → MVP or later.

### 6.1 Language / runtime → **Python 3.12+** `[REC]`
- **Options:** Python, Node/TypeScript, a no-code platform (n8n/Make/Zapier).
- **Pick:** Python.
- **Why:** the two libraries that carry this project — `python-telegram-bot` (mature, async, built-in webhook server *and* job scheduler) and Google's `google-genai` SDK — are both first-class in Python. You (the user) will supply the API keys and left the stack to me; Python gives Claude Code the most training data and the fewest moving parts for AI + Telegram.
- **Tradeoff:** if you later want the review UI and the backend in one TypeScript codebase, Node would be tidier. Not worth it now.
- **Fit:** a note→AI→message pipeline is exactly Python's sweet spot.
- **MVP.** (Node with `grammY` + `@google/genai` is a fine alternative if you personally prefer TS — say so and the prompts port cleanly.)
- **Why not no-code:** Meera *already rejected* end-to-end no-code tools, and the case is explicitly a build exercise (Telegram bot, Components Map). Code gives the control the review-gate boundary needs.

### 6.2 Telegram library → **`python-telegram-bot` v22.x** `[RESEARCH/REC]`
- **Options:** `python-telegram-bot` (PTB), `aiogram`, raw Bot API over `httpx`.
- **Pick:** PTB.
- **Why:** it ships an async webhook server, an inline-keyboard/callback system for the Approve/Edit/Discard buttons, and a `JobQueue` scheduler — so ingest, review UI, and cadence all live in one dependency with no extra cron/queue service.
- **Tradeoff:** slightly heavier than raw API calls; irrelevant at this scale.
- **MVP.**

### 6.3 AI provider/model → **Gemini** (`gemini-3.5-flash` draft, `gemini-3.5-flash-lite` triage) `[DOC + RESEARCH]`
- **Options (within the mandate):** Gemini is fixed by the Components Map. Within Gemini, the live 3.x family (3.5 Flash / 3.5 Flash-Lite / 3.6–3.8 Flash) — the 2.5 models are now legacy/restricted for new projects.
- **Pick:** `gemini-3.5-flash` for drafting (voice nuance matters), `gemini-3.5-flash-lite` for triage.
- **Why:** Flash gives near-frontier writing quality; Flash-Lite is the cheapest current model and triage is easy. Both support JSON structured output. Free tier comfortably covers ~12 drafts + ~60 triage calls a week.
- **Tradeoff:** if voice matching underwhelms in eval, step drafting up to a 3.7/3.8 Flash — one string change (§10 keeps the model name in config).
- **Cost `[RESEARCH, 23 Sep 2026]`:** Flash-Lite $0.30 in / $2.50 out per 1M tokens; Flash 3.5 tier low; a week of use is **cents**. See §10.6.
- **MVP.**

### 6.4 SDK → **`google-genai`** (the unified SDK) `[RESEARCH/REC]`
- **Why:** `google-genai` is the current, actively-developed SDK (the older `google-generativeai` is deprecated). It exposes `client.models.generate_content(...)` with `response_schema` for typed JSON.
- **MVP.** Pin the version in `requirements.txt` and let Claude Code read the installed version's help before coding (prompt in §13 handles drift).

### 6.5 Database → **SQLite now, Postgres-ready** `[REC]`
- **Options:** SQLite, managed Postgres (Railway/Render/Supabase), a JSON file.
- **Pick:** SQLite behind a thin data-access module.
- **Why:** one user, tens of rows/week — a managed Postgres is real ops cost for no benefit yet. A JSON file loses you queries and safe concurrent writes.
- **Tradeoff:** SQLite on a single host doesn't survive an ephemeral filesystem — so the host must give it a **persistent volume**, or you move to Postgres. Called out in §13.
- **MVP** (SQLite); **SHOULD-HAVE** (Postgres) once a dashboard or multi-device durability is wanted.

### 6.6 Hosting → **Railway or Render, always-on** `[RESEARCH/REC — user chose "cheapest always-on"]`
- **Options:** Railway, Render, Fly.io, a $5 VPS, serverless functions.
- **Pick:** **Railway** (usage-based, simple) or **Render** (fixed small plan). Either runs one always-on web service with a public HTTPS URL for the Telegram webhook and a persistent volume for SQLite.
- **Why over serverless:** a webhook + an in-process scheduler + a long-lived bot application want a process that's always up. Serverless adds cold starts, webhook plumbing, and split scheduling for no MVP gain.
- **Tradeoff:** a few dollars/month vs. scale-to-zero. You explicitly chose always-on for debuggability — correct call here.
- **MVP.** (Start **local** with polling to prove the pipeline, then deploy — §15.)

### 6.7 Current-context source → **Google News RSS** `[DOC]`
- Fixed by the Components Map. Public RSS endpoint, no key, region-filterable to India. Parse with `feedparser`. Treated as *optional* enrichment with strict guardrails (§10.4, §11).

### 6.8 What I deliberately did **not** add
No vector database (few-shot retrieval over 15 documents is a dictionary lookup, not a similarity-search problem). No message queue. No Docker-compose fleet. No auth service. No web framework beyond what PTB already runs. No LangChain/agent framework — the pipeline is 4 deterministic steps with 2 model calls; an agent loop would add nondeterminism to a system whose whole point is a predictable review gate. `[REC — anti-overengineering]`

---

## 7. User Flows

Format: **USER ACTION → SYSTEM → AI → VALIDATION → DB → AUTOMATION → RESULT.**

### Flow A — Capture (happy path) `[DOC]`
- **USER:** Meera types/pastes a note into the capture channel.
- **SYSTEM:** Telegram sends a `channel_post` update to the webhook; handler checks it's from the configured `-100…` chat.
- **AI:** none (capture is deterministic — do not spend a model call here).
- **VALIDATION:** non-empty text; length within sane bounds; dedupe on Telegram `message_id`.
- **DB:** insert `notes` row, `status='new'`.
- **AUTOMATION:** none yet (drafting is batched).
- **RESULT:** silent success (optionally a 👍 reaction). Meera's habit is unchanged.

### Flow B — Draft generation (happy path) `[DOC]`
- **TRIGGER:** scheduler fires (Mon/Wed/Fri 07:30 IST).
- **SYSTEM:** load `notes` where `status='new'`.
- **AI #1 (triage):** Flash-Lite scores each note 0–10 for "worth developing" + assigns a category + one-line reason. Notes flagged "nothing new" score low.
- **VALIDATION:** valid JSON, score in range; pick the highest-scoring note above threshold. If none clears threshold, exit cleanly (see Flow F).
- **SYSTEM:** select 2–3 corpus exemplars from the same category; fetch Google News RSS for the note's topic keywords.
- **AI #2 (draft):** Flash writes a post in-voice, using exemplars; includes a current reference **only if** a fetched item is clearly relevant, with its source.
- **VALIDATION:** JSON parses; body within length bounds; if a news claim is present it must carry a matching source URL from the fetched feed, else the claim is stripped; no fabricated statistics (checker in §10.3).
- **DB:** insert `drafts` row, `status='pending_review'`, link `note_id`, store exemplar ids + any source URL; mark note `status='drafted'`.
- **AUTOMATION:** bot sends the draft to the **review chat** with inline buttons.
- **RESULT:** Meera has a review-ready draft on her phone.

### Flow C — Review & approve `[DOC + user decision]`
- **USER:** taps **Approve**.
- **SYSTEM:** callback handler verifies the press is from Meera's user id.
- **DB:** `drafts.status='approved'`, timestamp.
- **AUTOMATION:** bot replies with the **clean post text in a copy-friendly block** ("Approved — copy below and post to LinkedIn").
- **RESULT:** she copies, posts to LinkedIn herself. **BR1 upheld.**

### Flow D — Edit
- **USER:** taps **Edit** → bot says "send your revised version as a reply," or "reply with a one-line instruction and I'll redraft."
- **SYSTEM:** next message from Meera in the review chat is captured against the pending draft.
- **AI (optional):** if she gave an instruction, Flash redraws once with her note appended; if she pasted her own text, no model call — store verbatim.
- **DB:** new `drafts` revision row (keep history), `status='pending_review'` again → she Approves.
- **RESULT:** tightened draft; her edits are also **captured as future voice signal** (§10.5).

### Flow E — Discard
- **USER:** taps **Discard.**
- **DB:** `drafts.status='discarded'`; note `status='shelved'` (not deleted — could resurface).
- **RESULT:** nothing published; no nagging.

### Edge & failure paths `[REC]`
- **F1 Empty/low-quality note:** triage scores all `new` notes below threshold → **no draft**, log it, and (at most once/week) tell Meera "nothing ready to draft yet — drop a few more notes." Never draft junk.
- **F2 Invalid input:** non-text post (sticker/photo-only) → store as `unsupported`, skip; a photo with a caption stores the caption.
- **F3 Telegram API failure on delivery:** retry with backoff (3×); if still failing, leave draft `pending_review` and deliver on next run; log error.
- **F4 Gemini timeout / 5xx / 429:** exponential backoff retry (3×); on final failure, skip this run gracefully and post a quiet "couldn't generate today, will retry" to the review chat. Never crash the process. `[DOC brand-safety: silence is better than a broken/garbled post]`
- **F5 Gemini returns invalid JSON:** one automatic re-ask with a "return valid JSON only" repair prompt; if it fails again, skip. §10.7.
- **F6 RSS fetch fails or returns nothing relevant:** proceed **without** a current reference — the post is still valid. News is enrichment, never a hard dependency. `[DOC]`
- **F7 Duplicate webhook delivery:** idempotent insert on `message_id` (Telegram can redeliver).
- **F8 Scheduler double-fire after redeploy:** a `runs` row + advisory check prevents drafting twice for the same slot.
- **F9 Loading/empty state (first run, no notes):** review chat shows a one-time "I'm set up and listening — drop notes anytime." 
- **F10 Human override:** Meera can always ignore the buttons and do nothing; nothing publishes without her. The gate fails *safe* (closed).

---

## 8. MVP Scope

The MVP must prove the core hypothesis: **can automated, voice-matched drafts delivered to Telegram get Meera from "stalled" to "posting 3×/week"?** Everything is judged against that.

### MUST HAVE (the core loop)
- **M1** Telegram capture of text notes → DB. `[DOC — FR1/FR2]`
- **M2** Triage that filters out weak notes. `[DOC — FR3]`
- **M3** Voice-matched drafting grounded in the 15-piece corpus. `[DOC — FR4, and the hired-writer lesson makes this non-negotiable]`
- **M4** Deliver draft to Telegram review chat with Approve/Edit/Discard. `[DOC + user decision — FR6]`
- **M5** Scheduled cadence producing ~3 drafts/week. `[DOC — FR8]`
- **M6** No public publishing; review gate is the terminal step. `[DOC — FR7/BR1]`
- **M7** Secrets in env, structured logging, graceful AI/Telegram failure handling. `[REC — NFR3/NFR6]`
- **M8** Voice-note capture → Gemini transcription → same pipeline. `[DOC — answer key §0; was S2]`
- **M9** Google News RSS news hook, with source-citation guardrail and graceful no-news fallback. `[DOC — answer key §0; was S1]`

*Why these:* remove any one and the core hypothesis can't be tested. M3 is the single riskiest requirement, so it gets the most eval attention (§14).

### SHOULD HAVE (fast follow, post-MVP)
- ~~S1~~ / ~~S2~~ moved to MUST (M9 / M8) per the answer key (§0).
- **S3** "Redraft from a one-line instruction" in the Edit flow. `[REC]`
- **S4** Learning from her edits: append approved final versions to the corpus so voice improves over time. `[REC — compounding value]`
- **S5** A weekly digest ("3 drafts sent, 2 posted, 1 discarded").

### LATER (explicitly postponed)
- **L1** LinkedIn API auto-post — *deliberately out of scope; it's the exact thing she rejected.* Only revisit if she asks, and even then keep the review gate. `[DOC]`
- **L2** Web review dashboard. `[user chose Telegram]`
- **L3** Multi-user / multi-brand, auth, roles.
- **L4** Vector search over a larger corpus (only if the corpus grows past a few dozen pieces).
- **L5** Analytics ingestion (pull post performance back in to rank topics).
- **L6** Postgres migration (do when durability/dashboard demands it).

*Why postponed:* none is needed to test the hypothesis, and L1 actively violates the product's defining boundary.

---

## 9. Data Model

SQLite for MVP; types shown are Postgres-compatible so the migration is mechanical. All access goes through `db.py`. Store **only** note text, drafts, and run metadata — no analytics, no PII beyond Meera's own Telegram user id. `[REC — NFR privacy]`

### Entity: `notes`
Purpose: every raw fragment Meera posts.
| Field | Type | Req | Notes / validation |
|-------|------|-----|--------------------|
| `id` | INTEGER PK | yes | autoincrement |
| `tg_message_id` | INTEGER | yes | Telegram message id; **UNIQUE** (idempotent ingest) |
| `tg_chat_id` | INTEGER | yes | must equal configured capture chat id |
| `content` | TEXT | yes | the note text, or the voice transcript; empty only while `pending_transcription` |
| `tg_file_id` | TEXT | no | Telegram file id for voice notes (to download/transcribe) |
| `content_type` | TEXT | yes | `text` \| `voice` \| `unsupported`; default `text` |
| `created_at` | TIMESTAMP | yes | when posted |
| `status` | TEXT | yes | `pending_transcription` \| `new` \| `drafted` \| `shelved`; default `new` |
| `category` | TEXT | no | set by triage (e.g. `Ingredient Deep-Dive`) |
| `score` | REAL | no | triage 0–10 |

Example: `{id:12, tg_message_id:481, content:"batch fourteen came back… pH dropped 0.4 units…", content_type:"text", status:"drafted", category:"Industry Transparency", score:8.2}`

### Entity: `drafts`
Purpose: each generated/edited draft, with review state and provenance.
| Field | Type | Req | Notes / validation |
|-------|------|-----|--------------------|
| `id` | INTEGER PK | yes | |
| `note_id` | INTEGER FK→notes.id | yes | source note |
| `revision` | INTEGER | yes | 1,2,… (Edit creates a new revision) |
| `body` | TEXT | yes | the post text; length 200–3000 chars |
| `model` | TEXT | yes | model name used (audit) |
| `exemplar_ids` | TEXT | no | JSON list of corpus pieces used |
| `source_url` | TEXT | no | RSS item cited, if any (must be a real fetched URL) |
| `status` | TEXT | yes | `pending_review` \| `approved` \| `discarded` \| `superseded` |
| `created_at` | TIMESTAMP | yes | |
| `reviewed_at` | TIMESTAMP | no | when Meera acted |

Example: `{id:20, note_id:12, revision:1, body:"Batch fourteen came back with pH stability data that looked off…", model:"gemini-3.5-flash", exemplar_ids:"[3,9]", source_url:null, status:"approved"}`

### Entity: `runs`
Purpose: one row per scheduled pipeline execution — prevents double-fires, gives you an audit/heartbeat.
| Field | Type | Req | Notes |
|-------|------|-----|-------|
| `id` | INTEGER PK | yes | |
| `slot` | TEXT | yes | e.g. `2026-09-23-am`; **UNIQUE** (idempotent cadence) |
| `started_at` / `finished_at` | TIMESTAMP | yes/no | |
| `outcome` | TEXT | yes | `drafted` \| `no_candidate` \| `error` |
| `detail` | TEXT | no | note id / error summary |

### Corpus (voice reference) — **not a DB table**
The 15 published pieces live as files in `/corpus` (one `.txt` per piece) with a small `corpus_index.json` mapping id → `{category, path, title}`. Loaded into memory at startup. `[REC — it's static reference data, versioned in git, not user data.]`

### What must NOT be stored
- No LinkedIn credentials (never touches LinkedIn). No customer data from her notes beyond what she typed. No third-party analytics/tracking. `[REC]`

### Retention & audit
- Keep notes/drafts indefinitely for MVP (tiny). `runs` gives you an audit trail of what the system did and when. If Meera wants a note deleted, delete by id (cascade drafts). `[REC]`

---

## 10. AI Architecture

Two model calls, both deterministic in *shape* (typed JSON), never an open-ended agent. `[REC]`

### 10.1 Model selection
- **Triage:** `gemini-3.5-flash-lite` — cheap, fast, easy classification. `[RESEARCH/REC]`
- **Draft:** `gemini-3.5-flash` — best voice quality per rupee. Escalate to a 3.7/3.8 Flash only if eval (§14) shows voice misses. Model names live in `config.py` so this is a one-line change. `[REC]`

### 10.2 Prompt architecture
Three prompts, all as versioned files in `/prompts` (so Claude Code and you can iterate without touching logic):

1. **`triage.md`** — input: one note + the list of categories from the corpus. Output JSON: `{worth_developing: bool, score: 0-10, category: str, reason: str, suggested_angle: str}`. Instruction highlights: score *low* when the note itself says "nothing new here"; score *high* when it contains a concrete incident + a claim (batch-14, cold-pressed, layering-order patterns). 
2. **`draft.md`** — input: the note, its category, 2–3 full corpus exemplars, optional news item(s). Output JSON: `{body: str, used_source_url: str|null, self_check: {invented_stats: bool, on_voice: bool}}`. Instruction highlights: mirror the exemplars' structure (concrete opening scene, patient paragraphs, "I'm not saying X, I'm saying Y", understated close, no emoji/hashtags/CTA, British spelling); use ONLY facts present in the note or the provided source; if unsure, leave it out; never invent statistics or studies.
3. **`repair.md`** — used only when JSON parsing fails: "Return the same content as valid JSON matching this schema, nothing else."

### 10.3 Structured output & validation (the safety spine) `[REC — NFR5/BR3]`
Every model call uses Gemini `response_schema` (JSON mode). After parsing, deterministic **code** (not the model) enforces:
- Draft `body` length in [200, 3000] chars; else reject/redraft.
- If `used_source_url` is set, it **must** be one of the URLs actually fetched from RSS this run; otherwise strip the sentence that references it (or discard and retry once). This is what stops a hallucinated "a 2026 study found…".
- A regex/stat check: any `%`, "study", "N=", or year-with-claim in the body must trace to (a) the source note text, or (b) the cited source. Unverifiable → flag → redraft once → if still present, **don't send** (fail safe). 
- `self_check.invented_stats == true` → auto-reject and redraft once.

### 10.4 Grounding: voice + current context
- **Voice grounding = few-shot retrieval.** Pick 2–3 exemplars from the *same category* as the note (from `corpus_index.json`). This anchors structure and register far better than a description of her style, and it's why the writer she hired failed — he had the facts, not the shape. `[REC from DOC]`
- **Current context = Google News RSS, optional.** Query built from the note's topic keywords, region IN, last ~14 days. Take the top 1–2 items; pass title + source + link to the draft prompt as *available* material. The model is told: cite only if it genuinely fits, and always keep the link. If nothing fits, the post ships without news. `[DOC — FR5, guardrailed]`

### 10.5 Learning loop (SHOULD-HAVE S4)
When Meera **Approves** a draft (especially after an Edit), store the final body and optionally add it to the corpus as a new exemplar for its category. Over weeks the system drafts closer to what she actually ships. No fine-tuning, no retraining — just a growing few-shot pool. `[REC]`

### 10.6 Cost estimate `[RESEARCH 23 Sep 2026 + REC]`
Per week: ~60 triage calls (short) + ~3–12 draft calls (a draft prompt with 3 exemplars ≈ 3–5k input tokens, ~600 output).
- Triage on Flash-Lite: trivial — well under ₹1/week.
- Drafting on Flash: order of a few US cents/week.
- **Free tier covers the MVP outright.** Even at paid Standard rates this is **cents per week**; the dominant "cost" is your build time, not tokens. Batch/Flex pricing (−50%) exists if you ever scale. Set a billing alert anyway (§14 security).

### 10.7 AI failure handling (maps to §7 F4–F5)
| Failure | Handling |
|---------|----------|
| Timeout / 5xx | exp. backoff, 3 retries, then skip run gracefully |
| 429 rate limit | backoff honoring `retry-after`; if persistent, skip run |
| Invalid JSON | one `repair.md` re-ask; then skip |
| Incomplete/blank body | treat as invalid; redraft once; then skip |
| Fabricated fact detected | strip/redraft once; if unfixable, **do not send** |
| API key missing/invalid | fail fast at startup with a clear log; never run half-configured |

### 10.8 Where AI is deliberately NOT used `[REC — anti-overengineering]`
- Deciding *whether* a Telegram update is a note (deterministic filter).
- Dedup, scheduling, routing, button handling, length checks, source verification — all plain code. The model writes and classifies; **code decides and gates.**

---

## 11. Automation Architecture

### Triggers
- **T1 Webhook (event):** `POST /telegram/<secret>` receives updates; handler processes only `channel_post` from the capture chat id. `[DOC]`
- **T2 Schedule (time):** PTB `JobQueue` job at Mon/Wed/Fri 07:30 IST runs the draft pipeline. `[REC — G1]`
- **T3 Callback (event):** inline-button presses (`approve|edit|discard:<draft_id>`) hit the callback handler. `[REC]`

### Actions (mapped to the Components Map columns) `[DOC]`
| Map column | Actor | Concrete action |
|------------|-------|-----------------|
| Trigger | Telegram / Schedule | channel_post arrives / cron slot fires |
| Input | Telegram | note text → `notes` |
| Context | Google News RSS | fetch topical current items |
| Processing | Service (code) | triage select, exemplar pick, validate, gate |
| AI | Gemini | classify + draft |
| Output | Telegram (Review Gate) | draft + Approve/Edit/Discard to Meera |
| **Human gate** | **Meera** | **approves; posts to LinkedIn herself** |

### Conditions
- Draft only if ≥1 `new` note scores above threshold. 
- Attach news only if a fetched item is relevant. 
- Send at most **one** draft per scheduled slot (protect her attention; 3 slots/week = 3 drafts). `[REC]`

### Webhooks vs polling
- **Local dev:** long-polling (no public URL needed). 
- **Prod:** webhook (platform gives HTTPS). Include a secret path + Telegram `secret_token` header check so only Telegram can post updates. `[REC — NFR6]`

### Failure handling (system-level, complements §7/§10.7)
- Every external call wrapped; failures logged with context and never crash the process.
- Idempotency: `notes.tg_message_id` UNIQUE, `runs.slot` UNIQUE.
- Self-healing cadence: a failed slot simply retries content next slot; unsent drafts remain `pending_review` and are re-delivered.
- Heartbeat: each run writes a `runs` row; a weekly log line summarizes outcomes. Optional: on `error` outcome, one quiet message to the review chat.

### The two-chat delivery detail `[REC — resolves C1]`
- **Capture channel** (`-100…`): bot is admin (Post Messages), reads `channel_post`. Meera posts notes here.
- **Review chat:** a **private 1:1 chat between Meera and the bot** (she taps the bot and presses Start once) *or* a small private "Drafts" group with the bot. Drafts + buttons go here. Keeping review separate from capture keeps the capture channel a clean idea-inbox and gives the bot a place it can freely send messages. Config: `TELEGRAM_REVIEW_CHAT_ID`.

---

## 12. Claude Code Build Plan

Ten small phases. **Build and test one at a time.** Each phase lists Objective, Files, Implementation, the Claude Code instruction (full prompts in §13), Expected result, Verification, Common failure modes, and a "Do not proceed until" gate.

> **Golden rule:** never let Claude Code build two phases in one prompt. After each phase, run the verification and read the diff before moving on. This is how you avoid a 2,000-line blob you can't debug — exactly the failure the case warns about with the end-to-end tools.

### Target project structure (end state)
```
skinstinct-content-engine/
├── README.md
├── CLAUDE.md                 # coding standards (provided separately)
├── requirements.txt
├── .env.example              # names only, no secrets
├── .gitignore
├── config.py                 # env loading, model names, schedule
├── db.py                     # SQLite access, migrations
├── app.py                    # entrypoint: bot, webhook/polling, jobqueue
├── ingest.py                 # channel_post → notes
├── triage.py                 # Gemini classify/score
├── news.py                   # Google News RSS fetch + filter
├── draft.py                  # Gemini draft + validation
├── review.py                 # send draft + buttons; callbacks; edit flow
├── gemini_client.py          # thin wrapper: retries, JSON parse, repair
├── prompts/  triage.md  draft.md  repair.md
├── corpus/   piece_01.txt … piece_15.txt   corpus_index.json
└── tests/    test_*.py  fixtures/
```

### PHASE 0 — Environment & prerequisites (mostly you, not Claude Code)
- **Objective:** keys and accounts ready before any code.
- **Do:** complete Doc B (Telegram: channel, bot via BotFather, bot as admin *Post Messages only*, Chat ID `-100…`). Get a **Gemini API key** (Google AI Studio). Start a 1:1 chat with your bot and get your **review chat id** (message @userinfobot / your own id). Install Python 3.12+, `git`, and Claude Code.
- **Expected result:** you hold `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (capture, `-100…`), `TELEGRAM_REVIEW_CHAT_ID`, `GEMINI_API_KEY`.
- **Verification:** `curl https://api.telegram.org/bot<token>/getMe` returns your bot. 
- **Failure modes:** Chat ID positive (wrong — must be `-100…`); bot not admin (won't see posts).
- **Do not proceed until:** all four secrets exist and `getMe` works.

### PHASE 1 — Scaffolding & config
- **Objective:** repo, deps, config, secret-loading, empty modules, no logic.
- **Files:** all of the tree above as stubs; `requirements.txt`, `.env.example`, `.gitignore`, `config.py`.
- **Implementation:** `config.py` loads env via `python-dotenv`, exposes typed settings + model names + schedule, and **fails fast** if a required var is missing. `.gitignore` excludes `.env` and `*.db`.
- **Claude Code instruction:** §13 Prompt 1.
- **Expected result:** `python -c "import config"` prints loaded config (with secrets masked) or a clear error naming the missing var.
- **Verification:** run it with and without a var set.
- **Failure modes:** secrets committed (check `.gitignore` first); config silently defaulting.
- **Do not proceed until:** config loads and masks secrets; `.env` is git-ignored.

### PHASE 2 — Database layer
- **Objective:** `notes`, `drafts`, `runs` tables + typed access functions + idempotency.
- **Files:** `db.py`, `tests/test_db.py`.
- **Implementation:** create-tables-if-not-exist; functions `add_note`, `get_new_notes`, `set_note_status`, `add_draft`, `get_draft`, `set_draft_status`, `start_run/finish_run`. UNIQUE on `tg_message_id` and `runs.slot`. All parameterised queries.
- **Claude Code instruction:** §13 Prompt 2.
- **Expected result / verification:** `pytest tests/test_db.py` green; inserting a duplicate `tg_message_id` is a no-op, not a crash.
- **Failure modes:** string-formatted SQL (injection); missing UNIQUE; no persistent path.
- **Do not proceed until:** tests pass and duplicate insert is idempotent.

### PHASE 3 — Ingest (capture)
- **Objective:** store notes from `channel_post`; ignore everything else.
- **Files:** `ingest.py`, wire into `app.py` (polling for now), `tests/test_ingest.py`.
- **Implementation:** handler filters `channel_post` from `TELEGRAM_CHAT_ID`; extracts text (or caption); rejects empty; stores via `db.add_note`. **Voice notes** are stored with `content_type='voice'`, their `tg_file_id`, and `status='pending_transcription'` (transcribed in Phase 4, once the Gemini client exists). Other non-text → `unsupported`.
- **Claude Code instruction:** §13 Prompt 3.
- **Expected result:** posting a note in the capture channel inserts one `notes` row; re-delivery doesn't duplicate.
- **Verification:** run `app.py` in polling mode; post 3 notes; check DB has 3 rows; a sticker adds none/`unsupported`.
- **Failure modes:** filtering `message` instead of `channel_post` (nothing arrives); bot not admin; not masking token in logs.
- **Do not proceed until:** real notes land in the DB from your phone.

### PHASE 4 — Gemini client wrapper
- **Objective:** one reliable place for model calls: JSON schema, retries, repair, timeouts.
- **Files:** `gemini_client.py`, `prompts/repair.md`, `tests/test_gemini_client.py` (mock the SDK).
- **Implementation:** `transcribe_audio(audio_bytes, mime_type, model) -> str` for voice notes (wired into ingest so `pending_transcription` notes become `new`); `generate_json(prompt, schema, model)` → dict; exponential backoff on 5xx/429/timeout (3×); on JSON parse failure, one `repair.md` re-ask; raise a typed error on final failure. Never logs the key.
- **Claude Code instruction:** §13 Prompt 4.
- **Expected result / verification:** unit tests with a mocked SDK cover success, retry-then-success, invalid-JSON-then-repair, hard-fail. A tiny live smoke script returns JSON from Gemini.
- **Failure modes:** using the deprecated `google-generativeai` SDK; blocking calls freezing the async bot (run model calls in a thread executor); infinite retry.
- **Do not proceed until:** mocked tests pass and one real call returns valid JSON.

### PHASE 5 — Triage
- **Objective:** score `new` notes, pick the best, filter junk.
- **Files:** `triage.py`, `prompts/triage.md`, `tests/test_triage.py`.
- **Implementation:** `score_notes(notes) -> list[TriageResult]` via Flash-Lite + schema; `pick_best(results, threshold)`; persists `category`/`score`. "Nothing-new" notes score low.
- **Claude Code instruction:** §13 Prompt 5.
- **Expected result:** given the 5 sample notes (Doc E), batch-14 / cold-pressed / layering-order rank high; the clean-beauty "no new angle" note ranks low.
- **Verification:** a fixture test asserting that ordering; threshold filters correctly.
- **Failure modes:** model returns prose not JSON (schema fixes it); everything scores 0.9 (tighten the rubric in `triage.md`).
- **Do not proceed until:** ranking on the sample notes matches expectation and low-value notes are filtered.

### PHASE 6 — Corpus + drafting (the heart)
- **Objective:** voice-matched draft from a note using retrieved exemplars, with validation.
- **Files:** `corpus/` (15 files + index — you paste the published pieces from Doc C), `draft.py`, `prompts/draft.md`, `tests/test_draft.py`.
- **Implementation:** `prompts/voice_skill.md` — Meera's voice skill distilled from `corpus/` (structure, stance, register, negative rules); `load_corpus()`; `pick_exemplars(category, n=3)`; `make_draft(note, category, exemplars, news=None) -> Draft`; then **validators** (length, source-URL match, no-invented-stats, self_check) with one redraft on failure and fail-safe skip.
- **Claude Code instruction:** §13 Prompt 6.
- **Expected result:** a draft that reads like Meera — concrete opening, patient paragraphs, understated close, no emoji/hashtags, no invented numbers.
- **Verification:** generate drafts for the 5 sample notes; run the **voice checklist** (§14 AI tests) by eye and with an automated "no emoji/no hashtag/length/invented-stat" test. Compare against the 15 originals.
- **Failure modes:** generic LinkedIn-guru tone (add stronger negative instructions + more exemplars); invents a statistic (validator must catch — test it deliberately); too long/short.
- **Do not proceed until:** at least 3 of 5 sample drafts pass the voice checklist and **zero** contain invented facts.

### PHASE 7 — Review gate (Telegram delivery + buttons)
- **Objective:** send draft to review chat; handle Approve/Edit/Discard; edit flow.
- **Files:** `review.py`, callbacks in `app.py`, `tests/test_review.py`.
- **Implementation:** `send_for_review(draft)` → message + inline keyboard `Approve|Edit|Discard` carrying `draft_id`; callback verifies sender == Meera; Approve → status + copy-friendly text; Discard → shelve; Edit → capture next reply (verbatim or one-line-instruction redraft). 
- **Claude Code instruction:** §13 Prompt 7.
- **Expected result:** a draft appears in your review chat; each button does the right DB transition; Approve returns clean copy-paste text.
- **Verification:** manual on your phone: Approve, Edit (both modes), Discard; check DB transitions; press a button from another account → rejected.
- **Failure modes:** callback data too long (keep it `action:id`); buttons actable by anyone (must check user id); lost edit context after restart (persist pending-edit state in DB).
- **Do not proceed until:** all three actions work from your phone and only Meera can act.

### PHASE 8 — Orchestration & schedule
- **Objective:** wire triage → news → draft → review into one scheduled job; idempotent.
- **Files:** `app.py` (JobQueue), a `run_pipeline()` in a new `pipeline.py`, `news.py` (basic), `tests/test_pipeline.py`.
- **Implementation:** job at Mon/Wed/Fri 07:30 IST: `start_run(slot)` (skip if slot done) → get new notes → triage/pick → (news optional) → draft+validate → `send_for_review` → `finish_run`. One draft per slot. Handles "no candidate" and errors per §7.
- **Claude Code instruction:** §13 Prompt 8.
- **Expected result:** a manual `run_pipeline()` on demand produces exactly one review draft or a clean "no candidate" outcome.
- **Verification:** trigger the job manually (a `/run` admin command guarded to Meera's id); confirm one `runs` row and one draft; run twice on same slot → second is a no-op.
- **Failure modes:** double-fire after redeploy (slot UNIQUE guards it); blocking the event loop; news failure aborting the run (must degrade).
- **Do not proceed until:** end-to-end (note in → draft in review chat) works on demand and is idempotent per slot.

### PHASE 9 — Testing pass & hardening
- **Objective:** the §14 test suite green; failure paths exercised.
- **Files:** everything under `tests/`, plus logging polish.
- **Claude Code instruction:** §13 Prompt 9.
- **Expected result / verification:** `pytest` green; deliberately break Gemini (bad key) and RSS (bad URL) and confirm graceful degradation, not a crash.
- **Do not proceed until:** all tests pass and both AI-down and RSS-down paths are proven graceful.

### PHASE 10 — Deployment
- **Objective:** run always-on on Railway/Render with webhook + persistent SQLite.
- **Files:** `Procfile`/start command, platform config, README deploy notes.
- **Claude Code instruction:** §13 Prompt 10 (+ manual steps in §15).
- **Expected result:** notes posted from your phone are captured by the deployed service; scheduled drafts arrive; buttons work.
- **Verification:** the §16 production checklist.
- **Do not proceed until:** §16 passes and you've watched one real scheduled draft arrive.

---

## 13. Copy-Paste Claude Code Prompts

Use these **one at a time**, in order. Each assumes the previous phase is committed. Preamble to paste **once** at the very start of the session:

> **Session preamble (paste first):**
> "We're building the Skinstinct Content Engine described in `PLAN.md` and following the rules in `CLAUDE.md`. Read both before writing any code. Work **one phase at a time**; do not start the next phase until I say so. After each change, show me the diff and how to verify it. Prefer small, readable functions; type hints; no secrets in code; graceful error handling; parameterised SQL. If a library's API differs from what you expect, check the installed version's docs/`--help` before guessing. Ask me before adding any dependency not already in `requirements.txt`."

**Prompt 1 — Scaffolding**
> "Phase 1 only. Create the project structure listed in PLAN.md §12 as stubs (empty functions with docstrings and `# TODO`), plus `requirements.txt` (python-telegram-bot v22.x, google-genai, python-dotenv, feedparser, pytest), `.gitignore` (ignore `.env`, `*.db`, `__pycache__`), `.env.example` (variable NAMES only: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_REVIEW_CHAT_ID, GEMINI_API_KEY, plus model names and schedule), and `config.py` that loads env with python-dotenv, exposes typed settings + model names (`gemini-3.5-flash`, `gemini-3.5-flash-lite`) + schedule, masks secrets in its `__repr__`, and raises a clear error naming any missing required var at import. Don't implement other modules yet. Show me `config.py` and how to test it loads."

**Prompt 2 — Database**
> "Phase 2 only. Implement `db.py` using the stdlib `sqlite3` module against a file path from config. Create tables `notes`, `drafts`, `runs` exactly as specified in PLAN.md §9 (UNIQUE on `notes.tg_message_id` and `runs.slot`; FK draft→note). Provide the access functions listed in §12 Phase 2 with type hints and parameterised queries only. `add_note` must be idempotent on `tg_message_id`. Write `tests/test_db.py` covering insert, duplicate-insert no-op, status transitions, and run idempotency. Run pytest and show results."

**Prompt 3 — Ingest**
> "Phase 3 only. Implement `ingest.py` and wire a handler into `app.py` running in **long-polling** mode for now. The handler must process only `channel_post` updates whose chat id equals `config.TELEGRAM_CHAT_ID`, extract text or caption, reject empty, store via `db.add_note` (content_type `text`, else `unsupported`). Never log the bot token. Add `tests/test_ingest.py` with a fake update object. Tell me exactly how to run the bot and verify a note from my phone lands in the DB."

**Prompt 4 — Gemini client**
> "Phase 4 only. Implement `gemini_client.py` using the **google-genai** SDK (not the deprecated google-generativeai). Expose `generate_json(prompt: str, schema: dict, model: str) -> dict` that: runs the (blocking) SDK call in a thread executor so it doesn't block the async bot; requests JSON via response schema; parses the result; on parse failure re-asks once using `prompts/repair.md`; retries 5xx/429/timeout with exponential backoff (max 3); raises a typed `GeminiError` on final failure; never logs the API key. Create `prompts/repair.md`. Write `tests/test_gemini_client.py` mocking the SDK for success, retry-then-success, invalid-JSON-then-repair, and hard-fail. Also give me a 10-line `smoke_gemini.py` I can run once to confirm a real call returns JSON. Check the installed google-genai version's usage before coding."

**Prompt 5 — Triage**
> "Phase 5 only. Implement `triage.py` and `prompts/triage.md`. `score_notes(notes)` calls Gemini (`config.TRIAGE_MODEL`) via `gemini_client.generate_json` with a schema `{worth_developing: bool, score: number, category: string, reason: string, suggested_angle: string}`. `pick_best(results, threshold)` returns the top note above threshold or None. Persist category/score. The prompt must score LOW when a note says it has nothing new to add, and HIGH for notes with a concrete incident plus a claim. Add `tests/test_triage.py` using the 5 sample notes in `tests/fixtures/sample_notes.txt` (I'll paste them) and assert the clean-beauty note ranks below the batch-14 note. Mock Gemini in tests. Show me results."

**Prompt 6 — Drafting**
> "Phase 6 only. First, confirm `corpus/` contains 15 `.txt` files and `corpus_index.json` mapping id→{category,title,path} (I've added them). Implement `draft.py` + `prompts/draft.md`. `load_corpus()`, `pick_exemplars(category, n=3)` (same-category first, fall back to any), `make_draft(note, category, exemplars, news=None)` calling `config.DRAFT_MODEL` with schema `{body: string, used_source_url: string|null, self_check:{invented_stats: boolean, on_voice: boolean}}`. Then implement validators in code: body length 200–3000 chars; if `used_source_url` set it must be in the provided news URLs else strip that sentence; reject if `self_check.invented_stats` true or if a stat/%/study/year-claim in the body isn't traceable to the note or source; on any rejection redraft once, then fail safe (return None). The draft prompt must instruct the model to mirror the exemplars' structure and register (concrete opening scene; patient paragraphs; the 'I'm not saying X, I'm saying Y' move; understated close; NO emoji, NO hashtags, NO CTA; British spelling) and use only facts in the note or provided source. Write `tests/test_draft.py` for the validators (including a deliberately fabricated-stat case that must be caught). Generate drafts for the 5 sample notes and print them so I can eyeball voice."

**Prompt 7 — Review gate**
> "Phase 7 only. Implement `review.py` and callback handling in `app.py`. `send_for_review(bot, draft)` sends the draft body to `config.TELEGRAM_REVIEW_CHAT_ID` with an inline keyboard [Approve|Edit|Discard], callback_data `action:draft_id`. The callback handler must reject presses whose `from_user.id` != `config.MEERA_USER_ID`. Approve → set status approved, reply with the body in a monospace/copy-friendly block prefixed 'Approved — copy and post to LinkedIn'. Discard → status discarded + shelve note. Edit → reply asking for either a pasted rewrite or a one-line instruction; capture Meera's next message in the review chat against the pending draft (persist pending-edit state in the DB so it survives restart); a pasted rewrite is stored verbatim as a new revision, an instruction triggers one redraft. Add `tests/test_review.py` with fake callback objects. Tell me how to test all three actions from my phone."

**Prompt 8 — Orchestration & schedule**
> "Phase 8 only. Implement `pipeline.py::run_pipeline(slot)` chaining: start_run(slot) — skip if that slot already has a run — → get new notes → triage.score+pick → optionally news.fetch (must degrade to None on any error) → draft.make_draft+validate → review.send_for_review → finish_run(outcome). Exactly one draft per slot; handle 'no candidate' and errors per PLAN.md §7 (log, never crash, quiet notice to review chat on error). In `app.py`, register a PTB JobQueue job Mon/Wed/Fri 07:30 Asia/Kolkata calling run_pipeline, and add an admin-only `/run` command (guarded to MEERA_USER_ID) to trigger a slot on demand. Add `tests/test_pipeline.py` mocking the stages, including the idempotent-slot and news-down cases. Show me how to run one pipeline manually."

**Prompt 9 — Testing & hardening**
> "Phase 9 only. Do not add features. Review the whole codebase against CLAUDE.md. Fill test gaps so we cover: ingest filtering, DB idempotency, gemini retry/repair, triage ranking, draft validators (esp. fabricated-stat rejection), review authorization, pipeline idempotency and graceful degradation when Gemini or RSS fail. Add structured logging (module, event, note/draft id; never secrets) and a startup self-check that verifies all required env vars and a Telegram getMe. Run the full suite and show me the report. List any risks you couldn't test."

**Prompt 10 — Deployment prep**
> "Phase 10 only. Prepare for always-on deployment on Railway (or Render) in webhook mode. Add: a start command / Procfile; switch `app.py` to use webhook when `PUBLIC_URL` is set (with a secret path segment and Telegram `secret_token` header verification) and fall back to polling locally; ensure SQLite uses a path under a persistent volume from config; a `/healthz` route; and README deploy steps. Do NOT put secrets in any file. List the exact env vars I must set in the platform dashboard and the exact command to register the Telegram webhook after first deploy."

---

## 14. Testing Plan

### Unit tests (logic in isolation)
- **Config:** missing var raises; secrets masked.
- **DB:** insert, duplicate no-op, status transitions, run-slot idempotency.
- **Gemini client:** success / retry-then-success / invalid-JSON-then-repair / hard-fail (SDK mocked).
- **Triage:** JSON shape; the sample-note ranking (batch-14 > clean-beauty); threshold filter.
- **Draft validators:** length bounds; **fabricated-stat rejection** (feed a body with "a 2026 study of 5,000 women found…" and assert it's caught); source-URL-must-match; emoji/hashtag rejection.
- **Review:** only Meera's user id can act; each action → correct DB transition.
- **Pipeline:** one draft per slot; idempotent slot; news-down and Gemini-down degrade gracefully.

### Integration tests (services together, mocked externally)
- Ingest handler + DB (fake `channel_post` → row).
- Pipeline with mocked Gemini + mocked RSS + real DB → one `pending_review` draft.
- Review callback + DB round-trip.

### End-to-end (manual, real accounts — the ones that actually matter)
1. Post a note from your phone → appears in DB.
2. Trigger `/run` → a draft arrives in the review chat.
3. Approve → copy-paste text returned; DB `approved`.
4. Edit (paste) and Edit (instruction) → new revision; Approve.
5. Discard → shelved.
6. Post nothing for a slot → "nothing ready to draft" path.

### AI tests (the highest-value, least-standard part) `[REC]`
Build a tiny **eval harness** (`tests/eval_voice.py`, run manually, not in CI):
- **Valid output:** 5 sample notes → 5 drafts; each scored against a **voice checklist**: concrete opening (not a cliché hook)? patient multi-sentence paragraphs? at least one "I'm not saying X, I'm saying Y"-type concession? understated close (no "DM me"/CTA)? no emoji/hashtags? British spelling? only facts from the note? **Target ≥ 4/5 drafts pass.**
- **Invalid/junk input:** feed a two-word note and a "nothing new" note → assert triage filters them (no draft).
- **Missing information:** note references a stat vaguely ("returns went up") → draft must not invent a precise number.
- **Hallucination probe:** run drafting 10× on the same note with news disabled → assert **zero** invented statistics/studies across all 10 (this is the brand-existential test).
- **Edge cases:** very long note; note that's already basically a finished post; note in a mix of English/Hindi; note that's just a URL.
- **Prompt-regression guard:** keep the 5 sample notes + their last-known-good drafts; when you change `draft.md`, re-run and diff — catch voice regressions before they ship.

### Security tests
- **AuthZ:** button presses / `/run` from a non-Meera account are rejected.
- **Secrets:** grep the repo and logs for the token/key → none present; `.env` git-ignored; keys only in platform secret store.
- **Input validation:** oversized note, non-text payloads, malformed update → handled, no crash.
- **Webhook abuse:** requests to the webhook without the correct secret path + `secret_token` header are rejected.
- **Injection:** all SQL parameterised (no f-string SQL); note text is never `eval`'d or shell-interpolated.
- **Data exposure:** error messages sent to Telegram never include stack traces or secrets.
- **Rate/abuse:** a flood of notes can't trigger a flood of model calls (drafting is batched on the schedule, capped at one draft/slot).

### Performance (bottlenecks & how to test)
- The only latency that matters is a scheduled job (Meera isn't waiting live). Time `run_pipeline` end-to-end; a Gemini draft call is the dominant cost (seconds) — fine.
- Ensure model calls run off the event loop (thread executor) so ingest/buttons stay responsive during a draft job.
- SQLite is trivially fast at this scale; no load testing needed for MVP.

---

## 15. Deployment Plan

**What Claude Code does vs. what you do manually** is marked on each step.

1. **Repository — [you + CC].** `git init`, first commit after each green phase. Push to a **private** GitHub repo (it will reference secrets by name only). CC writes `.gitignore`; you create the repo.
2. **Environment variables — [you].** In Railway/Render dashboard set: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (`-100…`), `TELEGRAM_REVIEW_CHAT_ID`, `MEERA_USER_ID`, `GEMINI_API_KEY`, `PUBLIC_URL` (the service's URL), `WEBHOOK_SECRET`, `DB_PATH` (under the mounted volume), model names. Never in code.
3. **Database — [CC + you].** SQLite on a **persistent volume** (Railway volume / Render disk). CC points `DB_PATH` there; you attach the volume in the dashboard. (Or provision managed Postgres and set `DATABASE_URL` — CC's `db.py` swap.)
4. **API configuration — [you].** Confirm the Gemini key is active in AI Studio; set a **billing budget alert** even though you'll be on free tier.
5. **AI keys — [you].** Only in the platform secret store.
6. **External integrations — [you].** RSS needs nothing. Confirm the bot is still channel admin (Post Messages) and you've pressed Start in the review chat.
7. **Build — [CC + platform].** `requirements.txt` drives install; start command from the Procfile.
8. **Deploy — [you + platform].** Connect the GitHub repo; platform builds and runs. Watch logs for the startup self-check (env + getMe).
9. **Domain — [n/a].** Use the platform-provided HTTPS URL; no custom domain needed for MVP.
10. **HTTPS — [platform].** Provided automatically — required for the Telegram webhook.
11. **Register webhook — [you, once].** After first deploy: `curl "https://api.telegram.org/bot<token>/setWebhook?url=<PUBLIC_URL>/telegram/<WEBHOOK_SECRET>&secret_token=<WEBHOOK_SECRET>"`. Verify with `getWebhookInfo`.
12. **Logging — [platform].** Structured stdout is captured by the platform's log viewer.
13. **Monitoring — [CC].** `/healthz` route + weekly run summary line; optional error ping to the review chat.
14. **Migrations — [CC].** `db.py` runs create-if-not-exists on boot; for schema changes, a small numbered migration function. (Postgres: same pattern or a light tool later.)
15. **Rollback — [you + platform].** Keep each phase as a tagged commit; platforms redeploy a previous commit in one click. If a deploy misbehaves, `deleteWebhook`, redeploy the last-good commit, re-`setWebhook`.

**Deploy sequence in practice:** run locally with polling through Phase 9 → create private repo + host project → set env vars + volume → deploy → switch to webhook + register it → post a test note → trigger `/run` → watch a draft arrive. Only then let the schedule run unattended.

---

## 16. Production Readiness Checklist

Do not call it live until every box is true:

- [ ] `.env` git-ignored; no token/key anywhere in the repo or logs (grep-verified).
- [ ] All API keys only in the platform secret store; Gemini billing alert set.
- [ ] Startup self-check passes (all env vars present; Telegram `getMe` OK).
- [ ] Bot is channel admin with **Post Messages only**; capture chat id is `-100…`.
- [ ] Webhook uses HTTPS + secret path + `secret_token` header; unauthenticated calls rejected.
- [ ] Only `MEERA_USER_ID` can press buttons or run `/run` (authZ tested).
- [ ] Ingest is idempotent (duplicate `message_id` → no dupes).
- [ ] Cadence is idempotent (a slot never drafts twice; verified across a redeploy).
- [ ] Gemini-down and RSS-down paths degrade gracefully — no crash, quiet notice, retry next slot (tested by breaking each).
- [ ] Draft validators catch invented stats and unmatched source URLs (tested with a deliberately fabricated draft).
- [ ] Voice eval: ≥ 4/5 sample drafts pass the checklist; hallucination probe returns **zero** invented facts over 10 runs.
- [ ] SQLite on a persistent volume (survives redeploy) — or Postgres provisioned.
- [ ] No public publishing anywhere; review gate is the terminal step (BR1 upheld).
- [ ] Errors surfaced to Telegram never contain stack traces or secrets.
- [ ] `pytest` green; README documents run + deploy + webhook registration.
- [ ] Backup: repo is the source of truth; note the DB is the only stateful piece and where its volume lives.

---

## 17. Exact Next Action

**Do this first — before any code:**

1. **Complete the Telegram setup from Doc B (≈15 min).** Create the private capture channel, create the bot via @BotFather, add the bot as **admin with Post Messages only**, and get the capture **Chat ID** (`-100…`). Then open a 1:1 chat with your bot, press **Start**, and get **your own Telegram user id** and that **review chat id** (via @userinfobot).
2. **Get a Gemini API key** from Google AI Studio.
3. **Confirm you hold all five values:** `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (capture, `-100…`), `TELEGRAM_REVIEW_CHAT_ID`, `MEERA_USER_ID`, `GEMINI_API_KEY`. Sanity-check the bot: `curl https://api.telegram.org/bot<token>/getMe`.
4. **Create the project folder and drop in two files:** this `PLAN.md` and the `CLAUDE.md` (delivered alongside). `git init`.
5. **Prepare the corpus:** create `corpus/` and paste each of the 15 published pieces from Doc C into `piece_01.txt … piece_15.txt`, and write `corpus_index.json` (id → category/title/path). *(This is the one manual data step; the whole voice quality rests on it, so don't skip or shortcut it.)*
6. **Open Claude Code in that folder, paste the Session preamble (§13), then Prompt 1.** Verify Phase 1, commit, and only then move to Prompt 2.

**Concretely, your very first keystroke:** open Telegram and start Doc B's Section 02 (create the capture channel). Everything else is blocked on having the bot token and chat IDs.

Do **not** start by asking Claude Code to "build the app." Start by holding the four secrets and the corpus, then drive it phase by phase with the prompts above — testing after each — exactly the discipline that separates a working pipeline from the end-to-end tools Meera already rejected.




