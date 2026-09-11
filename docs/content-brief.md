# Content Brief (P1) — company social hub

**Design-only.** This file is a reviewable contract, not a ship. No intake UI, no
publish wiring, no mix/spend/concurrency/deploy change. Implementation needs a
**separate Rodrigo yes**. Until then, live YouTube generate / render / publish
stays the `Channel → Topic → Video` pipeline in `app/models.py`.

GitHub is **out of this hub** (Community stays on-call, separate). Do not add
`github` or `github_discussion` to the schema. Personal channels are **out**
(do not mix IG/X handles):

- Instagram `@rod_recio`
- X `@rrecio`
- LinkedIn `rodrigorecio`
- YouTube RR / ch2 (`brand=rr`)

Machine-readable copy: [`schemas/content-brief.schema.json`](schemas/content-brief.schema.json).
Review instances: news+social [`content-brief.example.json`](schemas/content-brief.example.json); YPP#5 Subscribe-fork [`content-brief.ypp5.example.json`](schemas/content-brief.ypp5.example.json).

`publications[].network` must be the same set as `target_networks` (exactly one adapter each).
Each publication **must** declare its own native `format` (kind + ratio + size +
teaser duration). Do **not** reuse one generic cut across networks.

---

## Where it sits (additive)

Live source of truth today:

```
Channel ─┬─ Topic ─ Video     ← youtube short / long (do not re-spec)
         └─ RenderProfile
```

`Video` is the existing youtube-short unit (`content_format=short`, MPT platform
`youtube_shorts`). Lifecycle is `VideoStatus` in `app/models.py` (`draft → queued
→ … → published`). **Do not conflate** brief `draft` with `Video.draft`.

Content Brief is a **new root**, sibling of `Channel`, not a column on `Video`
and not a `Topic`:

```
ContentBrief
  ├─ art                    ONE object on the brief root (not a second art root)
  └─ Publication[]          one per target network; native format lives here
       ├─ instagram         → Reel 9:16 — Social Ops after Rodrigo yes
       ├─ linkedin          → video 1:1 (alt 4:5) — Social Ops after Rodrigo yes
       ├─ x                 → video 16:9 — Social Ops after Rodrigo yes
       └─ youtube_os        → Short 9:16 — handoff to existing Video (Channels ops)
```

`youtube_os` is optional: only when the brief marks it. It is a pointer into the
existing OS channel (`brand=os` / ch1 via `craft.brand_of`), not a second render
stack. Do not re-spec storyboard, HyperFrames, metadata, or `publish_loop`.

New networks (IG / LI / X), if they ever become Channel-like rows: default
`daily_publish_budget=0` and `daily_render_budget=0`. Skip-gate is **per
Publication** (not inherited from `Channel.default_skip_gate` /
`Video.skip_gate` — see `_effective_skip_gate` in `app/services/render_loop.py`).

---

## Routing (one brief)

| Marked networks | After `rodrigo_yes` | Who executes |
| --- | --- | --- |
| `youtube_os` | YT Channels ops | existing Video pipeline (OS only) |
| `instagram` `linkedin` `x` | Copy + Designer + Research → Social Ops | company handles only |

Company handles (locked):

- Instagram `@owerasoftware`
- LinkedIn company
- X `@owerasoftware`
- YouTube OS (Owera Software / ch1) — only if `youtube_os` is in `target_networks`

Personal out (IG `@rod_recio` · X `@rrecio` · LinkedIn `rodrigorecio` · YouTube RR / ch2 `brand=rr`). GitHub out.

---

## Status machine (required, no auto-publish)

```
draft → cmo → cos → rodrigo_yes → publish
```

| Status | Actor | Meaning |
| --- | --- | --- |
| `draft` | CMO files | brief is being written |
| `cmo` | CMO | voice cut |
| `cos` | CoS | routing / hard-nos check |
| `rodrigo_yes` | Rodrigo | authorization to execute |
| `publish` | Social Ops (IG/LI/X); Channels ops (YT if marked) | humans execute |

No auto-advance. No auto-upload. `publish` is “ops may execute,” not
`VideoStatus.PUBLISHING`. A later skip-gate, if built, is per Publication and
defaults off.

---

## Content Brief — CMO brief v2 fields

