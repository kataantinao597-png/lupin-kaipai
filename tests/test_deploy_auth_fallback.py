"""部署适配回归：Authorization 头被网关剥离时，get_current_user 回退 refresh Cookie 校验。"""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.integration

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import backend.auth  # noqa: F401,E402


@pytest.fixture()
def db_session(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path}/authfb.db"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("LUPIN_DB_AUTO_CREATE", "0")
    from backend.auth.database import reset_engine, get_session_factory
    reset_engine()
    from alembic.config import Config
    from alembic import command
    cfg = Config(os.path.join(PROJECT_ROOT, "alembic.ini"))
    command.upgrade(cfg, "head")
    from backend.auth.models import User as AuthUser
    s = get_session_factory()()
    s.add(AuthUser(id="u1", email="u1@test.local", password_hash="x"))
    s.commit()
    yield s
    s.close()
    reset_engine()


def _request_with_cookie(token=None):
    from starlette.requests import Request
    headers = []
    if token:
        headers = [(b"cookie", f"lupin_refresh={token}".encode())]
    return Request({"type": "http", "method": "GET", "headers": headers})


def _make_refresh(db_session, token="tok-abc-123", user_id="u1", revoked=False):
    from backend.repositories.user_repository import RefreshTokenRepository
    row = RefreshTokenRepository(db_session).create(
        user_id, token, datetime.now(timezone.utc) + timedelta(days=7))
    if revoked:
        RefreshTokenRepository(db_session).revoke(row)
    return token


def test_cookie_fallback_ok(db_session):
    from backend.auth.deps import get_current_user
    token = _make_refresh(db_session)
    user = get_current_user(credentials=None,
                            request=_request_with_cookie(token),
                            db=db_session)
    assert user.id == "u1"


def test_no_bearer_no_cookie_401(db_session):
    from fastapi import HTTPException
    from backend.auth.deps import get_current_user
    with pytest.raises(HTTPException) as e:
        get_current_user(credentials=None,
                         request=_request_with_cookie(None),
                         db=db_session)
    assert e.value.status_code == 401


def test_revoked_cookie_401(db_session):
    from fastapi import HTTPException
    from backend.auth.deps import get_current_user
    token = _make_refresh(db_session, token="tok-revoked", revoked=True)
    with pytest.raises(HTTPException) as e:
        get_current_user(credentials=None,
                         request=_request_with_cookie(token),
                         db=db_session)
    assert e.value.status_code == 401


def test_bearer_still_preferred(db_session):
    from backend.auth.deps import get_current_user
    from backend.auth.security import create_access_token
    access, _ = create_access_token("u1")

    class Cred:
        credentials = access

    user = get_current_user(credentials=Cred(),
                            request=_request_with_cookie(None),
                            db=db_session)
    assert user.id == "u1"


def test_expired_bearer_falls_back_to_cookie(db_session):
    from backend.auth.deps import get_current_user

    class Cred:
        credentials = "this.is.not.a.valid.token"

    token = _make_refresh(db_session, token="tok-fallback")
    user = get_current_user(credentials=Cred(),
                            request=_request_with_cookie(token),
                            db=db_session)
    assert user.id == "u1"
