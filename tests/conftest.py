"""Shared test setup: no test may reach DNS through the gate's default MX check."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _offline_mx(monkeypatch):
    monkeypatch.setattr("moh_leadgen.outreach.gate.DEFAULT_MX_CHECK", lambda domain: None)
