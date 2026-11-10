"""Exercise the real stdio MCP transport / 验证真实 stdio MCP 传输."""

import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from tests.unit.test_project_workflow import make_elf

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_client_can_inspect_project_over_stdio(tmp_path):
    (tmp_path / "demo.ioc").write_text("Mcu.Name=STM32F407VGTx\n")
    make_elf(tmp_path / "demo.elf")
    parameters = StdioServerParameters(command=sys.executable, args=["-m", "jlink_mcp"])
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as client:
            initialized = await client.initialize()
            assert initialized.serverInfo.name == "jlink-insight-mcp"
            tools = {tool.name for tool in (await client.list_tools()).tools}
            assert {"inspect_project", "deploy_project", "get_runtime_report", "start_firmware_watch"} <= tools
            result = await client.call_tool("inspect_project", {"project_path": str(tmp_path)})
            assert not result.isError
            assert result.structuredContent["data"]["chip_name"] == "STM32F407VG"
            status = await client.call_tool("get_runtime_report", {})
            assert status.structuredContent["data"]["state"] == "idle"
