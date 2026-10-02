"""Read-only live check over the real MCP stdio protocol: list tools, read orders, returnable
items, one product and the cart. Places nothing and changes nothing.

usage: uv run python scripts/live_smoke.py [ASIN]
"""

import asyncio
import json
import sys
from pathlib import Path

from mcp.client import Client
from mcp.client.stdio import StdioServerParameters

ROOT = Path(__file__).resolve().parent.parent
EXE = (
    ROOT
    / ".venv"
    / ("Scripts/cartclaw.exe" if sys.platform == "win32" else "bin/cartclaw")
)


async def main(asin: str) -> None:
    async with Client(StdioServerParameters(command=str(EXE))) as client:
        tools = [t.name for t in (await client.list_tools()).tools]
        print("tools:", ", ".join(tools))
        for name, args in [
            ("orders", {"period": "year-2026", "max_pages": 1}),
            ("returnable_items", {}),
            ("product", {"asin": asin}),
            ("cart", {}),
        ]:
            r = await client.call_tool(name, args)
            data = r.structured_content or {}
            if r.is_error:
                print(f"{name}: ERROR {r.content[0].text}")
            elif name == "orders":
                o = data["orders"]
                print(
                    f"orders: {len(o)} orders; first {o[0]['placed']} ${o[0]['total']} "
                    f"{len(o[0]['items'])} items, return window {o[0]['items'][0]['return_window']}"
                )
            else:
                print(f"{name}:", json.dumps(data)[:300])


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "B07V3946XL"))
