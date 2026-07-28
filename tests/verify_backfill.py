"""Regression checks for run/backfill_ch2_metadata.py (backlog #14).

Run: PYTHONPATH=. uv run python tests/verify_backfill.py

The tool mutates LIVE published videos in --apply mode, so these checks pin the
safety properties: plan mode performs zero update calls and zero DB writes,
candidate selection/ranking, the retag-vs-regenerate split (a proven PT title is
never rewritten), the snippet merge preserving categoryId, drift skipping, the
10/day quota cap, JobRun logging, and DB write-back. In-memory DB + a fake
YouTube service — no network, no manager.db. Exits non-zero on first failure.
"""
import importlib.util
import json
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "backfill_ch2_metadata", ROOT / "run" / "backfill_ch2_metadata.py")
bf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bf)

from app.models import (Channel, JobRun, OAuthStatus, Playlist, Topic, Video,  # noqa: E402
                        VideoMetric, VideoStatus, utcnow)

_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


# ---- fake YouTube service ---------------------------------------------------
class _Req:
    def __init__(self, fn):
        self._fn = fn

    def execute(self):
        return self._fn()


class FakeVideosApi:
    """videos().list/update backed by a dict of yt_id -> snippet; records calls."""

    def __init__(self, snippets):
        self.snippets = snippets
        self.update_calls = []
        self.list_calls = 0

    def list(self, part=None, id=None, maxResults=None):
        ids = (id or "").split(",")
        self.list_calls += 1
        return _Req(lambda: {"items": [
            {"id": i, "snippet": dict(self.snippets[i])} for i in ids
            if i in self.snippets]})

    def update(self, part=None, body=None):
        self.update_calls.append(body)

        def _do():
            self.snippets[body["id"]] = dict(body["snippet"])
            return body
        return _Req(_do)


class FakeService:
    def __init__(self, snippets):
        self._v = FakeVideosApi(snippets)

    def videos(self):
        return self._v


# ---- fixture DB -------------------------------------------------------------
engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                       poolclass=StaticPool)
SQLModel.metadata.create_all(engine)
session = Session(engine)

ch = Channel(slug="rodrigo-recio", name="Rodrigo Recio",
             yt_channel_id="UCCH2", oauth_status=OAuthStatus.CONNECTED)
other = Channel(slug="owera-software", name="Owera", oauth_status=OAuthStatus.CONNECTED)
session.add(ch)
session.add(other)
session.commit()
pl = Playlist(channel_id=ch.id, yt_playlist_id="PLabc", title="Serie")
session.add(pl)
session.commit()
t_short = Topic(channel_id=ch.id, name="T", content_format="short", playlist_id=pl.id)
t_other = Topic(channel_id=other.id, name="O", content_format="short")
session.add(t_short)
session.add(t_other)
session.commit()

MARKER = bf._SUB_CONFIRM_MARKER


def mkvideo(subject, yt, desc, status=VideoStatus.PUBLISHED, channel=None,
            topic=None, script="roteiro em português"):
    v = Video(channel_id=(channel or ch).id, topic_id=(topic or t_short).id,
              subject=subject, status=status, yt_video_id=yt,
              description=desc, script=script)
    session.add(v)
    session.commit()
    return v


# v1: PT title live, missing tags+CTA, top views -> retag_only
v1 = mkvideo("vram", "yt1", "descrição sem bloco")
# v2: EN title+desc live -> regenerate
v2 = mkvideo("crewai trap", "yt2", "plain english pitch")
# v3: already has the CTA marker in DB -> never a candidate
v3 = mkvideo("done", "yt3", f"ok {MARKER}")
# v4: draft -> excluded
v4 = mkvideo("draft", None, "x", status=VideoStatus.DRAFT)
# v5: other channel -> excluded
v5 = mkvideo("other", "yt5", "sem bloco", channel=other, topic=t_other)
# v6: candidate whose LIVE snippet is already fully localized -> dropped in plan
v6 = mkvideo("lagged", "yt6", "db ficou pra trás")

now = utcnow()
session.add(VideoMetric(video_id=v1.id, channel_id=ch.id, views=999,
                        captured_at=now - timedelta(days=2)))
session.add(VideoMetric(video_id=v1.id, channel_id=ch.id, views=400,
                        captured_at=now))  # latest wins, not max
session.add(VideoMetric(video_id=v2.id, channel_id=ch.id, views=50, captured_at=now))
session.commit()

