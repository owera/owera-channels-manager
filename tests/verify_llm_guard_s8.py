"""S8 LLM guard checks (PO CM cut S8; LegalOps L2/L3/L5/L7; kill switch; release).

Run: PYTHONPATH=. uv run python tests/verify_llm_guard_s8.py

No network, no real grok, in-memory SQLite. Covers:
  - flag MANAGER_LLM_EXTERNAL_RELEASE default OFF = #217 allowlist exactly
  - kill switch (Workspace.llm_disabled) incl. internal; ops CLI enable/disable
  - release (flag ON): paid|is_test AND terms+privacy; backend api+ack; purpose;
    declared fields; unknown workspace; render pre-check follows the same rule
  - L2: ws A data never in a ws B prompt (context builder + note_data re-check)
  - L3: field allowlist == the L6 line; outside fields rejected / stripped;
    llm_call log = workspace, call type, field names, tokens, never content
  - L7: raw YouTube API payload / token fields refused before any spawn
  - L5: Gate B stays mandatory (external never skip-gate auto-approves; no bypass)
"""
from __future__ import annotations

import contextlib
import inspect
import io
import json
import logging
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

import app.db as appdb
from app.config import settings
from app.models import Channel, ChannelMetric, Topic, Video, Workspace
from app.services import llm, llm_context, llm_guard, mpt_client, render_loop

ROOT = Path(__file__).resolve().parent.parent
_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record):
        self.lines.append(record.getMessage())


cap = _Capture()
for name in ("manager.llm_guard",):
    logging.getLogger(name).addHandler(cap)
    logging.getLogger(name).setLevel(logging.INFO)

NOW = datetime(2026, 11, 14, tzinfo=timezone.utc)
eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
SQLModel.metadata.create_all(eng)
with Session(eng) as s:
    s.add(Workspace(id=1, slug="owera", name="Owera"))
    s.add(Workspace(id=2, slug="paid-a", name="A", subscription_status="active",
                    terms_accepted_at=NOW, privacy_accepted_at=NOW))
    s.add(Workspace(id=3, slug="test-b", name="B", is_test=True,
                    terms_accepted_at=NOW, privacy_accepted_at=NOW))
    s.add(Workspace(id=4, slug="trial-c", name="C"))                       # trialing, nothing
    s.add(Workspace(id=5, slug="paid-noterms", name="D", subscription_status="active",
                    privacy_accepted_at=NOW))
    s.add(Workspace(id=6, slug="test-noprivacy", name="E", is_test=True, terms_accepted_at=NOW))
    s.add(Workspace(id=7, slug="pastdue", name="F", subscription_status="past_due",
                    terms_accepted_at=NOW, privacy_accepted_at=NOW))
    s.commit()
    chA = Channel(workspace_id=2, slug="a", name="A ch", default_skip_gate=True)
    chB = Channel(workspace_id=3, slug="b", name="B ch", default_skip_gate=True)
    ch1 = Channel(workspace_id=1, slug="o", name="O ch", default_skip_gate=True)
    s.add(chA); s.add(chB); s.add(ch1); s.commit()
    for ch, word in ((chA, "ALPHA"), (chB, "BRAVO")):
        t = Topic(channel_id=ch.id, name=f"{word} topic"); s.add(t); s.commit()
        for i in range(3):
            s.add(Video(channel_id=ch.id, topic_id=t.id, subject=f"{word}-SUBJ-{i}",
                        title=f"{word}-TITLE-{i}", description=f"{word}-DESC-{i} " + "x" * 3000,
                        tags_json=json.dumps([f"{word.lower()}tag{i}"])))
        s.add(ChannelMetric(channel_id=ch.id, subscriber_count=111 if word == "ALPHA" else 222,
                            view_count=5000, video_count=3))
    s.commit()
    CHA, CHB, CH1 = chA.id, chB.id, ch1.id

appdb_patch = patch.object(appdb, "engine", eng)
appdb_patch.start()

ON = dict(llm_external_release=True, llm_backend="api", xai_data_retention_ack="30d_no_training")


@contextlib.contextmanager
def cfg(**kw):
    with contextlib.ExitStack() as st:
        for k, v in kw.items():
            st.enter_context(patch.object(settings, k, v))
        yield


def blocked(fn):
    try:
        fn()
        return None
    except llm_guard.LLMBlocked as e:
        return e


F = ("video_title", "script")

