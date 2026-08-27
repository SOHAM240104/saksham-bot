#!/usr/bin/env python3
"""Mint a senior or TechSaathi JWT for local API testing."""

from __future__ import annotations

import argparse
import os
import sys

import jwt
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

load_dotenv(os.path.join(ROOT, ".env"))

from app.config.base import DATABASE_URL  # noqa: E402


def _lookup_user(*, phone_suffix: str | None, user_id: int | None) -> dict:
    engine = create_engine(DATABASE_URL)
    with engine.connect() as conn:
        if user_id is not None:
            row = conn.execute(
                text(
                    """
                    SELECT u.id, u.email, u.user_type, u.first_name, u.phone_number,
                           s.id AS senior_id, s.is_active AS senior_active,
                           t.id AS tech_saathi_id, t.is_active AS tech_saathi_active
                    FROM access_user u
                    LEFT JOIN senior_senior s ON s.user_id = u.id
                    LEFT JOIN tech_saathi_techsaathi t ON t.user_id = u.id
                    WHERE u.id = :uid
                    """
                ),
                {"uid": user_id},
            ).fetchone()
        else:
            row = conn.execute(
                text(
                    """
                    SELECT u.id, u.email, u.user_type, u.first_name, u.phone_number,
                           s.id AS senior_id, s.is_active AS senior_active,
                           t.id AS tech_saathi_id, t.is_active AS tech_saathi_active
                    FROM access_user u
                    LEFT JOIN senior_senior s ON s.user_id = u.id
                    LEFT JOIN tech_saathi_techsaathi t ON t.user_id = u.id
                    WHERE u.phone_number LIKE :suffix
                    LIMIT 1
                    """
                ),
                {"suffix": f"%{phone_suffix}"},
            ).fetchone()
    if not row:
        raise SystemExit("User not found")
    return dict(row._mapping)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--phone-suffix",
        default="8328827545",
        help="Match access_user.phone_number ending with this (default: Soham test user)",
    )
    parser.add_argument("--user-id", type=int, help="Look up by access_user.id instead")
    parser.add_argument(
        "--role",
        choices=("senior", "tech_saathi"),
        default="senior",
        help="JWT user_type (default: senior)",
    )
    args = parser.parse_args()

    secret = os.getenv("JWT_SECRET_KEY", "").strip()
    if not secret:
        raise SystemExit("JWT_SECRET_KEY is not set in .env")

    user = _lookup_user(phone_suffix=args.phone_suffix, user_id=args.user_id)
    phone = (user.get("phone_number") or "").strip()
    if not phone:
        raise SystemExit(
            f"User {user['id']} has no phone_number; JWT auth needs phone (name optional)."
        )

    user_type = args.role
    if user_type == "senior":
        if not user.get("senior_id"):
            raise SystemExit("No senior_senior profile for this user")
        if not user.get("senior_active"):
            raise SystemExit("Senior profile is inactive")
    else:
        if not user.get("tech_saathi_id"):
            raise SystemExit("No tech_saathi_techsaathi profile for this user")
        if not user.get("tech_saathi_active"):
            raise SystemExit("TechSaathi profile is inactive")

    if (user.get("user_type") or "").strip().lower() != user_type:
        raise SystemExit(
            f"DB user_type is {user.get('user_type')!r}, requested --role {user_type!r}"
        )

    payload = {
        "user_id": user["id"],
        "user_type": user_type,
        "phone_number": phone,
        "first_name": (user.get("first_name") or "").strip(),
    }
    token = jwt.encode(payload, secret, algorithm="HS256")

    print(
        f"# user_id={user['id']} name={user.get('first_name')} phone={phone}"
    )
    print(f"export SENIOR_JWT='{token}'" if user_type == "senior" else f"export AGENT_JWT='{token}'")
    print(token)


if __name__ == "__main__":
    main()
