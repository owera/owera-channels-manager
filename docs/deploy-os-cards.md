# Deploy / rollback — OS VO-sentence cards (Gate B PR #122)

Kill switch is the module constant `app/services/craft.py` `OS_VO_SENTENCE_CARDS = True` (not env / Settings). Compose reads it at call time.

## Deploy

1. Confirm `/health` (unauthenticated): `status` is `ok` or `degraded`; `channels_connected` / `channels_total`; `channels_needing_attention`; `videos_failed` counts `status=failed` only.
2. Run the grandfather snapshot (read-only select of OS `approved`/`publishing`). **Expected grandfather = `[]`.** `#1328` is held and always excluded (`--exclude` defaults to `1328`; extra ids are comma-separated). OS videos still in `review` are not on the list — they take the new gate on approve.

```bash
PYTHONPATH=. .venv/bin/python scripts/gate_grandfather_snapshot.py --dry-run
PYTHONPATH=. .venv/bin/python scripts/gate_grandfather_snapshot.py --exclude 1328
# then, if the printed ids should be recorded:
PYTHONPATH=. .venv/bin/python scripts/gate_grandfather_snapshot.py
```

The script prints `grandfather ids (N): […]` and `count: N`. Paste that into the PR **Deploy effect** section.

3. Smoke one OS video **only** when `narration_words.json` exists for that handle. Missing words print `UNVERIFIED` and exit 2 (refuses synthesized timings).

```bash
PYTHONPATH=. .venv/bin/python scripts/smoke_os_cards.py 1452
```

4. Restart uvicorn after the pull so the new `OS_VO_SENTENCE_CARDS` / Gate B code is loaded.

## Grandfather rule (closed list)

Not “missing `gate_b_rules` = old gate”. Only IDs on the closed list may use the stored gate at publish.

1. At deploy, CTO runs `scripts/gate_grandfather_snapshot.py` (read-only select of OS `approved` / `publishing` — there is no separate `scheduled` status; `approved` is the publish queue). It writes `app/data/gate_grandfather.json` and prints `ids` + `count`. Paste that list into the PR **Deploy effect** section after deploy.
2. The script **drops** any row whose `vm_pass` does not match the **current render artifact**. `vm_pass` is not a git SHA: `review_guard.set_vm_pass` stores `video_path` + `artifact` = `size:mtime_ns` (`review_guard.artifact_fingerprint`). `vm_pass_of` returns None when the path or fingerprint does not match this file (a PASS for an older master does not count). `#1328` (approved, old master `adcdc31f…`, VM_FAIL) is always excluded even if pasted.
3. At publish, `craft.trust_stored_gate` → `gate_grandfather.is_grandfathered`: id on the list **and** `vm_pass_of` for the current file. Grandfather does **not** skip the #108 `vm_pass_required_reason` lock — `_publish_one` still parks `held: needs vm_pass` when the current master has no PASS.
4. Any OS video **not** on the list (with or without `gate_b_rules`) → new gate, fail-closed → `park_os_held`: `status=review`, `Video.held=True`, `error=held: …`. Never `status=failed`.
5. `held` never becomes `approved` or `published` automatically. It leaves only via explicit audited `POST /api/videos/{id}/approve` (`X-Actor`) after a re-render with the new generator + Gate B PASS, or `POST /api/videos/{id}/unhold`. Worker / render_loop / publish_loop never set `held=False` and skip-gate will not auto-approve a held row.
6. **RR — what applies.** Pre-deploy RR snapshots (no `gate_b_rules`) still trust the stored gate at **publish and approve**. New OS checks do **not** apply to RR: fail-closed missing words / unspoken card, no quote-hold exemption, `description_missing_source`, `os_card_hits` (hook skip), closed-list grandfather, `park_os_held`. The one new check that **does** apply to RR is `blank_gap` (OS+RR, already agreed), and only on boards that already run the live gate (new `gate_b_rules="122"` RR renders). Operator `held` on RR is unchanged (selection skips it; skip-gate / approve-fail / publish-fail do not set `Video.held`).

## Rollback

```bash
# 1. Flip the kill switch and restart
#    in app/services/craft.py: OS_VO_SENTENCE_CARDS = False
sudo systemctl restart ower-channels   # or the host's uvicorn unit

# 2. Or revert this stack to main (c35e110) and restart
git revert --no-edit <this-merge-sha>
```

`OS_VO_SENTENCE_CARDS = False` restores the previous OS template path. Already-rendered mp4s are unchanged. Grandfathered (or RR pre-deploy) approved rows keep publishing on the stored gate.

## Health after deploy

Deploy must not raise `/health` `videos_failed` or `channels_needing_attention`, and must not mark already-approved OS videos `failed`. Gate B FAIL writes `review` + `craft_review=fail` (or `409 held:`), never `failed`, never an OAuth attention flip.
