# Autonomous Code Agent — Operator Runbook

A second autonomous agent that improves the **codebase and product** over time, mirroring the growth
agent's architecture. It delivers every change as a **tested, reviewed draft PR you merge** — nothing
auto-merges, nothing touches `main` until you say so.

Files: policy `run/code-agent-playbook.md` · work list `BACKLOG.md` · decision log
`run/code-experiments.jsonl` · headless runner `run/code-agent.sh` + `run/com.owera.code-agent.plist`.

---

## Mode 1 — Interactive `/loop` (start here)
Run it yourself in a Claude Code session on the Mac. One cycle:
```
/loop following run/code-agent-playbook.md, execute one code-improvement cycle from BACKLOG.md end-to-end (worktree → gate → draft PR → log), then stop
```
Let it keep going (self-paced) by dropping “then stop”. It re-fires on its own until you interrupt.

**Autonomy ladder** (widen as it earns trust):
- **L1 (now):** the command above — draft PRs only, you merge. This is the default and the only mode the
  playbook currently permits.
- **L2 (later):** after ~10 clean merges, add a bounded auto-merge envelope to the playbook (tests/docs/
  refactors that pass every gate; the money-path files stay draft-PR). Until you edit the playbook, the
  agent will not auto-merge.
- **L3 (later):** let it maintain `BACKLOG.md` itself (audit → re-rank → execute) so it never needs feeding.

## Mode 2 — Headless 24/7 (launchd, shipped OFF)
The runner and timer are installed as templates but **disabled** (`run/code-agent.disabled` is committed,
and the plist is not loaded). Turn it on only when you trust Mode 1:
```
rm run/code-agent.disabled
sed "s|/Users/you|$HOME|g" run/com.owera.code-agent.plist > ~/Library/LaunchAgents/com.owera.code-agent.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.owera.code-agent.plist
launchctl kickstart -k gui/$(id -u)/com.owera.code-agent   # run once now to test
```
It runs nightly at 02:00 (avoids the 09:00–10:00 growth window), opening up to 3 draft PRs per run.

**Kill switches:**
```
touch run/code-agent.disabled                          # hard off (next run no-ops)
launchctl bootout gui/$(id -u)/com.owera.code-agent     # remove the timer entirely
```
Logs: `tail -f ~/Library/Logs/owera-code-agent.log`.

---

## Your loop as the operator
1. Review the draft PRs it opens (`gh pr list --draft`). Check the gate evidence in the PR body, that it's
   one focused change, and that `/verify` actually drove the flow.
2. Merge the wins; close the rest with a one-line why (the agent reads closed PRs and learns).
3. Skim `run/code-experiments.jsonl` for what it tried and discarded.

## Guardrails it obeys (see `run/code-agent-playbook.md`)
- Draft PRs only — never `main`, never merge, never force-push. One change per cycle, isolated worktree.
- Gate before every PR: import check + `verify_storyboard` green + `/verify` the flow + `/code-review`
  clean + `npm run build` if frontend touched.
- Never restarts the manager, never edits the growth-agent files, never runs during 09:00–10:00.
- HARD-GATED (deploy, OAuth, account/secret changes, external posting, deletes) → it stops and hands you
  the step in the PR body.

## Health check
`git remote -v` → `origin git@github.com:owera/owera-channels-manager.git` (SSH — the runner needs an SSH
key loaded to push); PRs open via the authenticated `gh` CLI. If pushes fail headlessly, ensure the SSH
key is in the agent/keychain.
