#!/usr/bin/env python3
"""Standalone loader — push pre-built SOP HTML templates into Mongo.

Loads the given HTML SOP templates into the ``sop_source_html`` MongoDB
collection so the claim-detail **"View SOP"** action can serve them, in the
EXACT document shape ``sop_ingestion.sop_html_crawler`` writes (keyed by
``{"sop_id": <int>}``). This is the manual counterpart to the crawler for SOPs
whose source is a local ``file://`` upload (which the crawler cannot fetch).

This is a STANDALONE script, NOT a Django management command. Run it directly.

For each HTML file it looks up every ``sop_ingestion_auditsop`` row whose title
matches that SOP (hash-prefixed ``<hash>_OBH_Facets_Timely_Filing`` or clean
``OBH Facets Timely Filing`` — both match) and upserts one Mongo doc per
``sop_id``, so whichever row a claim's trace resolves to will find the HTML.

Usage
-----
    # Prod (Atlas Mongo + managed Postgres)
    APP_ENV=prod \
    MONGO_URI='mongodb+srv://user:pass@cluster.mongodb.net/?retryWrites=true' \
    MONGO_DATABASE='uhc' \
    PG_HOST=... PG_PORT=5432 PG_USER=... PG_PASSWORD=... PG_DATABASE=... \
    [PG_SSLMODE=require] \
    python scripts/load_sop_html_to_mongo.py

    # Local docker Mongo/Postgres (APP_ENV unset)
    MONGO_HOST=127.0.0.1 MONGO_PORT=27017 MONGO_DATABASE=uhc_backend \
    PG_HOST=127.0.0.1 PG_PORT=5432 PG_USER=postgres PG_PASSWORD=postgres \
    PG_DATABASE=uhc_backend \
    python scripts/load_sop_html_to_mongo.py --dry-run

Options
-------
    files...                 HTML files to load (default: the three OBH SOPs).
    --html-dir DIR           Directory the files live in (default: repo root).
    --collection NAME        Mongo collection (default: sop_source_html).
    --force-sop-id FILE=ID   Force a file onto an explicit sop_id (repeatable);
                             bypasses the Postgres title match for that file.
    --dry-run                Resolve + report, write nothing.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_COLLECTION = "sop_source_html"
DEFAULT_FILES = [
    "OBH_Facets_Duplicate_Claim_Handling.html",
    "OBH_Facets_Provider_Selection_Guidelines.html",
    "OBH_Facets_Timely_Filing.html",
]


def _norm(s: str) -> str:
    """Lowercase alphanumeric-only key for tolerant title matching."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _mongo_uri() -> str:
    """Resolve the Mongo URI. Reuse the project's env-gated helper when it is
    importable (so prod uses the Atlas ``MONGO_URI``); otherwise build it from
    the raw env vars."""
    try:
        sys.path.insert(0, str(REPO_ROOT))
        from sop_backend.db_config import mongo_uri_from_env  # type: ignore
        return mongo_uri_from_env()
    except Exception:
        uri = (os.environ.get("MONGO_URI") or "").strip()
        if uri and os.environ.get("APP_ENV", "").lower() in {"prod", "production"}:
            return uri
        host = os.environ.get("MONGO_HOST", "127.0.0.1")
        port = os.environ.get("MONGO_PORT", "27017")
        user = os.environ.get("MONGO_USER") or ""
        pwd = os.environ.get("MONGO_PASSWORD") or ""
        from urllib.parse import quote_plus
        creds = f"{quote_plus(user)}:{quote_plus(pwd)}@" if (user and pwd) else ""
        return f"mongodb://{creds}{host}:{port}/"


def _mongo_collection(name: str):
    from pymongo import MongoClient
    db = os.environ.get("MONGO_DATABASE")
    if not db:
        sys.exit("ERROR: MONGO_DATABASE is not set.")
    client = MongoClient(_mongo_uri(), serverSelectionTimeoutMS=15000)
    return client[db][name]


def _pg_connect():
    import psycopg2
    kwargs = dict(
        host=os.environ.get("PG_HOST"),
        port=os.environ.get("PG_PORT"),
        user=os.environ.get("PG_USER"),
        password=os.environ.get("PG_PASSWORD"),
        dbname=os.environ.get("PG_DATABASE"),
        options="-c search_path=public,agent_tools",
    )
    sslmode = os.environ.get("PG_SSLMODE")
    if sslmode:
        kwargs["sslmode"] = sslmode
    if not (kwargs["host"] and kwargs["dbname"]):
        sys.exit("ERROR: PG_HOST / PG_DATABASE (+ user/password) must be set.")
    return psycopg2.connect(**kwargs)


