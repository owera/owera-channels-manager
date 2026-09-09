"""Craft gates: spoken-title publish lock, Decolar claim alignment, CTA ban.

    PYTHONPATH=. .venv/bin/python tests/verify_craft.py
"""
import sys
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.models import Channel, OAuthStatus, Topic, Video, VideoStatus
from app.services import craft, publish_loop, thumbnail, youtube
from app.services.engines import storyboard, theme

_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


# ---------------------------------------------------------------------------
print("spoken title regex")

ok(craft.spoken_title_ok("Copilot billed the cancelled run · Copilot Credits 14"),
   "EN Copilot Credits nn matches")
ok(craft.spoken_title_ok("A API chegou como produto · IA 3"),
   "PT IA nn matches")
ok(craft.spoken_title_ok("Ollama na RTX · Local 7"), "Local nn matches")
ok(craft.spoken_title_ok("Memory died between chats · Agent memory 2"),
   "Agent memory nn matches")
ok(craft.spoken_title_ok("Crew blew the budget · CrewAI 9"), "CrewAI nn matches")
ok(craft.spoken_title_ok("Claude ate the context · Claude Code 11"),
   "Claude Code nn matches")
ok(craft.spoken_title_ok("x · copilot credits 1"),
   "series match is case-insensitive")
ok(not craft.spoken_title_ok("Copilot billed the cancelled run"),
   "missing · series nn is rejected")
ok(not craft.spoken_title_ok("Hook · Other Series 1"),
   "unknown series label is rejected")
ok(not craft.spoken_title_ok("Hook · Copilot Credits"),
   "missing integer nn is rejected")
ok(not craft.spoken_title_ok(""), "empty title is rejected")
ok(not craft.spoken_title_ok(None), "None title is rejected")

ok(craft.title_gate_reason("nope", "short") == craft.TITLE_GATE_REASON,
   "shorts without the pattern get the parked-via-reject reason")
ok(craft.title_gate_reason("nope", "long") is None,
   "longs are exempt from the series suffix")
ok(craft.title_gate_reason("x · IA 1", "short") is None,
   "matching short is allowed")
ok("pré-pattern" in craft.TITLE_GATE_REASON and "reject" in craft.TITLE_GATE_REASON,
   "error text tells ops to park via reject, not mass-retitle")


# ---------------------------------------------------------------------------
print("Decolar: first spoken sentence + claim alignment")

ok(craft.first_spoken_sentence("Your RAG reads junk. Then we fix it.")
   == "Your RAG reads junk.",
   "first sentence is split on period")
ok(craft.spoken_hook_source("Your RAG reads junk · Copilot Credits 1",
                            "Ignored other script.")
   == "Your RAG reads junk",
   "title before · is the spoken hook source")
ok(craft.compress_claim("Your RAG reads junk because it embeds garbage tokens", 8)
   == "Your RAG reads junk because it embeds garbage",
   "compress_claim keeps the first 8 words, original casing (not Title Case)")
ok(craft.claim_aligned("Your RAG reads junk", "Your RAG reads junk because X"),
   "compression of the same claim aligns")
ok(craft.claim_aligned("Reranking in 5 lines", "Reranking in 5 lines · Copilot Credits 1"),
   "title echo aligns")
ok(not craft.claim_aligned("The Cache Is Lying", "Reranking in 5 lines"),
   "curiosity-gap slogan does NOT align (the old thumbnail brief)")


# ---------------------------------------------------------------------------
print("banned CTA scan/strip")

ok(craft.contains_banned("Follow for more RAG fixes tomorrow"),
   "follow for more is banned")
ok(craft.contains_banned("Siga-amanhã o servidor MCP"),
   "Siga-amanhã is banned")
ok(craft.contains_banned("Siga → Amanhã: overlap"),
   "Siga → is banned")
ok(craft.contains_banned("Join the waitlist for Owera Cloud"),
   "waitlist + Cloud is banned")
ok(craft.contains_banned("Follow us on Instagram and LinkedIn"),
   "Instagram/LinkedIn is banned")
ok(craft.contains_banned("SMY drop tomorrow"), "SMY is banned")
ok(not craft.contains_banned("Retrieve wide, rerank hard, generate thin."),
   "a builder punch is clean")
ok("Rerank hard" in craft.strip_banned(
    "Follow for more. Rerank hard. Siga amanhã."),
   "strip_banned drops CTA sentences and keeps the lesson")


# ---------------------------------------------------------------------------
print("brand_of")

ok(craft.brand_of("ch1") == "os", "ch1 → os (Owera B&W)")
ok(craft.brand_of("owera-software", "Owera Software") == "os", "owera slug → os")
ok(craft.brand_of("ch2") == "rr", "ch2 → rr")
ok(craft.brand_of("rodrigo-recio") == "rr", "recio slug → rr")
ok(craft.brand_of("other") is None, "unknown slug stays unbranded (legacy palette)")


# ---------------------------------------------------------------------------
print("theme brand palettes kill shared neon")

os_th = theme.resolve(1, "hello", brand="os")
ok(os_th["bg_base"] == "#000000" and os_th["fg"] == "#ffffff",
   "OS canvas is black/white")
ok(os_th["accent"].lower() in {p[0] for p in theme._OS_PALETTE},
   "OS accent stays in the B&W family")