| Field | Contract |
| --- | --- |
| `objective` | `ypp_click_watch_sub` **or** `news_angle_social` |
| `target_networks` | subset of `youtube_os` \| `instagram` \| `linkedin` \| `x` (min 1). No GitHub. |
| `angle` | pillar / angle. `source_url` **required** when `objective=news_angle_social` |
| `voz` | **CMO delta.** EN spoken/builder. First line **is** spoken (1ª linha falada). Company English. Simple spoken cadence like Rodrigo — not a corporate teaser. |
| `hard_nos` | locked set below (always in force; extras allowed, removals forbidden) |
| `art` | **ONE** object on the brief root. Must illustrate **this** copy, not a generic O lockup. `asset_path` **or** Designer brief. Adaptations live on `publications[].format` — **not** a second art root. |
| `cta` | `https://owera.com` waitlist **only if natural**. **Never** in YouTube title or script |
| `status` | machine above |
| `publications` | one adapter object per target network; each declares native `format` |

`voz` is the voice field. Do not add a second “copy tone” knob that can drift.

### Locked `voz`

```
language: en
register: spoken_builder
first_line_is_spoken: true    # 1ª linha falada
cadence: rodrigo_simple       # company EN, spoken, not teaser
```

Company inherits the *tone* of Rodrigo’s personal spoken anchors, not the content (do not paste Grok/Chrome/TCE into company briefs).

### Locked hard nos (company social + YT-if-marked)

Aquila · TCE · Agenda internals · Cloud-as-if-ready · walkthrough · IP/VPN ·
client names · invented metrics · spend.

Also: no Cloud-as-product, no `owera.ai` / `cli.owera.ai` (see `CONTRIBUTING.md`
and `app/services/craft.py`).

---

## Per-network format (locked — Rodrigo YES)

CMO + Designer lock. This table **replaces** any earlier LI/X still proposal
(LI is **not** still 1.91:1; X is **not** still 1:1). Use **only** this matrix.
Each publication declares its own native cut. Do not reuse one generic cut.

`linkedin` and `x` publication `format.kind` is **`video`**. Enum is `video`.
**NEVER** `image` or `still` (or `still+caption` / `native_video`). A still may
exist only as a **review placeholder** (`publications[].review_still` — Designer
sample / teaser frame). It is **not** `format.kind`.

| network | kind | ratio | size | teaser duration |
| --- | --- | --- | --- | --- |
| youtube_os | short | 9:16 | 1080×1920 | ~30–45s (≤60s) |
| instagram | reel | 9:16 | 1080×1920 | ~15–30s |
| linkedin | video | 1:1 (alt 4:5) | 1080×1080 (alt 1080×1350) | ~30–45s |
| x | video | 16:9 | 1920×1080 | ~15–30s |

Schema field map on `publications[].format`:

| Table | Schema |
| --- | --- |
| kind short | `kind: short` (`youtube_os` only) |
| kind reel | `kind: reel` (`instagram` only) |
| kind video | `kind: video` (`linkedin` and `x` only) |
| forbidden kinds | `image`, `still`, `still+caption`, `native_video` |
| ratio / size / teaser duration | `ratio`, `size`, `teaser_duration` — exact strings from the table |
| LinkedIn alt | `ratio: 4:5` pairs with `size: 1080×1350`; primary is `1:1` / `1080×1080` |

`reuse_generic_cut` is `false` on every publication.

`youtube_os` is the live Short pipeline, OS only (`channel_scope: os_only`).
Do not re-spec generate / render / publish.

### Designer nits (required)

1. **Teaser, not walkthrough.** Frames are a teaser of the coming-soon product.
   **NEVER** a final UI / walkthrough-as-if-ready. Hard no walkthrough /
   Cloud-as-if-ready already locked. Feature videos of Channels Manager are
   teasers for a future launch (CM still in development), not a product
   walkthrough.
2. **Tokens in every aspect.** OS/RR split tokens + Decolar object + widget
   text `color:#fff` apply in **every** aspect (YT 9:16, IG 9:16, LI 1:1/4:5,
   X 16:9) — even frames cropped from the teaser.
3. **IG Reel cut.** The Instagram Reel is the **object beat 0–3s**, not a
   random crop of the YouTube short.

