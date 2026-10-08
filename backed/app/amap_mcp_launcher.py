"""启动高德 MCP；协议 stdout 保持纯净，第三方调试输出写入日志。"""

import logging

from amap_mcp_server import server


def _server_debug_print(*args, **kwargs):
    logger = logging.getLogger("amap_mcp_server.debug")
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("%s", kwargs.get("sep", " ").join(map(str, args)))


def main():
    # 当前上游公交工具包含 print(data)，直接输出会污染 stdio JSON-RPC。
    # 只替换该模块的 print，不重定向 MCP 自身的标准输出。
    server.print = _server_debug_print
    server.mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
