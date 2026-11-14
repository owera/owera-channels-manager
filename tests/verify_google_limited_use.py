"""S2 follow-up — Google Limited Use consent (LegalOps 14/11) + PO CM 14/11 decisions.

Run: PYTHONPATH=. uv run python tests/verify_google_limited_use.py

In-memory SQLite + TestClient, temp LLM ledger. No network, no Google, no Stripe
call, no real LLM (``llm._complete_api`` mocked, CLI raises). Covers:
  (a) the consent checkbox is unchecked by default (SPA source) and the server
      refuses an unticked box
  (b) without the consent only the LLM is blocked: connect + view keep working
  (c) stored: ai_data_ack_at, user, workspace, locale, sha of the EXACT text
      shown (EN or PT); the server only accepts the known shas (not the L6 sha)
  (d) a stale text sha blocks the LLM (ai_data_ack_stale) and the ack page shows
  (e) withdraw endpoint + Settings control, and disconnect / request-deletion,
      cut the LLM; the #214 clock is the existing one (no second purge path)
  (f) naming: "Google Limited Use consent" only; repo grep for the forbidden
      Brazilian-law name (pattern built at runtime so this file doesn't match)
  (g) the L3 Google-field allowlist is 1:1 with the fields the text names
  - ack-only page: required for any external workspace without a current
    consent (terms accepted outside the wizard too); internal allowlist exempt
  - PO CM: buyer week-1 budget 2/day + day-1 burst 3; internal channels untouched
  - PO CM: per-workspace monthly subcap → workspace_monthly_cap, banner EN/PT,
    persisted (S8 llm_blocked) and counted in S1's A5; internal exempt
  - CMO copy (CM-WIZARD-COPY.md sha df45bd814374) in the SPA dictionary and presets
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import app.db as appdb
import app.main as main
from app.config import settings
from app.db import get_session
from app.models import (Channel, GoogleLimitedUseConsentEvent, OAuthStatus, Settings, Topic, Video,
                        VideoStatus, Workspace)
from app.services import (activation, buyer_budget, google_limited_use as glu, llm, llm_guard,
                          notify, render_loop, stripe_provision, topic_presets, youtube)

ROOT = Path(__file__).resolve().parent.parent
_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


EN, PT = glu.TEXT_EN, glu.TEXT_PT
SHA_EN = hashlib.sha256(EN.encode()).hexdigest()
SHA_PT = hashlib.sha256(PT.encode()).hexdigest()
NOW = datetime.now(timezone.utc)

eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
SQLModel.metadata.create_all(eng)
appdb_patch = patch.object(appdb, "engine", eng)
appdb_patch.start()

with Session(eng) as s:
    s.add(Workspace(id=1, slug="owera", name="Internal"))
    s.add(Settings(id=1, render_concurrency=4))
    s.commit()
    ich = Channel(workspace_id=1, slug="int-a", name="Internal A", oauth_status=OAuthStatus.CONNECTED,
                  daily_render_budget=2, daily_publish_budget=2)
    s.add(ich); s.commit(); s.refresh(ich)
    ICH = ich.id


def provision(email, cs):
    with Session(eng) as s:
        wid = stripe_provision.handle_checkout_completed(
            s, {"id": cs, "mode": "subscription", "customer": f"cus_{cs}",
                "subscription": f"sub_{cs}", "customer_details": {"email": email}},
            event_id=f"evt_{cs}", event_created=int(NOW.timestamp()))
        s.commit()
        return wid


WA = provision("buyer-a@example.com", "cs_a")     # goes through consent here
WB = provision("buyer-b@example.com", "cs_b")     # terms accepted outside the wizard
with Session(eng) as s:
    SLUG = {w: s.get(Workspace, w).slug for w in (WA, WB)}
    for w in (WA, WB):
        r = s.get(Workspace, w)
        r.terms_accepted_at = NOW
        r.privacy_accepted_at = NOW
        s.add(r)
    s.commit()


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


def refused(ws, ct="ideas", fields=("topic",)):
    try:
        llm_guard.require(ct, workspace_id=ws, fields=fields)
        return None
    except llm_guard.LLMBlocked as e:
        return e.reason


llm_patches = [patch.object(llm, "_complete_api", lambda *a, **k: "Idea one\n"),
               patch.object(llm, "_complete_cli", lambda *a, **k: (_ for _ in ()).throw(AssertionError("CLI")))]
for p in llm_patches:
    p.start()

# ---- the exact LegalOps texts ---------------------------------------------------
print("LegalOps texts: verbatim on both sides, shas known")
ok(EN == ("I agree that Channels Manager sends my videos' titles, descriptions, tags and channel stats "
          "to an AI provider in the US, only to create my own drafts."), "EN text verbatim (server)")
ok(PT == ("Concordo que o Channels Manager envie os títulos, descrições e tags dos meus vídeos e as "
          "estatísticas do canal a um provedor de IA nos EUA, só para criar os meus próprios rascunhos."),
   "PT text verbatim (server)")
ok(glu.KNOWN_SHAS == {SHA_EN: "en", SHA_PT: "pt"}, "server accepts exactly the two text shas")
ts_src = (ROOT / "frontend/src/googleLimitedUse.ts").read_text()
ok(json.dumps(EN, ensure_ascii=False) in ts_src and json.dumps(PT, ensure_ascii=False) in ts_src,
   "SPA googleLimitedUse.ts carries the same EN/PT strings")
ok(f'"{SHA_EN}"' in ts_src and f'"{SHA_PT}"' in ts_src, "SPA shas = sha256 of the strings")
ok('"Privacy policy"' in ts_src and '"Política de privacidade"' in ts_src, "link labels EN/PT")
box = (ROOT / "frontend/src/GoogleLimitedUseBox.tsx").read_text()
ok('target="_blank"' in box and "href={copy.href}" in box and "noopener" in box
   and "POLICY_LINKS" not in box, "privacy link next to the text opens in a new tab (per-locale href)")
ok('GOOGLE_LIMITED_USE_HREF_EN = "https://owera.com/privacy/#cm-google"' in ts_src
   and 'GOOGLE_LIMITED_USE_HREF_PT = "https://owera.com/privacy/pt/#cm-google"' in ts_src
   and "href: GOOGLE_LIMITED_USE_HREF_PT" in ts_src and "href: GOOGLE_LIMITED_USE_HREF_EN" in ts_src,
   "PO CM: privacy link by locale (EN /privacy/#cm-google, PT /privacy/pt/#cm-google)")

# ---- (a) unchecked by default -----------------------------------------------------
print("(a) checkbox unchecked by default")
wiz_tsx = (ROOT / "frontend/src/pages/Wizard.tsx").read_text()
page_tsx = (ROOT / "frontend/src/pages/AiData.tsx").read_text()
cons = wiz_tsx[wiz_tsx.index("function ConsentsStep"):wiz_tsx.index("function FirstDraftStep")]
ok("const [ai, setAi] = useState(false)" in cons and "checked={ai}" in cons, "wizard: AI box starts unchecked")
ok("const [ai, setAi] = useState(false)" in page_tsx and "checked={ai}" in page_tsx, "ack page: box starts unchecked")
ok("defaultChecked" not in box and "checked={checked}" in box, "the box itself has no default tick")
with cfg(**ON):
    r = client.post("/api/onboarding/ai-data/ack", headers=H(WA),
                    json={"ai_data": False, "text_sha": SHA_EN, "locale": "en"})
    ok(r.status_code == 400 and row(WA).ai_data_ack_at is None, "server: unticked box → 400, nothing stored")

# ---- (b) only the LLM is blocked ---------------------------------------------------
print("(b) without the consent: LLM blocked; connect + view keep working")
with cfg(**ON):
    ok(refused(WA) == "ai_data_not_acknowledged", "require() refused: ai_data_not_acknowledged")
    r = client.post("/api/channels", headers=H(WA), json={"name": "Buyer A", "slug": "buyer-a"})
    ok(r.status_code == 201, "create channel works")
    CHA = r.json()["id"]
    ok(client.get("/api/channels", headers=H(WA)).status_code == 200, "view: GET /api/channels 200")
    ok(client.get("/api/onboarding/status", headers=H(WA)).status_code == 200, "view: onboarding status 200")
    with patch.object(youtube, "has_client_secret", lambda slug: True), \
         patch.object(youtube, "build_flow", lambda slug, uri: object()), \
         patch.object(youtube, "authorization_url", lambda flow, **k: ("https://accounts.google.com/x", "st-a")):
        r = client.post(f"/api/channels/{CHA}/oauth/start", headers=H(WA))
    ok(r.status_code == 200 and r.json()["auth_url"].startswith("https://accounts.google.com"),
       "connect: OAuth start works without the consent")
    st = client.get("/api/onboarding/ai-data", headers=H(WA)).json()
    ok(st["required"] is True and st["valid"] is False and st["exempt"] is False,
       "ack-only page required (flag ON, external, no consent)")

# ---- (c) storage ---------------------------------------------------------------------
print("(c) stored fields + server-side sha validation")
with cfg(**ON):
    for body, code, why in (({"ai_data": True, "text_sha": "0" * 64, "locale": "en"}, "ai_data_text_changed", "unknown sha"),
                            ({"ai_data": True, "text_sha": "845c22005853", "locale": "en"}, "ai_data_text_changed", "the L6 sha"),
                            ({"ai_data": True, "text_sha": SHA_PT, "locale": "en"}, "ai_data_locale_mismatch", "PT sha with EN locale")):
        r = client.post("/api/onboarding/ai-data/ack", headers=H(WA), json=body)
        ok(r.status_code == 409 and r.json()["detail"]["code"] == code and row(WA).ai_data_ack_at is None,
           f"{why} → 409 {code}, nothing stored")
    with patch.object(glu, "request_user", lambda request: "buyer-a@example.com"):
        r = client.post("/api/onboarding/ai-data/ack?locale=pt-BR", headers=H(WA),
                        json={"ai_data": True, "text_sha": SHA_PT, "locale": "pt-BR"})
    w = row(WA)
    ok(r.status_code == 200 and r.json()["valid"] is True, "PT ack accepted")
    ok(w.ai_data_ack_at is not None and w.ai_data_ack_version == SHA_PT and w.ai_data_ack_locale == "pt"
       and w.ai_data_ack_user == "buyer-a@example.com",
       "ai_data_ack_at + sha of the exact PT text + locale pt + user stored on the workspace")
    with Session(eng) as s:
        ev = s.exec(select(GoogleLimitedUseConsentEvent).where(GoogleLimitedUseConsentEvent.workspace_id == WA)).all()
    ok(len(ev) == 1 and ev[0].action == "ack" and ev[0].text_sha == SHA_PT and ev[0].user == "buyer-a@example.com"
       and ev[0].locale == "pt" and ev[0].workspace_id == WA, "audit event: workspace, user, locale, text sha")
    ok(refused(WA) is None, "LLM allowed after the consent")
    ok(w.ai_data_ack_version != "845c22005853", "stored sha is the text sha, not the L6 sha")


class _Req:
    def __init__(self, scope, session):
        self.scope = scope
        self.session = session
        self.headers = {}


from app.services import spa_google_login  # noqa: E402
ok(glu.request_user(_Req({"session": {}}, {spa_google_login.SESSION_KEY: {"email": "Buyer-A@Example.com"}}))
   == "buyer-a@example.com", "user = the SPA Google session e-mail (normalized)")

# ---- (d) stale sha -------------------------------------------------------------------
print("(d) text sha changed → asked again")
with cfg(**ON):
    with Session(eng) as s:
        r_ = s.get(Workspace, WA); r_.ai_data_ack_version = hashlib.sha256(b"older wording").hexdigest()
        s.add(r_); s.commit()
    ok(refused(WA) == "ai_data_ack_stale", "stale consent → LLM refused (ai_data_ack_stale)")
    st = client.get("/api/onboarding/ai-data", headers=H(WA)).json()
    ok(st["required"] is True and st["stale"] is True, "ack-only page required again, marked stale")
    b = st["llm_blocked"]
    ok(b and b["step"] == "ai_data", "banner points at the AI data step")
    with patch.object(glu, "KNOWN_SHAS", {SHA_EN: "en"}):   # e.g. PT wording replaced
        with Session(eng) as s:
            r_ = s.get(Workspace, WA); r_.ai_data_ack_version = SHA_PT; s.add(r_); s.commit()
        ok(refused(WA) == "ai_data_ack_stale", "a sha dropped from the known texts is stale")
    r = client.post("/api/onboarding/ai-data/ack", headers=H(WA),
                    json={"ai_data": True, "text_sha": SHA_EN, "locale": "en"})
    ok(r.status_code == 200 and row(WA).ai_data_ack_version == SHA_EN and refused(WA) is None
       and row(WA).llm_blocked_reason is None, "re-ack with the current text → allowed, banner cleared")

# ---- (e) withdraw + disconnect ------------------------------------------------------
print("(e) withdraw / disconnect cut the LLM; #214 path reused")
with cfg(**ON):
    r = client.post("/api/onboarding/ai-data/withdraw", headers=H(WA))
    w = row(WA)
    ok(r.status_code == 200 and r.json()["valid"] is False and w.ai_data_ack_at is None
       and w.ai_data_ack_withdrawn_at is not None, "POST /api/onboarding/ai-data/withdraw clears the consent")
    ok(refused(WA) == "ai_data_not_acknowledged", "LLM refused after withdraw")
    with Session(eng) as s:
        acts = [e.action + ":" + (e.reason or "") for e in s.exec(
            select(GoogleLimitedUseConsentEvent).where(GoogleLimitedUseConsentEvent.workspace_id == WA)
            .order_by(GoogleLimitedUseConsentEvent.id)).all()]
    ok(acts[-1] == "withdraw:user", "withdraw event logged (reason user)")
    settings_src = (ROOT / "frontend/src/pages/Settings.tsx").read_text()
    ok(settings_src.count("<GoogleLimitedUseSettings />") == 2 and "withdraw.mutate" in page_tsx,
       "Settings shows the withdraw control (both scopes)")
    client.post("/api/onboarding/ai-data/ack", headers=H(WA), json={"ai_data": True, "text_sha": SHA_EN, "locale": "en"})
    ok(refused(WA) is None, "re-consented")
    calls = []
    real_mark = notify.mark_operator_disconnect
    with patch.object(youtube, "disconnect_channel", lambda s, ch: None), \
         patch.object(notify, "mark_operator_disconnect",
                      lambda s, ch: (calls.append(("operator_disconnect", ch.id)), real_mark(s, ch))[1]):
        r = client.post(f"/api/channels/{CHA}/disconnect", headers=H(WA))
    ok(r.status_code == 200 and calls == [("operator_disconnect", CHA)], "disconnect: existing #214 mark (once)")
    with Session(eng) as s:
        ok(s.get(Channel, CHA).access_lost_at is not None, "#214 7-day purge clock started by the existing path")
    ok(row(WA).ai_data_ack_at is None and refused(WA) == "ai_data_not_acknowledged", "disconnect cut the LLM")
    with Session(eng) as s:
        last = s.exec(select(GoogleLimitedUseConsentEvent).order_by(GoogleLimitedUseConsentEvent.id.desc())).first()
    ok(last.action == "withdraw" and last.reason == "disconnect", "withdraw event reason=disconnect")
    client.post("/api/onboarding/ai-data/ack", headers=H(WA), json={"ai_data": True, "text_sha": SHA_EN, "locale": "en"})
    with patch.object(youtube, "disconnect_channel", lambda s, ch: None):
        r = client.post(f"/api/channels/{CHA}/request-deletion", headers=H(WA))
    ok(r.status_code == 200 and row(WA).ai_data_ack_at is None, "request-deletion also cuts the LLM")
glu_src = (ROOT / "app/services/google_limited_use.py").read_text()
code_only = "\n".join(l for l in glu_src.split('"""', 2)[-1].splitlines() if not l.strip().startswith("#"))
ok(not re.search(r"purge|\.delete\(|youtube_purge|access_lost_at", code_only),
   "consent module never purges / deletes (no duplicate of #214)")

