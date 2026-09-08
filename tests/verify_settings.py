"""Regression checks for PATCH /api/settings integer floors.

This project has no pytest; run directly:
    PYTHONPATH=. uv run python tests/verify_settings.py

``PATCH /api/settings`` wrote ``render_concurrency`` / ``publish_drip_minutes``
(and the autogen ints) with no range check, and ``exclude_unset`` still
forwards JSON ``null``. Two live failure modes:

  * concurrency=0 (or negative): ``render_loop._submit_new`` gates on
    ``in_flight >= cfg.render_concurrency``. in_flight is always >= 0, so
    ``>= 0`` is always true and every render stalls. The Settings number
    input's empty-blur is ``Number("") === 0``; the growth agent PATCHes
    the same body with no HTML min/max.
  * concurrency=null: ``setattr(cfg, "render_concurrency", None)``. The
    next ``in_flight >= cfg.render_concurrency`` is a TypeError that
    kills the render tick (``_safe`` logs it; nothing renders that cycle).
    The UI's non-numeric blur is ``Number("x") === NaN`` → JSON ``null``.

Pins:
  * GET returns the row + mpt_base_url + youtube_quota_reset_at
  * a valid PATCH persists and leaves omitted fields alone
  * 0 / negative / null concurrency is 400 and writes nothing
  * negative / null drip is 400; 0 drip (no spacing) is allowed
  * null / negative autogen ints are 400; 0 is allowed (autofill already floors)
  * unauthenticated is still 401
  * Settings.tsx empty/invalid blur does not ``Number()`` the raw input
    (``Number("") === 0`` is now a 400); it restores the current value
    and skips the PATCH. ``intFromBlur`` is the choke point.
  * Channels.tsx budget blur is the same class, but 0 IS legal there
    (``daily_publish_budget=0`` stalls the channel on purpose — #27/#28).
    Empty-blur ``Number("") === 0`` would silently halt publish/render;
    ``Number("x") === NaN`` → JSON ``null`` setattr TypeErrors the tick.
    Restore + skip PATCH; typed 0 still PATCHes (min=0).

Uses an in-memory SQLite DB and FastAPI's TestClient (no real manager.db,
no network, lifespan/scheduler never started). The blur helper is driven
with one node process against a type-stripped temp copy (null/NaN/inf
are distinct; no --experimental-strip-types). Exits non-zero on the
first failed assertion.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

import app.main as main
from app.config import settings
from app.db import get_session
from app.models import Settings
from app.routers import settings as settings_router
from app.services import render_loop

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
ok(Path(settings_router.__file__).resolve().parents[2] == Path(__file__).resolve().parents[1],
   "settings module loaded from this tree")

# Why 0 is fatal — a gate change to `>` would make 0 start a render, and the
# 400 would then be a policy choice rather than a stall-preventer. Pin the
# live comparison so this suite's "must be >= 1" stays coupled to the loop.
_src = Path(render_loop.__file__).read_text()
ok("in_flight >= cfg.render_concurrency" in _src,
   "render_loop still gates on in_flight >= concurrency (0 would stall)")
ok("in_flight > cfg.render_concurrency" not in _src,
   "render_loop does not use a > comparison (that would make 0 safe)")


engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                       poolclass=StaticPool)
SQLModel.metadata.create_all(engine)

with Session(engine) as s:
    s.add(Settings(id=1, render_concurrency=1, publish_drip_minutes=30,
                   scheduler_paused=False, topic_autogen_enabled=False,
                   topic_autogen_min_pending=3, topic_autogen_target=6))
    s.commit()


def _override_session():
    with Session(engine) as s:
        yield s


main.app.dependency_overrides[get_session] = _override_session
_orig_pw = settings.app_password
settings.app_password = "testpw"
client = TestClient(main.app)
auth = ("x", "testpw")


def row() -> Settings:
    with Session(engine) as s:
        cfg = s.get(Settings, 1)
        # Detach so later asserts don't depend on the session.
        s.expunge(cfg)
        return cfg


def snapshot():
    cfg = row()
    return {
        "render_concurrency": cfg.render_concurrency,
        "publish_drip_minutes": cfg.publish_drip_minutes,
        "scheduler_paused": cfg.scheduler_paused,
        "topic_autogen_enabled": cfg.topic_autogen_enabled,
        "topic_autogen_min_pending": cfg.topic_autogen_min_pending,
        "topic_autogen_target": cfg.topic_autogen_target,
    }


def patch(**body):
    return client.patch("/api/settings", auth=auth, json=body)


try:
    print("GET /api/settings")
    r = client.get("/api/settings", auth=auth)
    ok(r.status_code == 200, "GET /api/settings is 200")
    body = r.json()
    ok(body.get("render_concurrency") == 1, "GET surfaces seeded concurrency")
    ok(body.get("publish_drip_minutes") == 30, "GET surfaces seeded drip")
    ok(body.get("scheduler_paused") is False, "GET surfaces seeded paused")
    ok(body.get("topic_autogen_enabled") is False, "GET surfaces seeded autogen flag")
    ok(body.get("topic_autogen_min_pending") == 3, "GET surfaces seeded autogen min")
    ok(body.get("topic_autogen_target") == 6, "GET surfaces seeded autogen target")
    ok(isinstance(body.get("mpt_base_url"), str) and body["mpt_base_url"],
       "GET adds mpt_base_url (not a Settings column)")
    reset_at = body.get("youtube_quota_reset_at")
    ok(isinstance(reset_at, str) and reset_at, "GET adds youtube_quota_reset_at")
    datetime.fromisoformat(reset_at)
    ok(True, "youtube_quota_reset_at is ISO-8601 parseable")

    print("PATCH /api/settings: valid persist + omitted fields stay put")
    before = snapshot()
    r = patch(render_concurrency=2, scheduler_paused=True)
    ok(r.status_code == 200, "valid PATCH is 200")
    got = r.json()
    ok(got.get("render_concurrency") == 2, "response concurrency is the new value")
    ok(got.get("scheduler_paused") is True, "response paused is the new value")
    ok(got.get("publish_drip_minutes") == 30,
       "omitted drip is not reset (exclude_unset)")
    ok(got.get("topic_autogen_min_pending") == 3,
       "omitted autogen min is not reset")
    after = snapshot()
    ok(after["render_concurrency"] == 2, "concurrency persisted")
    ok(after["scheduler_paused"] is True, "paused persisted")
    ok(after["publish_drip_minutes"] == before["publish_drip_minutes"],
       "omitted drip column unchanged in DB")
    ok(after["topic_autogen_target"] == before["topic_autogen_target"],
       "omitted autogen target unchanged in DB")

    # Restore paused so later "unchanged" snapshots are easier to read, keep
    # concurrency=2 as the known-good value a 400 must not clobber.
    r = patch(scheduler_paused=False)
    ok(r.status_code == 200, "PATCH paused back to false is 200")
    ok(snapshot()["render_concurrency"] == 2, "paused-only PATCH left concurrency")

    print("PATCH /api/settings: 0 / negative / null concurrency is 400, writes nothing")
    before = snapshot()
    r = patch(render_concurrency=0)
    ok(r.status_code == 400, "concurrency=0 is 400 (empty-blur / Number(''))")
    ok(">= 1" in r.text, "0-concurrency 400 names the floor")
    ok(snapshot() == before, "concurrency=0 writes nothing")

    r = patch(render_concurrency=-1)
    ok(r.status_code == 400, "concurrency=-1 is 400")
    ok(">= 1" in r.text, "negative-concurrency 400 names the floor")
    ok(snapshot() == before, "concurrency=-1 writes nothing")

    r = patch(render_concurrency=None)
    ok(r.status_code == 400, "concurrency=null is 400 (NaN blur → JSON null)")
    ok(">= 1" in r.text or "null" in r.text.lower(),
       "null-concurrency 400 names the rejection")
    ok(snapshot() == before, "concurrency=null writes nothing")
    ok(row().render_concurrency is not None,
       "null PATCH did not persist SQL NULL into render_concurrency")

    print("PATCH /api/settings: drip floor (0 allowed, negative/null 400)")
    r = patch(publish_drip_minutes=0)
    ok(r.status_code == 200, "drip=0 is allowed (no spacing)")
    ok(snapshot()["publish_drip_minutes"] == 0, "drip=0 persisted")
    ok(snapshot()["render_concurrency"] == 2,
       "drip=0 PATCH left concurrency alone")

    before = snapshot()
    r = patch(publish_drip_minutes=-5)
    ok(r.status_code == 400, "drip=-5 is 400")
    ok(">= 0" in r.text, "negative-drip 400 names the floor")
    ok(snapshot() == before, "drip=-5 writes nothing")

    r = patch(publish_drip_minutes=None)
    ok(r.status_code == 400, "drip=null is 400")
    ok(snapshot() == before, "drip=null writes nothing")
    ok(row().publish_drip_minutes is not None,
       "null drip PATCH did not persist SQL NULL")

    r = patch(publish_drip_minutes=15)
    ok(r.status_code == 200, "positive drip PATCH is 200")
    ok(snapshot()["publish_drip_minutes"] == 15, "drip=15 persisted")

    print("PATCH /api/settings: autogen ints (0 allowed, negative/null 400)")
    r = patch(topic_autogen_min_pending=0, topic_autogen_target=0)
    ok(r.status_code == 200, "autogen 0/0 is allowed (autofill already floors)")
    ok(snapshot()["topic_autogen_min_pending"] == 0, "autogen min=0 persisted")
    ok(snapshot()["topic_autogen_target"] == 0, "autogen target=0 persisted")

    before = snapshot()
    r = patch(topic_autogen_min_pending=-1)
    ok(r.status_code == 400, "autogen min=-1 is 400")
    ok(snapshot() == before, "autogen min=-1 writes nothing")

    r = patch(topic_autogen_target=-2)
    ok(r.status_code == 400, "autogen target=-2 is 400")
    ok(snapshot() == before, "autogen target=-2 writes nothing")

    r = patch(topic_autogen_min_pending=None)
    ok(r.status_code == 400, "autogen min=null is 400")
    ok(snapshot() == before, "autogen min=null writes nothing")

    r = patch(topic_autogen_target=None)
    ok(r.status_code == 400, "autogen target=null is 400")
    ok(snapshot() == before, "autogen target=null writes nothing")

    r = patch(topic_autogen_min_pending=4, topic_autogen_target=8)
    ok(r.status_code == 200, "positive autogen PATCH is 200")
    ok(snapshot()["topic_autogen_min_pending"] == 4, "autogen min=4 persisted")
    ok(snapshot()["topic_autogen_target"] == 8, "autogen target=8 persisted")

    print("PATCH /api/settings: mixed body cannot smuggle a stall past a 400")
    before = snapshot()
    r = patch(render_concurrency=0, scheduler_paused=True, publish_drip_minutes=45)
    ok(r.status_code == 400, "mixed body with concurrency=0 is 400")
    ok(snapshot() == before,
       "a 400 mixed body writes none of the fields (paused/drip stay put)")

    print("PATCH /api/settings: empty body + auth")
    before = snapshot()
    r = patch()
    ok(r.status_code == 200, "empty PATCH is 200 (no-op)")
    ok(snapshot() == before, "empty PATCH writes nothing")

    r = client.patch("/api/settings", json={"render_concurrency": 3})
    ok(r.status_code == 401, "PATCH still requires auth")
    ok(snapshot() == before, "unauthenticated PATCH writes nothing")

    r = client.get("/api/settings")
    ok(r.status_code == 401, "GET still requires auth")
finally:
    main.app.dependency_overrides.clear()
    settings.app_password = _orig_pw


# --- Settings.tsx empty-blur (BACKLOG #33 follow-up / #34) -----------------
# After the API floor, empty-blur ``Number("") === 0`` became a 400 instead
# of a stall. The SPA must not send 0/NaN; it restores and skips the PATCH.
print("Settings.tsx empty-blur skips PATCH instead of Number('')===0")
_root = Path(__file__).resolve().parents[1]
settings_tsx = (_root / "frontend/src/pages/Settings.tsx").read_text()
ok("Number(e.target.value)" not in settings_tsx,
   "Settings.tsx does not Number() blur values (Number('')===0 is now a 400)")
ok('from "../intFromBlur"' in settings_tsx,
   "Settings.tsx imports intFromBlur (not an inlined copy)")
ok("intFromBlur(e.target.value, min)" in settings_tsx,
   "commitInt feeds the raw input + floor into intFromBlur")
ok(re.search(r'commitInt\(\s*"render_concurrency"\s*,\s*1\s*\)', settings_tsx),
   "render_concurrency blur uses min=1")
ok(re.search(r'commitInt\(\s*"publish_drip_minutes"\s*,\s*0\s*\)', settings_tsx),
   "publish_drip_minutes blur uses min=0 (0 drip is legal)")
ok(re.search(r'commitInt\(\s*"topic_autogen_min_pending"\s*,\s*0\s*\)', settings_tsx),
   "autogen min-pending blur uses min=0")
ok(re.search(r'commitInt\(\s*"topic_autogen_target"\s*,\s*0\s*\)', settings_tsx),
   "autogen target blur uses min=0")
ok("if (n === null) { e.target.value = String(s[key]); return; }" in settings_tsx,
   "null restores the current value AND returns (does not PATCH)")
ok("patch({ [key]: n })" in settings_tsx,
   "valid int still PATCHes the same key (not a no-op / not hardcoded concurrency)")
ok("valueAsNumber" not in settings_tsx,
   "Settings.tsx does not use valueAsNumber (empty is 0, same class as Number(''))")
ok("+e.target.value" not in settings_tsx,
   "Settings.tsx does not coerce with unary-plus")

helper_path = _root / "frontend/src/intFromBlur.ts"
ok(helper_path.is_file(), "intFromBlur.ts exists (single choke point)")
helper_src = helper_path.read_text()
ok("Number.isInteger" in helper_src,
   "intFromBlur rejects non-integers (1.5 is not truncated to 1)")
ok("n < min" in helper_src,
   "intFromBlur rejects below-min (concurrency 0 dies here, not at the API)")
ok('trimmed === ""' in helper_src,
   "intFromBlur treats empty as null BEFORE Number('')===0")
empty_at = helper_src.find('trimmed === ""')
number_at = helper_src.find("Number(trimmed)")
ok(0 <= empty_at < number_at,
   "empty check precedes Number() so empty never becomes 0")

# Strip the one typed signature into a temp .mjs so the suite does not
# depend on node --experimental-strip-types (Node 18/20 fail that flag;
# JSON.stringify(NaN) is also "null", which hid a return-NaN mutant).
_TYPED = "export function intFromBlur(raw: string, min: number): number | null {"
_JS = "export function intFromBlur(raw, min) {"
ok(_TYPED in helper_src, "helper keeps a typed signature (stripped only for the node drive)")


def drive_cases(cases):
    """One node process; null / NaN / inf / number are distinct tags."""
    js = helper_src.replace(_TYPED, _JS, 1)
    payload = json.dumps([{"raw": raw, "min": mn} for raw, mn, _exp, _msg in cases])
    with tempfile.TemporaryDirectory() as td:
        mjs = Path(td) / "intFromBlur.mjs"
        mjs.write_text(js)
        script = (
            f"import {{ intFromBlur }} from {json.dumps(mjs.resolve().as_uri())};\n"
            f"const cases = {payload};\n"
            "const out = [];\n"
            "for (const c of cases) {\n"
            "  const n = intFromBlur(c.raw, c.min);\n"
            "  if (n === null) out.push(null);\n"
            '  else if (typeof n !== "number") out.push({bad: typeof n});\n'
            '  else if (Number.isNaN(n)) out.push("NaN");\n'
            '  else if (!Number.isFinite(n)) out.push("inf");\n'
            "  else out.push(n);\n"
            "}\n"
            "console.log(JSON.stringify(out));\n"
        )
        r = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            capture_output=True, text=True,
        )
    if r.returncode != 0:
        print("FAIL: intFromBlur drive:", r.stderr.strip() or r.stdout.strip())
        sys.exit(1)
    line = r.stdout.strip().splitlines()[-1]
    return json.loads(line)


print("intFromBlur driven with node (null/NaN/inf distinct)")
CASES = [
    ("", 1, None, "empty string is null (not 0)"),
    ("", 0, None, "empty with min=0 is null (not silently drip=0)"),
    ("   ", 1, None, "whitespace-only is null (not 0)"),
    ("   ", 0, None, "whitespace with min=0 is null (Number('  ')===0)"),
    ("0", 1, None, "0 with min=1 is null (concurrency floor)"),
    ("0", 0, 0, "0 with min=0 is 0 (drip/autogen legal)"),
    ("1", 1, 1, "exact min is allowed"),
    ("2", 1, 2, "valid int passes"),
    (" 3 ", 1, 3, "whitespace-padded int passes"),
    ("4", 1, 4, "concurrency=4 (HTML max) is allowed"),
    ("1.5", 1, None, "non-integer is null (not truncated)"),
    ("abc", 1, None, "NaN is null (not JSON null via Number)"),
    ("-1", 0, None, "below min is null"),
    ("-1", 1, None, "negative with min=1 is null"),
    ("2.0", 1, 2, "2.0 is the integer 2 (Number('2.0')===2)"),
    ("+2", 1, 2, "unary-plus integer passes"),
    ("Infinity", 1, None, "Infinity is null (not a JSON-null collapse)"),
]
got = drive_cases(CASES)
ok(len(got) == len(CASES), "node drive returned one result per case")
for (raw, mn, exp, msg), g in zip(CASES, got):
    if g == "NaN":
        print("FAIL:", msg, "(helper returned NaN, which JSON.stringifies to null and 400s)")
        sys.exit(1)
    if g == "inf":
        print("FAIL:", msg, "(helper returned Infinity)")
        sys.exit(1)
    ok(g == exp, msg)


# --- Channels.tsx budget empty-blur (BACKLOG #34 follow-up / #35) ----------
# Settings empty-blur became a 400 after the API floor. Channel budgets
# accept 0 (tick skips when published_today >= budget), so the same
# Number("")===0 blur silently stalls the money pipeline, and Number("x")
# is JSON null which setattr's None and TypeErrors the comparison.
# Same choke point, min=0 so a typed 0 still PATCHes.
print("Channels.tsx budget empty-blur skips PATCH instead of Number('')===0")
channels_tsx = (_root / "frontend/src/pages/Channels.tsx").read_text()
ok("daily_render_budget: Number(e.target.value)" not in channels_tsx,
   "render-budget blur does not Number() (Number('')===0 stalls renders)")
ok("daily_publish_budget: Number(e.target.value)" not in channels_tsx,
   "publish-budget blur does not Number() (Number('')===0 is budget=0)")
ok('from "../intFromBlur"' in channels_tsx,
   "Channels.tsx imports intFromBlur (not an inlined copy)")
ok("intFromBlur(e.target.value, 0)" in channels_tsx,
   "budget blur uses intFromBlur with min=0 (typed 0 is still legal)")
ok("intFromBlur(e.target.value, 1)" not in channels_tsx,
   "budget blur does not reuse the concurrency min=1 (0 must PATCH)")
ok("if (n === null) { e.target.value = String(channel[key]); return; }" in channels_tsx,
   "null restores the current budget AND returns (does not PATCH)")
ok("updateChannel.mutate({ id: channel.id, body: { [key]: n } })" in channels_tsx,
   "valid int still PATCHes the same key (not a no-op / not hardcoded render)")
ok(re.search(r'commitBudget\(\s*"daily_render_budget"\s*\)', channels_tsx),
   "daily_render_budget input uses commitBudget")
ok(re.search(r'commitBudget\(\s*"daily_publish_budget"\s*\)', channels_tsx),
   "daily_publish_budget input uses commitBudget")
ok("valueAsNumber" not in channels_tsx,
   "Channels.tsx does not use valueAsNumber (empty is 0, same class as Number(''))")
ok("+e.target.value" not in channels_tsx,
   "Channels.tsx does not coerce with unary-plus")

print(f"ALL {_checks} CHECKS PASSED")
