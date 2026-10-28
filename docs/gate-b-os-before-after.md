# Gate B before/after — OS #1452, #1453, #1465

BEFORE = board visível dos `VM_FAIL_*.md` (main `c35e110`).
AFTER = gerador determinístico deste branch no **script do snapshot** (VO gravada) + âncoras whisper/`card_sync` do áudio real. Sem script S1453/S1465 reescrito e sem timing sintético uniforme. O check literal compara o card com as palavras faladas/renderizadas.

`SENTENCE_SPOKEN_MAX_S = 3.0` vale para **todo** mid card, quote literal incluída (sem isenção de span). Se nenhum split de cláusula cabe em ≤3,0s — ou o split de vírgula quebraria uma enumeração — Gate B FALLA e o script é que se parte.

Checks novos: `card_exits_early`, `card_enters_early` (lead > 0,3s), `card_overlap`, `cta_after_video_end`, `repeated_vo_sentence`, `code_read_too_short` (piso 1,2s em code/command), `description_missing_source`, `enumeration_needs_rewrite`.

`slide_spam` é o check Gate C existente (`STATEMENT_MAX_SHORTS = 1`).

Âncoras: timestamps whisper reais + visibilidade BEFORE (mp4, 30fps) em `tests/fixtures/os-gate-b/whisper-2026-10-19.json` (sem interpolação; pin que não casa uma palavra falha). Hold visual cruza com essa lista.

| vídeo | topic | BEFORE B | AFTER B | BEFORE kinds | AFTER kinds | hook BEFORE | hook AFTER |
|------:|------:|:--------:|:-------:|--------------|-------------|------------:|-----------:|
| #1452 | 47 | FAIL | FAIL | card_text, os_cards, card_timing, sentence_too_long, card_exits_early, card_enters_early, card_sync, vs_card_not_allowed, single_term_card, echo_card, stale_card, quote_before_speech, unspoken_card | beat_hold, card_timing, sentence_too_long, code_read_too_short, card_sync, stale_card, quote_before_speech | 2.33s | 2.54s |
| #1453 | 47 | FAIL | FAIL | card_text, os_cards, card_timing, sentence_too_long, card_exits_early, card_enters_early, ambiguous_cue, allowlist_required, card_sync, vs_card_not_allowed, single_term_card, echo_card, stale_card, quote_before_speech, unspoken_card | beat_hold, card_vo, card_timing, sentence_too_long, allowlist_required, card_sync, stale_card, quote_before_speech | 2.33s | 2.56s |
| #1465 | 7 | FAIL | FAIL | card_text, os_cards, card_timing, sentence_too_long, card_exits_early, card_enters_early, ambiguous_cue, repeated_vo_sentence, enumeration_needs_rewrite, description_missing_source, card_sync, vs_card_not_allowed, single_term_card, echo_card, stale_card, quote_before_speech, unspoken_card | beat_hold, card_text, os_cards, card_timing, sentence_too_long, repeated_vo_sentence, enumeration_needs_rewrite, description_missing_source, card_sync, stale_card, quote_before_speech | 7.43s | 3.62s |

Tabelas por vídeo abaixo.


# #1452 (topic 47, duration 30.59s)

BEFORE = board visível do `VM_FAIL_21fe.md` (main `c35e110`). AFTER = `storyboard.compose` no script do snapshot + âncoras whisper/card_sync do áudio gravado (sem S1453/S1465 reescrito, sem timing sintético uniforme).

### BEFORE (main c35e110 / VM_FAIL)

| # | type | start | dur | end | text |
|---|------|------:|----:|----:|------|
| 0 | hook | 0.00 | 2.33 | 2.33 | One folder broke Copilot CLI. |
| 1 | term_define | 2.53 | 2.40 | 4.93 | add-dir flag that adds a directory |
| 2 | compare | 5.17 | 1.56 | 6.73 | Claude subagent Claude subagent every one On the spot failed |
| 3 | quote | 7.07 | 0.76 | 7.83 | A maximum of 4 blocks with cache_control may be provided. |
| 4 | term_define | 8.07 | 1.30 | 9.37 | cache_control maximum of 4 blocks |
| 5 | quote | 9.70 | 2.03 | 11.73 | cache_control |
| 6 | compare | 11.93 | 2.40 | 14.33 | Over the limit Found 5 Limit one block over |
| 7 | quote | 14.60 | 1.83 | 16.43 | The parent agent got an empty answer. |
| 8 | term_define | 16.70 | 2.40 | 19.10 | Copilot CLI update for Anthropic subagents |
| 9 | compare | 19.30 | 2.40 | 21.70 | Directories extra directories Subagents keep working |
| 10 | term_define | 21.90 | 2.37 | 24.27 | version exact version in the description |
| 11 | quote | 24.60 | 0.93 | 25.53 | Subagent came back empty? |
| 12 | compare | 25.77 | 2.36 | 28.13 | Subagent its error Parent not the parent's |
| 13 | cta | 28.37 | 2.23 | 30.60 | Subscribe · Agent traps |

**Gate B:** `FAIL` / result `FAIL`

**kinds:** card_text, os_cards, card_timing, sentence_too_long, card_exits_early, card_enters_early, card_sync, vs_card_not_allowed, single_term_card, echo_card, stale_card, quote_before_speech, unspoken_card

**reasons:**