print("looks_en heuristic")
ok(bf.looks_en("Stop Chaining AI Agents — Do This Instead"), "EN title flagged")
ok(not bf.looks_en("Você comprou 32GB de RAM mas o gargalo era a VRAM"),
   "PT with diacritics not flagged")
ok(not bf.looks_en("Para de comprar GPU cara! Alugue cluster"),
   "PT stopwords without diacritics not flagged")
ok(not bf.looks_en(""), "empty text not flagged (conservative)")

print("candidate selection")
cands = bf.select_candidates(session, ch, top=20)
ids = [c["video"].id for c in cands]
ok(ids[0] == v1.id, "ranked by LATEST metric views (400), not historical max")
ok(v3.id not in ids, "video with CTA marker in DB excluded")
ok(v4.id not in ids, "non-published excluded")
ok(v5.id not in ids, "other channel excluded")
ok(v6.id in ids, "candidate without marker included")
ok(bf.select_candidates(session, ch, top=1)[0]["video"].id == v1.id,
   "--top caps the list at the highest-viewed")

print("merge_snippet")
live = {"title": "old", "description": "d", "categoryId": "28",
        "channelId": "UCCH2", "tags": ["a"]}
merged = bf.merge_snippet(live, {"title": "T" * 150, "description": "D",
                                 "tags": [str(i) for i in range(40)],
                                 "defaultLanguage": "pt-BR",
                                 "defaultAudioLanguage": "pt-BR"})
ok(merged["categoryId"] == "28" and merged["channelId"] == "UCCH2",
   "live snippet fields (categoryId etc.) preserved")
ok(len(merged["title"]) == 100 and len(merged["tags"]) == 30,
   "upload-body caps applied (title 100, tags 30)")
ok(live["title"] == "old", "merge does not mutate the live dict")

print("plan mode")
jr_before = len(session.exec(select(JobRun)).all())
vid_before = [(v.id, v.title, v.description, v.tags_json)
              for v in session.exec(select(Video)).all()]
live_snippets = {
    "yt1": {"title": "Você comprou 32GB de RAM", "description": "descrição sem bloco",
            "tags": ["ram"], "categoryId": "28"},
    "yt2": {"title": "The CrewAI Token Trap", "description": "plain english pitch",
            "categoryId": "28"},
    "yt6": {"title": "Já localizado", "description": f"tudo certo {MARKER}",
            "tags": [], "categoryId": "28", "defaultLanguage": "pt-BR",
            "defaultAudioLanguage": "pt-BR"},
}
svc = FakeService(live_snippets)
gen_calls = []


def fake_generate(subject, script, content_format="short", language=None):
    gen_calls.append((subject, language))
    return {"title": "Título novo em português", "description": "Descrição nova você vai gostar",
            "tags": ["#ia", "AI"]}


plan = bf.build_plan(session, ch, svc, top=20, language_code="pt-BR",
                     language_name="Brazilian Portuguese",
                     generate_fn=fake_generate, chapters_fn=lambda v: None)
by_id = {e["video_id"]: e for e in plan["entries"]}
ok(svc.videos().update_calls == [], "plan mode performs ZERO update calls")
ok(v6.id not in by_id, "live-already-localized video dropped from plan")
e1, e2 = by_id[v1.id], by_id[v2.id]
ok(e1["action"] == "retag_only" and "en-metadata" not in e1["reasons"],
   "PT-titled video -> retag_only")
ok(e1["proposed"]["title"] == "Você comprou 32GB de RAM",
   "retag_only keeps the proven live title")
ok(e1["proposed"]["description"].count(MARKER) == 1
   and "Inscreva-se" in e1["proposed"]["description"]
   and "PLabc" in e1["proposed"]["description"],
   "retag_only appends localized CTA block with playlist link exactly once")
ok(e1["proposed"]["defaultAudioLanguage"] == "pt-BR"
   and e1["proposed"]["defaultLanguage"] == "pt-BR", "language tags proposed")
ok(e2["action"] == "regenerate" and "en-metadata" in e2["reasons"],
   "EN-metadata video -> regenerate")
ok(gen_calls == [("crewai trap", "Brazilian Portuguese")],
   "generate called only for the regenerate entry, in the channel language")
ok(e2["proposed"]["title"] == "Título novo em português"
   and MARKER in e2["proposed"]["description"],
   "regenerate proposes new PT title + finalized description")
ok(all(not e["applied_at"] and not e["skip"] for e in plan["entries"]),
   "fresh plan entries unapplied and unskipped")
