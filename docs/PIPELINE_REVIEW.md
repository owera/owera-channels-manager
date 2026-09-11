# Video pipeline review (2026-09-11)

Investigation only. No mix/spend/deploy change. HEAD at review:
`b670c9d` (main). Live engine on claw0 is HyperFrames; MPT remains a profile
option (`DEFAULT_ENGINE` is still `"mpt"` if no profile names one).

Locked product intent this review is scored against:

- P1 live: frame0/thumb echo the spoken title hook (PRs [#17](https://github.com/owera/owera-channels-manager/pull/17), [#18](https://github.com/owera/owera-channels-manager/pull/18)).
- YPP yesed (implementing, not assumed shipped): (1) Decolar object on
  frame0/thumb = claim noun; (2) OS vs RR visual split; (3) first 3s + beats
  ≤3s, ban spoken list mid-video. (5) Rodrigo YES: series endcard
  `Subscribe — next {series} {noun}.` (visual + final VO) + chip `· {series}`.
  `_sanitize_cta` stays on mid-video / title / Follow-tomorrow / waitlist /
  Cloud. TTS upgrade deferred.
- Mix: 1 short/day each channel. No volume/spend up without human yes.
- Agents that must stay consistent with the code: Channels, CMO, Designer,
  Video Maker, CTO, Social Ops, CoS.

---

## 1. How it works today

```
Topic.theme_prompt
    │  autofill_loop / POST /topics/{id}/generate
    ▼
video_gen.generate_ideas  →  Video DRAFT (subject = idea title)
    │  produce (API) or render_loop._auto_produce
    ▼
QUEUED
    │  render_loop._submit_new  (budget + concurrency + 1L/4S leftover ordering)
    ▼
engine.submit  →  HyperFrames daemon thread (worker.run_job)
    1. LLM script          worker._generate_script          grok -p
    2. TTS + word times    worker._tts                      edge-tts WordBoundary
    3. Storyboard HTML     storyboard.compose               typed beats → GSAP
       └ fallback          worker._fallback_composition     kinetic title cards
    4. Silent render       npx hyperframes@0.6.97
    5. Mux + duck          worker._mux                      ffmpeg asplit + sidechaincompress
    ▼
RENDERING → _finalize
    copy storage/videos/{id}/video.mp4
    blank-frame pixel check
    ffmpeg still → thumb.jpg (preview only)
    metadata.generate + _lock_decolar_title
    craft.title_gate_reason  →  REVIEW (blocked) | APPROVED (skip_gate) | REVIEW
    ▼
publish_loop._publish_one
    title_gate again (no upload if blocked)
    metadata.finalize_description  (Subscribe/Inscreva-se + playlist)
    youtube.upload_video
    thumbnail.make_thumbnail_png   (best-effort custom 1280×720)
    first comment + playlist insert
```

Status machine (`app/models.py`): `draft → queued → rendering → (review|approved)
→ publishing → published` plus `failed` / `rejected`.

### Key modules

| Stage | File | What it actually does |
|---|---|---|
| Ideas | `app/services/video_gen.py` | LLM titles + `CRAFT_RULES_SHORT` prompt addendum. Drops banned-CTA lines. Does **not** require `· <series> <nn>`. |
| Autofill | `app/services/autofill_loop.py` | Tops DRAFT bench to `budget × board_horizon_days`. Still reserves a long slot for the old 1L+4S mix. |
| Render tick | `app/services/render_loop.py` | Auto-produce, mix rebalance, submit, poll, finalize, skip-gate, title gate. |
| Engine | `app/services/engines/hyperframes.py` + `worker.py` | Script → TTS → compose → render → mux. |
| Storyboard | `app/services/engines/storyboard.py` | Typed beats, word-sync, `_lock_opening_hook`, `_sanitize_cta`, list-hold cap 6s. |
| Theme | `app/services/engines/theme.py` | `brand=os` B&W / `brand=rr` warm ink / `None` neon. |
| Craft gates | `app/services/craft.py` | Spoken-title regex, claim alignment, CTA strip, `brand_of`. |
| Metadata | `app/services/metadata.py` | Title/desc/tags; patterned subject wins verbatim; description subscribe block at publish. |
| Thumb | `app/services/thumbnail.py` | Claim-text hook + hardcoded `RECEIPT` chrome. Best-effort, never blocks publish. |
| Publish | `app/services/publish_loop.py` | Windows, drip, quota, title gate, upload, thumb, comment, playlist. |
| Issues | `app/services/issues.py` | Ops digest. `title_pattern_blocked` is informational (`auto: false`). |
| Approve | `app/routers/videos.py` | Title-pattern 409. No frame0 / beat / object re-check. |

### Failure points (observed in code + growth reports)

1. **Grok timeout / unparseable storyboard** → kinetic-text `_fallback_composition` (or `STATE_FAILED` if `GrokCLIError`). Fallback skips Decolar, brand palettes, code beats.
2. **Blank frames** → one fallback re-render; still blank → finalize marks `FAILED`.
3. **edge-tts `NoAudioReceived` / `BlockingIOError`** → transient re-queue (max 2).
4. **Script opener ≠ title-before-`·`** → frame0 locks to **script**, thumb locks to **title**. Patterned subjects keep the YouTube title even when the spoken first sentence drifted (09-10 residual; title lock `fae2dc7` does not rewrite the script).
5. **Idea without `· <series> <nn>`** → metadata may emit a suffix-less title → skip-gate stays `review`, publish refuses. Ops must **reject**, not mass-retitle.
6. **ffmpeg duck graph** — PR #18 (`asplit` before `sidechaincompress`). Pre-#18 mux died on ffmpeg 7 (`Stream specifier 'n'`).
7. **Custom thumb failure** — logged, publish still succeeds. Review UI may show the ffmpeg still (`thumb.jpg`), not the uploaded card.
8. **MPT profile** — no storyboard/Decolar/brand path. Title gate still applies at finalize/publish. Live fleet is HyperFrames via render profile.

---

## 2. Craft locks vs intended behavior

### P1 — frame0/thumb = spoken title hook — **mostly live, two divergence paths**

Enforced:

- `storyboard._lock_opening_hook` overwrites beat 0; strips hook emoji; demotes a second hook.
- Safety clip is **12 words** (09-11, `407198b`) so PT objects like `modelo` survive. `_coerce_beat` still clips LLM hook text to 8w; the lock runs after.
- `thumbnail._hook_text` rejects unaligned LLM slogans; fallback `compress_claim(spoken, 8)`.
- `metadata._lock_decolar_title`: patterned subject wins verbatim; else title must be a compression of the first spoken sentence (`_is_title_echo`, stricter than `claim_aligned`).
- Publish/approve/retry/skip-gate: `title_gate_reason` (series suffix only — not “does frame0 match”).

Gaps:

| | Frame0 | Thumb | YouTube title |
|---|---|---|---|
| Source | `spoken_hook_source(None, script, subject)` — **script first sentence** | `spoken_hook_source(title, None, subject)` — **title before `·`** | patterned **subject** if present |
| Clip | 12 words | 8 words | 100 chars |
| Alignment check at approve | none | none (best-effort at publish) | series regex only |

So a 9-word PT opener can be complete on frame0 and clipped on the thumb; a drifted script opener can disagree with a locked patterned title. Curiosity-gap LLM copy is overwritten on the happy path; **fallback HTML is still typography-only title cards** and can show the full subject including `· Series nn`.

`docs/CRAFT.md` / rubric R1 still say ≤8-word compression for frame0. That is stale vs the 12w lock.

### YPP (1) — object on frame0/thumb = claim noun — **not enforced**

Hypothesis confirmed: **typography-only thumbs (and frame0) are still the default.**

- Frame0 renderer is word-spans of the claim text. No object still.
- Thumb HTML always paints `#chrome>RECEIPT` and a generic slab, regardless of the claim noun (invoice, terminal, VRAM, Copilot bill, …). Prompt *asks* the LLM to keep the object **in the words**. Tests pin `"RECEIPT" in html` as “object-of-angle chrome”.
- Designer-winning stills (real receipt / terminal) are not selected or generated.

### YPP (2) — OS vs RR visual split — **live on the happy path**

- `craft.brand_of(slug, name)` → `os` / `rr`.
- `render_loop._submit_new` passes `params["brand"]`; `publish_loop` passes brand into `make_thumbnail_png`.
- `theme.resolve(..., brand=)` swaps palettes (ch1 B&W, ch2 warm ink). Unbranded unit tests keep neon.

Gaps: `_fallback_composition` hardcodes `#0b0b16` / `#c9d2ff` (legacy neon). Unknown slugs → `brand=None` → neon. CTA box text color is still `#08080f` on both brands. Thumb chrome is the same RECEIPT slab on both.

### YPP (3) — first 3s + beats ≤3s, ban spoken list mid-video — **not enforced**

Shipped pacing (`storyboard.py`):

- `_MIN_DUR = 0.5`, `_MID_MIN = 1.8`, `_MID_MAX = 7.5`, `_TAIL_MIN = 2.0`
- Hook pinned at `t=0`; duration is “until next cue”, often well over 3s
- Last beat (CTA) is an **uncapped** absorber
- `list` is in `composition_beat_types` and the system prompt *asks* for lists for steps/reasons
- `_cap_list_holds` only caps a **visual** list dump at 6s; it does not ban a spoken list in the script

No first-3s gate. No ≤3s beat ceiling. Script prompt does not forbid mid-video enumeration.

### YPP (5) — series endcard Subscribe — **Rodrigo YES (locked; not shipped)**

Locked exception: Subscribe is allowed **only** on the series endcard template,
**visual + final VO**:

```
Subscribe — next {series} {noun}.
```

plus chip `· {series}` on that card. Nowhere else.

`_sanitize_cta` **stays** on mid-video / title / Follow-tomorrow / waitlist /
Cloud. Do **not** invert the global ban. Do **not** punch a hole in
`craft.contains_banned` or script prompts for a generic “subscribe”.

Runtime today (implementation gap, not a policy dispute):

- `_sanitize_cta` still overwrites the last card from the last spoken sentence
  and strips Follow/Siga/subscribe — endcard template is not wired
- Script + storyboard prompts still forbid a subscribe ask (including final VO)
- `metadata.finalize_description` still appends `🔔 Subscribe` / `Inscreva-se`
  + channel URL (generic description — not the endcard; sanitize stays)
- Author first comment is an engagement seed, not a series endcard
- Series identity lives only in the title suffix `· Copilot Credits 14` —
  **no endcard chip**

Playbook `run/daily-agent-playbook.md` directive 3 (“Part N tomorrow” in the
close) is Follow-tomorrow: still banned mid-video. The yes’d close is the
endcard template only.

### TTS — deferred, as locked

`edge-tts` only. PT: `rate=-8%`, `pitch=-2Hz`. No paid voice API in this repo.

### Mix leftover

Live budgets are 1 render / 1 publish (growth reports). Code + playbook still encode **1 long + 4 shorts** (`render_loop._queued_candidates`, `_auto_produce`, `publish_loop._next_approved`, autofill long reserve, playbook directive 2). Harmless while longs are parked and budget=1; it will surprise anyone who restores budget>1 without a CoS yes.

---

## 3. Agent handoffs vs automation

Skip-gate on a patterned title means the artifact never stops for a human. That is the main collaboration gap.

| Agent | Should gate | What the app does today |
|---|---|---|
| **Channels (ops)** | Budgets, produce, reject leftovers, claw0 restart | Dashboard + growth agent (`run/daily-agent-playbook.md`) via REST. Issues digest. Auto-produce fills budget. **No craft sample.** |
| **CMO (voice/funnel)** | Hook language, series promise, endcard Subscribe (visual + final VO) | Rodrigo YES’d the endcard exception. Runtime still sanitizes last-card + script. Generic description still auto-appends Subscribe/Inscreva-se (not the endcard). **No voice review.** Funnel today = YouTube only. |
| **Designer (visual)** | Frame0/thumb object, OS vs RR | Palettes automated. Thumb is a template. **No sample gate.** Fallback can ship neon kinetic text. |
| **Video Maker (craft)** | Frame0=spoken, object, beat length, endcard, no mid-list | **Title-pattern gate only.** Compose locks are render-time; approve does not re-read HTML/frames. Rubric review is a growth-agent batch (`run/rubric_review.py`), not a per-video gate. |
| **CTO (infra)** | grok OIDC, hyperframes pin, ffmpeg, quotas | Transient retries, blank-frame check, publish stall cap. `DEFAULT_ENGINE="mpt"` is a footgun if a profile is missing. |
| **Social Ops** | Hub: YouTube + Instagram + LinkedIn + X | `run/seeding/YYYY-MM-DD.md` kits (operator posts). Instagram/LinkedIn **banned in YT copy**. No adapters, no Publication rows. GitHub / Community is **out of this app** (Community stays on-call, separate). Personal channels stay out of the hub. |
| **CoS** | Volume, spend, YPP yeses, ch1 pause | Not in the app. Growth/code agents commit to `main` inside playbook caps. ch1 pause is still a report recommendation (14 < 15), not a code gate. |

Skip-gate + patterned title remains the collaboration hole. Designer has no sample (fallback can ship neon). Video Maker has no frame0/beat/object re-check. Do **not** open a `craft_audit` workstream in this track.

---

## 4. Multi-network architecture (design only)

Keep the YouTube HyperFrames pipeline as the canonical **short renderer**. Do not pretend Cloud-as-if-ready. Hub surface is **YouTube + Instagram + LinkedIn + X only**. GitHub / `github_discussion` / Community-as-hub is **out of Channels Manager** — Community stays on-call, separate. Personal channels stay out of the hub.

Other hub networks start as **publications of an already-approved YouTube package**, then grow adapters.

### Phase A — YouTube harden (this repo)

Priority order (YPP #5 is Rodrigo YES — implement later, not this PR):

1. Series **endcard template** (visual + final VO): `Subscribe — next {series} {noun}.` + chip `· {series}` on that card only. Keep live `_sanitize_cta` on mid-video / title / Follow-tomorrow / waitlist / Cloud. Do **not** invert global sanitize.
2. Point frame0 lock at the **same claim source as the title** (title-before-`·`, or rewrite script sentence 0 to match). Align thumb clip with frame0 (12 vs 8).
3. Replace hardcoded `RECEIPT` with a claim-noun still (or at least label). Typography-only is the live CTR miss vs Designer intent.
4. Beat ceiling / first-3s / no mid-video spoken list — new aligner rules + script ban. Current 7.5s mid cap is the opposite shape.
5. Fallback must honor brand + Decolar or fail the job (do not auto-publish kinetic neon).
6. Reconcile playbook mix (4S+1L @ budget 5) with locked **1 short/day**. Leave the 1L+4S code inert until CoS unparks longs.

TTS stays edge-tts until a paid yes.

### Phase B — adapters (additive)

Do **not** widen `Channel` into a social graph. Channel stays a YouTube identity (OAuth, quotas, playlists). Hub networks only:

```
Channel (existing, YT)
  └── ContentPackage          # canonical claim, not a network post
        claim, series, episode_n, brand, language
        script, title_hook, frame0_text
        assets: video_mp4, thumb_png, captions
        └── Publication[]      # one row per network attempt
              network: youtube_short | instagram_reel | linkedin_post | x_post
              adapter_payload_json
              status: draft | review | approved | publishing | published | failed
              gates: cmo_voice | designer_sample | video_maker_craft | cos_volume
NetworkAccount                # IG/LI/X credentials, daily_budget default 0
  channel_id, network, handle, credentials_ref
```

Adapters (interface only): `submit(package) -> handle`, `poll`, `public_url`. YouTube adapter **is** today’s `publish_loop`. LinkedIn + X start as “copy out of seeding kit into `Publication.body`” — same human-post rule as today. Instagram reel = crop/repost of the YT short, no extra render budget without CoS. Each adapter owns its CTA rules so a LinkedIn/X post can name that network while the YT script cannot.

Approval: skip-gate becomes **per Publication**, not per Channel. YT skip-gate today would otherwise blast every hub network.

### Phase C — CMO console

One board: packages on the X axis, hub networks (YouTube, Instagram, LinkedIn, X) on the Y axis. Existing `/review/:id` becomes the YouTube cell. CMO sees voice variants; Designer sees frame0/thumb; Video Maker sees the live craft locks; Social Ops sees non-YT hub cells; CoS sees budget=0 cells until yesed. Growth agent keeps writing seeding kits until Phase B publications exist — then it should open `Publication` drafts instead of markdown-only.

Out of scope until yesed: posting automation, paid APIs, extra daily volume, Cloud-as-product mentions, TTS upgrade, GitHub/Community, personal-channel adapters, `craft_audit`.

---

## 5. What this PR changes

Docs only: this file, plus `docs/CRAFT.md` pointers so agents stop treating
≤8w frame0 / a global Subscribe invert / 1L+4S playbook as live law.

Hub sketch is YouTube + Instagram + LinkedIn + X. GitHub/Community is out.
YPP #5 is Rodrigo YES: endcard visual + final VO only; live `_sanitize_cta`
stays on mid-video / title / Follow-tomorrow / waitlist / Cloud.

No pipeline, budget, prompt, or mix edits.
