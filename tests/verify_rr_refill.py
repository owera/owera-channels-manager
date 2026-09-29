"""Dependency-free regression checks for the 2026-09-28 RR refill fixes.

This project has no pytest; run directly:
    PYTHONPATH=. .venv/bin/python tests/verify_rr_refill.py

Background (Sun 2026-09-27): RR published 4/5. Two craft-PASS renders (#1357,
#1358) sat in `review` all day because the 09:00 growth-agent pass only decided
`stuck_review` (> 48h), and review age was measured from Video.updated_at, which a
finished render never bumps. Two render slots also went to subjects whose leading
number had been stripped ("B em Q4…", "camadas na GPU…").

Pins:
  1. review_ready: every rendered craft-PASS REVIEW item, no age filter, with a
     decide_by of 10:45 America/Fortaleza (the morning pass's decision list).
  2. review age measured from last_attempt_at (fallback updated_at/created_at).
  3. under_publish escalation: in-window, approved + published_today < budget
     while craft-ready review waits — silent outside the window, and silent
     when every waiting review item fails review_ready (no artifact / craft
     fail). `review > 0` alone must not page needs_operator.
  4. _auto_produce subject guard: stripped-number / bare-unit / currency subjects
     stay DRAFT (reason recorded once), the next valid draft takes the slot;
     `node_modules …` and normal PT/EN subjects pass.
  5. runway_low when approved + published_today < budget + runway_buffer (6).
  6. currency value anywhere in a TITLE is a publish-gate reject (R$50, $47,
     R$ 22, $1.5k); script-only amounts pass; plain numbers (7GB, 12B) pass.
  7. idea prompts no longer teach the billing-amount hook; currency ideas drop.
  9. Agent memory ideas: domain spread + at most 1 in 5 billing/price.
Uses in-memory SQLite — no network, no creds. Exits non-zero on first failure.
"""
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.config import settings
from app.models import (Channel, JobRun, OAuthStatus, Topic, Video, VideoStatus,
                        utcnow)
from app.services import craft, issues, render_loop, video_gen
from app.services.subject_guard import HOLD_PREFIX, subject_guard_reason

settings.bgm_pool_min = 0      # keep the filesystem-backed BGM bucket out of the way
_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


def fresh_session() -> Session:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def make_channel(s, **kw):
    ch = Channel(slug=kw.pop("slug", "rr"), name=kw.pop("name", "Rodrigo Recio"),
                 oauth_status=OAuthStatus.CONNECTED,
                 daily_render_budget=kw.pop("daily_render_budget", 5),
                 daily_publish_budget=kw.pop("daily_publish_budget", 5), **kw)
    s.add(ch)
    s.commit()
    s.refresh(ch)
    return ch


def make_topic(s, ch, **kw):
    t = Topic(channel_id=ch.id, name=kw.pop("name", "IA"), theme_prompt="x",
              weight=kw.pop("weight", 1), active=kw.pop("active", True), **kw)
    s.add(t)
    s.commit()
    s.refresh(t)
    return t


def make_video(s, ch, t, **kw):
    v = Video(channel_id=ch.id, topic_id=t.id,
              subject=kw.pop("subject", "Test subject"), **kw)
    s.add(v)
    s.commit()
    s.refresh(v)
    return v


PASS_CC = json.dumps({"craft_gate": {"result": "PASS", "checks": {"A": "PASS", "B": "PASS",
                                                                   "C": "PASS"},
                                     "reasons": []}})
FAIL_CC = json.dumps({"craft_gate": {"result": "FAIL", "reasons": ["[A] no object"]}})
GOOD_TITLE = "O 14B no NVMe rodou. No HDD, travou. · Local 60"


def ready_review(s, ch, t, **kw):
    """A rendered REVIEW video that clears the craft gate (like #1357)."""
    kw.setdefault("status", VideoStatus.REVIEW)
    kw.setdefault("title", GOOD_TITLE)
    kw.setdefault("script", "O 14B no NVMe rodou. No HDD, travou.")
    kw.setdefault("video_path", "/tmp/nonexistent-rr-refill.mp4")
    kw.setdefault("creation_config", PASS_CC)
    kw.setdefault("craft_review", craft.CRAFT_REVIEW_PENDING)
    return make_video(s, ch, t, **kw)