ok(len(session.exec(select(JobRun)).all()) == jr_before
   and [(v.id, v.title, v.description, v.tags_json)
        for v in session.exec(select(Video)).all()] == vid_before
   and not session.new and not session.dirty,
   "plan mode writes NOTHING to the DB")

print("plan file round-trip")
tmpdir = Path(tempfile.mkdtemp())
plan_path = tmpdir / "plan.json"
bf.write_plan(plan, plan_path)
ok(json.loads(plan_path.read_text())["channel"] == "rodrigo-recio",
   "plan file written and parseable")

print("apply mode")
plan2 = json.loads(plan_path.read_text())
n = bf.apply_plan(session, ch, svc, plan2, limit=10)
ok(n == 2, "both pending entries applied")
ok(len(svc.videos().update_calls) == 2, "exactly one update call per entry")
b1 = next(b for b in svc.videos().update_calls if b["id"] == "yt1")
ok(b1["snippet"]["categoryId"] == "28"
   and b1["snippet"]["defaultAudioLanguage"] == "pt-BR",
   "update body merges proposal onto the LIVE snippet")
session.expire_all()
v1db = session.get(Video, v1.id)
ok(v1db.description.count(MARKER) == 1 and v1db.title == "Você comprou 32GB de RAM",
   "DB row synced with what went live")
runs = session.exec(select(JobRun).where(JobRun.kind == "metadata")).all()
ok(len(runs) == 2 and all(r.status == "success"
                          and r.detail.startswith(bf.DETAIL_PREFIX)
                          and r.quota_cost == bf.QUOTA_VIDEO_UPDATE + 1
                          for r in runs),
   "each apply writes a prefixed JobRun with the quota cost")
ok(all(e["applied_at"] for e in plan2["entries"]), "entries stamped applied_at")
ok(bf.apply_plan(session, ch, svc, plan2, limit=10) == 0
   and len(svc.videos().update_calls) == 2,
   "re-apply of a fully-applied plan is a no-op")

print("drift + deletion safety")
v7 = mkvideo("drift case", "yt7", "ainda sem bloco")
pt_snip7 = {"title": "Título em português do v7", "description": "ainda sem bloco",
            "categoryId": "28"}
plan3 = bf.build_plan(session, ch, FakeService({"yt7": dict(pt_snip7)}),
                      top=20, language_code="pt-BR",
                      language_name="Brazilian Portuguese",
                      generate_fn=fake_generate, chapters_fn=lambda v: None)
ok([e["video_id"] for e in plan3["entries"]] == [v7.id],
   "plan contains only the video the live fetch found")
drift_svc = FakeService({"yt7": {"title": "Operador editou no YouTube",
                                 "description": "x", "categoryId": "28"}})
ok(bf.apply_plan(session, ch, drift_svc, plan3, limit=10) == 0
   and drift_svc.videos().update_calls == [],
   "live title drift -> entry skipped, no update call")
ok(plan3["entries"][0]["skip"] is True, "drifted entry marked skip for the operator")
v8 = mkvideo("gone case", "yt8", "também sem bloco")
gone_plan = bf.build_plan(session, ch,
                          FakeService({"yt8": {"title": "Outro título em português",
                                               "description": "também sem bloco",
                                               "categoryId": "28"}}),
                          top=20, language_code="pt-BR",
                          language_name="Brazilian Portuguese",
                          generate_fn=fake_generate, chapters_fn=lambda v: None)
gone_svc = FakeService({})
ok(bf.apply_plan(session, ch, gone_svc, gone_plan, limit=10) == 0
   and gone_svc.videos().update_calls == [],
   "video deleted live -> entry skipped, no update call")

print("crash-resume repair + limit clamp")
v11 = mkvideo("crash case", "yt11", "sem bloco por enquanto")
crash_store = {"yt11": {"title": "Vídeo onze em português", "categoryId": "28",
                        "description": "sem bloco por enquanto", "tags": ["x"]}}
crash_svc = FakeService(crash_store)
crash_plan = bf.build_plan(session, ch, crash_svc, top=20, language_code="pt-BR",
                           language_name="Brazilian Portuguese",
                           generate_fn=fake_generate, chapters_fn=lambda v: None)
ok([e["video_id"] for e in crash_plan["entries"]] == [v11.id],
   "crash fixture plan holds exactly the fresh video")
ok(bf.apply_plan(session, ch, crash_svc, crash_plan, limit=-1) == 0
   and crash_svc.videos().update_calls == [],
   "negative --limit clamps to zero applies, no negative-slice cap bypass")
