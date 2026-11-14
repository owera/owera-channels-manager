"""Non-secret feature flags for the SPA (WS4 PR-F)."""

from fastapi import APIRouter

from app.config import settings

router = APIRouter(prefix="/api", tags=["features"])


@router.get("/features")
def get_features():
    return {
        "google_web_oauth": settings.google_web_oauth,
        "multi_workspace": settings.multi_workspace,
        "spa_google_login": settings.spa_google_login,
        "youtube_subscriptions_admin": settings.youtube_subscriptions_admin,
    }