# --------------------------------------------------------------------------- flag OFF
print("flag MANAGER_LLM_EXTERNAL_RELEASE default OFF = #217 allowlist")
ok(settings.llm_external_release is False, "llm_external_release defaults to False")
ok(not llm_guard.external_release_enabled(), "external_release_enabled() False by default")
for ws in range(1, 8):
    allowed = llm_guard.block_reason(ws, "render", F) is False
    ok(allowed == (ws == 1), f"flag OFF ws{ws}: allowed iff allowlisted ({allowed})")
e = blocked(lambda: llm_guard.require("ideas", workspace_id=2, fields=F))
ok(e is not None and e.reason is None and str(e) == str(llm_guard.LLMBlocked(2, "ideas")),
   "flag OFF: paid+accepted external → same LLMBlocked message as #217")
cap.lines.clear()
blocked(lambda: llm_guard.require("ideas", workspace_id=3))
ok(cap.lines == ["llm_blocked_external_google_data workspace_id=3 call_type=ideas"],
   "flag OFF: exact pre-S8 block log line")
with cfg(llm_internal_workspace_ids="1,2"):
    ok(llm_guard.require("ideas", workspace_id=2) == 2, "allowlist 1,2 still lets tenant ws2 through")
ok(not llm_guard.would_allow(2, "render") and llm_guard.would_allow(1, "render"),
   "would_allow mirrors require without raising")

# --------------------------------------------------------------------------- kill switch
print("kill switch")


def set_kill(ws, on):
    with Session(eng) as s:
        w = s.get(Workspace, ws); w.llm_disabled = on; s.add(w); s.commit()


set_kill(1, True)
e = blocked(lambda: llm_guard.require("ideas", workspace_id=1))
ok(e is not None and e.reason == "kill_switch", "internal ws1 kill-switched → blocked reason=kill_switch")
ok(not llm_guard.would_allow(1, "render"), "render pre-check honours the kill switch")
set_kill(1, False)
ok(llm_guard.require("ideas", workspace_id=1) == 1, "switch off → allowed again (no restart)")
with patch.object(llm_guard, "_workspace_row", return_value=(None, "OperationalError")):
    ok(llm_guard.require("ideas", workspace_id=1) == 1,
       "internal + unreadable row → today's behavior (allowed, warning)")
    with cfg(**ON):
        e = blocked(lambda: llm_guard.require("ideas", workspace_id=2, fields=F))
        ok(e is not None and e.reason == "workspace_unreadable", "external + unreadable → blocked")
from app import llm_kill_switch as ks  # noqa: E402

with patch.object(ks, "engine", eng), patch.object(ks, "init_db", lambda: None):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = ks.main(["disable", "--workspace", "1"])
    row = json.loads(out.getvalue().strip())
    ok(rc == 0 and row["llm_disabled"] is True and row["render_llm"] == "kill_switch",
       "ops CLI disable → llm_disabled true, guard verdict kill_switch")
    ok(blocked(lambda: llm_guard.require("x", workspace_id=1)) is not None, "CLI disable is effective")
    with contextlib.redirect_stdout(io.StringIO()):
        ok(ks.main(["enable", "--workspace", "1"]) == 0, "ops CLI enable")
    ok(llm_guard.require("x", workspace_id=1) == 1, "CLI enable restores")
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        ok(ks.main(["disable"]) == 2, "disable without --workspace → usage error")
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        ks.main(["status"])
    ok(len(out.getvalue().strip().splitlines()) == 7 and "token" not in out.getvalue().lower(),
       "status lists every workspace, no secrets")