# ---- (f) naming -------------------------------------------------------------------------
FORBIDDEN = "L" + "GPD consent"
print(f"(f) 'Google Limited Use consent', never '{FORBIDDEN}'")
bad = re.compile("l" + "gpd" + r"[\s_\-]*(consent|consentimento)|(consent|consentimento)[\s_\-]*(da\s+)?l" + "gpd",
                 re.I)
files = subprocess.run(["git", "ls-files", "-co", "--exclude-standard"], cwd=ROOT, capture_output=True,
                       text=True).stdout.split()
hits = []
for f in files:
    p = ROOT / f
    if any(x in f for x in ("node_modules/", "dist/", ".png", ".jpg", ".ico", ".woff")) or not p.is_file():
        continue
    try:
        t = p.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        continue
    if bad.search(t):
        hits.append(f)
ok(hits == [], f"no '{FORBIDDEN}' naming anywhere in the repo ({hits})")
ok(glu.LOG_EVENT == "google_limited_use_consent" and "Google Limited Use consent" in glu_src,
   "code + log event named Google Limited Use consent")

# ---- (g) L3 allowlist 1:1 with the text --------------------------------------------------
print("(g) L3 Google-field allowlist = the fields the text names")
TEXT_WORDS = {"video_title": ("titles", "títulos"), "video_description": ("descriptions", "descrições"),
              "video_tags": ("tags", "tags"), "channel_stats": ("channel stats", "estatísticas do canal")}


