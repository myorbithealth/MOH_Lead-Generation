"""`leadgen daily` keeps going when a step fails, skips steps without keys, and `leadgen ingest` works."""

from __future__ import annotations

from datetime import date

from typer.testing import CliRunner

from moh_leadgen import cli
from moh_leadgen.config import settings
from moh_leadgen.store import LeadStore

runner = CliRunner()


def test_run_steps_isolates_failures():
    calls = []

    def boom():
        calls.append("boom")
        raise ValueError("bad")

    def skip():
        raise cli.StepSkipped("no key")

    rows = cli.run_steps([("a", boom), ("b", skip), ("c", lambda: calls.append("c") or "fine")])
    assert calls == ["boom", "c"]
    assert [r[1] for r in rows] == ["failed", "skipped", "ok"]
    assert "ValueError: bad" in rows[0][2]


def _no_keys(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "db_path", tmp_path / "leads.db")
    for name in ("supabase_url", "supabase_key", "smartlead_api_key", "anthropic_key"):
        monkeypatch.setattr(settings, name, "")


def test_daily_steps_skip_without_keys(monkeypatch, tmp_path):
    _no_keys(monkeypatch, tmp_path)
    monkeypatch.setattr(cli, "discover", lambda *a, **k: 0)
    monkeypatch.setattr(cli, "enrich", lambda *a, **k: 0)
    steps = cli._daily_steps(
        discover_limit=1,
        enrich_limit=1,
        retry_limit=1,
        workers=1,
        min_score=0,
        daily_cap=None,
        do_enroll=True,
        do_classify=True,
        today=date(2026, 11, 30),  # a Monday
    )
    rows = {name: status for name, status, _ in cli.run_steps(steps)}
    assert rows["score"] == "ok"
    assert rows["enrich --retry-failed"] == "ok"
    for name in (
        "sync portal",
        "outreach enroll",
        "outreach classify",
        "outreach pull-stats",
        "outreach learn",
    ):
        assert rows[name] == "skipped", name
    assert all(status != "failed" for status in rows.values())


def test_daily_exit_code_is_one_when_a_step_fails(monkeypatch, tmp_path):
    _no_keys(monkeypatch, tmp_path)
    monkeypatch.setattr(cli, "discover", lambda *a, **k: 0)

    def broken_enrich(*a, **k):
        raise RuntimeError("crawler down")

    monkeypatch.setattr(cli, "enrich", broken_enrich)
    result = runner.invoke(cli.app, ["daily", "--no-enroll", "--no-classify"])
    assert result.exit_code == 1
    assert "failed" in result.output
    assert "score" in result.output  # later steps still ran


def test_ingest_command_adds_leads(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "db_path", tmp_path / "leads.db")
    path = tmp_path / "meta.csv"
    path.write_text(
        "full_name,email,company_name,website\nJane Doe,jane@acmeclinic.com,Acme Clinic,acmeclinic.com\n"
    )
    result = runner.invoke(cli.app, ["ingest", str(path), "--source", "meta_ads"])
    assert result.exit_code == 0, result.output
    assert LeadStore(tmp_path / "leads.db").get("acmeclinic.com") is not None


def test_ingest_rejects_unknown_source(tmp_path):
    path = tmp_path / "x.csv"
    path.write_text("email\n")
    result = runner.invoke(cli.app, ["ingest", str(path), "--source", "billboard"])
    assert result.exit_code != 0


def test_campaign_ids_fall_back_to_portal_settings(tmp_path):
    from moh_leadgen.outreach.enroll import load_state

    class Sink:
        def get_app_setting(self, key):
            assert key == "outreach"
            return {"campaigns": {"telehealth": 101, "generic": "202"}}

    state = load_state(tmp_path / "missing.json", Sink())
    assert state["campaigns"] == {"telehealth": {"id": 101}, "generic": {"id": 202}}
    assert load_state(tmp_path / "missing.json") == {"campaigns": {}}
