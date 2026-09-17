"""Regression checks for POST /api/music/generate style filtering
and the count floor.

This project has no pytest; run directly:
    PYTHONPATH=. uv run python tests/verify_music.py

Backlog #31: GenerateBody.style is documented as "optional style
description to filter presets" but generate_music always
``random.choice(TECHNO_STYLES)`` and never reads ``body.style``. A
dashboard (or growth-agent) pick is silently discarded.

Backlog #45: ``count = min(max(1, body.count), 20)`` turned 0 /
negative into a silent one-track generate (same class as #41/#43).
JSON bool (lax ``int`` coerces ``false→0`` / ``true→1``) is the
same silent-1. These checks pin count ``<= 0`` / bool as 4xx
before any generate_and_save, and that omitted count still
defaults to 1. The documented 20-per-call cap stays a clamp.

Pins:
  - a known unique desc is the one generate_and_save is asked for, and
    the response style/bpm match that preset (count>1 so a lucky random
    hit cannot hide the ignore)
  - unknown style is 400 and writes nothing (even with count=5)
  - omitted / null / blank style still generates (random among all)
  - a partial desc needle that is NOT itself a preset ("dorian") 200s
    and only yields matching descs (kills exact-match-only)
  - case + surrounding whitespace still match
  - unauthenticated is still 401

Uses FastAPI's TestClient; ``music_gen.generate_and_save`` is stubbed
so the suite never synthesises audio or touches the live bgm_dir.
Exits non-zero on the first failed assertion.
"""
from __future__ import annotations

import atexit
import inspect
import shutil
import sys
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

import app.main as main
from app.config import settings
from app.routers import music as music_router
from app.services import music_gen

_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


# Isolated-copy batteries pin this so a stale pyc / wrong PYTHONPATH cannot
# silently test a different checkout (08-01 lesson).
ok(Path(music_router.__file__).resolve().parents[2] == Path(__file__).resolve().parents[1],
   "music module loaded from this tree")

KNOWN_DESC = "EBM Bb minor 136"
KNOWN = next((s for s in music_gen.TECHNO_STYLES if s["desc"] == KNOWN_DESC), None)
ok(KNOWN is not None, "fixture unique desc still exists in TECHNO_STYLES")
ok(sum(1 for s in music_gen.TECHNO_STYLES if s["desc"] == KNOWN_DESC) == 1,
   "fixture desc is unique (a duplicate would make the identity pin vacuous)")

DORIAN = [s for s in music_gen.TECHNO_STYLES if "dorian" in s["desc"].lower()]
NON_DORIAN = [s for s in music_gen.TECHNO_STYLES if "dorian" not in s["desc"].lower()]
ok(len(DORIAN) >= 2, "dorian is a multi-match filter needle (not a full desc)")
ok(not any(s["desc"].lower() == "dorian" for s in music_gen.TECHNO_STYLES),
   "'dorian' itself is not a preset desc (exact-match-only would 400)")
ok(NON_DORIAN, "non-dorian presets exist so an unfiltered draw can be caught")

_TMP = Path(tempfile.mkdtemp(prefix="verify-music-"))
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)

_orig_pw = settings.app_password
_orig_bgm = settings.bgm_dir
settings.app_password = "testpw"
settings.bgm_dir = str(_TMP)

client = TestClient(main.app)
auth = ("x", "testpw")

_orig_save = music_gen.generate_and_save
# The router imports generate_and_save via `from app.services import music_gen`
# then `music_gen.generate_and_save(...)` — patching the module attribute is
# the live seam. Also patch the router-bound name if it ever aliases.
_orig_router_save = getattr(music_router, "generate_and_save", None)

calls: list[dict] = []


def fake_save(prompt, bgm_dir, duration_s=30):
    calls.append({
        "prompt": prompt,
        "bgm_dir": Path(bgm_dir),
        "duration_s": duration_s,
    })
    p = Path(bgm_dir) / f"techno_fake_{len(calls)}.wav"
    p.write_bytes(b"RIFFFAKE")
    return p


music_gen.generate_and_save = fake_save
if _orig_router_save is not None:
    music_router.generate_and_save = fake_save


def post_generate(**body):
    return client.post("/api/music/generate", auth=auth, json=body)


