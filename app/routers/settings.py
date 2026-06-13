from fastapi import APIRouter, Depends
from sqlmodel import Session

from app.config import settings as app_config
from app.db import app_settings, get_session
from app.schemas import SettingsUpdate
from app.services.mpt_client import mpt

router = APIRouter(prefix="/api", tags=["settings"])


@router.get("/settings")
def get_settings(session: Session = Depends(get_session)):
    cfg = app_settings(session)
    return {
        **cfg.model_dump(),
        "mpt_base_url": app_config.mpt_base_url,
    }


@router.patch("/settings")
def update_settings(body: SettingsUpdate, session: Session = Depends(get_session)):
    cfg = app_settings(session)
    for k, v in body.model_dump(exclude_unset=True).items():
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
