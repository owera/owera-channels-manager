"""L6 AI-consent line on the Connect YouTube panel (LegalOps FINAL).

Run: PYTHONPATH=. .venv/bin/python tests/verify_l6_consent_line.py

The EN and PT lines (verbatim from CM-CONNECT-AI-LINE.md, sha 845c22005853)
must appear in the source before the OAuth button. The SPA has no other i18n:
this one line is PT when navigator.language (else languages[0]) starts with
"pt", and EN otherwise.
"""
from __future__ import annotations

import json
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
call_at = CHANNELS.find("{l6ConnectAiLine(")
oauth_at = CHANNELS.find(': "Connect YouTube"}')
ok(call_at != -1 and oauth_at != -1 and call_at < oauth_at,
   "l6ConnectAiLine(...) is called in the Connect YouTube panel, before the OAuth button")
# The call sits in a visible <p>, not a title/tooltip attribute.
around = CHANNELS[call_at - 160:call_at]
ok("<p " in around and "title=" not in around and "tooltip" not in around.lower(),
   "the L6 line is plain visible text, not hidden behind a title/tooltip")
# This cut adds no i18n framework — only the one-line picker.
suspect = [p for p in (ROOT / "frontend/src").rglob("*")
           if "node_modules" not in p.parts
           and p.suffix in {".ts", ".tsx", ".js", ".json"}
           and "i18n" in p.name.lower()]
ok(suspect == [], f"no other i18n modules added ({[p.name for p in suspect]})")
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
    "import { l6ConnectAiLine, L6_CONNECT_AI_LINE_EN, L6_CONNECT_AI_LINE_PT } "
    "from './frontend/src/l6ConnectAiLine.ts';\n"
    f"const cases = {json.dumps(navs)};\n"
    "const out = cases.map((nav) => {\n"
    "  const text = l6ConnectAiLine(nav);\n"
    "  if (text === L6_CONNECT_AI_LINE_PT) return 'PT';\n"
    "  if (text === L6_CONNECT_AI_LINE_EN) return 'EN';\n"
    "  return 'OTHER';\n"
    "});\n"
    "process.stdout.write(JSON.stringify(out));\n"
)
proc = subprocess.run(
    ["node", "--experimental-strip-types", "--no-warnings", "--input-type=module", "-e", runner],
    cwd=ROOT, capture_output=True, text=True,
)
ok(proc.returncode == 0, f"node ran l6ConnectAiLine.ts ({proc.stderr.strip()[:300]})")
got = json.loads(proc.stdout)
ok(len(got) == len(cases), "one result per locale case")
for (nav, expect, label), actual in zip(cases, got):
    ok(actual == expect, f"{label} → {expect} (got {actual})")

print(f"\n{_checks} checks passed")
