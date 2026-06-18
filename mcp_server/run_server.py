"""Run the CorridorKey MCP server (use this, not server.py directly)."""
import sys
from pathlib import Path

# put repo root on path so mcp_server package resolves
sys.path.insert(0, str(Path(__file__).parent.parent))

from mcp_server.server import mcp

if __name__ == "__main__":
    mcp.run()
