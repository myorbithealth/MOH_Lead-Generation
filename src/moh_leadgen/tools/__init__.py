"""Each tool = a named bundle of discovery sources + defaults, run through the shared pipeline."""

from .base import Tool
from .competitor_platform import CompetitorPlatformTool
from .legitscript import LegitScriptTool
from .local_business import LocalBusinessTool

TOOLS: dict[str, type[Tool]] = {
    LegitScriptTool.name: LegitScriptTool,
    CompetitorPlatformTool.name: CompetitorPlatformTool,
    LocalBusinessTool.name: LocalBusinessTool,
}

__all__ = ["TOOLS", "CompetitorPlatformTool", "LegitScriptTool", "LocalBusinessTool", "Tool"]
