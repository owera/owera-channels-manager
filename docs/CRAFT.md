# Craft gates (spoken title, Decolar, series endcard)

Live render engine is HyperFrames. Theme prompts already ask for the spoken-title
pattern; this repo now **enforces** it in code.

Pipeline map, YPP gaps, agent handoffs, and the multi-network sketch:
[`docs/PIPELINE_REVIEW.md`](PIPELINE_REVIEW.md).

## Spoken title (P0)

Shorts titles must match:

```
·\s*(Copilot Credits|Agent memory|CrewAI|IA|Local|Claude Code|Shipping|Agent traps)\s+\d+
```

Example: `Copilot billed the cancelled run · Copilot Credits 14`

The gate refuses:

- `POST /api/videos/{id}/approve` (409 + reason)
- skip-gate auto-approve (stays `review` with `error` set)
- `POST /api/videos/{id}/retry` when the artifact would re-enter `approved`
- `publish_loop._publish_one` (auto-rejects with `craft_review=fail`, does not upload)

Long-form is exempt (no series suffix).

### Pré-pattern leftovers

Ops park leftover pré-pattern items via **reject**. Do **not** mass-retitle.
`GET /api/agent/issues` exposes `title_pattern_blocked` (informational) with
`suggested_action: reject (pré-pattern leftover — do not mass-retitle)`.


## $N numeral lock (frame0 + thumb)

Since 2026-09-28 titles carry **no currency value** (see the publish gate
below), so the lock only applies when a `$N` reaches frame0 / thumb from the
spoken claim; with no `$N` it simply does not apply. When one is present,
`opening_object` / `compress_claim` / thumbnail hook compression must
**never** expand it to "seventy-nine dollars". Same `$N` on thumb and frame0.

## Credits / IA pre-approve title lock (new queue only)

For titles matching `· Copilot Credits|IA <nn>`, approve / skip-gate /
retry-to-approved / publish also require:

1. a useful spoken first phrase (head before `·`)
2. a concrete noun (bill / RAG / terminal / …) — a `$N` no longer counts
   (currency values are blocked in titles)
3. the `· series nn` suffix

Regression FAIL: Follow, Follow-tomorrow, Siga, Siga-amanhã, waitlist,
owera.com on the title. Does **not** mass-retitle the catalog — park
pré-pattern leftovers via reject.

## Decolar lock (frame 0 + thumb)

`storyboard._lock_opening_hook` and `thumbnail._hook_text` force frame 0 / the
thumbnail to echo the spoken claim. Repeating the title is required.
Curiosity-gap / “don’t reuse the title’s words” is inverted.

Overlay copy keeps the claim **as written** (`craft.overlay_hook_source` +
`craft.overlay_claim`, since 2026-09-29):

- Frame 0 shows the title head (the text before ` · Series N`) when the
  narration opens on it, so a two-sentence head like `Chat routed to Maya. Prod
  paged Lee.` stays two sentences. Otherwise it shows the first spoken
  sentence. The thumb uses the title head, or the subject head when there's
  no title.
- `. , ? !` and **every digit** are kept, including a leading one (`16GB …`,
  `48 tok/s. Sem, 11.`). Up to 12 words are shown verbatim. Longer copy keeps
  the whole sentences that fit; a single over-long sentence is clipped at 12
  words, and only a dangling `, ; :` is trimmed.
- An LLM thumb compression is accepted only if
  `craft.overlay_preserves_claim` holds: every number and every `. , ? !` of
  the claim is still there. A run-on or a dropped digit falls back to the
  verbatim claim.
- The series episode number (` · Local 57`) is not overlay copy. The endcard
  chip still uses `compress_claim`.

Regression: `tests/verify_overlay_text.py`.

The opening visual is **1 concrete object** on frame0 AND the thumb
(`craft.opening_object` + `object_markup`). The object **is the noun of the
spoken first phrase**; hook typography stays (P1). Object sits **above**
(9:16) or **beside** (16:9) the type — never covers line 1. Widgets inherit
the live OS/RR stroke (cold gray + O mark / burgundy, no Owera mark) — not a
generic slab that collapses the brand split.

Designer mapping:

