#!/usr/bin/env python3
"""Archive a served legal document version by sha256 and print the sha for the deploy.

Usage:
    PYTHONPATH=. uv run python scripts/archive_legal_docs.py SOURCE [--label terms-en] [--dir DIR]

SOURCE is a saved HTML file or a URL (http(s):// or file://), e.g. the page
served at https://owera.com/terms/. The exact bytes are stored as
``<DIR>/<sha256>.html`` with a ``<sha256>.json`` sidecar (source, label, size,
archived_at UTC). An already-archived version is left as is (idempotent). DIR
defaults to MANAGER_LEGAL_ARCHIVE_DIR (storage/legal-archive).

The printed sha goes into the deploy config: MANAGER_TERMS_DOC_SHA_EN / _PT and
MANAGER_PRIVACY_DOC_SHA_EN / _PT. The app never fetches the pages itself; each
Terms acceptance records the configured sha (TermsConsentEvent), and this
archive keeps the text that sha stands for.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

_TIMEOUT = 30


def read_source(source: str) -> bytes:
    if "://" in source:
        req = urllib.request.Request(source, headers={"User-Agent": "owera-legal-archive/1"})
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:  # noqa: S310 (operator-given URL)
            return r.read()
    return Path(source).read_bytes()


def archive(source: str, archive_dir: str | Path, label: str | None = None) -> tuple[str, Path, bool]:
    """(sha256, stored path, created) for SOURCE."""
    data = read_source(source)
    if not data:
        raise ValueError(f"{source}: empty document")
    sha = hashlib.sha256(data).hexdigest()
    d = Path(archive_dir)
    d.mkdir(parents=True, exist_ok=True)
    out = d / f"{sha}.html"
    created = not out.exists()
    if created:
        tmp = d / f".{sha}.tmp"
        tmp.write_bytes(data)
        tmp.replace(out)
        (d / f"{sha}.json").write_text(json.dumps({
            "sha256": sha, "source": source, "label": label, "bytes": len(data),
            "archived_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}, indent=2) + "\n")
    elif out.read_bytes() != data:   # sha collision is not a real concern; a corrupted file is
        raise RuntimeError(f"{out} exists but differs from the source bytes")
    return sha, out, created


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("source", help="served HTML file or URL")
    ap.add_argument("--label", help="e.g. terms-en, terms-pt, privacy-en, privacy-pt")
    ap.add_argument("--dir", help="archive dir (default MANAGER_LEGAL_ARCHIVE_DIR)")
    a = ap.parse_args(argv)
    if a.dir:
        archive_dir = a.dir
    else:
        from app.config import settings
        archive_dir = settings.legal_archive_dir
    try:
        sha, out, created = archive(a.source, archive_dir, a.label)
    except Exception as e:  # noqa: BLE001
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"{'archived' if created else 'already archived'} {out}", file=sys.stderr)
    print(sha)
    return 0


if __name__ == "__main__":
    sys.exit(main())
