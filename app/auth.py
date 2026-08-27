from dataclasses import dataclass

import jwt
from fastapi import Depends, HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.config.base import get_db
from app.models.senior import Senior
from app.models.techsaathi import TechSaathi
from app.models.user import User
from app.settings import ADMIN_TOKEN, JWT_SECRET_KEY

_bearer_scheme = HTTPBearer(auto_error=False, scheme_name="BearerAuth")
_jwt_bearer_scheme = HTTPBearer(auto_error=False, scheme_name="UserJWT")


def is_valid_token(token: str) -> bool:
    return bool(token) and token == ADMIN_TOKEN


def require_admin_token(
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer_scheme),
) -> None:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid authorization header",
        )

    if not is_valid_token(credentials.credentials):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
        )


def decode_jwt_token(token: str) -> dict:
    if not JWT_SECRET_KEY:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="JWT verification is not configured",
        )
    try:
        return jwt.decode(token, JWT_SECRET_KEY, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token is expired",
        ) from None
    except jwt.InvalidTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
        ) from None


def _user_from_jwt_payload(payload: dict, db: Session) -> User:
    user_id = payload.get("user_id")
    user_type = (payload.get("user_type") or "").strip()
    if not user_id or not user_type:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or unauthorized token",
        )

    phone = (
        payload.get("phone_number")
        or payload.get("phone")
        or ""
    )
    phone = str(phone).strip()
    email = str(payload.get("email") or "").strip()

    query = db.query(User).filter(
        User.id == user_id,
        User.user_type == user_type,
    )
    # Prefer phone (seniors often have no email); email kept for older tokens.
    if phone:
        user = query.filter(User.phone_number == phone).first()
    elif email:
        user = query.filter(User.email == email).first()
    else:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or unauthorized token",
        )
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
        )
    return user


@dataclass(frozen=True)
class AuthenticatedSenior:
    user: User
    senior: Senior


@dataclass(frozen=True)
class AuthenticatedTechSaathi:
    user: User
    tech_saathi: TechSaathi


def require_senior_user(
    credentials: HTTPAuthorizationCredentials | None = Security(_jwt_bearer_scheme),
    db: Session = Depends(get_db),
) -> AuthenticatedSenior:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid authorization header",
        )

    payload = decode_jwt_token(credentials.credentials)
    user = _user_from_jwt_payload(payload, db)

    if (user.user_type or "").strip().lower() != "senior":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Senior access required",
        )

    senior = db.query(Senior).filter(Senior.user_id == user.id).first()
    if not senior:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Senior profile not found",
        )
    if not senior.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Senior account is inactive",
        )

    return AuthenticatedSenior(user=user, senior=senior)


def require_techsaathi_user(
    credentials: HTTPAuthorizationCredentials | None = Security(_jwt_bearer_scheme),
    db: Session = Depends(get_db),
) -> AuthenticatedTechSaathi:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid authorization header",
        )

    payload = decode_jwt_token(credentials.credentials)
    user = _user_from_jwt_payload(payload, db)

    if (user.user_type or "").strip().lower() != "tech_saathi":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="TechSaathi access required",
        )

    tech_saathi = db.query(TechSaathi).filter(TechSaathi.user_id == user.id).first()
    if not tech_saathi:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="TechSaathi profile not found",
        )
    if not tech_saathi.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="TechSaathi account is inactive",
        )

    return AuthenticatedTechSaathi(user=user, tech_saathi=tech_saathi)
