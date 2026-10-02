import sys

import pytest

from cartclaw import browser


@pytest.mark.skipif(sys.platform != "win32", reason="Windows install paths")
def test_candidates_survive_trimmed_mcp_environment(monkeypatch):
    # MCP stdio clients start servers without PROGRAMFILES; Brave was not found live on 2026-10-02.
    for key in (
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "LOCALAPPDATA",
        "CARTCLAW_BROWSER",
    ):
        monkeypatch.delenv(key, raising=False)
    assert (
        r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"
        in browser._candidates()
    )


def test_browser_override(monkeypatch):
    monkeypatch.setenv("CARTCLAW_BROWSER", "/opt/x/chrome")
    assert browser._candidates() == ["/opt/x/chrome"]
