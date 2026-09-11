# Engagement Rubric — what makes a good, engaging technical explainer

This is the growth agent's **standing quality standard**. It is the bottom-up definition of a
video worth watching, and the target the daily self-improvement loop optimizes. Read it every run
(playbook step 0), score the current output against it (via `run/rubric_review.py`), and improve the
**weakest high-leverage lever** one change at a time. You may refine this file as you learn what
works — but only with evidence (a render-and-judge win or a matured cohort), and say so in the report.

## How to use it each run
1. Render the golden set on `HEAD` with `run/rubric_review.py` and **score every lever** below as
   `2` (strong), `1` (weak), or `0` (broken) — by READING the extracted frames (vision) and the
   generated script / title / thumbnail hook. This is the baseline.
2. Pick the **weakest lever with the highest priority** (ties → higher priority number wins).
3. Make ONE focused change to that lever's prompt file, re-render the golden set, re-score.
4. **Ship only if the changed lever's score goes up and no other lever drops** (and regressions
   pass). Else discard/revert. This is the mandatory gate — never ship on faith.

Scoring is **relative** (before vs after on the same golden-set subjects), never absolute — the
judge is your own inspection, so consistency comes from comparing like-for-like.

## Priority order (attack weakest-first within this order)
For a small channel, discovery is gated by **CTR** and the **first 3 seconds**. So:
**R1 (hook) ≥ R8 (thumbnail/title) ≥ R2 (information gain) ≥ R6 (payoff) ≥ everything else.**

---

## The levers

### R1 — Hook (0–2 seconds)  ·  PRIORITY 1
**Good:** the very first frame shows the hook beat at t=0, and that on-screen text IS the first spoken sentence (the title hook) or a faithful ≤8-word compression of that same claim — Decolar lock. Repeating the title is required. No second typographic hook, no curiosity-gap slogan, no hook emoji as a second punch. The spoken line still voices the viewer's pain. No "In this video / Today / Welcome".
**Controls:** `app/services/engines/worker.py` `_generate_script` (opening line);
`app/services/engines/storyboard.py` `_lock_opening_hook` + `_system_prompt`; `app/services/thumbnail.py`
`_hook_text`; titles in `app/services/metadata.py` / `app/services/video_gen.py`.
**Self-review:** read frame `b0` — does it say the same claim as the first spoken sentence / title?
**Signal (later):** hook-hold = retention at the first decile; CTR.

### R2 — Information gain per second  ·  PRIORITY 3
**Good:** every beat adds information the narration *cannot say* — a diagram, code, a stat, an A/B
compare — not a text card echoing the spoken words. Varied beat types; at most 1–2 `statement` beats.
**Controls:** `storyboard._system_prompt`, `_variety_ok` (already enforces variety), the beat renderers.
**Self-review:** list the beat mix from the harness output — is it varied and explanatory, or mostly
`statement`? Do the frames show diagrams/code/stats, or just kinetic text?
**Signal:** `avg_view_pct`, retention slope (no steep decay).

### R3 — One idea per beat / mobile clarity  ·  PRIORITY 6
**Good:** ≤8-word headlines, ≤~30-char code lines, one concept per screen, readable on a phone,
high contrast, safe margins. Nothing clipped or overflowing.
**Controls:** `storyboard` renderers + `_system_prompt` (word/char clamps + adaptive code sizing).
**Self-review:** any clipped text, cramped layout, or two ideas fighting on one frame?
**Signal:** retention; comment confusion.

### R4 — Pacing synced to speech  ·  PRIORITY 7
**Good:** visuals land on the word being spoken (word-sync is built via `align_storyboard`); cadence
isn't frantic (too many beats) or draggy (a beat held too long). Script length matches format.
**Controls:** `storyboard.align_storyboard` (built), script length in `worker._generate_script`.
**Self-review:** play the final.mp4 mentally against the beat starts — do beats change roughly with
sentences? Any beat on screen far too long/short?
**Signal:** retention shape.

