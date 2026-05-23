"""
agent_tools.tools
=================

Slim, repo-local copies of the 18 LangChain tools from
``extracted-tools-main``. Each tool keeps its original public **name** and
input/output schema but its dependencies (logging, cache, SQL, LLM) come
from the lightweight shims in this package:

* :mod:`agent_tools.tools._logging`  — stdlib logger wrapper.
* :mod:`agent_tools.tools._cache`    — in-process TTL cache.
* :mod:`agent_tools.tools._http`     — requests with retry.
* :mod:`agent_tools.tools._sql_memory` — pymssql-free in-memory backend.

The 18 LangChain tools, in registry order, are exposed by
:func:`agent_tools.registry.iter_tools` and the convenience exports below.
"""
from __future__ import annotations

# The tool modules import each other lazily inside ``iter_tools`` so a
# missing optional dependency in one tool doesn't break the whole import
# graph.  Keep this __init__ free of heavy imports.