- [B] Card sync: FAIL — card 1 (term_define) at 2.53s leads its speech (3.84s) by 1.31s (max 1.1s). A card never appears after its own words; it may lead them by ≤1.1s.
- [B] Card text: FAIL — single_term_card: card 1 type=term_define 'add-dir flag that adds a directory' is an isolated-term / term_define card (OS mid cards are whole VO sentences); vs_card_not_allowed: card 2 type=compare 'Claude subagent Claude subagent every one On the spot failed' is a VS/list template (OS mid cards are whole VO sentences); single_term_card: card 4 type=term_define 'cache_control maximum of 4 blocks' is an isolated-term / term_define card (OS mid cards are whole VO sentences); echo_card: card 4 repeats term 'cache_control' right after card 3 already showed it; single_term_card: card 5 type=quote 'cache_control' is an isolated-term / term_define card (OS mid cards are whole VO sentences); echo_card: card 5 repeats term 'cache_control' right after card 4 already showed it; vs_card_not_allowed: card 6 type=compare 'Over the limit Found 5 Limit one block over' is a VS/list template (OS mid cards are whole VO sentences); single_term_card: card 8 type=term_define 'Copilot CLI update for Anthropic subagents' is an isolated-term / term_define card (OS mid cards are whole VO sentences); vs_card_not_allowed: card 9 type=compare 'Directories extra directories Subagents keep working' is a VS/list template (OS mid cards are whole VO sentences); single_term_card: card 10 type=term_define 'version exact version in the description' is an isolated-term / term_define card (OS mid cards are whole VO sentences); vs_card_not_allowed: card 12 type=compare "Subagent its error Parent not the parent's" is a VS/list template (OS mid cards are whole VO sentences). One card per spoken sentence, quotes only of words the VO says, no invented tool output, PT-BR copy on PT cards, no back-to-back near-duplicate cards.
- [B] Card timing: FAIL — quote_before_speech: card 3 quote starts at 7.07s, first spoken word at 8.04s (must enter at the first word); quote_before_speech: card 5 quote starts at 9.70s, first spoken word at 10.00s (must enter at the first word); quote_before_speech: card 7 quote starts at 14.60s, first spoken word at 14.72s (must enter at the first word); stale_card: card 1 type=term_define ends at 4.93s, last spoken word at 3.88s (1.05s leftover; max 0.50s); unspoken_card: card 2 type=compare 'Claude subagent Claude subagent every one On the spot failed' has no spoken span (fail closed — every mid card must map to word timings); unspoken_card: card 4 type=term_define 'cache_control maximum of 4 blocks' has no spoken span (fail closed — every mid card must map to word timings); stale_card: card 5 type=quote ends at 11.73s, last spoken word at 10.58s (1.15s leftover; max 0.50s); unspoken_card: card 6 type=compare 'Over the limit Found 5 Limit one block over' has no spoken span (fail closed — every mid card must map to word timings); unspoken_card: card 8 type=term_define 'Copilot CLI update for Anthropic subagents' has no spoken span (fail closed — every mid card must map to word timings); unspoken_card: card 9 type=compare 'Directories extra directories Subagents keep working' has no spoken span (fail closed — every mid card must map to word timings); unspoken_card: card 10 type=term_define 'version exact version in the description' has no spoken span (fail closed — every mid card must map to word timings); unspoken_card: card 12 type=compare "Subagent its error Parent not the parent's" has no spoken span (fail closed — every mid card must map to word timings); unspoken_card: card 2 type=compare 'Claude subagent Claude subagent every one On the spot failed' has no spoken span (fail closed — every mid card must map to word timings); unspoken_card: card 4 type=term_define 'cache_control maximum of 4 blocks' has no spoken span (fail closed — every mid card must map to word timings); unspoken_card: card 6 type=compare 'Over the limit Found 5 Limit one block over' has no spoken span (fail closed — every mid card must map to word timings); unspoken_card: card 8 type=term_define 'Copilot CLI update for Anthropic subagents' has no spoken span (fail closed — every mid card must map to word timings); unspoken_card: card 9 type=compare 'Directories extra directories Subagents keep working' has no spoken span (fail closed — every mid card must map to word timings); unspoken_card: card 10 type=term_define 'version exact version in the description' has no spoken span (fail closed — every mid card must map to word timings); unspoken_card: card 12 type=compare "Subagent its error Parent not the parent's" has no spoken span (fail closed — every mid card must map to word timings); unspoken_card: card 2 type=compare 'Claude subagent Claude subagent every one On the spot failed' has no spoken span (fail closed — every mid card must map to word timings); unspoken_card: card 4 type=term_define 'cache_control maximum of 4 blocks' has no spoken span (fail closed — every mid card must map to word timings); unspoken_card: card 6 type=compare 'Over the limit Found 5 Limit one block over' has no spoken span (fail closed — every mid card must map to word timings); unspoken_card: card 8 type=term_define 'Copilot CLI update for Anthropic subagents' has no spoken span (fail closed — every mid card must map to word timings); unspoken_card: card 9 type=compare 'Directories extra directories Subagents keep working' has no spoken span (fail closed — every mid card must map to word timings); unspoken_card: card 10 type=term_define 'version exact version in the description' has no spoken span (fail closed — every mid card must map to word timings); unspoken_card: card 12 type=compare "Subagent its error Parent not the parent's" has no spoken span (fail closed — every mid card must map to word timings); unspoken_card: card 2 type=compare 'Claude subagent Claude subagent every one On the spot failed' has no spoken span (fail closed — every mid card must map to word timings); unspoken_card: card 4 type=term_define 'cache_control maximum of 4 blocks' has no spoken span (fail closed — every mid card must map to word timings); unspoken_card: card 6 type=compare 'Over the limit Found 5 Limit one block over' has no spoken span (fail closed — every mid card must map to word timings); unspoken_card: card 8 type=term_define 'Copilot CLI update for Anthropic subagents' has no spoken span (fail closed — every mid card must map to word timings); unspoken_card: card 9 type=compare 'Directories extra directories Subagents keep working' has no spoken span (fail closed — every mid card must map to word timings); unspoken_card: card 10 type=term_define 'version exact version in the description' has no spoken span (fail closed — every mid card must map to word timings); unspoken_card: card 12 type=compare "Subagent its error Parent not the parent's" has no spoken span (fail closed — every mid card must map to word timings). A quote enters at its first spoken word; a card (including the hook) may not stay up more than 0.5s after its last spoken word (stale_card / quote_before_speech).
- [B] Sentence too long: FAIL — sentence_too_long: card 3 'A maximum of 4 blocks with cache_control may be provided.' spoken 3.54s (first word 8.04s–last word 11.58s; max 3.0s) — split the sentence. Any mid card (quotes included) may not cover more than 3.0s of speech (first word to last word). If no clause split lands ≤ that cap, rewrite the VO.
- [B] Card exits early: FAIL — card_exits_early: card 0 ends at 2.33s, last spoken word at 2.22s (must hold through the last word + 0.20s pad); card_exits_early: card 3 ends at 7.83s, last spoken word at 11.58s (must hold through the last word + 0.20s pad); card_exits_early: card 7 ends at 16.43s, last spoken word at 16.34s (must hold through the last word + 0.20s pad); card_exits_early: card 11 ends at 25.53s, last spoken word at 25.78s (must hold through the last word + 0.20s pad). A card must hold through its last spoken word.
- [B] Card enters early: FAIL — card_enters_early: card 1 starts at 2.53s, first spoken word at 3.84s (lead 1.31s; max 0.3s); card_enters_early: card 3 starts at 7.07s, first spoken word at 8.04s (lead 0.97s; max 0.3s). A card may lead its speech by at most 0.3s.