try:
    print("POST /api/music/generate: known unique desc is the one generated")
    r = post_generate(count=8, style=KNOWN_DESC)
    ok(r.status_code == 200, "known style generate returns 200")
    body = r.json()
    ok(body.get("generated") == 8, "known style generates the requested count")
    ok(body.get("errors") == [], "known style writes no errors")
    ok(len(calls) == 8, "generate_and_save called once per requested track")
    ok(all(c["prompt"] == KNOWN_DESC for c in calls),
       "every generate_and_save prompt is the requested unique desc "
       "(pre-fix random.choice would almost surely drift across 8 draws)")
    ok(all(f.get("style") == KNOWN_DESC for f in body.get("files") or []),
       "response style is the requested desc, not a different random pick")
    ok(all(f.get("bpm") == KNOWN["bpm"] for f in body.get("files") or []),
       "response bpm is the matching preset's bpm")
    ok(all(c["bgm_dir"] == _TMP for c in calls),
       "generate_and_save received the settings bgm_dir")

    print("POST /api/music/generate: case + whitespace still match")
    n_before = len(calls)
    r = post_generate(count=1, style=f"  {KNOWN_DESC.upper()}  ")
    ok(r.status_code == 200, "case/whitespace-padded unique desc is 200")
    ok(len(calls) == n_before + 1, "padded desc still called generate_and_save")
    ok(calls[-1]["prompt"] == KNOWN_DESC,
       "padded/upper desc resolves to the canonical preset desc")

    print("POST /api/music/generate: unknown style is 400 and writes nothing")
    n_before = len(calls)
    files_before = list(_TMP.glob("techno_fake_*.wav"))
    r = post_generate(count=5, style="jazz fusion xyz 999")
    ok(r.status_code == 400, "unknown style is 400, not a silent random generate")
    detail = str(r.json().get("detail") or "")
    ok("style" in detail.lower() or "unknown" in detail.lower(),
       "400 detail names the style miss (not a generic validation error)")
    ok(len(calls) == n_before,
       "unknown style never called generate_and_save (even with count=5)")
    ok(list(_TMP.glob("techno_fake_*.wav")) == files_before,
       "unknown style writes no wav files")

    print("POST /api/music/generate: partial desc needle filters, does not 400")
    n_before = len(calls)
    r = post_generate(count=12, style="dorian")
    ok(r.status_code == 200,
       "partial needle 'dorian' is 200 (exact-match-only would 400)")
    body = r.json()
    ok(body.get("generated") == 12, "partial needle generates the requested count")
    new_calls = calls[n_before:]
    ok(len(new_calls) == 12, "partial needle called generate_and_save 12 times")
    dorian_descs = {s["desc"] for s in DORIAN}
    ok(all(c["prompt"] in dorian_descs for c in new_calls),
       "every partial-needle prompt is a dorian preset desc")
    ok(all("dorian" in (f.get("style") or "").lower()
           for f in body.get("files") or []),
       "every response style contains the dorian needle")
    leaked = [c["prompt"] for c in new_calls if c["prompt"] not in dorian_descs]
    ok(not leaked, "partial needle never drew a non-dorian preset")

    print("POST /api/music/generate: omitted / null / blank still random-generate")
    n_before = len(calls)
    r = post_generate(count=1)
    ok(r.status_code == 200, "omitted style still generates")
    ok(len(calls) == n_before + 1, "omitted style called generate_and_save")
    ok(calls[-1]["prompt"] in {s["desc"] for s in music_gen.TECHNO_STYLES},
       "omitted style still picks a real preset desc")

    n_before = len(calls)
    r = post_generate(count=1, style=None)
    ok(r.status_code == 200, "style=null still generates")
    ok(len(calls) == n_before + 1, "null style called generate_and_save")

    n_before = len(calls)
    r = post_generate(count=1, style="   ")
    ok(r.status_code == 200, "whitespace-only style is treated as unset, not 400")
    ok(len(calls) == n_before + 1, "blank style called generate_and_save")

    n_before = len(calls)
    r = post_generate(count=1, style="")
    ok(r.status_code == 200, "empty-string style is treated as unset, not 400")
    ok(len(calls) == n_before + 1, "empty-string style called generate_and_save")

    print("POST /api/music/generate: auth")
    n_before = len(calls)
    r = client.post("/api/music/generate", json={"count": 1, "style": KNOWN_DESC})
    ok(r.status_code == 401, "generate still requires auth")
    ok(len(calls) == n_before, "unauthenticated generate never called generate_and_save")

    print("POST /api/music/generate: count<=0 is 400, not a silent one-track")
    # Defect: count = min(max(1, body.count), 20) turned 0 / negative
    # into a one-track generate. Generate topics already 400s count<=0
    # (#41); music is the same growth-agent path.
    n_before = len(calls)
    files_before = list(_TMP.glob("techno_fake_*.wav"))
    r = post_generate(count=0, style=KNOWN_DESC)
    ok(r.status_code == 400, "count=0 is 400 (max(1, 0) would 200 and write 1)")
    detail = str(r.json().get("detail") or r.text)
    ok("count" in detail.lower() and ">= 1" in detail,
       "count=0 400 names the field floor, not a style miss")
    ok("unknown" not in detail.lower(),
       "count=0 400 is the count floor, not a style 400")
    ok(len(calls) == n_before, "count=0 never called generate_and_save")
    ok(list(_TMP.glob("techno_fake_*.wav")) == files_before,
       "count=0 writes no wav files")

    r = post_generate(count=-1, style=KNOWN_DESC)
    ok(r.status_code == 400, "count=-1 is 400")
    ok(len(calls) == n_before, "count=-1 never called generate_and_save")
    ok(list(_TMP.glob("techno_fake_*.wav")) == files_before,
       "negative count writes no wav files")

    r = client.post("/api/music/generate", auth=auth,
                    json={"count": False, "style": KNOWN_DESC})
    ok(r.status_code in (400, 422),
       "count=false is 4xx (must not coerce to 0 then max(1,0)=1)")
    ok(len(calls) == n_before, "count=false never called generate_and_save")

    r = client.post("/api/music/generate", auth=auth,
                    json={"count": True, "style": KNOWN_DESC})
    ok(r.status_code in (400, 422),
       "count=true is 4xx (must not coerce to 1 and generate)")
    ok(len(calls) == n_before, "count=true never called generate_and_save")

    r = client.post("/api/music/generate", auth=auth,
                    json={"count": None, "style": KNOWN_DESC})
    ok(r.status_code in (400, 422), "count=null is 4xx")
    ok(len(calls) == n_before, "count=null never called generate_and_save")

    r = post_generate(count=0, style="jazz fusion xyz 999")
    ok(r.status_code == 400, "count=0 + unknown style is still 400")
    ok("count" in str(r.json().get("detail") or r.text).lower(),
       "count floor runs before the style pool "
       "(style-first would 400 unknown-style and hide a missing floor)")
    ok(len(calls) == n_before,
       "count=0 + unknown style never called generate_and_save")

    r = post_generate(count=1, style=KNOWN_DESC)
    ok(r.status_code == 200, "count=1 still 200")
    ok(r.json().get("generated") == 1, "count=1 generated exactly one track")
    ok(len(calls) == n_before + 1, "count=1 called generate_and_save once")

    n_before = len(calls)
    r = client.post("/api/music/generate", auth=auth, json={"style": KNOWN_DESC})
    ok(r.status_code == 200, "omitted count still 200 (GenerateBody default 1)")
    ok(r.json().get("generated") == 1,
       "omitted count defaults to 1, not 400")
    ok(len(calls) == n_before + 1, "omitted count called generate_and_save once")
    ok(music_router.GenerateBody.model_fields["count"].default == 1,
       "GenerateBody.count default is 1")

    n_before = len(calls)
    r = post_generate(count=21, style=KNOWN_DESC)
    ok(r.status_code == 200, "count=21 still 200 (cap is a clamp, not a 400)")
    ok(r.json().get("generated") == 20,
       "count=21 clamps to the documented 20-per-call cap")
    ok(len(calls) == n_before + 20, "count=21 called generate_and_save 20 times")

    gen_src = inspect.getsource(music_router.generate_music)
    ok("_require_int" in gen_src,
       "generate_music floors count through _require_int")
    ok("max(1, body.count)" not in gen_src,
       "count is no longer max(1, ...) (that is the silent-1 defect)")
    ok(gen_src.index("_require_int") < gen_src.index("_style_pool"),
       "_require_int runs before _style_pool "
       "(style-first would 400 unknown-style on count=0)")
    ok(gen_src.index("_require_int") < gen_src.index("generate_and_save"),
       "_require_int runs before generate_and_save")
    ok("min(body.count, 20)" in gen_src or "min(count, 20)" in gen_src,
       "the 20-per-call cap is still a clamp (not dropped, not a 400)")
    body_src = inspect.getsource(music_router.GenerateBody)
    ok('_reject_bool_count' in body_src and 'mode="before"' in body_src,
       "GenerateBody._reject_bool_count is mode=before "
       "(a comment containing 'before' is not the decorator)")
finally:
    music_gen.generate_and_save = _orig_save
    if _orig_router_save is not None:
        music_router.generate_and_save = _orig_router_save
    settings.app_password = _orig_pw
    settings.bgm_dir = _orig_bgm

print(f"ALL {_checks} CHECKS PASSED")
