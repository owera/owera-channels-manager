"""Encrypted OAuth token storage for platform web OAuth (WS4 PR-D).

Tokens live in ``oauthcredential`` as AES-GCM blobs with AAD ``workspace_id:channel_id``
so ciphertext cannot be swapped across channels. Keys come from ``MANAGER_TOKEN_ENC_KEY``
(JSON ``{"active":"v1","keys":{"v1":"<urlsafe_b64_32bytes>"}}`` or a single urlsafe key).
"""

from __future__ import annotations

import base64
import json
import logging
import os
from typing import Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from google.oauth2.credentials import Credentials
from sqlmodel import Session, select

from app.config import settings
from app.models import Channel, OAuthCredential

logger = logging.getLogger("manager.token_store")

_BLOB_VERSION = 1
_MAX_KEY_ID_LEN = 32


class TokenStoreError(Exception):
    """Misconfiguration or corrupt ciphertext."""


class TokenStoreNotConfigured(TokenStoreError):
    """MANAGER_TOKEN_ENC_KEY unset — platform OAuth cannot persist tokens."""


def _parse_keyring() -> tuple[str, dict[str, bytes]]:
    raw = (settings.token_enc_key or "").strip()
    if not raw:
        raise TokenStoreNotConfigured("MANAGER_TOKEN_ENC_KEY is not set")
    if raw.startswith("{"):
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as e:
            raise TokenStoreError("MANAGER_TOKEN_ENC_KEY JSON is invalid") from e
        active = str(obj.get("active") or "v1")
        keys_raw = obj.get("keys") or {}
        keys: dict[str, bytes] = {}
        for kid, b64 in keys_raw.items():
            keys[str(kid)] = _decode_key_material(str(b64))
        if active not in keys:
            raise TokenStoreError(f"active key_id '{active}' missing from keys map")
        return active, keys
    key = _decode_key_material(raw)
    return "v1", {"v1": key}


def _decode_key_material(b64: str) -> bytes:
    try:
        key = base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4))
    except Exception as e:
        raise TokenStoreError("encryption key must be urlsafe base64") from e
    if len(key) != 32:
        raise TokenStoreError("encryption key must decode to 32 bytes (AES-256)")
    return key


def _aad(workspace_id: int, channel_id: int) -> bytes:
    return f"{workspace_id}:{channel_id}".encode("utf-8")


def encrypt_blob(workspace_id: int, channel_id: int, plaintext: bytes,
                 *, key_id: str | None = None) -> tuple[str, bytes]:
    active, keys = _parse_keyring()
    kid = key_id or active
    if kid not in keys:
        raise TokenStoreError(f"unknown key_id '{kid}'")
    if len(kid) > _MAX_KEY_ID_LEN:
        raise TokenStoreError("key_id too long")
    aes = AESGCM(keys[kid])
    nonce = os.urandom(12)
    ct = aes.encrypt(nonce, plaintext, _aad(workspace_id, channel_id))
    kid_b = kid.encode("utf-8")
    blob = bytes([_BLOB_VERSION, len(kid_b)]) + kid_b + nonce + ct
    return kid, blob


def decrypt_blob(workspace_id: int, channel_id: int, blob: bytes,
                 key_id_hint: str | None = None) -> bytes:
    if len(blob) < 3:
        raise TokenStoreError("ciphertext too short")
    if blob[0] != _BLOB_VERSION:
        raise TokenStoreError(f"unsupported blob version {blob[0]}")
    kid_len = blob[1]
    if kid_len > _MAX_KEY_ID_LEN or 2 + kid_len + 12 >= len(blob):
        raise TokenStoreError("malformed ciphertext header")
    kid = blob[2:2 + kid_len].decode("utf-8")
    if key_id_hint and kid != key_id_hint:
        raise TokenStoreError("key_id hint mismatch")
    nonce = blob[2 + kid_len:2 + kid_len + 12]
    ct = blob[2 + kid_len + 12:]
    _, keys = _parse_keyring()
    if kid not in keys:
        raise TokenStoreError(f"unknown key_id '{kid}' (rotation?)")
    aes = AESGCM(keys[kid])
    try:
        return aes.decrypt(nonce, ct, _aad(workspace_id, channel_id))
    except InvalidTag as e:
        raise TokenStoreError("ciphertext authentication failed (wrong binding or key)") from e


def has_credential(session: Session, channel_id: int) -> bool:
    return session.get(OAuthCredential, channel_id) is not None


def delete_credential(session: Session, channel_id: int) -> None:
    row = session.get(OAuthCredential, channel_id)
    if row:
        session.delete(row)


def save_credentials(session: Session, channel: Channel, creds: Credentials) -> None:
    """Persist authorized-user JSON encrypted at rest."""
    if channel.id is None:
        raise ValueError("channel must be persisted before saving credentials")
    payload = creds.to_json().encode("utf-8")
    kid, blob = encrypt_blob(channel.workspace_id, channel.id, payload)
    row = session.get(OAuthCredential, channel.id)
    if row is None:
        row = OAuthCredential(
            channel_id=channel.id,
            workspace_id=channel.workspace_id,
            key_id=kid,
        )
    row.key_id = kid
    row.ciphertext_b64 = base64.b64encode(blob).decode("ascii")
    row.workspace_id = channel.workspace_id
    session.add(row)


def load_token_info(session: Session, channel: Channel) -> Optional[dict]:
    if channel.id is None:
        return None
    row = session.get(OAuthCredential, channel.id)
    if row is None:
        return None
    if row.workspace_id != channel.workspace_id:
        logger.error(
            "oauth credential workspace mismatch channel=%s cred_ws=%s ch_ws=%s",
            channel.id, row.workspace_id, channel.workspace_id,
        )
        return None
    try:
        blob = base64.b64decode(row.ciphertext_b64)
        plain = decrypt_blob(channel.workspace_id, channel.id, blob, key_id_hint=row.key_id)
        info = json.loads(plain.decode("utf-8"))
    except Exception as e:
        logger.error("failed to decrypt oauth credential for channel %s: %s", channel.id, e)
        return None
    if not isinstance(info, dict):
        return None
    return info


def load_credentials(session: Session, channel: Channel,
                     scopes: Optional[list] = None) -> Optional[Credentials]:
    info = load_token_info(session, channel)
    if info is None:
        return None
    if scopes is None:
        info.pop("scopes", None)
    creds = Credentials.from_authorized_user_info(info, scopes)
    return _ensure_web_client_fields(creds)


def _ensure_web_client_fields(creds: Credentials) -> Credentials:
    if creds.client_id and creds.client_secret:
        return creds
    cid = settings.google_client_id
    secret = settings.google_client_secret
    if not cid or not secret:
        raise TokenStoreNotConfigured("platform OAuth client id/secret not configured")
    return Credentials(
        token=creds.token,
        refresh_token=creds.refresh_token,
        token_uri=creds.token_uri,
        client_id=cid,
        client_secret=secret,
        scopes=creds.scopes,
    )


def list_credentials_for_workspace(session: Session, workspace_id: int) -> list[OAuthCredential]:
    return list(session.exec(
        select(OAuthCredential).where(OAuthCredential.workspace_id == workspace_id)
    ))