### AFTER (este branch, 1 frase da VO)

| # | type | start | dur | end | text |
|---|------|------:|----:|----:|------|
| 0 | hook | 0.00 | 2.54 | 2.54 | One folder broke Copilot CLI. |
| 1 | quote | 2.66 | 2.40 | 5.06 | Add a directory with the add-dir flag |
| 2 | quote | 5.18 | 3.02 | 8.20 | and every Claude subagent failed on the spot. |
| 3 | quote | 8.32 | 3.78 | 12.10 | A maximum of 4 blocks with cache_control may be provided. |
| 4 | code | 12.22 | 0.90 | 13.12 | Found 5. |
| 5 | quote | 13.24 | 1.54 | 14.78 | One block over the limit. |
| 6 | quote | 14.90 | 1.82 | 16.72 | The parent agent got an empty answer. |
| 7 | quote | 16.88 | 5.40 | 22.28 | Update Copilot CLI, and Anthropic subagents keep working with extra directories. |
| 8 | quote | 22.40 | 1.98 | 24.38 | Exact version in the description. |
| 9 | quote | 24.60 | 1.38 | 25.98 | Subagent came back empty? |
| 10 | quote | 26.18 | 1.94 | 28.12 | Read its error, not the parent's. |
| 11 | cta | 28.12 | 2.47 | 30.59 | Subscribe · Agent traps same series |

**Gate B:** `FAIL` / result `FAIL`

**kinds:** beat_hold, card_timing, sentence_too_long, code_read_too_short, card_sync, stale_card, quote_before_speech

**reasons:**

- [B] Beats ≤3.0s incl. fade: FAIL — beat[2] type=quote mid card 3.22s incl. fade (hold 3.02s + 0.20s fade; limit 3.0s); beat[3] type=quote mid card 3.98s incl. fade (hold 3.78s + 0.20s fade; limit 3.0s); beat[7] type=quote mid card 5.60s incl. fade (hold 5.40s + 0.20s fade; limit 3.0s). Mid cards/slides must be ≤3.0s including the fade (hold ≤2.80s); cta/endcard ≤3.9s including the fade (hold ≤3.78s) — split or add beats, never stretch a card (not a Follow-tomorrow hold).
- [B] Card sync: FAIL — card 3 (quote) at 8.32s appears 0.28s AFTER its speech (8.04s); card 4 (code) at 12.22s appears 0.28s AFTER its speech (11.94s); card 5 (quote) at 13.24s appears 0.34s AFTER its speech (12.90s); card 6 (quote) at 14.90s appears 0.18s AFTER its speech (14.72s); card 8 (quote) at 22.40s appears 0.08s AFTER its speech (22.32s). A card never appears after its own words; it may lead them by ≤1.1s.
- [B] Card timing: FAIL — stale_card: card 3 type=quote ends at 12.10s, last spoken word at 11.58s (0.52s leftover; max 0.50s); stale_card: card 5 type=quote ends at 14.78s, last spoken word at 14.24s (0.54s leftover; max 0.50s). A quote enters at its first spoken word; a card (including the hook) may not stay up more than 0.5s after its last spoken word (stale_card / quote_before_speech).
- [B] Sentence too long: FAIL — sentence_too_long: card 3 'A maximum of 4 blocks with cache_control may be provided.' spoken 3.54s (first word 8.04s–last word 11.58s; max 3.0s) — split the sentence; sentence_too_long: card 7 'Update Copilot CLI, and Anthropic subagents keep working with extra directories.' spoken 5.20s (first word 16.88s–last word 22.08s; max 3.0s) — split the sentence. Any mid card (quotes included) may not cover more than 3.0s of speech (first word to last word). If no clause split lands ≤ that cap, rewrite the VO.
- [B] Code read time: FAIL — code_read_too_short: card 4 'Found 5.' held 0.90s (min 1.2s to read a code/command line). Code/command cards need ≥1.2s on screen.

