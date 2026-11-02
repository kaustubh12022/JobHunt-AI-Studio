"""
JobHunt AI Studio - Browser Local Storage Integration
Provides client-side persistence for user credentials and candidate profiles
without storing sensitive keys on the server or in the database.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Optional

import streamlit.components.v1 as components

logger = logging.getLogger("JobHuntAIStudio")

_COMPONENT_PATH = Path(__file__).parent / "browser_storage"

# Safely declare the component
try:
    _storage_component = components.declare_component(
        "jobhunt_browser_storage",
        path=str(_COMPONENT_PATH),
    )
except Exception as e:
    logger.warning(f"Could not declare browser_storage component: {e}")
    _storage_component = None


def sync_browser_storage(
    action: str = "read",
    payload: Optional[Dict[str, Any]] = None,
    key: str = "browser_storage_bridge",
) -> Optional[Dict[str, Any]]:
    """
    Interacts with client-side browser localStorage:
    - action='read': reads and returns current stored data
    - action='save': writes payload to browser localStorage
    - action='clear': deletes stored data from browser localStorage

    Returns the stored dictionary if present, or None.
    """
    if _storage_component is None:
        return None

    try:
        val = _storage_component(
            action=action,
            payload=payload or {},
            key=key,
            default=None,
        )
        if isinstance(val, dict):
            return val
    except Exception as e:
        logger.debug(f"browser_storage exception (safe to ignore in non-browser/test envs): {e}")

    return None
