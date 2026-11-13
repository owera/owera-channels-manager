"""P4: in-app Privacy / Terms / YouTube ToS links (Google OAuth verification,
YouTube API Services Developer Policies III.A.1 + III.A.2.a). Static source
check so a refactor cannot silently drop them.

Run: PYTHONPATH=. uv run python tests/verify_policy_links.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "frontend" / "src"
_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


ui = (ROOT / "ui.tsx").read_text()
for url in ("https://owera.com/privacy/", "https://owera.com/terms/", "https://www.youtube.com/t/terms"):
    ok(url in ui, f"POLICY_LINKS has {url}")
ok('rel="noopener noreferrer"' in ui and 'target="_blank"' in ui, "external links open safely in a new tab")
app = (ROOT / "App.tsx").read_text()
ok(app.count("<PolicyLinks") >= 2, "app shell shows links in the sidebar (desktop) and footer (mobile)")
ok("<PolicyLinks" in (ROOT / "pages" / "Login.tsx").read_text(), "login page shows the links")
ok("<ConnectConsentNote" in (ROOT / "pages" / "Channels.tsx").read_text(),
   "Connect YouTube shows the consent note (Terms, Privacy, YouTube ToS)")
print(f"\nALL {_checks} CHECKS PASSED")