# --------------------------------------------------------------------------- release (flag ON)
print("release check (flag ON)")
with cfg(**ON):
    ok(llm_guard.require("render", workspace_id=2, fields=F) == 2, "paid + terms + privacy → allowed")
    ok(llm_guard.require("ideas", workspace_id=3, fields=F) == 3, "is_test + terms + privacy → allowed")
    cases = [(4, "not_paid_or_test"), (5, "terms_privacy_not_accepted"),
             (6, "terms_privacy_not_accepted"), (7, "not_paid_or_test"), (99, "workspace_unknown")]
    for ws, why in cases:
        e = blocked(lambda: llm_guard.require("render", workspace_id=ws, fields=F))
        ok(e is not None and e.reason == why, f"ws{ws} → {why}")
    e = blocked(lambda: llm_guard.require("rubric_review", workspace_id=2, fields=F))
    ok(e is not None and e.reason == "purpose_not_released", "unreleased purpose blocked")
    e = blocked(lambda: llm_guard.require("render/rubric_review", workspace_id=2, fields=F))
    ok(e is not None and e.reason == "purpose_not_released", "composite call type checks every part")
    e = blocked(lambda: llm_guard.require("ideas", workspace_id=2, fields=None))
    ok(e is not None and e.reason == "fields_undeclared", "undeclared prompt fields → blocked (fail closed)")
    set_kill(2, True)
    e = blocked(lambda: llm_guard.require("ideas", workspace_id=2, fields=F))
    ok(e is not None and e.reason == "kill_switch", "kill switch beats release")
    set_kill(2, False)
    with cfg(llm_backend="cli"):
        e = blocked(lambda: llm_guard.require("ideas", workspace_id=2, fields=F))
        ok(e is not None and e.reason == "backend_may_train", "CLI consumer backend never released")
    with cfg(xai_data_retention_ack=""):
        e = blocked(lambda: llm_guard.require("ideas", workspace_id=2, fields=F))
        ok(e is not None and e.reason == "backend_may_train", "api without retention ack → blocked")
    ok(llm_guard.would_allow(2, "render") and not llm_guard.would_allow(4, "render"),
       "render pre-check follows the release rule")
    ok(llm_guard.is_allowed(2) is False, "is_allowed stays the internal allowlist only")
    # MPT: declared fields, released purpose
    with patch.object(mpt_client.MPTClient, "_client", side_effect=AssertionError("http")):
        with llm_guard.scope(4, "render_submit"):
            e = blocked(lambda: mpt_client.MPTClient().submit({}))
        ok(e is not None and e.reason == "not_paid_or_test", "MPT submit gated by release (no HTTP)")
    # complete() through the API backend, tokens logged, no content
    got = []

    def fake_api(prompt, system, **kw):
        got.append(prompt)
        llm._last_usage.set((42, 7))
        return "draft"

    cap.lines.clear()
    with patch.object(llm, "_complete_api", side_effect=fake_api):
        with llm_guard.scope(2, "ideas"), llm_guard.declare("topic", "video_title"):
            ok(llm.complete("SECRET-PROMPT-BODY ALPHA") == "draft", "released ws2 complete() works")
    line = [l for l in cap.lines if l.startswith("llm_call ")]
    ok(line == ["llm_call workspace_id=2 call_type=ideas fields=topic,video_title backend=api "
                "input_tokens=42 output_tokens=7 tokens=reported"], f"llm_call line: {line}")
    ok(all("SECRET-PROMPT" not in l and "draft" not in l for l in cap.lines), "no content in logs")
    with patch.object(llm, "_complete_api", side_effect=fake_api):
        with llm_guard.scope(2, "ideas"):
            e = blocked(lambda: llm.complete("p"))
    ok(e is not None and e.reason == "fields_undeclared", "complete() without declared fields → blocked when released")

# render pre-check (flag OFF / ON) parks the row with the reason
class _S:
    def add(self, o):
        pass


with patch.object(render_loop.quota, "log", lambda *a, **k: None):
    v = SimpleNamespace(id=1, status="queued", error=None)
    with cfg(**ON):
        ok(render_loop._llm_guard_block(_S(), v, SimpleNamespace(workspace_id=4, id=9)) is True
           and "not_paid_or_test" in v.error, "render pre-check parks unpaid external row with reason")
        v2 = SimpleNamespace(id=2, status="queued", error=None)
        ok(render_loop._llm_guard_block(_S(), v2, SimpleNamespace(workspace_id=3, id=9)) is False,
           "render pre-check passes released ws3")

# --------------------------------------------------------------------------- L2
print("L2: workspace-scoped data")
prompts: list[str] = []


def fake_run(cmd, **kw):
    prompts.append(cmd[-1])
    return SimpleNamespace(returncode=0, stdout="ok", stderr="")


