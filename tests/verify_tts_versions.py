"""Dotted version numbers in the VO are spoken as versions, never as dates
(VM FAIL P0 #1449 08/10: edge-tts read "3.20.21" as "March 20th, 21").

Run: PYTHONPATH=. .venv/bin/python tests/verify_tts_versions.py

Pins:
  * the 3 exact cases: 3.20.21 / 1.15.19 / 1.2.13 → "three point twenty point
    twenty-one" / "one point fifteen point nineteen" / "one point two point
    thirteen" — no digits left, no month/ordinal, i.e. never a date;
  * v-prefixed 2-part and 3-part, leading zeros, trailing punctuation;
  * ordinary decimals, prices, percentages, 2-part numbers (Python 3.10),
    URLs and paths are unchanged; English rule global (every EN voice / None);
  * PT voices (CoS 08/10 15:27, RR PT): 3.20.21 / 1.15.19 / 1.2.13 → "três
    ponto vinte ponto vinte e um" / "um ponto quinze ponto dezenove" / "um
    ponto dois ponto treze", bare "3.20" → "três ponto vinte" — never a date
    (no digit, no month, no "de"); thousands grouping, money (R$), % / x and
    URLs unchanged; edge-tts words merge back to the digits;
  * worker._tts: edge-tts gets the spoken form, the returned words carry the
    displayed "3.20.21" (one word, first start → last end), so cards / cue
    alignment / claim timing keep the digits; display text never rewritten.
"""
import re
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import edge_tts as _edge_tts

from app.config import settings
from app.services import craft
from app.services.engines import worker

_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


def not_a_date(spoken: str, version_words: str) -> bool:
    return (not re.search(r"\d", version_words)
            and not re.search(r"(?i)january|february|march|april|may|june|july|august|september|"
                              r"october|november|december|\b(?:twentieth|first|second|third|"
                              r"fifteenth|nineteenth|thirteenth)\b", spoken))


print("the three cases (never dates)")
for disp, spoken in [
    ("The fix: update Cursor to 3.20.21 or higher.",
     "The fix: update Cursor to three point twenty point twenty-one or higher."),
    ("CrewAI 1.15.19 fixed the role order.",
     "CrewAI one point fifteen point nineteen fixed the role order."),
    ("It's fixed in langgraph 1.2.13.",
     "It's fixed in langgraph one point two point thirteen."),
]:
    got = craft.tts_spoken_text(disp, "en-US-AndrewNeural")
    ok(got == spoken, f"{disp!r} → {got!r}")
    vw = got.split(" point ")[0].split()[-1] + " point " + " point ".join(got.split(" point ")[1:])
    ok(not_a_date(got, vw), "  …no digits, no month, no ordinal: not a date")

print("forms")
ok(craft.version_spoken("v2.1.283") == "version two point one point two hundred eighty-three",
   "v2.1.283 → 'version two point one point two hundred eighty-three'")
ok(craft.tts_spoken_text("Claude Code v2.1 shipped.") == "Claude Code version two point one shipped.",
   "v-prefixed 2-part → spoken version")
ok(craft.tts_spoken_text("Copilot CLI 1.0.73 changelog") == "Copilot CLI one point zero point seventy-three changelog",
   "zero part spoken 'zero'")
ok(craft.version_spoken("1.05.2") == "one point zero five point two", "leading-zero part spelled digit by digit")
ok(craft.tts_spoken_text("Use 3.20.21+, or newer.") == "Use three point twenty point twenty-one+, or newer.",
   "trailing + / punctuation kept")

print("unchanged: decimals, prices, 2-part, URLs, PT")
for t in ["It costs $3.20 a month.", "2.5x faster at 99.9% uptime.", "Python 3.10 and Python 3.13.",
          "It took 1.5 seconds.", "See https://github.com/x/releases/tag/1.2.13 now.",
          "Open /opt/app/1.2.3/bin first.", "Pay €1.20.", "MCP Python SDK 2.1"]:
    ok(craft.tts_spoken_text(t, "en-US-AndrewNeural") == t, f"{t!r} unchanged")
for v in ("en-US-AndrewNeural", "en-GB-RyanNeural", "en-AU-WilliamNeural", None):
    ok(craft.tts_spoken_text("Update to 3.20.21.", v) == "Update to three point twenty point twenty-one.",
       f"English rule is global ({v})")
ok(craft.tts_spoken_text("n_batch on Owera 3.20.21") == "n batch on Oh-weh-ruh three point twenty point twenty-one",
   "composes with the identifier + brand rules")


print("PT voices (RR PT)")
PT = "pt-BR-AntonioNeural"
_MONTHS_PT = ("janeiro fevereiro março abril maio junho julho agosto setembro outubro "
              "novembro dezembro").split()
for disp, spoken in (("3.20.21", "três ponto vinte ponto vinte e um"),
                     ("1.15.19", "um ponto quinze ponto dezenove"),
                     ("1.2.13", "um ponto dois ponto treze")):
    got = craft.tts_spoken_text(f"Atualize para {disp} hoje.", PT)
    ok(got == f"Atualize para {spoken} hoje.", f"PT {disp} → {spoken!r} ({got!r})")
    ok(not re.search(r"\d", got) and not any(m in got for m in _MONTHS_PT)
       and not re.search(r"\b(de|primeiro)\b", got), f"PT {disp}: never a date (no digit / month / 'de')")
