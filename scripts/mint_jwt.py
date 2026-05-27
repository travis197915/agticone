#!/usr/bin/env python3
"""Mint a dev JWT for the agentic backend.

The backend's auth class (builder.auth.CorebackendJWTAuthentication) accepts
`Authorization: Bearer <jwt>` signed HS256 with the JWT_SECRET env var.
This script signs a token with the same secret so you can hit protected
endpoints from curl/Postman during local development.

Usage:
    # reads JWT_SECRET from .env or environment
    python scripts/mint_jwt.py

    # override claims
    python scripts/mint_jwt.py --sub alice --email alice@x.com --role MEMBER --days 7

The output is a single JWT on stdout — pipe or paste into your Authorization
header:
    curl -H "Authorization: Bearer $(python scripts/mint_jwt.py)" \\
         http://localhost:8000/api/builder/workflows/
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

try:
    import jwt
except ImportError:
    sys.exit("PyJWT not installed. Run: pip install pyjwt")

try:
    from dotenv import load_dotenv
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if env_path.exists():
        load_dotenv(env_path, override=False)
except ImportError:
    pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--sub",   default="dev-user",
                        help="Subject claim (user id). Default: dev-user")
    parser.add_argument("--email", default="dev@example.com",
                        help="Email claim. Default: dev@example.com")
    parser.add_argument("--role",  default="ADMIN", choices=["ADMIN", "MEMBER"],
                        help="Role claim. Default: ADMIN")
    parser.add_argument("--days",  type=int, default=30,
                        help="Token lifetime in days. Default: 30")
    args = parser.parse_args()

    secret = os.environ.get("JWT_SECRET", "")
    if not secret or len(secret) < 16:
        sys.exit("JWT_SECRET missing or <16 chars. Set it in .env or env.")

    now = int(time.time())
    token = jwt.encode(
        {
            "sub":   args.sub,
            "email": args.email,
            "role":  args.role,
            "iat":   now,
            "exp":   now + args.days * 86400,
        },
        secret,
        algorithm="HS256",
    )
    print(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
