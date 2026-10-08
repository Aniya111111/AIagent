"""旅行规划错误处理及健康检查的离线回归测试。"""

import copy
import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from test_amap_service import load_service


class FakeAgent:
    def __init__(self, name, llm, system_prompt):
        self.name = name
        self.llm = llm
        self.tools = []
        self.response = "工具查询结果"
        self.calls = []

    def add_tool(self, tool):
        self.tools.extend(tool._available_tools)

    def list_tools(self):
        return self.tools

    def run(self, query):
        self.calls.append(query)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def load_planner():
    amap, schemas, _ = load_service()
    app_dir = Path(__file__).resolve().parents[1] / "backed" / "app"

    def load(name, filename):
        spec = importlib.util.spec_from_file_location(name, app_dir / filename)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    hello_agents = types.ModuleType("hello_agents")
    hello_agents.SimpleAgent = FakeAgent
    llm = types.ModuleType("llm_service")
    llm.get_llm = lambda: object()
    with patch.dict(sys.modules, {
        "hello_agents": hello_agents, "llm_service": llm,
        "amap_service": amap, "schemas": schemas,
    }):
        planner = load("_trip_test_planner", "trip_planner_agent.py")
        with patch.dict(sys.modules, {"trip_planner_agent": planner}):
            routes = load("_trip_test_routes", "trip.py")
    return planner, amap, schemas, routes


class TripPlannerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module, cls.amap, cls.schemas, cls.routes = load_planner()

    def setUp(self):
        self.amap._amap_mcp_tool = None
        self.module._multi_agent_planner = None
        command_patch = patch.object(self.amap, "_resolve_uvx", return_value="uvx")
        command_patch.start()
        self.addCleanup(command_patch.stop)
        self.planner = self.module.MutiAgentTripPlanner()
        self.request = self.schemas.TripRequest(
            city="北京", start_date="2026-10-08", end_date="2026-10-08",
            travel_days=1, transportation="公共交通", accommodation="经济型酒店",
        )
        self.plan_data = {
            "city": "北京", "start_date": "2026-10-08", "end_date": "2026-10-08",
            "days": [{
                "date": "2026-10-08", "day_index": 0, "description": "参观故宫",
                "transportation": "公共交通", "accommodation": "经济型酒店",
                "attractions": [{
                    "name": "故宫博物院", "address": "景山前街4号",
                    "location": {"longitude": 116.397, "latitude": 39.918},
                    "visit_duration": 120, "description": "历史文化景点",
                }],
            }],
            "weather_info": [], "overall_suggestions": "提前预约",
        }
        self.planner.planner_agent.response = json.dumps(self.plan_data)

    def client(self):
        app = FastAPI()
        app.include_router(self.routes.router, prefix="/api")
        return TestClient(app)

    def test_planner_uses_shared_mcp_instance(self):
        self.assertIs(self.planner.amap_tool, self.amap.get_amap_mcp_tool())

    def test_initialization_failure_not_cached_and_retry_succeeds(self):
        error = self.amap.AmapServiceUnavailableError("MCP unavailable")
        with patch.object(self.module, "get_amap_mcp_tool", side_effect=error):
            with self.assertRaises(self.module.TripPlannerUnavailableError):
                self.module.get_trip_planner_agent()
        self.assertIsNone(self.module._multi_agent_planner)
        planner = self.module.get_trip_planner_agent()
        self.assertIs(self.module.get_trip_planner_agent(), planner)

    def test_llm_initialization_error_does_not_expose_credentials(self):
        with patch.object(self.module, "get_llm", side_effect=RuntimeError("secret-value")):
            with self.assertRaises(self.module.TripPlannerUnavailableError) as raised:
                self.module.MutiAgentTripPlanner()
        self.assertNotIn("secret-value", str(raised.exception))

    def test_successful_plan_preserves_generated_data(self):
        plan = self.planner.plan_trip(self.request)
        self.assertEqual(plan.days[0].attractions[0].name, "故宫博物院")
        self.assertEqual(plan.days[0].attractions[0].location.longitude, 116.397)

    def test_agent_failure_propagates_instead_of_creating_fallback(self):
        self.planner.attraction_agent.response = RuntimeError("upstream failed")
        with self.assertRaises(self.module.TripPlanningError):
            self.planner.plan_trip(self.request)
        self.assertEqual(self.planner.weather_agent.calls, [])
        self.assertEqual(self.planner.planner_agent.calls, [])
        self.assertFalse(hasattr(self.planner, "_create_fallback_plan"))

    def test_empty_or_explicit_error_responses_fail(self):
        for response in (None, "", "  ", "错误：查询失败", "Error: service unavailable"):
            with self.subTest(response=response):
                self.planner.attraction_agent.response = response
                with self.assertRaises(self.module.TripPlanningError):
                    self.planner.plan_trip(self.request)

    def test_malformed_json_and_schema_errors_do_not_return_success(self):
        for response in ("无法生成计划", "{broken}", "{}", "```json\n{}", "{\"days\": []}"):
            with self.subTest(response=response):
                self.planner.planner_agent.response = response
                with self.assertRaises(self.module.TripPlanningError):
                    self.planner.plan_trip(self.request)

    def test_fenced_json_and_plain_json_are_supported(self):
        raw = json.dumps(self.plan_data)
        for response in (raw, f"```json\n{raw}\n```", f"```\n{raw}\n```", f"旅行计划如下:\n{raw}"):
            with self.subTest(response=response[:20]):
                result = self.planner._parse_response(response, self.request)
                self.assertEqual(len(result.days), 1)

    def test_generated_request_mismatch_is_rejected(self):
        for key, value in (("city", "上海"), ("start_date", "2026-10-09"),
                           ("end_date", "2026-10-10"), ("days", [])):
            with self.subTest(key=key):
                data = copy.deepcopy(self.plan_data)
                data[key] = value
                with self.assertRaises(self.module.TripPlanningError):
                    self.planner._parse_response(json.dumps(data), self.request)

    def test_health_lists_real_agents_and_probes_mcp_without_llm_generation(self):
        health = self.planner.get_health()
        self.assertEqual(health["status"], "healthy")
        self.assertEqual(health["tools_count"], 7)
        self.assertEqual(set(health["agents"]), {"attraction", "weather", "hotel", "planner"})
        self.assertEqual(health["agents"]["planner"]["tools_count"], 0)
        self.assertEqual(self.planner.amap_tool.calls[-1], {"action": "list_tools"})
        for role in health["agents"]:
            self.assertEqual(getattr(self.planner, f"{role}_agent").calls, [])

    def test_health_rejects_empty_mcp_tools_and_connection_failure(self):
        self.planner.amap_tool.health_result = "MCP 操作失败: disconnected"
        with self.assertRaises(self.module.TripPlannerUnavailableError):
            self.planner.get_health()
        self.planner.amap_tool._available_tools = []
        with self.assertRaises(self.module.TripPlannerUnavailableError):
            self.planner.get_health()

    def test_health_rejects_missing_or_unregistered_agents_and_llm(self):
        for role in ("attraction", "weather", "hotel", "planner"):
            with self.subTest(role=role):
                attr = f"{role}_agent"
                with patch.object(self.planner, attr, None):
                    with self.assertRaises(self.module.TripPlannerUnavailableError):
                        self.planner.get_health()
        self.planner.weather_agent.tools = []
        with self.assertRaises(self.module.TripPlannerUnavailableError):
            self.planner.get_health()
        self.planner.weather_agent.add_tool(self.planner.amap_tool)
        self.planner.llm = None
        with self.assertRaises(self.module.TripPlannerUnavailableError):
            self.planner.get_health()

    def test_http_successful_plan_and_health(self):
        with patch.object(self.routes, "get_trip_planner_agent", return_value=self.planner):
            with self.client() as client:
                response = client.post("/api/trip/plan", json=self.request.model_dump())
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.json()["success"])
                self.assertEqual(client.get("/api/trip/health").status_code, 200)

    def test_http_planning_failure_is_502_without_fake_data(self):
        self.planner.planner_agent.response = "not JSON"
        with patch.object(self.routes, "get_trip_planner_agent", return_value=self.planner):
            with self.client() as client:
                response = client.post("/api/trip/plan", json=self.request.model_dump())
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("data", response.json())
        self.assertNotIn("success", response.json())

    def test_http_unavailable_planner_and_empty_tools_are_503(self):
        error = self.module.TripPlannerUnavailableError("依赖未就绪")
        with patch.object(self.routes, "get_trip_planner_agent", side_effect=error):
            with self.client() as client:
                self.assertEqual(client.get("/api/trip/health").status_code, 503)
                self.assertEqual(client.post("/api/trip/plan", json=self.request.model_dump()).status_code, 503)
        self.planner.amap_tool._available_tools = []
        with patch.object(self.routes, "get_trip_planner_agent", return_value=self.planner):
            with self.client() as client:
                self.assertEqual(client.get("/api/trip/health").status_code, 503)


if __name__ == "__main__":
    unittest.main()