# ---------------------------------------------------------------------------
print("2. review age = last_attempt_at (fallback updated_at / created_at)")
s = fresh_session()
ch = make_channel(s)
t = make_topic(s, ch)
now = utcnow()
# #1357 shape: idea created 32h ago (updated_at never bumped), rendered 2h ago.
v_fresh = ready_review(s, ch, t, updated_at=now - timedelta(hours=60),
                       created_at=now - timedelta(hours=60),
                       last_attempt_at=now - timedelta(hours=2))
# Rendered 50h ago, updated_at recently touched by something unrelated.
v_old = ready_review(s, ch, t, updated_at=now - timedelta(hours=1),
                     last_attempt_at=now - timedelta(hours=50))
# Never rendered by this loop: falls back to updated_at.
v_legacy = make_video(s, ch, t, status=VideoStatus.REVIEW,
                      updated_at=now - timedelta(hours=49), last_attempt_at=None)
ok(issues.review_age_ref(v_fresh) == v_fresh.last_attempt_at,
   "review_age_ref prefers last_attempt_at over updated_at")
ok(issues.review_age_ref(v_legacy) == v_legacy.updated_at,
   "review_age_ref falls back to updated_at when last_attempt_at is null")
d = issues.detect(s)
stuck_ids = {e["id"] for e in d["stuck_review"]}
ok(v_fresh.id not in stuck_ids,
   "rendered 2h ago is NOT stuck even though updated_at is 60h old (the #1357 misread)")
ok(v_old.id in stuck_ids,
   "rendered 50h ago IS stuck even though updated_at is 1h old")
ok(v_legacy.id in stuck_ids, "legacy review with null last_attempt_at uses updated_at (49h)")
age = next(e["age_hours"] for e in d["review_ready"] if e["id"] == v_fresh.id)
ok(1.9 <= age <= 2.1, f"review_ready age_hours is measured from the render ({age}h)")


# ---------------------------------------------------------------------------
print("1. review_ready: every craft-PASS rendered review item, no age filter")
s = fresh_session()
ch = make_channel(s)
t = make_topic(s, ch)
now = utcnow()
a = ready_review(s, ch, t, last_attempt_at=now - timedelta(hours=3))      # fresh (<48h)
b = ready_review(s, ch, t, last_attempt_at=now - timedelta(hours=100))    # stale
c_nofile = ready_review(s, ch, t, video_path=None, last_attempt_at=now)
d_fail = ready_review(s, ch, t, craft_review=craft.CRAFT_REVIEW_FAIL,
                      error="Video Maker craft gate FAIL")
e_gate = ready_review(s, ch, t, creation_config=FAIL_CC)
f_cur = ready_review(s, ch, t, title="Ollama parado sai R$50 por mês · Local 70")
g_approved = ready_review(s, ch, t, status=VideoStatus.APPROVED)
dig = issues.detect(s)
rr = {e["id"]: e for e in dig["review_ready"]}
ok(a.id in rr, "a fresh (<48h) craft-PASS review item is in review_ready")
ok(b.id in rr, "a 100h craft-PASS review item is in review_ready too (no age filter)")
ok(set(rr) == {a.id, b.id},
   "excluded: no artifact, craft_review=fail, craft_gate FAIL, currency title, non-review")
ok(rr[a.id]["craft_gate"] == "PASS" and rr[a.id]["auto"] is True,
   "entry reports craft_gate=PASS and is agent-actionable (auto)")
ok("approve" in rr[a.id]["suggested_action"] and "10:45" in rr[a.id]["suggested_action"],
   "suggested_action says approve/reject by 10:45")
ok(a.status == VideoStatus.REVIEW, "the digest never approves anything (read-only)")
ok(dig["summary"]["clean"] is False, "pending review decisions make the digest not clean")

fixed_now = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)        # 09:00 Fortaleza
dl = issues.review_decide_by(fixed_now)
ok(dl == datetime(2026, 9, 28, 13, 45, tzinfo=timezone.utc),
   "decide_by = 10:45 America/Fortaleza (13:45Z)")
dig = issues.detect(s, now=fixed_now)
ok(all(e["decide_by"] == dl.isoformat() and e["overdue"] is False
       for e in dig["review_ready"]),
   "at 09:00 local every review_ready item carries decide_by 10:45 and is not overdue")
