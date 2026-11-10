# RR sentence pace — sample script + Gate B evidence (PO CM cut, 2026-11-10)

Stub LLM output (tests/fixtures/rr_pace/stub_llm_1464_style.txt, #1464 topic) → `worker._generate_script` (brand rr, pace 2.2s/2.5s) → real edge-tts 7.2.8 pt-BR-AntonioNeural (-8%, -2Hz, hook join as in the worker) → `storyboard.compose` brand rr → #201 Gate B (`video_maker_gate` + `card_timing_hits` + `blank_gap_hits`). Reproduced offline by `tests/verify_sentence_pace.py` from `tests/fixtures/rr_pace/sample_vo.json`. No paid LLM calls, no renders.

## Sample script (for the CMO voice pass)

```
Pedir resumo não é engenharia.
O modelo escolhe sozinho o que importa.
O detalhe certo fica de fora.
A multa da cláusula 14 some.
Ninguém percebe.
Eu pergunto por campo.
O modelo devolve cada valor.
Cada valor volta com a página.
Eu abro a página.
Confiro o número na mão.
Campo sem página é chute.
Serve pra contrato, laudo e nota.
Se inscreve.
Próxima armadilha de IA.
```

## Post-pass changes

| # | kind | before (est s) | after (est s) |
|---|---|---|---|
| 1 | split | O modelo escolhe sozinho o que importa, e o detalhe certo fica de fora. (4.68) | O modelo escolhe sozinho o que importa. / O detalhe certo fica de fora. (2.5, 1.999) |
| 2 | split | A multa da cláusula 14 some, e ninguém percebe. (3.585) | A multa da cláusula 14 some. / Ninguém percebe. (2.319, 1.085) |
| 3 | split | Eu pergunto por campo; o modelo devolve cada valor. (3.374) | Eu pergunto por campo. / O modelo devolve cada valor. (1.433, 1.934) |
| 5 | split | Eu abro a página e confiro o número na mão. (2.913) | Eu abro a página. / Confiro o número na mão. (1.433, 1.716) |

Dropped words: ['e', 'e', 'e'] (only `FILLER_WORDS` + the split conjunction 'e'). Unsplittable: none.

## Duration before / after

| | sentences | est total | real audio | video | longest sentence (real) |
|---|---|---|---|---|---|
| before (stub LLM) | 10 | 33.81s | 33.91s | 33.46s | 4.44s |
| after (post-pass) | 14 | 37.44s | 37.13s | 36.68s | 2.50s |

4 splits → +3.22s real audio (≈0.80s per split: the PT full-stop pause ≈0.94s minus the dropped 'e' / comma pause).

## Gate B

Result: **{g['result']}** {g['checks']}; reasons: {g['reasons'] or 'none'}; card_timing hits: {g['card_timing_hits'] or 'none'}; blank_gap hits: {g['blank_gap_hits'] or 'none'}; card_sync hits: {g['card_sync_hits'] or 'none'}.

| sentence | est s | real s |
|---|---|---|
| Pedir resumo não é engenharia. | 1.934 | 1.81 |
| O modelo escolhe sozinho o que importa. | 2.5 | 2.28 |
| O detalhe certo fica de fora. | 1.999 | 1.97 |
| A multa da cláusula 14 some. | 2.319 | 2.38 |
| Ninguém percebe. | 1.085 | 1.24 |
| Eu pergunto por campo. | 1.433 | 1.44 |
| O modelo devolve cada valor. | 1.934 | 1.96 |
| Cada valor volta com a página. | 1.999 | 1.93 |
| Eu abro a página. | 1.433 | 1.18 |
| Confiro o número na mão. | 1.716 | 1.50 |
| Campo sem página é chute. | 1.716 | 1.71 |
| Serve pra contrato, laudo e nota. | 2.416 | 2.50 |
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
| quote | 16.18 | 2.32 | O modelo devolve cada valor. |
| quote | 19.08 | 2.3 | Cada valor volta com a página. |
| quote | 21.94 | 1.55 | Eu abro a página. |
| quote | 24.06 | 1.86 | Confiro o número na mão. |
| quote | 26.5 | 2.08 | Campo sem página é chute. |
| quote | 29.14 | 2.8 | Serve pra contrato, laudo e nota. |
| cta | 32.51 | 3.78 | Se inscreve · IA |