with Session(eng) as s, patch.object(subprocess, "run", side_effect=fake_run), \
        cfg(llm_internal_workspace_ids="1,2,3"):
    with llm_guard.scope(3, "ideas"):
        e = blocked(lambda: llm_context.youtube_context(s, CHA))
        ok(isinstance(e, llm_guard.LLMCrossWorkspace), "ws B scope reading ws A channel → LLMCrossWorkspace")
        e = blocked(lambda: llm.complete("anything", fields=("topic",)))
        ok(isinstance(e, llm_guard.LLMCrossWorkspace), "the noted foreign read also blocks the next complete()")
    ok(prompts == [], "nothing spawned after a cross-workspace read")
    with llm_guard.scope(3, "ideas"):
        ctxB = llm_context.youtube_context(s, CHB)
        with llm_guard.declare(*llm_context.fields_of(ctxB)):
            llm.complete("ideas for:\n" + llm_context.render_block(ctxB))
    pB = prompts[-1]
    ok("BRAVO-TITLE-0" in pB and "222" in pB, "ws B prompt carries ws B data")
    ok("ALPHA" not in pB and "111" not in pB, "ws A data never appears in a ws B prompt")
    with llm_guard.scope(2, "ideas"):
        ctxA = llm_context.youtube_context(s, CHA)
        llm.complete(llm_context.render_block(ctxA), fields=llm_context.fields_of(ctxA))
    ok("ALPHA-TITLE-0" in prompts[-1] and "BRAVO" not in prompts[-1], "and vice versa")
    with llm_guard.scope(2, "ideas"):
        e = blocked(lambda: llm_context.youtube_context(s, CHB))
    ok(isinstance(e, llm_guard.LLMCrossWorkspace), "ws A scope reading ws B → blocked")
    e = blocked(lambda: llm_context.youtube_context(s, CHA))
    ok(isinstance(e, llm_guard.LLMCrossWorkspace), "no scope → context read refused")
    with llm_guard.scope(2, "ideas"):
        llm_guard.note_data(3)
        e = blocked(lambda: llm.complete("x", fields=("topic",)))
    ok(isinstance(e, llm_guard.LLMCrossWorkspace), "note_data from another ws → complete() refused")
    with llm_guard.scope(2, "ideas"):
        ok(llm_guard.foreign_data_workspaces(2) == set(), "notes do not leak across scopes")

# --------------------------------------------------------------------------- L3
print("L3: field allowlist")
ok(llm_guard.YT_FIELDS == {"video_title", "video_description", "video_tags", "channel_stats"},
   "Google-sourced allowlist == the L6 line (titles, descriptions, tags, channel stats)")
ok(llm_guard.CHANNEL_STATS_KEYS == {"subscriber_count", "view_count", "video_count"},
   "channel stats = raw counts only (L4)")
ok(not (llm_guard.CM_FIELDS & {"email", "comment", "comments", "commenter", "token",
                               "access_token", "refresh_token", "owner_email", "analytics"}),
   "no personal / comment / token field in the CM set")
with llm_guard.scope(1, "ideas"):
    e = blocked(lambda: llm_guard.declare("comment_author").__enter__())
    ok(isinstance(e, llm_guard.LLMFieldRejected), "declare(outside field) → LLMFieldRejected")
    prompts.clear()
    with patch.object(subprocess, "run", side_effect=fake_run):
        e = blocked(lambda: llm.complete("x", fields=("video_title", "owner_email")))
    ok(isinstance(e, llm_guard.LLMFieldRejected) and prompts == [], "complete(outside field) rejected before spawn")
    clean, stripped = llm_guard.minimize({
        "video_title": "T" * 500, "video_description": "D" * 5000, "video_tags": ["a"] * 99,
        "channel_stats": {"subscriber_count": 10, "view_count": 20, "video_count": 3,
                          "avg_views": 6.6, "avg_ctr": 0.04, "subDelta": 2, "pct": 50},
        "owner_email": "x@y.z", "access_token": "ya29.x", "comments": ["hi"], "impressions": 0,
        "ctr": 0.0, "topic": "rag", "niche": {"nested": 1}})
ok(set(clean) == {"video_title", "video_description", "video_tags", "channel_stats", "topic"},
   f"only allowlisted fields kept ({sorted(clean)})")
ok(clean["channel_stats"] == {"subscriber_count": 10, "view_count": 20, "video_count": 3},
   "derived stats (avg_*, ctr, delta, pct) stripped")
ok({"owner_email", "access_token", "comments", "impressions", "ctr", "niche",
    "channel_stats.avg_views", "channel_stats.avg_ctr", "channel_stats.subDelta",
    "channel_stats.pct"} <= set(stripped), "stripped names reported")
ok(len(clean["video_title"]) == llm_guard.MAX_TITLE_CHARS
   and len(clean["video_description"]) == llm_guard.MAX_DESCRIPTION_CHARS
   and len(clean["video_tags"]) == llm_guard.MAX_TAGS, "L7 caps applied")
