"""AmapService 的离线单元测试，不访问真实高德服务。"""

import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient


class FakeSettings:
    amap_api_key = "test-key"
    amap_mcp_timeout = 12.5


class FakeMCPTool:
    instances = []

    def __init__(self, **kwargs):
        self.timeout = kwargs.pop("timeout", None)
        self.kwargs = kwargs
        self._available_tools = [{"name": name} for name in (
            "maps_text_search", "maps_weather", "maps_geo", "maps_search_detail",
            "maps_direction_walking_by_address", "maps_direction_driving_by_address",
            "maps_direction_transit_integrated_by_address",
        )]
        self.result = "[]"
        self.health_result = None
        self.tool_results = {}
        self.calls = []
        self.instances.append(self)

    def run(self, parameters):
        self.calls.append(parameters)
        if isinstance(self.result, Exception):
            raise self.result
        if parameters.get("action") == "list_tools":
            if self.health_result is not None:
                return self.health_result
            return "找到可用工具:\n" + "\n".join(
                f"- {tool['name']}: test tool" for tool in self._available_tools
            )
        if parameters.get("tool_name") in self.tool_results:
            return self.tool_results[parameters["tool_name"]]
        return self.result


def load_service():
    """只替换外部服务与配置，使用真实响应模型且不读取 .env。"""
    app_dir = Path(__file__).resolve().parents[1] / "backed" / "app"

    def load(name, filename):
        spec = importlib.util.spec_from_file_location(name, app_dir / filename)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    schemas = load("_trip_test_schemas", "schemas.py")
    hello_agents = types.ModuleType("hello_agents")
    tools = types.ModuleType("hello_agents.tools")
    tools.MCPTool = FakeMCPTool
    hello_agents.tools = tools
    transport = types.ModuleType("amap_mcp_tool")
    transport.BoundedMCPTool = FakeMCPTool
    config = types.ModuleType("config")
    config.get_setting = lambda: FakeSettings()
    with patch.dict(sys.modules, {
        "hello_agents": hello_agents,
        "hello_agents.tools": tools,
        "amap_mcp_tool": transport,
        "config": config,
        "schemas": schemas,
    }):
        service = load("_trip_test_amap", "amap_service.py")
        with patch.dict(sys.modules, {"amap_service": service}):
            routes = load("_trip_test_map", "map.py")
    return service, schemas, routes


class AmapServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.service_module, cls.schemas, cls.routes = load_service()
        cls.resolve_uvx = staticmethod(cls.service_module._resolve_uvx)

    def setUp(self):
        FakeMCPTool.instances.clear()
        self.service_module._amap_mcp_tool = None
        self.resolve_patch = patch.object(self.service_module, "_resolve_uvx", return_value="uvx")
        self.resolve_patch.start()
        self.addCleanup(self.resolve_patch.stop)

    def service_with(self, result):
        tool = FakeMCPTool()
        tool.result = result
        return self.service_module.AmapService(mcp_tool=tool), tool

    def assert_service_error(self, service, operation, *args):
        with self.assertRaises(self.service_module.AmapServiceError):
            getattr(service, operation)(*args)

    def test_tool_is_cached_and_configured_once(self):
        first = self.service_module.get_amap_mcp_tool()
        second = self.service_module.get_amap_mcp_tool()

        self.assertIs(first, second)
        self.assertEqual(len(FakeMCPTool.instances), 1)
        self.assertEqual(first.kwargs["env"], {
            "AMAP_MAPS_API_KEY": "test-key", "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
        })
        self.assertEqual(first.kwargs["server_command"], [
            "uvx", "--python", self.service_module.sys.executable,
            "--no-python-downloads", "--from", "amap-mcp-server", "python", "-u",
            str(Path(self.service_module.__file__).with_name("amap_mcp_launcher.py")),
        ])
        self.assertEqual(first.timeout, 12.5)

    def test_tool_requires_api_key(self):
        original_key = FakeSettings.amap_api_key
        FakeSettings.amap_api_key = ""
        try:
            with self.assertRaises(self.service_module.AmapServiceUnavailableError):
                self.service_module.get_amap_mcp_tool()
            self.assertEqual(FakeMCPTool.instances, [])
        finally:
            FakeSettings.amap_api_key = original_key

    def test_uvx_resolution_from_path_and_interpreter_environment(self):
        module = self.service_module
        with patch.object(module.shutil, "which", return_value="C:/tools/uvx.exe"):
            self.assertEqual(self.resolve_uvx(), "C:/tools/uvx.exe")
        candidate = Path(module.sys.executable).resolve().parent / "Scripts" / "uvx.exe"
        with patch.object(module.shutil, "which", return_value=None):
            with patch.object(module.Path, "is_file", autospec=True,
                              side_effect=lambda path: path == candidate):
                self.assertEqual(self.resolve_uvx(), str(candidate))
            with patch.object(module.Path, "is_file", return_value=False):
                with self.assertRaises(module.AmapServiceUnavailableError):
                    self.resolve_uvx()

    def test_missing_uvx_prevents_mcp_initialization(self):
        error = self.service_module.AmapServiceUnavailableError("uvx unavailable")
        self.resolve_patch.stop()
        with patch.object(self.service_module, "_resolve_uvx", side_effect=error):
            with self.assertRaises(type(error)):
                self.service_module.get_amap_mcp_tool()
        self.assertEqual(FakeMCPTool.instances, [])
        self.assertIsNone(self.service_module._amap_mcp_tool)

    def test_empty_tool_discovery_is_not_cached_and_can_retry(self):
        invalid = FakeMCPTool()
        invalid._available_tools = []
        valid = FakeMCPTool()
        with patch.object(self.service_module, "MCPTool", side_effect=[invalid, valid]):
            with self.assertRaises(self.service_module.AmapServiceUnavailableError):
                self.service_module.get_amap_mcp_tool()
            self.assertIsNone(self.service_module._amap_mcp_tool)
            self.assertIs(self.service_module.get_amap_mcp_tool(), valid)

    def test_missing_required_tools_is_not_cached(self):
        invalid = FakeMCPTool()
        invalid._available_tools = [{"name": "maps_text_search"}]
        with patch.object(self.service_module, "MCPTool", return_value=invalid):
            with self.assertRaises(self.service_module.AmapServiceUnavailableError):
                self.service_module.get_amap_mcp_tool()
        self.assertIsNone(self.service_module._amap_mcp_tool)

    def test_constructor_failure_is_not_cached_or_exposed(self):
        with patch.object(self.service_module, "MCPTool", side_effect=RuntimeError("secret-value")):
            with self.assertRaises(self.service_module.AmapServiceUnavailableError) as raised:
                self.service_module.get_amap_mcp_tool()
        self.assertNotIn("secret-value", str(raised.exception))
        self.assertIsNone(self.service_module._amap_mcp_tool)

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

    def test_search_poi_enriches_real_text_search_shape_with_detail(self):
        tool = FakeMCPTool()
        tool.result = {"pois": [{
            "id": "B0FFG6QCDT", "name": "故宫博物院", "address": "景山前街4号",
            "typecode": "110200",
        }]}
        tool.tool_results["maps_search_detail"] = {
            "id": "B0FFG6QCDT", "name": "故宫博物院",
            "location": "116.397026,39.918058", "type": "风景名胜;博物馆",
        }

        pois = self.service_module.AmapService(mcp_tool=tool).search_poi("故宫", "北京")

        self.assertEqual(len(pois), 1)
        self.assertEqual(pois[0].location.longitude, 116.397026)
        self.assertEqual(pois[0].type, "风景名胜;博物馆")
        self.assertEqual([call["tool_name"] for call in tool.calls],
                         ["maps_text_search", "maps_search_detail"])

    def test_search_poi_returns_empty_for_invalid_input_but_raises_on_mcp_error(self):
        tool = FakeMCPTool()
        service = self.service_module.AmapService(mcp_tool=tool)
        self.assertEqual(service.search_poi("", "北京"), [])
        self.assertEqual(tool.calls, [])

        tool.result = RuntimeError("server unavailable")
        self.assert_service_error(service, "search_poi", "景点", "北京")

    def test_serialized_content_wrapper_is_unwrapped(self):
        payload = {"pois": [{"id": "B01", "name": "故宫", "location": "116.397,39.918"}]}
        wrapper = {"content": [{"type": "text", "text": json.dumps(payload)}]}
        for result in (wrapper, json.dumps(wrapper), "工具执行结果:\n" + repr(wrapper)):
            with self.subTest(result_type=type(result).__name__):
                service, _ = self.service_with(result)
                self.assertEqual(service.search_poi("故宫", "北京")[0].id, "B01")

    def test_upstream_failures_and_malformed_payloads_are_not_empty_success(self):
        results = [
            "异步操作失败: unavailable", "{broken", "null", 42, {},
            {"status": "0", "info": "INVALID_USER_KEY"},
            {"isError": True, "content": [{"type": "text", "text": '{"pois":[]}'}]},
            {"content": []}, {"pois": "not-a-list"},
        ]
        for result in results:
            with self.subTest(result=result):
                service, _ = self.service_with(result)
                self.assert_service_error(service, "search_poi", "景点", "北京")

    def test_search_poi_direct_dict_and_invalid_coordinates(self):
        service, _ = self.service_with({"data": {"pois": [
            {"id": "ok", "name": "故宫", "location": {"longitude": 116.397, "latitude": 39.918}},
            {"name": "范围错误", "location": "181,20"},
            {"name": "非数值", "location": "nan,20"},
            {"location": "116,39"},
        ]}})
        self.assertEqual([poi.id for poi in service.search_poi("景点", "北京")], ["ok"])

    def test_search_poi_empty_result(self):
        service, _ = self.service_with({"status": "1", "pois": []})
        self.assertEqual(service.search_poi("景点", "北京"), [])

    def test_weather_forecasts_casts(self):
        service, tool = self.service_with({"forecasts": [{"casts": [
            {"date": "2026-10-08", "dayweather": "晴", "nightweather": "多云",
             "daytemp": "25°C", "nighttemp": "15", "daywind": "南", "daypower": "1-3"},
            {"date": "2026-10-09", "daytemp": "23", "nighttemp": "14"},
        ]}]})
        weather = service.get_weather("北京")
        self.assertEqual(len(weather), 2)
        self.assertEqual(weather[0].day_temp, 25)
        self.assertEqual(weather[0].night_temp, 15)
        self.assertEqual(weather[0].night_weather, "多云")
        self.assertEqual(weather[0].wind_direction, "南")
        self.assertEqual(tool.calls[0]["tool_name"], "maps_weather")

    def test_weather_supports_real_flat_forecasts_shape(self):
        service, _ = self.service_with({"city": "北京市", "forecasts": [
            {"date": "2026-10-08", "dayweather": "晴", "nightweather": "多云",
             "daytemp": "21", "nighttemp": "10", "daywind": "北", "daypower": "1-3"},
        ]})

        weather = service.get_weather("北京")

        self.assertEqual(len(weather), 1)
        self.assertEqual(weather[0].date, "2026-10-08")
        self.assertEqual(weather[0].day_temp, 21)

    def test_weather_realtime_and_empty_results(self):
        service, tool = self.service_with({"data": {"lives": [{
            "reporttime": "2026-10-08 10:00:00", "weather": "晴",
            "temperature": "22", "winddirection": "东", "windpower": "≤3",
        }]}})
        weather = service.get_weather("北京")[0]
        self.assertEqual(weather.date, "2026-10-08")
        self.assertEqual(weather.day_temp, 22)
        self.assertEqual(weather.wind_power, "≤3")
        tool.result = {"forecasts": []}
        self.assertEqual(service.get_weather("北京"), [])
        tool.result = {"unexpected": []}
        self.assert_service_error(service, "get_weather", "北京")

    def test_weather_invalid_fields_are_reported_as_upstream_errors(self):
        for row in (None, {"weather": "晴"}, {"date": "2026-10-08", "daytemp": []}):
            with self.subTest(row=row):
                service, _ = self.service_with({"casts": [row]})
                self.assert_service_error(service, "get_weather", "北京")
                with patch.object(self.routes, "get_amap_service", return_value=service):
                    with self.client_for(service) as client:
                        response = client.get("/api/map/weather", params={"city": "北京"})
                        self.assertEqual(response.status_code, 502)

    def test_routes_return_real_response_models_for_all_modes(self):
        for mode, key, tool_name in (
            ("walking", "paths", "maps_direction_walking_by_address"),
            ("driving", "paths", "maps_direction_driving_by_address"),
            ("transit", "transits", "maps_direction_transit_integrated_by_address"),
        ):
            with self.subTest(mode=mode):
                service, tool = self.service_with({"route": {key: [{
                    "distance": "1234.5", "duration": "600", "steps": [
                        {"instruction": "向东走"}, {"instruction": "到达终点"},
                    ],
                }]}})
                route = service.plan_route("起点", "终点", "北京", "北京", mode)
                response = self.schemas.RouteResponse(success=True, data=route)
                self.assertEqual(response.data.distance, 1234.5)
                self.assertEqual(response.data.duration, 600)
                self.assertEqual(response.data.route_type, mode)
                self.assertIn("到达终点", response.data.description)
                call = tool.calls[0]
                self.assertEqual(call["action"], "call_tool")
                self.assertNotIn("aciton", call)
                self.assertEqual(call["tool_name"], tool_name)
                self.assertEqual(call["arguments"]["origin_city"], "北京")

    def test_transit_nested_walking_instructions(self):
        service, _ = self.service_with({"route": {"transits": [{
            "distance": "2000", "duration": "1200", "segments": [{
                "walking": {"steps": [{"instruction": "步行到车站"}]},
            }],
        }]}})
        route = service.plan_route("起点", "终点", route_type="transit")
        self.assertEqual(route.description, "步行到车站")

    def test_transit_supports_real_parent_distance_shape(self):
        service, _ = self.service_with({"route": {
            "origin": "116.392773,39.914836", "destination": "116.397755,39.903182",
            "distance": "1720", "transits": [{
                "duration": "2812", "walking_distance": "1491",
                "segments": [{"walking": {"steps": [{"instruction": "步行到公交站"}]}}],
            }],
        }})

        route = service.plan_route("故宫博物院", "天安门广场", route_type="transit")

        self.assertEqual(route.distance, 1720)
        self.assertEqual(route.duration, 2812)
        self.assertEqual(route.description, "步行到公交站")

    def test_route_rejects_missing_or_invalid_distance_and_duration(self):
        for result in (
            {"route": {"paths": []}},
            {"route": {"paths": [{"distance": "100"}]}},
            {"route": {"paths": [{"distance": "-1", "duration": "10"}]}},
            {"route": {"paths": [{"distance": "100", "duration": "inf"}]}},
        ):
            with self.subTest(result=result):
                service, _ = self.service_with(result)
                self.assert_service_error(service, "plan_route", "起点", "终点")

    def test_unknown_route_type_does_not_call_mcp(self):
        service, tool = self.service_with({})
        with self.assertRaises(ValueError):
            service.plan_route("起点", "终点", route_type="unknown")
        self.assertEqual(tool.calls, [])

    def test_geocode_coordinates_correct_action_and_empty_results(self):
        for value in ("116.397,39.918", {"lng": 116.397, "lat": 39.918}):
            with self.subTest(value=value):
                service, tool = self.service_with({"geocodes": [{"location": value}]})
                location = service.geocode("故宫", "北京")
                self.assertEqual(location.longitude, 116.397)
                call = tool.calls[0]
                self.assertEqual(call["action"], "call_tool")
                self.assertNotIn("aciton", call)
                self.assertEqual(call["tool_name"], "maps_geo")
                self.assertEqual(call["arguments"]["city"], "北京")
                tool.result = {"geocodes": []}
                self.assertIsNone(service.geocode("不存在的地址"))
                tool.result = {"geocodes": [{"location": "broken"}]}
                self.assertIsNone(service.geocode("错误坐标"))

    def test_geocode_mcp_failure_raises(self):
        service, _ = self.service_with(RuntimeError("unavailable"))
        self.assert_service_error(service, "geocode", "故宫")

    def test_geocode_supports_real_return_field(self):
        service, _ = self.service_with({"return": [{
            "city": "北京市", "location": "116.397,39.918", "level": "兴趣点",
        }]})
        location = service.geocode("故宫博物院", "北京")
        self.assertEqual((location.longitude, location.latitude), (116.397, 39.918))

    def client_for(self, service):
        app = FastAPI()
        app.include_router(self.routes.router, prefix="/api")
        return TestClient(app)

    def test_http_routes_report_upstream_errors_as_502(self):
        service, _ = self.service_with(RuntimeError("unavailable"))
        with patch.object(self.routes, "get_amap_service", return_value=service):
            with self.client_for(service) as client:
                responses = [
                    client.get("/api/map/poi", params={"keywords": "景点", "city": "北京"}),
                    client.get("/api/map/weather", params={"city": "北京"}),
                    client.post("/api/map/route", json={"origin_address": "起点", "destination_address": "终点"}),
                ]
                self.assertEqual([response.status_code for response in responses], [502, 502, 502])

    def test_http_routes_report_unavailable_dependencies_as_503(self):
        error = self.service_module.AmapServiceUnavailableError("uvx unavailable")
        with patch.object(self.routes, "get_amap_service", side_effect=error):
            with self.client_for(None) as client:
                responses = [
                    client.get("/api/map/poi", params={"keywords": "景点", "city": "北京"}),
                    client.get("/api/map/weather", params={"city": "北京"}),
                    client.post("/api/map/route", json={"origin_address": "起点", "destination_address": "终点"}),
                ]
                self.assertEqual([response.status_code for response in responses], [503, 503, 503])

    def test_http_routes_report_mcp_timeout_as_504(self):
        service, _ = self.service_with(TimeoutError("slow MCP"))
        with patch.object(self.routes, "get_amap_service", return_value=service):
            with self.client_for(service) as client:
                responses = [
                    client.get("/api/map/poi", params={"keywords": "景点", "city": "北京"}),
                    client.get("/api/map/weather", params={"city": "北京"}),
                    client.post("/api/map/route", json={"origin_address": "起点", "destination_address": "终点"}),
                ]
                self.assertEqual([response.status_code for response in responses], [504, 504, 504])

    def test_map_health_probes_connection_and_rejects_empty_tools(self):
        service, tool = self.service_with({})
        with patch.object(self.routes, "get_amap_service", return_value=service):
            with self.client_for(service) as client:
                response = client.get("/api/map/health")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["mcp_tools_count"], 7)
                self.assertEqual(tool.calls[-1], {"action": "list_tools"})
                tool.health_result = "MCP 操作失败: connection closed"
                self.assertEqual(client.get("/api/map/health").status_code, 503)
                tool.health_result = "找到一个工具:\n- maps_text_search: test"
                self.assertEqual(client.get("/api/map/health").status_code, 503)
                tool._available_tools = []
                self.assertEqual(client.get("/api/map/health").status_code, 503)

    def test_http_empty_results_have_clear_messages(self):
        service, tool = self.service_with({"pois": []})
        with patch.object(self.routes, "get_amap_service", return_value=service):
            with self.client_for(service) as client:
                response = client.get("/api/map/poi", params={"keywords": "景点", "city": "北京"})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["data"], [])
                self.assertIn("未找到", response.json()["message"])
                tool.result = {"forecasts": []}
                response = client.get("/api/map/weather", params={"city": "北京"})
                self.assertEqual(response.status_code, 200)
                self.assertIn("未找到", response.json()["message"])

    def test_http_route_success_missing_route_and_invalid_type(self):
        service, tool = self.service_with({"route": {"paths": [{"distance": "100", "duration": "60"}]}})
        data = {"origin_address": "起点", "destination_address": "终点"}
        with patch.object(self.routes, "get_amap_service", return_value=service):
            with self.client_for(service) as client:
                response = client.post("/api/map/route", json=data)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["data"]["duration"], 60)
                tool.result = {"route": {"paths": []}}
                self.assertEqual(client.post("/api/map/route", json=data).status_code, 502)
                self.assertEqual(client.post("/api/map/route", json={**data, "route_type": "unknown"}).status_code, 422)


if __name__ == "__main__":
    unittest.main()
