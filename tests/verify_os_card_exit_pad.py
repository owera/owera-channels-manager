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
_tok = storyboard._OS_VO_CARDS.set(True)
try:
    storyboard._snap_os_cards_to_speech(board, w1452, S1452, duration=D1452)
finally:
    storyboard._OS_VO_CARDS.reset(_tok)
for i in (1, 2):
    b = board[i]
    sp = craft.spoken_span(craft._card_spoken_text(b, None), w1452)
    end = float(b["start"]) + float(b["dur"])
    floor = float(sp[1])
    if i == 1:
        # Next card is speech-anchored on the CAPIError quote (8.04); pad may truncate.
        nxt_sp = craft.spoken_span(craft._card_spoken_text(board[2], None), w1452)
        pad_cap = float(nxt_sp[0]) - craft.BEAT_GAP_S if nxt_sp else floor + craft.CARD_PAD_FLOOR_S
        floor = max(floor, min(floor + craft.CARD_PAD_FLOOR_S, pad_cap))
    else:
        floor = floor + craft.CARD_PAD_FLOOR_S
    ok(sp and end + 1e-6 >= floor - craft.SYNC_TOL_S,
       f"after snap card {i} end {end:.2f} >= min hold floor {floor:.2f}")

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
forum_line = "Can you check that you're on 3.20.21 or higher? It should be fixed there!"
allow_quote = [{"kind": "quote", "text": forum_line, "source": "https://forum.example/raw/1"}]
allowlisted_not_vo = [
    _beat("hook", 0.0, 2.0, text="One folder broke Copilot CLI.", object="terminal"),
    _beat("quote", 2.12, 2.5, text=forum_line),
    _beat("cta", 5.0, 3.0, text="Subscribe"),
]
hits_q = craft.os_card_hits(allowlisted_not_vo, S1452, allow_quote, words=w1452)
ok(any(h["check"] == "quote_not_in_vo" for h in hits_q),
   "allowlisted forum quote not in VO → quote_not_in_vo (no allowlist bypass)")
ok(not craft.card_is_literal(allowlisted_not_vo[1], S1452, allow_quote, words=w1452),
   "card_is_literal: quote type requires VO substring, not allowlist alone")
ok(not craft._part_is_literal("text", forum_line, S1452, allow_quote, typ="quote"),
   "_part_is_literal: quote body ignores allowlist")
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


ALLOW1452 = [
    {"kind": "quote", "text": "A maximum of 4 blocks with cache_control may be provided.",
     "source": "https://example.test"},
    {"kind": "log", "text": "Found 5.", "source": "https://example.test"},
]

print("TTS whisper pins: #1451 c2/c9 + #1458 c8 (1452 board) end ≥ last_word + pad")
_words1452 = whisper_words(1452)
_out1452 = storyboard.compose(
    subject="acceptance pins",
    script=S1452,
    words=_words1452,
    duration=D1452,
    resolution="portrait",
    width=1080,
    height=1920,
    topic_id=47,
    content_format="short",
    language="English",
    llm=boom,
    brand="os",
    topic_name="Agent traps",
    on_screen_allow=ALLOW1452,
)
_beats1452 = craft.beats_from_html(_out1452)
def _min_exit_floor(b, board, idx, words):
    sp = craft.spoken_span(craft._card_spoken_text(b, None), words)
    if not sp:
        return None
    floor = float(sp[1]) + craft.CARD_PAD_FLOOR_S
    if idx + 1 < len(board):
        nxt = board[idx + 1]
        if (nxt.get("type") or "") not in craft.CTA_TYPES:
            nsp = craft.spoken_span(craft._card_spoken_text(nxt, None), words)
            if nsp is not None:
                try:
                    nst = float(nxt.get("start") or 0)
                except (TypeError, ValueError):
                    nst = None
                if nst is not None and abs(nst - float(nsp[0])) <= craft.SYNC_TOL_S + 1e-6:
                    pad_cap = float(nsp[0]) - craft.BEAT_GAP_S
                    floor = max(float(sp[1]), min(floor, pad_cap))
    return sp, floor


for idx, label in ((2, "#1451 c2"), (8, "#1458 c8"), (9, "#1451 c9")):
    b = _beats1452[idx]
    sp, floor = _min_exit_floor(b, _beats1452, idx, _words1452)
    end = float(b["start"]) + float(b["dur"])
    ok(sp and end + 1e-6 >= floor - craft.SYNC_TOL_S,
       f"{label} card {idx} end {end:.2f} ≥ hold floor {floor:.2f} (last {sp[1]:.2f})")

print("Gate B #1451 beat[9]: incl-fade uses BEAT_GAP_S (not CARD_PAD_FLOOR_S)")
# Staging #1451 Gate B visual PASS: mid quote hold 2.825s + 0.12s fade = 2.945s.
# False FAIL was hold 2.83 + CARD_PAD_FLOOR_S (0.20) treated as fade → 3.03s.
_hold9 = 2.825
_staging9 = [
    _beat("hook", 0.0, 2.5, text="One folder broke Copilot CLI.", object="terminal"),
    _beat("stat", 2.62, 2.0, value="1", label="x"),
    _beat("quote", 4.74, _hold9, text="Subagent came back empty?", plain=True),
    _beat("cta", 7.695, 3.0, text="Subscribe · Agent traps", cue="Subscribe"),
]
_card9 = _hold9 + craft.BEAT_GAP_S
ok(_card9 <= craft.MID_CARD_MAX_S + 1e-6,
   f"#1451 beat[9] card {_card9:.2f}s = hold {_hold9:.2f}s + fade {craft.BEAT_GAP_S:.2f}s ≤ 3.0")
ok(_hold9 + craft.CARD_PAD_FLOOR_S > craft.MID_CARD_MAX_S + 1e-6,
   "regression: pad-as-fade accounting (hold+0.20) falsely exceeded 3.0s cap")
_g1451 = craft.video_maker_gate(_staging9, beat_timing=craft.BEAT_TIMING_CURRENT)
ok(_g1451["checks"]["B"] == "PASS",
   "#1451 beat[9] Gate B incl-fade PASS (BEAT_GAP_S fade, not CARD_PAD_FLOOR_S pad)")

print("blank gap budget ≤ 0.62s (0.5s stale + 0.12s fade) on #1452 compose")
_blank_max = craft.BLANK_GAP_MAX_S + craft.BEAT_GAP_S
for i in range(len(_beats1452) - 1):
    a_end = float(_beats1452[i]["start"]) + float(_beats1452[i]["dur"])
    b_start = float(_beats1452[i + 1]["start"])
    extra = b_start - a_end - craft.BEAT_GAP_S
    ok(extra <= _blank_max + 1e-6,
       f"gap after card {i} extra {extra:.2f}s ≤ {_blank_max:.2f}s")

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
        on_screen_allow=ALLOW1452 if vid == 1452 else None,
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
