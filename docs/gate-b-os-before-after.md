# Gate B before/after — OS #1452, #1453, #1465

BEFORE = board visível dos `VM_FAIL_*.md` (main `c35e110`).
AFTER = gerador determinístico deste branch no **script do snapshot** (VO gravada) + âncoras whisper/`card_sync` do áudio real. Sem script S1453/S1465 reescrito e sem timing sintético uniforme. O check literal compara o card com as palavras faladas/renderizadas.

`SENTENCE_SPOKEN_MAX_S = 3.0` vale para **todo** mid card, quote literal incluída (sem isenção de span). Se nenhum split de cláusula cabe em ≤3,0s — ou o split de vírgula quebraria uma enumeração — Gate B FALLA e o script é que se parte.

Checks novos: `card_exits_early`, `card_enters_early` (lead > 0,3s), `card_overlap`, `cta_after_video_end`, `repeated_vo_sentence`, `code_read_too_short` (piso 1,2s em code/command), `description_missing_source`, `enumeration_needs_rewrite`.

`slide_spam` é o check Gate C existente (`STATEMENT_MAX_SHORTS = 1`).

Âncoras: frases do VM_FAIL (whisper small+medium) + `card_sync.speech_start` do snapshot. Palavras entre âncoras interpolam só o vão adjacente.

| vídeo | topic | BEFORE B | AFTER B | BEFORE kinds | AFTER kinds | hook BEFORE | hook AFTER |
|------:|------:|:--------:|:-------:|--------------|-------------|------------:|-----------:|
| #1452 | 47 | FAIL | FAIL | card_text, os_cards, card_timing, sentence_too_long, card_exits_early, card_enters_early, vs_card_not_allowed, single_term_card, echo_card, stale_card, quote_before_speech | beat_hold, card_timing, sentence_too_long, card_overlap, cta_after_video_end, code_read_too_short, stale_card, quote_before_speech | 2.33s | 2.38s |
| #1453 | 47 | FAIL | FAIL | card_text, os_cards, card_timing, sentence_too_long, card_exits_early, card_enters_early, allowlist_required, vs_card_not_allowed, single_term_card, echo_card, stale_card, quote_before_speech | blank_gap, sentence_too_long, card_exits_early, allowlist_required | 2.33s | 2.38s |
| #1465 | 7 | FAIL | FAIL | card_text, os_cards, card_timing, card_exits_early, card_enters_early, repeated_vo_sentence, enumeration_needs_rewrite, description_missing_source, vs_card_not_allowed, single_term_card, card_text_not_literal, echo_card, stale_card, quote_before_speech | blank_gap, card_text, os_cards, sentence_too_long, repeated_vo_sentence, enumeration_needs_rewrite, description_missing_source, card_text_not_literal | 7.43s | 3.54s |

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

**kinds:** card_text, os_cards, card_timing, sentence_too_long, card_exits_early, card_enters_early, vs_card_not_allowed, single_term_card, echo_card, stale_card, quote_before_speech

**reasons:**

- [B] Card text: FAIL — single_term_card: card 1 type=term_define 'add-dir flag that adds a directory' is an isolated-term / term_define card (OS mid cards are whole VO sentences); vs_card_not_allowed: card 2 type=compare 'Claude subagent Claude subagent every one On the spot failed' is a VS/list template (OS mid cards are whole VO sentences); single_term_card: card 4 type=term_define 'cache_control maximum of 4 blocks' is an isolated-term / term_define card (OS mid cards are whole VO sentences); echo_card: card 4 repeats term 'cache_control' right after card 3 already showed it; single_term_card: card 5 type=quote 'cache_control' is an isolated-term / term_define card (OS mid cards are whole VO sentences); echo_card: card 5 repeats term 'cache_control' right after card 4 already showed it; vs_card_not_allowed: card 6 type=compare 'Over the limit Found 5 Limit one block over' is a VS/list template (OS mid cards are whole VO sentences); single_term_card: card 8 type=term_define 'Copilot CLI update for Anthropic subagents' is an isolated-term / term_define card (OS mid cards are whole VO sentences); vs_card_not_allowed: card 9 type=compare 'Directories extra directories Subagents keep working' is a VS/list template (OS mid cards are whole VO sentences); single_term_card: card 10 type=term_define 'version exact version in the description' is an isolated-term / term_define card (OS mid cards are whole VO sentences); vs_card_not_allowed: card 12 type=compare "Subagent its error Parent not the parent's" is a VS/list template (OS mid cards are whole VO sentences). One card per spoken sentence, quotes only of words the VO says, no invented tool output, PT-BR copy on PT cards, no back-to-back near-duplicate cards.
- [B] Card timing: FAIL — quote_before_speech: card 3 quote starts at 7.07s, first spoken word at 8.04s (must enter at the first word); quote_before_speech: card 5 quote starts at 9.70s, first spoken word at 9.88s (must enter at the first word); stale_card: card 5 type=quote ends at 11.73s, last spoken word at 10.14s (1.59s leftover; max 0.50s). A quote enters at its first spoken word; a card (including the hook) may not stay up more than 0.5s after its last spoken word (stale_card / quote_before_speech).
- [B] Sentence too long: FAIL — sentence_too_long: card 3 'A maximum of 4 blocks with cache_control may be provided.' spoken 3.01s (first word 8.04s–last word 11.05s; max 3.0s) — split the sentence. Any mid card (quotes included) may not cover more than 3.0s of speech (first word to last word). If no clause split lands ≤ that cap, rewrite the VO.
- [B] Card exits early: FAIL — card_exits_early: card 3 ends at 7.83s, last spoken word at 11.05s (must hold through the last word). A card must hold through its last spoken word.
- [B] Card enters early: FAIL — card_enters_early: card 1 starts at 2.53s, first spoken word at 4.16s (lead 1.63s; max 0.3s); card_enters_early: card 3 starts at 7.07s, first spoken word at 8.04s (lead 0.97s; max 0.3s). A card may lead its speech by at most 0.3s.

