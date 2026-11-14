"""S2 — single Terms + Privacy checkbox (LegalOps FIX4, CMO sha 6a389cd4d636, 14/11) + PT gate.

Run: PYTHONPATH=. uv run python tests/verify_terms_consent.py

Pure checks (no network, no DB): the LegalOps sentences are verbatim on both
sides, ONE checkbox in the wizard consents step, unchecked by default, links on
the document names, sha of the exact sentence, and the PT gate
(MANAGER_TERMS_PT_LIVE=false → PT locale sees the EN sentence + EN links; the server
accepts only the EN sha). The API side (400 unchecked, 409 unknown / PT sha,
terms_accepted_version + locale stored) is in tests/verify_onboarding_wizard.py.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from app.services import terms_consent as tc

ROOT = Path(__file__).resolve().parent.parent
_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


print("LegalOps sentences (FIX4, CMO sha 6a389cd4d636)")
ok(tc.TEXT_EN == "I accept the Owera Terms of Service and have read the Privacy Notice.", "EN verbatim")
ok(tc.TEXT_PT == "Aceito os Termos de Serviço da Owera e li o Aviso de Privacidade.", "PT verbatim")
ok(tc.SHA_EN == hashlib.sha256(tc.TEXT_EN.encode()).hexdigest()
   and tc.SHA_PT == hashlib.sha256(tc.TEXT_PT.encode()).hexdigest(), "server shas = sha256 of the sentences")
cmo = Path("/workspace/cm-onboarding-wizard-2026-11-14/CM-WIZARD-COPY.md")
if cmo.exists():
    raw = cmo.read_text()
    ok(f'EN: "{tc.TEXT_EN}"' in raw and f'PT: "{tc.TEXT_PT}"' in raw, "same strings as the LegalOps FIX4 (CMO file)")
ts = (ROOT / "frontend/src/termsConsent.ts").read_text()
ok(json.dumps(tc.TEXT_EN) in ts and json.dumps(tc.TEXT_PT, ensure_ascii=False) in ts, "SPA carries the same sentences")
ok(f'"{tc.SHA_EN}"' in ts and f'"{tc.SHA_PT}"' in ts, "SPA shas = server shas")


def parts(block):
    return [(json.loads('"' + t + '"'), h) for t, h in
            re.findall(r'\{ text: "((?:[^"\\]|\\.)*)"(?:, href: "([^"]+)")? \}', block)]


en_block = ts[ts.index("const EN: TermsCopy"):ts.index("const PT: TermsCopy")]
pt_block = ts[ts.index("const PT: TermsCopy"):ts.index("export function termsConsent")]
pe, pp = parts(en_block), parts(pt_block)
ok("".join(t for t, _ in pe) == tc.TEXT_EN and "".join(t for t, _ in pp) == tc.TEXT_PT,
   "rendered parts join to exactly the sentence (before + link + middle + link + after)")
ok([(t, h) for t, h in pe if h] == [("Terms of Service", tc.TERMS_URL_EN), ("Privacy Notice", tc.PRIVACY_URL_EN)],
   "EN links on 'Terms of Service' and 'Privacy Notice'")
ok([(t, h) for t, h in pp if h] == [("Termos de Serviço", "https://owera.com/terms/pt/"),
                                    ("Aviso de Privacidade", tc.PRIVACY_URL_PT)],
   "PT links on 'Termos de Serviço' (→ owera.com/terms/pt/) and 'Aviso de Privacidade'")

print("single checkbox, unchecked by default")
wiz = (ROOT / "frontend/src/pages/Wizard.tsx").read_text()
cons = wiz[wiz.index("function ConsentsStep"):wiz.index("function FirstDraftStep")]
ok(cons.count('type="checkbox"') == 1 and "<GoogleLimitedUseBox" in cons,
   "ONE Terms + Privacy checkbox; the AI-data box stays separate (GoogleLimitedUseBox)")
ok("setPrivacy" not in cons and "privacy" not in cons.replace("Privacy", ""),
   "no separate Privacy checkbox / state")
ok("const [terms, setTerms] = useState(false)" in cons and "checked={terms}" in cons and "defaultChecked" not in cons,
   "unchecked by default")
ok("terms_text_sha: tc.sha, terms_locale: tc.locale" in cons, "sends the sha + locale of the sentence shown")
ok("disabled={!(terms && ai)" in cons, "Continue needs both boxes")
ok(tc.TEXT_EN not in (ROOT / "frontend/src/wizardI18n.ts").read_text(), "sentence not in the CMO dictionary")

print("PT gate (MANAGER_TERMS_PT_LIVE, server flag → SPA)")
from types import SimpleNamespace as NS  # noqa: E402
from app.config import Settings as _AppSettings, settings  # noqa: E402
from app.services import llm_guard  # noqa: E402
ok(_AppSettings.model_fields["terms_pt_live"].default is False and settings.terms_pt_live is False,
   "MANAGER_TERMS_PT_LIVE defaults to false")
with patch.dict("os.environ", {"MANAGER_TERMS_PT_LIVE": "true"}):
    ok(_AppSettings().terms_pt_live is True, "MANAGER_TERMS_PT_LIVE=true is read from the env")
ok("export const TERMS_PT_LIVE" not in ts and "= TERMS_PT_LIVE" not in ts and "export function termsConsent(nav: L6Locale | undefined, ptLive: boolean)" in ts,
   "SPA: no hard-coded gate constant; the flag is a required argument")
ok("return pt && ptLive === true ? PT : EN;" in ts, "SPA: PT sentence only when the server flag is exactly true")
ok("termsConsent(navLocale(), state.terms?.pt_live === true)" in wiz,
   "wizard reads the gate from the API (GET /api/onboarding/wizard → terms.pt_live; missing = closed)")
ok('terms: { pt_live: boolean; accepted_shas:' in (ROOT / "frontend/src/api.ts").read_text(), "WizardState.terms typed")
ok(tc.displayed_locale("pt-BR") == "en" and tc.displayed_locale("en-US") == "en", "server: pt-BR → EN sentence")
ok(tc.known_shas() == {tc.SHA_EN: "en"}, "server accepts only the EN sha")
ok(tc.public_flags() == {"pt_live": False, "accepted_shas": {"en": tc.SHA_EN}}, "API flag: pt_live false, EN sha only")
for sha, loc, code in ((tc.SHA_PT, "pt", "terms_text_changed"), ("0" * 64, "en", "terms_text_changed"),
                       (None, "en", "terms_text_changed"), (tc.SHA_EN, "pt", "terms_locale_mismatch")):
    try:
        tc.validate(sha, loc)
        got = None
    except tc.TermsError as e:
        got = e.code
    ok(got == code, f"validate({str(sha)[:8]}, {loc}) → {code}")
ok(tc.validate(tc.SHA_EN, "en") == "en", "EN sha + en accepted")
with patch.object(settings, "terms_pt_live", True):
    ok(tc.displayed_locale("pt-BR") == "pt" and tc.validate(tc.SHA_PT, "pt-BR") == "pt",
       "gate open (after /terms/pt/ is live): PT sentence shown and accepted")
    ok(tc.public_flags()["pt_live"] is True and tc.public_flags()["accepted_shas"] == {"en": tc.SHA_EN, "pt": tc.SHA_PT},
       "gate open: API flag true, both shas")

print("(a) a PT acceptance is not consent while the PT gate is closed")
T = datetime(2026, 11, 14, tzinfo=timezone.utc)
ws_pt = NS(terms_accepted_at=T, privacy_accepted_at=T, terms_accepted_version=tc.SHA_PT, terms_accepted_locale="pt",
           llm_disabled=False, is_test=True, paid_at=None, subscription_status=None)
ws_en = NS(**{**vars(ws_pt), "terms_accepted_version": tc.SHA_EN, "terms_accepted_locale": "en"})
ok(tc.block_reason(ws_pt) == "terms_pt_not_live" and not tc.is_valid(ws_pt), "stored PT acceptance → terms_pt_not_live")
ok(tc.block_reason(ws_en) is None and tc.is_valid(ws_en), "EN acceptance (PT-locale user shown EN) counts")
ok(tc.block_reason(NS(terms_accepted_at=None, privacy_accepted_at=T)) == "terms_privacy_not_accepted", "nothing accepted")
with patch.object(settings, "terms_pt_live", True):
    ok(tc.block_reason(ws_pt) is None, "gate open: the PT acceptance counts")


@__import__("contextlib").contextmanager
def _release(row):
    from app.services import llm as _llm
    with patch.object(_llm, "backend", return_value="api"), patch.object(_llm, "data_retention", return_value="30d"), \
         patch.object(llm_guard, "_workspace_row", return_value=(row, None)):
        yield


with _release(ws_pt):
    ok(llm_guard.release_block_reason(9, "ideas", fields=("subject",)) == "terms_pt_not_live",
       "S8 release: an external workspace consented only under the PT text is blocked (terms_pt_not_live)")

ok(llm_guard.BLOCK_USER_TEXT["terms_pt_not_live"][0] == "terms_privacy",
   "banner sends the user back to the 'Terms and privacy' step (never silent)")

print("(b) configurable accepted sha (MANAGER_TERMS_VERSION_SHA_EN / _PT)")
ok(_AppSettings.model_fields["terms_version_sha_en"].default == tc.SHA_EN
   and _AppSettings.model_fields["terms_version_sha_pt"].default == tc.SHA_PT,
   "code defaults = sha256 of the current FIX4 sentences")
NEW = "a" * 64
with patch.dict("os.environ", {"MANAGER_TERMS_VERSION_SHA_EN": NEW, "MANAGER_TERMS_VERSION_SHA_PT": "b" * 64}):
    ok(_AppSettings().terms_version_sha_en == NEW and _AppSettings().terms_version_sha_pt == "b" * 64,
       "both shas are read from the env")
with patch.object(settings, "terms_version_sha_en", NEW):
    ok(tc.known_shas() == {NEW: "en"}, "server accepts ONLY the configured EN sha")
    try:
        tc.validate(tc.SHA_EN, "en"); got = None
    except tc.TermsError as e:
        got = e.code
    ok(got == "terms_text_changed", "the old (code) sha is refused once the config moves → reload + accept again")
    ok(tc.validate(NEW.upper(), "en") == "en", "configured sha accepted (case-insensitive)")
with patch.object(settings, "terms_version_sha_pt", NEW), patch.object(settings, "terms_pt_live", True):
    ok(tc.known_shas() == {tc.SHA_EN: "en", NEW: "pt"}, "configured PT sha used once the gate opens")
with patch.object(settings, "terms_version_sha_en", ""):
    ok(tc.known_shas() == {}, "empty EN sha → nothing accepted (fail closed)")
env = (ROOT / ".env.example").read_text()
ok(f"# MANAGER_TERMS_VERSION_SHA_EN={tc.SHA_EN}" in env and f"# MANAGER_TERMS_VERSION_SHA_PT={tc.SHA_PT}" in env
   and "# MANAGER_TERMS_PT_LIVE=false" in env, ".env.example documents the three settings (commented)")

print("CMO v3 footer links (PT)")
ui = (ROOT / "frontend/src/ui.tsx").read_text()
i18n = (ROOT / "frontend/src/wizardI18n.ts").read_text()
pl = ui[ui.index("export function PolicyLinks"):ui.index("export function ConnectConsentNote")]
ok('aria-label={t("footer.legal_aria")}' in pl and '"footer.legal_aria": { en: "Legal", pt: "Jurídico" }' in i18n,
   "footer nav aria-label Legal / Jurídico")
for k, en, pt in (("footer.privacy", "Privacy", "Privacidade"), ("footer.terms", "Terms", "Termos"),
                  ("footer.youtube_terms", "YouTube Terms of Service", "Termos de Serviço do YouTube")):
    ok(f't("{k}")' in pl and f'"{k}": {{ en: "{en}", pt: "{pt}" }}' in i18n, f"{k}: {en} / {pt}")
cn = ui[ui.index("export function ConnectConsentNote"):]
ok("By connecting, you agree to the Owera" in cn, "#215 Connect consent note untouched (out of scope)")

print(f"\nALL {_checks} CHECKS PASSED")