def allowlist_matches_text(yt_fields) -> bool:
    return set(yt_fields) == set(TEXT_WORDS) == set(glu.TEXT_FIELDS)


ok(all(en in EN and pt in PT for en, pt in TEXT_WORDS.values()), "each field is named in EN and PT")
ok(allowlist_matches_text(llm_guard.YT_FIELDS), "llm_guard.YT_FIELDS == {titles, descriptions, tags, channel stats}")
ok(not allowlist_matches_text(set(llm_guard.YT_FIELDS) | {"video_comments"}), "an extra Google field fails the check")
ok(llm_guard.ALLOWED_FIELDS - llm_guard.YT_FIELDS == llm_guard.CM_FIELDS,
   "every other allowed field is a CM-authored field (CM_FIELDS), not Google data")
# PO CM 14/11: pin the FULL L3 allowlist. Any new field of any kind fails here.
PINNED_ALLOWLIST = frozenset({
    "video_title", "video_description", "video_tags", "channel_stats",            # Google (the text)
    "topic", "theme_prompt", "niche", "steer_notes", "language", "script",       # CM-authored
    "storyboard", "craft_rules"})
ok(llm_guard.ALLOWED_FIELDS == PINNED_ALLOWLIST and len(llm_guard.ALLOWED_FIELDS) == 12,
   f"L3 allowlist pinned to exactly the 12 fields (got {sorted(llm_guard.ALLOWED_FIELDS)})")