ok("#5b8cff" not in os_th["accent"], "OS does not use the neon blue")
rr_th = theme.resolve(1, "hello", brand="rr")
ok(rr_th["bg_base"] == "#14110e", "RR canvas is warm personal, not #0b0b16 neon")
ok(rr_th["accent"] != os_th["accent"], "OS and RR are distinct systems")
legacy = theme.resolve(1, "hello")
ok(legacy["accent"] == "#00c9a7" and legacy["fg_dim"] == "#c9d2ff",
   "unbranded resolve keeps the legacy palette (unit tests / leftover templates)")


# ---------------------------------------------------------------------------
print("storyboard lock: frame0 = first spoken sentence, no Follow force")

WORDS = [{"text": w, "start": i * 0.5, "dur": 0.4}
         for i, w in enumerate("Your RAG reads junk then we fix the embed".split())]
script = "Your RAG reads junk. Then we fix the embed path."


def _board():
    import json
    return json.dumps({"beats": [
        {"type": "hook", "cue": "Your RAG", "text": "Curiosity gap slogan", "emoji": "💸"},
        {"type": "stat", "cue": "reads junk", "value": "42", "unit": "ms", "label": "per call"},
        {"type": "list", "cue": "then we", "items": ["one", "two"]},
        {"type": "cta", "cue": "fix the", "text": "Follow", "sub": "Follow for more"},
    ]})


html = storyboard.compose(
    subject="Your RAG reads junk · Copilot Credits 1", script=script,
    words=WORDS, duration=12.0, resolution="portrait", width=1080, height=1920,
    topic_id=1, content_format="short", language="English",
    llm=lambda *a, **k: _board(),
)
ok(html is not None, "compose returns HTML")
ok("Your RAG reads junk" in html, "frame0 is the first spoken sentence (Decolar)")
ok("Curiosity gap slogan" not in html,
   "curiosity-gap hook text is overwritten, not shown")
ok("Follow" not in html and "Siga" not in html,
   "compose does NOT force Follow/Siga (banned)")
ok("💸" not in html.split("beat hook")[1].split("beat ")[0] if "beat hook" in html else True,
   "hook emoji is stripped (no second punch)")


# ---------------------------------------------------------------------------
print("thumbnail hook: aligned compression, not a gap")

from unittest.mock import patch


def _gap_llm(prompt, system=None, max_tokens=None):
    return "The Cache Is Lying"


with patch.object(thumbnail, "_llm", side_effect=_gap_llm):
    out = thumbnail._hook_text("ignored", "Reranking in 5 lines · Copilot Credits 1")
ok(out == "Reranking in 5 lines",
   "curiosity-gap LLM output is rejected; fallback is the spoken title (not Title Case)")

sys_src = thumbnail._hook_text.__doc__ + ""
import inspect
src = inspect.getsource(thumbnail._hook_text)
ok("DECOLAR LOCK" in src and "Repeating the title is REQUIRED" in src,
   "thumbnail system prompt requires repeating the title")
ok("curiosity GAP" not in src and "Do NOT reuse the title" not in src,
   "old curiosity-gap / do-not-reuse-title brief is gone")
ok("Compress THIS claim" in src,
   "user prompt asks to compress THIS claim, not open a gap")


# ---------------------------------------------------------------------------
print("publish_loop refuses a pré-pattern title (no upload)")

engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                       poolclass=StaticPool)
SQLModel.metadata.create_all(engine)
s = Session(engine)
ch = Channel(slug="ch1", name="Owera Software", oauth_status=OAuthStatus.CONNECTED)
s.add(ch); s.commit(); s.refresh(ch)
t = Topic(channel_id=ch.id, name="Credits", theme_prompt="x", content_format="short")
s.add(t); s.commit(); s.refresh(t)
v = Video(channel_id=ch.id, topic_id=t.id, subject="billed",
          status=VideoStatus.APPROVED, video_path="/tmp/x.mp4",
          title="Copilot billed the cancelled run")  # no · series nn
s.add(v); s.commit(); s.refresh(v)

_uploads = []
_orig_get, _orig_up = youtube.get_service, youtube.upload_video
youtube.get_service = lambda slug: object()
youtube.upload_video = lambda *a, **k: (_uploads.append("up") or "vid")
publish_loop._publish_one(s, ch, v)
ok(_uploads == [], "bad title never opens upload_video")
ok(v.status == VideoStatus.REVIEW, "blocked publish returns the row to review")
ok(v.error and "spoken series pattern" in v.error,
   "blocked publish records the gate reason")
ok(v.yt_video_id is None, "no YouTube id on a blocked publish")

v2 = Video(channel_id=ch.id, topic_id=t.id, subject="ok",
           status=VideoStatus.APPROVED, video_path="/tmp/x.mp4",
           title="Copilot billed the cancelled run · Copilot Credits 14")
s.add(v2); s.commit(); s.refresh(v2)
# Don't actually run the rest of publish (playlist/thumbnail) — just the gate.
blocked = craft.title_gate_reason(v2.title, "short")
ok(blocked is None, "matching title is not blocked")
youtube.get_service, youtube.upload_video = _orig_get, _orig_up


print(f"\nALL {_checks} CHECKS PASSED")
