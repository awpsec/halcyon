from typing import Annotated

from fastapi import Cookie, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.session import get_db
from app.models.entities import SessionToken, UserProfile
from app.services.auth import hash_session_token, is_hashed_session_token

settings = get_settings()


def resolve_session_token(db: Session, raw_session_token: str | None) -> SessionToken | None:
    if not raw_session_token:
        return None
    hashed = hash_session_token(raw_session_token)
    token = db.scalar(
        select(SessionToken)
        .where(SessionToken.token.in_([hashed, raw_session_token]))
        .order_by(SessionToken.id.asc())
        .limit(1)
    )
    if token and not is_hashed_session_token(token.token):
        token.token = hashed
        db.commit()
        db.refresh(token)
    return token


def get_current_user(
    db: Session = Depends(get_db),
    session_token: Annotated[str | None, Cookie(alias=settings.session_cookie_name)] = None,
) -> UserProfile:
    if not session_token:
        raise HTTPException(status_code=401, detail="Not signed in")
    token = resolve_session_token(db, session_token)
    if not token or not token.user:
        raise HTTPException(status_code=401, detail="Invalid session")
    return token.user


def get_current_admin_user(current_user: UserProfile = Depends(get_current_user)) -> UserProfile:
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    return current_user


def get_configured_admin_user(current_user: UserProfile = Depends(get_current_admin_user)) -> UserProfile:
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    if current_user.requires_admin_setup:
        raise HTTPException(status_code=403, detail="Admin setup must be completed first")
    return current_user
