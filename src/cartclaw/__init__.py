import sys


def main() -> None:
    """`cartclaw` runs the MCP server on stdio; `cartclaw login` opens the Amazon window to sign in."""
    if sys.argv[1:2] == ["login"]:
        from .browser import login

        login()
        return
    from .server import mcp

    mcp.run()