---

# #1453 (topic 47, duration 30.76s)

BEFORE = board visível do `VM_FAIL_c638.md` (main `c35e110`). AFTER = `storyboard.compose` no script do snapshot + âncoras whisper/card_sync do áudio gravado (sem S1453/S1465 reescrito, sem timing sintético uniforme).

### BEFORE (main c35e110 / VM_FAIL)

| # | type | start | dur | end | text |
|---|------|------:|----:|----:|------|
| 0 | hook | 0.00 | 2.33 | 2.33 | One type hint broke MCP tools. |
| 1 | term_define | 2.53 | 2.40 | 4.93 | Python three ten runtime where the tool failed |
| 2 | term_define | 5.13 | 1.50 | 6.63 | NotRequired optional key on the TypedDict |
| 3 | quote | 6.97 | 2.26 | 9.23 | registration |
| 4 | term_define | 9.47 | 2.36 | 11.83 | Pydantic called the annotation invalid |
| 5 | compare | 12.07 | 2.36 | 14.43 | Python runtime three ten tool failed three eleven and up were fine |
| 6 | term_define | 14.67 | 2.36 | 17.03 | MCP Python SDK update it and registration works |
| 7 | quote | 17.37 | 1.16 | 18.53 | Registration works, and optional keys are left out instead of sent as null. |
| 8 | term_define | 18.73 | 1.74 | 20.47 | Optional keys NotRequired fields on the return |
| 9 | compare | 20.67 | 2.30 | 22.97 | Instead of Left out not as null Sent as null null on the wire |
| 10 | quote | 23.30 | 1.10 | 24.40 | Exact version in the description. |
| 11 | term_define | 24.60 | 1.33 | 25.93 | Old Python test registration in prod |
| 12 | quote | 26.23 | 1.67 | 27.90 | prod |
| 13 | cta | 28.13 | 2.64 | 30.77 | Subscribe · Agent traps |

**Gate B:** `FAIL` / result `FAIL`

**kinds:** card_text, os_cards, card_timing, sentence_too_long, card_exits_early, card_enters_early, ambiguous_cue, allowlist_required, card_sync, vs_card_not_allowed, single_term_card, echo_card, stale_card, quote_before_speech, unspoken_card

**reasons:**