### AFTER (este branch, 1 frase da VO)

| # | type | start | dur | end | text |
|---|------|------:|----:|----:|------|
| 0 | hook | 0.00 | 2.38 | 2.38 | One folder broke Copilot CLI. |
| 1 | quote | 2.55 | 2.48 | 5.03 | Add a directory with the add-dir flag |
| 2 | quote | 5.31 | 2.48 | 7.79 | and every Claude subagent failed on the spot. |
| 3 | quote | 8.04 | 3.31 | 11.35 | A maximum of 4 blocks with cache_control may be provided. |
| 4 | code | 11.94 | 0.50 | 12.44 | Found 5. |
| 5 | quote | 12.56 | 1.95 | 14.51 | One block over the limit. |
| 6 | quote | 14.62 | 1.92 | 16.54 | The parent agent got an empty answer. |
| 7 | quote | 16.66 | 3.04 | 19.70 | Update Copilot CLI |
| 8 | quote | 19.82 | 2.43 | 22.26 | and Anthropic subagents keep working with extra directories. |
| 9 | quote | 22.20 | 2.12 | 24.32 | Exact version in the description. |
| 10 | quote | 24.44 | 1.52 | 25.95 | Subagent came back empty? |
| 11 | quote | 26.07 | 2.29 | 28.37 | Read its error, not the parent's. |
| 12 | cta | 28.67 | 2.28 | 30.95 | Subscribe · Agent traps same series |

**Gate B:** `FAIL` / result `FAIL`

**kinds:** beat_hold, card_timing, sentence_too_long, card_overlap, cta_after_video_end, code_read_too_short, stale_card, quote_before_speech

**reasons:**

- [B] Beats ≤3.0s incl. fade: FAIL — beat[7] type=quote mid card 3.24s incl. fade (hold 3.04s + 0.20s fade; limit 3.0s). Mid cards/slides must be ≤3.0s including the fade (hold ≤2.80s); cta/endcard ≤3.9s including the fade (hold ≤3.78s) — split or add beats, never stretch a card (not a Follow-tomorrow hold).
- [B] Card timing: FAIL — stale_card: card 7 type=quote ends at 19.70s, last spoken word at 18.12s (1.58s leftover; max 0.50s). A quote enters at its first spoken word; a card (including the hook) may not stay up more than 0.5s after its last spoken word (stale_card / quote_before_speech).
- [B] Sentence too long: FAIL — sentence_too_long: card 3 'A maximum of 4 blocks with cache_control may be provided.' spoken 3.01s (first word 8.04s–last word 11.05s; max 3.0s) — split the sentence. Any mid card (quotes included) may not cover more than 3.0s of speech (first word to last word). If no clause split lands ≤ that cap, rewrite the VO.
- [B] Card overlap: FAIL — card_overlap: card 8 ends at 22.26s, card 9 starts at 22.20s (cards must not overlap). Two cards must not share the same visual interval.
- [B] CTA after video end: FAIL — cta_after_video_end: card 12 CTA ends at 30.95s, video ends at 30.59s. The CTA must end at or before the video duration.
- [B] Code read time: FAIL — code_read_too_short: card 4 'Found 5.' held 0.50s (min 1.2s to read a code/command line). Code/command cards need ≥1.2s on screen.

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