def _load_auditsops() -> list[tuple[int, str, str]]:
    """Return [(id, title, url)] for every current AuditSop row."""
    conn = _pg_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, title, url FROM sop_ingestion_auditsop "
                "WHERE is_current = TRUE ORDER BY id"
            )
            return [(int(r[0]), r[1] or "", r[2] or "") for r in cur.fetchall()]
    finally:
        conn.close()


def _sanitize_html(raw: str) -> str:
    """Strip <script>/<noscript> so the stored template renders inertly. Falls
    back to the raw string when BeautifulSoup is unavailable."""
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(raw, "html.parser")
        for junk in soup(["script", "noscript"]):
            junk.decompose()
        return str(soup)
    except Exception:
        return raw


def _build_doc(sop_id: int, title: str, url: str, html: str,
               source_file: str) -> dict:
    return {
        "sop_id": int(sop_id),
        "title": title or "",
        "source_url": url or "",
        "html": html,
        "pages": [url or f"file://{source_file}"],
        "page_count": 1,
        "byte_size": len(html),
        "crawled_at": datetime.now(timezone.utc).isoformat(),
        "loaded_by": "load_sop_html_to_mongo",
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Load SOP HTML templates into Mongo.")
    ap.add_argument("files", nargs="*", default=DEFAULT_FILES,
                    help="HTML files to load (default: the three OBH SOPs).")
    ap.add_argument("--html-dir", default=str(REPO_ROOT),
                    help="Directory the HTML files live in (default: repo root).")
    ap.add_argument("--collection", default=DEFAULT_COLLECTION,
                    help=f"Mongo collection (default: {DEFAULT_COLLECTION}).")
    ap.add_argument("--force-sop-id", action="append", default=[],
                    metavar="FILE=ID",
                    help="Force a file onto an explicit sop_id (repeatable).")
    ap.add_argument("--dry-run", action="store_true",
                    help="Resolve + report only; write nothing.")
    opts = ap.parse_args()

    html_dir = Path(opts.html_dir).resolve()
    forced: dict[str, list[int]] = {}
    for spec in opts.force_sop_id:
        if "=" not in spec:
            sys.exit(f"ERROR: --force-sop-id expects FILE=ID, got {spec!r}")
        fname, ids = spec.split("=", 1)
        forced.setdefault(Path(fname).name, []).extend(
            int(x) for x in ids.split(",") if x.strip()
        )

    # Read every file up front so a missing file fails before we touch the DB.
    files: list[tuple[str, str]] = []  # (filename, html)
    for f in opts.files:
        p = html_dir / f
        if not p.is_file():
            sys.exit(f"ERROR: HTML file not found: {p}")
        files.append((p.name, _sanitize_html(p.read_text(encoding="utf-8"))))

    # Resolve sop_ids: explicit overrides win; otherwise match by title.
    auditsops = [] if all(fn in forced for fn, _ in files) else _load_auditsops()
    norm_sops = [(sid, title, url, _norm(title)) for sid, title, url in auditsops]

    col = None if opts.dry_run else _mongo_collection(opts.collection)

    grand_total = 0
    for fname, html in files:
        stem = _norm(Path(fname).stem)
        if fname in forced:
            matches = [
                (sid, next((t for s, t, u in auditsops if s == sid), ""),
                 next((u for s, t, u in auditsops if s == sid), ""))
                for sid in forced[fname]
            ]
            how = "forced"
        else:
            matches = [(sid, title, url) for sid, title, url, nt in norm_sops
                       if nt.endswith(stem) or stem in nt]
            how = "matched"
        if not matches:
            print(f"  [SKIP] {fname}: no AuditSop title matched "
                  f"(normalized stem={stem!r}); use --force-sop-id.")
            continue

        print(f"  {fname} ({len(html)} bytes) -> {how} sop_id(s): "
              f"{', '.join(str(m[0]) for m in matches)}")
        for sid, title, url in matches:
            doc = _build_doc(sid, title, url, html, fname)
            if opts.dry_run:
                print(f"      [dry-run] would upsert sop_id={sid} title={title[:60]!r}")
            else:
                col.replace_one({"sop_id": sid}, doc, upsert=True)
                print(f"      upserted sop_id={sid} title={title[:60]!r}")
            grand_total += 1

    verb = "would load" if opts.dry_run else "loaded"
    print(f"\nDone. {verb} {grand_total} SOP HTML doc(s) into "
          f"'{opts.collection}'.")


if __name__ == "__main__":
    main()