- [B] Card sync: FAIL — card 10 (quote) at 23.30s appears 0.06s AFTER its speech (23.24s). A card never appears after its own words; it may lead them by ≤1.1s.
- [B] Card text: FAIL — single_term_card: card 1 type=term_define 'Python three ten runtime where the tool failed' is an isolated-term / term_define card (OS mid cards are whole VO sentences); single_term_card: card 2 type=term_define 'NotRequired optional key on the TypedDict' is an isolated-term / term_define card (OS mid cards are whole VO sentences); single_term_card: card 3 type=quote 'registration' is an isolated-term / term_define card (OS mid cards are whole VO sentences); single_term_card: card 4 type=term_define 'Pydantic called the annotation invalid' is an isolated-term / term_define card (OS mid cards are whole VO sentences); vs_card_not_allowed: card 5 type=compare 'Python runtime three ten tool failed three eleven and up wer' is a VS/list template (OS mid cards are whole VO sentences); single_term_card: card 6 type=term_define 'MCP Python SDK update it and registration works' is an isolated-term / term_define card (OS mid cards are whole VO sentences); single_term_card: card 8 type=term_define 'Optional keys NotRequired fields on the return' is an isolated-term / term_define card (OS mid cards are whole VO sentences); echo_card: card 8 repeats term 'Optional keys' right after card 7 already showed it; vs_card_not_allowed: card 9 type=compare 'Instead of Left out not as null Sent as null null on the wir' is a VS/list template (OS mid cards are whole VO sentences); single_term_card: card 11 type=term_define 'Old Python test registration in prod' is an isolated-term / term_define card (OS mid cards are whole VO sentences); single_term_card: card 12 type=quote 'prod' is an isolated-term / term_define card (OS mid cards are whole VO sentences); echo_card: card 12 repeats term 'prod' right after card 11 already showed it. One card per spoken sentence, quotes only of words the VO says, no invented tool output, PT-BR copy on PT cards, no back-to-back near-duplicate cards.
- [B] Card timing: FAIL — quote_before_speech: card 3 quote starts at 6.97s, first spoken word at 8.06s (must enter at the first word); quote_before_speech: card 7 quote starts at 17.37s, first spoken word at 18.42s (must enter at the first word); quote_before_speech: card 12 quote starts at 26.23s, first spoken word at 26.32s (must enter at the first word); unspoken_card: card 1 type=term_define 'Python three ten runtime where the tool failed' has no spoken span (fail closed — every mid card must map to word timings); unspoken_card: card 2 type=term_define 'NotRequired optional key on the TypedDict' has no spoken span (fail closed — every mid card must map to word timings); stale_card: card 3 type=quote ends at 9.23s, last spoken word at 8.62s (0.61s leftover; max 0.50s); unspoken_card: card 5 type=compare 'Python runtime three ten tool failed three eleven and up wer' has no spoken span (fail closed — every mid card must map to word timings); unspoken_card: card 6 type=term_define 'MCP Python SDK update it and registration works' has no spoken span (fail closed — every mid card must map to word timings); unspoken_card: card 8 type=term_define 'Optional keys NotRequired fields on the return' has no spoken span (fail closed — every mid card must map to word timings); unspoken_card: card 9 type=compare 'Instead of Left out not as null Sent as null null on the wir' has no spoken span (fail closed — every mid card must map to word timings); unspoken_card: card 11 type=term_define 'Old Python test registration in prod' has no spoken span (fail closed — every mid card must map to word timings); stale_card: card 12 type=quote ends at 27.90s, last spoken word at 26.66s (1.24s leftover; max 0.50s); unspoken_card: card 1 type=term_define 'Python three ten runtime where the tool failed' has no spoken span (fail closed — every mid card must map to word timings); unspoken_card: card 2 type=term_define 'NotRequired optional key on the TypedDict' has no spoken span (fail closed — every mid card must map to word timings); unspoken_card: card 5 type=compare 'Python runtime three ten tool failed three eleven and up wer' has no spoken span (fail closed — every mid card must map to word timings); unspoken_card: card 6 type=term_define 'MCP Python SDK update it and registration works' has no spoken span (fail closed — every mid card must map to word timings); unspoken_card: card 8 type=term_define 'Optional keys NotRequired fields on the return' has no spoken span (fail closed — every mid card must map to word timings); unspoken_card: card 9 type=compare 'Instead of Left out not as null Sent as null null on the wir' has no spoken span (fail closed — every mid card must map to word timings); unspoken_card: card 11 type=term_define 'Old Python test registration in prod' has no spoken span (fail closed — every mid card must map to word timings); unspoken_card: card 1 type=term_define 'Python three ten runtime where the tool failed' has no spoken span (fail closed — every mid card must map to word timings); unspoken_card: card 2 type=term_define 'NotRequired optional key on the TypedDict' has no spoken span (fail closed — every mid card must map to word timings); unspoken_card: card 5 type=compare 'Python runtime three ten tool failed three eleven and up wer' has no spoken span (fail closed — every mid card must map to word timings); unspoken_card: card 6 type=term_define 'MCP Python SDK update it and registration works' has no spoken span (fail closed — every mid card must map to word timings); unspoken_card: card 8 type=term_define 'Optional keys NotRequired fields on the return' has no spoken span (fail closed — every mid card must map to word timings); unspoken_card: card 9 type=compare 'Instead of Left out not as null Sent as null null on the wir' has no spoken span (fail closed — every mid card must map to word timings); unspoken_card: card 11 type=term_define 'Old Python test registration in prod' has no spoken span (fail closed — every mid card must map to word timings); unspoken_card: card 1 type=term_define 'Python three ten runtime where the tool failed' has no spoken span (fail closed — every mid card must map to word timings); unspoken_card: card 2 type=term_define 'NotRequired optional key on the TypedDict' has no spoken span (fail closed — every mid card must map to word timings); unspoken_card: card 5 type=compare 'Python runtime three ten tool failed three eleven and up wer' has no spoken span (fail closed — every mid card must map to word timings); unspoken_card: card 6 type=term_define 'MCP Python SDK update it and registration works' has no spoken span (fail closed — every mid card must map to word timings); unspoken_card: card 8 type=term_define 'Optional keys NotRequired fields on the return' has no spoken span (fail closed — every mid card must map to word timings); unspoken_card: card 9 type=compare 'Instead of Left out not as null Sent as null null on the wir' has no spoken span (fail closed — every mid card must map to word timings); unspoken_card: card 11 type=term_define 'Old Python test registration in prod' has no spoken span (fail closed — every mid card must map to word timings). A quote enters at its first spoken word; a card (including the hook) may not stay up more than 0.5s after its last spoken word (stale_card / quote_before_speech).
- [B] Sentence too long: FAIL — sentence_too_long: card 7 'Registration works, and optional keys are left out instead of sent as null.' spoken 4.38s (first word 18.42s–last word 22.80s; max 3.0s) — split the sentence. Any mid card (quotes included) may not cover more than 3.0s of speech (first word to last word). If no clause split lands ≤ that cap, rewrite the VO.
- [B] Card exits early: FAIL — card_exits_early: card 4 ends at 11.83s, last spoken word at 12.52s (must hold through the last word + 0.20s pad); card_exits_early: card 7 ends at 18.53s, last spoken word at 22.80s (must hold through the last word + 0.20s pad); card_exits_early: card 10 ends at 24.40s, last spoken word at 25.04s (must hold through the last word + 0.20s pad). A card must hold through its last spoken word.
- [B] Card enters early: FAIL — card_enters_early: card 3 starts at 6.97s, first spoken word at 8.06s (lead 1.09s; max 0.3s); card_enters_early: card 4 starts at 9.47s, first spoken word at 10.42s (lead 0.95s; max 0.3s); card_enters_early: card 7 starts at 17.37s, first spoken word at 18.42s (lead 1.05s; max 0.3s). A card may lead its speech by at most 0.3s.
- [B] Ambiguous cue: FAIL — ambiguous_cue: card 3 cue 'registration' is 1 word(s) and occurs 3 times in the VO — spoken_span anchors on the first (need >=4 words or exactly one occurrence). A cue shorter than 4 words must occur exactly once in the VO; spoken_span anchors on the first match.
- [B] Allowlist required: FAIL — allowlist_required: topic 47 has no on_screen_allow. allowlist_required topics need overrides on_screen_allow.

### AFTER (este branch, 1 frase da VO)