**kinds:** card_text, os_cards, card_timing, sentence_too_long, card_exits_early, card_enters_early, allowlist_required, vs_card_not_allowed, single_term_card, echo_card, stale_card, quote_before_speech

**reasons:**

- [B] Card text: FAIL — single_term_card: card 1 type=term_define 'Python three ten runtime where the tool failed' is an isolated-term / term_define card (OS mid cards are whole VO sentences); single_term_card: card 2 type=term_define 'NotRequired optional key on the TypedDict' is an isolated-term / term_define card (OS mid cards are whole VO sentences); single_term_card: card 3 type=quote 'registration' is an isolated-term / term_define card (OS mid cards are whole VO sentences); single_term_card: card 4 type=term_define 'Pydantic called the annotation invalid' is an isolated-term / term_define card (OS mid cards are whole VO sentences); vs_card_not_allowed: card 5 type=compare 'Python runtime three ten tool failed three eleven and up wer' is a VS/list template (OS mid cards are whole VO sentences); single_term_card: card 6 type=term_define 'MCP Python SDK update it and registration works' is an isolated-term / term_define card (OS mid cards are whole VO sentences); single_term_card: card 8 type=term_define 'Optional keys NotRequired fields on the return' is an isolated-term / term_define card (OS mid cards are whole VO sentences); echo_card: card 8 repeats term 'Optional keys' right after card 7 already showed it; vs_card_not_allowed: card 9 type=compare 'Instead of Left out not as null Sent as null null on the wir' is a VS/list template (OS mid cards are whole VO sentences); single_term_card: card 11 type=term_define 'Old Python test registration in prod' is an isolated-term / term_define card (OS mid cards are whole VO sentences); single_term_card: card 12 type=quote 'prod' is an isolated-term / term_define card (OS mid cards are whole VO sentences); echo_card: card 12 repeats term 'prod' right after card 11 already showed it. One card per spoken sentence, quotes only of words the VO says, no invented tool output, PT-BR copy on PT cards, no back-to-back near-duplicate cards.
- [B] Card timing: FAIL — quote_before_speech: card 3 quote starts at 6.97s, first spoken word at 9.44s (must enter at the first word); quote_before_speech: card 7 quote starts at 17.37s, first spoken word at 18.42s (must enter at the first word); stale_card: card 12 type=quote ends at 27.90s, last spoken word at 26.46s (1.44s leftover; max 0.50s). A quote enters at its first spoken word; a card (including the hook) may not stay up more than 0.5s after its last spoken word (stale_card / quote_before_speech).
- [B] Sentence too long: FAIL — sentence_too_long: card 7 'Registration works, and optional keys are left out instead of sent as null.' spoken 4.03s (first word 18.42s–last word 22.45s; max 3.0s) — split the sentence. Any mid card (quotes included) may not cover more than 3.0s of speech (first word to last word). If no clause split lands ≤ that cap, rewrite the VO.
- [B] Card exits early: FAIL — card_exits_early: card 3 ends at 9.23s, last spoken word at 9.74s (must hold through the last word); card_exits_early: card 4 ends at 11.83s, last spoken word at 12.63s (must hold through the last word); card_exits_early: card 7 ends at 18.53s, last spoken word at 22.45s (must hold through the last word). A card must hold through its last spoken word.
- [B] Card enters early: FAIL — card_enters_early: card 3 starts at 6.97s, first spoken word at 9.44s (lead 2.47s; max 0.3s); card_enters_early: card 4 starts at 9.47s, first spoken word at 10.28s (lead 0.80s; max 0.3s); card_enters_early: card 7 starts at 17.37s, first spoken word at 18.42s (lead 1.05s; max 0.3s). A card may lead its speech by at most 0.3s.
- [B] Allowlist required: FAIL — allowlist_required: topic 47 has no on_screen_allow. allowlist_required topics need overrides on_screen_allow.

### AFTER (este branch, 1 frase da VO)

