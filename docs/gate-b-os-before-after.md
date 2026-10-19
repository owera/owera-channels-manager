# Gate B before/after — OS #1452, #1453, #1465

BEFORE = board visível dos `VM_FAIL_*.md` (main `c35e110`).
AFTER = gerador determinístico deste branch (1 frase da VO; quote literal segura o span inteiro + 0,3s; sem compare/term/list/termo isolado).

Quando o span falado de uma frase passa do cap mid e deixaria um buraco > 0,5s além do fade, a ordem é: (1) cláusulas fortes #96 (`; : — –`); (2) vírgula / conjunção coordenativa, cada peça um span contíguo da VO sem eco do card vizinho; (3) último recurso `cap_extended_spoken` — o card plain segura até a última palavra falada + 0,3s (nunca na frase seguinte). Gate B isenta esse hold do cap mid (não é `stale_card` nem `blank_gap`). Span falado (1ª→última palavra) > `SENTENCE_SPOKEN_MAX_S` (4,5s) no `cap_extended_spoken` ou quote literal = `sentence_too_long`.

`slide_spam` é o check Gate C existente (`STATEMENT_MAX_SHORTS = 1`): statement/list que só reexibe a narração sem tipo rico (code/command/diagram/compare/stat). O gerador OS não emite statement (allowlist log fica `code`) e junta peças curtas demais da mesma frase.

Âncoras de fala dos VM_FAIL (whisper). Resto das palavras preenche o vão entre âncoras até `duration − END_TAIL_S`. Sem render/TTS novo.

| vídeo | topic | BEFORE B | AFTER B | BEFORE kinds | AFTER kinds | hook BEFORE | hook AFTER |
|------:|------:|:--------:|:-------:|--------------|-------------|------------:|-----------:|
| #1452 | 47 | FAIL | PASS | card_text, os_cards, card_timing, vs_card_not_allowed, single_term_card, echo_card, stale_card, quote_before_speech | — | 2.33s | 1.88s |
| #1453 | 47 | FAIL | FAIL | card_text, os_cards, card_timing, allowlist_required, vs_card_not_allowed, single_term_card, echo_card, stale_card, quote_before_speech | allowlist_required | 2.33s | 1.98s |
| #1465 | 7 | FAIL | PASS | card_text, os_cards, card_timing, vs_card_not_allowed, single_term_card, echo_card, stale_card, quote_before_speech | — | 7.43s | 3.08s |

Tabelas por vídeo abaixo.


# #1452 (topic 47, duration 30.60s)

BEFORE = board visível do `VM_FAIL_21fe.md` (main `c35e110`). AFTER = `storyboard.compose` determinístico neste branch (sem LLM; âncoras de fala do VM_FAIL; resto das palavras preenche o vão entre âncoras).

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

**kinds:** card_text, os_cards, card_timing, vs_card_not_allowed, single_term_card, echo_card, stale_card, quote_before_speech

**reasons:**

- [B] Card text: FAIL — single_term_card: card 1 type=term_define 'add-dir flag that adds a directory' is an isolated-term / term_define card (OS mid cards are whole VO sentences); vs_card_not_allowed: card 2 type=compare 'Claude subagent Claude subagent every one On the spot failed' is a VS/list template (OS mid cards are whole VO sentences); single_term_card: card 4 type=term_define 'cache_control maximum of 4 blocks' is an isolated-term / term_define card (OS mid cards are whole VO sentences); echo_card: card 4 repeats term 'cache_control' right after card 3 already showed it; single_term_card: card 5 type=quote 'cache_control' is an isolated-term / term_define card (OS mid cards are whole VO sentences); echo_card: card 5 repeats term 'cache_control' right after card 4 already showed it; vs_card_not_allowed: card 6 type=compare 'Over the limit Found 5 Limit one block over' is a VS/list template (OS mid cards are whole VO sentences); single_term_card: card 8 type=term_define 'Copilot CLI update for Anthropic subagents' is an isolated-term / term_define card (OS mid cards are whole VO sentences); vs_card_not_allowed: card 9 type=compare 'Directories extra directories Subagents keep working' is a VS/list template (OS mid cards are whole VO sentences); single_term_card: card 10 type=term_define 'version exact version in the description' is an isolated-term / term_define card (OS mid cards are whole VO sentences); vs_card_not_allowed: card 12 type=compare "Subagent its error Parent not the parent's" is a VS/list template (OS mid cards are whole VO sentences). One card per spoken sentence, quotes only of words the VO says, no invented tool output, PT-BR copy on PT cards, no back-to-back near-duplicate cards.
- [B] Card timing: FAIL — quote_before_speech: card 3 quote starts at 7.07s, first spoken word at 8.04s (must enter at the first word); quote_before_speech: card 5 quote starts at 9.70s, first spoken word at 9.88s (must enter at the first word); stale_card: card 1 type=term_define ends at 4.93s, last spoken word at 4.33s (0.60s leftover; max 0.50s); stale_card: card 5 type=quote ends at 11.73s, last spoken word at 10.14s (1.59s leftover; max 0.50s). A quote enters at its first spoken word; a card (including the hook) may not stay up more than 0.5s after its last spoken word (stale_card / quote_before_speech).