ok(llm_guard.CM_FIELDS == PINNED_ALLOWLIST - set(TEXT_WORDS), "the 8 non-Google fields pinned")
ok(frozenset(llm_guard.ALLOWED_FIELDS | {"steer_history"}) != PINNED_ALLOWLIST,
   "a new non-Google field would fail the pin")
declared = set()
for p in (ROOT / "app").rglob("*.py"):
    for m_ in re.finditer(r"llm_guard\.declare\(([^)]*)\)", p.read_text()):
        declared |= set(re.findall(r'"([a-z_]+)"', m_.group(1)))
from app.services.engines import worker as _worker  # noqa: E402
declared |= set(_worker._WORKER_LLM_FIELDS)
ok(declared <= llm_guard.ALLOWED_FIELDS and declared & llm_guard.YT_FIELDS <= set(TEXT_WORDS),
   f"every declare() site stays inside the allowlist ({sorted(declared)})")

# ---- ack-only page: who sees it --------------------------------------------------------
print("ack-only page: external without consent (terms outside the wizard); internal exempt")
with cfg(**ON):
    w = row(WB)
    ok(w.wizard_started_at is None and w.terms_accepted_at is not None, "B accepted terms outside the wizard")
    st = client.get("/api/onboarding/ai-data", headers=H(WB)).json()
    ok(st["required"] is True, "B: ack-only page required")
    wz = client.get("/api/onboarding/wizard", headers=H(WB)).json()
    ok(wz["google_limited_use"]["required"] is True, "wizard state carries it (AuthGate redirect)")
    st1 = client.get("/api/onboarding/ai-data", headers=H(1)).json()
    ok(st1["exempt"] is True and st1["required"] is False, "internal ws1: exempt via the allowlist")
    ok(refused(1) is None, "internal ws1 LLM unchanged")
    with cfg(llm_internal_workspace_ids="1,2"):
        ok(glu.status(row(WB))["required"] is (WB not in (1, 2)), "prod allowlist 1,2 honored")
