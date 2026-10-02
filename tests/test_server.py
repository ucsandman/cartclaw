from cartclaw.server import mcp


async def test_tools_registered():
    names = {t.name for t in await mcp.list_tools()}
    assert names == {
        "orders",
        "returnable_items",
        "product",
        "cart",
        "cart_add",
        "cart_remove",
        "checkout_request",
        "checkout_status",
    }


async def test_no_tool_can_place_an_order():
    for t in await mcp.list_tools():
        assert not any(
            w in t.name for w in ("place", "buy", "purchase", "confirm", "approve")
        )


async def test_checkout_status_unknown_id_is_an_error():
    import pytest
    from mcp.server.mcpserver.exceptions import ToolError

    with pytest.raises(ToolError):
        await mcp.call_tool("checkout_status", {"approval_id": "nope"})
