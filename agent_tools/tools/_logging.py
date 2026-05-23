"""Logger shim replacing thynkr_bhagenticai.logging_utils.get_logger."""
from __future__ import annotations

import logging


def get_logger(name: str) -> logging.Logger:
    """Drop-in replacement for the missing upstream helper."""
    return logging.getLogger(f"agent_tools.{name}")
