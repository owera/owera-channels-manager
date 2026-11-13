"""RR auto-produce: stale drafts never burn a render slot + RR blank-gap fix.

Run: PYTHONPATH=. .venv/bin/python tests/verify_rr_autoproduce_fresh.py

2026-11-13 08:15–08:46 Fortaleza, RR auto-produce rendered 5 Sept/Oct drafts
(#1316, #1431, #1433, #1434, #1435); all 5 FAILED Gate B "RR hook pace".
Fixture tests/fixtures/rr_autoproduce/renders_20261113.json = their recorded
creation_config (narration_words = real edge-tts pt-BR timings, script,
duration, title, beats, gate reasons), read from a read-only DB copy.

Root cause, verified by replaying compose on the recorded words (no LLM, no TTS):
  * hook pace = draft TEXT. The hook claim is the draft's title head; those heads
    predate the draft-time hook check (#70, 2026-10-06) and the RR pace rules
    (#202): 9–11 words, or "num_ctx 2048" read out long. Replay reproduces the
    exact recorded board + gate reasons, and nothing in compose rewrites a title.
  * blank gap (#1316, #1434) = ENGINE. _os_extend_spoken held a long sentence
    only to last word + 0.3s; the PT inter-sentence pause is ≈0.94s, so the hole
    was 0.64s = 0.52s beyond the 0.12s fade (max 0.50s). Fix: gate-aligned
    brands (RR) hold into the pause just enough to stay under the blank budget,
    never past last word + STALE_CARD_MAX_S − _VO_EDGE. OS unchanged.
  * selection: _auto_produce took drafts by position/id with no freshness or
    hook check, and stale drafts counted as "pending", so autofill never
    refilled those topics with fresh (hook-checked) ideas.
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import os as _os; _os.environ["MANAGER_TOPIC_FLAGS"] = '{"vm_pass_required": []}'
from app.config import settings
from app.models import Channel, JobRun, OAuthStatus, Topic, Video, VideoStatus
from app.services import autoproduce_policy as ap
from app.services import craft, render_loop, video_gen
from app.services import episode_guard as _eg
from app.services.engines import storyboard

_eg.duplicate_episode_reason = lambda session, video: None
_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


def no_tts(*a, **k):
    raise AssertionError("test must not call edge-tts")


video_gen.measure_claim = no_tts   # every measurement below is injected

FX = json.loads((Path(__file__).parent / "fixtures" / "rr_autoproduce"
                 / "renders_20261113.json").read_text())
NOW = datetime(2026, 11, 13, 11, 15, tzinfo=timezone.utc)   # 08:15 Fortaleza


def no_llm(*a, **k):
    raise AssertionError("RR VO-sentence path must not call the LLM")


def replay(fx, brand="rr"):
    S, W, D, T = fx["script"], fx["words"], fx["duration"], fx["title"]
    html = storyboard.compose(subject=fx["subject"], script=S, words=W, duration=D,
                              resolution="portrait", width=1080, height=1920,
                              topic_id=fx["topic_id"], content_format="short",
                              language="Portuguese", llm=no_llm, brand=brand, title=T,
                              topic_name=fx["topic_name"])
    beats = craft.beats_from_html(html)
    g = craft.video_maker_gate(
        beats, content_format="short", used_fallback=False,
        beat_timing=craft.BEAT_TIMING_CURRENT,
        hook_pace=craft.hook_pace_marker(beats, W, brand, "short"),
        card_sync=craft.card_sync_marker(beats, W, "short", script=S, brand=brand),
        card_text=craft.card_text_marker(beats, S, W, brand=brand),
        card_vo=craft.card_vo_marker(beats, S, brand=brand))
    return beats, g


def checks_of(g):
    return {k.get("check") for k in (g.get("kinds") or [])}


# ---------------------------------------------------------------------------
print("replay: the 5 recorded RR renders (real edge-tts words, no LLM / TTS)")
for vid, fx in FX.items():
    beats, g = replay(fx)
    kinds = checks_of(g)
    ok(g["result"] == "FAIL" and any(c.startswith("hook_") for c in kinds),
       f"#{vid}: still FAILs RR hook pace — draft text ({fx['title'].split('·')[0].strip()!r})")
    ok("blank_gap" not in kinds, f"#{vid}: no blank gap after the fix")
    ok(not any("Blank gap" in r for r in g["reasons"]) and
       all(r.startswith("[B] RR hook pace") for r in g["reasons"]),
       f"#{vid}: the only remaining reason is RR hook pace (no new gate reason)")
    rec = [r for r in fx["recorded_gate"] if "RR hook pace" in r]
    ok([r for r in g["reasons"] if "RR hook pace" in r] == rec,
       f"#{vid}: hook-pace reason identical to the recorded render")
for vid in ("1316", "1434"):
    ok(any("Blank gap" in r for r in FX[vid]["recorded_gate"]),
       f"#{vid}: recorded render had the 0.64s blank gap (pre-fix)")

print("engine: _os_extend_spoken — RR closes the PT pause, OS keeps the 0.3s tail")
fx = FX["1316"]
W = fx["words"]
card = {"type": "quote", "start": 8.723, "dur": 2.8,
        "cue": "O CSS mudou sem o meu aceite.", "text": "O CSS mudou sem o meu aceite."}
last = craft.spoken_span(card["text"], W)[1]
nxt = 12.351
os_card = dict(card)
tok = storyboard._OS_VO_CARDS.set(True)
try:
    storyboard._os_extend_spoken(os_card, nxt, W, [])
    rr_card = dict(card)
    with craft.gate_aligned_sync(True):
        storyboard._os_extend_spoken(rr_card, nxt, W, [])
finally:
    storyboard._OS_VO_CARDS.reset(tok)
os_end = os_card["start"] + os_card["dur"]
rr_end = rr_card["start"] + rr_card["dur"]
ok(abs(os_end - (last + craft.QUOTE_HOLD_TAIL_S)) < 2e-3, "OS: last word + 0.3s (unchanged)")
hole = nxt - rr_end - craft.BEAT_GAP_S
ok(hole <= craft.BLANK_GAP_MAX_S + 1e-6, f"RR: hole beyond fade {hole:.3f}s ≤ 0.50s")
ok(rr_end - last <= craft.STALE_CARD_MAX_S - storyboard._VO_EDGE + 1e-6,
   f"RR: card leaves {rr_end - last:.3f}s after its last word (≤ stale budget − edge)")
ok(rr_end <= nxt - craft.BEAT_GAP_S + 1e-6, "RR: never into the next card")

# ---------------------------------------------------------------------------
print("policy: config is per channel, RR only by default")
ok(isinstance(settings.auto_produce_draft_policy, dict)
   and set(settings.auto_produce_draft_policy) == {"rr"}, "default policy key = rr only")
rr_ch = Channel(id=2, slug="rodrigo-recio", name="Rodrigo Recio")
os_ch = Channel(id=1, slug="owera-software", name="Owera Software")
ok(ap.policy_for(rr_ch) is not None and ap.policy_for(rr_ch)["key"] == "rr", "RR has a policy")
ok(ap.policy_for(os_ch) is None, "OS: no policy (auto-produce unchanged)")
ok(ap.policy_for(Channel(id=7, slug="cust", name="Acme")) is None, "other tenants unchanged")

POL = dict(settings.auto_produce_draft_policy["rr"])
SHORT = Topic(id=1, channel_id=2, name="IA", content_format="short")


def draft(title, created, **kw):
    return Video(channel_id=2, topic_id=1, subject=title, title=kw.pop("title", None),
                 status=VideoStatus.DRAFT, created_at=created, **kw)


print("policy: the 5 burned drafts + the next 5 (#1321 #1322 #1432 #1436 #1437) are stale")
NEXT = {1321: ("O agente queimou tokens no terminal sem eu ver. · Claude Code 10", "2026-09-23 01:50:54"),
        1322: ("O agente force-pushou a main. · Claude Code 11", "2026-09-23 01:50:54"),
        1432: ("O pull do 32B derrubou o 14B da VRAM. · Local 74", "2026-10-04 02:36:32"),
        1436: ("Power limit em 150W corta a 4090, não é engenharia. · IA 231", "2026-10-05 02:33:42"),
        1437: ("sentence-transformers no CPU deixa a GPU ociosa, não é engenharia. · IA 232", "2026-10-05 02:33:42")}
cases = {int(k): (f["title"], f["created_at"][:19].replace("T", " ")) for k, f in FX.items()}
cases.update(NEXT)
for vid, (subj, created) in sorted(cases.items()):
    v = draft(subj, datetime.fromisoformat(created))
    r = ap.stale_reason(v, SHORT, POL, now=NOW, voice="pt-BR-AntonioNeural",
                        measure=lambda h, vo: 2.0)
    ok(r and "old" in r, f"#{vid}: stale ({r})")

print("policy: hook preflight on fresh drafts (existing #70 rule, injected measure)")
fresh = NOW - timedelta(hours=2)
v = draft("Eu travei o contexto em 8K. · IA 240", fresh)
calls = []
r = ap.stale_reason(v, SHORT, POL, now=NOW, voice="pt-BR-AntonioNeural",
                    measure=lambda h, vo: calls.append(h) or 1.9)
ok(r is None and calls == ["Eu travei o contexto em 8K."], "fresh ≤8-word claim spoken 1.9s → eligible")
ok(json.loads(v.creation_config)[ap.CC_KEY]["result"] == "PASS", "PASS cached on creation_config")
r = ap.stale_reason(v, SHORT, POL, now=NOW, voice="pt-BR-AntonioNeural",
                    measure=lambda h, vo: calls.append(h) or 1.9)
ok(r is None and len(calls) == 1, "cached PASS: no second measurement on the next tick")
v = draft("Na bateria o 14B fez 12. Na tomada, 46. · Local 80", fresh)
r = ap.stale_reason(v, SHORT, POL, now=NOW, voice="pt-BR-AntonioNeural", measure=lambda h, vo: 2.0)
ok(r and "9 words" in r, f"fresh 9-word claim → skipped ({r})")
v = draft("num_ctx 2048 corta o prompt. · IA 241", fresh)
r = ap.stale_reason(v, SHORT, POL, now=NOW, voice="pt-BR-AntonioNeural", measure=lambda h, vo: 3.4)
ok(r and "spoken by 3.40s" in r, f"fresh claim spoken 3.4s → skipped ({r})")


def tts_down(h, vo):
    raise RuntimeError("edge-tts 403")


v = draft("Eu travei o contexto em 8K. · IA 242", fresh)
ok(ap.stale_reason(v, SHORT, POL, now=NOW, voice="pt-BR-AntonioNeural", measure=tts_down) is None,
   "TTS outage → word count only, fail-open (same as draft time)")
v = draft("O agente queimou tokens no terminal sem eu ver.", datetime(2026, 9, 1),
          script="O agente queimou tokens no terminal sem eu ver. Resto.",
          creation_config=json.dumps({"script_source": craft.SCRIPT_SOURCE_PROVIDED}))
ok(ap.stale_reason(v, SHORT, POL, now=NOW) is None,
   "provided script exempt (its own pre-render hook check runs at submit)")
LONG = Topic(id=3, channel_id=2, name="L", content_format="long")
v = draft("Um título longo de vídeo longo com muitas palavras demais aqui", fresh)
ok(ap.stale_reason(v, LONG, POL, now=NOW) is None, "long format: no short hook preflight")
pol_nb = {"not_before": "2026-11-11T12:32:50Z"}
v = draft("Eu travei o contexto em 8K.", datetime(2026, 11, 10, 12, 0))
ok("before 2026-11-11" in (ap.stale_reason(v, SHORT, pol_nb, now=NOW) or ""), "not_before option")


# ---------------------------------------------------------------------------
def setup(slug, name, budget=2):
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(eng)
    s = Session(eng)
    ch = Channel(name=name, slug=slug, oauth_status=OAuthStatus.CONNECTED,
                 daily_render_budget=budget, daily_publish_budget=2)
    s.add(ch); s.commit(); s.refresh(ch)
    t = Topic(channel_id=ch.id, name="IA", weight=1, active=True, content_format="short")
    s.add(t); s.commit(); s.refresh(t)
    return s, ch, t


def add(s, ch, t, subject, created, pos):
    v = Video(channel_id=ch.id, topic_id=t.id, subject=subject, status=VideoStatus.DRAFT,
              position=pos, created_at=created)
    s.add(v); s.commit(); s.refresh(v)
    return v


print("_auto_produce RR: stale drafts skipped with a reason, fresh draft takes the slot")
s, ch, t = setup("rodrigo-recio", "Rodrigo Recio", budget=2)
now = datetime.now(timezone.utc).replace(tzinfo=None)
old1 = add(s, ch, t, "Na bateria o 14B fez 12. Na tomada, 46. · Local 73", now - timedelta(days=40), 1)
old2 = add(s, ch, t, "O agente force-pushou a main. · Claude Code 11", now - timedelta(days=51), 2)
long_claim = add(s, ch, t, "O Defender escaneia o GGUF a cada load, não é engenharia. · IA 300",
                 now - timedelta(hours=1), 3)
good = add(s, ch, t, "Eu travei o contexto em 8K. · IA 301", now - timedelta(hours=1), 4)
render_loop._auto_produce(s); s.commit()
for v in (old1, old2, long_claim, good):
    s.refresh(v)
ok(good.status == VideoStatus.QUEUED, "fresh passing draft queued")
ok(all(v.status == VideoStatus.REJECTED for v in (old1, old2, long_claim)),
   "stale + failing drafts out of the bench (rejected)")
ok(all((v.rejected_reason or "").startswith(ap.SKIP_PREFIX) for v in (old1, old2, long_claim)),
   "rejected_reason says 'auto-produce skipped (stale draft): …'")
ok("11 words" in long_claim.rejected_reason, "hook-preflight reason recorded")
skips = s.exec(select(JobRun).where(JobRun.kind == "produce", JobRun.status == "skipped")).all()
ok(len(skips) == 3, "one produce/skipped JobRun per skipped draft")
renders = s.exec(select(JobRun).where(JobRun.kind == "render")).all()
ok(renders == [], "no render slot spent on a skipped draft")
from app.services import autofill_loop
ok(autofill_loop._pending_count(s, t.id) == 1,
   "skipped drafts no longer count as pending → autofill refills the topic")
render_loop._auto_produce(s); s.commit()
skips2 = s.exec(select(JobRun).where(JobRun.kind == "produce", JobRun.status == "skipped")).all()
ok(len(skips2) == 3, "idempotent: next tick writes nothing new")

print("_auto_produce OS: same stale draft still queued (default unchanged)")
s, ch, t = setup("owera-software", "Owera Software", budget=1)
old = add(s, ch, t, "Na bateria o 14B fez 12. Na tomada, 46. · Local 73", now - timedelta(days=40), 1)
render_loop._auto_produce(s); s.commit(); s.refresh(old)
ok(old.status == VideoStatus.QUEUED and not old.rejected_reason, "OS stale draft queued as before")

print("dry-run helper: apply=False writes nothing")
s, ch, t = setup("rodrigo-recio", "Rodrigo Recio")
old = add(s, ch, t, "O agente force-pushou a main. · Claude Code 11", now - timedelta(days=51), 1)
rows = s.exec(select(Video, Topic).join(Topic, Topic.id == Video.topic_id)).all()
kept, skipped = ap.filter_rows(s, ch, rows, apply=False)
s.refresh(old)
ok(kept == [] and len(skipped) == 1 and old.status == VideoStatus.DRAFT, "dry-run: reported, not written")

print(f"\nALL {_checks} CHECKS PASSED")
