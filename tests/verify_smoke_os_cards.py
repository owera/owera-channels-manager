"""Smoke script: read-only OS cards + Gate B, no LLM / TTS / DB writes.

Run: PYTHONPATH=. .venv/bin/python tests/verify_smoke_os_cards.py
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from sqlmodel import Session, SQLModel, create_engine, select

import inspect

from app.main import _health_snapshot
from app.models import Channel, OAuthStatus, Topic, Video, VideoStatus
from app.services import craft
from app.services.engines import storyboard
from tests.os_gate_b_real_vo import whisper_words

_checks = 0
REPO = Path(__file__).resolve().parent.parent
SMOKE = REPO / "scripts" / "smoke_os_cards.py"
SNAP = REPO / "tests" / "fixtures" / "os-gate-b" / "snapshot-2026-10-19_07c2.json"


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def seed_db(path: Path, *, brand="os", script=None, video_id=1452) -> None:
    raw = json.loads(SNAP.read_text())
    engine = create_engine(f"sqlite:///{path}")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        if brand == "os":
            s.add(Channel(id=1, slug="owera", name="Owera Software",
                          oauth_status=OAuthStatus.CONNECTED))
        else:
            s.add(Channel(id=2, slug="rodrigo-recio", name="Rodrigo Recio",
                          oauth_status=OAuthStatus.CONNECTED))
        s.add(Topic(id=47, channel_id=1 if brand == "os" else 2, name="Agent traps",
                    content_format="short"))
        s.add(Video(
            id=video_id,
            channel_id=1 if brand == "os" else 2,
            topic_id=47,
            subject=raw["subject"],
            status=VideoStatus.REVIEW,
            script=script if script is not None else raw["script"],
            description=raw.get("description"),
            creation_config=raw["creation_config"],
            overrides_json=raw.get("overrides_json"),
            mpt_task_id=raw.get("mpt_task_id"),
        ))
        s.commit()


def run_smoke(db: Path, video_id: int, extra_env=None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(SMOKE), str(video_id), "--db", str(db)],
        cwd=str(REPO), env=env, capture_output=True, text=True,
    )


print("smoke script shape")
src = SMOKE.read_text()
ok(SMOKE.is_file(), "scripts/smoke_os_cards.py exists")
ok("mode=ro" in src and "OS_VO_SENTENCE_CARDS" in src, "opens sqlite mode=ro, requires flag")
ok("_boom" in src and "LLM called" in src, "LLM callable is a hard fail")
ok("narration_words.json" in src and "No TTS" in src, "words from store / creation_config, no TTS")
ok(craft.OS_VO_SENTENCE_CARDS is True, "flag default on")

print("deploy commands lock to code")
import app.main as main_mod
health_src = inspect.getsource(_health_snapshot)
ok('"status": "degraded" if' in health_src and 'else "ok"' in health_src,
   "GET /health status is ok|degraded")
ok("channels_connected" in health_src and "channels_total" in health_src,
   "2/2 connected = /health channels_connected / channels_total (no literal field)")
ok("channels_needing_attention" in health_src,
   "needing_attention=0 = /health channels_needing_attention (no needing_attention field)")
ok("needing_attention" not in health_src.replace("channels_needing_attention", ""),
   "no field named needing_attention")
ok('path == "/health"' in inspect.getsource(main_mod.basic_auth),
   "GET /health is unauthenticated")
ok("\nOS_VO_SENTENCE_CARDS = True\n" in Path(craft.__file__).read_text(),
   "kill switch is app/services/craft.py OS_VO_SENTENCE_CARDS = True (not env/Settings)")
ok("OS_VO_SENTENCE_CARDS" in inspect.getsource(storyboard._compose_board),
   "compose reads the module constant at call time")


print("UNVERIFIED without narration_words.json")
with tempfile.TemporaryDirectory() as tmp:
    db = Path(tmp) / "smoke.db"
    seed_db(db)
    proc = run_smoke(db, 1452)
    combo = (proc.stderr or "") + (proc.stdout or "")
    ok(proc.returncode == 2, f"missing words exits 2 (got {proc.returncode})\n{combo}")
    ok("UNVERIFIED" in combo, "prints UNVERIFIED (refuses synthesized timings)")

print("happy path on snapshot #1452 (temp sqlite + real words file)")
with tempfile.TemporaryDirectory() as tmp:
    db = Path(tmp) / "smoke.db"
    seed_db(db)
    hf = Path(tmp) / "hf"
    handle = json.loads(SNAP.read_text()).get("mpt_task_id") or "smoke-words"
    (hf / handle).mkdir(parents=True)
    (hf / handle / "narration_words.json").write_text(
        json.dumps(whisper_words(1452)))
    # rewrite mpt_task_id if the snapshot was empty
    before = digest(db)
    proc = run_smoke(db, 1452, extra_env={"MANAGER_HYPERFRAMES_STORAGE_DIR": str(hf)})
    after = digest(db)
    out = proc.stdout
    ok(proc.returncode == 0, f"exit 0 (got {proc.returncode})\n{proc.stderr}")
    ok("CARDS" in out and "GATE B" in out, "prints CARDS and GATE B")
    ok("One folder broke Copilot CLI" in out, "prints hook card from stored script")
    ok("result:" in out and "checks:" in out, "prints Gate B result/checks")
    ok('"video_id": 1452' in out, "prints JSON summary")
    ok(before == after, "sqlite bytes unchanged (no DB writes)")
    payload = json.loads(out.strip().splitlines()[-1])
    ok(payload["brand"] == "os" and payload["os_vo_sentence_cards"] is True,
       "summary brand=os, flag on")
    ok(payload["cards"] and payload["cards"][0]["type"] == "hook", "cards start with hook")
    ok(payload["cards"][-1]["type"] == "cta", "cards end with cta")
    banned = {c["type"] for c in payload["cards"]} & {"compare", "term_define", "list"}
    ok(not banned, f"no banned OS templates {banned}")
    ok(payload["gate"]["checks"].get("B") in ("PASS", "FAIL"), "Gate B check present")


print("errors")
with tempfile.TemporaryDirectory() as tmp:
    db = Path(tmp) / "smoke.db"
    seed_db(db)
    missing = run_smoke(db, 99999)
    ok(missing.returncode != 0, "unknown id exits non-zero")
    ok("not found" in (missing.stderr + missing.stdout).lower(), "unknown id names the miss")

    rr = Path(tmp) / "rr.db"
    seed_db(rr, brand="rr", video_id=2001)
    bad = run_smoke(rr, 2001)
    ok(bad.returncode != 0, "RR video exits non-zero")
    ok("brand=" in (bad.stderr + bad.stdout), "RR error cites brand")

    empty = Path(tmp) / "empty.db"
    seed_db(empty, script="", video_id=2002)
    noscript = run_smoke(empty, 2002)
    ok(noscript.returncode != 0, "empty script exits non-zero")


print(f"verify_smoke_os_cards: {_checks}/{_checks} passed")