### AFTER (este branch, 1 frase da VO)

| # | type | start | dur | end | text |
|---|------|------:|----:|----:|------|
| 0 | hook | 0.00 | 1.88 | 1.88 | One folder broke Copilot CLI. |
| 1 | quote | 2.00 | 2.48 | 4.48 | Add a directory with the add-dir flag |
| 2 | quote | 4.82 | 3.10 | 7.92 | and every Claude subagent failed on the spot. |
| 3 | quote | 8.04 | 3.31 | 11.35 | A maximum of 4 blocks with cache_control may be provided. |
| 4 | code | 11.94 | 0.50 | 12.44 | Found 5. |
| 5 | quote | 12.56 | 1.78 | 14.34 | One block over the limit. |
| 6 | quote | 14.46 | 2.48 | 16.94 | The parent agent got an empty answer. |
| 7 | quote | 17.34 | 1.11 | 18.45 | Update Copilot CLI |
| 8 | quote | 18.57 | 3.17 | 21.75 | and Anthropic subagents keep working with extra directories. |
| 9 | quote | 21.87 | 1.94 | 23.81 | Exact version in the description. |
| 10 | quote | 23.93 | 1.53 | 25.45 | Subagent came back empty? |
| 11 | quote | 25.57 | 2.35 | 27.92 | Read its error, not the parent's. |
| 12 | cta | 28.07 | 2.56 | 30.63 | Subscribe · Agent traps same series |

**Gate B:** `PASS` / result `PASS`

**kinds:** (none)

**reasons:**

- (none)

---

# #1453 (topic 47, duration 30.77s)

BEFORE = board visível do `VM_FAIL_c638.md` (main `c35e110`). AFTER = `storyboard.compose` determinístico neste branch (sem LLM; âncoras de fala do VM_FAIL; resto das palavras preenche o vão entre âncoras).

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

**kinds:** card_text, os_cards, card_timing, allowlist_required, vs_card_not_allowed, single_term_card, echo_card, stale_card, quote_before_speech

**reasons:**

- [B] Card text: FAIL — single_term_card: card 1 type=term_define 'Python three ten runtime where the tool failed' is an isolated-term / term_define card (OS mid cards are whole VO sentences); single_term_card: card 2 type=term_define 'NotRequired optional key on the TypedDict' is an isolated-term / term_define card (OS mid cards are whole VO sentences); single_term_card: card 3 type=quote 'registration' is an isolated-term / term_define card (OS mid cards are whole VO sentences); single_term_card: card 4 type=term_define 'Pydantic called the annotation invalid' is an isolated-term / term_define card (OS mid cards are whole VO sentences); vs_card_not_allowed: card 5 type=compare 'Python runtime three ten tool failed three eleven and up wer' is a VS/list template (OS mid cards are whole VO sentences); single_term_card: card 6 type=term_define 'MCP Python SDK update it and registration works' is an isolated-term / term_define card (OS mid cards are whole VO sentences); single_term_card: card 8 type=term_define 'Optional keys NotRequired fields on the return' is an isolated-term / term_define card (OS mid cards are whole VO sentences); echo_card: card 8 repeats term 'Optional keys' right after card 7 already showed it; vs_card_not_allowed: card 9 type=compare 'Instead of Left out not as null Sent as null null on the wir' is a VS/list template (OS mid cards are whole VO sentences); single_term_card: card 11 type=term_define 'Old Python test registration in prod' is an isolated-term / term_define card (OS mid cards are whole VO sentences); single_term_card: card 12 type=quote 'prod' is an isolated-term / term_define card (OS mid cards are whole VO sentences); echo_card: card 12 repeats term 'prod' right after card 11 already showed it. One card per spoken sentence, quotes only of words the VO says, no invented tool output, PT-BR copy on PT cards, no back-to-back near-duplicate cards.
- [B] Card timing: FAIL — quote_before_speech: card 3 quote starts at 6.97s, first spoken word at 9.16s (must enter at the first word); quote_before_speech: card 7 quote starts at 17.37s, first spoken word at 18.42s (must enter at the first word); stale_card: card 12 type=quote ends at 27.90s, last spoken word at 26.48s (1.42s leftover; max 0.50s). A quote enters at its first spoken word; a card (including the hook) may not stay up more than 0.5s after its last spoken word (stale_card / quote_before_speech).
- [B] Allowlist required: FAIL — allowlist_required: topic 47 has no on_screen_allow. allowlist_required topics need overrides on_screen_allow.

### AFTER (este branch, 1 frase da VO)

