# Publishability rubric (v2)

How every note or voice transcript is scored before anything is drafted. The model scores; **code** verifies evidence, applies guardrail caps, computes the weighted overall score and makes the decision.

## 1. Research (September 2026)

Labels: **[RESEARCH]** = found in a source (quality noted), **[ASSUME]** = my inference, **[REC]** = my recommendation.

### What makes founder content credible
- **[RESEARCH, primary]** The Edelman–LinkedIn B2B Thought Leadership reports (2021–2025) found most decision-makers rate the thought leadership they read as mediocre; only about 13% call it very good or excellent. The shortcomings they name: *too focused on selling, lacking original thinking, written by non-experts, a staid corporate tone, and thin evidence or poor data.* Low-quality thought leadership made some organisations **lose** respect: 45% of decision-makers, 53% of C-level executives. ([Edelman 2021](https://www.edelman.com/expertise/business-marketing/2021-b2b-thought-leadership-impact-study), [2024 report PDF](https://www.edelman.com/sites/g/files/aatuss191/files/2024-02/_2024%20Edelman-LinkedIn%20B2B%20Thought%20Leadership%20Impact%20Report%20Final.pdf))
- **[RESEARCH, secondary]** 2026 practitioner guides agree that polished AI posts are now cheap, so the scarce input is a *defensible point of view with a lived source*: a customer objection, a product decision, a pattern noticed. ([Gallium](https://gallium.ai/blog/founder-led-linkedin-content-strategy-2026), [Blueberry Media](https://blueberry-media.co.uk/blog/founders-guide-linkedin-2026))
- **[RESEARCH, secondary]** Personal profiles get markedly more engagement than brand pages (figures vary by source), and stories that combine expertise with personal experience hold attention longer. ([Hootsuite](https://blog.hootsuite.com/linkedin-algorithm/), [SocialBee](https://socialbee.com/blog/linkedin-algorithm/))

### What LinkedIn itself rewards and punishes (secondary sources; LinkedIn doesn't publish the algorithm)
- **[RESEARCH, secondary]** Distribution favours dwell time and topical expertise ("does this person reliably say useful things about X") over follower count. Educational posts with frameworks or data travel beyond the author's own network. ([Hootsuite](https://blog.hootsuite.com/linkedin-algorithm/), [Dataslayer](https://www.dataslayer.ai/blog/linkedin-algorithm-february-2026-whats-working-now))
- **[RESEARCH, secondary]** Engagement bait ("Agree?", "Comment YES", "Tag someone") is detected and demoted; recycled templates and pods are deprioritised. ([LinkedIn Post Preview](https://linkedinpreview.com/blog/linkedin-engagement-bait-2026), [Expert LinkedIn](https://expertlinked.in/posts/2026-02-10-linkedin-authenticity-algorithm-shift/))

### Strong founder patterns, which score high
Lessons from building; customer insights; product and formulation observations; founder challenges; contrarian but *defensible* views; observations backed by her own data; educational explainers; practical frameworks; industry commentary with her own perspective; story-driven posts about something that actually happened.

### Weak patterns, which score low
Generic motivation; generic AI-style advice; self-promotion; unsupported claims; engagement bait; manufactured controversy; corporate jargon; recycled ideas (including repeats of her own published pieces); news summaries with no perspective of hers; content unrelated to her actual experience.

### What this means for Meera [ASSUME/REC]
Meera's traction came from posts that explain the chemistry and documentation behind claims (see `corpus/`). The goal is **credibility with a specific audience** (28–40 urban Indian women, plus industry peers), not virality. So the rubric weights originality, authenticity, credibility and postability (the absence of anything that would have to be invented) above platform-fit signals.

## 2. The 10 parameters

Each parameter gets a score from 0 to 10, a reason, a **verbatim evidence quote from the transcript**, the gap (what's missing), and a guardrail status (pass / warn / fail).

| # | Parameter | Question | Weight | Guardrail: what forces warn or fail |
|---|---|---|---:|---|
| 1 | Founder Relevance | Does it come from Meera's own role, formulation expertise, business or journey? | 1.0 | Fail if the topic is outside her expertise and presented as authority |
| 2 | Audience Relevance | Does it matter to her audience (Indian skincare buyers, industry peers)? | 1.0 | Warn if it only matters to a niche with no link to her audience |
| 3 | Originality | Is there an observation or angle that isn't generic, and isn't a repeat of her published pieces? | 1.5 | Fail if it restates a published piece with nothing new, or is generic advice |
| 4 | Insight Depth | Is there a mechanism or a "why", not just a statement? | 1.0 | Warn if it's surface-level |
| 5 | Authenticity | Did she experience, observe, test or believe this? | 1.5 | Fail if it can't reasonably be attributed to Meera |
| 6 | Practical Value | Does the reader get a lesson, check or principle? | 1.0 | Warn if there is no takeaway |
| 7 | Credibility | Is it supported by her experience, evidence or reasoning? | 1.5 | Warn for a sweeping claim without support; fail for unsupported factual or statistical claims |
| 8 | LinkedIn Fit | Would it make a natural founder-led LinkedIn post? | 0.5 | Warn if it only works as bait or controversy |
| 9 | Discussion Potential | Could it start thoughtful professional discussion without bait? | 0.5 | Fail if discussion depends on attacking someone |
| 10 | Postability | Could it stand alone **without inventing** missing facts, stories or numbers? | 1.5 | Fail if a post would need invented material |

**Why these weights [REC]:**
- **1.5 each** for Originality, Authenticity, Credibility and Postability. These are the documented reasons thought leadership fails (no original thinking, non-expert authorship, thin evidence), plus this product's core rule: never invent.
- **0.5 each** for LinkedIn Fit and Discussion Potential. They're platform-optimisation signals, and the brief says not to optimise for virality.
- **1.0** for everything else.
- Weights live in code (`triage.PARAMETERS`), so any change is one edit plus a re-run of the evals.

## 3. How code turns this into a decision
1. **Validate** that the model returned all 10 parameters, each exactly once, with scores from 0 to 10. Otherwise one repair attempt, then the note is retried later.
2. **Verify evidence.** A parameter's evidence quote must actually appear in the transcript (at least 80% of its words, in order-insensitive token overlap). If it doesn't, that parameter is **capped at 5**, because the model can't score something it can't point to.
3. **Guardrail caps.** Warn caps a parameter at **6**; fail caps it at **2**. A violation can never earn a high score.
4. **Overall** = Σ(weight × capped score) ÷ Σ(weights), rounded half-up to one decimal. Reproducible from the scorecard.
5. **Hard guardrails** are raised by the model with a quote, or by code for emails, phone numbers and long ID numbers. They cover:
   - fabricated information, unsupported factual claims, fake statistics, invented experiences
   - confidential business information, private customer information, sensitive personal information
   - defamation, unverified accusations, misleading claims, plagiarism
   - anything not attributable to Meera or not fit to share publicly

   **Any hard flag → HUMAN REVIEW REQUIRED, whatever the score.**
6. **Decision:**
   - hard flag → `HUMAN REVIEW REQUIRED — GUARDRAIL`
   - otherwise overall **> 8.0** → `QUALIFIED FOR DRAFT`
   - otherwise → `REJECTED — SCORE TOO LOW`

   **Exactly 8.0 does not qualify.**
7. **A low-confidence transcript** (the model rates the audio as unclear, or more than 20% of the words are marked `[unclear]`) goes to human review before triage. We never score or draft from words we can't trust.

## 4. When a note doesn't qualify (8.0 or below)
The scorecard lists the weakest parameters and what would strengthen them. A third message suggests **3 topics that suit Meera specifically**:
- They're drawn from her content areas and current credible headlines, and never repeat a published piece.
- Each is phrased as a question about her own experience, so nothing is invented for her.
- Any linked headline must be one that was actually fetched.

Suggestions are generated once and stored with the assessment. If generating them fails, the scorecard is still sent.

## 5. After a note qualifies
- **News:** a hook only from recent (14 days), relevant (keyword overlap) and credible (allowlisted publisher) Google News items. The draft records the headline, source, date, URL and *why it's relevant*. If nothing fits, there's no hook.
- **Draft QA, in two layers:**
  1. The existing code validators: invented numbers, research claims and timing; unverified sources; emoji, hashtags, links, calls to action, clichés and lists.
  2. A second model pass that lists every claim in the draft not supported by the transcript or the cited headline, and checks whether Meera's idea was changed.

  Any finding → one redraft with the findings → otherwise drop (fail closed).
- **Review:** Approve / Edit / Reject / Regenerate. Meera's final approved text is stored separately from the AI draft. Publishing stays manual: she posts it herself and can mark it "posted".
