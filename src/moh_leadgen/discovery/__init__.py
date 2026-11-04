"""Discovery sources: anything that yields candidate domains."""

from .base import DiscoverySource
from .crtsh import CrtShDiscovery
from .free_index import CommonCrawlDiscovery, WaybackDiscovery
from .overpass import OverpassDiscovery
from .search import SearchDiscovery, build_search_provider
from .seedfile import SeedFileDiscovery
from .semrush import SemrushAdvertisersDiscovery, SemrushBacklinksDiscovery
from .sourcesearch import NerdyDataDiscovery, PublicWWWDiscovery

__all__ = [
    "CommonCrawlDiscovery",
    "CrtShDiscovery",
    "DiscoverySource",
    "NerdyDataDiscovery",
    "OverpassDiscovery",
    "PublicWWWDiscovery",
    "SearchDiscovery",
    "SeedFileDiscovery",
    "SemrushAdvertisersDiscovery",
    "SemrushBacklinksDiscovery",
    "WaybackDiscovery",
    "build_search_provider",
]
