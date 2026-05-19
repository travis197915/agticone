"""Public API for the API-calling agent.

    from uhc_api_agent import ApiAgentPipeline

    pipeline = ApiAgentPipeline()

    # First call: provide auth — it's stored against the URL automatically
    pipeline.run(
        url="https://api.example.com/v1/me",
        auth={"type": "bearer", "token": "sk-xyz"},
    )

    # Subsequent calls: just give the URL, stored auth is reused
    out = pipeline.run(url="https://api.example.com/v1/me")
    print(out["status_code"], out["json"])
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from .config import AgentConfig
from .graph import build_graph
from .store import AuthSpec, CredentialStore


class ApiAgentPipeline:
    """LangGraph pipeline that hits an HTTP API and returns JSON.

    Parameters
    ----------
    env_path : str | Path, optional
        Explicit path to a .env file. Omit to auto-discover.
    config : AgentConfig, optional
        Pass a pre-built config (overrides env_path).
    """

    def __init__(
        self,
        env_path: Optional[str | Path] = None,
        config: Optional[AgentConfig] = None,
    ):
        self.cfg = config or AgentConfig.from_env(env_path)
        self.store = CredentialStore(self.cfg)
        self._graph = build_graph(self.cfg)

    # ── primary entry point ──────────────────────────────────────────────────

    def run(
        self,
        url: str,
        *,
        method: str = "GET",
        auth: dict | AuthSpec | None = None,
        body: Any = None,
        query: dict | None = None,
        headers: dict | None = None,
        name: str = "",
        save_auth: bool = False,
        use_cache: bool = False,
    ) -> dict:
        """Call URL, return a dict with the parsed JSON + audit info.

        Auth-handling
        -------------
        - Pass `auth=` on the first call to register credentials.
        - On later calls to the same URL the stored auth is loaded automatically;
          omit `auth=` and it Just Works™.
        - Set `save_auth=True` to force-save even when reusing stored auth
          (handy from the `register` CLI subcommand).
        """
        if not url:
            raise ValueError("url must be provided")

        if isinstance(auth, AuthSpec):
            auth_dict = auth.to_dict()
        elif isinstance(auth, dict):
            auth_dict = auth
        else:
            auth_dict = {}

        initial = {
            "url": url,
            "method": method,
            "body": body,
            "query": query or {},
            "headers": headers or {},
            "auth_input": auth_dict,
            "name": name,
            "save_auth": save_auth,
            "use_cache": use_cache,
            "stages": [],
        }
        final = self._graph.invoke(initial)

        # Return only the publicly useful fields (skip resolved_auth which has secrets).
        return {
            "call_id":        final.get("call_id"),
            "endpoint_id":    final.get("endpoint_id"),
            "method":         final.get("method"),
            "url":            final.get("url"),
            "status_code":    final.get("status_code"),
            "duration_ms":    final.get("duration_ms"),
            "response_bytes": final.get("response_bytes"),
            "is_json":        final.get("is_json", False),
            "json":           final.get("json"),
            "response_text":  None if final.get("is_json") else final.get("response_text"),
            "success":        final.get("success", False),
            "cache_hit":      final.get("cache_hit", False),
            "error":          final.get("error", ""),
            "stages":         final.get("stages", []),
        }

    # ── convenience wrappers around the store ────────────────────────────────

    def register(
        self,
        url: str,
        *,
        method: str = "GET",
        auth: dict | AuthSpec | None = None,
        headers: dict | None = None,
        query: dict | None = None,
        name: str = "",
    ) -> str:
        """Save endpoint + auth without making a call. Returns endpoint_id."""
        spec = auth if isinstance(auth, AuthSpec) else AuthSpec.from_dict(auth)
        return self.store.save_endpoint(
            method=method, url=url, auth=spec,
            name=name, default_headers=headers, default_query=query,
        )

    def list_endpoints(self) -> list[dict]:
        return self.store.list_endpoints()

    def history(self, *, url: str = "", limit: int = 50) -> list[dict]:
        return self.store.list_history(url=url, limit=limit)

    def delete_endpoint(self, url: str, *, method: str = "GET") -> bool:
        return self.store.delete_endpoint(method=method, url=url)
