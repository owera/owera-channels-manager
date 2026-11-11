# RR sentence pace — CMO-corrected sample + Gate B evidence (PO CM cut, 2026-11-10)

CMO voice pass 2026-11-10 FAILed the first sample (echo 'cada valor. Cada valor' / 'a página. Eu abro a página', a dropped 'eu', the list 'contrato, laudo e nota'). This is CMO's corrected 12-sentence script. Pipeline: stub LLM output (`tests/fixtures/rr_pace/stub_llm_1464_style.txt`, the same script with 2 sentences joined by ', e') → `worker._generate_script` (brand rr, pace 2.2s/2.5s), whose post-pass yields exactly the CMO script → real edge-tts 7.2.8 pt-BR-AntonioNeural (-8%, -2Hz, hook join as in the worker) → `storyboard.compose` brand rr → #201 Gate B (`video_maker_gate` + `card_timing_hits` + `blank_gap_hits`). Reproduced offline by `tests/verify_sentence_pace.py` from `tests/fixtures/rr_pace/sample_vo.json`. The post-pass leaves the CMO script unchanged (no change, no WARN). No paid LLM calls, no renders.

## Sample script

```
Pedir resumo não é engenharia.
O modelo escolhe sozinho o que importa.
O detalhe certo fica de fora.
A multa da cláusula 14 some.
Ninguém percebe.
Eu pergunto por campo.
Cada valor volta com a sua página.
Eu vou lá e confiro o número.
Campo sem página eu trato como chute.
Serve pra contrato e pra laudo.
Se inscreve.
Próxima armadilha de IA.
```

## Post-pass changes (stub → sample)

| # | kind | before (est s) | after (est s) |
|---|---|---|---|
| 1 | split | O modelo escolhe sozinho o que importa, e o detalhe certo fica de fora. (4.68) | O modelo escolhe sozinho o que importa. / O detalhe certo fica de fora. (2.5, 1.999) |
| 2 | split | A multa da cláusula 14 some, e ninguém percebe. (3.585) | A multa da cláusula 14 some. / Ninguém percebe. (2.319, 1.085) |

Dropped: ['e', 'e'] (split conjunction only). WARNs: none.

## Duration before / after

| | sentences | est total | real audio | video | longest sentence (real) |
|---|---|---|---|---|---|
| before (stub, 2 joined) | 10 | 31.24s | 31.51s | 31.07s | 4.44s |
| after (CMO sample) | 12 | 32.75s | 32.95s | 32.50s | 2.38s |

2 splits → +1.44s real audio (≈0.72s per split).

## Gate B

Result: **PASS** {'A': 'PASS', 'B': 'PASS', 'C': 'PASS'}; reasons: none; card_timing: none; blank_gap: none; card_sync: none. CTA chip lead over spoken 'Se inscreve.': -0.00s (RR max 0.3s).

| sentence | est s | real s |
|---|---|---|
| Pedir resumo não é engenharia. | 1.934 | 1.81 |
| O modelo escolhe sozinho o que importa. | 2.5 | 2.28 |
| O detalhe certo fica de fora. | 1.999 | 1.97 |
| A multa da cláusula 14 some. | 2.319 | 2.38 |
| Ninguém percebe. | 1.085 | 1.24 |
| Eu pergunto por campo. | 1.433 | 1.44 |
| Cada valor volta com a sua página. | 2.173 | 2.25 |
| Eu vou lá e confiro o número. | 2.064 | 1.81 |
| Campo sem página eu trato como chute. | 2.282 | 2.36 |
| Serve pra contrato e pra laudo. | 1.89 | 2.04 |
| Se inscreve. | 0.976 | 0.90 |
| Próxima armadilha de IA. | 1.76 | 1.77 |

Board:

| type | start | dur | text |
|---|---|---|---|
| hook | 0.0 | 2.07 | Pedir resumo não é engenharia. |
| quote | 2.19 | 2.65 | O modelo escolhe sozinho o que importa. |
| quote | 5.41 | 2.34 | O detalhe certo fica de fora. |
| quote | 8.31 | 2.75 | A multa da cláusula 14 some. |
| quote | 11.63 | 1.6 | Ninguém percebe. |
| quote | 13.8 | 1.81 | Eu pergunto por campo. |
| quote | 16.18 | 2.62 | Cada valor volta com a sua página. |
| quote | 19.38 | 2.17 | Eu vou lá e confiro o número. |
| quote | 22.12 | 2.73 | Campo sem página eu trato como chute. |
| quote | 25.42 | 2.49 | Serve pra contrato e pra laudo. |
| cta | 28.4 | 3.78 | Se inscreve · IA |

## Previous (pre-voice-pass) sample

`tests/fixtures/rr_pace/sample_vo_presplit.json` (first sample, pre-closer sentence 2.50s real) is kept as the CTA-lead regression: chip leads 0.07s (≤0.3s), Gate B PASS.

## 2026-11-11 — real grok run, RR hook-pace WARN, CMO voice rules v2

### Real generation (2 offline `grok -p` calls, Rodrigo yes via CoS; no live writes)

`_generate_script` (compose seam, effort medium) on claw0 scratch dir; replayed on the box through the post-pass + real edge-tts + Gate B.