- Copilot / credits / billed / `$` → bill UI / invoice / credit counter (large `$58` ok)
- API / paid / product → receipt / API stub
- GPU / VRAM / board / batch → GPU icon or VRAM meter — **not** 💸
- Chrome / Discord / apps → named app icons
- Ollama / local / terminal → terminal / Ollama prompt

Hard FAIL: typography-only thumb/frame0; 💸/🔥 as the object; neon-bar
rainbow; glass/abstract diagrams without the noun; punchline ≠ spoken first
phrase. Shorts thumb is native 9:16.

Live clips (not the same number):

- Frame 0: first spoken **script** sentence, safety-clipped to **12 words**
  (`_lock_opening_hook`). Approve does not re-check the HTML.
- Thumb: **title before `·`**, compressed to **≤8 words**, native 9:16 for shorts.
  Custom thumb is best-effort at publish.

Happy-path OS/RR palettes are live (`theme.resolve(brand=)`). Fallback
composition ignores brand and Decolar (kinetic title cards).

YPP #3 (object 0–3s / beats ≤3s / no mid-video spoken list) is the Video
Maker craft gate below. YPP #5 runtime pins the series endcard.

YPP #5 is Rodrigo YES (locked): Subscribe is allowed **only** on the series
endcard template — **visual + final VO** (`Subscribe — next {series} {noun}.`
+ chip `Subscribe · {series}`). `_sanitize_cta` stays on mid-video / title /
Follow-tomorrow / waitlist / Cloud. Do **not** invert the global CTA ban.
Generic description still appends Subscribe/Inscreva-se; that is not the
endcard.

## Publish craft gate (`craft_review`)

Durable column `Video.craft_review` ∈ {`pending`, `pass`, `fail`}.

The publish loop **only selects** `status=approved` **and** `craft_review=pass`.
Pending approved rows are evaluated on each publish tick
(`_sweep_craft_reviews`):

- clear → `craft_review=pass` (eligible)
- blocked → auto-**reject** (`craft_review=fail`)

Publish eligibility (`craft.publish_craft_block_reason`), in order:

1. configurable nonsense-title patterns (default: `billed $N` head, bare series nn,
   and — since 2026-09-28 — **any currency value anywhere in the title**:
   `R$` or `$` followed by a number, case-insensitive; title-only, the script /
   VO may still say the amount)
2. existing title lock + Video Maker A/B/C (`review_gate_reason`)
3. non-empty `script`
4. provided scripts only (`creation_config.script_source=provided`): first spoken
   line carries the title claim (`provided_script_hook_reason`, see below)
5. optional VO/beats on `creation_config` (legacy rows without `creation_config` fail-open)
6. mute / no audio track when `ffprobe` can read the file