with cfg(llm_external_release=False):
    ok(client.get("/api/onboarding/ai-data", headers=H(WB)).json()["required"] is False,
       "flag OFF: no ack page (#217 unchanged)")
gate = (ROOT / "frontend/src/AuthGate.tsx").read_text()
ok('google_limited_use?.required === true' in gate and '"/ai-data"' in gate, "AuthGate redirects to /ai-data")
ok('path="/ai-data"' in (ROOT / "frontend/src/App.tsx").read_text(), "route /ai-data")
ok("l6ConnectAiLineProps" in page_tsx and "GoogleLimitedUseBox" in page_tsx, "page reuses the L6 line + box")

# ---- PO CM: week-1 budget + day-1 burst ----------------------------------------------
print("buyer week-1 budget 2/day + day-1 burst 3")
r = client.post("/api/channels", headers=H(WB), json={"name": "Buyer B", "slug": "buyer-b"})
CHB = r.json()["id"]
ok(r.status_code == 201 and r.json()["daily_render_budget"] == 2 and r.json()["daily_publish_budget"] == 2,
   "buyer (Stripe) channel defaults to 2/2")
r = client.post("/api/channels", headers=H(WB), json={"name": "B2", "slug": "b2", "daily_render_budget": 5})
ok(r.status_code == 201 and r.json()["daily_render_budget"] == 5 and r.json()["daily_publish_budget"] == 2,
   "explicit budget kept; the other one defaults to 2")
