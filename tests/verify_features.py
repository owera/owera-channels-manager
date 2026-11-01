"""GET /api/features — SPA feature flags (WS4 PR-F)."""
from __future__ import annotations

import sys

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import settings
from app.routers import features as features_router

_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


app = FastAPI()
app.include_router(features_router.router)
client = TestClient(app)

settings.google_web_oauth = False
r = client.get("/api/features")
ok(r.status_code == 200, "features 200")
body = r.json()
ok("google_client" not in str(body).lower() and "secret" not in str(body).lower(), "no secrets")
ok(body.get("google_web_oauth") is False, "web oauth flag off by default")

print(f"\nALL {_checks} CHECKS PASSED")
