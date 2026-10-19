# Gate B — OS #1452 — **FAIL**
_Revisão read-only, 19/10/2026 (BRT). Nada foi aprovado, publicado ou renderizado, e nada foi gravado no manager/DB._

- Master: `os-1452/video.mp4` · sha256 `427939c62b1638461a971aec93c0f64afea79df64bc137a8c36c1a88f8ce228a` · mtime 19/10 09:41:49 BRT · 30.60 s
- Snapshot: `os-1452/snapshot-2026-10-19.json`
- Sheet: `os-1452/GATE_B_1452_sheet.png`
- Dados: `/workspace/gb19/` (cards, whisper small/medium)
- Método: frames a 30 fps; card visível quando n(px>100)>30, com máscara do logo; whisper small+medium com timestamps por palavra.

Título: "One folder broke Copilot CLI. · Agent traps 12"

## Cards (visível)
| # | intervalo | texto |
|---|---|---|
| 0 hook | 0.00–2.33 | One folder broke Copilot CLI. (+ widget de terminal vazio "$") |
| 1 | 2.53–4.93 | add-dir / flag that adds a directory |
| 2 | 5.17–6.73 | Claude subagent · every one VS On the spot · failed |
| 3 | 7.07–7.83 | *A maximum of 4 blocks with cache_control may be provided.* |
| 4 | 8.07–9.37 | cache_control / maximum of 4 blocks |
| 5 | 9.70–11.73 | *cache_control* (isolado) |
| 6 | 11.93–14.33 | Over the limit: Found · 5 VS Limit · one block over |
| 7 | 14.60–16.43 | The parent agent got an empty answer. |
| 8 | 16.70–19.10 | Copilot CLI / update for Anthropic subagents |
| 9 | 19.30–21.70 | Directories · extra directories VS Subagents · keep working |
| 10 | 21.90–24.27 | version / exact version in the description |
| 11 | 24.60–25.53 | *Subagent came back empty?* |
| 12 | 25.77–28.13 | Subagent · its error VS Parent · not the parent's |
| 13 CTA | 28.37–30.60 | Subscribe · Agent traps |

## Motivos de FAIL
1. **Forma do card "Found 5." violada.** O card #6 (11.93–14.33) junta "Found / 5" num compare com "Limit / one block over" e o título "Over the limit". Não é o card literal do CAPIError, nem um card só com "5"/"Found 5.". A VO diz "Found five." em 11.94.
2. **Card de citação fora de sincronia, na prática uma citação não falada.** O card #3, com o texto literal do erro, fica na tela só de 7.07 a 7.83 (0,77 s, curto demais para ler). A VO fala essa frase de 8.04 a 11.1, quando a tela já mostra os cards #4 e #5.
3. **Eco de palavra única.** O card #5, "cache_control" isolado (9.70–11.73), é a terceira vez seguida do mesmo termo depois de #3 e #4.
4. **Eco.** O card #4, "cache_control / maximum of 4 blocks", repete o #3.
5. **Jumble/fragmento.** O card #2, "Claude subagent · every one VS On the spot · failed", não é um par de comparação.
6. **Eco/jumble.** O card #9, "Directories · extra directories VS Subagents · keep working", repete palavras e não compara nada.
7. **Eco.** O card #10, "version / exact version in the description", e o card #12, "Parent / not the parent's".

## OK
- Frame0 mostra o claim inteiro em t=0, luma 255.
- O claim termina em ~2.0 s (a palavra "CLI." começa em 1.62/1.78, e a próxima palavra só vem em 2.56). O corte é em 2.33–2.53, depois do claim e antes de 2,5 s.
- Cards do meio: visível ≤2.40 e PM ≤2.65. Nenhum passa de 2.80.
- **"underscore" não é lido** na janela 7.9–11.5: small e medium ouviram "cache control".
- `cache_control` aparece com underscore na tela.
- Não há nenhum comando `copilot … --add-dir` inventado. "add-dir" aparece só como termo.
- Nenhuma versão é falada.
- O medium ouviu "Claude" e "Anthropic" (o small ouviu "clod" uma vez, então WARN leve).
- Fatos conferidos:
  - Issue github/copilot-cli#4185 (fechada em 07/08 21:09 BRT) contém literalmente "CAPIError: 400 A maximum of 4 blocks with cache_control may be provided. Found 5." e fala da resposta vazia para o parent.
  - A release v1.0.73 tem a linha "Anthropic subagents continue working when additional directories are configured".
- A descrição tem os links públicos da issue e da release.
- CTA ouvido: "Subscribe — next agent trap." (small/medium: "Subscribe! Next Agent Trap") ✔. Chip "Subscribe · Agent traps" ✔.

## WARNs
- Widget de terminal vazio ("$") no hook.
- Card #11 com 0,93 s.
- O small ouviu "clod" no lugar de "Claude" uma vez.
- Episódio "Agent traps 12": duplicidade não checada (só nota).

## Correções
- Card #6: trocar por "Found 5." sozinho ou pelo card literal do CAPIError.
- Card #3: segurar a citação enquanto ela é falada (8.0–11.1).
- Remover #4 e #5.
- Reescrever #2, #9, #10 e #12 como frases da VO.
