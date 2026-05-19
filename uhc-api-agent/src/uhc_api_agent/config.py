"""Centralised configuration for the standalone API agent.

Reads the SAME .env file used by uhc-sop-ingestion (PG_*, REDIS_*, MONGO_*).
No new credentials are required — the agent reuses the existing DBs.

Usage:
    from uhc_api_agent.config import AgentConfig
    cfg = AgentConfig.from_env()                       # auto-finds .env
    cfg = AgentConfig.from_env("/path/to/.env")        # explicit
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


# ── .env loader ───────────────────────────────────────────────────────────────

def load_env(env_path: str | Path | None = None) -> None:
    """Load KEY=VALUE pairs from a .env file into os.environ (idempotent)."""
    try:
        from dotenv import load_dotenv
    except ImportError as e:
        raise RuntimeError("python-dotenv is required: pip install python-dotenv") from e

    if env_path:
        path = Path(env_path)
        if not path.exists():
            raise FileNotFoundError(f".env not found: {path}")
        load_dotenv(dotenv_path=path, override=False)
        return

    search = Path.cwd()
    for candidate in [search, *search.parents]:
        f = candidate / ".env"
        if f.exists():
            load_dotenv(dotenv_path=f, override=False)
            return


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _env_int(key: str, default: int) -> int:
    return int(os.environ.get(key, str(default)).strip())


# ── Config dataclass ──────────────────────────────────────────────────────────

@dataclass
class AgentConfig:
    # Postgres — endpoint registry + call history (system of record)
    pg_host: str
    pg_port: int
    pg_user: str
    pg_password: str
    pg_database: str

    # MongoDB — raw JSON response archive
    mongo_host: str
    mongo_port: int
    mongo_user: str
    mongo_password: str
    mongo_database: str

    # Redis — endpoint lookup + response cache (short-lived)
    redis_host: str
    redis_port: int
    redis_user: str
    redis_password: str

    # Agent runtime knobs
    http_timeout: int           # seconds for the HTTP call
    cache_ttl: int              # Redis cache TTL in seconds
    max_response_bytes: int     # safety limit on body size

    # ── Computed connection strings ───────────────────────────────────────────

    @property
    def pg_dsn(self) -> str:
        return (
            f"host={self.pg_host} port={self.pg_port} "
            f"dbname={self.pg_database} user={self.pg_user} "
            f"password={self.pg_password}"
        )

    @property
    def mongo_uri(self) -> str:
        return (
            f"mongodb://{self.mongo_user}:{self.mongo_password}"
            f"@{self.mongo_host}:{self.mongo_port}/"
        )

    @property
    def redis_url(self) -> str:
        return (
            f"redis://{self.redis_user}:{self.redis_password}"
            f"@{self.redis_host}:{self.redis_port}/0"
        )

    # ── Factory ───────────────────────────────────────────────────────────────

    @classmethod
    def from_env(cls, env_path: str | Path | None = None) -> "AgentConfig":
        load_env(env_path)
        return cls(
            pg_host=_env("PG_HOST"),
            pg_port=_env_int("PG_PORT", 5432),
            pg_user=_env("PG_USER", "postgres"),
            pg_password=_env("PG_PASSWORD"),
            pg_database=_env("PG_DATABASE", "postgres"),

            mongo_host=_env("MONGO_HOST"),
            mongo_port=_env_int("MONGO_PORT", 27017),
            mongo_user=_env("MONGO_USER", "admin"),
            mongo_password=_env("MONGO_PASSWORD"),
            mongo_database=_env("MONGO_DATABASE", "sop_ingestion"),

            redis_host=_env("REDIS_HOST"),
            redis_port=_env_int("REDIS_PORT", 6379),
            redis_user=_env("REDIS_USER", "default"),
            redis_password=_env("REDIS_PASSWORD"),

            http_timeout=_env_int("API_AGENT_TIMEOUT", 30),
            cache_ttl=_env_int("API_AGENT_CACHE_TTL", 300),
            max_response_bytes=_env_int("API_AGENT_MAX_BYTES", 50 * 1024 * 1024),
        )


# ── Lazy connection singletons ────────────────────────────────────────────────

_pg_conn = None
_mongo_client = None
_redis_client = None


def get_pg(cfg: AgentConfig):
    """Return a persistent psycopg2 connection (autocommit)."""
    global _pg_conn
    if _pg_conn is None or _pg_conn.closed:
        import psycopg2
        _pg_conn = psycopg2.connect(cfg.pg_dsn)
        _pg_conn.autocommit = True
    return _pg_conn


def get_mongo(cfg: AgentConfig):
    global _mongo_client
    if _mongo_client is None:
        from pymongo import MongoClient
        _mongo_client = MongoClient(cfg.mongo_uri, serverSelectionTimeoutMS=10000)
    return _mongo_client


def get_redis(cfg: AgentConfig):
    global _redis_client
    if _redis_client is None:
        import redis
        _redis_client = redis.Redis(
            host=cfg.redis_host, port=cfg.redis_port,
            username=cfg.redis_user, password=cfg.redis_password,
            decode_responses=True, socket_connect_timeout=10,
        )
    return _redis_client