### R5 — Retention mechanics  ·  PRIORITY 5
**Good:** an open loop early ("but here's the catch…"), no mid-video sag, each section earns the next.
No filler.
**Controls:** `worker._generate_script`.
**Self-review:** read the script — does the middle keep a reason to stay, or does it flatten?
**Signal:** mid-roll retention.

### R6 — Payoff + memorable close  ·  PRIORITY 4
**Good:** the promised insight is actually delivered, and the last line is quotable / crystallizes the
lesson in one sentence. A `quote` or strong final `statement` beat carries it.
**Controls:** `worker._generate_script` (close), `storyboard` quote/cta beats.
**Self-review:** read the last sentence + final frames — is there a real payoff and a line worth
repeating, or does it just stop?
**Signal:** ending retention, rewatches.

### R7 — Call to action  ·  PRIORITY 8
**Good:** series endcard after the claim — spoken `Subscribe — next {series} {noun}.` (≤8 words) + on-screen chip `· {series}` (optional micro `same series` if it fits). Subscribe is ALLOWED only on that last slot. ZERO Subscribe text/VO/chip in the mid-short. ZERO Follow / Follow-tomorrow / Siga / amanhã / waitlist / owera.com / Cloud / “part 2 coming” / SMY / 💸 / neon on the card. Must not compete with frame0. Hold ≤4.0s.
**Controls:** `storyboard` cta beat (`_sanitize_cta`, `_cap_endcard`), `worker._generate_script` (`ensure_series_endcard_vo`), `app/services/craft.py`.
**Self-review:** last spoken line is the Subscribe VO; last card is the series chip, not a Follow ask or a second hook.
**Signal:** subscribers_gained.

### R8 — Thumbnail + title CTR  ·  PRIORITY 2
**Good:** thumbnail IS the title hook (first spoken sentence, or ≤8-word compression of that same claim). Repeating the title is required — no curiosity gap, no second slogan. Prefer the object of the angle (receipt, terminal, bill) over generic emoji. Title leads with the viewer's problem and carries `· <series> <nn>`.
**Controls:** `app/services/thumbnail.py`, `app/services/metadata.py`, `app/services/craft.py` title gate.
**Self-review:** read thumb + title together — same claim?
**Signal:** CTR, impressions (once measured).

### R9 — Audio  ·  PRIORITY 9
**Good:** clear voice, BGM ducked well under narration, no clipping, natural delivery.
**Controls:** `worker._mux` (BGM volume/duck), voice choice, BGM pool.
**Self-review:** (mostly a listen check) is the narration clearly above the music? Voice fits the niche?
**Signal:** retention (bad audio kills it).

### R10 — Brand / visual identity  ·  PRIORITY 10
**Good:** Owera Software = B&W brand; Rodrigo Recio = personal warm look. No shared neon. Accent still matches the thumbnail via `theme.resolve(..., brand=)`.
**Controls:** `app/services/engines/theme.py` brand palettes, `storyboard` backgrounds, `thumbnail._thumbnail_html`.
**Self-review:** is ch1 black/white and ch2 personal, not the old rainbow?
**Signal:** CTR, recall.

### R11 — Format fit  ·  PRIORITY 11
**Good:** shorts are punchy (fast hook, 1–2 key visuals, quick payoff); long-form is structured
(sections, deeper diagrams/code). The format matches the content's depth.
**Controls:** `content_format` handling in the script + storyboard prompts.
**Self-review:** does a short stay tight, and does a long-form earn its length?
**Signal:** `by_format` analytics.

---

## Notes
- Levers R1, R2, R3, R4, R6, R10 are already partly enforced by the shipped storyboard system
  (word-sync, `_variety_ok`, adaptive code sizing, hook-at-0, per-topic palette). Improving them means
  sharpening the **prompts**, not rebuilding the mechanics.
- When signal is scarce (few `measured` videos, no retention curve — the current reality), the
  render-and-judge score IS the evidence. As channels grow, shift weight to the measured signals in
  the right column and settle experiments on real data. Never invent metrics.
