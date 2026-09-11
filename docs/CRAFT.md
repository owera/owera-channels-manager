# Craft gates (spoken title, Decolar, CTA ban)

Live render engine is HyperFrames. Theme prompts already ask for the spoken-title
pattern; this repo now **enforces** it in code.

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
thumbnail to equal the first spoken sentence (title before `·`) or a faithful
≤8-word compression of **that same claim**. Repeating the title is required.
Curiosity-gap / “don’t reuse the title’s words” is inverted.

## CTA ban

Generation + post-gen strip/reject: Follow, Siga, Siga-amanhã, follow for more,
waitlist, Owera Cloud-as-product, SMY, Instagram, LinkedIn.

Shorts close on builder/confiança, not a subscribe ask.

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
