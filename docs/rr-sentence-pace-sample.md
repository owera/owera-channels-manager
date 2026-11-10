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