ok(craft.tts_spoken_text("O 3.20 saiu.", PT) == "O três ponto vinte saiu.", "PT bare 2-part '3.20' → 'três ponto vinte'")
ok(craft.tts_spoken_text("Use a v2.1 ou 3.05.", PT) == "Use a versão dois ponto um ou três ponto zero cinco.",
   "PT v-prefix → 'versão', leading zero digit by digit")
ok(craft.tts_spoken_text("Versão 2.1.283.", PT) == "Versão dois ponto um ponto duzentos e oitenta e três.",
   "PT hundreds ('duzentos e oitenta e três')")
for t in ["São 1.500 usuários e 2.000.000 de linhas.", "Custa R$ 3.20 por mês.", "Subiu 2.5% e ficou 2.5x.",
          "Veja https://github.com/x/releases/tag/1.2.13 agora.", "Abra /opt/app/1.2.3/bin."]:
    ok(craft.tts_spoken_text(t, PT) == t, f"PT {t!r} unchanged")
pw = craft.remerge_tts_words([{"text": t_, "start": i * 0.3, "dur": 0.25} for i, t_ in enumerate(
    "atualize para três ponto vinte ponto vinte e um agora".split())], "atualize para 3.20.21 agora", PT)
ok([x["text"] for x in pw] == ["atualize", "para", "3.20.21", "agora"]
   and abs(pw[2]["start"] - 0.6) < 1e-6 and abs(pw[2]["start"] + pw[2]["dur"] - (8 * 0.3 + 0.25)) < 1e-6,
   "PT edge-tts words merge back to '3.20.21' (first start → last end)")
ok(craft.tts_spoken_text("A IA da 3.20.21", PT) == "A I-A da três ponto vinte ponto vinte e um",
   "PT composes with the IA lexicon")


print("worker._tts: spoken to edge-tts, displayed in words")


class _SpeakComm:
    seen: list = []

    def __init__(self, text, voice, **kw):
        _SpeakComm.seen.append(text)
        self._text = text

    async def stream(self):
        t = 0.0
        for w in self._text.split():
            yield {"type": "WordBoundary", "text": w.strip(".,"), "offset": t * 1e7,
                   "duration": 0.3 * 1e7}
            t += 0.35
        yield {"type": "audio", "data": b"ID3fake"}


_att = settings.tts_attempts
settings.tts_attempts = 1
try:
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "n.mp3"
        disp = "The fix: update Cursor to 3.20.21 or higher."
        _SpeakComm.seen = []
        with patch.object(_edge_tts, "Communicate", _SpeakComm):
            w = worker._tts(disp, "en-US-AndrewNeural", out)
        ok(_SpeakComm.seen == ["The fix: update Cursor to three point twenty point twenty-one or higher."],
           "edge-tts gets the spoken version")
        texts = [x["text"] for x in w]
        ok(texts == ["The", "fix:", "update", "Cursor", "to", "3.20.21", "or", "higher"],
           f"words carry the displayed '3.20.21' as ONE word ({texts})")
        v = w[5]
        ok(abs(v["start"] - 5 * 0.35) < 1e-6 and abs(v["start"] + v["dur"] - (9 * 0.35 + 0.3)) < 1e-6,
           "merged word spans 'three' start → 'twenty-one' end")
        ok(abs(craft.claim_spoken_end("The fix: update Cursor to 3.20.21 or higher.", w)
               - (11 * 0.35 + 0.3)) < 1e-6,
           "claim timing measured against the displayed text still matches")
        # edge-tts may split "twenty-one" into two boundaries
        words = [{"text": t_, "start": i * 0.3, "dur": 0.25} for i, t_ in enumerate(
            "update to three point twenty point twenty one now".split())]
        mw = craft.remerge_tts_words(words, "update to 3.20.21 now", "en-US-AndrewNeural")
        ok([x["text"] for x in mw] == ["update", "to", "3.20.21", "now"],
           "split 'twenty' 'one' boundaries also merge back")
        two = craft.remerge_tts_words(
            [{"text": t_, "start": i * 0.3, "dur": 0.25} for i, t_ in enumerate(
                "one point fifteen point nineteen then one point two point thirteen".split())],
            "1.15.19 then 1.2.13", None)
        ok([x["text"] for x in two] == ["1.15.19", "then", "1.2.13"], "two versions in one line both merge")
        ok(craft.remerge_tts_words([{"text": "costs", "start": 0, "dur": .2}, {"text": "$3.20", "start": .3, "dur": .2}],
                                   "costs $3.20", None)[1]["text"] == "$3.20", "prices untouched in words")
finally:
    settings.tts_attempts = _att

print("cards keep digits")
ok("tts_spoken_text" not in open(Path(worker.__file__).parent / "storyboard.py").read(),
   "the storyboard (cards) never sees the spoken form")
import inspect  # noqa: E402
src = inspect.getsource(worker._tts)
ok("display_text = text" in src and "remerge_tts_words(words, display_text, voice)" in src,
   "worker keeps the display text for the returned words")

print()
print(f"ALL {_checks} CHECKS PASSED")
