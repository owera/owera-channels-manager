"""S2 — guided onboarding wizard to the first draft (PO CM NEXT-CUT S2 + condition ii).

Run: PYTHONPATH=. uv run python tests/verify_onboarding_wizard.py

In-memory SQLite + TestClient. No network, no YouTube, no Stripe call, no real
LLM: ``llm._complete_api`` is mocked and ``llm._complete_cli`` raises. Covers:
  - wizard flow and each step's API, strictly ordered (409 out of order)
  - terms / privacy / Google Limited Use consent persistence (first acceptance
    kept, sha of the exact LegalOps text)
  - E2E: paid (S1 paid_at via the real checkout handler) + wizard complete +
    MANAGER_LLM_EXTERNAL_RELEASE ON → require() allowed, a draft queued with top
    render priority
  - without the AI-data ack → refused, "AI data" step banner (S8), cleared on ack
  - workspace isolation of every wizard route (S4: foreign channel = 404)
  - first-draft priority: order only; render_concurrency, budgets and the
    internal (OS/RR-style) channels' rows untouched; no preemption; lapses once
    S1 stamps first_draft_at
  - per instance: presets carry no Owera topics (CMO copy, EN + PT, same ids);
    autofill flag never flipped
  - PO CM 14/11: buyer week-1 defaults 2/2; 1 idea from each of the 3 topics
"""
from __future__ import annotations

import contextlib
import inspect
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import app.db as appdb
import app.main as main
from app.config import settings
# CTO 14/11: the external release needs the legal document shas pinned for the
# accepted locale (else terms_doc_unpinned; pinned in tests/verify_terms_consent.py).
from _terms_fixture import pin_terms_record  # noqa: E402
PINNED_DOC_SHA = "0" * 64
for _k in ("terms_doc_sha_en", "terms_doc_sha_pt", "privacy_doc_sha_en", "privacy_doc_sha_pt"):
    setattr(settings, _k, PINNED_DOC_SHA)
from app.db import get_session
from app.models import (Channel, OAuthStatus, Settings, Topic, Video, VideoStatus, Workspace)
from app.services import (activation, google_limited_use, llm, llm_guard, onboarding_wizard,
                          render_loop, stripe_provision, topic_presets)

ROOT = Path(__file__).resolve().parent.parent
_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


# ---- fixtures -----------------------------------------------------------------
NOW = datetime(2026, 11, 14, tzinfo=timezone.utc)
eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
SQLModel.metadata.create_all(eng)
appdb_patch = patch.object(appdb, "engine", eng)   # llm_guard reads rows via app.db.engine
appdb_patch.start()

with Session(eng) as s:
    s.add(Workspace(id=1, slug="owera", name="Internal"))
    s.add(Settings(id=1, render_concurrency=1))
    s.commit()
    # Two internal channels standing in for the OS/RR mix: queued rows first in
    # line (lower ids/positions) before the buyer exists.
    ia = Channel(workspace_id=1, slug="int-a", name="Internal A", oauth_status=OAuthStatus.CONNECTED,
                 daily_render_budget=1, daily_publish_budget=1)
    ib = Channel(workspace_id=1, slug="int-b", name="Internal B", oauth_status=OAuthStatus.CONNECTED,
                 daily_render_budget=4, daily_publish_budget=4)
    s.add_all([ia, ib]); s.commit(); s.refresh(ia); s.refresh(ib)
    ta = Topic(channel_id=ia.id, name="Internal topic A", weight=1)
    tb = Topic(channel_id=ib.id, name="Internal topic B", weight=1)
    s.add_all([ta, tb]); s.commit(); s.refresh(ta); s.refresh(tb)
    for i in range(2):
        s.add(Video(channel_id=ia.id, topic_id=ta.id, subject=f"Internal A queued {i}",
                    status=VideoStatus.QUEUED, position=i))
        s.add(Video(channel_id=ib.id, topic_id=tb.id, subject=f"Internal B queued {i}",
                    status=VideoStatus.QUEUED, position=i))
    s.commit()
    IA, IB = ia.id, ib.id


def provision(email, cs):
    """S1: the real checkout handler writes paid_at (no Stripe call)."""
    with Session(eng) as s:
        wid = stripe_provision.handle_checkout_completed(
            s, {"id": cs, "mode": "subscription", "customer": f"cus_{cs}",
                "subscription": f"sub_{cs}", "customer_details": {"email": email}},
            event_id=f"evt_{cs}", event_created=int(NOW.timestamp()))
        s.commit()
        return wid


WA = provision("buyer-a@example.com", "cs_a")
WB = provision("buyer-b@example.com", "cs_b")
WC = provision("buyer-c@example.com", "cs_c")
with Session(eng) as s:
    SLUG = {w: s.get(Workspace, w).slug for w in (WA, WB, WC)}
    ok(all(s.get(Workspace, w).paid_at is not None for w in (WA, WB, WC)),
       "S1: checkout handler set paid_at on the three buyer workspaces")


def _override_session():
    with Session(eng) as session:
        yield session


settings.multi_workspace = True
settings.allow_workspace_header = True
settings.app_password = ""
main.app.dependency_overrides[get_session] = _override_session
client = TestClient(main.app)


