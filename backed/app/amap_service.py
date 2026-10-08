"""高德地图 MCP 服务封装。"""

import ast
import json
import math
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from amap_mcp_tool import BoundedMCPTool as MCPTool
from config import get_setting
from schemas import Location, POIInfo, RouteInfo, WeatherInfo


class AmapServiceError(RuntimeError):
    """高德地图服务调用或响应解析失败。"""


class AmapServiceUnavailableError(AmapServiceError):
    """MCP 启动依赖或必要工具不可用。"""


class AmapServiceTimeoutError(AmapServiceError):
    """MCP 请求超过配置的等待时间。"""


AMAP_REQUIRED_TOOLS = frozenset({
    "maps_text_search", "maps_weather", "maps_geo", "maps_search_detail",
    "maps_direction_walking_by_address", "maps_direction_driving_by_address",
    "maps_direction_transit_integrated_by_address",
})


def _resolve_uvx() -> str:
    """兼容激活 Conda 和直接运行环境解释器两种启动方式。"""
    command = shutil.which("uvx")
    if command:
        return command
    python_dir = Path(sys.executable).resolve().parent
    for candidate in (python_dir / "Scripts" / "uvx.exe", python_dir / "uvx.exe",
                      python_dir / "uvx"):
        if candidate.is_file():
            return str(candidate)
    raise AmapServiceUnavailableError(
        "未找到 uvx，请在当前 Python 环境安装 uv 并确认环境已激活"
    )


def check_amap_tools(mcp_tool: MCPTool, probe: bool = False) -> List[str]:
    """检查必要工具；健康探测通过实际的 list_tools 请求确认连接。"""
    names = {
        item.get("name") for item in mcp_tool._available_tools
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }
    if not names:
        raise AmapServiceUnavailableError("高德 MCP 未发现可用工具，请检查服务启动和网络")
    if probe:
        try:
            result = mcp_tool.run({"action": "list_tools"})
        except Exception as exc:
            raise AmapServiceUnavailableError("高德 MCP 连接探测失败") from exc
        # HelloAgents MCPTool.run 将 list_tools 格式化为逐行工具名称。
        names = set(re.findall(r"(?m)^-\s+([A-Za-z0-9_]+):", result)) if isinstance(result, str) else set()
        if not names:
            raise AmapServiceUnavailableError("高德 MCP 连接探测失败，未返回工具列表")
    missing = AMAP_REQUIRED_TOOLS - names
    if missing:
        raise AmapServiceUnavailableError("高德 MCP 缺少必要工具: " + ", ".join(sorted(missing)))
    return sorted(names)


# 全局MCP工具实例
_amap_mcp_tool = None


