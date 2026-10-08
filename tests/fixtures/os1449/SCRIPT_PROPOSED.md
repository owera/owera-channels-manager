# #1449 → #1460 (new draft) · Cursor · "Cursor timed out on French Windows. · Agent traps 9" · SCRIPT v3 FINAL (VM PASS, applied 08/10)

**Status:** the v3 remake is **draft #1460** (created 08/10 ~15:00 BRT). **The #1449 original is now PRIVATE** (verified by the parent: oEmbed 403), so "· Agent traps 9" on #1460 meets the VM's condition and the **title is kept**. The VM's alternative (Agent traps 15) is not needed; CMO note: don't renumber, 15 is the autogen's next number.

**Applied in the manager 08/10 ~15:10 BRT:** `PATCH /api/videos/1460/script` with this VO. The description was already v3 and is unchanged. Verified: status `draft`, not in `queue-plan?channel_id=1`, script/description byte-equal, title unchanged, `error: null`. Pre-v3 backup: `os-1460/bak-pre-v3.json` (the created text); after: `os-1460/after-v3.json`. No produce/approve/requeue/queue/reorder. Creation record: `os-1449/remake/`.

## VO (v3 final, byte-for-byte from `os-named-tool-2026-10-06/scripts.json` slot 1 (CMO 08/10 15:08; includes the VM's recommended edit with the CMO's comma: "a PowerShell parser error, from a quoting bug on non-English Windows."); VM PASS 08/10; = manager script)
```
Cursor timed out on French Windows. Every Agent request failed right away with Agent Execution Timed Out. The real error was in the dev tools: a PowerShell parser error, from a quoting bug on non-English Windows. The timeout message pointed at the wrong place. The fix: update Cursor. Exact version in the description. Cursor support said so on the forum, and the reporter confirmed it works. Agent times out instantly? Check the logs before the network. Subscribe — next agent trap.
```

## Description (v3 first line + source block; = manager description)
```
Fixed in Cursor 3.20.21 (Cursor staff, accepted answer on the forum).
Cursor timed out on French Windows. Fixed upstream. Sources:
https://forum.cursor.com/t/agent-fails-with-error-extension-host-timeout-on-non-english-windows-agent-exec-powershell-sid-lookup-has-a-quoting-bug/171409
https://forum.cursor.com/t/agent-fails-with-error-extension-host-timeout-on-non-english-windows-agent-exec-powershell-sid-lookup-has-a-quoting-bug/171409/14
```
The render loop keeps an existing description (`render_loop.py:250-265`), so a description PATCHed before render survives.

## Gate audit (edge-tts en-US-AndrewNeural +0% through the real `craft.tts_spoken_text`, measured 08/10 ~15:15; `os-1451/tts/v3final_measured.json`)
| gate | target | v3 | result |
|---|---|---|---|
| title = first spoken line | equal | "Cursor timed out on French Windows." = sentence 1 | PASS |
| title words | 4–6 | 6 | PASS |
| claim end | 1.85–2.23s | 1.85s | **PASS (edge)**: 1.85 is on the lower bound (VM measured 1.84–1.86 on the v2 master) |
| first cut after the claim, by 2.5s | claim end < cut ≤ 2.50 | next speech at 2.26s | OK if the first card enters between 1.85 and 2.26 (≤2.5) |
| TTS total | 26.5–31.6s | 29.36s | PASS |
| money / Credits | none | none | PASS |
| endcard VO | "Subscribe — next agent trap." | present | PASS |
| mid cards | ~2.6s incl. fade (VM ≤2.80) | the code caps the hold at 2.80 → ≈2.92 measured | **Not reachable with current code** (see PREFLIGHT Q3) |

