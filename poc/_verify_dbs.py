"""Verify every ingested POC SOP landed in Postgres, Neo4j, Mongo and Redis.

Run:
    PYTHONPATH=. python poc/_verify_dbs.py
"""
from __future__ import annotations

import os
import json

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "sop_backend.settings")
import django  # noqa: E402

django.setup()

from sop_ingestion.models import (  # noqa: E402
    IngestionJob, AuditSop, AuditStep, AuditDecision, AuditCode,
    AuditGraphNode, AuditGraphEdge,
)
from uhc_sop_ingestion.config import (  # noqa: E402
    PipelineConfig, get_neo4j, get_mongo, get_redis,
)

cfg = PipelineConfig.from_env()


def hr(t):
    print("\n" + "=" * 70)
    print(t)
    print("=" * 70)


hr("POSTGRES — IngestionJob rows")
for j in IngestionJob.objects.order_by("created_at"):
    print(f"  {j.status:10s} url=...{j.seed_url[-55:]:55s} "
          f"llm={j.total_llm_calls}")

hr("POSTGRES — AuditSop (the SOP documents)")
sops = list(AuditSop.objects.order_by("id"))
print(f"  total AuditSop rows: {len(sops)}")
for s in sops:
    steps = AuditStep.objects.filter(sop=s).count()
    decs = AuditDecision.objects.filter(step__sop=s).count()
    codes = AuditCode.objects.filter(sop=s).count()
    gnodes = AuditGraphNode.objects.filter(sop=s).count()
    gedges = AuditGraphEdge.objects.filter(sop=s).count()
    print(f"  #{s.id:<3} {s.title[:48]:48s} steps={steps:<3} "
          f"dec={decs:<4} codes={codes:<3} gnode={gnodes:<4} gedge={gedges}")

hr("NEO4J — SopDocument nodes + label counts")
drv = get_neo4j(cfg)
with drv.session(database=cfg.neo4j_database) as ses:
    docs = ses.run(
        "MATCH (d:SopDocument) RETURN d.sop_id AS id, d.title AS title "
        "ORDER BY d.title").data()
    print(f"  SopDocument nodes: {len(docs)}")
    for d in docs:
        print(f"    {d['id']:<28} {(d['title'] or '')[:46]}")
    print("\n  Node label counts:")
    for r in ses.run(
        "MATCH (n) UNWIND labels(n) AS l RETURN l AS label, count(*) AS c "
        "ORDER BY c DESC").data():
        print(f"    {r['label']:<18} {r['c']}")
    print("\n  Relationship type counts:")
    for r in ses.run(
        "MATCH ()-[r]->() RETURN type(r) AS t, count(*) AS c "
        "ORDER BY c DESC").data():
        print(f"    {r['t']:<20} {r['c']}")
    # Per-document step + rule counts
    print("\n  Per-SopDocument Step / Rule / Code:")
    for r in ses.run("""
        MATCH (d:SopDocument)
        OPTIONAL MATCH (d)-[:HAS_STEP]->(st:Step)
        OPTIONAL MATCH (st)-[:HAS_RULE]->(rl:Rule)
        OPTIONAL MATCH (d)-[:USES_CODE]->(c:Code)
        RETURN d.title AS title, count(DISTINCT st) AS steps,
               count(DISTINCT rl) AS rules, count(DISTINCT c) AS codes
        ORDER BY title""").data():
        print(f"    {(r['title'] or '')[:46]:46s} steps={r['steps']:<3} "
              f"rules={r['rules']:<4} codes={r['codes']}")

hr("MONGO — raw + parsed documents")
mdb = get_mongo(cfg)[cfg.mongo_database]
print(f"  ingestion_jobs:    {mdb['ingestion_jobs'].count_documents({})}")
print(f"  raw_documents:     {mdb['raw_documents'].count_documents({})}")
print(f"  parsed_documents:  {mdb['parsed_documents'].count_documents({})}")
for d in mdb["parsed_documents"].find({}, {"url": 1, "neo4j_sop_id": 1,
                                           "steps": 1}):
    nsteps = len(d.get("steps") or [])
    print(f"    {d.get('neo4j_sop_id',''):<28} steps={nsteps:<3} "
          f"...{d.get('url','')[-45:]}")

hr("REDIS — job + anchor keys")
r = get_redis(cfg)
job_keys = list(r.scan_iter("sop:job:*"))
anchor_keys = list(r.scan_iter("sop:anchors:*"))
print(f"  sop:job:* keys:     {len(job_keys)}")
print(f"  sop:anchors:* keys: {len(anchor_keys)}")
for k in sorted(job_keys):
    ktype = r.type(k)
    if ktype == "hash":
        h = r.hgetall(k)
        print(f"    {k}  status={h.get('status')} "
              f"processed={h.get('total_processed')}")
    elif ktype == "set":
        print(f"    {k}  (set size={r.scard(k)})")
    else:
        print(f"    {k}  (type={ktype})")

print("\nDONE.")
