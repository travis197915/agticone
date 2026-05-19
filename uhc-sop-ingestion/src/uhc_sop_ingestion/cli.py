"""CLI — sop-ingest <URL>

Examples:
  sop-ingest https://intranet.uhc.com/sops/OBH_Facets_Duplicate_Claim.html
  sop-ingest ./OBH_Facets_Timely_Filing.html --max-depth 3 --max-docs 50
  sop-ingest <URL> --env /path/to/.env --stream
"""
from __future__ import annotations

import json
import sys
import click

from .pipeline import SopIngestionPipeline


@click.command()
@click.argument("url")
@click.option("--env",       default=None,  help="Path to .env file")
@click.option("--job-id",    default=None,  help="Custom job UUID")
@click.option("--max-depth", default=4,     show_default=True, help="Max link depth")
@click.option("--max-docs",  default=200,   show_default=True, help="Max documents to ingest")
@click.option("--stream",    is_flag=True,  help="Stream node-by-node progress to stdout")
def main(url, env, job_id, max_depth, max_docs, stream):
    """Ingest a SOP document tree starting from URL."""
    try:
        pipeline = SopIngestionPipeline(env_path=env)
    except FileNotFoundError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    if stream:
        click.echo(f"[sop-ingest] Starting stream for: {url}", err=True)
        for node_name, delta in pipeline.stream(url, job_id=job_id,
                                                max_depth=max_depth, max_docs=max_docs):
            # Print meaningful progress lines
            if node_name in ("fetch_stage", "enrich_stage", "write_neo4j",
                             "write_postgres", "link_stage"):
                current = delta.get("current_url","") or delta.get("neo4j_sop_id","")
                if current:
                    click.echo(f"[{node_name}] {current}", err=True)
        click.echo(json.dumps({"status": "complete"}, indent=2))
    else:
        click.echo(f"[sop-ingest] Ingesting: {url}", err=True)
        result = pipeline.run(url, job_id=job_id,
                              max_depth=max_depth, max_docs=max_docs)
        click.echo(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
