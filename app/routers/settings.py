from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session

from app.config import settings as app_config
from app.db import app_settings, get_session
from app.schemas import SettingsUpdate
from app.services import quota
from app.services.mpt_client import mpt

router = APIRouter(prefix="/api", tags=["settings"])


def _require_int(fields: dict, key: str, minimum: int, hint: str) -> None:
    """Reject JSON null / bool / below-floor ints before they hit the DB.

    ``int`` columns on Settings are NOT NULL. ``setattr(..., None)`` persists
    SQL NULL, and the next ``in_flight >= cfg.render_concurrency`` TypeErrors
    the render tick. ``bool`` is a subclass of ``int``, so an explicit check
    keeps ``true`` from becoming concurrency=1.
    """
    if key not in fields:
        return
    v = fields[key]
    if not isinstance(v, int) or isinstance(v, bool) or v < minimum:
        raise HTTPException(400, hint)


@router.get("/settings")
def get_settings(session: Session = Depends(get_session)):
    cfg = app_settings(session)
    return {
        **cfg.model_dump(),
        "mpt_base_url": app_config.mpt_base_url,
        # When the YouTube Data API project quota next resets (Pacific midnight).
        "youtube_quota_reset_at": quota.next_quota_reset().isoformat(),
    }


@router.patch("/settings")
def update_settings(body: SettingsUpdate, session: Session = Depends(get_session)):
    fields = body.model_dump(exclude_unset=True)
    # 0/null concurrency stalls every render: render_loop gates on
    # ``in_flight >= cfg.render_concurrency`` and in_flight is always >= 0.
    _require_int(fields, "render_concurrency", 1,
                 "render_concurrency must be >= 1 "
                 "(0/null stalls every render: in_flight >= 0 is always true)")
    _require_int(fields, "publish_drip_minutes", 0,
                 "publish_drip_minutes must be >= 0")
    _require_int(fields, "topic_autogen_min_pending", 0,
                 "topic_autogen_min_pending must be >= 0")
    _require_int(fields, "topic_autogen_target", 0,
                 "topic_autogen_target must be >= 0")
    cfg = app_settings(session)
    for k, v in fields.items():
        setattr(cfg, k, v)
    session.add(cfg)
    session.commit()
    session.refresh(cfg)
    return cfg


@router.get("/health")
def health():
    return {
        "manager": "ok",
        "mpt_reachable": mpt.ping(),
        "mpt_base_url": app_config.mpt_base_url,
    }
