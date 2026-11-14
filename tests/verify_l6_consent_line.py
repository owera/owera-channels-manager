"""L6 AI-consent line on the Connect YouTube panel (LegalOps FINAL).

Run: PYTHONPATH=. .venv/bin/python tests/verify_l6_consent_line.py

The EN and PT lines (verbatim from CM-CONNECT-AI-LINE.md, sha 845c22005853)
must appear in the source before the OAuth button. The SPA has no other i18n:
this one line is PT when navigator.language (else languages[0]) starts with
"pt", and EN otherwise.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

_checks = 0
ROOT = Path(__file__).resolve().parent.parent
CHANNELS = (ROOT / "frontend/src/pages/Channels.tsx").read_text()
HELPER = ROOT / "frontend/src/l6ConnectAiLine.ts"
HELPER_SRC = HELPER.read_text()

EN = (
    "To write your drafts, Channels Manager sends your videos' titles, "
    "descriptions, tags and channel stats to an AI provider in the US, "
    "only to create your own drafts. Nothing is published until you approve it."
)
PT = (
    "Para escrever seus rascunhos, o Channels Manager envia os títulos, "
    "descrições e tags dos seus vídeos e as estatísticas do canal a um "
    "provedor de IA nos EUA, só para criar os seus próprios rascunhos. "
    "Nada é publicado sem a sua aprovação."
)


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


print("L6 consent line in source")
ok(EN in HELPER_SRC, "exact EN L6 line is in l6ConnectAiLine.ts")
ok(PT in HELPER_SRC, "exact PT L6 line is in l6ConnectAiLine.ts")
# Other screens use btn-signal first (topic/channel forms). Pin the Connect YouTube
# OAuth button, which is the one whose label is "Connect YouTube" / "Reconnect YouTube".
props_at = CHANNELS.find("l6ConnectAiLineProps(")
line_def_at = CHANNELS.find("const l6Line = ")
oauth_at = CHANNELS.find(': "Connect YouTube"}')
reconnect_at = CHANNELS.find('? "Reconnect YouTube"')
ok(props_at != -1 and line_def_at != -1 and oauth_at != -1 and reconnect_at != -1
   and props_at < oauth_at and line_def_at < oauth_at,
   "l6ConnectAiLineProps(...) / l6Line are defined in ChannelDetail, before the OAuth button")
# The line sits in a visible <p>, not a title/tooltip attribute.
line_def = CHANNELS[line_def_at:CHANNELS.find("</p>", line_def_at) + 4]
ok("<p " in line_def and "{l6.text}" in line_def and "title=" not in line_def
   and "tooltip" not in line_def.lower() and "hidden" not in line_def
   and "sr-only" not in line_def,
   "the L6 line is plain visible <p> text, not hidden behind a title/tooltip")
ok("id={l6Id}" in line_def, "the L6 paragraph carries id={l6Id}")
ok("lang={l6.lang}" in line_def, "the L6 paragraph sets lang from the picker (pt when PT shows)")

print("L6 line shows whenever a Google OAuth button is present")
# Every button that opens Google OAuth: find each window.open(auth_url ...) and the
# <button ...> that owns it.
opens = [m.start() for m in re.finditer(r"window\.open\(auth_url", CHANNELS)]
ok(len(opens) >= 1, f"found the Google OAuth opener(s) in Channels.tsx ({len(opens)})")
for i, at in enumerate(opens):
    btn_at = CHANNELS.rfind("<button", 0, at)
    ok("aria-describedby={l6Id}" in CHANNELS[btn_at:CHANNELS.find("onClick", btn_at)],
       f"OAuth button #{i + 1} has aria-describedby={{l6Id}} → the L6 paragraph")
    # That button renders unconditionally in ChannelDetail (not inside {!connected && ...}):
    ok("connected &&" not in CHANNELS[CHANNELS.rfind("\n", 0, btn_at):btn_at],
       f"OAuth button #{i + 1} is not gated on connection state (Connect and Reconnect)")
panel_at = CHANNELS.find("{!connected && (")
panel_end = CHANNELS.find(")}", CHANNELS.find("</div>", CHANNELS.find("Connect YouTube</h3>", panel_at)))
panel = CHANNELS[panel_at:panel_end]
ok(panel_at != -1 and 'l6Line("' in panel,
   "disconnected Connect YouTube panel renders the L6 line (before the button)")
reco_at = CHANNELS.find("{connected && l6Line(")
btn_row_at = CHANNELS.find("aria-describedby={l6Id}")
ok(reco_at != -1 and reco_at < btn_row_at,
   "connected channel (Reconnect YouTube) renders the L6 line before the OAuth button")
ok(CHANNELS.count("l6Line(\"") == 2 and "{!connected && (" in CHANNELS,
   "exactly one L6 paragraph renders per state (disconnected panel XOR reconnect row) → id is unique")
ok("l6ConnectAiLineId(channel.id)" in CHANNELS,
   "paragraph id is per channel (l6ConnectAiLineId(channel.id)) so two channel panels never share it")

# This cut adds no i18n framework — only the one-line picker.
suspect = [p for p in (ROOT / "frontend/src").rglob("*")
           if "node_modules" not in p.parts
           and p.suffix in {".ts", ".tsx", ".js", ".json"}
           and "i18n" in p.name.lower()
           # S2 (parent/PO 14/11): the one sanctioned minimal dictionary for the
           # wizard + banner strings (CMO copy), same locale rule as this line.
           and p.name != "wizardI18n.ts"]
ok(suspect == [], f"no other i18n modules added ({[p.name for p in suspect]})")
_wi = (ROOT / "frontend/src/wizardI18n.ts").read_text()
ok("l6ConnectAiLine" in _wi and "To write your drafts" not in _wi,
   "wizardI18n.ts reuses the L6 locale type and never carries the L6 text")
ok("i18next" not in CHANNELS and "useTranslation" not in CHANNELS
   and "react-intl" not in CHANNELS,
   "no general i18n library hooked into Channels.tsx")

print("L6 locale pick (runs the TypeScript helper)")
cases = [
    ({"language": "pt"}, "PT", "language=pt"),
    ({"language": "pt-BR"}, "PT", "language=pt-BR"),
    ({"language": "pt-PT"}, "PT", "language=pt-PT"),
    ({"language": "PT-br"}, "PT", "language=PT-br is case-insensitive"),
    ({"language": "en"}, "EN", "language=en"),
    ({"language": "en-US"}, "EN", "language=en-US"),
    ({"language": "fr-FR"}, "EN", "language=fr-FR falls back to EN"),
    ({}, "EN", "empty navigator falls back to EN"),
    (None, "EN", "missing navigator falls back to EN"),
    ({"language": "", "languages": ["pt-BR", "pt"]}, "PT", "languages[0]=pt-BR when language is empty"),
    ({"languages": ["pt"]}, "PT", "languages[0]=pt when language is absent"),
    ({"language": "en-US", "languages": ["pt-BR"]}, "EN", "language takes precedence over languages[0]"),
    ({"language": "  pt-BR  "}, "PT", "language is trimmed"),
    ({"languages": []}, "EN", "empty languages list falls back to EN"),
]

navs = [nav for nav, _expect, _label in cases]
runner = (
    "import { l6ConnectAiLine, l6ConnectAiLineProps, l6ConnectAiLineId, "
    "L6_CONNECT_AI_LINE_EN, L6_CONNECT_AI_LINE_PT } "
    "from './frontend/src/l6ConnectAiLine.ts';\n"
    f"const cases = {json.dumps(navs)};\n"
    "const out = cases.map((nav) => {\n"
    "  const text = l6ConnectAiLine(nav);\n"
    "  if (text === L6_CONNECT_AI_LINE_PT) return 'PT';\n"
    "  if (text === L6_CONNECT_AI_LINE_EN) return 'EN';\n"
    "  return 'OTHER';\n"
    "});\n"
    "process.stdout.write(JSON.stringify({ out, "
    "props: cases.map((nav) => l6ConnectAiLineProps(nav)), "
    "ids: [l6ConnectAiLineId(1), l6ConnectAiLineId(2), l6ConnectAiLineId(1)] }));\n"
)
proc = subprocess.run(
    ["node", "--experimental-strip-types", "--no-warnings", "--input-type=module", "-e", runner],
    cwd=ROOT, capture_output=True, text=True,
)
ok(proc.returncode == 0, f"node ran l6ConnectAiLine.ts ({proc.stderr.strip()[:300]})")
res = json.loads(proc.stdout)
got = res["out"]
ok(len(got) == len(cases), "one result per locale case")
for (nav, expect, label), actual in zip(cases, got):
    ok(actual == expect, f"{label} → {expect} (got {actual})")

print("L6 paragraph lang + id")
for (nav, expect, label), props in zip(cases, res["props"]):
    want_lang = "pt" if expect == "PT" else None
    want_text = PT if expect == "PT" else EN
    ok(props.get("lang") == want_lang and props.get("text") == want_text,
       f"{label}: lang={want_lang!r} with the {expect} text")
ids = res["ids"]
ok(ids[0] == "l6-connect-ai-line-1" and ids[0] != ids[1] and ids[0] == ids[2],
   "paragraph id is stable per channel and differs between channels")

print(f"\n{_checks} checks passed")
