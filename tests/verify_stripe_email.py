"""Direct coverage for app/services/stripe_email.py (backlog #7).

This project has no pytest; run directly:
    PYTHONPATH=. uv run python tests/verify_stripe_email.py

``send_email`` is the Resend choke point (welcome + access-lost). Provision
suites stub ``send_welcome_email``, so the real branches (blocklist, missing
API key, httpx success/fail, never-raise, PII-redacted logs, subject/html/
label forwarding) had zero direct coverage. A regression here either emails
the operator, POSTs to Resend with a live key from .env, or marks a
workspace "sent" when nothing queued.

Drives the real module with only ``httpx.post`` stubbed — no network, no
Resend. ``MANAGER_RESEND_API_KEY`` is set to a fake BEFORE settings load
(the operator .env may hold a real key). A module-level ``httpx.post``
guard refuses an unpatched send. Exits non-zero on the first failed
assertion.
"""
from __future__ import annotations

import inspect
import logging
import os
import sys
from unittest.mock import patch

# Safety: fake the key before pydantic-settings reads env / .env.
os.environ["MANAGER_RESEND_API_KEY"] = "re_test_never_send"

import httpx

from app.config import settings
from app.services import spa_google_login, stripe_email, stripe_provision

_checks = 0

settings.resend_api_key = "re_test_never_send"
stripe_email.logger.addHandler(logging.NullHandler())
stripe_email.logger.propagate = False

_REAL_POST = stripe_email.httpx.post


def _unpatched_post(*args, **kwargs):
    raise AssertionError("httpx.post called without a test patch — refusing live Resend")


stripe_email.httpx.post = _unpatched_post

BUYER = "buyer@example.com"
BLOCKED = "rodrigo@owera.com"


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.messages: list[str] = []
        stripe_email.logger.addHandler(self)
        stripe_email.logger.setLevel(logging.DEBUG)

    def emit(self, record):
        self.messages.append(record.getMessage())

    def close(self):
        stripe_email.logger.removeHandler(self)
        super().close()


class _FakeResp:
    raises = 0

    def __init__(self, status=200):
        self.status_code = status

    def raise_for_status(self):
        _FakeResp.raises += 1
        if self.status_code >= 400:
            req = httpx.Request("POST", stripe_email.RESEND_URL)
            resp = httpx.Response(self.status_code, request=req)
            raise httpx.HTTPStatusError(
                f"{self.status_code}", request=req, response=resp,
            )


def _record(calls, status=200, exc=None):
    def _post(*args, **kwargs):
        calls.append((args, kwargs))
        if exc is not None:
            raise exc
        return _FakeResp(status)
    return _post


def _send(email):
    """Call send_welcome_email; FAIL (not traceback) if it raises.

    AssertionError from the unpatched-post guard must propagate.
    """
    try:
        return stripe_email.send_welcome_email(email)
    except AssertionError:
        raise
    except Exception as e:
        ok(False, f"send_welcome_email never raises, got {type(e).__name__}: {e}")
        return None  # unreachable


# --- module contracts --------------------------------------------------------
print("stripe_email: module contracts")
ok(stripe_email.RESEND_URL == "https://api.resend.com/emails", "RESEND_URL")
ok(stripe_email.FROM_ADDRESS == "channels@mail.owera.com", "FROM_ADDRESS")
ok(stripe_email.REPLY_TO == "contact@owera.com", "REPLY_TO")
ok(stripe_email.SIGN_IN_URL == "https://channels.owera.com", "SIGN_IN_URL")
ok(isinstance(stripe_email._BLOCKED_RECIPIENTS, frozenset), "blocklist is frozenset")
ok(spa_google_login.normalized_email(BLOCKED) in stripe_email._BLOCKED_RECIPIENTS,
   "blocklist is the normalized operator email")
ok(stripe_email._redact(BUYER) == "buyer@…", "_redact keeps local-part and ellipsis")
ok(stripe_email._redact("a@b@c") == "a@…", "_redact splits on the first @ only")
src = inspect.getsource(stripe_provision.deliver_welcome)
ok("stripe_email.send_welcome_email(ws.owner_email)" in src,
   "deliver_welcome source still calls stripe_email.send_welcome_email(ws.owner_email)")

html = stripe_email.welcome_email_html()
ok(stripe_email.SIGN_IN_URL in html, "html embeds SIGN_IN_URL")
ok("connect your YouTube channel" in html, "html names the YouTube connect next-step")
ok("Welcome to Owera Channels Manager" in html, "html welcome line")


# --- send_welcome_email is a thin forwarder ----------------------------------
print("stripe_email: send_welcome_email forwards subject/html/label")
seen: list = []


def _capture_send(to_email, subject, html, *, label="email"):
    seen.append((to_email, subject, html, label))
    return True


with patch.object(stripe_email, "send_email", side_effect=_capture_send):
    ok(stripe_email.send_welcome_email("  Buyer@Example.COM  ") is True,
       "welcome wrapper returns send_email's result")
ok(seen == [("  Buyer@Example.COM  ", "Welcome to Channels Manager",
             stripe_email.welcome_email_html(), "welcome email")],
   "welcome wrapper forwards raw to_email + welcome subject/html/label")


