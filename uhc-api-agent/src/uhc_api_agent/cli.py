"""CLI for the standalone API agent.

Examples
--------
  api-agent call https://api.example.com/v1/users
  api-agent call https://api.example.com/v1/users --bearer sk-xyz
  api-agent call https://api.example.com/v1/login --method POST --json '{"u":"x"}'
  api-agent call https://api.example.com/v1/users --basic alice:s3cret
  api-agent call https://api.example.com/v1/users --header "X-Tenant: acme"
  api-agent call https://api.example.com/v1/users --api-key abc123 --api-key-header X-API-Key

  api-agent register https://api.example.com/v1/users --bearer sk-xyz --name "User list"
  api-agent endpoints
  api-agent show https://api.example.com/v1/users
  api-agent history --url https://api.example.com/v1/users --limit 20
  api-agent delete https://api.example.com/v1/users
"""
from __future__ import annotations

import json as _json
import sys
from typing import Optional

import click

from .pipeline import ApiAgentPipeline
from .store import AUTH_TYPES, AuthSpec


# ── shared auth flag helpers ──────────────────────────────────────────────────

def _auth_options(f):
    """Common --bearer / --basic / --api-key / --header flags."""
    f = click.option("--bearer",          default=None, help="Bearer token (Authorization header).")(f)
    f = click.option("--basic",           default=None, help="Basic auth as 'user:password'.")(f)
    f = click.option("--api-key",         default=None, help="API key value.")(f)
    f = click.option("--api-key-header",  default="Authorization",
                     show_default=True, help="Header name for --api-key.")(f)
    f = click.option("--header", "headers", multiple=True,
                     help="Extra header 'Key: Value' (repeatable).")(f)
    return f


def _build_auth(bearer, basic, api_key, api_key_header, headers) -> AuthSpec:
    extra_headers: dict = {}
    for h in headers or ():
        if ":" not in h:
            raise click.UsageError(f"--header must be 'Key: Value', got: {h}")
        k, v = h.split(":", 1)
        extra_headers[k.strip()] = v.strip()

    if bearer:
        return AuthSpec(type="bearer", token=bearer, headers=extra_headers)
    if basic:
        if ":" not in basic:
            raise click.UsageError("--basic must be 'user:password'.")
        u, p = basic.split(":", 1)
        return AuthSpec(type="basic", username=u, password=p, headers=extra_headers)
    if api_key:
        return AuthSpec(type="api_key", api_key=api_key,
                        header_name=api_key_header, headers=extra_headers)
    if extra_headers:
        return AuthSpec(type="custom", headers=extra_headers)
    return AuthSpec(type="none")


def _parse_kv(items, flag: str) -> dict:
    out: dict = {}
    for raw in items or ():
        if "=" not in raw:
            raise click.UsageError(f"{flag} must be 'key=value', got: {raw}")
        k, v = raw.split("=", 1)
        out[k.strip()] = v.strip()
    return out


# ── root group ────────────────────────────────────────────────────────────────

@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--env", default=None,
              help="Path to a .env file (auto-discovered if omitted).")
@click.pass_context
def main(ctx, env):
    """LangGraph agent: URL in, JSON out, with persistent auth."""
    try:
        ctx.obj = ApiAgentPipeline(env_path=env)
    except FileNotFoundError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


# ── call ──────────────────────────────────────────────────────────────────────

@main.command()
@click.argument("url")
@click.option("--method", default="GET", show_default=True,
              help="HTTP method.")
@click.option("--json", "body_json", default=None,
              help="Request body as a JSON string.")
@click.option("--data", "body_data", default=None,
              help="Raw request body (sent as-is).")
@click.option("--query", "queries", multiple=True,
              help="Query param 'key=value' (repeatable).")
@click.option("--name", default="", help="Friendly name to store with the endpoint.")
@click.option("--save-auth/--no-save-auth", default=False,
              help="Persist the auth even if it matches what is stored.")
@click.option("--use-cache/--no-cache", default=False,
              help="Read the response from Redis cache when available (GET only).")
@click.option("--pretty/--raw", default=True, show_default=True,
              help="Pretty-print JSON output.")