r = client.post("/api/channels", headers=H(1), json={"name": "Int new", "slug": "int-new"})
ok(r.status_code == 201 and r.json()["daily_render_budget"] == 6, "internal ws1 keeps the app defaults (6)")
with Session(eng) as s:
    chb = s.get(Channel, CHB)
    ok(buyer_budget.effective_render_budget(s, chb) == 3, "day 1: buyer channel may render 3")
    ok(buyer_budget.effective_render_budget(s, chb, NOW + timedelta(hours=25)) == 2, "after 24 h: back to 2")
    ok(buyer_budget.effective_render_budget(s, s.get(Channel, ICH)) == 2, "internal channel (OS/RR-style): unchanged")
    chb.daily_render_budget = 0
    ok(buyer_budget.effective_render_budget(s, chb) == 0, "a 0 budget (stall) is never lifted")
    chb.daily_render_budget = 2
    legacy = Workspace(slug="legacy-ext", name="Legacy ext")
    s.add(legacy); s.commit(); s.refresh(legacy)
    lch = Channel(workspace_id=legacy.id, slug="lx", name="LX", daily_render_budget=2, daily_publish_budget=2)
    s.add(lch); s.commit(); s.refresh(lch)
    ok(buyer_budget.effective_render_budget(s, lch) == 2 and not buyer_budget.is_buyer(legacy),
       "non-wizard / non-Stripe workspace: no burst")


class _FakeEngine:
    submitted: list[int] = []

    def submit(self, video, params):
        self.submitted.append(video.id)
        return f"task-{video.id}"


fake = _FakeEngine()
with Session(eng) as s:
    chb = s.get(Channel, CHB)
    chb.oauth_status = OAuthStatus.CONNECTED
    tb = Topic(channel_id=CHB, name="T", weight=1)
    s.add_all([chb, tb]); s.commit(); s.refresh(tb)
    for i in range(2):
        s.add(Video(channel_id=CHB, topic_id=tb.id, subject=f"q{i}", status=VideoStatus.QUEUED, position=i))
    s.commit()
