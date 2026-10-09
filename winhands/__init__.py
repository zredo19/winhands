"""winhands: computer use MCP server for Windows."""
from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("winhands")
except PackageNotFoundError:  # running from a source tree that is not installed
    __version__ = "unknown"