strip_line = [l for l in cap.lines if l.startswith("llm_fields_stripped")]
ok(strip_line and "ya29" not in strip_line[-1] and "x@y.z" not in strip_line[-1],
   "strip log has names only, no values")
with Session(eng) as s, cfg(llm_internal_workspace_ids="1,2"), llm_guard.scope(2, "ideas"):
    ctx = llm_context.youtube_context(s, CHA)
ok(set(ctx) <= llm_guard.YT_FIELDS and all(len(d) <= llm_guard.MAX_DESCRIPTION_CHARS
                                           for d in ctx["video_description"]),
   "context builder emits only L6 fields, capped")
cli_line = []
cap.lines.clear()
with patch.object(subprocess, "run", side_effect=fake_run), llm_guard.scope(1, "metadata"):
    llm.complete("CONTENT-XYZ " * 10, fields=("video_title",))
cli_line = [l for l in cap.lines if l.startswith("llm_call ")]
ok(len(cli_line) == 1 and "workspace_id=1 call_type=metadata fields=video_title backend=cli" in cli_line[0]
   and "tokens=estimated" in cli_line[0] and "CONTENT-XYZ" not in cli_line[0],
   "CLI llm_call line: ws, call type, fields, estimated tokens, no content")
for mod, needle in (("app/services/video_gen.py", "llm_guard.declare("),
                    ("app/services/metadata.py", "llm_guard.declare("),
                    ("app/services/engines/worker.py", "llm_guard.declare(*_WORKER_LLM_FIELDS)")):
    ok(needle in (ROOT / mod).read_text(), f"{mod} declares its prompt fields")

# --------------------------------------------------------------------------- L7
print("L7: no raw dumps")
raw = json.dumps({"kind": "youtube#videoListResponse", "etag": "abc", "items": []})
prompts.clear()
with patch.object(subprocess, "run", side_effect=fake_run), llm_guard.scope(1, "ideas"):
    for bad in (raw, '{"kind": "youtubeAnalytics#resultTable"}', '{"columnHeaders": [{"name": "views"}]}',
                '{"refresh_token": "1//x"}'):
        e = blocked(lambda: llm.complete("summarize " + bad, fields=("video_title",)))
        ok(isinstance(e, llm_guard.LLMRawDump), f"raw payload refused: {bad[:30]}")
    e = blocked(lambda: llm.complete("p", system=raw, fields=("video_title",)))
    ok(isinstance(e, llm_guard.LLMRawDump), "raw payload in system prompt refused")
    ok(prompts == [], "nothing spawned for raw dumps")
    ok(llm.complete("plain text about youtube kind of videos", fields=("topic",)) == "ok",
       "normal prompt mentioning youtube passes")

# --------------------------------------------------------------------------- L5
print("L5: Gate B mandatory")
v = SimpleNamespace(skip_gate=None)
ok(render_loop._effective_skip_gate(v, SimpleNamespace(workspace_id=2, default_skip_gate=True)) is False,
   "external workspace: channel skip_gate ignored (customer approves)")
ok(render_loop._effective_skip_gate(SimpleNamespace(skip_gate=True),
                                    SimpleNamespace(workspace_id=3, default_skip_gate=True)) is False,
   "external workspace: per-video skip_gate ignored")
with cfg(**ON):
    ok(render_loop._effective_skip_gate(v, SimpleNamespace(workspace_id=2, default_skip_gate=True)) is False,
       "release flag ON does not re-enable skip_gate for external")
ok(render_loop._effective_skip_gate(v, SimpleNamespace(workspace_id=1, default_skip_gate=True)) is True,
   "internal ws1 skip_gate unchanged")
fin = inspect.getsource(render_loop)
ok(fin.index("_effective_skip_gate(video, channel) and _vm_pass_missing(video)")
   < fin.index("elif _effective_skip_gate(video, channel):\n        video.status = VideoStatus.APPROVED"),
   "skip_gate auto-approve still needs the VM Gate B PASS first")
for mod in ("app/services/llm_guard.py", "app/services/llm_context.py", "app/llm_kill_switch.py"):
    src = (ROOT / mod).read_text()
    ok("skip_gate" not in src and "approved_at" not in src and "VideoStatus" not in src,
       f"{mod} has no approval / Gate B bypass")

appdb_patch.stop()
print(f"\nALL {_checks} CHECKS PASSED")