sub = [patch.object(render_loop, "_llm_guard_block", lambda *a, **k: False),
       patch.object(render_loop, "_prepare_provided_script", lambda *a, **k: ""),
       patch.object(render_loop, "_gate_b_preflight_block", lambda *a, **k: False),
       patch.object(render_loop, "ensure_topic_playlist", lambda *a, **k: None),
       patch.object(render_loop, "resolve_engine", lambda *a, **k: "fake"),
       patch.object(render_loop, "get_engine", lambda name: fake)]
with contextlib.ExitStack() as st_:
    for p in sub:
        st_.enter_context(p)
    with Session(eng) as s:
        with patch.object(render_loop.quota, "rendered_today", lambda s_, cid: 2 if cid == CHB else 0):
            render_loop._submit_new(s)
            s.commit()
            ok(len([v for v in fake.submitted if s.get(Video, v).channel_id == CHB]) == 1,
               "render loop: 2 rendered today, budget 2, day 1 → exactly one more (3rd) submitted")
            fake.submitted.clear()
            with patch.object(buyer_budget, "utcnow", lambda: NOW + timedelta(hours=30)):
                render_loop._submit_new(s)
            ok(not [v for v in fake.submitted if s.get(Video, v).channel_id == CHB],
               "after day 1: budget 2 reached → nothing submitted")

# ---- PO CM: per-workspace monthly subcap ------------------------------------------------
print("per-workspace monthly subcap (MANAGER_LLM_WORKSPACE_MONTHLY_USD_CAP)")
ok(settings.llm_workspace_monthly_usd_cap == 5.0, "code default 5 USD")
env_ex = (ROOT / ".env.example").read_text()
ok("MANAGER_LLM_WORKSPACE_MONTHLY_USD_CAP=5" in env_ex, ".env.example documents it (default 5)")
ledger = llm.usage_path()
ledger.parent.mkdir(parents=True, exist_ok=True)


def write_ledger(rows):
    ledger.write_text("".join(json.dumps(r) + "\n" for r in rows))


month = NOW.strftime("%Y-%m")
last_month = (NOW.replace(day=1) - timedelta(days=1)).isoformat()
with cfg(**ON):
    client.post("/api/onboarding/ai-data/ack", headers=H(WA), json={"ai_data": True, "text_sha": SHA_EN, "locale": "en"})
    write_ledger([{"ts": NOW.isoformat(), "workspace_id": WA, "cost_usd": 4.99},
                  {"ts": last_month, "workspace_id": WA, "cost_usd": 50},
                  {"ts": NOW.isoformat(), "workspace_id": 1, "cost_usd": 100},
                  {"ts": NOW.isoformat(), "workspace_id": WB, "cost_usd": 9}])
    ok(refused(WA) is None, "4.99 this month (50 last month ignored) → allowed")
    write_ledger([{"ts": NOW.isoformat(), "workspace_id": WA, "cost_usd": 4.99},
                  {"ts": NOW.isoformat(), "workspace_id": str(WA), "cost_usd": 0.01},
                  {"ts": NOW.isoformat(), "workspace_id": 1, "cost_usd": 100}])
    ok(refused(WA) == "workspace_monthly_cap", "5.00 reached → refused: workspace_monthly_cap")
    ok(row(WA).llm_blocked_reason == "workspace_monthly_cap" and row(WA).llm_blocked_at is not None,
       "persisted via S8 llm_blocked")
    b = client.get("/api/onboarding/status", headers=H(WA)).json()["llm_blocked"]
    ok(b["step"] == "monthly_cap" and b["message"].startswith("Monthly draft limit reached."), "EN banner")
    bpt = client.get("/api/onboarding/status?locale=pt-BR", headers=H(WA)).json()["llm_blocked"]
    ok(bpt["message"].startswith("Limite mensal de rascunhos atingido."), "PT banner (server-side, by locale)")
    with Session(eng) as s:
        a5 = activation.report(s, s.get(Workspace, WA))["A5"]
    ok(a5["done"] is False and a5["llm_blocked"]["reason"] == "workspace_monthly_cap" and a5["monthly_cap"]["reached"]
       and a5["monthly_cap"]["spent_usd"] == 5.0 and a5["monthly_cap"]["cap_usd"] == 5.0,
       "counted in S1's A5 (llm_blocked + monthly_cap)")
    ok(refused(1) is None, "internal ws1 with 100 USD this month: exempt")
    with Session(eng) as s:
        a5i = activation.report(s, s.get(Workspace, 1))["A5"]
    ok(a5i["monthly_cap"]["exempt"] is True and a5i["monthly_cap"]["reached"] is False, "A5: internal exempt")
    with cfg(llm_workspace_monthly_usd_cap=10.0):
        ok(refused(WA) is None, "env raises the cap → allowed")
    with cfg(llm_workspace_monthly_usd_cap=0.0):
        ok(refused(WA) == "workspace_monthly_cap", "cap 0 → fail closed")
    with patch.object(llm, "month_to_date_usd", lambda *a, **k: (_ for _ in ()).throw(OSError("ro"))):
        ok(refused(WA) == "workspace_monthly_cap", "unreadable ledger → fail closed")
    import inspect
    ok("llm_monthly_usd_cap" in inspect.getsource(llm.check_budget), "global LLM_MONTHLY_USD_CAP check unchanged")
    write_ledger([])
    ok(refused(WA) is None and row(WA).llm_blocked_reason is None, "new month (empty ledger) → allowed, banner cleared")
