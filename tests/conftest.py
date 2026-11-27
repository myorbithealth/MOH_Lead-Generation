"""Shared test setup: no test may reach DNS through the gate's default MX check."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _offline_mx(monkeypatch):
    monkeypatch.setattr("moh_leadgen.outreach.gate.DEFAULT_MX_CHECK", lambda domain: None)
    # has_mx() runs a one-time control lookup against the real resolver; tests never should
    monkeypatch.setattr("moh_leadgen.enrich.email_hygiene._MX_TRUST", {"ok": True})