## Digits and versions the TTS could misread
- **No dotted version in the VO.** ASR small/base hear "The fix, update Cursor. Exact version in the description." PASS.
- The version 3.20.21 is **only in the description, not on screen**. **VM accepted description-only for this batch** (VM_SCRIPT_REVIEW, Q5). A generated version stat must match the description exactly; any other number is a FAIL.
- Flag: the generator may draw a `stat` "3.20.21" or "3.20.21+" just from "update Cursor". No gate stops a stat value. If it appears it is factually right (source #14), but it was generated, not scripted.

## On-screen literal allowlist (the only logs/errors/commands/outputs/quotes that may appear)
Each string is copied from the source, with its URL. Anything else in a terminal, code or quote card is a FAIL.
**Re-copied byte-for-byte from the RAW posts** (VM must-fix). Discourse's rendered HTML changes the text (`(:) [` became an emoji and checkbox; curly quotes). Raw files: `os-1460/sources/raw_171409_1.md` and `raw_171409_9.md`, fetched 08/10 15:09 BRT. sha256 `8c9eb1b1…` / `0b960bf9…`.

Source: https://forum.cursor.com/raw/171409/1 (OP, DevTools console, French Windows). Lines 52, 55–57:
- `ERR [Extension Host:agent-exec] Au caractère Ligne:1 : 118` (the raw line starts `console output : ` before it)
- `Jeton inattendu «S-1-5-18» dans l'expression ou l'instruction.` (straight apostrophes, guillemets « »)
- `    + CategoryInfo          : ParserError: (:) [], ParentContainsErrorRecordException` (4 leading spaces, as in the raw post)
- `    + FullyQualifiedErrorId : UnexpectedToken`
- The same block repeats for `S-1-5-32-544` (lines 59–64): `Jeton inattendu «S-1-5-32-544» dans l'expression ou l'instruction.`
- Symptom sentence (raw line 5): "Every Agent request fails immediately with "Agent Execution Timed Out" (ERROR_EXTENSION_HOST_TIMEOUT / deadline_exceeded) on a French Windows installation."

Source: https://forum.cursor.com/raw/171409/9 (error JSON, straight quotes):
- `{"error":"ERROR_EXTENSION_HOST_TIMEOUT","details":{"title":"Agent Execution Timed Out", …` (the raw post continues with `"detail":"The agent execution provider did not respond in time. …"`)
- `ConnectError: [deadline_exceeded] Agent Execution Timed Out` (the raw escapes the brackets as `\[ \]` for Markdown only)
- "Agent Execution Timed Out" as a text card.

**Removed:** the `… s[0]).Translate([System.Security.Principal.NTAccount]).Value S-1-5-18` line. In the raw post it reads `+ ... s<a href="" class="citation-link" …>[0]</a>).Translate(…)`, so the source is corrupt there.

**Any English version of the log is NOT allowed** ("Unexpected token …" etc.). It is a translation, so FAIL on screen.

Quotes: #14 (Colin, staff, accepted answer) “Can you check that you’re on 3.20.21 or higher? It should be fixed there!” and #18 “I started using 3.20.21 today and it works well now.” are **not spoken**, so **no quote cards**.

## What the generator could invent here (check the render)
- **High:** rule 2b forces a `code`/`command` card. The LLM will write a "realistic" PowerShell log, which is exactly #1449's `MissingTerminator`/`"exécution"`. Nothing guards against it on OS (no teaser, not rr).
- **High:** a quote card for "Cursor support said so on the forum" with an LLM `attribution` ("— Cursor forum" / "— Cursor support"). Attribution is never checked.
- **Medium:** a card that says "Reproduced" (v2 #10) or a paraphrase posing as a quote.
- **Low:** a `stat` version (see digits).
- **Accepted object cards:** "Agent Execution Timed Out" text card, the ParserError/UnexpectedToken lines above, the stat "100% Agent requests failed" (VM: verified for the reporter).

## Mid-card plan (target ~2.6s incl. fade)
| VO span (s) | dur | sentence | expected cards at ~2.6s |
|---|---|---|---|
| 0.10–1.85 | 1.75 | Cursor timed out on French Windows. | hook |
| 2.26–6.25 | 3.99 | Every Agent request failed right away with Agent Execution Timed Out. | 2 (object: “Agent Execution Timed Out” / error JSON + text) |
| 6.67–12.38 | 5.71 | The real error was in the dev tools: a PowerShell parser error, from a quoting bug on non-English Windows. | 3 (ParserError literal lines + 2 continuations) |
| 12.80–15.01 | 2.21 | The timeout message pointed at the wrong place. | 1 |
| 15.44–17.02 | 1.58 | The fix: update Cursor. | 1 (pairs with the next sentence) |
| 17.44–19.25 | 1.81 | Exact version in the description. | 1 |
| 19.66–23.56 | 3.90 | Cursor support said so on the forum, and the reporter confirmed it works. | 2 (text + continuation; never an attributed quote) |
| 23.96–25.18 | 1.22 | Agent times out instantly? | 1 |
| 25.59–27.18 | 1.59 | Check the logs before the network. | 1 |
| 27.59–29.36 | 1.77 | Subscribe — next agent trap. | endcard |

Long sentences (>2.6s) need two or three cards: an object card (literal, from the allowlist) and/or Card ⊂ VO continuation cards that pick up the literal next words. Card times follow the speech anchors (`align_storyboard`/`_split_long_mids`/`_sync_dp`). With the current cap a card can reach a 2.80 hold, ≈2.92 measured, so the VM must still measure each render. The ~2.6s target needs the Dev change (`_MID_MAX`/`MID_BEAT_MAX_S` ≈2.48).

## Diff vs the script currently in the manager (v2)
```
- … a PowerShell parser error, because a quote broke on non-English Windows. …
+ … a PowerShell parser error, from a quoting bug on non-English Windows. …
- The fix: update to Cursor 3.20.21 or higher.
+ The fix: update Cursor. Exact version in the description.
```
- **Why:**
  - "3.20.21" was heard as "March 20th, 21" (VM #1449 FAIL P0). The version moves to the description.
  - "from a quoting bug" follows the thread title "…SID lookup has a quoting bug" (VM recommendation, CMO-approved with a comma).
- **TTS:** claim end 1.85s (lower edge; the render must measure ≥1.85), total **29.36s**.
- **Source:** forum thread 171409, raw posts #1 and #9; #14 staff accepted answer; #18 reporter.