def H(ws):
    return {"X-Workspace-Slug": "owera" if ws == 1 else SLUG[ws]}


def get_state(ws):
    r = client.get("/api/onboarding/wizard", headers=H(ws))
    assert r.status_code == 200, r.text
    return r.json()


def post(ws, path, body):
    return client.post(f"/api/onboarding/wizard/{path}", headers=H(ws), json=body)


def row(ws):
    with Session(eng) as s:
        return s.get(Workspace, ws)


ON = dict(llm_external_release=True, llm_backend="api", xai_data_retention_ack="30d_no_training",
          llm_internal_workspace_ids="1")


@contextlib.contextmanager
def cfg(**kw):
    with contextlib.ExitStack() as st:
        for k, v in kw.items():
            st.enter_context(patch.object(settings, k, v))
        yield


IDEAS = ("Why Your Sourdough Starter Keeps Failing\n"
         "3 Bread Mistakes Every Beginner Makes\n"
         "Stop Kneading for Ten Minutes and Try This Instead\n")
llm_calls: list[str] = []


def _fake_api(prompt, system, **kw):
    llm_calls.append(kw.get("call_type") or "")
    return IDEAS


def _no_cli(*a, **k):
    raise AssertionError("CLI LLM must not run in this test")


llm_patches = [patch.object(llm, "_complete_api", _fake_api), patch.object(llm, "_complete_cli", _no_cli)]
for p in llm_patches:
    p.start()

PRESET = topic_presets.NICHE_PRESETS[0]
SHA_EN = google_limited_use.text_sha(google_limited_use.TEXT_EN)
from app.services import terms_consent  # noqa: E402
CONSENTS = {"terms": True, "terms_text_sha": terms_consent.SHA_EN, "terms_locale": "en", "ai_data": True,
            "ai_data_text_sha": SHA_EN, "ai_data_locale": "en"}


def walk_to(ws, step, *, connect=True):
    """Drive the wizard of ``ws`` up to (not including) ``step``."""
    st = get_state(ws)
    order = onboarding_wizard.STEP_IDS
    for sid in order[:order.index(step)]:
        if sid == "channel" and not st["steps"][0]["done"]:
            r = post(ws, "channel", {"name": f"Channel {ws}"})
            assert r.status_code == 200, r.text
        elif sid == "youtube" and connect:
            with Session(eng) as s:   # stubbed Google: the OAuth callback's end state
                ch = s.exec(select(Channel).where(Channel.workspace_id == ws)).first()
                ch.oauth_status = OAuthStatus.CONNECTED
                ch.yt_channel_id = f"UC{ws}"
                s.add(ch); s.commit()
        elif sid == "topics":
            r = post(ws, "topics", {"channel_id": get_state(ws)["channel"]["id"], "niche": PRESET["niche"],
                                    "preset_id": PRESET["id"], "topics": PRESET["topics"]})
            assert r.status_code == 200, r.text
        elif sid == "budgets":
            r = post(ws, "budgets", {"channel_id": get_state(ws)["channel"]["id"]})
            assert r.status_code == 200, r.text
        elif sid == "consents":
            r = post(ws, "consents", CONSENTS)
            assert r.status_code == 200, r.text
        st = get_state(ws)
    return st


# ---- 1. wizard flow, step by step -----------------------------------------------
print("wizard: entry (0 channels) and legacy workspaces")
st = get_state(WA)
ok(st["active"] is True and st["current_step"] == "channel", "buyer A, 0 channels → wizard active at 'channel'")
ok([x["id"] for x in st["steps"]] == ["channel", "youtube", "topics", "budgets", "consents",
                                      "ideas", "first_draft"], "seven steps in the S2 order")
ok(get_state(1)["active"] is False, "workspace with channels and no wizard_started_at → no wizard")
ok(st["presets"]["copy_status"] == "CMO copy 2026-11-14" and len(st["presets"]["niches"]) == 5
   and all(len(n["topics"]) == 3 for n in st["presets"]["niches"]), "presets payload: 5 niches × 3 topics, CMO copy")
ok([n["id"] for n in st["presets"]["niches"]] == ["cooking", "fitness", "personal_finance", "tech_tips",
                                                   "local_business"] and st["presets"]["locale"] == "en",
   "CMO ids, EN by default")
pt = client.get("/api/onboarding/wizard?locale=pt-BR", headers=H(WA)).json()["presets"]
ok(pt["locale"] == "pt" and [n["id"] for n in pt["niches"]] == [n["id"] for n in st["presets"]["niches"]]
   and pt["niches"][0]["label"] == "Culinária", "?locale=pt-BR → PT presets, same ids")
pt_h = client.get("/api/onboarding/wizard", headers={**H(WA), "Accept-Language": "pt-BR,pt;q=0.9"}).json()
ok(pt_h["presets"]["locale"] == "pt", "Accept-Language pt-BR → PT presets")
ok(topic_presets.get("cooking", "pt")["niche"].startswith("Receitas") and topic_presets.get("cooking")["label"] == "Cooking",
   "get(id, locale) defaults to EN")
ok(st["default_budgets"] == {"daily_render_budget": 2, "daily_publish_budget": 2},
   "PO CM: buyer week-1 default budgets 2/2")
