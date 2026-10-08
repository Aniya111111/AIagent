"""地图服务API路由"""
from fastapi import APIRouter,HTTPException,Query
from schemas import POISearchResponse, RouteRequest, RouteResponse, WeatherResponse

from amap_service import (
    AmapServiceError, AmapServiceTimeoutError, AmapServiceUnavailableError,
    check_amap_tools, get_amap_service,
)

router = APIRouter(prefix="/map",tags=["地图服务"])

@router.get(
    "/poi",
    response_model=POISearchResponse,
    summary="搜索POI",
    description="根据关键词搜索POI(兴趣点)"
)

def search_poi(
        keywords: str = Query(..., description="搜索关键词", examples=["故宫"]),
        city: str = Query(..., description="城市", examples=["北京"]),
        citylimit: bool = Query(True, description="是否限制在城市范围内")
):
    """
    搜索POI

    Args:
        keywords: 搜索关键词
        city: 城市
        citylimit: 是否限制在城市范围内

    Returns:
        POI搜索结果
    """
    try:
        service = get_amap_service()
        pois = service.search_poi(keywords,city,citylimit)

        return POISearchResponse(
            success=True,
            message="POI搜索成功" if pois else "未找到匹配的POI",
            data=pois,
        )
    except AmapServiceTimeoutError as e:
        raise HTTPException(status_code=504, detail=str(e)) from e
    except AmapServiceUnavailableError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except AmapServiceError as e:
        print(f"❌ POI搜索失败: {str(e)}")
        raise HTTPException(
            status_code=502,
            detail=f"POI搜索失败: {str(e)}"
        ) from e
@router.get(
    "/weather",
    response_model=WeatherResponse,
    summary="查询天气",
    description="查询指定城市的天气信息"
)
def get_weather(
        city: str = Query(..., description="城市名称", examples=["北京"])
):
    """
    查询天气

    Args:
        city: 城市名称

    Returns:
        天气信息
    """
    try:
        # 获取服务实例
        service = get_amap_service()
        weather_info = service.get_weather(city)
        return WeatherResponse(
            success=True,
            message="天气查询成功" if weather_info else "未找到天气信息",
            data=weather_info
        )
    except AmapServiceTimeoutError as e:
        raise HTTPException(status_code=504, detail=str(e)) from e
    except AmapServiceUnavailableError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except AmapServiceError as e:
        print(f"❌ 天气查询失败: {str(e)}")
        raise HTTPException(
            status_code=502,
            detail=f"天气查询失败: {str(e)}"
        ) from e
@router.post(
    "/route",
    response_model=RouteResponse,
    summary="规划路线",
    description="规划两点之间的路线"
)
def plan_router(request:RouteRequest):
    """
    规划路线

    Args:
        request: 路线规划请求

    Returns:
        路线信息
    """
    try:
        service = get_amap_service()

        # 规划路线
        route_info = service.plan_route(
            origin_address=request.origin_address,
            destination_address=request.destination_address,
            origin_city=request.origin_city,
            destination_city=request.destination_city,
            route_type=request.route_type
        )
        return RouteResponse(
            success=True,
            message="路线规划成功",
            data=route_info
        )
    except AmapServiceTimeoutError as e:
        raise HTTPException(status_code=504, detail=str(e)) from e
    except AmapServiceUnavailableError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except AmapServiceError as e:
        print(f"❌ 路线规划失败: {str(e)}")
        raise HTTPException(
            status_code=502,
            detail=f"路线规划失败: {str(e)}"
        ) from e
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
@router.get(
    "/health",
    summary="健康检查",
    description="检查地图服务是否正常"
)
def health_check():
    """健康检查"""
    try:
        service = get_amap_service()
        tool_names = check_amap_tools(service.mcp_tool, probe=True)
        return {
            "status":"healthy",
            "service":"map-service",
            "mcp_tools_count": len(tool_names),
            "check": "mcp_tool_discovery",
        }
    except Exception as e:
        raise HTTPException(
            status_code=503,
            detail=str(e) if isinstance(e, AmapServiceError) else "地图服务初始化失败"
        ) from e
