# Deploy / rollback — OS VO-sentence cards (Gate B PR #122)

Kill switch is the module constant `app/services/craft.py` `OS_VO_SENTENCE_CARDS = True` (not env / Settings). Compose reads it at call time.

## Deploy

1. Confirm `/health` (unauthenticated): `status` is `ok` or `degraded`; `channels_connected` / `channels_total`; `channels_needing_attention`; `videos_failed` counts `status=failed` only.
2. Run the grandfather snapshot (read-only select of OS `approved`/`publishing`). Expected list: **empty**. `#1328` is always excluded.

```bash
PYTHONPATH=. .venv/bin/python scripts/gate_grandfather_snapshot.py --dry-run
# then, if the printed ids should be recorded:
PYTHONPATH=. .venv/bin/python scripts/gate_grandfather_snapshot.py
```

Paste the printed `ids` + `count` into the PR **Deploy effect** section.

3. Smoke one OS video **only** when `narration_words.json` exists for that handle. Missing words print `UNVERIFIED` and exit 2 (refuses synthesized timings).

```bash
PYTHONPATH=. .venv/bin/python scripts/smoke_os_cards.py 1452
```

4. Restart uvicorn after the pull so the new `OS_VO_SENTENCE_CARDS` / Gate B code is loaded.

## Grandfather rule (closed list)

- New boards carry `creation_config.gate_b_rules = "122"`. Full fail-closed Gate B applies to those.
- OS publish of an already-approved row uses the **old** stored gate **only** if its id is on `app/data/gate_grandfather.json` **and** `vm_pass` matches the current master (`review_guard.vm_pass_of`). Missing marker is not enough.
- `#1328` is never on the list (held; re-render with the new generator).
- OS videos still in review (no marker / not on the list): new gate on approve, fail-closed → `409 held: …`, `status=review`, `craft_review=fail`. Never `status=failed`.
- `held` leaves only via re-render with the new generator + Gate B PASS, or an explicit audited approve (`X-Actor` on `POST /api/videos/{id}/approve`). No worker / render loop / publish loop auto-promotes held.
- RR: pre-deploy snapshots (no `gate_b_rules`) still trust the stored gate at publish and approve. New OS-only checks (description source, fail-closed missing words / unspoken card, no quote hold exemption, hook skip, grandfather/held) do not apply to RR. `blank_gap` stays OS+RR on boards that already run the live gate.

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