late = datetime(2026, 9, 28, 17, 0, tzinfo=timezone.utc)             # 14:00 Fortaleza
ok(all(e["overdue"] for e in issues.detect(s, now=late)["review_ready"]),
   "after 10:45 local the still-undecided items are flagged overdue")


# ---------------------------------------------------------------------------
print("3. under_publish: in window + review waiting, silent outside the window")


def rr_channel(s, **kw):
    ch = make_channel(s, publish_windows="11:00-22:00", publish_tz="America/Sao_Paulo", **kw)
    t = make_topic(s, ch)
    make_video(s, ch, t, status=VideoStatus.PUBLISHED)     # an operating channel
    return ch, t


in_win = datetime(2026, 9, 27, 19, 49, tzinfo=timezone.utc)   # 16:49 local (the watch)
out_win = datetime(2026, 9, 28, 3, 0, tzinfo=timezone.utc)    # 00:00 local
s = fresh_session()
ch, t = rr_channel(s)
ready_review(s, ch, t, last_attempt_at=utcnow() - timedelta(hours=10))
ready_review(s, ch, t, last_attempt_at=utcnow() - timedelta(hours=10))
d_in = issues.detect(s, now=in_win)
ok(len(d_in["under_publish"]) == 1 and d_in["under_publish"][0]["channel_id"] == ch.id,
   "approved 0 + published 0 < 5 with 2 review waiting at 16:49 local -> under_publish")
up = d_in["under_publish"][0]
ok(up["auto"] is False and up["review_waiting"] == 2 and up["review_ready"] == 2,
   "under_publish escalates (auto=False) and reports review waiting/ready counts")
ok(d_in["summary"]["needs_operator"] >= 1, "under_publish counts toward needs_operator")
ok(issues.detect(s, now=out_win)["under_publish"] == [],
   "the same state outside the 11:00-22:00 window does NOT fire")

s = fresh_session()
ch, t = rr_channel(s)
for _ in range(5):
    make_video(s, ch, t, status=VideoStatus.APPROVED)
ready_review(s, ch, t)
ok(issues.detect(s, now=in_win)["under_publish"] == [],
   "approved 5 covers the 5/day budget -> no under_publish even with review waiting")

s = fresh_session()
ch, t = rr_channel(s)
ok(issues.detect(s, now=in_win)["under_publish"] == [],
   "nothing in review -> no under_publish (publish_starved territory instead)")

s = fresh_session()
ch, t = rr_channel(s, paused=True)
ready_review(s, ch, t)
ok(issues.detect(s, now=in_win)["under_publish"] == [], "a paused channel never fires")

# Defect: under_publish keyed off review_waiting > 0. A REVIEW row with no
# artifact (or a craft fail) cannot be approved, but the page is auto=False
# and tells the growth agent to "decide review_ready now". That list is empty.
s = fresh_session()
ch, t = rr_channel(s)
make_video(s, ch, t, status=VideoStatus.REVIEW, video_path=None,
           title=GOOD_TITLE, script="O 14B no NVMe rodou. No HDD, travou.",
           last_attempt_at=in_win)
d_unready = issues.detect(s, now=in_win)
ok(d_unready["review_ready"] == [],
   "precondition: a review row with no artifact is not review_ready")
ok(d_unready["under_publish"] == [],
   "unready review does not page under_publish (review > 0 would)")
sig = issues.publish_signals(s, ch, in_win)
ok(sig["review_waiting"] == 1 and sig["review_ready"] == 0
   and sig["under_publish"] is False,
   "dashboard signal agrees: 1 waiting, 0 ready, not under_publish")
ready_review(s, ch, t, last_attempt_at=in_win)
d_mixed = issues.detect(s, now=in_win)
ok(len(d_mixed["under_publish"]) == 1
   and d_mixed["under_publish"][0]["review_waiting"] == 2
   and d_mixed["under_publish"][0]["review_ready"] == 1,
   "one craft-ready sibling still pages, and the count is the ready one")

s = fresh_session()
ch = make_channel(s)                      # no windows configured: loop publishes any time
t = make_topic(s, ch)
make_video(s, ch, t, status=VideoStatus.PUBLISHED)
ready_review(s, ch, t)
ok(len(issues.detect(s, now=out_win)["under_publish"]) == 1,
   "a channel without publish_windows is always in window (mirrors publish_loop._window_ok)")