def get_amap_mcp_tool() -> MCPTool:
    """
    获取高德地图MCP工具实例(单例模式)

    Returns:
        MCPTool实例
    """
    global _amap_mcp_tool

    if _amap_mcp_tool is None:
        settings = get_setting()

        if not settings.amap_api_key:
            raise AmapServiceUnavailableError("高德地图API Key未配置,请在.env文件中设置AMAP_API_KEY")

        # 创建MCP工具
        command = _resolve_uvx()
        try:
            candidate = MCPTool(
                name="amap",
                description="高德地图服务,支持POI搜索、路线规划、天气查询等功能",
                server_command=[command, "--python", sys.executable,
                                "--no-python-downloads", "--from", "amap-mcp-server",
                                "python", "-u", str(Path(__file__).with_name("amap_mcp_launcher.py"))],
                env={"AMAP_MAPS_API_KEY": settings.amap_api_key,
                     "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
                timeout=settings.amap_mcp_timeout,
                auto_expand=True,
            )
        except Exception as exc:
            raise AmapServiceUnavailableError("高德 MCP 初始化失败，请检查服务启动和网络") from exc
        check_amap_tools(candidate)
        # 初始化失败或发现空工具时不缓存，允许下次请求重试。
        _amap_mcp_tool = candidate

        print(f"✅ 高德地图MCP工具初始化成功")
        print(f"   工具数量: {len(_amap_mcp_tool._available_tools)}")

        # 打印可用工具列表
        if _amap_mcp_tool._available_tools:
            print("   可用工具:")
            for tool in _amap_mcp_tool._available_tools[:5]:  # 只打印前5个
                print(f"     - {tool.get('name', 'unknown')}")
            if len(_amap_mcp_tool._available_tools) > 5:
                print(f"     ... 还有 {len(_amap_mcp_tool._available_tools) - 5} 个工具")

    return _amap_mcp_tool


class AmapService:
    """将 MCP 返回数据转换为应用的数据模型。"""

    def __init__(self, mcp_tool: Optional[MCPTool] = None):
        self.mcp_tool = mcp_tool if mcp_tool is not None else get_amap_mcp_tool()

    def _call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Any:
        try:
            result = self.mcp_tool.run({
                "action": "call_tool",
                "tool_name": tool_name,
                "arguments": arguments,
            })
        except TimeoutError as exc:
            raise AmapServiceTimeoutError(f"调用 {tool_name} 超时，请稍后重试") from exc
        except Exception as exc:
            raise AmapServiceError(f"调用 {tool_name} 失败") from exc
        return self._parse_mcp_payload(result)

    @classmethod
    def _parse_mcp_payload(cls, result: Any, depth: int = 0) -> Any:
        """解析 MCP 的原始值、文本内容块或带说明前缀的序列化结果。"""
        if depth > 10:
            raise AmapServiceError("MCP响应包装层级过多")
        if isinstance(result, dict):
            if (result.get("isError") or result.get("success") is False
                    or str(result.get("status", "")) == "0" or result.get("error")):
                raise AmapServiceError("高德或MCP服务返回错误响应")
            if isinstance(result.get("structuredContent"), (dict, list)):
                return cls._parse_mcp_payload(result["structuredContent"], depth + 1)
            content = result.get("content")
            if isinstance(content, list):
                if not content:
                    raise AmapServiceError("MCP返回了空内容块")
                return cls._parse_mcp_payload(content, depth + 1)
            return result

        if isinstance(result, list):
            text_blocks = [
                item.get("text")
                for item in result
                if isinstance(item, dict) and isinstance(item.get("text"), str)
            ]
            if text_blocks:
                decoded = [cls._parse_mcp_payload(text, depth + 1) for text in text_blocks]
                if len(decoded) == 1:
                    return decoded[0]
                return [item for block in decoded for item in (block if isinstance(block, list) else [block])]
            return result

        if not isinstance(result, str):
            raise AmapServiceError(f"不支持的MCP响应类型: {type(result).__name__}")

        text = result.strip()
        if not text:
            raise AmapServiceError("MCP返回了空响应")

        try:
            decoded = json.loads(text)
        except json.JSONDecodeError:
            pass
        else:
            return cls._parse_mcp_payload(decoded, depth + 1)

        error_markers = ("异步操作失败", "MCP 操作失败", "MCP操作失败", "错误：", "错误:")
        if text.startswith(error_markers):
            raise AmapServiceError("MCP服务执行失败")

        candidate_positions = sorted({
            index
            for token in ("{", "[")
            for index in [text.find(token)]
            if index >= 0
        })
        decoder = json.JSONDecoder()
        for start in candidate_positions:
            candidate = text[start:]
            try:
                decoded, _ = decoder.raw_decode(candidate)
            except json.JSONDecodeError:
                try:
                    decoded = ast.literal_eval(candidate)
                except (SyntaxError, ValueError):
                    continue
            return cls._parse_mcp_payload(decoded, depth + 1)
        raise AmapServiceError("无法解析MCP返回数据")

    @staticmethod
    def _as_list(payload: Any, *keys: str) -> List[Any]:
        if isinstance(payload, list):
            return payload
        if not isinstance(payload, dict):
            raise AmapServiceError("MCP结果不是对象或列表")
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                return value
        data = payload.get("data")
        if data is not None and data is not payload:
            return AmapService._as_list(data, *keys)
        raise AmapServiceError("MCP结果缺少有效的列表字段")

    @staticmethod
    def _parse_location(value: Any) -> Optional[Location]:
        longitude = latitude = None
        if isinstance(value, str):
            parts = [part.strip() for part in value.split(",")]
            if len(parts) == 2:
                longitude, latitude = parts
        elif isinstance(value, dict):
            longitude = value.get("longitude", value.get("lng", value.get("lon")))
            latitude = value.get("latitude", value.get("lat"))

        try:
            longitude = float(longitude)
            latitude = float(latitude)
        except (TypeError, ValueError):
            return None
        if not (-180 <= longitude <= 180 and -90 <= latitude <= 90):
            return None
        return Location(longitude=longitude, latitude=latitude)

    def search_poi(self, keywords: str, city: str, citylimit: bool = True) -> List[POIInfo]:
        """搜索 POI 并转换为结构化结果。"""
        if not keywords.strip() or not city.strip():
            return []

        payload = self._call_tool(
            "maps_text_search",
            {
                "keywords": keywords.strip(),
                "city": city.strip(),
                "citylimit": str(citylimit).lower(),
            },
        )
        pois = []
        for item in self._as_list(payload, "pois", "data"):
            if not isinstance(item, dict) or not item.get("name"):
                continue
            location = self._parse_location(item.get("location"))
            if location is None and item.get("id"):
                # 上游 text_search 仅返回 ID/name/address/typecode，按 ID 获取真实坐标。
                detail = self.get_poi_detail(str(item["id"]))
                if detail.get("id") and str(detail["id"]) != str(item["id"]):
                    raise AmapServiceError("POI详情与搜索结果的ID不匹配")
                item = {**item, **detail}
                location = self._parse_location(item.get("location"))
            if location is None:
                continue
            pois.append(
                POIInfo(
                    id=str(item.get("id") or ""),
                    name=str(item["name"]),
                    type=str(item.get("type") or item.get("typecode") or ""),
                    address=str(item.get("address") or ""),
                    location=location,
                    tel=str(item["tel"]) if item.get("tel") else None,
                )
            )
        return pois

    def get_weather(self, city: str) -> List[WeatherInfo]:
        """查询天气并兼容预报和实时天气数据。"""
        if not city.strip():
            return []

        payload = self._call_tool("maps_weather", {"city": city.strip()})
        weather = []
        for item in self._weather_rows(payload):
            if not isinstance(item, dict):
                raise AmapServiceError("天气记录不是有效对象")
            date = item.get("date") or item.get("reporttime")
            if not date:
                raise AmapServiceError("天气记录缺少日期或发布时间")
            day_temp = item.get("daytemp", item.get("temperature", 0))
            night_temp = item.get("nighttemp", item.get("temperature", 0))
            try:
                entry = WeatherInfo(
                    date=str(date).split(" ", 1)[0],
                    day_weather=str(item.get("dayweather") or item.get("weather") or ""),
                    night_weather=str(item.get("nightweather") or item.get("weather") or ""),
                    day_temp=0 if day_temp is None or day_temp == "" else day_temp,
                    night_temp=0 if night_temp is None or night_temp == "" else night_temp,
                    wind_direction=str(
                        item.get("daywind") or item.get("winddirection") or item.get("nightwind") or ""
                    ),
                    wind_power=str(
                        item.get("daypower") or item.get("windpower") or item.get("nightpower") or ""
                    ),
                )
            except (TypeError, ValueError) as exc:
                raise AmapServiceError("天气记录字段格式无效") from exc
            weather.append(entry)
        return weather

    @classmethod
    def _weather_rows(cls, payload: Any) -> List[Any]:
        if isinstance(payload, list):
            rows = []
            for item in payload:
                if isinstance(item, dict) and "casts" in item:
                    rows.extend(cls._as_list(item, "casts"))
                else:
                    rows.append(item)
            return rows
        if not isinstance(payload, dict):
            raise AmapServiceError("天气结果不是对象或列表")
        forecasts = payload.get("forecasts")
        if isinstance(forecasts, list):
            return cls._weather_rows(forecasts)
        for key in ("casts", "lives"):
            if isinstance(payload.get(key), list):
                return payload[key]
        data = payload.get("data")
        if data is not None and data is not payload:
            return cls._weather_rows(data)
        if "date" in payload or "reporttime" in payload:
            return [payload]
        raise AmapServiceError("天气结果缺少预报或实时天气字段")

    def plan_route(
            self,
            origin_address: str,
            destination_address: str,
            origin_city: Optional[str] = None,
            destination_city: Optional[str] = None,
            route_type: str = "walking"
    ) -> RouteInfo:
        """规划路线并返回与 API 响应模型一致的数据。"""
        tool_map = {
            "walking": "maps_direction_walking_by_address",
            "driving": "maps_direction_driving_by_address",
            "transit": "maps_direction_transit_integrated_by_address",
        }
        if route_type not in tool_map:
            raise ValueError(f"不支持的路线类型: {route_type}")

        arguments = {
            "origin_address": origin_address,
            "destination_address": destination_address,
        }
        if origin_city:
            arguments["origin_city"] = origin_city
        if destination_city:
            arguments["destination_city"] = destination_city

        payload = self._call_tool(tool_map[route_type], arguments)
        route = self._find_route(payload)
        if route is None:
            raise AmapServiceError("未找到有效路线")

        distance = self._number(route.get("distance"))
        duration = self._number(route.get("duration"), integer=True)
        if distance is None or duration is None:
            raise AmapServiceError("路线结果缺少有效的距离或耗时")

        instructions = self._collect_instructions(route)
        description = "；".join(instructions) if instructions else f"{route_type}路线"
        return RouteInfo(
            distance=distance,
            duration=duration,
            route_type=route_type,
            description=description,
        )

    @classmethod
    def _find_route(cls, payload: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(payload, dict):
            return None
        for key in ("paths", "transits"):
            candidates = payload.get(key)
            if isinstance(candidates, list) and candidates and isinstance(candidates[0], dict):
                selected = dict(candidates[0])
                # 公交响应的总距离在 route 层，耗时在 transits[0] 层。
                for field in ("distance", "duration"):
                    if field not in selected and field in payload:
                        selected[field] = payload[field]
                return selected
        route = payload.get("route")
        if isinstance(route, dict):
            selected = cls._find_route(route)
            if selected is not None:
                return selected
            if "distance" in route and "duration" in route:
                return route
        data = payload.get("data")
        if isinstance(data, dict):
            return cls._find_route(data)
        if "distance" in payload and "duration" in payload:
            return payload
        return None

    @staticmethod
    def _number(value: Any, integer: bool = False) -> Optional[Any]:
        if isinstance(value, bool):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(number) or number < 0:
            return None
        return int(number) if integer else number

    @classmethod
    def _collect_instructions(cls, value: Any) -> List[str]:
        instructions = []
        if isinstance(value, dict):
            instruction = value.get("instruction")
            if instruction:
                instructions.append(str(instruction))
            for child in value.values():
                if isinstance(child, (dict, list)):
                    instructions.extend(cls._collect_instructions(child))
        elif isinstance(value, list):
            for child in value:
                instructions.extend(cls._collect_instructions(child))
        return instructions

    def geocode(self, address: str, city: Optional[str] = None) -> Optional[Location]:
        """
        地理编码(地址转坐标)

        Args:
            address: 地址
            city: 城市

        Returns:
            经纬度坐标
        """
        if not address.strip():
            return None
        arguments = {"address": address.strip()}
        if city:
            arguments["city"] = city.strip()
        payload = self._call_tool("maps_geo", arguments)
        geocodes = self._as_list(payload, "geocodes", "return", "data")
        if not geocodes or not isinstance(geocodes[0], dict):
            return None
        return self._parse_location(geocodes[0].get("location"))

    def get_poi_detail(self,poi_id) -> Dict[str,Any]:
        """
        获取POI详情

        Args:
            poi_id: POI ID

        Returns:
            POI详情信息
        """

        data = self._call_tool("maps_search_detail", {"id": str(poi_id)})
        if not isinstance(data, dict):
            raise AmapServiceError("POI详情不是有效对象")
        return data

_amap_service = None

def get_amap_service() -> AmapService:
    """获取高德地图服务实例(单例模式)"""
    global _amap_service
    if _amap_service is None:
        _amap_service = AmapService()
    return _amap_service
