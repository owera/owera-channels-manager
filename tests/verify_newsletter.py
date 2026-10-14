"""OS newsletter link (description + endcard), RR never, placeholder = omitted.

Run: PYTHONPATH=. .venv/bin/python tests/verify_newsletter.py
"""
import sys

from app.config import Settings, settings
from app.services import craft, metadata, newsletter
from app.services.engines import storyboard, theme

_n = 0


def ok(cond, msg):
    global _n
    _n += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


def board(brand, chip="Subscribe · Agent memory"):
    th = theme.resolve(None, "x", brand=brand)
    beats = [{"type": "statement", "cue": "a", "text": "Agents forget.", "w": 2, "start": 0.0, "dur": 2.0},
             {"type": "cta", "cue": "b", "text": chip, "sub": "", "endcard": True,
              "start": 2.0, "dur": 2.0}]
    return storyboard.build_index_html(beats, th, "1080x1920", 1080, 1920, 4.0), beats


print("defaults: placeholder → nothing published")
d = Settings()
ok(d.newsletter_url == "TODO-CMO" and d.newsletter_text == "TODO-CMO", "defaults are TODO-CMO")
_u, _t = settings.newsletter_url, settings.newsletter_text
try:
    settings.newsletter_url, settings.newsletter_text = "TODO-CMO", "TODO-CMO"
    ok(newsletter.config() is None and newsletter.description_line("os") is None
       and newsletter.endcard_text("os") is None, "placeholder → no line anywhere")
    html, _ = board("os")
    ok("cta-news\"" not in html.split("<style")[0] and '<div class="cta-news">' not in html,
       "placeholder: OS endcard has no newsletter line")
    base = metadata.finalize_description("Base.", "en-US", "UCx", "PLx")
    ok(metadata.finalize_description("Base.", "en-US", "UCx", "PLx",
                                     newsletter_line=newsletter.description_line("os")) == base,
       "placeholder: description unchanged")
    for url, text in [("", "Join"), ("https://n.example/x", ""), ("n.example/x", "Join"),
                      ("https://TODO-CMO", "Join")]:
        settings.newsletter_url, settings.newsletter_text = url, text
        ok(newsletter.config() is None, f"unsafe config ({url!r}, {text!r}) → omitted")

    print("configured")
    settings.newsletter_url, settings.newsletter_text = "https://news.example.com/os", "Weekly agent notes"
    line = newsletter.description_line("os")
    ok(line == "📰 Weekly agent notes https://news.example.com/os", "OS description line")
    ok(newsletter.description_line("rr") is None and newsletter.description_line(None) is None,
       "RR / unbranded → no description line")
    d1 = metadata.finalize_description("Base.", "en-US", "UCx", "PLx", newsletter_line=line)
    ok(line in d1 and d1.index("Subscribe") < d1.index(line), "line lands in the links block")
    ok(metadata.finalize_description(d1, "en-US", "UCx", "PLx", newsletter_line=line) == d1,
       "idempotent on publish retry")
    d2 = metadata.finalize_description("Base.", "en-US", None, None, newsletter_line=line)
    ok(metadata.finalize_description(d2, "en-US", None, None, newsletter_line=line).count(line) == 1,
       "idempotent even without the subscribe marker")
    html, beats = board("os")
    ok('<div class="cta-news">Weekly agent notes</div>' in html, "OS endcard shows the text")
    ok("news.example.com" not in html, "no URL on the endcard (text only)")
    ok('<div class="cta-chip">Subscribe · Agent memory</div>' in html
       and craft.is_endcard_chip(beats[-1]["text"]) and "newsletter" not in beats[-1],
       "chip 'Subscribe · {series}' intact; beat JSON untouched (Gate B board unchanged)")
    html_rr, _ = board("rr", chip="Se inscreve · IA")
    ok('<div class="cta-news">' not in html_rr, "RR endcard: no newsletter")
    settings.newsletter_text = "Join the waitlist"
    ok(newsletter.endcard_text("os") is None, "endcard-banned text (waitlist) dropped from endcard")
    settings.newsletter_text = "Subscribe to the newsletter"
    ok(newsletter.endcard_text("os") is None, "Subscribe-CTA text dropped from endcard (chip owns Subscribe)")
    ok(theme.infer_brand(1, "owera", "Owera Software") == "os"
       and theme.infer_brand(2, "rodrigo-recio", "Rodrigo Recio") == "rr", "publish brand: ch1 os / ch2 rr")
finally:
    settings.newsletter_url, settings.newsletter_text = _u, _t

print(f"\nALL {_n} CHECKS PASSED")
