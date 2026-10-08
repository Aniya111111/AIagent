"""带请求超时的 MCPTool，保留 HelloAgents 的工具自动展开接口。"""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor # 创建多线程

from fastmcp import Client
from fastmcp.client.transports import StdioTransport
from hello_agents.tools import MCPTool


class BoundedMCPTool(MCPTool):
    def __init__(self, *, timeout=30.0, **kwargs):
        self.timeout = float(timeout)
        if self.timeout <= 0:
            raise ValueError("MCP timeout 必须大于 0")
        super().__init__(**kwargs)

    def _make_client(self):
        command = self.server_command
        transport = StdioTransport(
            command=command[0], args=command[1:] + self.server_args, env=self.env,
        )
        # 内层超时略长，保证本类的统一计时器负责转换为稳定的 TimeoutError。
        return Client(transport, timeout=self.timeout * 2, init_timeout=self.timeout * 2)

    def _run_sync(self, operation):
        async def bounded():
            # 覆盖连接、请求及连接退出，超时取消后由传输层关闭子进程。
            return await asyncio.wait_for(operation(), timeout=self.timeout)

        try:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                return asyncio.run(bounded())
            # Agent 也可能在 FastAPI 的事件循环中同步调用展开后的工具。
            with ThreadPoolExecutor(max_workers=1) as executor:
                return executor.submit(lambda: asyncio.run(bounded())).result()
        except asyncio.TimeoutError as exc:
            raise TimeoutError(f"MCP 请求超过 {self.timeout:g} 秒") from exc

    def _discover_tools(self):
        async def discover():
            async with self._make_client() as client:
                tools = await client.list_tools()
                return [{"name": tool.name, "description": tool.description or "",
                         "input_schema": tool.inputSchema} for tool in tools]

        # 不吞掉发现阶段的异常，调用方不会缓存初始化失败的实例。
        self._available_tools = self._run_sync(discover)

    def run(self, parameters):
        action = parameters.get("action", "list_tools")
        if action not in ("list_tools", "call_tool"):
            raise ValueError(f"高德 MCP 不支持操作: {action}")

        async def request():
            async with self._make_client() as client:
                if action == "list_tools":
                    tools = await client.list_tools()
                    return "可用工具:\n" + "\n".join(
                        f"- {tool.name}: {tool.description or ''}" for tool in tools
                    )
                result = await client.call_tool_mcp(
                    parameters["tool_name"], parameters.get("arguments", {}),
                )
                return json.dumps(result.model_dump(mode="json", by_alias=True),
                                  ensure_ascii=False)

        return self._run_sync(request)
