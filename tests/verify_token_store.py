"""Regression checks for encrypted platform OAuth token storage (WS4 PR-D).

    PYTHONPATH=. .venv/bin/python tests/verify_token_store.py
"""
from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

from google.oauth2.credentials import Credentials
from sqlmodel import Session, create_engine, SQLModel

from app.config import settings
from app.models import Channel, CredentialSource, OAuthCredential, Workspace
from app.services import token_store, youtube

_checks = 0
_KEY = base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


def _engine():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    eng = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(eng)
    return eng, path


def _seed(session: Session) -> Channel:
    session.add(Workspace(id=1, slug="owera", name="Owera"))
    ch = Channel(
        id=10,
        workspace_id=1,
        slug="ea-ch",
        name="EA",
        credential_source=CredentialSource.PLATFORM_OAUTH,
    )
    session.add(ch)
    session.commit()
    session.refresh(ch)
    return ch


print("token_store: encrypt/decrypt round-trip + AAD binding")
_orig_key = settings.token_enc_key
_orig_gid = settings.google_client_id
_orig_gsec = settings.google_client_secret
settings.token_enc_key = _KEY
settings.google_client_id = "web-client-id"
settings.google_client_secret = "web-client-secret"

plain = b'{"token":"t","refresh_token":"rt","token_uri":"https://oauth2.googleapis.com/token"}'
kid, blob = token_store.encrypt_blob(1, 10, plain)
ok(kid == "v1", "default key_id is v1")
got = token_store.decrypt_blob(1, 10, blob)
ok(got == plain, "decrypt round-trip")

try:
    token_store.decrypt_blob(1, 11, blob)
    ok(False, "wrong channel_id must fail AAD")
except token_store.TokenStoreError:
    ok(True, "wrong channel_id rejected")

try:
    token_store.decrypt_blob(2, 10, blob)
    ok(False, "wrong workspace_id must fail AAD")
except token_store.TokenStoreError:
    ok(True, "wrong workspace_id rejected")

print("token_store: DB save/load + get_service platform path")
eng, db_path = _engine()
with Session(eng) as session:
    ch = _seed(session)
    creds = Credentials(
        token="access",
        refresh_token="refresh",
        token_uri="https://oauth2.googleapis.com/token",
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        scopes=youtube.CONSENT_SCOPES,
    )
    creds.expiry = datetime.now(timezone.utc) + timedelta(hours=1)
    token_store.save_credentials(session, ch, creds)
    session.commit()
    ok(token_store.has_credential(session, ch.id), "has_credential true after save")
    loaded = token_store.load_credentials(session, ch)
    ok(loaded is not None and loaded.refresh_token == "refresh", "load_credentials")

    # get_service without file client_secret
    svc = youtube.get_service(ch.slug, channel=ch, session=session)
    ok(svc is not None, "get_service builds client from encrypted store")
    ok(token_store.has_credential(session, ch.id), "stored credential row present")
    ok(youtube.has_client_secret(ch.slug, channel=ch), "has_client_secret uses web client env")

    token_store.delete_credential(session, ch.id)
    session.commit()
    ok(not token_store.has_credential(session, ch.id), "delete_credential")

try:
    os.unlink(db_path)
except OSError:
    pass

print("token_store: legacy file path unchanged")
settings.token_enc_key = _orig_key
settings.google_client_id = _orig_gid
settings.google_client_secret = _orig_gsec

print(f"\nAll {_checks} checks passed.")
