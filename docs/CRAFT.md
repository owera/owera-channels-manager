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

- **os** (Owera Software / ch1): B&W
- **rr** (Rodrigo Recio / ch2): personal warm ink
- Unbranded unit tests keep the legacy neon palette

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
