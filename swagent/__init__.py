"""An MCP server that lets an agent drive SOLIDWORKS 2022 over COM.

Public entry points:

    python -m swagent                 run the server on stdio
    python -m swagent --list-tools    print the tool names
"""
from .session import Session, SessionWithExtras
from .tools import TOOLS

__all__ = ["Session", "SessionWithExtras", "TOOLS"]
__version__ = "1.0.0"