# simulate a crash AFTER videos().update landed but BEFORE anything was recorded:
# the live snippet is localized, the entry is still unstamped
crash_store["yt11"] = {**crash_store["yt11"],
                       "description": crash_plan["entries"][0]["proposed"]["description"],
                       "defaultLanguage": "pt-BR", "defaultAudioLanguage": "pt-BR"}
cap_before_repair = bf.applied_today(session)
crash_path = tmpdir / "crash-plan.json"
n = bf.apply_plan(session, ch, crash_svc, crash_plan, limit=10, plan_path=crash_path)
ok(n == 0 and crash_svc.videos().update_calls == [],
   "already-localized live video repaired with no second update call")
ok(crash_plan["entries"][0]["applied_at"]
   and json.loads(crash_path.read_text())["entries"][0]["applied_at"],
   "repair stamps applied_at and persists it to disk mid-run")
ok(bf.applied_today(session) == cap_before_repair,
   "repair does not burn a daily-cap slot")
rr = session.exec(select(JobRun).where(
    JobRun.detail.like(f"{bf.DETAIL_PREFIX}-repair%"))).all()
ok(len(rr) == 1 and rr[0].quota_cost == 1 and rr[0].video_id == v11.id,
   "repair logs its own JobRun with list-only quota cost")
session.expire_all()
ok(MARKER in session.get(Video, v11.id).description,
   "repair syncs the DB row from the live state")

v12 = mkvideo("desc drift", "yt12", "corpo original sem bloco")
plan5 = bf.build_plan(session, ch,
                      FakeService({"yt12": {"title": "Título fixo em português",
                                            "description": "corpo original sem bloco",
                                            "categoryId": "28"}}),
                      top=20, language_code="pt-BR",
                      language_name="Brazilian Portuguese",
                      generate_fn=fake_generate, chapters_fn=lambda v: None)
plan5["entries"] = [e for e in plan5["entries"] if e["video_id"] == v12.id]
ddrift_svc = FakeService({"yt12": {"title": "Título fixo em português",
                                   "description": "operador melhorou a descrição",
                                   "categoryId": "28"}})
ok(bf.apply_plan(session, ch, ddrift_svc, plan5, limit=10) == 0
   and ddrift_svc.videos().update_calls == [],
   "description-only live drift also skipped (never clobbered)")

print("daily cap")
for r in session.exec(select(JobRun)).all():
    session.delete(r)
session.commit()
for i in range(bf.DAILY_CAP - 1):
    session.add(JobRun(kind="metadata", status="success", channel_id=ch.id,
                       detail=f"{bf.DETAIL_PREFIX}: seeded {i}"))
session.add(JobRun(kind="metadata", status="success", channel_id=ch.id,
                   detail="unrelated metadata run"))  # must NOT count
session.add(JobRun(kind="metadata", status="success", channel_id=ch.id,
                   detail=f"{bf.DETAIL_PREFIX}: run before the quota day",
                   created_at=utcnow() - timedelta(days=2)))  # must NOT count
session.commit()
ok(bf.applied_today(session) == bf.DAILY_CAP - 1,
   "cap counts prefixed success JobRuns from this quota day only")
v9 = mkvideo("cap case A", "yt9", "sem bloco nenhum")
v10 = mkvideo("cap case B", "yt10", "sem bloco também")
cap_svc = FakeService({
    "yt9": {"title": "Nono vídeo em português", "description": "sem bloco nenhum",
            "categoryId": "28"},
    "yt10": {"title": "Décimo vídeo em português", "description": "sem bloco também",
             "categoryId": "28"},
})
cap_plan = bf.build_plan(session, ch, cap_svc, top=20, language_code="pt-BR",
                         language_name="Brazilian Portuguese",
                         generate_fn=fake_generate, chapters_fn=lambda v: None)
ok(len([e for e in cap_plan["entries"] if not e["applied_at"]]) == 2,
   "fresh plan has two pending entries for the cap check")
n = bf.apply_plan(session, ch, cap_svc, cap_plan, limit=10)
ok(n == 1 and len(cap_svc.videos().update_calls) == 1,
   "cap leaves exactly one slot -> exactly one update applied")
n = bf.apply_plan(session, ch, cap_svc, cap_plan, limit=10)
ok(n == 0 and len(cap_svc.videos().update_calls) == 1,
   "cap reached -> further applies refused with zero calls")

session.close()
print(f"\nALL {_checks} CHECKS PASSED")
