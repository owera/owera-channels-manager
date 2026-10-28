#!/usr/bin/env python3
"""OS card exit pad (CARD_PAD_FLOOR_S) + fade-at-end — generator regression."""
from __future__ import annotations

import re
import sys

sys.path.insert(0, str(__file__).rsplit("/", 2)[0] or ".")

from app.services import craft
from app.services.engines import storyboard, theme
from tests.os_gate_b_real_vo import whisper_words, snapshot_script, snapshot_duration

_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if cond:
        print(f"  ok: {msg}")
    else:
        print(f"FAIL: {msg}")
        sys.exit(1)


print("CARD_PAD_FLOOR_S + card_exits_early gate")
ok(craft.CARD_PAD_FLOOR_S == 0.20, "pad floor is 0.20s")

w1452 = whisper_words(1452)
S1452 = snapshot_script(1452)
D1452 = snapshot_duration(1452)


def _beat(typ, start, dur, **kw):
    b = {"type": typ, "start": start, "dur": dur}
    b.update(kw)
    return b


# Regression: shrink-to-next must not cut before last word + pad (1452 card 2 pattern).
board = [
    _beat("hook", 0.0, 2.0, text="One folder broke Copilot CLI.", object="terminal"),
    _beat("quote", 5.14, 2.48, text="and every Claude subagent failed on the spot."),
    _beat("quote", 8.04, 3.0, text="A maximum of 4 blocks with cache_control may be provided."),
    _beat("cta", 28.0, 2.5, text="Subscribe · Agent traps", cue="Subscribe"),
]
storyboard._snap_os_cards_to_speech(board, w1452, S1452, duration=D1452)
for i in (1, 2):
    b = board[i]
    sp = craft.spoken_span(craft._card_spoken_text(b, None), w1452)
    end = float(b["start"]) + float(b["dur"])
    ok(sp and end + 1e-6 >= float(sp[1]) + craft.CARD_PAD_FLOOR_S - craft.SYNC_TOL_S,
       f"after snap card {i} end {end:.2f} >= last {sp[1]:.2f} + pad")

# Fade starts at card end (not end − fade).
th = theme.resolve(47, "One folder broke Copilot CLI.", brand="os")
html = storyboard.build_index_html(
    board, th, "portrait", 1080, 1920, D1452,
)
m = re.search(r"tl\.to\('#b1',\{opacity:0[^}]+\},([\d.]+)\)", html)
ok(m is not None, "beat 1 has a fade-out tween")
fade_at = float(m.group(1))
end1 = float(board[1]["start"]) + float(board[1]["dur"])
ok(abs(fade_at - end1) < 0.02, f"fade starts at card end {end1:.2f} (got {fade_at:.2f})")

print("quote_not_in_vo + cli_not_in_allowlist (OS pre-check)")
bad_quote = [
    _beat("hook", 0.0, 2.0, text="One folder broke Copilot CLI.", object="terminal"),
    _beat("quote", 2.12, 2.5, text="This sentence is not in the VO at all."),
    _beat("cta", 5.0, 3.0, text="Subscribe"),
]
hits = craft.os_card_hits(bad_quote, S1452, None, words=w1452)
ok(any(h["check"] == "quote_not_in_vo" for h in hits),
   "paraphrase / invented quote → quote_not_in_vo")
allow = [{"kind": "code", "text": "Found 5.", "source": "https://example.test/log"}]
bad_code = [
    _beat("hook", 0.0, 2.0, text="One folder broke Copilot CLI.", object="terminal"),
    _beat("code", 2.12, 1.5, lines=["Invented prose on a code card."], lang="log"),
    _beat("cta", 5.0, 3.0, text="Subscribe"),
]
hits2 = craft.os_card_hits(bad_code, S1452, allow, words=w1452)
ok(any(h["check"] == "cli_not_in_allowlist" for h in hits2),
   "code line not in allowlist → cli_not_in_allowlist")

print("compose #1452/#1453: no card_exits_early on mids (whisper VO)")
def boom(*a, **k):
    raise AssertionError("LLM must not run")


for vid in (1452, 1453):
    words = whisper_words(vid)
    script = snapshot_script(vid)
    dur = snapshot_duration(vid)
    out = storyboard.compose(
        subject=f"regression {vid}",
        script=script,
        words=words,
        duration=dur,
        resolution="portrait",
        width=1080,
        height=1920,
        topic_id=47,
        content_format="short",
        language="English",
        llm=boom,
        brand="os",
        topic_name="Agent traps",
    )
    ok(out, f"#{vid} compose returns HTML")
    beats = craft.beats_from_html(out)
    early = craft.card_exits_early_hits(beats, words)
    mids = [h for h in early if h["i"] > 0 and h["i"] < len(beats) - 1]
    ok(not mids, f"#{vid} no mid card_exits_early ({mids})")
    ok(not craft.card_time_overlap_hits(beats),
       f"#{vid} no card_overlap")

print()
print(f"ALL {_checks} CHECKS PASSED")