| # | type | start | dur | end | text |
|---|------|------:|----:|----:|------|
| 0 | hook | 0.00 | 2.38 | 2.38 | One type hint broke MCP tools. |
| 1 | quote | 2.54 | 1.51 | 4.05 | On Python three ten |
| 2 | quote | 5.10 | 4.58 | 9.68 | a tool that returns a TypedDict with a NotRequired key failed at registration |
| 3 | quote | 9.80 | 0.50 | 10.30 | before it ever ran. |
| 4 | quote | 10.42 | 2.32 | 12.74 | Pydantic called the annotation invalid. |
| 5 | quote | 12.86 | 2.24 | 15.10 | Python three eleven and up were fine. |
| 6 | quote | 15.43 | 2.02 | 17.45 | Update the MCP Python SDK. |
| 7 | quote | 19.05 | 3.71 | 22.75 | and optional keys are left out instead of sent as null. |
| 8 | quote | 23.12 | 1.74 | 24.86 | Exact version in the description. |
| 9 | quote | 25.35 | 1.13 | 26.48 | Old Python in prod? |
| 10 | quote | 26.60 | 1.74 | 28.34 | Test registration there. |
| 11 | cta | 28.49 | 2.27 | 30.76 | Subscribe · Agent traps same series |

**Gate B:** `FAIL` / result `FAIL`

**kinds:** blank_gap, sentence_too_long, card_exits_early, allowlist_required

**reasons:**

- [B] Blank gap: FAIL — beat[1]→beat[2] hole at 4.05s of 1.05s (0.93s beyond 0.12s fade; max 0.50s extra); beat[6]→beat[7] hole at 17.45s of 1.60s (1.48s beyond 0.12s fade; max 0.50s extra). A card owns its hold + the 0.12s inter-beat fade; a hole longer than 0.5s beyond that fade is a blank screen (OS and RR).
- [B] Sentence too long: FAIL — sentence_too_long: card 2 'a tool that returns a TypedDict with a NotRequired key failed at registration' spoken 4.64s (first word 5.10s–last word 9.74s; max 3.0s) — split the sentence; sentence_too_long: card 7 'and optional keys are left out instead of sent as null.' spoken 3.41s (first word 19.05s–last word 22.45s; max 3.0s) — split the sentence. Any mid card (quotes included) may not cover more than 3.0s of speech (first word to last word). If no clause split lands ≤ that cap, rewrite the VO.
- [B] Card exits early: FAIL — card_exits_early: card 2 ends at 9.68s, last spoken word at 9.74s (must hold through the last word). A card must hold through its last spoken word.
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

**kinds:** card_text, os_cards, card_timing, card_exits_early, card_enters_early, repeated_vo_sentence, enumeration_needs_rewrite, description_missing_source, vs_card_not_allowed, single_term_card, card_text_not_literal, echo_card, stale_card, quote_before_speech

**reasons:**