| # | type | start | dur | end | text |
|---|------|------:|----:|----:|------|
| 0 | hook | 0.00 | 2.56 | 2.56 | One type hint broke MCP tools. |
| 1 | quote | 2.68 | 1.44 | 4.12 | On Python three ten |
| 2 | quote | 4.28 | 4.54 | 8.82 | a tool that returns a TypedDict with a NotRequired key failed at registration |
| 3 | quote | 8.94 | 1.56 | 10.50 | before it ever ran. |
| 4 | quote | 10.62 | 2.30 | 12.92 | Pydantic called the annotation invalid. |
| 5 | quote | 13.04 | 2.34 | 15.38 | Python three eleven and up were fine. |
| 6 | quote | 15.50 | 2.64 | 18.14 | Update the MCP Python SDK. |
| 7 | quote | 18.42 | 1.20 | 19.62 | Registration works |
| 8 | quote | 19.74 | 3.28 | 23.02 | and optional keys are left out instead of sent as null. |
| 9 | quote | 23.24 | 2.00 | 25.24 | Exact version in the description. |
| 10 | quote | 25.36 | 1.82 | 27.18 | Old Python in prod? |
| 11 | quote | 27.30 | 0.52 | 27.82 | Test registration there. |
| 12 | cta | 27.82 | 2.94 | 30.76 | Subscribe · Agent traps same series |

**Gate B:** `FAIL` / result `FAIL`

**kinds:** beat_hold, card_vo, card_timing, sentence_too_long, allowlist_required, card_sync, stale_card, quote_before_speech

**reasons:**

- [B] Beats ≤3.0s incl. fade: FAIL — beat[2] type=quote mid card 4.74s incl. fade (hold 4.54s + 0.20s fade; limit 3.0s); beat[8] type=quote mid card 3.48s incl. fade (hold 3.28s + 0.20s fade; limit 3.0s). Mid cards/slides must be ≤3.0s including the fade (hold ≤2.80s); cta/endcard ≤3.9s including the fade (hold ≤3.78s) — split or add beats, never stretch a card (not a Follow-tomorrow hold).
- [B] Card sync: FAIL — card 3 (quote) at 8.94s appears 0.32s AFTER its speech (8.62s); card 4 (quote) at 10.62s appears 0.20s AFTER its speech (10.42s); card 5 (quote) at 13.04s appears 0.06s AFTER its speech (12.98s); card 10 (quote) at 25.36s appears 0.32s AFTER its speech (25.04s); card 11 (quote) at 27.30s appears 0.18s AFTER its speech (27.12s). A card never appears after its own words; it may lead them by ≤1.1s.
- [B] Card ⊂ VO: FAIL — card_fragment: card 7 'Registration works' is a piece of the sentence 'Registration works, and optional keys are left out instead of sent as null.': not a whole VO sentence nor an isolated term (has a verb (a clause, not a term)) (a card shows the whole sentence or an isolated term). Every text card shows one whole sentence the VO says (a long sentence may continue on the next card only with its literal next words), and each spoken sentence gets one card.
- [B] Card timing: FAIL — stale_card: card 3 type=quote ends at 10.50s, last spoken word at 9.98s (0.52s leftover; max 0.50s); stale_card: card 10 type=quote ends at 27.18s, last spoken word at 26.66s (0.52s leftover; max 0.50s). A quote enters at its first spoken word; a card (including the hook) may not stay up more than 0.5s after its last spoken word (stale_card / quote_before_speech).
- [B] Sentence too long: FAIL — sentence_too_long: card 2 'a tool that returns a TypedDict with a NotRequired key failed at registration' spoken 4.34s (first word 4.28s–last word 8.62s; max 3.0s) — split the sentence; sentence_too_long: card 8 'and optional keys are left out instead of sent as null.' spoken 3.08s (first word 19.72s–last word 22.80s; max 3.0s) — split the sentence. Any mid card (quotes included) may not cover more than 3.0s of speech (first word to last word). If no clause split lands ≤ that cap, rewrite the VO.
- [B] Allowlist required: FAIL — allowlist_required: topic 47 has no on_screen_allow. allowlist_required topics need overrides on_screen_allow.

---

# #1465 (topic 7, duration 30.12s)

BEFORE = board visível do `VM_FAIL_0465.md` (main `c35e110`). AFTER = `storyboard.compose` no script do snapshot + âncoras whisper/card_sync do áudio gravado (sem S1453/S1465 reescrito, sem timing sintético uniforme).

### BEFORE (main c35e110 / VM_FAIL)

| # | type | start | dur | end | text |
|---|------|------:|----:|----:|------|
| 0 | hook | 0.00 | 7.43 | 7.43 | The crew failed the handoff. tasks.yaml was empty. |
| 1 | term_define | 7.63 | 2.40 | 10.03 | CrewAI will not invent the work for you |
| 2 | term_define | 10.23 | 2.40 | 12.63 | crew runner only moves what that file declares |
| 3 | term_define | 12.83 | 2.40 | 15.23 | task needs a description, an expected output |
| 4 | compare | 15.47 | 1.30 | 16.77 | Needs an agent Handoff never starts |
| 5 | quote | 17.07 | 2.03 | 19.10 | handoff |
| 6 | compare | 19.33 | 2.37 | 21.70 | Crew logs will not fill Blank file a blank file |
| 7 | term_define | 21.93 | 1.30 | 23.23 | before you run write each task in tasks.yaml |
| 8 | quote | 23.57 | 0.86 | 24.43 | tasks yaml |
| 9 | quote | 24.77 | 2.26 | 27.03 | Empty file, empty crew. |
| 10 | cta | 27.27 | 2.86 | 30.13 | Subscribe · CrewAI |

**Gate B:** `FAIL` / result `FAIL`

**kinds:** card_text, os_cards, card_timing, sentence_too_long, card_exits_early, card_enters_early, ambiguous_cue, repeated_vo_sentence, enumeration_needs_rewrite, description_missing_source, card_sync, vs_card_not_allowed, single_term_card, echo_card, stale_card, quote_before_speech, unspoken_card

