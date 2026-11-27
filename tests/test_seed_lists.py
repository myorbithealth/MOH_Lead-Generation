"""Format checks for the public seed list fed to `leadgen discover competitor --seed`."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from moh_leadgen.tools.competitor_platform import PLATFORM_OWNED

ROOT = Path(__file__).resolve().parents[1]
SEED = ROOT / "seeds" / "public_lists_2026-11-27.txt"
OTHER_SEEDS = [
    ROOT / "seeds" / "competitor_platform_seed_domains.txt",
    ROOT / "seeds" / "legitscript_seed_domains.txt",
]
DOMAIN_RE = re.compile(r"^(?!-)[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}$")


def _domains(path: Path) -> list[str]:
    out = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            out.append(line.split(",", 1)[0].strip())
    return out


@pytest.fixture(scope="module")
def domains() -> list[str]:
    return _domains(SEED)


def test_every_line_is_a_bare_lowercase_domain(domains):
    bad = [d for d in domains if not DOMAIN_RE.match(d) or d.startswith("www.")]
    assert not bad
    assert all("/" not in d and ":" not in d for d in domains)


def test_no_duplicates(domains):
    assert len(domains) == len(set(domains))


def test_no_platform_vendor_domains(domains):
    assert not set(domains) & PLATFORM_OWNED


def test_no_overlap_with_existing_seed_files(domains):
    existing = {d for p in OTHER_SEEDS for d in _domains(p)}
    assert not set(domains) & existing


def test_has_sources_and_enough_rows(domains):
    text = SEED.read_text(encoding="utf-8")
    assert "Fetched: " in text and "https://www.fda.gov/" in text
    assert len(domains) >= 100


def test_seedfile_discovery_reads_every_domain(domains):
    from moh_leadgen.discovery.seedfile import SeedFileDiscovery

    found = [c.domain for c in SeedFileDiscovery(SEED, source="competitor_platform").discover()]
    assert set(found) <= set(domains)
    assert len(found) >= len(domains) - 5  # is_junk may drop a few