| call | subject | post-pass | longest real | audio | first cut | Gate B |
|---|---|---|---|---|---|---|
| 1 | Perguntar se está certo não é engenharia. · IA 236 | unchanged, no WARN | 2.51s (hook) | 43.73s | 2.80s (joined) | FAIL B: RR hook pace |
| 2 | Pedir um sim não é engenharia. · IA 236 | unchanged, no WARN | 2.28s | 38.88s | 2.08s (joined) | PASS A/B/C, CTA lead 0.00s |

Call-2's raw script is `tests/fixtures/rr_pace/real_grok_call2_raw.txt`. CMO's voice pass FAILed it ('sim' ×4, 'é teatro' / 'como recibo', bare 'Perguntei', 'ele', 14 sentences).

### Hook-pace WARN (RR, check-only)

- Title prompt (`video_gen.generate_ideas`, RR via `idea_pace_kwargs` → `sentence_pace.pace_for`): `HOOK LENGTH: … aim for it to be spoken in {cap_s}s or less`. OS prompt byte-identical.
- Post-pass: hook (sentence 0) estimated over `cap_s` → WARN `hook pace: est X.XXs over the 2.5s cap — hook kept as written (shorter title head): <hook>`; job detail `; WARN hook pace: …`; same `creation_config.sentence_pace_post.warnings` / `sentence_pace_warnings` / `manager.worker` WARNING paths. The hook is never rewritten.
- **Gap (open, PO):** the estimator puts the call-1 hook at **2.391s** (real 2.51s, first cut 2.80s), so at `cap_s` = 2.5s it does **not** WARN. Estimator unchanged. Proposal: threshold the hook at `target_s` (2.2s) — first cut ≈ hook + ~0.3s join pause, so the 2.5s first-cut limit means a hook ≲ 2.2s. On the 6 probed heads that separates every Gate B fail (est 2.28 / 2.39) from every pass (est ≤ 2.04).

### CMO voice rules v2 (RR prompt + check-only WARNs)

Prompt (`sentence_pace.voice_guidance`): keyword in ≤2 sentences outside the hook; no metaphor/aphorism; explicit 'Eu'; 'ele/ela' only for the previous subject; problem → example → method → check; ≤12 sentences incl. closer, ≈35s. WARNs (never rewrite): keyword stem in >2 sentences outside hook/closer; >12 sentences or est total >35s; sentence opening on a bare 1st-person verb ('-ei' preterite ≥5 letters or a listed present 1sg). Metaphor, 'ele/ela' and order are prompt-only (no reliable cheap check).

Call-2 raw → WARN `keyword 'sim' in 4 sentences`, `script length: 14 sentences (max 12), est 38.6s total`, `bare first-person verb: Perguntei se o fluxo estava certo.`

### CMO v2 sample (`tests/fixtures/rr_pace/sample_script_v2.txt`)

Post-pass: unchanged, no new-rule WARN, but **2 pre-existing WARNs**: `over 2.5s cap (est 2.59s), unsplittable: Se tudo passa, eu desconfio do teste.` and `echo: Então eu monto o caso que quebra. | Eu rodo esse caso antes do merge.`

Real edge-tts (hook join on; s2 2.867s → 2.092s): audio 35.04s, video 34.59s, est 34.38s. Gate B **FAIL** {'A': 'PASS', 'B': 'FAIL', 'C': 'PASS'}: ['[B] Blank gap: FAIL — beat[8]→beat[9] hole at 23.94s of 0.64s (0.52s beyond 0.12s fade; max 0.50s extra). A card owns its hold + the 0.12s inter-beat fade; a hole longer than 0.5s beyond that fade is a blank screen (OS and RR).']. CTA lead 0.0s.

| # | sentence | est s | real s |
|---|---|---|---|
| 0 | Pedir um sim não é engenharia. | 1.89 | 1.712 |
| 1 | O modelo concorda sem pensar. | 1.825 | 1.929 |
| 2 | Eu perguntei se o fluxo estava certo. | 2.282 | 2.418 |
| 3 | A resposta veio rápida e vazia. | 1.999 | 2.201 |
| 4 | Um aceite bonito não valida a regra. | 2.391 | 2.283 |
| 5 | Então eu monto o caso que quebra. | 2.064 | 2.038 |
| 6 | Eu rodo esse caso antes do merge. | 2.173 | 2.242 |
| 7 | Se tudo passa, eu desconfio do teste. | 2.59 | 2.812 |
| 8 | Se quebra, eu guardo a falha. | 2.198 | 2.024 |
| 9 | Eu confio no teste que recusa. | 1.89 | 2.011 |
| 10 | Se inscreve. | 0.976 | 0.897 |
| 11 | Próxima armadilha de IA. | 1.76 | 1.767 |

'Se tudo passa, eu desconfio do teste.' speaks in 2.81s, the board splits it at the comma and leaves a 0.64s hole (0.02s over the blank-gap limit). Offline probe (TTS only): **'Se tudo passa, eu desconfio.'** → 2.23s real, Gate B PASS A/B/C, only the 'caso' echo WARN left; 'Se passa, eu desconfio do teste.' → 2.54s, still FAIL. Text choice is CMO's.
