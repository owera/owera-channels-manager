"""WS3 onboarding checklist API."""

from fastapi import APIRouter, Depends
from sqlmodel import Session

from app.db import get_session
from app.services import onboarding as onboarding_svc
from app.workspace import Workspace, current_workspace

router = APIRouter(prefix="/api/onboarding", tags=["onboarding"])


@router.get("/status")
def onboarding_status(
    session: Session = Depends(get_session),
    ws: Workspace = Depends(current_workspace),
):
    return onboarding_svc.evaluate(session, ws)