# ---------------------------------------------------------------------------
print("5. runway_low: approved + published today < budget + runway_buffer (6)")
s = fresh_session()
ch, t = rr_channel(s)
for _ in range(5):
    make_video(s, ch, t, status=VideoStatus.APPROVED)
d = issues.detect(s, now=in_win)
ok(len(d["runway_low"]) == 1 and d["runway_low"][0]["runway_target"] == 6,
   "5 approved against a 5/day budget is below the 6 runway target")
ok(d["runway_low"][0]["runway"] == 5 and d["runway_low"][0]["auto"] is True,
   "runway_low reports runway=5 and is agent-actionable")
make_video(s, ch, t, status=VideoStatus.APPROVED)
ok(issues.detect(s, now=in_win)["runway_low"] == [], "6 approved meets the runway target")
ok(ch.daily_publish_budget == 5 and settings.runway_buffer == 1,
   "budget untouched (5) and the buffer is a settings default (1)")
settings.runway_buffer = 2
ok(len(issues.detect(s, now=in_win)["runway_low"]) == 1,
   "runway_buffer is tunable (buffer 2 -> target 7 -> 6 approved is low again)")
settings.runway_buffer = 1

s = fresh_session()
ch = make_channel(s)
t = make_topic(s, ch)
make_video(s, ch, t, status=VideoStatus.APPROVED)
ok(issues.detect(s)["runway_low"] == [],
   "a never-published channel signals nothing (not started, not starving)")

# dashboard carries the same signals per channel
from app.routers.queue import dashboard  # noqa: E402
s = fresh_session()
ch, t = rr_channel(s)
ready_review(s, ch, t)
dash = dashboard(session=s)
sig = dash[0]["publish_signals"]
ok(sig["runway_target"] == 6 and sig["review_waiting"] == 1 and sig["runway_low"] is True,
   "/api/dashboard exposes publish_signals (runway, target, review counts)")


# ---------------------------------------------------------------------------
print("4. subject guard: rules")
HELD = [
    "regenerações no mesmo bug saem 22 vezes, não é engenharia. · IA 208",
    "camadas na GPU, o resto no CPU, não é engenharia. · IA 209",
    "B em Q4 rodou na 24GB. O Q8, não. · Local 56",
    "GB rodou o 14B. O contexto 32k, não. · Local 50",
    "% das Empresas Já Mudaram",
    "R$ por mês no cloud, não é engenharia. · IA 1",
    "x mais rápido no CPU · Local 1",
    ", o resto no CPU · IA 1",
    "mil tokens por um sim não é engenharia. · IA 189",
    "O agente me cobrou $47 no terminal. · Claude Code 10",
    "node_modules no contexto sai R$50, não é engenharia. · IA 206",
]
ALLOWED = [
    "node_modules no contexto não é engenharia. · IA 206",
    "Jogar o node_modules no contexto não é engenharia. · IA 206",
    "Ollama parado prende 7GB de VRAM, não é engenharia. · IA 207",
    "Gemma 12B Q4 na 8GB rodou. O Q6, não. · Local 62",
    "27B em Q3 na 16GB rodou. O Q4, não. · Local 63",
    "O prompt processa no CPU por 8 segundos, não é engenharia. · IA 211",
    "Chat targeted staging. Prod wrote production. · Agent memory 35",
    "Chat set limit 3. Prod looped 30 calls. · Agent memory 36",
    "Chat picked annual. Prod billed monthly. · Agent memory 37",
    "Your eval passed because the agent saw the answer key. · Agent traps 2",
    "The preview passed. The live click timed out. · Shipping 5",
    "eGPU Vale a Pena Para Treinar Modelos?",
    "vLLM vs TensorRT: Qual Vence?",
    "npm install apagou o lockfile · Claude Code 12",
    "package.json travou o build · Claude Code 13",
    "\"Abri o terminal\" e o agente fez o resto · Claude Code 14",
]
for subj in HELD:
    r = subject_guard_reason(subj)
    ok(r is not None and r.startswith(HOLD_PREFIX), f"held: {subj[:50]!r}")
for subj in ALLOWED:
    ok(subject_guard_reason(subj) is None, f"allowed: {subj[:50]!r}")