wiz_banner = (ROOT / "frontend/src/ui.tsx").read_text()
ok("LlmBlockedBanner" in wiz_banner and "blocked.message" in wiz_banner, "SPA renders the server banner text")

# ---- CMO copy ---------------------------------------------------------------------------
print("CMO copy v2 (CM-WIZARD-COPY.md sha a8b4aa958efd)")
cmo = Path("/workspace/cm-onboarding-wizard-2026-11-14/CM-WIZARD-COPY.md")
dict_src = (ROOT / "frontend/src/wizardI18n.ts").read_text()
if cmo.exists():
    raw = cmo.read_bytes()
    ok(hashlib.sha256(raw).hexdigest().startswith("a8b4aa958efd"), "CMO file v2 sha a8b4aa958efd")
    rows = re.findall(r'^\| `((?:wizard|banner|ai_data|settings\.glu|status|nav)\.[a-z_.]+)` \| (.*?) \| (?:\d+|—) \| (.*?) \| (?:\d+|—) \|(?: [^|]*\|)?$',
                      raw.decode(), re.M)
    missing = [k for k, en, pt in rows if "[LegalOps" not in en and (
        f'"{k}": {{ en: {json.dumps(en, ensure_ascii=False)}, pt: {json.dumps(pt, ensure_ascii=False)} }}' not in dict_src)]
    ok(len(rows) >= 100 and missing == [], f"every CMO key in wizardI18n.ts, EN + PT verbatim ({missing})")
else:
    print("  (CMO file not on this box: verbatim check skipped)")
ok(len(topic_presets.NICHE_PRESETS_EN) == 5 and len(topic_presets.NICHE_PRESETS_PT) == 5
   and [p["id"] for p in topic_presets.NICHE_PRESETS_EN] == [p["id"] for p in topic_presets.NICHE_PRESETS_PT],
   "5 niches EN + PT, same ids")
ok(topic_presets.public_presets()["locale"] == "en" and topic_presets.public_presets("pt")["locale"] == "pt",
   "public_presets(locale) defaults to EN")
msgs = llm_guard.BLOCK_USER_TEXT
ok(msgs["ai_data_not_acknowledged"][1] == {"en": 'Drafts paused: finish setup step "AI data".',
                                           "pt": 'Rascunhos pausados: termine a etapa "Dados de IA".'},
   "BLOCK_USER_TEXT keyed by reason + locale (CMO)")
ok(llm_guard.block_message("nope", "pt")["message"].startswith("Rascunhos pausados: fale com o suporte"),
   "support fallback PT")
ok(EN not in dict_src and PT not in dict_src, "the consent text is not in the CMO dictionary (LegalOps only)")

for p in llm_patches:
    p.stop()
appdb_patch.stop()
print(f"\nALL {_checks} CHECKS PASSED")
