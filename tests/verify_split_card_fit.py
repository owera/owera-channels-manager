"""Split-card fit (Designer council 06/10, FINDINGS problem 1).

Run: PYTHONPATH=. .venv/bin/python tests/verify_split_card_fit.py

9 of 22 split cards on air ran past 72% of the height (#1408 text to 88.6%):
each card picked its own font against 26% of the height and the pair was
stacked with no global fit; on frame0 the top card also started at 5–6%.
craft.split_card_markup now lays the pair out — for BOTH the thumb and
frame0 — in one 9%–72% box with 8% lateral padding, and the two cards share
ONE font size that shrinks step-wise until the estimated block fits.
"""
import re
import sys

from app.services import craft, thumbnail
from app.services.engines import storyboard, theme

_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


SIZES = ((1080, 1920), (720, 1280))
# Designer FINDINGS: the 9 over-72% cards (7 text + 2 border-only) …
OVERFLOW = [
    "Chat chose daily digest. Prod sent each alert. · Agent memory 46",
    "Chat used account Ana. Prod posted as Bob. · Agent memory 43",
    "Chat expired invite link. Prod left it live. · Agent memory 48",
    "Chat added suite 400. Prod omitted it. · Agent memory 54",
    "Chat updated ZIP 10001. Prod kept 90210. · Agent memory 39",
    "Chat selected work profile. Prod used home. · Agent memory 58",
    "Chat noted allergy nuts. Prod plated almonds. · Agent memory 41",
    "Chat stored UTC. Prod showed PST. · Agent memory 56",
    "Chat set quiet hours. Prod texted 2am. · Agent memory 60",
]
# … plus short/long pairs and the IA verdict split.
SHORT = "Chat stored UTC. Prod showed PST. · Agent memory 56"
LONG = "Chat renamed config.yaml. Prod read the old path. · Agent memory 49"
PAIRS = {
    "short/short": {"top_label": "Chat", "top": "UTC.", "bottom_label": "Prod", "bottom": "PST."},
    "short/short 2": {"top_label": "Chat", "top": "kept 7.", "bottom_label": "Prod", "bottom": "kept 90."},
    "long/short": {"top_label": "Chat", "top": "declined the extended warranty on the order.",
                   "bottom_label": "Prod", "bottom": "added it."},
    "short/long": {"top_label": "Chat", "top": "said no.",
                   "bottom_label": "Prod", "bottom": "shipped the whole migration to every region anyway."},
    "long/long": {"top_label": "Chat", "top": "chose the daily digest for every channel and user.",
                  "bottom_label": "Prod", "bottom": "sent each single alert as its own push notification."},
    "ia verdict": craft.contrast_split("Jogar o node_modules no contexto não é engenharia. · IA 206"),
    "no labels": {"top_label": "", "top": "The preview passed.", "bottom_label": "",
                  "bottom": "The live click timed out."},
}


def fonts(markup):
    return [int(x) for x in re.findall(r'class="sc-text" style="font-size:(\d+)px"', markup)]


def inner_w(w, h):
    box = craft.split_box(w, h)
    return box["width"] - 2 * w * craft.SPLIT_CARD_PAD_X_FRAC - 2 * craft.SPLIT_BORDER_PX


print("box")
for w, h in SIZES:
    b = craft.split_box(w, h)
    ok(abs(b["top"] / h - 0.09) < 1e-9 and abs((b["top"] + b["height"]) / h - 0.72) < 1e-9
       and abs(b["left"] / w - 0.08) < 1e-9 and abs(b["width"] / w - 0.84) < 1e-9,
       f"{w}x{h}: box is 9%–72% of the height, 8% padding each side")

print("fit: every pair, both sizes")
for name, sp in PAIRS.items():
    for w, h in SIZES:
        f = craft.split_fit(sp, w, h)
        b = f["box"]
        ok(f["fits"] and f["block_h"] <= b["height"] + 1e-6
           and (b["top"] + f["block_h"]) / h <= 0.72 + 1e-9,
           f"{name} @{w}x{h}: block ends at {100 * (b['top'] + f['block_h']) / h:.1f}% (≤72%), px={f['px']}")
        words = [p for t in (sp["top"], sp["bottom"]) for wd in t.split()
                 for p in re.split(r"(?<=[_/])", wd) if p]
        ok(max(craft._split_word_em(x) for x in words) * f["px"] <= inner_w(w, h) + 1e-6,
           f"{name} @{w}x{h}: the longest word fits one line at the shared size")

print("Designer overflow titles now fit (estimate)")
for t in OVERFLOW:
    sp = craft.contrast_split(t)
    for w, h in SIZES:
        f = craft.split_fit(sp, w, h)
        ok(f["fits"] and (f["box"]["top"] + f["block_h"]) / h <= 0.72 + 1e-9,
           f"{t.split(' · ')[1]} @{w}x{h}: {100 * (f['box']['top'] + f['block_h']) / h:.1f}% ≤ 72%")

print("shared size, step-wise shrink")
for name, sp in PAIRS.items():
    mk = craft.split_card_markup(sp, 1080, 1920)
    fs = fonts(mk)
    ok(len(fs) == 2 and fs[0] == fs[1] == craft.split_fit(sp, 1080, 1920)["px"],
       f"{name}: both cards render at the same font size ({fs})")
