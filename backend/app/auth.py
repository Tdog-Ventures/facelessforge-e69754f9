"""Authentication utilities: password hashing, JWT, current-user dependency."""
import os
from datetime import datetime, timezone, timedelta
from typing import Optional

import bcrypt
import jwt
from fastapi import HTTPException, Request, Depends

JWT_ALGORITHM = "HS256"
ACCESS_TTL_MINUTES = 60 * 24  # 1 day for comfort
REFRESH_TTL_DAYS = 7


def hash_password(password: str) -> str:
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


def _secret() -> str:
    return os.environ["JWT_SECRET"]


def create_access_token(user_id: str, email: str, role: str) -> str:
    payload = {
        "sub": user_id,
        "email": email,
        "role": role,
        "exp": datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TTL_MINUTES),
        "type": "access",
    }
    return jwt.encode(payload, _secret(), algorithm=JWT_ALGORITHM)


def create_refresh_token(user_id: str) -> str:
    payload = {
        "sub": user_id,
        "exp": datetime.now(timezone.utc) + timedelta(days=REFRESH_TTL_DAYS),
        "type": "refresh",
    }
    return jwt.encode(payload, _secret(), algorithm=JWT_ALGORITHM)


def set_auth_cookies(response, access_token: str, refresh_token: str, request: Optional[Request] = None) -> None:
    dev_mode = os.environ.get("DEV_MODE", "false").lower() in ("1", "true", "yes")
    # In production (cross-origin HTTPS), cookies need SameSite=None + Secure.
    # In dev (same-origin HTTP), SameSite=Lax + Secure=False.
    # When the request is available, derive the flags from the actual scheme so
    # the app works on HTTP deployments without manual DEV_MODE toggles.
    if request is not None:
        proto = request.headers.get("x-forwarded-proto", request.url.scheme)
        secure = proto == "https"
        samesite = "none" if secure else "lax"
    else:
        samesite = "lax" if dev_mode else "none"
        secure = not dev_mode
    response.set_cookie(
        key="access_token", value=access_token, httponly=True,
        secure=secure, samesite=samesite, max_age=ACCESS_TTL_MINUTES * 60, path="/",
    )
    response.set_cookie(
        key="refresh_token", value=refresh_token, httponly=True,
        secure=secure, samesite=samesite, max_age=REFRESH_TTL_DAYS * 86400, path="/",
    )


def clear_auth_cookies(response) -> None:
    response.delete_cookie("access_token", path="/")
    response.delete_cookie("refresh_token", path="/")


def _read_token(request: Request) -> Optional[str]:
    token = request.cookies.get("access_token")
    if token:
        return token
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:]
    return None


def _check_cookie_origin(request: Request) -> None:
    """Unsafe cookie requests need same-origin or explicitly trusted frontend evidence.

    Bearer-only clients and safe reads do not depend on ambient browser cookies.
    Non-browser cookie clients must send Origin or Referer on mutations too.
    """
    if request.method.upper() in {"GET", "HEAD", "OPTIONS"}:
        return
    if not (request.cookies.get("access_token") or request.cookies.get("refresh_token")):
        return
    from urllib.parse import urlsplit

    def origin(value):
        try:
            url = urlsplit(value)
            if url.scheme not in {"http", "https"} or not url.netloc or url.username or url.password:
                return None
            port = url.port or (443 if url.scheme == "https" else 80)
            return (url.scheme, url.hostname, port)
        except ValueError:
            return None

    trusted = {
        origin(str(request.base_url)),
        origin("https://facelessforge.ethinx.solutions"),
        origin("https://ethinx.solutions"),
        origin("https://www.ethinx.solutions"),
        origin(os.environ.get("FRONTEND_URL", "")),
    } - {None}
    supplied = request.headers.get("origin")
    if supplied is None:
        supplied = request.headers.get("referer", "")
    # Cross-site unsafe requests without verifiable provenance fail closed.
    if origin(supplied) not in trusted:
        raise HTTPException(status_code=403, detail="Untrusted origin for cookie-authenticated request")


async def get_current_user(request: Request):
    _check_cookie_origin(request)
    from .db import get_db
    token = _read_token(request)
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        payload = jwt.decode(token, _secret(), algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")
    if payload.get("type") != "access":
        raise HTTPException(status_code=401, detail="Invalid token type")
    db = get_db()
    user = await db.users.find_one({"id": payload["sub"]}, {"_id": 0, "password_hash": 0})
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return user


def require_roles(*roles: str):
    async def dep(user=Depends(get_current_user)):
        if user.get("role") not in roles:
            raise HTTPException(status_code=403, detail="Forbidden: insufficient role")
        return user
    return dep
