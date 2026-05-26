# app/core/jwt.py
# ─────────────────────────────────────────────────────────────
# JWT token creation and verification
# Used in OAuth callback (create) and protected routes (verify)
# ─────────────────────────────────────────────────────────────

from datetime import datetime, timedelta, timezone
from jose import JWTError, jwt
from app.core.config import settings


def create_access_token(data: dict) -> str:
    """
    Create a signed JWT token.
    Called after successful OAuth — token sent to frontend.
    Frontend sends this token in every API request header.
    """
    to_encode = data.copy()

    # Set expiry time
    expire = datetime.now(timezone.utc) + timedelta(
        minutes=settings.jwt_access_token_expire_minutes
    )
    to_encode.update({"exp": expire})

    # Sign and return token
    return jwt.encode(
        to_encode,
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm
    )


def verify_access_token(token: str) -> dict | None:
    """
    Verify JWT token and return payload.
    Returns None if token is invalid or expired.
    """
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm]
        )
        return payload
    except JWTError:
        return None