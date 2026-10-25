"""Code/command card wrap + code_overflow gate (#1453 c5).

    PYTHONPATH=. .venv/bin/python tests/verify_code_overflow.py
"""
import re
import sys

from app.services import craft
from app.services.engines import storyboard

_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


CTX = {"i": 3, "start": 1.0, "dur": 3.0, "is_last": False, "width": 1080, "height": 1920}
LONG = ("TypeError: The annotation 'NotRequired' is only valid inside a TypedDict "
        "body and cannot be used as a bare field annotation here")

print("wrap_code_line")
ok(craft.wrap_code_line("short", 58) == ["short"], "short line untouched")
w = craft.wrap_code_line(LONG, 58)
ok(len(w) > 1 and all(len(p) <= 58 for p in w), "long line wraps at spaces <= 58")
ok(" ".join(w) == LONG, "wrap is lossless (join == original)")
ok(craft.wrap_code_line("x" * 80, 58) == ["x" * 80], "unbroken token stays whole")

print("render_code (1453 c5 error card)")
beat = {"type": "code", "lines": [LONG], "start": 1.0, "dur": 3.0}
html, _ = storyboard.render_code(beat, dict(CTX))
spans = re.findall(r'<span class="ln">([^<]*)</span>', html)
ok(len(spans) >= 2, "error card renders as several visual lines")
ok(all(len(x) <= 58 for x in spans), "no visual line beyond the floor width")
ok("font-size:24px" in html, "font at the 24px floor")
ok(beat["lines"] == [LONG], "beat['lines'] untouched (allowlist byte match)")
html2, _ = storyboard.render_code({"type": "code", "lines": ["x = 1"], "start": 1.0, "dur": 3.0}, dict(CTX))
ok(html2.count('class="ln"') == 1, "short code unchanged: one line")
hb = {"type": "code", "lines": ["a", LONG], "highlight": [1], "start": 1.0, "dur": 3.0}
h3, _ = storyboard.render_code(hb, dict(CTX))
ok(h3.count('class="ln hl"') >= 2, "highlight applies to every wrapped piece")

print("render_command")
cb = {"type": "command", "prompt": "$", "command": "pip install foo",
      "output": [LONG], "start": 1.0, "dur": 3.0}
ch, _ = storyboard.render_command(cb, dict(CTX))
ok("<br>" in ch and ch.count('class="cmd-out"') == 1, "long output wraps inside its row")
ok(cb["output"] == [LONG], "beat output untouched")

print("gate code_overflow")
ok(craft.code_overflow_hits([beat]) == [], "wrappable long line: no overflow")
tok = {"type": "code", "lines": ["see https://example.com/" + "a" * 70]}
ok(craft.code_overflow_hits([tok]), "unbroken long token overflows")
tall = {"type": "code", "lines": [LONG] * 20}
ok(craft.code_overflow_hits([tall]), "too many visual lines overflows")
ok(craft.code_overflow_hits([{"type": "command", "command": "x" * 90}]),
   "command with unbroken long token overflows")
g = craft.video_maker_gate([tok], content_format="short")
ok(g["result"] == "FAIL" and any("code_overflow" in r for r in g["reasons"]),
   "gate FAILs closed with code_overflow reason")
ok(any(k.get("check") == "code_overflow" for k in g.get("kinds") or []),
   "kinds carries check=code_overflow")
g2 = craft.video_maker_gate([beat], content_format="short")
ok(not any("code_overflow" in r for r in g2["reasons"]), "wrapped card does not trip code_overflow")

print(f"\nALL {_checks} CHECKS PASSED")