ok(st["ai_data_line_version"] == "845c22005853", "L6 line version exposed for the ack")

print("wizard: order is enforced server-side")
r = post(WA, "consents", CONSENTS)
ok(r.status_code == 409 and r.json()["detail"]["missing"] == "channel", "consents before channel → 409 missing channel")

print("step channel")
r = post(WA, "channel", {"name": "  "})
ok(r.status_code == 400, "blank channel name → 400")
r = post(WA, "channel", {"name": "Bread at Home"})
ok(r.status_code == 200 and r.json()["steps"][0]["done"], "POST channel creates the workspace's first channel")
st = r.json()
CHA = st["channel"]["id"]
ok(st["channel"]["daily_render_budget"] == 2 and st["channel"]["daily_publish_budget"] == 2
   and st["current_step"] == "youtube", "buyer defaults 2/2 applied, next = youtube")
ok(row(WA).wizard_started_at is not None, "wizard_started_at stamped")
r = post(WA, "channel", {"name": "Another"})
ok(r.status_code == 200 and r.json()["channel"]["id"] == CHA, "re-submit is idempotent (no second channel)")
with Session(eng) as s:
    ok(len(s.exec(select(Channel).where(Channel.workspace_id == WA)).all()) == 1, "still one channel")
r = post(1, "channel", {"name": "x"})
ok(r.status_code == 409 and r.json()["detail"]["code"] == "workspace_has_channels",
   "legacy workspace with channels can't start the wizard")

print("step youtube (existing OAuth routes; observed via channel.oauth_status)")
r = post(WA, "topics", {"channel_id": CHA, "niche": "n", "topics": PRESET["topics"]})
ok(r.status_code == 409 and r.json()["detail"]["missing"] == "youtube", "topics before Connect YouTube → 409")
walk_to(WA, "topics")
ok(get_state(WA)["current_step"] == "topics", "connected → next = topics")

print("step topics (niche + 3 topics; preset or free text)")
r = post(WA, "topics", {"channel_id": CHA, "niche": PRESET["niche"], "topics": PRESET["topics"][:2]})
ok(r.status_code == 400, "two topics → 400 (exactly 3)")
r = post(WA, "topics", {"channel_id": CHA, "niche": "", "topics": PRESET["topics"]})
ok(r.status_code == 400, "empty niche → 400")
dup = [{"name": "Same"}, {"name": "same"}, {"name": "Other"}]
r = post(WA, "topics", {"channel_id": CHA, "niche": "n", "topics": dup})
ok(r.status_code == 400, "duplicate topic names → 400")
free = [{"name": "Starter care"}, {"name": "Shaping loaves"}, {"name": "Oven tricks", "theme_prompt": "home ovens"}]
r = post(WA, "topics", {"channel_id": CHA, "niche": "Home baking for beginners", "topics": free})
ok(r.status_code == 200 and [t["name"] for t in r.json()["topics"]] == ["Starter care", "Shaping loaves", "Oven tricks"],
   "free-text topics accepted, in order")
r = post(WA, "topics", {"channel_id": CHA, "niche": PRESET["niche"], "preset_id": PRESET["id"],
                        "topics": PRESET["topics"]})
st = r.json()
ok(r.status_code == 200 and [t["name"] for t in st["topics"]] == [t["name"] for t in PRESET["topics"]]
   and st["channel"]["niche"] == PRESET["niche"], "re-submit before ideas replaces topics; niche saved")
with Session(eng) as s:
    ok(len(s.exec(select(Topic).where(Topic.channel_id == CHA)).all()) == 3, "exactly 3 topics on the channel")
ok(st["current_step"] == "budgets", "next = budgets")

print("step budgets")
r = post(WA, "budgets", {"channel_id": CHA, "daily_render_budget": 0})
ok(r.status_code == 400, "budget 0 → 400 (would stall the first draft)")
r = post(WA, "budgets", {"channel_id": CHA, "daily_render_budget": 3})
st = r.json()
ok(r.status_code == 200 and st["channel"]["daily_render_budget"] == 3
   and st["channel"]["daily_publish_budget"] == 2 and st["current_step"] == "consents",
   "budgets saved (missing one = default), next = consents")

print("step consents: terms + privacy + Google Limited Use consent")
r = post(WA, "first-draft", {"channel_id": CHA})
ok(r.status_code == 409 and r.json()["detail"]["missing"] == "consents", "first-draft before consents → 409")
r = post(WA, "consents", dict(CONSENTS, ai_data=False))
ok(r.status_code == 400 and row(WA).terms_accepted_at is None, "ai_data unchecked → 400, nothing written")
r = post(WA, "consents", dict(CONSENTS, ai_data_text_sha="deadbeef"))
ok(r.status_code == 409 and r.json()["detail"]["code"] == "ai_data_text_changed" and row(WA).ai_data_ack_at is None
   and row(WA).terms_accepted_at is None, "unknown text sha → 409, nothing written")