print("4. _auto_produce skips invalid subjects and takes the next valid draft")
s = fresh_session()
ch = make_channel(s, daily_render_budget=3)
t = make_topic(s, ch)
bad1 = make_video(s, ch, t, status=VideoStatus.DRAFT, position=0,
                  subject="camadas na GPU, o resto no CPU, não é engenharia. · IA 209")
bad2 = make_video(s, ch, t, status=VideoStatus.DRAFT, position=1,
                  subject="B em Q4 rodou na 24GB. O Q8, não. · Local 56")
bad3 = make_video(s, ch, t, status=VideoStatus.DRAFT, position=2,
                  subject="O agente me cobrou $47 no terminal. · Claude Code 10")
ok1 = make_video(s, ch, t, status=VideoStatus.DRAFT, position=3,
                 subject="node_modules no contexto não é engenharia. · IA 206")
ok2 = make_video(s, ch, t, status=VideoStatus.DRAFT, position=4,
                 subject="Chat targeted staging. Prod wrote production. · Agent memory 35")
ok3 = make_video(s, ch, t, status=VideoStatus.DRAFT, position=5,
                 subject="Gemma 12B Q4 na 8GB rodou. O Q6, não. · Local 62")
spare = make_video(s, ch, t, status=VideoStatus.DRAFT, position=6,
                   subject="Ollama parado prende 7GB de VRAM, não é engenharia. · IA 207")
render_loop._auto_produce(s)
s.commit()
for v in (bad1, bad2, bad3, ok1, ok2, ok3, spare):
    s.refresh(v)
ok(all(v.status == VideoStatus.DRAFT for v in (bad1, bad2, bad3)),
   "stripped-number / bare-unit / currency subjects stay DRAFT (no render slot)")
ok(all(v.status == VideoStatus.QUEUED for v in (ok1, ok2, ok3)),
   "the next valid drafts (node_modules…, EN OS title, 12B…) take the 3 slots")
ok(spare.status == VideoStatus.DRAFT, "budget still caps the fill (3 of 3 used)")
ok(all((v.error or "").startswith(HOLD_PREFIX) for v in (bad1, bad2, bad3)),
   "the hold reason is recorded on video.error")
render_loop._auto_produce(s)      # a second tick must not duplicate the audit rows
s.commit()
held_runs = s.exec(select(JobRun).where(JobRun.kind == "produce",
                                        JobRun.status == "error")).all()
ok(len(held_runs) == 3 and all("held draft" in r.detail for r in held_runs),
   "one produce/error run per held draft, not one per tick")
dig = issues.detect(s)
ok({e["id"] for e in dig["subject_held"]} == {bad1.id, bad2.id, bad3.id},
   "the digest lists held drafts under subject_held")

# Fix a held subject and free capacity: it is queued and the stale reason cleared.
bad1.subject = "Metade das camadas na GPU, o resto no CPU, não é engenharia. · IA 209"
s.add(bad1)
ch.daily_render_budget = 4
s.add(ch)
s.commit()
render_loop._auto_produce(s)
s.commit()
s.refresh(bad1)
ok(bad1.status == VideoStatus.QUEUED and bad1.error is None,
   "a fixed subject is produced on the next tick and the hold reason is cleared")


# ---------------------------------------------------------------------------
print("6. currency value anywhere in the TITLE is blocked (title-only)")
for title in ("Ollama parado sai R$50 por mês · IA 1",
              "Regenerar o bug sai R$ 22 · IA 208",
              "O agente me cobrou $47 no terminal. · Claude Code 10",
              "The agent spent $1.5k overnight · Agent traps 4",
              "US$9 por dia no cloud · IA 3",
              "chat declined. PROD ADDED $29. · Agent memory 29"):
    ok(craft.nonsense_title_reason(title) == craft.CURRENCY_TITLE_REASON,
       f"blocked: {title!r}")
ok(craft.nonsense_title_reason("billed $79 on a cancelled run · Copilot Credits 80")
   == craft.NONSENSE_TITLE_REASON,
   "leading 'billed $79' is still blocked (original pattern keeps its reason)")
for title in ("Ollama parado prende 7GB de VRAM, não é engenharia. · IA 207",
              "O prompt processa no CPU por 8 segundos, não é engenharia. · IA 211",
              "Gemma 12B Q4 na 8GB rodou. O Q6, não. · Local 62",
              "Chat picked annual. Prod billed monthly. · Agent memory 37",
              "Copilot re-bills the whole receipt · Copilot Credits 1"):
    ok(craft.nonsense_title_reason(title) is None, f"passes: {title!r}")
