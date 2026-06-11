"""Centralised configuration loaded from .env (KEY=VALUE format).

Resolution order:
  1. Explicit .env file path passed to load_env() (if it exists)
  2. .env in current working directory
  3. .env walking up parent directories (finds project root automatically)
  4. Already-set environment variables (docker, CI, etc.)

  If an explicit path is given but missing, steps 2–4 apply instead of raising.

Usage:
    from uhc_sop_ingestion.config import PipelineConfig
    cfg = PipelineConfig.from_env()          # auto-finds .env
    cfg = PipelineConfig.from_env("/path/to/.env")   # explicit path
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


# ── .env loader ───────────────────────────────────────────────────────────────

def load_env(env_path: str | Path | None = None) -> None:
    """Load KEY=VALUE pairs from a .env file into os.environ.

    If env_path is given and exists, loads that file. Otherwise searches from
    cwd upward for the first .env file. If none is found, uses already-set
    environment variables (Docker / CI). Already-set env vars are NOT
    overridden (dotenv override=False behaviour).
    """
    try:
        from dotenv import load_dotenv
    except ImportError:
        raise RuntimeError("python-dotenv is required: pip install python-dotenv")

    if env_path:
        path = Path(env_path)
        if path.exists():
            load_dotenv(dotenv_path=path, override=False)
            return

    # Walk up from cwd until we find a .env
    search = Path.cwd()
    for candidate in [search, *search.parents]:
        dotenv_file = candidate / ".env"
        if dotenv_file.exists():
            load_dotenv(dotenv_path=dotenv_file, override=False)
            return

    # Nothing found — env vars may already be set (Docker / CI)


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()

def _env_int(key: str, default: int) -> int:
    return int(os.environ.get(key, str(default)).strip())


# ── Config dataclass ──────────────────────────────────────────────────────────

@dataclass
class PipelineConfig:
    # Postgres
    pg_host: str
    pg_port: int
    pg_user: str
    pg_password: str
    pg_database: str

    # Redis
    redis_host: str
    redis_port: int
    redis_user: str
    redis_password: str

    # Neo4j
    neo4j_host: str
    neo4j_port: int
    neo4j_user: str
    neo4j_password: str
    neo4j_database: str

    # MongoDB
    mongo_host: str
    mongo_port: int
    mongo_user: str
    mongo_password: str
    mongo_database: str

    # RabbitMQ
    rabbitmq_host: str
    rabbitmq_port: int
    rabbitmq_user: str
    rabbitmq_password: str

    # File storage
    storage_access_key: str
    storage_secret: str
    storage_url: str

    # LLM — dual provider (Anthropic for reasoning, OpenAI for extraction)
    openai_api_key: str
    anthropic_api_key: str
    openai_model: str       # default: gpt-4o
    anthropic_model: str    # default: claude-sonnet-4-5-20250929
    # legacy single-provider fields (kept for backward compat)
    llm_provider: str
    llm_model: str

    # Pipeline
    max_depth: int
    max_docs: int

    # ── Computed connection strings ───────────────────────────────────────────

    @property
    def pg_dsn(self) -> str:
        return (
            f"host={self.pg_host} port={self.pg_port} "
            f"dbname={self.pg_database} user={self.pg_user} "
            f"password={self.pg_password}"
        )

    @property
    def redis_url(self) -> str:
        return (
            f"redis://{self.redis_user}:{self.redis_password}"
            f"@{self.redis_host}:{self.redis_port}/0"
        )

    @property
    def neo4j_uri(self) -> str:
        return f"neo4j://{self.neo4j_host}:{self.neo4j_port}"

    @property
    def mongo_uri(self) -> str:
        # Omit the "user:pass@" credentials block when either is empty —
        # an auth-less Mongo (local Docker) rejects "mongodb://:@host".
        if self.mongo_user and self.mongo_password:
            from urllib.parse import quote_plus
            creds = f"{quote_plus(self.mongo_user)}:{quote_plus(self.mongo_password)}@"
        else:
            creds = ""
        return f"mongodb://{creds}{self.mongo_host}:{self.mongo_port}/"

    @property
    def rabbitmq_url(self) -> str:
        return (
            f"amqp://{self.rabbitmq_user}:{self.rabbitmq_password}"
            f"@{self.rabbitmq_host}:{self.rabbitmq_port}/"
        )

    # ── Factory ───────────────────────────────────────────────────────────────

    @classmethod
    def from_env(cls, env_path: str | Path | None = None) -> "PipelineConfig":
        """Load .env then build config from environment variables."""
        load_env(env_path)
        return cls(
            # Postgres
            pg_host=_env("PG_HOST"),
            pg_port=_env_int("PG_PORT", 5432),
            pg_user=_env("PG_USER", "postgres"),
            pg_password=_env("PG_PASSWORD"),
            pg_database=_env("PG_DATABASE", "postgres"),
            # Redis
            redis_host=_env("REDIS_HOST"),
            redis_port=_env_int("REDIS_PORT", 6379),
            redis_user=_env("REDIS_USER", "default"),
            redis_password=_env("REDIS_PASSWORD"),
            # Neo4j
            neo4j_host=_env("NEO4J_HOST"),
            neo4j_port=_env_int("NEO4J_PORT", 7687),
            neo4j_user=_env("NEO4J_USER", "neo4j"),
            neo4j_password=_env("NEO4J_PASSWORD"),
            neo4j_database=_env("NEO4J_DATABASE", "neo4j"),
            # MongoDB
            mongo_host=_env("MONGO_HOST"),
            mongo_port=_env_int("MONGO_PORT", 27017),
            mongo_user=_env("MONGO_USER", "admin"),
            mongo_password=_env("MONGO_PASSWORD"),
            mongo_database=_env("MONGO_DATABASE", "sop_ingestion"),
            # RabbitMQ
            rabbitmq_host=_env("RABBITMQ_HOST"),
            rabbitmq_port=_env_int("RABBITMQ_PORT", 5672),
            rabbitmq_user=_env("RABBITMQ_USER", "guest"),
            rabbitmq_password=_env("RABBITMQ_PASSWORD"),
            # Storage
            storage_access_key=_env("STORAGE_ACCESS_KEY"),
            storage_secret=_env("STORAGE_SECRET"),
            storage_url=_env("STORAGE_URL"),
            # LLM — dual provider
            openai_api_key=_env("OPENAI_API_KEY"),
            anthropic_api_key=_env("ANTHROPIC_API_KEY"),
            openai_model=_env("OPENAI_MODEL", "gpt-4o"),
            anthropic_model=_env("ANTHROPIC_MODEL", "claude-sonnet-4-5-20250929"),
            llm_provider=_env("LLM_PROVIDER", "anthropic"),
            llm_model=_env("LLM_MODEL", "claude-sonnet-4-5-20250929"),
            # Pipeline
            max_depth=_env_int("MAX_DEPTH", 4),
            max_docs=_env_int("MAX_DOCS", 200),
        )


# ── Connection singletons (lazy) ──────────────────────────────────────────────

_redis_client = None
_neo4j_driver = None
_mongo_client = None


def get_redis(cfg: PipelineConfig):
    global _redis_client
    if _redis_client is None:
        import redis
        _redis_client = redis.Redis(
            host=cfg.redis_host, port=cfg.redis_port,
            username=cfg.redis_user, password=cfg.redis_password,
            decode_responses=True, socket_connect_timeout=10,
        )
    return _redis_client


def get_neo4j(cfg: PipelineConfig):
    global _neo4j_driver
    if _neo4j_driver is None:
        from neo4j import GraphDatabase
        _neo4j_driver = GraphDatabase.driver(
            cfg.neo4j_uri, auth=(cfg.neo4j_user, cfg.neo4j_password)
        )
    return _neo4j_driver


def get_mongo(cfg: PipelineConfig):
    global _mongo_client
    if _mongo_client is None:
        from pymongo import MongoClient
        _mongo_client = MongoClient(cfg.mongo_uri, serverSelectionTimeoutMS=10000)
    return _mongo_client


def get_pg_conn(cfg: PipelineConfig):
    import psycopg2
    return psycopg2.connect(cfg.pg_dsn)


def get_llm(cfg: PipelineConfig, provider: str | None = None):
    """Return an LLM client.  provider overrides cfg.llm_provider."""
    prov = provider or cfg.llm_provider
    if prov == "anthropic":
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(
            model=cfg.anthropic_model,
            api_key=cfg.anthropic_api_key,
            max_tokens=4096,
        )
    from langchain_openai import ChatOpenAI
    return ChatOpenAI(
        model=cfg.openai_model or "gpt-4o",
        api_key=cfg.openai_api_key,
        max_tokens=4096,
    )