r = post(WA, "consents", dict(CONSENTS, ai_data_text_sha=onboarding_wizard.AI_DATA_LINE_SHA))
ok(r.status_code == 409 and row(WA).ai_data_ack_at is None, "the L6 sha is not accepted as the consent sha")
r = post(WA, "consents", dict(CONSENTS, terms=False))
ok(r.status_code == 400 and row(WA).terms_accepted_at is None, "Terms box unchecked → 400, nothing written")
r = post(WA, "consents", dict(CONSENTS, terms_text_sha="0" * 64))
ok(r.status_code == 409 and r.json()["detail"]["code"] == "terms_text_changed" and row(WA).terms_accepted_at is None,
   "unknown Terms text sha → 409 terms_text_changed, nothing written")
r = post(WA, "consents", dict(CONSENTS, terms_text_sha=terms_consent.SHA_PT, terms_locale="pt"))
ok(r.status_code == 409 and r.json()["detail"]["code"] == "terms_text_changed" and row(WA).terms_accepted_at is None,
   "PT gate closed (MANAGER_TERMS_PT_LIVE=false): PT Terms sha refused")
r = post(WA, "consents", CONSENTS)
w = row(WA)
ok(r.status_code == 200 and w.terms_accepted_at and w.privacy_accepted_at and w.ai_data_ack_at,
   "terms_accepted_at / privacy_accepted_at / ai_data_ack_at persisted")
ok(w.terms_accepted_version == terms_consent.SHA_EN and w.terms_accepted_locale == "en",
   "terms_accepted_version = sha of the exact single-box sentence shown; locale en")
ok(w.ai_data_ack_version == SHA_EN and w.ai_data_ack_locale == "en",
   "ai_data_ack_version = sha of the exact EN text shown; locale en")
c = r.json()["consents"]
ok(c["terms_accepted_at"].endswith("Z") and c["ai_data_ack_at"].endswith("Z"), "state shows ISO UTC timestamps")
T0 = (w.terms_accepted_at, w.privacy_accepted_at, w.ai_data_ack_at)
post(WA, "consents", CONSENTS)
w = row(WA)
ok((w.terms_accepted_at, w.privacy_accepted_at, w.ai_data_ack_at) == T0, "re-submit keeps the first acceptance")
ok(r.json()["current_step"] == "ideas", "next = ideas")

print("CTO / LegalOps gates: API flag, configured sha, PT acceptance not consent")
st = get_state(WA)
ok(st["terms"] == {"pt_live": False, "accepted_shas": {"en": terms_consent.SHA_EN}},
   "GET wizard → terms.pt_live false + the EN sha the server accepts (the SPA reads the gate here)")
with patch.object(settings, "terms_pt_live", True):
    ok(get_state(WA)["terms"]["pt_live"] is True, "MANAGER_TERMS_PT_LIVE=true reaches the SPA flag")
# (a) a row consented under the PT text while the gate is closed: not consent, asked again in EN.
with Session(eng) as s_:
    w_ = s_.get(Workspace, WA)
    SAVED = (w_.terms_accepted_version, w_.terms_accepted_locale)
    w_.terms_accepted_version, w_.terms_accepted_locale = terms_consent.SHA_PT, "pt"
    s_.add(w_); s_.commit()
st = get_state(WA)
ok(st["current_step"] == "consents" and not {x["id"]: x["done"] for x in st["steps"]}["consents"],
   "stored PT acceptance with the gate closed → consents step not done (asked again)")
ok(onboarding_wizard.consents_done(row(WA)) is False, "consents_done False")
with patch.object(settings, "terms_pt_live", True):
    ok(onboarding_wizard.consents_done(row(WA)) is True, "gate open → the same PT acceptance counts")
with patch.object(settings, "terms_version_sha_en", "c" * 64):
    r = post(WA, "consents", CONSENTS)
    ok(r.status_code == 409 and r.json()["detail"]["code"] == "terms_text_changed"
       and row(WA).terms_accepted_locale == "pt",
       "(b) configured EN sha moved → the code-default sha is refused, nothing written")
# A pt-BR browser that switched the assistant to English: the SPA shows and sends the EN
# sentence (?locale=en / no PT box); even with a PT Accept-Language the record is locale en.
DOCS = dict(terms_doc_sha_en="d" * 64, privacy_doc_sha_en="e" * 64, terms_doc_sha_pt="f" * 64)
with contextlib.ExitStack() as _st:
    for k_, v_ in DOCS.items():
        _st.enter_context(patch.object(settings, k_, v_))
    r = client.post("/api/onboarding/wizard/consents", headers={**H(WA), "Accept-Language": "pt-BR,pt;q=0.9"},
                    json=CONSENTS)
w = row(WA)
ok(r.status_code == 200 and w.terms_accepted_version == terms_consent.SHA_EN and w.terms_accepted_locale == "en"
   and w.terms_accepted_at == T0[0], "re-accept in EN replaces the PT record (first timestamp kept); locale en")

print("consent record (TermsConsentEvent)")
from app.models import TermsConsentEvent  # noqa: E402
with Session(eng) as s_:
    evs = list(s_.exec(select(TermsConsentEvent).where(TermsConsentEvent.workspace_id == WA)
                       .order_by(TermsConsentEvent.id)).all())