- [B] Card text: FAIL — card_text_not_literal: card 0 type=hook 'The crew failed the handoff. tasks.yaml was empty.' is not VO-literal or allowlist-literal; single_term_card: card 1 type=term_define 'CrewAI will not invent the work for you' is an isolated-term / term_define card (OS mid cards are whole VO sentences); single_term_card: card 2 type=term_define 'crew runner only moves what that file declares' is an isolated-term / term_define card (OS mid cards are whole VO sentences); single_term_card: card 3 type=term_define 'task needs a description, an expected output' is an isolated-term / term_define card (OS mid cards are whole VO sentences); vs_card_not_allowed: card 4 type=compare 'Needs an agent Handoff never starts' is a VS/list template (OS mid cards are whole VO sentences); single_term_card: card 5 type=quote 'handoff' is an isolated-term / term_define card (OS mid cards are whole VO sentences); echo_card: card 5 repeats term 'handoff' right after card 4 already showed it; vs_card_not_allowed: card 6 type=compare 'Crew logs will not fill Blank file a blank file' is a VS/list template (OS mid cards are whole VO sentences); single_term_card: card 7 type=term_define 'before you run write each task in tasks.yaml' is an isolated-term / term_define card (OS mid cards are whole VO sentences); single_term_card: card 8 type=quote 'tasks yaml' is an isolated-term / term_define card (OS mid cards are whole VO sentences); echo_card: card 8 repeats term 'tasks yaml' right after card 7 already showed it. One card per spoken sentence, quotes only of words the VO says, no invented tool output, PT-BR copy on PT cards, no back-to-back near-duplicate cards.
- [B] Card timing: FAIL — quote_before_speech: card 9 quote starts at 24.77s, first spoken word at 25.71s (must enter at the first word); stale_card: card 0 type=hook ends at 7.43s, last spoken word at 3.13s (4.30s leftover; max 0.50s); stale_card: card 5 type=quote ends at 19.10s, last spoken word at 1.75s (17.35s leftover; max 0.50s); stale_card: card 8 type=quote ends at 24.43s, last spoken word at 2.25s (22.18s leftover; max 0.50s). A quote enters at its first spoken word; a card (including the hook) may not stay up more than 0.5s after its last spoken word (stale_card / quote_before_speech).
- [B] Card exits early: FAIL — card_exits_early: card 3 ends at 15.23s, last spoken word at 16.29s (must hold through the last word). A card must hold through its last spoken word.
- [B] Card enters early: FAIL — card_enters_early: card 3 starts at 12.83s, first spoken word at 13.67s (lead 0.84s; max 0.3s); card_enters_early: card 9 starts at 24.77s, first spoken word at 25.71s (lead 0.94s; max 0.3s). A card may lead its speech by at most 0.3s.
- [B] Repeated VO sentence: FAIL — repeated_vo_sentence: VO sentence 'tasks.yaml was empty, so the next agent received nothing to do.' repeats sentence 1 — rewrite the VO. The same sentence must not be spoken twice — rewrite the VO.
- [B] Enumeration split: FAIL — enumeration_needs_rewrite: comma split would fragment the enumeration 'A task needs a description, an expected output, and an agent, or the handoff never starts.' into 'A task needs a description / an expected output / and an agent / or the handoff never starts.'; spoken 5.77s — rewrite the VO. A comma list is not a clause split; rewrite the VO.
- [B] Description source: FAIL — description_missing_source: description has no public source link (https://…). The description must cite a public source URL.

### AFTER (este branch, 1 frase da VO)

| # | type | start | dur | end | text |
|---|------|------:|----:|----:|------|
| 0 | hook | 0.00 | 3.54 | 3.54 | The crew failed the handoff. tasks.yaml was empty. |
| 1 | quote | 3.66 | 4.11 | 7.77 | tasks.yaml was empty, so the next agent received nothing to do. |
| 2 | quote | 7.89 | 2.19 | 10.08 | CrewAI will not invent the work for you. |
| 3 | quote | 10.20 | 2.80 | 13.00 | The crew runner only moves what that file declares. |
| 4 | quote | 13.29 | 5.87 | 19.16 | A task needs a description, an expected output, and an agent, or the handoff never starts. |
| 5 | quote | 19.27 | 2.80 | 22.07 | Staring at the crew logs will not fill a blank file. |
| 6 | quote | 22.46 | 2.30 | 24.76 | Write each task in tasks.yaml before you run. |
| 7 | quote | 25.71 | 1.74 | 27.45 | Empty file, empty crew. |
| 8 | cta | 27.62 | 2.50 | 30.12 | Subscribe · CrewAI same series |

**Gate B:** `FAIL` / result `FAIL`

**kinds:** blank_gap, card_text, os_cards, sentence_too_long, repeated_vo_sentence, enumeration_needs_rewrite, description_missing_source, card_text_not_literal

**reasons:**

- [B] Blank gap: FAIL — beat[6]→beat[7] hole at 24.76s of 0.95s (0.83s beyond 0.12s fade; max 0.50s extra). A card owns its hold + the 0.12s inter-beat fade; a hole longer than 0.5s beyond that fade is a blank screen (OS and RR).
- [B] Card text: FAIL — card_text_not_literal: card 0 type=hook 'The crew failed the handoff. tasks.yaml was empty.' is not VO-literal or allowlist-literal. One card per spoken sentence, quotes only of words the VO says, no invented tool output, PT-BR copy on PT cards, no back-to-back near-duplicate cards.
- [B] Sentence too long: FAIL — sentence_too_long: card 1 'tasks.yaml was empty, so the next agent received nothing to do.' spoken 3.89s (first word 3.66s–last word 7.55s; max 3.0s) — split the sentence; sentence_too_long: card 4 'A task needs a description, an expected output, and an agent, or the handoff never starts.' spoken 5.77s (first word 13.29s–last word 19.05s; max 3.0s) — split the sentence. Any mid card (quotes included) may not cover more than 3.0s of speech (first word to last word). If no clause split lands ≤ that cap, rewrite the VO.
- [B] Repeated VO sentence: FAIL — repeated_vo_sentence: VO sentence 'tasks.yaml was empty, so the next agent received nothing to do.' repeats sentence 1 — rewrite the VO. The same sentence must not be spoken twice — rewrite the VO.
- [B] Enumeration split: FAIL — enumeration_needs_rewrite: comma split would fragment the enumeration 'A task needs a description, an expected output, and an agent, or the handoff never starts.' into 'A task needs a description / an expected output / and an agent / or the handoff never starts.'; spoken 5.77s — rewrite the VO. A comma list is not a clause split; rewrite the VO.
- [B] Description source: FAIL — description_missing_source: description has no public source link (https://…). The description must cite a public source URL.