@_auth_options
@click.pass_obj
def call(pipeline: ApiAgentPipeline, url, method, body_json, body_data, queries,
         name, save_auth, use_cache, pretty,
         bearer, basic, api_key, api_key_header, headers):
    """Call URL once. Saves URL + auth on first use."""
    auth = _build_auth(bearer, basic, api_key, api_key_header, headers)

    body: object = None
    if body_json is not None:
        try:
            body = _json.loads(body_json)
        except _json.JSONDecodeError as e:
            raise click.UsageError(f"--json is not valid JSON: {e}")
    elif body_data is not None:
        body = body_data

    out = pipeline.run(
        url=url,
        method=method,
        auth=auth if auth.type != "none" or auth.headers else None,
        body=body,
        query=_parse_kv(queries, "--query"),
        name=name,
        save_auth=save_auth,
        use_cache=use_cache,
    )
    click.echo(_json.dumps(out, indent=2 if pretty else None, default=str))
    sys.exit(0 if out.get("success") else 2)


# ── register ──────────────────────────────────────────────────────────────────

@main.command()
@click.argument("url")
@click.option("--method", default="GET", show_default=True)
@click.option("--name",   default="",   help="Friendly name.")
@click.option("--query", "queries", multiple=True,
              help="Default query param 'key=value' (repeatable).")
@_auth_options
@click.pass_obj
def register(pipeline: ApiAgentPipeline, url, method, name, queries,
             bearer, basic, api_key, api_key_header, headers):
    """Save URL + auth without making a call."""
    auth = _build_auth(bearer, basic, api_key, api_key_header, headers)
    eid = pipeline.register(
        url=url, method=method, auth=auth,
        headers=auth.headers, query=_parse_kv(queries, "--query"),
        name=name,
    )
    click.echo(_json.dumps({"endpoint_id": eid, "method": method, "url": url,
                            "auth_type": auth.type, "name": name}, indent=2))


# ── endpoints ─────────────────────────────────────────────────────────────────

@main.command()
@click.pass_obj
def endpoints(pipeline: ApiAgentPipeline):
    """List every saved endpoint."""
    rows = pipeline.list_endpoints()
    if not rows:
        click.echo("(no endpoints registered)")
        return
    click.echo(_json.dumps(rows, indent=2))


# ── show ──────────────────────────────────────────────────────────────────────

@main.command()
@click.argument("url")
@click.option("--method", default="GET", show_default=True)
@click.option("--reveal/--mask", default=False,
              help="Reveal stored secrets (default: masked).")
@click.pass_obj
def show(pipeline: ApiAgentPipeline, url, method, reveal):
    """Show stored config for one endpoint."""
    rec = pipeline.store.load_endpoint(method, url)
    if not rec:
        click.echo(f"No endpoint registered for {method.upper()} {url}", err=True)
        sys.exit(1)

    auth = dict(rec.get("auth") or {})
    if not reveal:
        for key in ("token", "password", "api_key"):
            if auth.get(key):
                auth[key] = "***"
    rec["auth"] = auth
    click.echo(_json.dumps(rec, indent=2, default=str))


# ── delete ────────────────────────────────────────────────────────────────────

@main.command()
@click.argument("url")
@click.option("--method", default="GET", show_default=True)
@click.confirmation_option(prompt="Delete this endpoint and its cached response?")
@click.pass_obj
def delete(pipeline: ApiAgentPipeline, url, method):
    """Delete a saved endpoint."""
    ok = pipeline.delete_endpoint(url=url, method=method)
    click.echo("deleted" if ok else "not found")
    sys.exit(0 if ok else 1)


# ── history ───────────────────────────────────────────────────────────────────

@main.command()
@click.option("--url",   default="", help="Filter history to a single URL.")
@click.option("--limit", default=20, show_default=True)
@click.pass_obj
def history(pipeline: ApiAgentPipeline, url, limit):
    """Show recent call log entries."""
    rows = pipeline.history(url=url, limit=limit)
    if not rows:
        click.echo("(no calls logged)")
        return
    click.echo(_json.dumps(rows, indent=2))


# ── auth-types helper ─────────────────────────────────────────────────────────

@main.command("auth-types")
def auth_types_cmd():
    """List supported auth types."""
    click.echo(_json.dumps(sorted(AUTH_TYPES), indent=2))


if __name__ == "__main__":
    main()