ok(len(evs) == 2, f"one record per stored acceptance (first accept + EN re-accept; re-submits add none) ({len(evs)})")
e0, e1 = evs
ok(e0.sentence_sha == terms_consent.SHA_EN and e0.locale == "en" and e0.workspace_id == WA
   and e0.terms_url == "https://owera.com/terms/" and e0.privacy_url == "https://owera.com/privacy/"
   and e0.terms_doc_sha == PINNED_DOC_SHA and e0.privacy_doc_sha == PINNED_DOC_SHA and e0.created_at is not None,
   "record: sentence sha, EN URLs, the configured EN doc shas, locale, time, workspace")
ok(e0.user_id == w.ai_data_ack_user, f"record: user id = who ticked (same identity as the AI-data ack: {e0.user_id!r})")
ok(e1.locale == "en" and e1.terms_doc_sha == "d" * 64 and e1.privacy_doc_sha == "e" * 64,
   "record: the locale's configured document shas (MANAGER_TERMS_DOC_SHA_EN / MANAGER_PRIVACY_DOC_SHA_EN), not PT's")
ok(r.json()["current_step"] == "ideas", "back to ideas")

# ---- 2. E2E: paid + wizard + flag ON → allowed, first draft at top priority -----
print("E2E: paid (S1) + wizard complete + MANAGER_LLM_EXTERNAL_RELEASE ON (LLM mocked)")
autogen_before = None
with Session(eng) as s:
    autogen_before = s.get(Settings, 1).topic_autogen_enabled
with cfg(**ON):
    ok(llm_guard.require("ideas", workspace_id=WA, fields=("topic",)) == WA,
       "require('ideas') allowed for buyer A")
    r = post(WA, "first-draft", {"channel_id": CHA})
    ok(r.status_code == 200, f"POST first-draft → 200 ({r.status_code} {r.text[:160]})")
    st = r.json()
    ok(llm_calls and all(c in ("ideas",) or c.startswith("ideas") for c in llm_calls),
       f"ideas generated through the mocked API backend only ({llm_calls})")
    fd = st["first_draft"]
    ok(fd and fd["status"] == "queued" and fd["priority"] is True, "a draft is queued with top priority")
    ok(st["active"] is False and st["current_step"] is None, "wizard complete → inactive (no more redirect)")
    ok(st["first_login"] is True, "first login continues until the first draft rendered (Trends/Settings hidden)")
    FD = fd["video_id"]
    ok(llm_guard.require("render_submit", workspace_id=WA, fields=("topic", "script")) == WA
       and llm_guard.would_allow(WA, "render_submit"), "require('render_submit') allowed for the draft")
    with Session(eng) as s:
        v = s.get(Video, FD)
        first_topic = s.exec(select(Topic).where(Topic.channel_id == CHA)
                             .order_by(Topic.position, Topic.id)).first()
        ok(v.channel_id == CHA and v.topic_id == first_topic.id, "draft comes from the wizard's first topic")
        drafts = s.exec(select(Video).where(Video.channel_id == CHA, Video.status == VideoStatus.DRAFT)).all()
        ok(len(drafts) == 2, "the other ideas stay drafts (normal flow)")
        per_topic = [len(s.exec(select(Video).where(Video.topic_id == t.id)).all())
                     for t in s.exec(select(Topic).where(Topic.channel_id == CHA)
                                     .order_by(Topic.position, Topic.id)).all()]
        ok(per_topic == [1, 1, 1], f"PO CM: 1 idea from each of the 3 topics ({per_topic})")
        ok(sorted({d.topic_id for d in drafts}) == sorted({t.id for t in s.exec(select(Topic).where(
            Topic.channel_id == CHA)).all()} - {first_topic.id}), "the 2 drafts are topics 2 and 3")
        cands = render_loop._queued_candidates(s)
        ok(cands[0].id == FD, "render queue offers the first draft before the internal rows")
        ok(s.get(Settings, 1).topic_autogen_enabled == autogen_before, "autofill flag not flipped")
    r = post(WA, "first-draft", {"channel_id": CHA})
    ok(r.status_code == 200 and r.json()["first_draft"]["video_id"] == FD, "first-draft is idempotent")
    ok(get_state(WA)["llm_blocked"] is None, "no banner when allowed")
    ok(len([c for c in llm_calls if c.startswith("ideas")]) == 3, "three idea calls (one per topic)")

# ---- 3. without the AI-data ack → refused with the "AI data" banner --------------
print("without the AI-data ack: refused, 'AI data' step banner (S8)")
walk_to(WC, "consents")
CHC = get_state(WC)["channel"]["id"]
with Session(eng) as s:   # terms + privacy only (e.g. accepted before S2 shipped)
    w = s.get(Workspace, WC)
    w.terms_accepted_at = NOW
    w.privacy_accepted_at = NOW
    s.add(w)
    pin_terms_record(s, WC)
    s.commit()
