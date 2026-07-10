"""Dependency-free regression checks for the subscriber-growth machinery.

Run: PYTHONPATH=. .venv/bin/python tests/verify_growth.py

Covers the 2026-07-09 Subscriber Offensive Phase 1:
  - language plumbing: voice -> language name / BCP-47 code, channel lookup
  - upload body carries defaultLanguage/defaultAudioLanguage (and omits when unknown)
  - finalize_description: localized CTA + links, idempotent on publish retries
  - author first-comment text: localized, playlist-aware
  - fetch_traffic_sources parses the Analytics API response shape (stubbed client)

No network, no creds. Exits non-zero on the first failed assertion.
"""
import json
import sys

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.services import metadata, video_gen, youtube
from app.services.publish_loop import _first_comment_text
from app.models import Channel, RenderProfile

_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


# --- language plumbing ---------------------------------------------------------
print("language plumbing")
ok(video_gen.language_from_voice("pt-BR-AntonioNeural-Male") == "Brazilian Portuguese",
   "voice pt-BR-* maps to Brazilian Portuguese")
ok(video_gen.code_from_voice("pt-BR-AntonioNeural-Male") == "pt-BR",
   "voice pt-BR-* maps to code pt-BR")
ok(video_gen.code_from_voice("en-US-AndrewNeural") == "en-US", "en voice maps to en-US")
ok(video_gen.code_from_voice(None) is None, "no voice -> no code")

engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                       poolclass=StaticPool)
SQLModel.metadata.create_all(engine)
with Session(engine) as s:
    p = RenderProfile(name="pt", params_json=json.dumps({"voice_name": "pt-BR-AntonioNeural"}))
    s.add(p); s.commit(); s.refresh(p)
    ch = Channel(slug="c2", name="C2", default_render_profile_id=p.id)
    s.add(ch); s.commit(); s.refresh(ch)
    ok(video_gen.channel_language(s, ch.id) == "Brazilian Portuguese",
       "channel_language resolves via the default render profile")
    ok(video_gen.channel_language_code(s, ch.id) == "pt-BR",
       "channel_language_code resolves via the default render profile")
    ok(video_gen.channel_language_code(s, None) is None, "no channel -> no code")

# --- upload body ----------------------------------------------------------------
print("upload body language tags")
body = youtube._upload_body("T", "D", ["a"], "public", language_code="pt-BR")
ok(body["snippet"]["defaultLanguage"] == "pt-BR", "defaultLanguage set from language_code")
ok(body["snippet"]["defaultAudioLanguage"] == "pt-BR", "defaultAudioLanguage set from language_code")
body2 = youtube._upload_body("T", "D", ["a"], "public")
ok("defaultLanguage" not in body2["snippet"] and "defaultAudioLanguage" not in body2["snippet"],
   "language fields omitted when unknown (legacy behavior preserved)")
ok(body2["snippet"]["categoryId"] == youtube.CATEGORY_SCIENCE_TECH, "category unchanged")

# --- finalize_description --------------------------------------------------------
print("finalize_description")
d = metadata.finalize_description("Legal.\n\n#ia #ml", "pt-BR", "UCabc", "PLxyz")
ok("sub_confirmation=1" in d and "youtube.com/channel/UCabc" in d,
   "subscribe link with sub_confirmation appended")
ok("Inscreva-se" in d, "PT channel gets the PT CTA line")
ok("playlist?list=PLxyz" in d and "Série completa" in d, "PT playlist line appended")
d_again = metadata.finalize_description(d, "pt-BR", "UCabc", "PLxyz")
ok(d_again == d, "idempotent — a publish retry never double-appends")
d_en = metadata.finalize_description("Nice.", "en-US", "UCabc", None)
ok("Subscribe" in d_en and "playlist" not in d_en,
   "EN CTA used; playlist line skipped when no playlist exists")
d_none = metadata.finalize_description("Nice.", None, None, None)
ok(d_none == "Nice.", "no ids -> description unchanged")
long_base = "x" * 4990
ok(len(metadata.finalize_description(long_base, "en-US", "UCabc", "PLxyz")) <= 5000,
   "description capped at YouTube's 5000-char limit")

# --- first comment ----------------------------------------------------------------
print("author first comment")
c_pt = _first_comment_text("pt-BR", "PLxyz")
ok("comentários" in c_pt and "playlist?list=PLxyz" in c_pt, "PT comment with playlist link")
c_en = _first_comment_text(None, None)
ok("comment" in c_en and "playlist" not in c_en, "EN fallback without playlist")

# --- fetch_traffic_sources (stubbed analytics client) ------------------------------
print("fetch_traffic_sources")


class _Query:
    def __init__(self, resp):
        self._resp = resp

    def execute(self):
        return self._resp


class _Reports:
    def __init__(self, responses):
        self._responses = responses
        self.calls = []

    def query(self, **kw):
        self.calls.append(kw)
        return _Query(self._responses[len(self.calls) - 1])


class _Analytics:
    def __init__(self, responses):
        self._reports = _Reports(responses)

    def reports(self):
        return self._reports


# search views present -> both queries run, terms parsed
a = _Analytics([
    {"rows": [["YT_SEARCH", 12, 30], ["EXT_URL", 5, 4]]},
    {"rows": [["rag tutorial", 8], ["fine tuning", 4]]},
])
t = youtube.fetch_traffic_sources(a, "UC1", "vid1", "2026-07-01", "2026-07-09")
ok(t["sources"]["YT_SEARCH"] == {"views": 12, "watch_min": 30}, "source rows parsed")
ok(t["search_terms"] == {"rag tutorial": 8, "fine tuning": 4}, "search terms parsed")
ok(len(a._reports.calls) == 2, "search-terms query only runs when YT_SEARCH has views")

# no search views -> single query
a2 = _Analytics([{"rows": [["EXT_URL", 5, 4]]}])
t2 = youtube.fetch_traffic_sources(a2, "UC1", "vid1", "2026-07-01", "2026-07-09")
ok(t2["search_terms"] == {} and len(a2._reports.calls) == 1,
   "no YT_SEARCH views -> no second query")

# API failure -> empty dict, no raise
class _Boom:
    def reports(self):
        raise RuntimeError("api down")


t3 = youtube.fetch_traffic_sources(_Boom(), "UC1", "vid1", "2026-07-01", "2026-07-09")
ok(t3 == {"sources": {}, "search_terms": {}}, "failure returns empty shape, never raises")

print(f"\nALL {_checks} CHECKS PASSED")
