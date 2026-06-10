# services/__init__.py
"""
Service layer — plug-in capabilities for the chat core.

Each service:
- Does one thing well
- Exposes a clean async interface
- Can run in-process or as a standalone HTTP service
"""

from .search import SearchService, SearchResult, SearchResponse
from .docs import DocsService, DocChunk, IndexResult
from .memory import MemoryService, Memory, MemorySearchResult
from .shell import ShellService, ShellResult

__all__ = [
    # Search
    "SearchService",
    "SearchResult",
    "SearchResponse",
    # Docs
    "DocsService",
    "DocChunk",
    "IndexResult",
    # Memory
    "MemoryService",
    "Memory",
    "MemorySearchResult",
    # Shell
    "ShellService",
    "ShellResult",
]
