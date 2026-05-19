"""uhc-api-agent — standalone LangGraph agent for API calls.

Quick start:
    from uhc_api_agent import ApiAgentPipeline

    pipeline = ApiAgentPipeline()
    result = pipeline.run(
        url="https://api.example.com/v1/users",
        auth={"type": "bearer", "token": "sk-xyz"},
    )
    print(result["status_code"], result["json"])

Subsequent calls to the same URL reuse the saved auth automatically:
    result = pipeline.run(url="https://api.example.com/v1/users")
"""
from .pipeline import ApiAgentPipeline
from .config import AgentConfig
from .store import CredentialStore

__all__ = ["ApiAgentPipeline", "AgentConfig", "CredentialStore"]