ok(craft.publish_craft_block_reason(
       title="Copilot re-bills the whole receipt · Copilot Credits 1",
       script="Copilot re-billed the whole receipt: $79 for a run you cancelled.",
       creation_config=None, check_audio=False) is None,
   "a currency value in the script/VO only still passes the publish gate")
ok(craft.publish_craft_block_reason(
       title="Copilot billed $79 for a cancelled run · Copilot Credits 1",
       script="x", creation_config=None, check_audio=False)
   == craft.CURRENCY_TITLE_REASON,
   "publish gate (approve / sweep / upload) rejects a mid-title $N")
# Credits/IA lock stays satisfiable via the concrete-noun path.
ok(craft.credits_ia_title_ok("Copilot re-bills the whole receipt · Copilot Credits 1"),
   "Credits lock passes on the noun path without any $N")
ok(craft.title_gate_reason("O terminal travou no meio do deploy. · IA 212", "short") is None,
   "IA lock passes on the noun path without any $N")
# $N numeral lock is a no-op when there is no $N.
ok(craft.preserves_dollar_numerals("Copilot re-bills the whole receipt", "RE-BILLS THE RECEIPT"),
   "numeral lock does not apply when the spoken claim has no $N")
ok(craft.opening_object("Copilot re-bills the whole receipt")["amount"] == "",
   "opening_object without a $N has an empty amount (nothing to lock)")
ok(craft.compress_claim("Copilot re-bills the whole receipt") ==
   "Copilot re-bills the whole receipt", "compress_claim unaffected without $N")
# The digest flags approved currency titles before the upload rejects them.
s = fresh_session()
ch = make_channel(s)
t = make_topic(s, ch, name="Agent memory")
v1360 = make_video(s, ch, t, status=VideoStatus.APPROVED, craft_review="pass",
                   title="Chat declined the warranty. Prod added $29. · Agent memory 29")
tpb = issues.detect(s)["title_pattern_blocked"]
ok(any(e["id"] == v1360.id and "retitle" in e["suggested_action"] for e in tpb),
   "an approved currency title is surfaced in title_pattern_blocked with a retitle hint")


# ---------------------------------------------------------------------------
print("7. idea prompts no longer teach the billing-amount hook")
_calls: list[str] = []


def _stub(text):
    def _complete(prompt, system=None, max_tokens=None, timeout=None):
        _calls.append(prompt)
        return text
    return _complete


CURRENCY_HOOK = re.compile(
    r"(?:r\$|us\$|\$)\s*(?:\d|N\b)"                                  # $79, R$N, $N
    r"|\b(?:charged|billed|cobrou|added|sai|costs?)\s+(?:r\$|us\$|\$)",  # verb + currency
    re.IGNORECASE)


def run_ideas(text, topic="Topic", theme=None, existing=(), fmt="short"):
    _calls.clear()
    with patch.object(video_gen, "complete", side_effect=_stub(text)):
        out = video_gen.generate_ideas(topic, theme, list(existing), n=10, content_format=fmt)
    return out, _calls[0]


for fmt in ("short", "long"):
    _, prompt = run_ideas("X", fmt=fmt)
    ok(not CURRENCY_HOOK.search(prompt), f"{fmt} idea prompt has no currency-hook pattern")
    ok(video_gen.NO_BILLING_AMOUNT_HOOK in prompt,
       f"{fmt} idea prompt carries the no-billing-amount-hook rule")
    ok("dollar amounts" not in prompt, f"{fmt} idea prompt no longer suggests dollar amounts")
ok(not CURRENCY_HOOK.search(craft.CRAFT_RULES_SHORT),
   "CRAFT_RULES_SHORT (idea/script addendum) has no currency-hook example")
ok("concrete noun" in video_gen.NO_BILLING_AMOUNT_HOOK,
   "replacement rule: useful spoken claim + concrete noun")
out, _ = run_ideas("O agente me cobrou $47 no terminal. · Claude Code 10\n"
                   "Chamar o modelo a cada push sai R$60 · IA 210\n"
                   "O agente force-pushou a main. · Claude Code 11\n"
                   "Chat picked annual. Prod billed monthly. · CrewAI 20\n"
                   "The coupon field ate the invoice note · Shipping 6")