Art stays **one** object on the brief root (`must_illustrate_copy`, no generic
O). Those nits are fields on that same `art` object. Per-network adaptations
(`format.kind` / `ratio` / `size` / `teaser_duration` / IG `cut`) live on
`publications[]` only.

---

## Publication adapters

### `instagram` — Reel 9:16 1080×1920 ~15–30s

Social Ops after `rodrigo_yes`. Company English. Caption follows `voz` (first
line spoken). Art illustrates the copy. CTA = owera.com waitlist only if the
sentence would exist without the link.

`format.kind=reel` (not story). `format.cut=object_beat_0_3s` — the object
beat, not a random crop of the YT short.

`skip_gate` lives on the Publication (default `false`). Do not inherit ch1’s
YouTube skip-gate.

### `linkedin` — video 1:1 (alt 4:5)

Social Ops after `rodrigo_yes`. Same voice / art / CTA rules as Instagram.

`format.kind=video` (never `image` / `still`). Primary `1:1` / `1080×1080`.
Alt `4:5` / `1080×1350`. Teaser `~30–45s`. **Not** a 1.91:1 still. A still
is a review placeholder only.

### `x` — video 16:9 1920×1080 ~15–30s

Social Ops after `rodrigo_yes`. Same voice / art / CTA rules as Instagram.

`format.kind=video` (never `image` / `still`). `16:9` / `1920×1080`. **Not**
a 1:1 still. A still is a review placeholder only.

### `youtube_os` — reference the live short pipeline, do not re-spec it

Handoff only. Craft source of truth remains [`CRAFT.md`](CRAFT.md) +
`app/services/craft.py` + HyperFrames storyboard. When a brief marks YT, the
brief must satisfy:

| Invariant | Live seat | This spec |
| --- | --- | --- |
| native format | live Short 9:16 1080×1920 | `kind=short`, `ratio=9:16`, `size=1080×1920`, teaser `~30–45s (≤60s)` |
| frame0 = spoken `· <series> <nn>` | title gate + Decolar | same; first line **is** the spoken line |
| Decolar object | `_lock_opening_hook` / `_hook_text` — frame0/thumb = title-before-`·` or that claim | same; also on every other publication aspect |
| OS vs RR | `craft.brand_of` → `os` (B&W) vs `rr` (warm ink) | this hub is **OS only**; RR is out |
| widget text | live `color:#fff` on object widgets | same in every aspect |
| beats ≤ 3s | live `align_storyboard` uses other caps (`_MID_MAX=7.5`) | **spec invariant for brief-driven YT**; do **not** retune live align in this PR |
| CTA | waitlist / owera.com **never** in title or script | same |

Do not change Banner OS v2, mix (1 short/day), or `render_concurrency=1`.

---

## Subscribe fork (document only — do not implement)

Live shorts close on a builder/confiança punch. `_sanitize_cta` strips Follow /
Subscribe from the last card; `craft.BANNED_RE` strips Follow-tomorrow, waitlist,
Cloud-as-product, IG/LI from generation. `docs/CRAFT.md` CTA ban stands.

**Known fork vs that live ban** (do **not** invert live code in this PR):

| Surface | Keep doing (live) | Spec exception |
| --- | --- | --- |
| Title | sanitize; no Follow-tomorrow, waitlist, owera.com | none |
| Script / mid-video | sanitize; no Follow-tomorrow, waitlist, owera.com | none |
| Generic description | sanitize those phrases; do not put waitlist/owera.com in the YT script | none |
| **YPP#5 series endcard** | live card is still the sanitized builder punch | **allow Subscribe here only** |
| Channel About waitlist branding | separate surface | **out of this hub** |

`metadata.finalize_description` already appends a Subscribe URL block at
**publish-description** time. That is live upload machinery, not the YPP#5
endcard, and this spec does not flip it.

Do not implement endcard or sanitize changes here.

---

## Frozen (out of scope)

- Banner OS v2
- Mix: 1 short/day
- `Settings.render_concurrency = 1`
- Live generate / render / publish / spend / deploy
- Personal channels, GitHub/Community
- Budget raise, auto-publish, intake UI

---

## Implementation gate

A later PR may add tables + intake. That PR is blocked on a **separate Rodrigo
yes**. Default for any newly materialized network row: budgets `0`, skip-gate
per Publication `false`. This document does not authorize that work.
