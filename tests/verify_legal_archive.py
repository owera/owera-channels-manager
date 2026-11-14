"""scripts/archive_legal_docs.py — archive each served legal document version by sha256.

Run: PYTHONPATH=. uv run python tests/verify_legal_archive.py   (no network: file + file:// only)
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import archive_legal_docs as ald  # noqa: E402

_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


tmp = Path(tempfile.mkdtemp(prefix="legal-arch-"))
page = tmp / "terms.html"
body = "<html><body><h1>Terms of Use</h1><p>v 2026-11-14</p></body></html>\n".encode()
page.write_bytes(body)
SHA = hashlib.sha256(body).hexdigest()
arch = tmp / "archive"

sha, out, created = ald.archive(str(page), arch, "terms-en")
ok(sha == SHA and out == arch / f"{SHA}.html" and created, "file → <dir>/<sha256>.html")
ok(out.read_bytes() == body, "exact served bytes kept")
meta = json.loads((arch / f"{SHA}.json").read_text())
ok(meta["sha256"] == SHA and meta["source"] == str(page) and meta["label"] == "terms-en"
   and meta["bytes"] == len(body) and meta["archived_at"].endswith("Z"), "sidecar: source, label, size, UTC time")
sha2, _, created2 = ald.archive(page.as_uri(), arch, "terms-en")
ok(sha2 == SHA and not created2, "same version via a URL (file://) → same sha, idempotent")
page.write_bytes(body.replace(b"2026-11-14", b"2026-12-01"))
sha3, out3, created3 = ald.archive(str(page), arch)
ok(sha3 != SHA and created3 and (arch / f"{SHA}.html").read_bytes() == body,
   "a new version gets its own file; the old one stays")
(arch / f"{sha3}.html").write_bytes(b"tampered")
try:
    ald.archive(str(page), arch); bad = None
except RuntimeError as e:
    bad = str(e)
ok(bad and "differs" in bad, "an archived file that no longer matches its sha is reported, not overwritten")
empty = tmp / "empty.html"; empty.write_bytes(b"")
try:
    ald.archive(str(empty), arch); err = None
except ValueError as e:
    err = str(e)
ok(err and "empty" in err, "empty document refused")

page.write_bytes(body)
env = {**os.environ, "PYTHONPATH": str(ROOT), "MANAGER_LEGAL_ARCHIVE_DIR": str(tmp / "env-dir")}
r = subprocess.run([sys.executable, str(ROOT / "scripts/archive_legal_docs.py"), str(page), "--label", "terms-en"],
                   capture_output=True, text=True, env=env, timeout=60)
ok(r.returncode == 0 and r.stdout.strip() == SHA, "CLI prints only the sha on stdout (for the deploy config)")
ok(__import__("re").fullmatch(r"[0-9a-f]{64}\n", r.stdout) is not None, "stdout is the full 64-hex sha256 + newline")
ok((tmp / "env-dir" / f"{SHA}.html").is_file(), "CLI default dir = MANAGER_LEGAL_ARCHIVE_DIR")
r = subprocess.run([sys.executable, str(ROOT / "scripts/archive_legal_docs.py"), str(tmp / "nope.html"),
                    "--dir", str(arch)], capture_output=True, text=True, env=env, timeout=60)
ok(r.returncode == 1 and "error" in r.stderr and not r.stdout, "missing source → exit 1, nothing on stdout")
from app.config import Settings  # noqa: E402
ok(Settings.model_fields["legal_archive_dir"].default.endswith("storage/legal-archive"), "default archive dir")
print(f"\nALL {_checks} CHECKS PASSED")