with cfg(**ON):
    try:
        llm_guard.require("ideas", workspace_id=WC, fields=("topic",))
        ok(False, "require must refuse without the ack")
    except llm_guard.LLMBlocked as e:
        ok(e.reason == "ai_data_not_acknowledged", "require() refused: ai_data_not_acknowledged")
    with patch.object(onboarding_wizard, "missing_before", lambda done, step: None):
        n_calls = len(llm_calls)
        r = post(WC, "first-draft", {"channel_id": CHC})   # bypass the UI order: guard still holds
    d = r.json()["detail"]
    ok(r.status_code == 409 and d["code"] == "llm_blocked" and d["llm_blocked"]["step"] == "ai_data",
       "first-draft refused with the ai_data step")
    ok(len(llm_calls) == n_calls, "no LLM call made")
    b = get_state(WC)["llm_blocked"]
    ok(b and b["step"] == "ai_data" and b["message"] == 'Drafts paused: finish setup step "AI data".',
       "wizard state banner: Drafts paused: finish setup step \"AI data\".")
    r = client.get("/api/onboarding/status", headers=H(WC))
    ok(r.json()["llm_blocked"]["step"] == "ai_data", "GET /api/onboarding/status shows the same banner")
    bpt = client.get("/api/onboarding/status?locale=pt-BR", headers=H(WC)).json()["llm_blocked"]
    ok(bpt["message"] == 'Rascunhos pausados: termine a etapa "Dados de IA".' and bpt["locale"] == "pt",
       "PT banner (CMO copy) by locale, server-side")
    with Session(eng) as s:
        ok(not s.exec(select(Video).where(Video.channel_id == CHC)).all(), "no draft created")
    r = post(WC, "consents", CONSENTS)
    ok(r.status_code == 200 and r.json()["llm_blocked"] is None and row(WC).llm_blocked_reason is None,
       "ack → clear_block(ws): banner gone at once")
    ok(row(WC).terms_accepted_at.replace(tzinfo=timezone.utc) == NOW, "earlier terms acceptance kept")
    ok(llm_guard.require("ideas", workspace_id=WC, fields=("topic",)) == WC, "require() allowed after the ack")
with cfg(llm_external_release=False, llm_internal_workspace_ids="1"):
    with Session(eng) as s:
        w = s.get(Workspace, WC); w.ai_data_ack_at = None; s.add(w); s.commit()
    try:
        llm_guard.require("ideas", workspace_id=WC, fields=("topic",))
        ok(False, "flag OFF must refuse")
    except llm_guard.LLMBlocked as e:
        ok(e.reason is None, "flag OFF: #217 allowlist block unchanged (reason None)")
    ok(get_state(WC)["llm_blocked"] is None, "flag OFF: no banner (S8 unchanged)")

# ---- 4. workspace isolation -------------------------------------------------------
print("isolation: buyer B on buyer A's wizard channel; A on the internal channel")
for path, body in (("topics", {"channel_id": CHA, "niche": "n", "topics": PRESET["topics"]}),
                   ("budgets", {"channel_id": CHA}),
                   ("first-draft", {"channel_id": CHA})):
    r = post(WB, path, body)
    ok(r.status_code == 404, f"B → A's channel on {path}: 404")
    r = post(WA, path, dict(body, channel_id=IA))
    ok(r.status_code == 404, f"A → internal channel on {path}: 404")
r = post(WA, "topics", {"channel_id": 999999, "niche": "n", "topics": PRESET["topics"]})
ok(r.status_code == 404, "missing channel id: same 404")
stb = get_state(WB)
ok(stb["channel"] is None and stb["topics"] == [] and stb["first_draft"] is None and stb["active"],
   "B's wizard state shows nothing of A")
with Session(eng) as s:
    ok(len(s.exec(select(Topic).where(Topic.channel_id == CHA)).all()) == 3 and s.get(Channel, CHA).daily_render_budget == 3,
       "A's rows untouched by B's attempts")
    ok(s.get(Workspace, WB).first_draft_video_id is None, "B got no first draft")
r = post(WB, "consents", CONSENTS)
ok(r.status_code == 409 and row(WB).terms_accepted_at is None, "B consents only after its own steps (order)")
with Session(eng) as s:
    w = s.get(Workspace, WB)
    w.first_draft_video_id = FD      # forged pointer to A's draft
    s.add(w); s.commit()
    ok(onboarding_wizard.priority_video_ids(s) == {FD} and get_state(WB)["first_draft"] is None,
       "a foreign first_draft_video_id grants no priority and is not shown")
    w = s.get(Workspace, WB); w.first_draft_video_id = None; s.add(w); s.commit()

# ---- 5. first-draft priority: order only ------------------------------------------
print("first-draft priority: concurrency, budgets, internal rows untouched")
with Session(eng) as s:
    before = {"conc": s.get(Settings, 1).render_concurrency,
              "budgets": {c.id: (c.daily_render_budget, c.daily_publish_budget)
                          for c in s.exec(select(Channel)).all()},
              "rows": {v.id: (v.status, v.position) for v in s.exec(select(Video)).all()}}
    with_prio = [v.id for v in render_loop._queued_candidates(s)]
    with patch.object(onboarding_wizard, "priority_video_ids", lambda _s: set()):
        no_prio = [v.id for v in render_loop._queued_candidates(s)]
    ok(with_prio[0] == FD and FD in no_prio and no_prio.index(FD) > 0,
       "without priority the draft would wait behind the internal rows")
    ok([i for i in with_prio if i != FD] == [i for i in no_prio if i != FD],
       "every other row keeps its relative order (per-channel long/short mix unchanged)")
    after = {"conc": s.get(Settings, 1).render_concurrency,
             "budgets": {c.id: (c.daily_render_budget, c.daily_publish_budget)
                         for c in s.exec(select(Channel)).all()},
             "rows": {v.id: (v.status, v.position) for v in s.exec(select(Video)).all()}}
    ok(before == after, "ordering writes nothing: render_concurrency, budgets, statuses, positions")

