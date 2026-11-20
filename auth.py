
"""HYTOMA admin authentication."""

import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError
from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Request,
    Response,
)
from fastapi.security import (
    HTTPAuthorizationCredentials,
    HTTPBearer,
)
from pydantic import BaseModel, Field

from db import db_connect


router = APIRouter(prefix="/auth", tags=["Admin Login"])
bearer = HTTPBearer(auto_error=False)
password_hasher = PasswordHasher()

COOKIE_NAME = "hytoma_session"
SESSION_HOURS = 8


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=1024)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def unauthorized():
    raise HTTPException(
        status_code=401,
        detail="Authentication required"
    )


def check_origin(request: Request):
    origin = request.headers.get("origin")

    # Requests without Origin are allowed for
    # non-browser clients. Browser sessions also
    # require an unpredictable CSRF token.
    if not origin:
        return

    allowed = {
        item.strip().rstrip("/")
        for item in os.getenv(
            "ADMIN_ALLOWED_ORIGINS", ""
        ).split(",")
        if item.strip()
    }

    # Browser requests from the API's own HTTPS
    # origin are allowed.
    own_origin = (
        f"{request.url.scheme}://{request.url.netloc}"
    )
    allowed.add(own_origin)

    if origin.rstrip("/") not in allowed:
        raise HTTPException(
            status_code=403,
            detail="Origin not allowed"
        )


def get_session(request: Request):
    token = request.cookies.get(COOKIE_NAME)

    if not token:
        return None

    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT username, csrf_hash
                FROM admin_sessions
                WHERE token_hash = %s
                  AND expires_at > NOW()
            """, (digest(token),))
            return cur.fetchone()


def require_admin(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
):
    """Support legacy Swagger bearer key or secure session."""

    expected_key = os.getenv("ADMIN_API_KEY", "")

    if credentials is not None:
        if (
            expected_key
            and secrets.compare_digest(
                credentials.credentials,
                expected_key
            )
        ):
            return True

        unauthorized()

    session = get_session(request)

    if session is None:
        unauthorized()

    if request.method in {
        "POST", "PUT", "PATCH", "DELETE"
    }:
        check_origin(request)

        csrf = request.headers.get("X-CSRF-Token", "")

        if not csrf or not secrets.compare_digest(
            digest(csrf),
            session["csrf_hash"]
        ):
            raise HTTPException(
                status_code=403,
                detail="Invalid CSRF token"
            )

    return True


@router.post("/login")
def login(payload: LoginRequest, request: Request, response: Response):
    check_origin(request)

    username = payload.username.strip()
    configured_user = os.getenv("ADMIN_USERNAME", "")
    configured_hash = os.getenv("ADMIN_PASSWORD_HASH", "")

    if not configured_user or not configured_hash:
        raise HTTPException(
            status_code=503,
            detail="Admin login is not configured"
        )

    with db_connect() as conn:
        with conn.cursor() as cur:
            # One administrator, with persistent
            # failed-login throttling.
            cur.execute("""
                INSERT INTO admin_login_attempts (username)
                VALUES (%s)
                ON CONFLICT (username) DO NOTHING
            """, (configured_user,))

            cur.execute("""
                SELECT failures, locked_until
                FROM admin_login_attempts
                WHERE username = %s
                FOR UPDATE
            """, (configured_user,))

            attempt = cur.fetchone()
            now = datetime.now(timezone.utc)

            if (
                attempt["locked_until"]
                and attempt["locked_until"] > now
            ):
                raise HTTPException(
                    status_code=429,
                    detail="Too many attempts. Try again later."
                )

            valid_password = False

            try:
                valid_password = password_hasher.verify(
                    configured_hash,
                    payload.password
                )
            except (VerifyMismatchError, VerificationError):
                pass

            valid = (
                secrets.compare_digest(
                    username,
                    configured_user
                )
                and valid_password
            )

            if not valid:
                failures = attempt["failures"] + 1

                cur.execute("""
                    UPDATE admin_login_attempts
                    SET failures = %s,
                        locked_until = %s
                    WHERE username = %s
                """, (
                    0 if failures >= 5 else failures,
                    (
                        now + timedelta(minutes=15)
                        if failures >= 5 else None
                    ),
                    configured_user
                ))

                # The failed-attempt update commits
                # when this connection context exits.
            else:
                token = secrets.token_urlsafe(48)
                csrf = secrets.token_urlsafe(32)

                cur.execute("""
                    DELETE FROM admin_sessions
                    WHERE expires_at <= NOW()
                """)

                cur.execute("""
                    INSERT INTO admin_sessions
                    (token_hash, csrf_hash, username, expires_at)
                    VALUES (%s, %s, %s, %s)
                """, (
                    digest(token),
                    digest(csrf),
                    username,
                    now + timedelta(hours=SESSION_HOURS)
                ))

                cur.execute("""
                    UPDATE admin_login_attempts
                    SET failures = 0,
                        locked_until = NULL
                    WHERE username = %s
                """, (configured_user,))

    if not valid:
        raise HTTPException(
            status_code=401,
            detail="Invalid username or password"
        )

    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=SESSION_HOURS * 3600,
        path="/",
        httponly=True,
        secure=True,
        samesite="lax",
    )

    return {
        "authenticated": True,
        "username": username,
        "csrf_token": csrf
    }


@router.get("/me")
def current_user(request: Request):
    session = get_session(request)

    if session is None:
        unauthorized()

    # Rotate CSRF token on session restoration.
    csrf = secrets.token_urlsafe(32)

    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE admin_sessions
                SET csrf_hash = %s
                WHERE token_hash = %s
                  AND expires_at > NOW()
            """, (
                digest(csrf),
                digest(request.cookies[COOKIE_NAME])
            ))

    return {
        "authenticated": True,
        "username": session["username"],
        "csrf_token": csrf
    }


@router.post("/logout")
def logout(
    request: Request,
    response: Response,
    _admin=Depends(require_admin),
):
    token = request.cookies.get(COOKIE_NAME)

    if token:
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    DELETE FROM admin_sessions
                    WHERE token_hash = %s
                """, (digest(token),))

    response.delete_cookie(
        COOKIE_NAME,
        path="/"
    )

    return {"authenticated": False}
