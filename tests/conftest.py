"""Shared pytest configuration."""

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable loading the integration from the local custom_components tree."""
    return enable_custom_integrations