src = inspect.getsource(render_loop._first_draft_first)
code = "\n".join(l for l in src.splitlines() if not l.strip().startswith(("#", '"""')) and "``" not in l)
ok("render_concurrency" not in code.split('"""')[-1] and "daily_render_budget" not in code.split('"""')[-1],
   "_first_draft_first reads no concurrency / budget")
for fn in ("_auto_produce", "_rebalance_queued_mix"):
    ok("priority" not in inspect.getsource(getattr(render_loop, fn)), f"{fn} (OS/RR mix) has no priority hook")


class _FakeEngine:
    submitted: list[int] = []

    def submit(self, video, params):
        self.submitted.append(video.id)
        return f"task-{video.id}"


fake = _FakeEngine()
submit_patches = [
    patch.object(render_loop, "_llm_guard_block", lambda *a, **k: False),
    patch.object(render_loop, "_prepare_provided_script", lambda *a, **k: ""),
    patch.object(render_loop, "_gate_b_preflight_block", lambda *a, **k: False),
    patch.object(render_loop, "ensure_topic_playlist", lambda *a, **k: None),
    patch.object(render_loop, "resolve_engine", lambda *a, **k: "fake"),
    patch.object(render_loop, "get_engine", lambda name: fake),
]
with contextlib.ExitStack() as st_:
    for p in submit_patches:
        st_.enter_context(p)
    # (a) an internal row already rendering fills render_concurrency=1: no preemption.
    with Session(eng) as s:
        busy = s.exec(select(Video).where(Video.channel_id == IB, Video.status == VideoStatus.QUEUED)).first()
        busy.status = VideoStatus.RENDERING
        s.add(busy); s.commit()
        render_loop._submit_new(s)
        ok(fake.submitted == [] and s.get(Video, FD).status == VideoStatus.QUEUED,
           "slot busy (concurrency 1): nothing submitted, in-flight render not preempted")
        busy.status = VideoStatus.QUEUED
        s.add(busy); s.commit()
    # (b) slot frees up: exactly one submit, and it is the first draft.
    with Session(eng) as s:
        render_loop._submit_new(s)
        s.commit()
        ok(fake.submitted == [FD], "free slot: exactly one submit (concurrency 1) and it is the first draft")
        rendering = s.exec(select(Video).where(Video.status == VideoStatus.RENDERING)).all()
        ok([v.id for v in rendering] == [FD], "internal rows stay QUEUED")
        ok(s.get(Settings, 1).render_concurrency == 1, "render_concurrency still 1")

print("S1 hook: first successful render stamps first_draft_at → priority lapses")
with Session(eng) as s:
    ok(activation.stamp_first_draft(s, s.get(Channel, CHA)) is True, "activation.stamp_first_draft (render_loop._finalize)")
    s.commit()
    ok(s.get(Workspace, WA).first_draft_at is not None, "first_draft_at set")
    ok(onboarding_wizard.priority_video_ids(s) == set(), "no priority once the first draft rendered")
ok(get_state(WA)["first_draft"]["priority"] is False and get_state(WA)["first_draft_at"], "state: priority false, first_draft_at shown")
ok(get_state(WA)["first_login"] is False and get_state(1)["first_login"] is False,
   "first draft rendered → full nav; legacy workspace never in first-login mode")
ok(render_loop._finalize.__code__.co_names.count("stamp_first_draft") >= 1
   or "stamp_first_draft" in inspect.getsource(render_loop._finalize), "render_loop._finalize calls the S1 hook")

# ---- 6. per instance, copy flag, frontend wiring ------------------------------------
print("CTO 14/11: re-acceptance when the latest consent record names no document sha")
# WC is ws 2 here, inside the internal floor {1, 2} of terms_consent; use buyer C (id > 2).
ok(WC > 2, "buyer C is an external workspace id")
client.post("/api/onboarding/ai-data/ack", headers=H(WC),
            json={"ai_data": True, "text_sha": google_limited_use.text_sha(google_limited_use.TEXT_EN), "locale": "en"})
