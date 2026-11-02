"""
Tests for browser storage integration and profile portability (Option A & B).
"""
import pytest
from unittest.mock import patch, MagicMock
from core.browser_storage import sync_browser_storage
from core.profile_manager import (
    DEFAULT_PROFILE_TEMPLATE,
    profile_to_yaml_str,
    load_profile_from_str,
)


def test_sync_browser_storage_headless_fallback():
    """In headless/test environments where component returns None or raises, it safely degrades."""
    res = sync_browser_storage(action="read")
    assert res is None or isinstance(res, dict)


def test_sync_browser_storage_mocked_read():
    """Verify sync_browser_storage correctly receives dict payloads from component."""
    mock_payload = {
        "provider": "Google Gemini",
        "api_keys": "AIzaSyFakeKey123",
        "profile_yaml": "personal_information:\n  name: Test\n",
    }
    with patch("core.browser_storage._storage_component", return_value=mock_payload):
        data = sync_browser_storage(action="read")
        assert data == mock_payload
        assert data["provider"] == "Google Gemini"
        assert data["api_keys"] == "AIzaSyFakeKey123"


def test_sync_browser_storage_mocked_actions():
    """Verify action parameter passing for save and clear."""
    mock_comp = MagicMock(return_value={"status": "ok"})
    with patch("core.browser_storage._storage_component", mock_comp):
        # Save action
        sync_browser_storage(action="save", payload={"api_keys": "key1"})
        mock_comp.assert_called_with(
            action="save",
            payload={"api_keys": "key1"},
            key="browser_storage_bridge",
            default=None,
        )

        # Clear action
        sync_browser_storage(action="clear")
        mock_comp.assert_called_with(
            action="clear",
            payload={},
            key="browser_storage_bridge",
            default=None,
        )


def test_default_profile_template_portability():
    """Ensure DEFAULT_PROFILE_TEMPLATE can be exported to YAML and re-parsed losslessly."""
    yaml_str = profile_to_yaml_str(DEFAULT_PROFILE_TEMPLATE)
    assert "personal_information:" in yaml_str
    assert "education_details:" in yaml_str

    parsed = load_profile_from_str(yaml_str)
    assert parsed["personal_information"]["name"] == "Jane"
    assert parsed["personal_information"]["surname"] == "Doe"
    assert len(parsed["education_details"]) > 0
    assert len(parsed["skills"]) > 0