# --- early returns: no httpx -------------------------------------------------
print("stripe_email: early returns never POST")
calls: list = []
with patch.object(stripe_email.httpx, "post", side_effect=_record(calls)):
    ok(_send("") is False, "empty email is False")
    ok(_send("   ") is False, "whitespace email is False")
    ok(_send(BLOCKED) is False, "blocked operator email is False")
    ok(_send("  RODRIGO@OWERA.COM  ") is False,
       "blocked email is case/whitespace-insensitive")
ok(calls == [], "early returns do not POST")

calls = []
settings.resend_api_key = ""
with patch.object(stripe_email.httpx, "post", side_effect=_record(calls)):
    cap = _Capture()
    ok(_send(BUYER) is False, "unset API key is False")
    cap.close()
ok(calls == [], "unset API key does not POST")
ok(any("MANAGER_RESEND_API_KEY is unset" in m for m in cap.messages),
   "unset key logs the missing-key warning")
ok(any("buyer@…" in m for m in cap.messages),
   "unset-key log uses the redacted local-part")
ok(all(BUYER not in m for m in cap.messages),
   "unset-key log does not contain the full recipient")

calls = []
settings.resend_api_key = "   "
with patch.object(stripe_email.httpx, "post", side_effect=_record(calls)):
    ok(_send(BUYER) is False, "whitespace-only API key is False")
ok(calls == [], "whitespace-only API key does not POST (strip before the truthy check)")


# --- send_email forwarding (distinct subject/html/label) ---------------------
print("stripe_email: send_email forwards caller subject/html/label")
settings.resend_api_key = "  re_test_never_send  "
calls = []
_FakeResp.raises = 0
cap = _Capture()
with patch.object(stripe_email.httpx, "post", side_effect=_record(calls)):
    ok(stripe_email.send_email(
        "  Other@Example.COM  ", "subj-x", "<p>html-x</p>", label="label-x",
    ) is True, "send_email happy path returns True")
cap.close()
ok(len(calls) == 1, "send_email POSTs once")
ok(_FakeResp.raises == 1, "send_email calls raise_for_status")
args, kwargs = calls[0]
url = args[0] if args else kwargs.get("url")
ok(url == stripe_email.RESEND_URL, "POST url is RESEND_URL")
ok(kwargs.get("timeout") == 10, "timeout is 10s")
headers = kwargs.get("headers") or {}
ok(headers.get("Authorization") == "Bearer re_test_never_send",
   "Authorization Bearer uses the stripped settings key")
payload = kwargs.get("json") or {}
ok(payload.get("from") == stripe_email.FROM_ADDRESS, "payload from")
ok(payload.get("reply_to") == stripe_email.REPLY_TO, "payload reply_to")
ok(payload.get("to") == ["other@example.com"],
   "payload to is a one-item list of the normalized email")
ok(payload.get("subject") == "subj-x",
   "payload subject is the caller argument (not a welcome constant)")
ok(payload.get("html") == "<p>html-x</p>",
   "payload html is the caller argument (not welcome_email_html)")
ok(any("label-x queued" in m and "other@…" in m for m in cap.messages),
   "success log uses the caller label and the redacted local-part")
ok(all("other@example.com" not in m and "Other@Example.COM" not in m
       for m in cap.messages),
   "success log does not contain the full recipient")


# --- welcome path still hits Resend with welcome body ------------------------
print("stripe_email: welcome path POSTs the Resend payload")
calls = []
_FakeResp.raises = 0
with patch.object(stripe_email.httpx, "post", side_effect=_record(calls)):
    ok(_send("  Buyer@Example.COM  ") is True, "happy path returns True")
ok(len(calls) == 1, "happy path POSTs once")
ok(_FakeResp.raises == 1, "happy path calls raise_for_status (200 still checked)")
payload = calls[0][1].get("json") or {}
ok(payload.get("subject") == "Welcome to Channels Manager", "payload subject")
ok(payload.get("html") == stripe_email.welcome_email_html(),
   "payload html IS welcome_email_html()")
ok(payload.get("to") == ["buyer@example.com"],
   "welcome to is the normalized email")


# --- httpx failures: False, never raise --------------------------------------
print("stripe_email: httpx failures return False and never raise")
settings.resend_api_key = "re_test_never_send"
calls = []
_FakeResp.raises = 0
cap = _Capture()
with patch.object(stripe_email.httpx, "post", side_effect=_record(calls, status=500)):
    ok(_send(BUYER) is False, "HTTP 500 is False")
cap.close()
ok(len(calls) == 1, "HTTP 500 still POSTed once")
ok(_FakeResp.raises == 1, "HTTP 500 calls raise_for_status (skip-raise would return True)")
ok(any("welcome email delivery failed" in m and "HTTPStatusError" in m
       for m in cap.messages),
   "HTTP 500 logs delivery failed with the exception class")
ok(any("buyer@…" in m for m in cap.messages),
   "failure log uses the redacted local-part")
ok(all(BUYER not in m for m in cap.messages),
   "failure log does not contain the full recipient")

calls = []
with patch.object(stripe_email.httpx, "post",
                  side_effect=_record(calls, exc=httpx.ConnectError("down"))):
    ok(_send(BUYER) is False, "ConnectError is False")
ok(len(calls) == 1, "ConnectError still attempted one POST")

calls = []
with patch.object(stripe_email.httpx, "post",
                  side_effect=_record(calls, exc=RuntimeError("boom"))):
    ok(_send(BUYER) is False, "unexpected Exception is False")
ok(len(calls) == 1, "unexpected Exception still attempted one POST")


print(f"\nALL {_checks} CHECKS PASSED")
