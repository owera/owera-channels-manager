# Autonomous Code Agent — Operator Runbook

A second autonomous agent that improves the **codebase and product** over time, mirroring the growth
agent's architecture and trust model: it **commits gated changes straight to `main`**, reversible,
and makes them live (frontend rebuild / manager restart). No PR queue — the gate is the reviewer,
`git log` is your review surface, `git revert <sha>` is your undo.

> History: it started draft-PR-only (PR #5, 2026-07-05) and was switched to direct-to-main by the
> operator on 2026-07-06 after six clean gated changes (#5–#10).

Files: policy `run/code-agent-playbook.md` · work list `BACKLOG.md` · decision log
`run/code-experiments.jsonl` · headless runner `run/code-agent.sh` + `run/com.owera.code-agent.plist`.

---

## Mode 1 — Headless nightly (the default, ENABLED 2026-07-06)
launchd runs `com.owera.code-agent` at **02:00** (avoiding the 09:00–10:00 growth window): up to
**3 gated commits to `main`** per night from `BACKLOG.md`, each deployed and observed live, each
individually revertable.

**Kill switches:**
```
touch run/code-agent.disabled                          # hard off (next run no-ops)
launchctl bootout gui/$(id -u)/com.owera.code-agent     # remove the timer entirely
```
**Run one sprint now:** `launchctl kickstart -k gui/$(id -u)/com.owera.code-agent`
**Logs:** `tail -f ~/Library/Logs/owera-code-agent.log`

## Mode 2 — Interactive `/loop`
In a Claude Code session on the Mac:
```
/loop following run/code-agent-playbook.md, execute one code-improvement cycle from BACKLOG.md end-to-end (worktree → gate → ship to main → log), then stop
```
Drop “then stop” to let it keep going self-paced until you interrupt.

---

## Your loop as the operator (a few minutes over coffee)
1. `git log --oneline main` — each agent commit is one focused change with gate evidence in the body.
2. Don't like one? `git revert <sha> && git push` (the agent ships revert-friendly commits; it also
   self-reverts anything whose post-deploy check failed).
3. Skim `run/code-experiments.jsonl` for what it shipped, PR'd (rare fallback), discarded, or reverted.
4. Feed `BACKLOG.md` whenever you want something specific — it re-ranks by leverage each cycle.

## Guardrails it obeys (see `run/code-agent-playbook.md`)
- Ship only through the full gate: imports + every `tests/verify_*.py` green + `/verify` the real flow
  + `/code-review` clean + `npm run build` if frontend touched. Gate evidence lives in the commit body.
- One focused, revertable commit per cycle, built in an isolated worktree, fast-forwarded to `main`.
  Never force-push, never rewrite history. **Fallback:** anything the gate can't fully verify goes to a
  draft PR instead of `main`.
- Deploys its own change (frontend build / manager restart) and **observes the after-state** (`/health`
  200, behavior confirmed live); any post-deploy failure → self-revert + redeploy.
- Never edits growth-agent files, never runs 09:00–10:00, never touches `manager.db`/`credentials/`/`.env`.
- HARD-GATED (OAuth grants, GCP/account/secrets, external posting, data deletion) → ships the inert code
  part and hands you the step in the commit body.

## Health check
`git remote -v` → `origin git@github.com:owera/owera-channels-manager.git` (SSH — the runner needs an
SSH key loaded to push). If pushes fail headlessly, ensure the key is in the agent/keychain.