ll = craft.split_fit(PAIRS["long/long"], 1080, 1920)
ss = craft.split_fit(PAIRS["short/short"], 1080, 1920)
ok(ll["steps"] > 0 and ss["steps"] == 0 and ll["px"] < ss["px"],
   f"long pair shrinks step-wise ({ll['steps']} steps → {ll['px']}px); short pair keeps its size ({ss['px']}px)")
start = min(craft.split_font_px(PAIRS["long/long"]["top"], 1080, 1920),
            craft.split_font_px(PAIRS["long/long"]["bottom"], 1080, 1920))
ok(ll["px"] <= int(start * craft.SPLIT_FONT_STEP ** ll["steps"]) + 1,
   "each step is a SPLIT_FONT_STEP shrink from the smaller per-card size")
ls = craft.split_fit(PAIRS["long/short"], 1080, 1920)
ok(fonts(craft.split_card_markup(PAIRS["long/short"], 1080, 1920))[1] == ls["px"],
   "a short card next to a long one shrinks with it (one size for the pair)")
ok(craft.split_card_height("Chat", "a b c d e f g h", 120, 1080, 1920)
   <= craft.split_card_height("Chat", "a b c d e f g h", 200, 1080, 1920),
   "card height estimate grows with the font size")
ok(craft.split_text_lines("NUM_PARALLEL multiplica", 150, 300) >= 2
   and craft.split_text_lines("", 150, 300) == 0, "line estimate wraps; empty text = 0 lines")
huge = {"top_label": "Chat", "top": "word " * 120, "bottom_label": "Prod", "bottom": "word " * 120}
fh = craft.split_fit(huge, 1080, 1920)
ok(fh["fits"] is False and fh["px"] >= int(1080 * craft.SPLIT_FONT_MIN_FRAC) - 1,
   "absurd text stops at the font floor and reports fits=False (no loop, no crash)")

print("both outputs: thumb + frame0")
for name, t in (("short", SHORT), ("long", LONG), ("#1408", OVERFLOW[0])):
    sp = craft.contrast_split(t)
    th_html = thumbnail._thumbnail_html(sp["head"], brand="os", split=sp)
    rw = int(re.search(r'data-width="(\d+)"', th_html).group(1))
    rh = int(re.search(r'data-height="(\d+)"', th_html).group(1))
    m = re.search(r"#stage\{position:absolute;left:0;right:0;top:(\d+)px;height:(\d+)px;padding:0 (\d+)px", th_html)
    ok(m and abs(int(m.group(1)) - int(rh * 0.09)) <= 1 and abs(int(m.group(1)) + int(m.group(2)) - int(rh * 0.72)) <= 1
       and int(m.group(3)) == int(rw * 0.08),
       f"thumb {name} @{rw}x{rh}: #stage = 9%–72% box, 8% lateral padding")
    tf = fonts(th_html)
    ok(len(tf) == 2 and tf[0] == tf[1] == craft.split_fit(sp, rw, rh)["px"],
       f"thumb {name}: the two cards share one font size ({tf})")
    beat = {"type": "hook", "text": sp["head"], "start": 0.0, "dur": 3.0, "split": sp}
    f0 = storyboard.build_index_html([beat], theme.resolve(1, sp["head"], brand="os"),
                                     "portrait", 1080, 1920, 3.0)
    mm = re.search(r"\.hook\[data-split\]\{inset:auto;left:0;right:0;top:(\d+)px;height:(\d+)px;"
                   r"justify-content:flex-start;padding:0 (\d+)px;", f0)
    ok(mm and int(mm.group(1)) == int(1920 * 0.09) and int(mm.group(1)) + int(mm.group(2)) == int(1920 * 0.72)
       and int(mm.group(3)) == int(1080 * 0.08),
       f"frame0 {name}: split hook = same 9%–72% box (was 5–6% top), 8% lateral padding")
    ff = fonts(f0)
    ok(len(ff) == 2 and ff[0] == ff[1] == craft.split_fit(sp, 1080, 1920)["px"],
       f"frame0 {name}: the two cards share one font size ({ff})")
    ok('data-split="1"' in f0 and craft.split_card_markup(sp, 1080, 1920) in f0,
       f"frame0 {name}: frame0 carries the shared split markup")
    if (rw, rh) == (1080, 1920):
        ok(craft.split_card_markup(sp, rw, rh) in th_html, f"thumb {name}: same markup as frame0 (frame0 ≡ thumb)")

print("markup")
mk = craft.split_card_markup(PAIRS["long/long"], 1080, 1920)
ok(f'gap:{int(1920 * craft.SPLIT_GAP_FRAC)}px' in mk and f'min-height:{int(1920 * craft.SPLIT_CARD_MIN_H_FRAC)}px' in mk,
   "gap and min-height are explicit px (the fit estimate matches the layout)")
ok("min-height:28%" not in craft.SPLIT_CSS and "gap:3.2%" not in craft.SPLIT_CSS,
   "old per-card 28% min-height / 3.2% gap removed from SPLIT_CSS")
ok(2 * craft.SPLIT_CARD_MIN_H_FRAC + craft.SPLIT_GAP_FRAC <= 0.72 - 0.09,
   "two min-height cards + gap always fit the box")
ok(f'data-font="{craft.split_fit(PAIRS["long/long"], 1080, 1920)["px"]}"' in mk, "markup records the shared size")

print()
print(f"ALL {_checks} CHECKS PASSED")
