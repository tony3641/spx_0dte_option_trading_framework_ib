"""The MCP server must not import DLL-backed modules inside a tool call.

On Windows, FastMCP's stdio transport keeps a worker thread blocked in a
synchronous read of the stdin pipe while a tool runs. Loading scipy's BLAS
extension at that moment runs its runtime initialisation, which queries the
std handles and waits behind that pending read; the read only returns when the
client sends another message, and the client is waiting for the tool's reply.
``analyze_strategy_compliance`` imported the tagging module (and with it
``scipy.optimize``) lazily, so its first call hung indefinitely.

The fix imports everything at module load, before ``mcp.run()`` starts the
reader thread. The first test pins that structurally on every platform; the
second drives a real stdio session, which reproduces the hang on Windows.
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_server_import_preloads_the_scipy_backed_tagging_module():
    code = (
        "import sys;"
        "import spx_trade_desk.mcp.server;"
        "missing = [m for m in ('scipy.optimize', 'spx_trade_desk.tradelog.analysis.tagging')"
        " if m not in sys.modules];"
        "assert not missing, f'imported lazily: {missing}'"
    )
    completed = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT,
                               capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr


def test_compliance_tool_answers_over_a_real_stdio_session(tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    # A missing config makes the tool return its "no strategies" error right
    # after the imports that used to deadlock, so no statement is needed.
    missing_config = tmp_path / "no_strategies.json"

    async def call() -> dict:
        params = StdioServerParameters(
            command=sys.executable, args=["-m", "spx_trade_desk.mcp.server"],
            cwd=str(REPO_ROOT))
        with open(tmp_path / "server_stderr.txt", "w") as errlog:
            async with stdio_client(params, errlog=errlog) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    return await asyncio.wait_for(
                        session.call_tool("analyze_strategy_compliance",
                                          {"strategies_path": str(missing_config)}),
                        timeout=60)

    result = asyncio.run(call())

    assert not result.isError
    assert "no strategies configured" in result.content[0].text