| # | type | start | dur | end | text |
|---|------|------:|----:|----:|------|
| 0 | hook | 0.00 | 1.98 | 1.98 | One type hint broke MCP tools. |
| 1 | quote | 2.10 | 2.48 | 4.58 | On Python three ten the tool failed. |
| 2 | quote | 5.19 | 3.41 | 8.60 | NotRequired is the optional key on the TypedDict. |
| 3 | quote | 8.72 | 0.76 | 9.48 | Clean registration. |
| 4 | quote | 9.60 | 2.09 | 11.68 | Pydantic called the annotation invalid. |
| 5 | quote | 11.80 | 2.48 | 14.28 | Python three eleven and up were fine. |
| 6 | quote | 14.89 | 3.41 | 18.30 | Update the MCP Python SDK and registration works. |
| 7 | quote | 18.42 | 3.96 | 22.38 | Registration works, and optional keys are left out instead of sent as null. |
| 8 | quote | 22.50 | 2.17 | 24.66 | Exact version in the description. |
| 9 | quote | 24.79 | 1.71 | 26.49 | Old Python in prod? |
| 10 | quote | 26.61 | 1.25 | 27.87 | Test registration there. |
| 11 | cta | 27.98 | 2.79 | 30.77 | Subscribe · Agent traps same series |

**Gate B:** `FAIL` / result `FAIL`

**kinds:** allowlist_required

**reasons:**

- [B] Allowlist required: FAIL — allowlist_required: topic 47 has no on_screen_allow. allowlist_required topics need overrides on_screen_allow.

---

# #1465 (topic 7, duration 30.13s)

BEFORE = board visível do `VM_FAIL_0465.md` (main `c35e110`). AFTER = `storyboard.compose` determinístico neste branch (sem LLM; âncoras de fala do VM_FAIL; resto das palavras preenche o vão entre âncoras).

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

**kinds:** card_text, os_cards, card_timing, vs_card_not_allowed, single_term_card, echo_card, stale_card, quote_before_speech

**reasons:**

- [B] Card text: FAIL — single_term_card: card 1 type=term_define 'CrewAI will not invent the work for you' is an isolated-term / term_define card (OS mid cards are whole VO sentences); single_term_card: card 2 type=term_define 'crew runner only moves what that file declares' is an isolated-term / term_define card (OS mid cards are whole VO sentences); single_term_card: card 3 type=term_define 'task needs a description, an expected output' is an isolated-term / term_define card (OS mid cards are whole VO sentences); vs_card_not_allowed: card 4 type=compare 'Needs an agent Handoff never starts' is a VS/list template (OS mid cards are whole VO sentences); single_term_card: card 5 type=quote 'handoff' is an isolated-term / term_define card (OS mid cards are whole VO sentences); echo_card: card 5 repeats term 'handoff' right after card 4 already showed it; vs_card_not_allowed: card 6 type=compare 'Crew logs will not fill Blank file a blank file' is a VS/list template (OS mid cards are whole VO sentences); single_term_card: card 7 type=term_define 'before you run write each task in tasks.yaml' is an isolated-term / term_define card (OS mid cards are whole VO sentences); single_term_card: card 8 type=quote 'tasks yaml' is an isolated-term / term_define card (OS mid cards are whole VO sentences); echo_card: card 8 repeats term 'tasks yaml' right after card 7 already showed it. One card per spoken sentence, quotes only of words the VO says, no invented tool output, PT-BR copy on PT cards, no back-to-back near-duplicate cards.
- [B] Card timing: FAIL — quote_before_speech: card 9 quote starts at 24.77s, first spoken word at 26.03s (must enter at the first word); stale_card: card 0 type=hook ends at 7.43s, last spoken word at 3.13s (4.30s leftover; max 0.50s); stale_card: card 1 type=term_define ends at 10.03s, last spoken word at 9.53s (0.50s leftover; max 0.50s); stale_card: card 5 type=quote ends at 19.10s, last spoken word at 1.75s (17.35s leftover; max 0.50s); stale_card: card 8 type=quote ends at 24.43s, last spoken word at 2.25s (22.18s leftover; max 0.50s). A quote enters at its first spoken word; a card (including the hook) may not stay up more than 0.5s after its last spoken word (stale_card / quote_before_speech).

### AFTER (este branch, 1 frase da VO)

| # | type | start | dur | end | text |
|---|------|------:|----:|----:|------|
| 0 | hook | 0.00 | 3.08 | 3.08 | The crew failed the handoff. tasks.yaml was empty. |
| 1 | quote | 3.20 | 2.80 | 6.00 | So the next agent received nothing to do. |
| 2 | quote | 6.40 | 2.80 | 9.20 | CrewAI will not invent the work for you. |
| 3 | quote | 9.61 | 3.48 | 13.09 | The crew runner only moves what that file declares. |
| 4 | quote | 13.21 | 1.88 | 15.09 | A task needs a description |
| 5 | quote | 15.21 | 1.08 | 16.30 | an expected output |
| 6 | quote | 16.41 | 1.42 | 17.84 | and an agent. |
| 7 | quote | 17.62 | 1.48 | 19.10 | The handoff never starts. |
| 8 | quote | 19.22 | 3.48 | 22.70 | The crew logs will not fill a blank file. |
| 9 | quote | 22.82 | 2.80 | 25.62 | Before you run, write each task in tasks.yaml. |
| 10 | quote | 26.03 | 1.48 | 27.51 | Empty file, empty crew. |
| 11 | cta | 27.67 | 2.50 | 30.17 | Subscribe · CrewAI same series |

**Gate B:** `PASS` / result `PASS`

**kinds:** (none)

**reasons:**

- (none)