**reasons:**

- [B] Card text: FAIL — single_term_card: card 1 type=term_define 'CrewAI will not invent the work for you' is an isolated-term / term_define card (OS mid cards are whole VO sentences); single_term_card: card 2 type=term_define 'crew runner only moves what that file declares' is an isolated-term / term_define card (OS mid cards are whole VO sentences); single_term_card: card 3 type=term_define 'task needs a description, an expected output' is an isolated-term / term_define card (OS mid cards are whole VO sentences); vs_card_not_allowed: card 4 type=compare 'Needs an agent Handoff never starts' is a VS/list template (OS mid cards are whole VO sentences); single_term_card: card 5 type=quote 'handoff' is an isolated-term / term_define card (OS mid cards are whole VO sentences); echo_card: card 5 repeats term 'handoff' right after card 4 already showed it; vs_card_not_allowed: card 6 type=compare 'Crew logs will not fill Blank file a blank file' is a VS/list template (OS mid cards are whole VO sentences); single_term_card: card 7 type=term_define 'before you run write each task in tasks.yaml' is an isolated-term / term_define card (OS mid cards are whole VO sentences); single_term_card: card 8 type=quote 'tasks yaml' is an isolated-term / term_define card (OS mid cards are whole VO sentences); echo_card: card 8 repeats term 'tasks yaml' right after card 7 already showed it. One card per spoken sentence, quotes only of words the VO says, no invented tool output, PT-BR copy on PT cards, no back-to-back near-duplicate cards.
- [B] Card timing: FAIL — quote_before_speech: card 9 quote starts at 24.77s, first spoken word at 25.72s (must enter at the first word); stale_card: card 0 type=hook ends at 7.43s, last spoken word at 3.12s (4.31s leftover; max 0.50s); unspoken_card: card 4 type=compare 'Needs an agent Handoff never starts' has no spoken span (fail closed — every mid card must map to word timings); stale_card: card 5 type=quote ends at 19.10s, last spoken word at 1.32s (17.78s leftover; max 0.50s); unspoken_card: card 6 type=compare 'Crew logs will not fill Blank file a blank file' has no spoken span (fail closed — every mid card must map to word timings); unspoken_card: card 7 type=term_define 'before you run write each task in tasks.yaml' has no spoken span (fail closed — every mid card must map to word timings); stale_card: card 8 type=quote ends at 24.43s, last spoken word at 2.66s (21.77s leftover; max 0.50s); unspoken_card: card 4 type=compare 'Needs an agent Handoff never starts' has no spoken span (fail closed — every mid card must map to word timings); unspoken_card: card 6 type=compare 'Crew logs will not fill Blank file a blank file' has no spoken span (fail closed — every mid card must map to word timings); unspoken_card: card 7 type=term_define 'before you run write each task in tasks.yaml' has no spoken span (fail closed — every mid card must map to word timings); unspoken_card: card 4 type=compare 'Needs an agent Handoff never starts' has no spoken span (fail closed — every mid card must map to word timings); unspoken_card: card 6 type=compare 'Crew logs will not fill Blank file a blank file' has no spoken span (fail closed — every mid card must map to word timings); unspoken_card: card 7 type=term_define 'before you run write each task in tasks.yaml' has no spoken span (fail closed — every mid card must map to word timings); unspoken_card: card 4 type=compare 'Needs an agent Handoff never starts' has no spoken span (fail closed — every mid card must map to word timings); unspoken_card: card 6 type=compare 'Crew logs will not fill Blank file a blank file' has no spoken span (fail closed — every mid card must map to word timings); unspoken_card: card 7 type=term_define 'before you run write each task in tasks.yaml' has no spoken span (fail closed — every mid card must map to word timings). A quote enters at its first spoken word; a card (including the hook) may not stay up more than 0.5s after its last spoken word (stale_card / quote_before_speech).
- [B] Sentence too long: FAIL — unspoken_card: card 4 type=compare 'Needs an agent Handoff never starts' has no spoken span (fail closed — every mid card must map to word timings); unspoken_card: card 6 type=compare 'Crew logs will not fill Blank file a blank file' has no spoken span (fail closed — every mid card must map to word timings); unspoken_card: card 7 type=term_define 'before you run write each task in tasks.yaml' has no spoken span (fail closed — every mid card must map to word timings). Any mid card (quotes included) may not cover more than 3.0s of speech (first word to last word). If no clause split lands ≤ that cap, rewrite the VO.
- [B] Card exits early: FAIL — card_exits_early: card 2 ends at 12.63s, last spoken word at 12.66s (must hold through the last word + 0.20s pad); card_exits_early: card 3 ends at 15.23s, last spoken word at 16.02s (must hold through the last word + 0.20s pad); card_exits_early: card 9 ends at 27.03s, last spoken word at 27.12s (must hold through the last word + 0.20s pad). A card must hold through its last spoken word.
- [B] Card enters early: FAIL — card_enters_early: card 3 starts at 12.83s, first spoken word at 13.42s (lead 0.59s; max 0.3s); card_enters_early: card 9 starts at 24.77s, first spoken word at 25.72s (lead 0.95s; max 0.3s). A card may lead its speech by at most 0.3s.
- [B] Ambiguous cue: FAIL — ambiguous_cue: card 5 cue 'handoff' is 1 word(s) and occurs 2 times in the VO — spoken_span anchors on the first (need >=4 words or exactly one occurrence); ambiguous_cue: card 8 cue 'tasks yaml' is 2 word(s) and occurs 3 times in the VO — spoken_span anchors on the first (need >=4 words or exactly one occurrence). A cue shorter than 4 words must occur exactly once in the VO; spoken_span anchors on the first match.
- [B] Repeated VO sentence: FAIL — repeated_vo_sentence: VO sentence 'tasks.yaml was empty, so the next agent received nothing to do.' repeats sentence 1 — rewrite the VO. The same sentence must not be spoken twice — rewrite the VO.
- [B] Enumeration split: FAIL — enumeration_needs_rewrite: comma split would fragment the enumeration 'A task needs a description, an expected output, and an agent, or the handoff never starts.' into 'A task needs a description / an expected output / and an agent / or the handoff never starts.'; spoken 5.30s — rewrite the VO. A comma list is not a clause split; rewrite the VO.
- [B] Description source: FAIL — description_missing_source: description has no public source link (https://…). The description must cite a public source URL.

### AFTER (este branch, 1 frase da VO)

| # | type | start | dur | end | text |
|---|------|------:|----:|----:|------|
| 0 | hook | 0.00 | 3.62 | 3.62 | The crew failed the handoff. tasks.yaml was empty. |
| 1 | quote | 4.00 | 3.64 | 7.64 | tasks.yaml was empty, so the next agent received nothing to do. |
| 2 | quote | 7.76 | 2.56 | 10.32 | CrewAI will not invent the work for you. |
| 3 | quote | 10.44 | 2.64 | 13.08 | The crew runner only moves what that file declares. |
| 4 | quote | 13.30 | 5.50 | 18.80 | A task needs a description, an expected output, and an agent, or the handoff never starts. |
| 5 | quote | 19.32 | 2.84 | 22.16 | Staring at the crew logs will not fill a blank file. |
| 6 | quote | 22.50 | 2.98 | 25.48 | Write each task in tasks.yaml before you run. |
| 7 | quote | 25.72 | 1.82 | 27.54 | Empty file, empty crew. |
| 8 | cta | 27.54 | 2.58 | 30.12 | Subscribe · CrewAI same series |

**Gate B:** `FAIL` / result `FAIL`

**kinds:** beat_hold, card_text, os_cards, card_timing, sentence_too_long, repeated_vo_sentence, enumeration_needs_rewrite, description_missing_source, card_sync, stale_card, quote_before_speech

**reasons:**

- [B] Beats ≤3.0s incl. fade: FAIL — beat[1] type=quote mid card 3.84s incl. fade (hold 3.64s + 0.20s fade; limit 3.0s); beat[4] type=quote mid card 5.70s incl. fade (hold 5.50s + 0.20s fade; limit 3.0s); beat[5] type=quote mid card 3.04s incl. fade (hold 2.84s + 0.20s fade; limit 3.0s); beat[6] type=quote mid card 3.18s incl. fade (hold 2.98s + 0.20s fade; limit 3.0s). Mid cards/slides must be ≤3.0s including the fade (hold ≤2.80s); cta/endcard ≤3.9s including the fade (hold ≤3.78s) — split or add beats, never stretch a card (not a Follow-tomorrow hold).
- [B] Card sync: FAIL — card 2 (quote) at 7.76s appears 0.32s AFTER its speech (7.44s); card 3 (quote) at 10.44s appears 0.22s AFTER its speech (10.22s). A card never appears after its own words; it may lead them by ≤1.1s.
- [B] Card text: FAIL — quote_not_in_vo: card 1 quote 'tasks.yaml was empty, so the next agent received nothing to ' is not a verbatim VO substring (allowlist does not exempt quote cards). One card per spoken sentence, quotes only of words the VO says, no invented tool output, PT-BR copy on PT cards, no back-to-back near-duplicate cards.
- [B] OS cards: FAIL — quote_not_in_vo: card 1 quote 'tasks.yaml was empty, so the next agent received nothing to ' is not a verbatim VO substring (allowlist does not exempt quote cards). OS mid cards are one whole VO sentence (or its strong-clause units); no compare/VS, term_define, list, or isolated term; copy is VO-literal or allowlist-literal.
- [B] Card timing: FAIL — stale_card: card 2 type=quote ends at 10.32s, last spoken word at 9.80s (0.52s leftover; max 0.50s). A quote enters at its first spoken word; a card (including the hook) may not stay up more than 0.5s after its last spoken word (stale_card / quote_before_speech).
- [B] Sentence too long: FAIL — sentence_too_long: card 1 'tasks.yaml was empty, so the next agent received nothing to do.' spoken 3.44s (first word 4.00s–last word 7.44s; max 3.0s) — split the sentence; sentence_too_long: card 4 'A task needs a description, an expected output, and an agent, or the handoff never starts.' spoken 5.30s (first word 13.30s–last word 18.60s; max 3.0s) — split the sentence. Any mid card (quotes included) may not cover more than 3.0s of speech (first word to last word). If no clause split lands ≤ that cap, rewrite the VO.
- [B] Repeated VO sentence: FAIL — repeated_vo_sentence: VO sentence 'tasks.yaml was empty, so the next agent received nothing to do.' repeats sentence 1 — rewrite the VO. The same sentence must not be spoken twice — rewrite the VO.
- [B] Enumeration split: FAIL — enumeration_needs_rewrite: comma split would fragment the enumeration 'A task needs a description, an expected output, and an agent, or the handoff never starts.' into 'A task needs a description / an expected output / and an agent / or the handoff never starts.'; spoken 5.30s — rewrite the VO. A comma list is not a clause split; rewrite the VO.
- [B] Description source: FAIL — description_missing_source: description has no public source link (https://…). The description must cite a public source URL.