`POST …/approve`, skip-gate finalize, and `POST …/retry` (artifact kept) write
`craft_review=pass` only when the full publish craft gate clears.
`PATCH …/craft` re-scores `craft_review` without flipping status (PR #31 path).

Nonsense patterns are configurable via `craft.set_nonsense_title_patterns([...])`
(defaults in `craft.NONSENSE_TITLE_PATTERNS`). No mix / concurrency / spend change.

Regression: `tests/verify_publish_craft_gate.py`.

## Provided scripts (spoken verbatim)

A video can carry an operator/CMO-authored VO instead of the `grok -p` script.

- **Set it on create:** `POST /api/videos` accepts optional `script` (and `title`).
- **Set / clear it before render:** `PATCH /api/videos/{id}/script` with
  `{"script": "..."}` (or `{"script": null}` to go back to generated). Allowed on
  `draft`, `queued` (not submitted yet) and `failed` without an artifact; 409
  otherwise. Post-render edits stay on `PATCH …/craft`; the wide
  `PATCH /api/videos/{id}` still ignores `script`.
- Either path writes `creation_config.script_source = "provided"` and a
  `script_set` JobRun. Rows without that marker (including requeues of
  generated videos) keep regenerating exactly as before.

At submit the render loop:

1. Checks the hook: the first spoken sentence must be `claim_aligned` with the
   title head before `·` (else the subject head), and a `$N` stake stays numerals.
   If it doesn't match, the video goes to `failed` with `craft_review=fail` and
   the `PROVIDED_SCRIPT_HOOK_REASON` error. No render slot, playlist or engine
   call is used, and the script is **never regenerated**. Fix it with
   `PATCH …/script`, then `POST …/retry`.
2. Applies only the deterministic rules the generated path already uses
   (`craft.prepare_provided_script`): banned-CTA sentences dropped, mid-short
   Subscribe dropped, and for shorts the standard `Subscribe — next {series} {noun}.`
   appended when the last sentence isn't already an endcard VO. An existing
   closer is kept as written and never duplicated. The edits go to
   `creation_config.script_edits`.
3. Passes the text to the engine: HyperFrames gets `params["provided_script"]`
   (`worker.run_job` skips `_generate_script`; TTS and storyboard use the text
   verbatim), and MPT gets its native `video_script`.

`_finalize` keeps the provided script (provided text plus endcard) as the text
of record. An empty, None or re-derived task script never overwrites it.
Everything else runs through the normal gates: Gate A/B/C, real VO / mute
check, title gate and `craft_review`. The worker stamps
`creation_config.script_source = provided|generated` on every render.

Regression: `tests/verify_provided_script.py`.

## Provided thumbnails (operator / Designer file)

A designer thumbnail replaces the template card for one video:

- `POST /api/videos/{id}/thumbnail`: multipart field `file`, or JSON
  `{"path": "..."}` (must resolve inside the manager `storage_dir`; symlinks and
  `../` that escape it get a 400). `DELETE` clears it. Allowed from any status
  before upload (draft … approved, plus failed/rejected). `publishing`/`published`
  return 409: nothing is re-set on YouTube.
- Stored at `<storage_dir>/videos/<id>/thumb_provided.<png|jpg>`, and
  `thumb_path` points at it. The filename is the marker of record.
  `creation_config.thumb_source="provided"` + `thumb_provided_path` are written
  too. Each set or clear logs a `thumbnail_set` JobRun.
- Validation: PNG or JPEG, sniffed from the bytes. Long side must be ≥ 640 and
  short side ≥ 360. Input over 25 MB is a 413. Over 2 MB (the YouTube
  thumbnails.set limit), the image is re-encoded to JPEG with ffmpeg: `-q:v`
  steps, then a scale to a 1280 px long side. If it is still over 2 MB, the
  upload is a 400. Portrait 1080×1920 is stored as-is (the Shorts template
  cards are already 720×1280 portrait and YouTube accepts them). An aspect that
  doesn't match the format only gives a warning.
- Render `_finalize` skips the 1s still when a provided thumbnail exists and
  carries the marker into the new `creation_config`. A PATCH `/craft` that
  replaces `creation_config` can drop the informational keys, but never the
  provided state, which lives in the filename.
- At publish, `_set_custom_thumbnail` uploads the provided file and skips
  `make_thumbnail_png`. If the file is missing on disk, it logs an error JobRun
  and falls back to the template (`thumb_path` is not overwritten). Without a
  provided thumbnail, publish works as before.

Regression: `tests/verify_provided_thumb.py`.

## Video Maker craft gate (Shorts A+B+C)

Automatic PASS/FAIL on the storyboard/render path (post-compose, before
publish). Long-form is exempt. No TTS / budget / concurrency change.

Evaluated from aligned beats (embedded in `index.html` as
`#storyboard-beats`, snapshotted on `creation_config.craft_gate`).

The gate refuses the same surfaces as the spoken-title lock:

- `POST /api/videos/{id}/approve` (409 + reason)
- skip-gate auto-approve (stays `review` with `error` set)
- `POST /api/videos/{id}/retry` when the artifact would re-enter `approved`
- `publish_loop._publish_one` (auto-rejects with `craft_review=fail`, does not upload)

`GET /api/agent/issues` exposes `craft_gate_blocked` (informational).

### A — Object 0–3s

PASS: in the first 3.0s of the timeline (beat 0 cue until t=3), ≥1 beat with
type ∈ {code, command, diagram, compare, stat} **or** the hook has a non-empty
`object` field (Decolar prop: receipt / terminal / bill).

FAIL: only hook/statement with text+emoji (typography-only) until t=3.
Emoji does **not** count as an object.

### B — Beats ≤ mid-hold (`craft.MID_BEAT_MAX_S`, live **3.0s** HARD)

PASS: for every beat except the final `cta` / endcard series, visual hold
(`dur`, which `align_storyboard` caps at `_MID_MAX`) ≤ `MID_BEAT_MAX_S`.
That constant **must equal** `storyboard._MID_MAX` (both **3.0s** — Rodrigo
YES via CoS 2026-09-15; closes the ~5–5.8s command-beat auto-approve hole
for **new queue**). Cue-to-cue (`next.start − start`) is `_GAP` (0.12s)
longer than `dur` — do **not** fail on the fade (v1258 measured hold, not
fade). Pré-gate inventory without a beats snapshot still fail-opens.

FAIL: any mid card/slide > `MID_BEAT_MAX_S` (3.0s).

cta/endcard series: max 4.0s (not a Follow-tomorrow hold).

### C — Kill spoken list/slide spam

PASS:

- `statement` ≤ 1 in the whole short (tightened from the old tolerance of 2)
- `list` forbidden, **or** if kept: max 1 list, ≤3 items, beat ≤
  `MID_BEAT_MAX_S`, item stagger ≤0.6s
- Prefer code/command/diagram/compare/stat in the middle

FAIL: ≥2 `statement` **or** a list with >3 items **or** list/statement that
only re-displays narration without a rich type **or** mid-short copy
contains Subscribe/subscribe (or PT Inscreva) CTA text.

Subscribe is **only** allowed on the trailing `cta` / `endcard` series
(Rodrigo YES via CoS). The endcard itself is not blocked. `subscribers`
(the noun) does not trip.

Pré-gate inventory with no beat snapshot fail-opens (do not mass-reject).
Kinetic-text fallback (`used_fallback`) fails A+C.

## Series endcard (shorts)

After the claim (never on frame0). Template fields: `{series}` from the title
suffix (OS default **Copilot Credits**, RR default **IA**); `{noun}` ∈
trap | receipt | bill | drop (default **trap**). Other series only swap those
two fields — no invented extra CTA.

- **VO** (1 line, ≤8 words): `Subscribe — next {series} {noun}.`
- **Chip** (1 line): `Subscribe · {series}`
- **Micro** (optional, only if it fits): `same series`

Pinned in `craft.series_endcard` / `ensure_series_endcard_vo` (script) and
`storyboard._sanitize_cta` (chip). Visual hold ≤ `ENDCARD_MAX_S` (4.0s).
Subscribe is **FORBIDDEN** in the mid-short / body / miolo (no Subscribe
text, VO, or chip before the endcard).

Subscribe is **ALLOWED only on this endcard** (final VO + last visual slot).
`strip_mid_subscribe` / `_strip_mid_subscribe_beats` drop Subscribe / Inscreva
from the mid-short (miolo): no Subscribe text, VO, or chip before the last beat.

### Hard bans on the endcard

Follow / Follow tomorrow / amanhã / waitlist / owera.com / Cloud /
“part 2 coming” / SMY / emoji 💸 / neon. Subscribe on the chip is only
`Subscribe · {series}` — not a Follow button or a mid-card CTA.

## CTA ban

Generation + post-gen strip/reject: Follow, Siga, Siga-amanhã, follow for more,
waitlist, Owera Cloud-as-product, SMY, Instagram, LinkedIn.

Shorts close on the series endcard (Subscribe VO + series chip), not a Follow ask.
Long-form close stays a builder/confiança punch (sanitize strips subscribe).
Subscribe is not in `BANNED_RE` (that would strip the endcard VO).

## Visual systems

- **os** (Owera Software / ch1): bg `#0A0A0A` → `#000000`, optional cold glow
  `#1A1A1A`. text `#FFFFFF` / secondary `#6B6B6B`. Accent B&W + gray max `#9A9A9A`.
  Signature thumb+frame0: `03-owera-o-avatar.png` (white O crop), bottom corner,
  48–72px on 1080×1920 (≤8% frame height), ~90% opacity, ≥48px from edges/YT UI.
  Forbidden: `#FF2D55` / magenta / burgundy / rainbow neon. No tagline/.com/Cloud
  on the mark.
- **rr** (Rodrigo Recio / ch2): bg `#0A0A0A` → `#000000` with burgundy glow
  `#4A1528` / `#2A0A14` (upper corner). text `#FFFFFF`. Accent `#C41E5A` as a
  thin highlight/stroke on boxes/objects only. Logo: NONE — zero O, zero "Owera",
  zero Owera asset paths.
- Unbranded unit tests keep the legacy neon palette
- Banner OS CMO v2 is frozen — do not change YouTube channel banner assets
- Mute-scroll (~0.3s) stills of OS vs RR must be distinguishable. No neon-bar
  top gradient rainbow and no glass boxes as identity.

## Audio

PT edge-tts: slight rate/pitch ease (no paid API). BGM: sidechain duck under
voice; rotate the full bed pool (not techno_* only).

## Deploy on claw0

Channels will pull/restart. Checkout:

```
~/src/owera-channels-manager/
```

Do **not** raise `daily_publish_budget`, `daily_render_budget`, or
`render_concurrency`. Do not change topic weights. After pull: restart the
manager unit so the publish gate is live before the next drip.

## Pre-produce subject guard (2026-09-28)

`render_loop._auto_produce` runs `subject_guard.subject_guard_reason` before a
draft takes a render slot. Held drafts stay `draft` with the reason on
`error` + one `produce` error run, and the next valid draft is queued. Held:
a lowercase first word (except allowlisted literals like `node_modules`,
code-ish tokens with `_ . / - @ :` or digits, camelCase brands like `eGPU`),
a bare unit / currency token with no number (`B`, `GB`, `%`, `R$`, `x` …),
leading punctuation, and any currency value (`R$N` / `$N`) in the subject.
The digest lists them under `subject_held`.

## Contrast split-card (Designer council 2026-09-29)

Frame0 and the template thumbnail share one markup (`craft.split_card_markup`) for
titles with a real ` · Agent memory|IA|Local N` suffix:

- `Chat … . Prod … .` / `Com … . Sem, … .` → labelled top card + bottom card with a ✕ stamp.
- Other two-sentence heads → unlabelled top/bottom cards.
- One-sentence IA heads `X, não é engenharia.` → split at the verdict.
- Text is the title head verbatim (punctuation + digits, #38). Frame0 is at full
  opacity at t=0 and shows the whole claim (#45); the split is only applied when the
  split text equals the hook text.
- Never applied when the operator provided a thumbnail (`thumb_source=provided`, #39),
  on long-form, or on other series (Shipping keeps the object-over-type card).
- Thumbnail cards sit in 9%–72% of the height; no first-word chip, O ring hidden on the card.

## RR hook pace (P1 d, council 2026-09-29)

RR channel shorts (brand `rr`) only:

- **Compose:** the first cut lands by 2.5 s (`storyboard._pull_first_cut`). Frame0 keeps the whole claim and ends at the cut.
  - A long window becomes a new quote card of the words spoken there.
  - A short window lets the first card lead the VO; capped cards push the next card earlier.
  - It is all-or-nothing: if the endcard would overflow, the board is left as it was.
- **Gate B (`[B] RR hook pace`):** the claim (hook text) must be ≤8 words and fully spoken by 3.0 s (TTS word timings), with the first cut by 2.5 s.
- **Marker:** new RR renders store `creation_config.hook_pace = {"version": "rr_v1", "claim_words", "claim_spoken_end"}`. Boards without the marker are never checked, so approved inventory is not rejected at publish.
- Compose cannot shorten the claim: the title and script are upstream. An over-long RR head fails Gate B.

## Series endcard chip source (P1 e, 2026-09-29)

The endcard chip and the pinned VO take their series from `craft.series_of(title, brand, topic_name)`, in this order:

1. The title suffix ` · <series> <nn>`.
2. The video's real topic name, as an exact label or a leading whole-word label (for example "Agent memory and state in production" → Agent memory).
3. The brand default (OS → Agent memory, RR → IA).
4. Copilot Credits.

`render_loop` passes `params.topic_name`. Shipping #1308/#1311 (rendered 22 Sep, before #33 and before the OS default) showed "Copilot Credits" even though their topic is "Shipping".
