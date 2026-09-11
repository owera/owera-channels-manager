# Craft gates (spoken title, Decolar, series endcard)

Live render engine is HyperFrames. Theme prompts already ask for the spoken-title
pattern; this repo now **enforces** it in code.

Pipeline map, YPP gaps, agent handoffs, and the multi-network sketch:
[`docs/PIPELINE_REVIEW.md`](PIPELINE_REVIEW.md).

## Spoken title (P0)

Shorts titles must match:

```
·\s*(Copilot Credits|Agent memory|CrewAI|IA|Local|Claude Code)\s+\d+
```

Example: `Copilot billed the cancelled run · Copilot Credits 14`

The gate refuses:

- `POST /api/videos/{id}/approve` (409 + reason)
- skip-gate auto-approve (stays `review` with `error` set)
- `POST /api/videos/{id}/retry` when the artifact would re-enter `approved`
- `publish_loop._publish_one` (returns the row to `review`, does not upload)

Long-form is exempt (no series suffix).

### Pré-pattern leftovers

Ops park leftover pré-pattern items via **reject**. Do **not** mass-retitle.
`GET /api/agent/issues` exposes `title_pattern_blocked` (informational) with
`suggested_action: reject (pré-pattern leftover — do not mass-retitle)`.

## Decolar lock (frame 0 + thumb)

`storyboard._lock_opening_hook` and `thumbnail._hook_text` force frame 0 / the
thumbnail to echo the spoken claim. Repeating the title is required.
Curiosity-gap / “don’t reuse the title’s words” is inverted.

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

## Video Maker craft gate (Shorts A+B+C)

Automatic PASS/FAIL on the storyboard/render path (post-compose, before
publish). Long-form is exempt. No TTS / budget / concurrency change.

Evaluated from aligned beats (embedded in `index.html` as
`#storyboard-beats`, snapshotted on `creation_config.craft_gate`).

The gate refuses the same surfaces as the spoken-title lock:

- `POST /api/videos/{id}/approve` (409 + reason)
- skip-gate auto-approve (stays `review` with `error` set)
- `POST /api/videos/{id}/retry` when the artifact would re-enter `approved`
- `publish_loop._publish_one` (returns the row to `review`, does not upload)

`GET /api/agent/issues` exposes `craft_gate_blocked` (informational).

### A — Object 0–3s

PASS: in the first 3.0s of the timeline (beat 0 cue until t=3), ≥1 beat with
type ∈ {code, command, diagram, compare, stat} **or** the hook has a non-empty
`object` field (Decolar prop: receipt / terminal / bill).

FAIL: only hook/statement with text+emoji (typography-only) until t=3.
Emoji does **not** count as an object.

### B — Beats ≤3s

PASS: for every beat except the final `cta` / endcard series, duration =
`next_cue_start − cue_start` (last pre-cta: `cta_cue − cue`) ≤ 3.0s.

FAIL: any mid card/slide >3.0s.

cta/endcard series: max 4.0s (not a Follow-tomorrow hold).

### C — Kill spoken list/slide spam

PASS:

- `statement` ≤ 1 in the whole short (tightened from the old tolerance of 2)
- `list` forbidden, **or** if kept: max 1 list, ≤3 items, beat ≤3.0s, item
  stagger ≤0.6s
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
