"""高德 MCP 有限时传输的独立测试。"""

import asyncio
import sys
import unittest
from pathlib import Path


APP_DIR = Path(__file__).resolve().parents[1] / "backed" / "app"
sys.path.insert(0, str(APP_DIR))
from amap_mcp_tool import BoundedMCPTool  # noqa: E402


class BoundedMCPToolTests(unittest.TestCase):
    def test_sync_bridge_converts_asyncio_timeout(self):
        tool = object.__new__(BoundedMCPTool)
        tool.timeout = 0.01

        with self.assertRaises(TimeoutError):
            tool._run_sync(lambda: asyncio.sleep(0.1))


if __name__ == "__main__":
    unittest.main()
