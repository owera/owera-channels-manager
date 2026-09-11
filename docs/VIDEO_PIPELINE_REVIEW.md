# Video pipeline review

**Repo:** `owera/owera-channels-manager` @ `b670c9d` (`main`, 2026-09-11)
**Scope:** how a topic becomes a published Short/long, vs the Craft/YPP locks already decided and the ones still WIP.
**Not in scope:** deploy, production config changes, multi-network social hub (named only as a future surface).

Live claw0 shape (from `run/agent-reports/2026-09-11.md`, not from this repo's defaults): both channels `daily_render_budget=1` / `daily_publish_budget=1`, skip-gate on when the title carries `· <series> <nn>`, ch1 window `07:00`, ch2 window `14:00`. Code defaults remain `6/6` budgets and `render_concurrency=1`. Do not raise those from this review.

---

## 1. Pipeline map

```
topic (theme + weight + format)
   │  autofill / Trends adopt / operator Generate
   ▼
ideas = Video.subject  (status=draft)
   │  produce (API or render_loop._auto_produce)
   ▼
queued ──► rendering ──► rendered ──► review | approved
   │            │                         │         │
   │            │                    title gate     │ skip-gate
   │            │                    (shorts only)  │
   ▼            ▼                                   ▼
script     HyperFrames job                    publishing
+ meta     (TTS → storyboard → render → mux)       │
+ thumb?                                           ▼
                                              published
                                         (YT upload + custom thumb
                                          + first comment + playlist)
```

Status machine (`app/models.py` `VideoStatus`):

`draft → queued → rendering → rendered → (review | approved) → publishing → published`
plus `failed` / `rejected`.

### Who owns what

| Stage | Module | Owns |
|---|---|---|
| Topic / idea bench | `app/models.py` `Topic`, `app/routers/topics.py`, `app/services/video_gen.py`, `app/services/autofill_loop.py` | Theme, `content_format`, `weight`, `theme_prompt`. Ideas are `Video.subject` rows. Autofill fills `draft` up to `daily_render_budget × board_horizon_days`. |
| Produce | `app/routers/videos.py`, `render_loop._auto_produce` / `_rebalance_queued_mix` | `draft → queued`. Mix policy: keep 1 long in the pipe, prefer shorts once a long is banked. Weight-0 / inactive topics are never auto-produced. |
| Script | `app/services/engines/worker.py` `_generate_script` | Spoken VO via `grok -p`. Language pinned to the render-profile voice (`pt`/`en`/`es`). Banned CTAs stripped in code. Word-count retry once. |
| Metadata | `app/services/metadata.py` | Title / description / tags at **finalize** (not at produce). MPT `/social-metadata` then Grok fallback. `_lock_decolar_title` after sanitize. Description subscribe block is appended later at publish (`finalize_description`). |
| Storyboard | `app/services/engines/storyboard.py` | Typed beats → one `index.html` + GSAP master timeline. `_lock_opening_hook`, `_sanitize_cta`, `_cap_list_holds`, `align_storyboard` (word-sync). |
| TTS | `worker._tts` | `edge-tts` only. WordBoundary timings for align. PT: `rate=-8%`, `pitch=-2Hz`. No paid TTS. |
| Render | `app/services/engines/hyperframes.py` + `worker.run_job` | Daemon thread: script → TTS → compose → `npx hyperframes@0.6.97` → blank-frame guard → ffmpeg mux. Status in `status.json`. |
| Theme | `app/services/engines/theme.py`, `craft.brand_of` | `os` B&W (ch1 / Owera), `rr` warm ink (ch2 / Recio), else legacy neon. |
| Thumbnail | `app/services/thumbnail.py` | Custom 1280×720 hook card at **publish**, best-effort. Preview `thumb.jpg` at finalize is a 1s ffmpeg still, not the designed card. |
| Approve | `app/routers/videos.py` `POST /approve`, `render_loop._finalize`, `craft.title_gate_reason` | Shorts without `· <series> <nn>` cannot leave review. Longs exempt. |
| Publish drip | `app/services/publish_loop.py`, `app/services/quota.py` | Per-channel: connected OAuth, not paused, inside `publish_windows`, under `daily_publish_budget`, under YouTube quota cap, drip-spaced, one in-flight upload. Mix: reserve first slot of the quota day for a long. |
| Scheduler | `app/services/scheduler.py` | APScheduler: render 15s, publish 60s, metrics 6h, analytics 12h, autofill 20m, BGM replenish 24h. `max_instances=1`, coalesce. |
| Craft gates | `app/services/craft.py` | Shared regex / strip / brand / claim helpers. Single source for title gate + Decolar claim + CTA ban. |
| Issues | `app/services/issues.py` | Growth-agent triage. `title_pattern_blocked` is informational (`auto: false`, suggested reject). |
| MPT (secondary) | `app/services/engines/mpt.py`, `mpt_client.py` | Stock-footage engine. **Code default** if no profile names an engine is still `"mpt"` (`engines/__init__.py`). Live craft docs say HyperFrames. A profile-less video would skip the whole storyboard lock set. |

### Render job (HyperFrames) in order

`worker.run_job`:

1. `_generate_script` (Grok)
2. `_tts` → `narration.mp3` + `narration_words.json`
3. `storyboard.compose` (or `_fallback_composition` if invalid / blank / render fail)
4. `npx hyperframes` silent MP4
5. `_mux` voice + ducked BGM → `final.mp4`
6. `creation_config` snapshot (`beat_types`, `used_fallback`, voice, theme, duration)

`render_loop._finalize` then copies the file, rejects blank pixels, generates metadata, applies the title gate, then skip-gate → `approved` or stays `review`.

---

## 2. Craft / YPP locks — code vs intent

Legend: **SHIPPED** matches intent · **PARTIAL** intent exists but leaks · **WIP** named, not enforced · **CONFLICT** upcoming intent fights a shipped lock.

### 2.1 Decolar — frame0 / thumb echo the spoken title hook (PR #17)

**Intent (PR #17 / `docs/CRAFT.md`):** frame 0 and the thumbnail **are** the first spoken sentence (title hook), or a faithful compression of **that same claim**. Repeating the title is required. No second typographic hook, no curiosity-gap slogan.

**Shipped:**

- `storyboard._lock_opening_hook` overwrites beat 0 after the LLM draft. Safety clip is **12 words** (09-11, `407198b`) so PT objects like `modelo` survive. Emoji stripped. A second `hook` beat is demoted to `statement`.
- Compose user prompt injects `First spoken sentence (THIS is frame 0…)`.
- `thumbnail._hook_text` compresses `spoken_hook_source(title, None, subject)` to ≤8 words; LLM slogans that fail `claim_aligned` fall back to `compress_claim`.
- `metadata._lock_decolar_title` (09-10, `fae2dc7`): patterned subject (`· <series> <nn>`) wins **verbatim** (v1249 class). Else a drifted slogan is replaced by the first spoken sentence. Stricter than `claim_aligned` (shared words like RAG+slow are not enough to keep a second slogan).
- Rubric R1 / R8 updated. Suites: `verify_craft`, `verify_storyboard` (hook overwrite + PT object), `verify_thumbnail`, `verify_metadata`.

**PARTIAL — curiosity-gap still exists in these seams:**

| Seam | What still happens |
|---|---|
| Frame0 source ≠ title source | `_lock_opening_hook` calls `spoken_hook_source(None, script, subject)` — **title is not passed**. Frame0 follows the script opener. YouTube title follows patterned **subject** (`_lock_decolar_title`). If those diverge, thumb (title-first) ≠ frame0 (script-first). This is the 09-10 residual class: spoken/frame0 vs title. |
| Kinetic fallback | `_fallback_composition` is title cards + 7-word script clips. No Decolar lock, no brand tokens, no object chrome. Triggered on unparseable storyboard, HyperFrames render fail, or blank frames. `used_fallback=true` still publishes if pixels are visible. |
| Idea prompts | `video_gen.generate_ideas` still teaches curiosity-gap patterns (`Why Your X Keeps [bad outcome]`, "root-cause curiosity"). `CRAFT_RULES_SHORT` is appended but does not rewrite those patterns. |
| Rubric R5 | Still asks for an early open loop ("but here's the catch…"). Fine for mid-script; easy to leak into the opener. |
| 12w / 8w clip | Frame0 can still drop a trailing object on a 13+ word opener. Thumb clip is still 8 words (09-11 report: thumb dropped `e` on a PT sentence). |
| Preview thumb | Finalize `thumb.jpg` is ffmpeg `@ t=1s`, not the designed card. Review UI posters that still. Custom thumb is publish-only. |

**Verdict:** lock is real and tested for the happy HyperFrames path. It is **not** a single claim identity across subject / script / title / frame0 / thumb. Curiosity-gap is inverted in compose+thumb+metadata, not in idea generation or fallback.

### 2.2 BGM duck filtergraph (PR #18 `asplit`)

**Intent:** duck the bed under voice. Do not die on ffmpeg 6.1 / 7 lavfi pad reuse.

**Shipped.** `worker._mux`:

```
[1:a]apad,atrim=0:{dur},asplit=2[voice][sc];
[2:a]volume={vol},atrim=0:{dur}[bed];
[bed][sc]sidechaincompress=threshold=0.06:ratio=8:attack=40:release=280[ducked];
[voice][ducked]amix=inputs=2:duration=first:normalize=0[a]
```

`verify_worker.py` pins `asplit=2`, `[voice]`/`[sc]`, and rejects the PR #17 `[n]`-reuse graph. Live 09-11: `Stream specifier 'n'` gone. `_pick_bgm` rotates the **full** bed pool (not `techno_*` only). Duck params unchanged.

**Verdict:** SHIPPED. Residual is operational (missing `sidechaincompress` on a weird ffmpeg build; empty BGM dir → voice-only, which is fine).

### 2.3 Title pattern `· Series {nn}` / `· IA {nn}`

**Intent:** Shorts titles carry a spoken series suffix. Pré-pattern leftovers are **rejected**, not mass-retitled.

**What the code actually accepts** (`craft.SPOKEN_TITLE_RE`) — closed allowlist, case-insensitive:

```
·\s*(Copilot Credits|Agent memory|CrewAI|IA|Local|Claude Code)\s+\d+\b
```

`Hook · Other Series 1` is **rejected** (`verify_craft`). There is no generic `· Series {nn}` matcher. Live subjects look like:

- ch1 t15: `… · Copilot Credits 76`
- ch2 t4: `… · IA 176`

**Enforced at four gates** (shorts only; longs exempt):

1. `POST /api/videos/{id}/approve` → 409
2. skip-gate in `_finalize` → stay `review` + `error`
3. `POST /retry` when the artifact would re-enter `approved`
4. `publish_loop._publish_one` → bounce to `review`, no upload

`issues.detect` exposes `title_pattern_blocked` with `suggested_action: reject (pré-pattern leftover — do not mass-retitle)`, `auto: false`.

**PARTIAL:** the suffix is **not generated in this repo**. `generate_ideas` does not append `· <series> <nn>`. The number lives in `Video.subject` because topic `theme_prompt` / the growth agent put it there. If an idea lands without the suffix, metadata will lock title to the first spoken sentence (no suffix) and the video dies in review forever unless a human rejects it. There is no incrementer, no uniqueness check, no "next episode" helper.

**Verdict:** gate is SHIPPED and correctly harsh. Pattern **minting** is a CMO/ops convention sitting in the live DB, not a code lock.

### 2.4 Upcoming (WIP) — verify absence

| Lock | Code today | Status |
|---|---|---|
| **Decolar object on frame0** (not typography-only) | `render_hook` is kinetic words + optional emoji (emoji stripped by the lock). No receipt/terminal/bill object on beat 0. Thumb has a **generic** `#slab` + `RECEIPT` chrome for every video — Designer-shaped, not claim-conditioned. Prompt says "prefer the object" but the lock overwrites hook text to the spoken claim. | **WIP** |
| **OS vs RR visual split** | `craft.brand_of(slug, name)` → `theme.resolve(..., brand=)`. `os`: B&W palettes + scan/overlay. `rr`: rust/ink/kraft. Unbranded unit tests keep neon. Wired at render submit and custom thumb. | **SHIPPED** (not WIP) |
| **Beats ≤3s** | `_MID_MIN=1.8`, `_MID_MAX=7.5`, `_LIST_MAX=6.0`, last-beat CTA **uncapped**. Prompt still says "8+ seconds is a DRAG". Rubric reviews routinely score mid beats at 5–7s as a 2. | **WIP** — current policy is "no 8s mid drag", not "≤3s" |
| **No spoken list-slides mid-video** | `list` is a first-class allowed type. Prompts **encourage** lists for steps/reasons. Variety retry can introduce a list. Cap is 6s hold, not a ban. | **WIP** — opposite of current R2 variety pressure |
| **Series endcard `Subscribe — next {Series} {noun}`** | On-screen CTA is a **builder punch**. `_sanitize_cta` forces last spoken sentence (≤4w) and treats `subscribe` / `inscreva` / Follow / Siga as banned. Rubric R7: ZERO subscribe ask on Shorts. Description **does** append `Subscribe for daily…` / `Inscreva-se` at publish (`metadata.finalize_description`). First comment is "what would you change?" + playlist link. | **WIP / CONFLICT** with shipped R7 |

---

## 3. Failure modes

### 3.1 Where renders fail

Transient → re-queue up to `retry_count < 2` (`render_loop._TRANSIENT` / `issues.TRANSIENT_SIGNATURES`):

| Signature | Typical cause |
|---|---|
| `grok.Timeout` | Long-form compose/script > `MANAGER_GROK_TIMEOUT_SECONDS` (600s). 09-01 longs at 300s shipped kinetic fallback until timeout was raised **and** `GrokCLIError` was re-raised instead of swallowed. |
| `NoAudioReceived` | edge-tts flake (ch1 v1213). No artifact; retry is free. |
| `BlockingIOError` | Pipe/FD contention when many midnight renders overlapped (09-03, concurrency 14). Live concurrency is 1 — mostly historical. |
| `overloaded` / `529` / `503` / `litellm.Timeout` | Legacy LiteLLM/Anthropic strings still listed. Live LLM is Grok CLI only. Harmless leftovers. |
| wall-clock `render timed out` | 40 min (`render_timeout_seconds=2400`). Orphaned in-process HyperFrames jobs recovered to `queued` at startup (`recover_orphaned_renders`). MPT tasks survive restart and are re-polled. |

Hard fail (no more retry, or after budget):

- HyperFrames CLI nonzero / missing `render.mp4`
- ffmpeg mux error (the old `[n]` specifier; should be dead post-#18)
- finalize: `final.mp4` missing
- finalize: `_has_visible_frames` false after fallback (`post-render frames blank at finalize — not publishing`)
- metadata `GrokCLIError` after two transient retries

**Soft fail that still ships:**

- Storyboard unparseable / invalid timing → kinetic `_fallback_composition`. Craft visual locks do not run. `used_fallback=true` in `creation_config`.
- Custom thumb fail → publish still succeeds (ffmpeg still @ t=1s is the Review poster).

### 3.2 Where curiosity-gap still exists

See §2.1 table. Highest-leverage leftover: **three sources of the "claim"** (subject, script sentence 1, generated title) and two lock functions that do not share a single input. Fallback is a fourth source (raw subject on frame0).

### 3.3 TTS path

```
voice_name on RenderProfile.params_json
    → worker._voice() strips -Male/-Female
    → edge_tts.Communicate(text, voice, boundary="WordBoundary")
    → PT voices: rate=-8%, pitch=-2Hz
    → narration.mp3 + [{text, start, dur}]
    → align_storyboard(cues → word offsets)
```

No ElevenLabs / OpenAI / paid path. Empty WordBoundary list degrades to even spacing (`align_storyboard`). Empty audio file raises `RuntimeError` → job `STATE_FAILED` → transient retry if the message contains `NoAudioReceived`.

Language: `video_gen.language_from_voice` / `code_from_voice` from the voice prefix. Script + ideas + metadata prompts get a HARD RULE. A stray EN subject on a PT voice still narrates in PT (07-07 incident).

### 3.4 Publish budgets / concurrency

| Knob | Where | Default in code | Live (reports) |
|---|---|---|---|
| `Channel.daily_render_budget` | render submit + auto-produce | 6 | **1** |
| `Channel.daily_publish_budget` | publish tick | 6 | **1** |
| `Settings.render_concurrency` | global in-flight HyperFrames threads | 1 | 1 (do not raise) |
| `Settings.publish_drip_minutes` | min gap after last publish | 30 | 30 |
| `youtube_daily_quota_cap` | upload+thumb+comment units | 9000 | 1750/9000 observed |
| `publish_windows` + `publish_tz` | audience-peak | unset = anytime | ch1 07:00 / ch2 14:00 |
| `publish_timeout_seconds` | stuck `publishing` recovery | 900 | — |
| `publish_max_retries` | then `failed` to unblock drip | 5 | — |

Accounting: **render** day = UTC midnight. **publish / quota** day = Pacific midnight (`quota._quota_day_start`). Cooldown: `uploadLimitExceeded` = +24h rolling; `quotaExceeded` = next Pacific midnight.

One publish in-flight per channel. Invalid `publish_windows` **fail open** (publish anytime, warn once) — a typo must not silently freeze a channel.

Mix code is built for 1 long + 4 shorts/day. At budget=1 the first approved long of the quota day wins; otherwise the highest-weight short. `_auto_produce` / `_rebalance_queued_mix` still shuffle the queue as if the 1L+4S world existed — mostly harmless at budget=1, occasionally demotes a queued long/short around the daily reset.

---

## 4. Human / agent ops roles

The app is one console. Roles are **lenses on the same pipeline**, not products. Map them to existing surfaces; do not invent a social hub.

### Channels (ops)

**Job:** keep the machine solvent. OAuth, budgets, windows, produce/reject, stuck rows.

**Surfaces:** Dashboard, Queue Board, Channels, Settings, `GET /api/agent/issues`, `GET /api/agent/state`.

**Do:** reconnect (`python -m app.reconnect <slug>`), park pré-pattern via reject, raise nothing on budgets without a written why, watch `title_pattern_blocked` / `pipeline_starved` / `oauth` / `cooldown`. Growth agent already remediates `auto: true` items (requeue transient fails, produce to fill tomorrow's slot).

**Do not:** mass-retitle leftovers; unpause a frozen topic to "fill volume"; raise `render_concurrency` (BlockingIOError class).

### CMO (voice)

**Job:** what is said and how the series is named.

**Owns:** topic `theme_prompt` (live DB — not in git), idea patterns in `video_gen.py`, script prompts in `worker._generate_script`, `CRAFT_RULES_SHORT`, banned-phrase list, description subscribe block, first-comment copy, series labels in `craft.SERIES_LABELS`.

**Gap:** episode numbers and series nouns are a convention in prompts, not a counter. CMO decides whether `Subscribe — next {Series} {noun}` is a **description/endcard** exception to R7 or a reversal of R7. That decision is not encoded.

### Designer (visual)

**Job:** OS vs RR, object-of-angle, beat chrome.

**Owns:** `theme.py` palettes/variants, storyboard CSS + renderers (`render_hook` / `code` / `command` / `diagram` / `list` / `cta`), `thumbnail._thumbnail_html`.

**Gap:** frame0 is type. Thumb chrome says `RECEIPT` even when the claim is VRAM or regex. Fallback is the 2024 neon kinetic card (`#0b0b16` / `#c9d2ff`) — brand leak when compose dies.

### Video Maker (craft gate)

**Job:** nothing ships that fails Decolar / title / CTA.

**Owns:** `craft.py`, the four title gates, `_lock_opening_hook`, `_lock_decolar_title`, `_sanitize_cta`, Review page (`frontend/src/pages/Review.tsx`), `used_fallback` as a ship/no-ship signal (today it is **not** a gate).

**Should become:** a real gate — reject or hold `used_fallback=true` Shorts; require frame0 == title-before-dot == thumb claim; optional object beat on frame0.

### Social Ops (other networks)

**Out of scope to implement.** Instagram / LinkedIn / SMY / Owera Cloud are **banned** in generation today. YouTube-only: upload, playlist, description CTA, author first comment, custom thumb, analytics.

Future surface (do not build now): a fan-out after `published` that takes the already-approved YouTube artifact + metadata and posts elsewhere. That hub must **not** reuse Shorts on-screen CTA (R7) and must not weaken the title gate. Treat it as a new `JobRun.kind`, not a second render pipeline.

---

## 5. Gaps and ranked recommendations

No implementation in this PR. Do not raise budgets/concurrency. Do not mass-retitle.

### P0 — claim identity + fallback leak

1. **One `claim` object per video.** Compute once at compose time: `spoken_hook_source(title or subject, script, subject)` and pass **that** into `_lock_opening_hook`, `_hook_text`, and `_lock_decolar_title`. Patterned subject still wins the YouTube title string (keep the suffix); frame0/thumb compress the **head** of that same string. Kills the script-vs-title split.
2. **Fallback is a craft miss.** `_fallback_composition` should still lock frame0 to the claim (and brand tokens). Better: `used_fallback=true` Shorts stay `review` with a hard error (Video Maker gate), same class as title-pattern. Kinetic neon must not auto-approve through skip-gate.
3. **Mint the series suffix in code** (or refuse the idea). If `theme_prompt` / autofill returns a Short without `· <label> <nn>`, either append the topic's series+next-n or drop the idea. Today's "hope the prompt remembered 76" is how v1249 happened.

### P1 — upcoming craft, without fighting R7

4. **Object on frame0.** New hook renderer: claim text **plus** a typed still (`receipt` / `terminal` / `bill` / `vram`) chosen from the claim words, not a hardcoded `RECEIPT` on every thumb. Keep typography as the lock; the object is the second channel, not a second slogan.
5. **Beat cap ≤3s (Shorts only).** Drop `_MID_MAX` 7.5 → 3.0 for `content_format != long`; keep CTA uncapped or cap at ~4s. Expect R2 pressure (more beats, more `code`/`command`, fewer `list`/`statement`). Re-score the golden set before shipping — this will move R4.
6. **Ban mid-video spoken list-slides on Shorts.** Allow `list` only if it is not a narrator-read slide (or demote `list` → `command`/`stat` in `_coerce_beat` for shorts). Conflicts with current variety prompt; change the prompt in the same commit.
7. **Series endcard — decide the conflict first.** Options:
   - **A (compatible with R7):** keep on-screen punch; put `Subscribe — next {Series} {noun}` only in description + first comment (description already has Subscribe).
   - **B (new lock):** last beat is an endcard, spoken or silent, and R7 is narrowed to "no Follow/Siga/waitlist" while Subscribe-next is allowed. Update `docs/CRAFT.md`, rubric R7, `_sanitize_cta`, and `verify_craft` together.
   Do not ship B as a prompt-only tweak — `_sanitize_cta` will strip it.

### P2 — hygiene / future

8. **Engine default.** `DEFAULT_ENGINE = "mpt"` vs live HyperFrames. Flip the default or make "no profile" an error. A profile-less produce skips every visual lock.
9. **Review poster.** Show `thumb_custom.png` when present; otherwise a frame0 extract, not `@ t=1s`.
10. **Purge dead LiteLLM signatures** from `_TRANSIENT` once you are sure no leftover MPT jobs emit them — or keep them if MPT is still a supported profile.
11. **OS/RR on fallback + code/command chrome.** Fallback and some Phase-B CSS (`#121521` / `#e8ecff`) still read as the old neon stack on an `os` video.
12. **Multi-network social hub (future only).** Post-publish fan-out. New job kind, new ops role (Social Ops), no second storyboard, no Instagram/LinkedIn in the YouTube script. Not a 2026-09 pipeline change.

---

## 6. Suggested implementation order (when someone does implement)

1. P0.1 claim identity — small, HIGH (touches storyboard + metadata + thumbnail). Isolated commit + `verify_storyboard` / `verify_metadata` / `verify_thumbnail`.
2. P0.2 fallback gate — HIGH (skip-gate / finalize). `verify_render` + `verify_craft`.
3. P0.3 suffix mint — needs a series/counter on `Topic` (schema). GATED if you want it operator-configured per topic.
4. P1.5 + P1.6 together (pacing + list ban) — one golden-set rubric run, not two.
5. P1.4 object hook — Designer + Video Maker.
6. P1.7 endcard — **after** CMO writes A vs B. Do not guess.

---

## 7. Suite pins (this review, `main` @ `b670c9d`)

Ran the lock-bearing `tests/verify_*.py` files against the tree the doc describes. All green:

| Suite | Checks | What it pins for this review |
|---|---|---|
| `verify_craft.py` | 56 | Allowlist `· <series> <nn>`, longs exempt, Decolar claim align, curiosity-gap slogan rejected, CTA strip, `brand_of`, frame0 overwrite, thumb fallback, publish bounce |
| `verify_theme.py` | 101 | OS B&W / RR warm / legacy neon; topic_id palette keying |
| `verify_metadata.py` | 74 | `_lock_decolar_title` (patterned subject verbatim, unpatterned → first spoken, longs exempt) |
| `verify_thumbnail.py` | 107 | Thumb compresses THIS claim; curiosity-gap LLM discarded |
| `verify_storyboard.py` | 243 | Hook overwrite, 12w PT object (`modelo`), Follow/Siga not forced |
| `verify_worker.py` | 207 | `asplit=2` + `[voice]`/`[sc]`; rejects `[n]`-reuse; GrokCLIError not swallowed into fallback |
| `verify_publish.py` | 232 | Title gate on `_publish_one`; drip / windows / mix |
| `verify_render.py` | 137 | Skip-gate + pré-pattern stays REVIEW; blank finalize fails |
| `verify_issues.py` | 115 | `title_pattern_blocked` informational |

These prove the **shipped** locks. They do not prove object-on-frame0, beats ≤3s, list-slide ban, or the Subscribe endcard — those have no pins because they are not in code.

## 8. Sources

- PRs: [#17](https://github.com/owera/owera-channels-manager/pull/17) Decolar invert (merged 2026-09-09), [#18](https://github.com/owera/owera-channels-manager/pull/18) `asplit` duck (merged 2026-09-09)
- Follow-ups on `main`: `fae2dc7` title lock, `407198b` hook 12w
- `docs/CRAFT.md`, `run/engagement-rubric.md`, `run/agent-reports/2026-09-10.md`, `2026-09-11.md`
- Pin files: `app/services/craft.py`, `engines/storyboard.py`, `engines/worker.py` (`_mux` / `_tts` / `_generate_script`), `metadata.py`, `thumbnail.py`, `theme.py`, `render_loop.py`, `publish_loop.py`, `video_gen.py`, `issues.py`
