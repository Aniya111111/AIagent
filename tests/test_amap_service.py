"""AmapService 的离线单元测试，不访问真实高德服务。"""

import importlib
import json
import sys
import types
import unittest
from dataclasses import dataclass
from typing import Optional


@dataclass
class FakeLocation:
    longitude: float
    latitude: float


@dataclass
class FakePOIInfo:
    id: str
    name: str
    type: str
    address: str
    location: FakeLocation
    tel: Optional[str] = None


class FakeSettings:
    amap_api_key = "test-key"


class FakeMCPTool:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self._available_tools = [{"name": "maps_text_search"}]
        self.result = "[]"
        self.calls = []
        self.instances.append(self)

    def run(self, parameters):
        self.calls.append(parameters)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def load_service():
    """注入最小依赖后加载服务模块，适配未安装项目运行时依赖的环境。"""

    hello_agents = types.ModuleType("hello_agents")
    tools = types.ModuleType("hello_agents.tools")
    tools.MCPTool = FakeMCPTool
    hello_agents.tools = tools
    sys.modules["hello_agents"] = hello_agents
    sys.modules["hello_agents.tools"] = tools

    config = types.ModuleType("backed.app.config")
    config.get_setting = lambda: FakeSettings()
    schemas = types.ModuleType("backed.app.schemas")
    schemas.Location = FakeLocation
    schemas.POIInfo = FakePOIInfo
    schemas.WeatherInfo = object
    sys.modules["backed.app.config"] = config
    sys.modules["backed.app.schemas"] = schemas

    sys.modules.pop("backed.app.amap_service", None)
    return importlib.import_module("backed.app.amap_service")


class AmapServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.service_module = load_service()

    def setUp(self):
        FakeMCPTool.instances.clear()
        self.service_module._amap_mcp_tool = None

    def test_tool_is_cached_and_configured_once(self):
        first = self.service_module.get_amap_mcp_tool()
        second = self.service_module.get_amap_mcp_tool()

        self.assertIs(first, second)
        self.assertEqual(len(FakeMCPTool.instances), 1)
        self.assertEqual(first.kwargs["env"], {"AMAP_MAPS_API_KEY": "test-key"})

    def test_tool_requires_api_key(self):
        original_key = FakeSettings.amap_api_key
        FakeSettings.amap_api_key = ""
        try:
            with self.assertRaises(ValueError):
                self.service_module.get_amap_mcp_tool()
            self.assertEqual(FakeMCPTool.instances, [])
        finally:
            FakeSettings.amap_api_key = original_key

    def test_search_poi_parses_wrapped_json_response(self):
        tool = FakeMCPTool()
        tool.result = "工具 'maps_text_search' 执行结果:\n" + json.dumps(
            {
                "pois": [
                    {
                        "id": "B001",
                        "name": "故宫",
                        "type": "风景名胜",
                        "address": "北京市东城区",
                        "location": "116.397,39.918",
                        "tel": "010-123456",
                    }
                ]
            }
        )
        pois = self.service_module.AmapService(mcp_tool=tool).search_poi(
            "故宫", "北京", citylimit=False
        )

        self.assertEqual(len(pois), 1)
        self.assertEqual(pois[0].name, "故宫")
        self.assertEqual(pois[0].location.longitude, 116.397)
        self.assertEqual(tool.calls[0]["arguments"]["citylimit"], "false")

    def test_search_poi_supports_content_list_and_skips_invalid_rows(self):
        tool = FakeMCPTool()
        tool.result = [
            {
                "type": "text",
                "text": json.dumps(
                    {
                        "data": [
                            {"name": "无坐标"},
                            {"id": "B002", "name": "天坛", "location": {"lng": 116.41, "lat": 39.88}},
                        ]
                    }
                ),
            }
        ]

        pois = self.service_module.AmapService(mcp_tool=tool).search_poi("天坛", "北京")

        self.assertEqual([poi.name for poi in pois], ["天坛"])
        self.assertEqual(pois[0].location.latitude, 39.88)

    def test_search_poi_accepts_python_literal_response(self):
        tool = FakeMCPTool()
        tool.result = "工具执行结果: {'pois': [{'id': 'B003', 'name': '颐和园', 'location': '116.275,39.999'}]}"

        pois = self.service_module.AmapService(mcp_tool=tool).search_poi("颐和园", "北京")

        self.assertEqual(len(pois), 1)
        self.assertEqual(pois[0].id, "B003")

    def test_search_poi_returns_empty_for_invalid_input_or_mcp_error(self):
        tool = FakeMCPTool()
        service = self.service_module.AmapService(mcp_tool=tool)
        self.assertEqual(service.search_poi("", "北京"), [])
        self.assertEqual(tool.calls, [])

        tool.result = RuntimeError("server unavailable")
        self.assertEqual(service.search_poi("景点", "北京"), [])


if __name__ == "__main__":
    unittest.main()
