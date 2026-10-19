# Gate B — OS #1453 — **FAIL**
_Revisão read-only, 19/10/2026 (BRT). Nada foi aprovado, publicado ou renderizado, e nada foi gravado no manager/DB._

- Master: `os-1453/video.mp4` · sha256 `c0a5e4a917f411bae11028d9520ca3c309182965114735836acbf1bc9fc4106b` · mtime 19/10 09:41:51 BRT · 30.77 s
- Snapshot: `os-1453/snapshot-2026-10-19.json`
- Sheet: `os-1453/GATE_B_1453_sheet.png`
- Dados: `/workspace/gb19/` (cards, whisper small/medium)
- Método: frames a 30 fps; card visível quando n(px>100)>30, com máscara do logo; whisper small+medium com timestamps por palavra.

Título: "One type hint broke MCP tools. · Agent traps 14"

## Cards (visível)
| # | intervalo | texto |
|---|---|---|
| 0 hook | 0.00–2.33 | One type hint broke MCP tools. (+ chip "MCP") |
| 1 | 2.53–4.93 | Python three ten / runtime where the tool failed |
| 2 | 5.13–6.63 | NotRequired / optional key on the TypedDict |
| 3 | 6.97–9.23 | *registration* (isolado) |
| 4 | 9.47–11.83 | Pydantic / called the annotation invalid |
| 5 | 12.07–14.43 | Python runtime: three ten · tool failed VS three eleven · and up were fine |
| 6 | 14.67–17.03 | MCP Python SDK / update it and registration works |
| 7 | 17.37–18.53 | *Registration works, and optional keys are left out instead of sent as null.* |
| 8 | 18.73–20.47 | Optional keys / NotRequired fields on the return |
| 9 | 20.67–22.97 | Instead of: Left out · not as null VS Sent as null · null on the wire |
| 10 | 23.30–24.40 | *Exact version in the description.* |
| 11 | 24.60–25.93 | Old Python / test registration in prod |
| 12 | 26.23–27.90 | *prod* (isolado) |
| 13 CTA | 28.13–30.77 | Subscribe · Agent traps |

## Motivos de FAIL
1. **Card de palavra única.** O card #3, "registration" (6.97–9.23), é eco/fragmento. A batida da "clean registration" tinha que ser um card de texto, e não é.
2. **Card de palavra única.** O card #12, "prod" (26.23–27.90), é eco/fragmento.
3. **Texto inventado, que não vem da VO nem da fonte.** O card #9 (20.67–22.97) traz "null on the wire" e "not as null". Além disso, o compare inverte os lados: "Left out / not as null" contra "Sent as null".
4. **Citação fora de sincronia.** O card #7 (17.37–18.53, 1,17 s) mostra a frase inteira, mas a VO fala "Registration works, and optional keys…" de 18.42 a 22.5. Durante quase toda a fala, a tela mostra #8 e #9.
5. **Jumble/fragmento.** O card #5 junta "three ten · tool failed" com "three eleven · and up were fine" ("and up were fine" é fragmento).
6. Textos que não são literais da VO nem da fonte (WARN forte):
   - #1 "runtime where the tool failed"
   - #8 "NotRequired fields on the return"
   - #11 "test registration in prod" (a VO diz "Test registration there")

## OK
- Frame0 mostra o claim inteiro em t=0, luma 255.
- O claim termina em ~2.1 s ("tools." em 1.66–1.78; a próxima palavra só vem em 2.52). O corte é em 2.33, antes de 2,5 s.
- Cards do meio: visível ≤2.40 e PM ≤2.65.
- **Não há o card de citação não falada "3.11 and above are unaffected."** ✔
- A VO diz "Python three eleven and up were fine" (12.86–14.64) ✔.
- As versões são faladas como números ("three ten", "three eleven"), não como datas. A tela também usa "three ten" por extenso.
- Não há nenhuma linha de código na tela, então também não há linha fora da allow-list.
- Fatos conferidos:
  - Issue python-sdk#3227: "On Python 3.10 … TypedDict containing a NotRequired key cannot be registered … 3.11 and above are unaffected".
  - PR #3331: NotRequired era serializado como `null`.
  - Release v2.1.0: "NotRequired keys are omitted instead of serialized as null, and registration no longer fails on Python 3.10".
- A descrição tem os links da issue, do PR e da release.
- CTA ouvido: "Subscribe. Next Agent Trap." (small) ✔. O medium alucinou no final. Chip "Subscribe · Agent traps" ✔.

## WARNs
- O small ouviu "type dicked" (o medium ouviu "typed dict" ✔).
- O small ouviu "The MCP Python SDK" (o medium ouviu "Update" ✔).
- Na VO, "Old Python in prod?" saiu como "…Python and prod." nos dois modelos. Vale ouvir de novo.

## Correções
- Remover #3 e #12.
- Reescrever #9 só com palavras da VO.
- Segurar o #7 enquanto a frase é falada.
- Trocar os subtítulos inventados por texto da VO.