ok(out == ["O agente force-pushou a main. · Claude Code 11",
           "Chat picked annual. Prod billed monthly. · CrewAI 20",
           "The coupon field ate the invoice note · Shipping 6"],
   "ideas with a money value drop; billing WORDS without an amount are kept")


# ---------------------------------------------------------------------------
print("9. Agent memory: domain spread + at most 1 in 5 billing/price")
AM_THEME = "Format: {chat vs prod}. · Agent memory {nn}. Example: Chat remembered. Prod forgot."
_, prompt = run_ideas("X", topic="Agent memory and state in production", theme=AM_THEME)
ok(video_gen.AGENT_MEMORY_SPREAD in prompt, "Agent memory prompt carries the spread rule")
ok("'Chat did X. Prod did Y.'" in prompt, "template 'Chat did X. Prod did Y.' is kept exactly")
for dom in ("calendar/scheduling", "shipping address", "permission/access",
            "language/locale", "user preference", "order status", "timezone",
            "notifications"):
    ok(dom in prompt, f"domain listed: {dom}")
ok("At most 1 in 5" in prompt, "prompt states the 1-in-5 billing/price cap")
_, other = run_ideas("X", topic="CrewAI", theme="· CrewAI {nn}")
ok(video_gen.AGENT_MEMORY_SPREAD not in other, "other series do not get the Agent memory rule")

for txt, exp in (("Chat priced it in EUR. Prod charged USD.", True),
                 ("Chat applied SAVE20. Prod charged full price.", True),
                 ("Chat picked annual. Prod billed monthly.", True),
                 ("Chat froze card 4412. Prod charged the card.", True),
                 ("Chat declined the warranty. Prod added it anyway.", True),
                 ("Chat booked 3pm. Prod booked 3am.", False),
                 ("Chat set pt-BR. Prod replied in English.", False),
                 ("Chat revoked access. Prod kept the token.", False),
                 ("Chat muted alerts. Prod paged at 2am.", False)):
    ok(video_gen.is_billing_themed(txt) is exp, f"billing classifier: {txt!r} -> {exp}")

BATCH = ("Chat booked 3pm. Prod booked 3am. · Agent memory 39\n"
         "Chat applied SAVE20. Prod charged full price again. · Agent memory 40\n"
         "Chat picked weekly. Prod billed yearly. · Agent memory 41\n"
         "Chat set pt-BR. Prod replied in English. · Agent memory 42\n"
         "Chat revoked access. Prod kept the token. · Agent memory 43\n"
         "Chat muted alerts. Prod paged at 2am. · Agent memory 44\n"
         "Chat saved the new address. Prod shipped to the old one. · Agent memory 45\n"
         "Chat marked it refunded. Prod kept the invoice open. · Agent memory 46")
out, _ = run_ideas(BATCH, topic="Agent memory and state in production", theme=AM_THEME,
                   existing=["Chat routed to Maya. Prod paged Lee. · Agent memory 32"])
billing = [video_gen.is_billing_themed(x) for x in out]
ok(sum(billing[:5]) <= 1 and all(sum(billing[i:i + 5]) <= 1 for i in range(len(billing))),
   "new batch keeps at most 1 billing idea in any 5 consecutive")
ok("Chat picked weekly. Prod billed yearly. · Agent memory 41" not in out,
   "a second billing idea inside the window is dropped")
recent_billing = ["Chat priced it in EUR. Prod charged USD. · Agent memory 30",
                  "Chat applied SAVE20. Prod charged full price. · Agent memory 33",
                  "Chat picked annual. Prod billed monthly. · Agent memory 37",
                  "Chat appended line 3. Prod overwrote the file. · Agent memory 38"]
out2, _ = run_ideas(BATCH, topic="Agent memory and state in production", theme=AM_THEME,
                    existing=recent_billing)
ok(not any(video_gen.is_billing_themed(x) for x in out2[:2]),
   "with billing in the last 5 existing subjects, the batch opens non-billing "
   "(existing + new window counted)")
out3, _ = run_ideas(BATCH, topic="CrewAI", theme="· CrewAI {nn}")
ok(len(out3) == 8, "the billing cap only applies to the Agent memory series")


print()
print(f"ALL {_checks} CHECKS PASSED")