from app.models import TermsConsentEvent  # noqa: E402,F811
with cfg(**ON):
    ok(llm_guard.block_reason(WC, "ideas") is False, "(b) WC's record carries the doc shas → passes")
    ok(client.get("/api/onboarding/terms", headers=H(WC)).json()["reaccept_required"] is False, "no re-accept needed")
    with Session(eng) as s_:   # e.g. accepted while the deploy had no doc shas set
        s_.add(TermsConsentEvent(workspace_id=WC, user_id="x", locale="en", sentence_sha=terms_consent.SHA_EN,
                                 terms_url=terms_consent.TERMS_URL_EN, terms_doc_sha=None,
                                 privacy_url=terms_consent.PRIVACY_URL_EN, privacy_doc_sha=None))
        s_.commit()
    ok(llm_guard.block_reason(WC, "ideas") == "terms_reaccept_required",
       "(a) blank latest record + shas now configured → refused terms_reaccept_required")
    ts_ = client.get("/api/onboarding/terms", headers=H(WC)).json()
    ok(ts_["reaccept_required"] is True and ts_["valid"] is False, "GET /terms: reaccept_required → Settings shows the box")
    st_ = get_state(WC)
    ok({x["id"]: x["done"] for x in st_["steps"]}["consents"] is False, "wizard: the consents step is open again")
    ok(llm_guard.block_message("terms_reaccept_required", "pt")["step"] == "terms_privacy", "banner → Terms step")
    r = client.post("/api/onboarding/terms/accept", headers=H(WC), json={"terms": False})
    ok(r.status_code == 400, "unchecked → 400")
    r = client.post("/api/onboarding/terms/accept", headers=H(WC),
                    json={"terms": True, "terms_text_sha": terms_consent.SHA_EN, "terms_locale": "en"})
    ok(r.status_code == 200 and r.json()["reaccept_required"] is False and r.json()["valid"] is True,
       "re-accept → cleared")
    with Session(eng) as s_:
        last = terms_consent.latest_record(s_, WC)
    ok(last.terms_doc_sha == PINNED_DOC_SHA and last.privacy_doc_sha == PINNED_DOC_SHA and last.locale == "en",
       "the new record carries the document shas now in force")
    ok(llm_guard.block_reason(WC, "ideas") is False, "release passes again")
    ok({x["id"]: x["done"] for x in get_state(WC)["steps"]}["consents"] is True, "wizard consents step done again")
    with patch.object(settings, "terms_doc_sha_en", "7" * 64):
        ok(llm_guard.block_reason(WC, "ideas") is False, "a later doc update (new configured sha) does not force it")
    with Session(eng) as s_:   # (c) internal ws 1 with a blank record
        s_.add(TermsConsentEvent(workspace_id=1, user_id="x", locale="en", sentence_sha=terms_consent.SHA_EN,
                                 terms_url=terms_consent.TERMS_URL_EN, privacy_url=terms_consent.PRIVACY_URL_EN))
        w1 = s_.get(Workspace, 1)
        w1.terms_accepted_at = w1.terms_accepted_at or NOW
        s_.add(w1); s_.commit()
    ok(llm_guard.block_reason(1, "ideas") is False and
       client.get("/api/onboarding/terms", headers=H(1)).json()["reaccept_required"] is False,
       "(c) internal ws 1: unaffected (allowed, no re-accept box)")

print("per instance + copy flag + SPA wiring")
blob = (ROOT / "app/services/topic_presets.py").read_text()
low = "\n".join(l for l in blob.splitlines() if not l.lstrip().startswith("#")).lower()
for bad in ("owera", "agent memory", "osi", "channels manager"):
    ok(bad not in low.split('"""', 2)[-1], f"presets carry no Owera-specific text ({bad!r})")
ok('COPY_STATUS = "CMO copy 2026-11-14"' in blob and "df45bd814374" in blob, "presets = CMO copy (sha cited)")
for f in ("app/services/topic_presets.py", "frontend/src/pages/Wizard.tsx", "docs/ONBOARDING_WIZARD.md",
          "docs/screenshots/s2/README.md"):
    ok("copy pending cmo review" not in (ROOT / f).read_text().lower(), f"no 'copy pending CMO review' marker left in {f}")
wsrc = (ROOT / "app/services/onboarding_wizard.py").read_text() + (ROOT / "app/routers/onboarding.py").read_text()
ok("workspace_id == 1" not in wsrc and "OWERA_WORKSPACE_ID" not in wsrc, "wizard code hardcodes no workspace id")
ok("topic_autogen_enabled" not in wsrc, "wizard never touches the global autofill flag")
fe = ROOT / "frontend/src"
wiz_tsx = (fe / "pages/Wizard.tsx").read_text()
ok("l6ConnectAiLineProps" in wiz_tsx and "ConnectConsentNote" in wiz_tsx and "aria-describedby" in wiz_tsx,
   "Wizard: Connect YouTube shows the #215 consent note + L6 line (aria-describedby)")
ok("googleLimitedUse" in wiz_tsx and "LlmBlockedBanner" in wiz_tsx and "wizardT as t" in wiz_tsx,
   "Wizard: Google Limited Use checkbox, S8 banner, i18n strings")
l6 = (fe / "l6ConnectAiLine.ts").read_text()
ok('L6_CONNECT_AI_LINE_SHA = "845c22005853"' in l6, "l6ConnectAiLine.ts exports the line sha")
gate = (fe / "AuthGate.tsx").read_text() + (fe / "App.tsx").read_text()
ok("useWizard" in gate and "/welcome" in gate, "AuthGate routes a 0-channel workspace to /welcome")
ok("hideOnFirstLogin" in gate and "first_login" in gate, "App hides Trends/Settings on the first login")

for p in llm_patches:
    p.stop()
appdb_patch.stop()
print(f"\nALL {_checks} CHECKS PASSED")
