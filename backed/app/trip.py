"""旅行规划API路由"""
from fastapi import APIRouter,HTTPException
from schemas import TripRequest, TripPlanResponse
from trip_planner_agent import (
    TripPlanningError, TripPlannerUnavailableError, get_trip_planner_agent,
)
router = APIRouter(prefix="/trip",tags=["旅游规划"])

@router.post(
    "/plan",
    response_model=TripPlanResponse,
    summary="生成旅游计划",
    description="根据用户输入的旅行需求，生成详细的计划"
)
async def plan_trip(request:TripRequest):
    """
    生成旅行计划

    Args:
        request: 旅行请求参数

    Returns:
        旅行计划响应
    """
    try:
        print(f"\n{'=' * 60}")
        print(f"📥 收到旅行规划请求:")
        print(f"   城市: {request.city}")
        print(f"   日期: {request.start_date} - {request.end_date}")
        print(f"   天数: {request.travel_days}")
        print(f"{'=' * 60}\n")
        # 获取Agent实例
        print("🔄 获取多智能体系统实例...")
        agent = get_trip_planner_agent()
        # 生成旅行计划
        print("🚀 开始生成旅行计划...")
        trip_plan = agent.plan_trip(request)
        print("✅ 旅行计划生成成功,准备返回响应\n")
        return TripPlanResponse(
            success=True,
            message="旅行计划生成成功",
            data=trip_plan
        )
    except TripPlannerUnavailableError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except TripPlanningError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail="旅行规划发生内部错误"
        ) from e
@router.get(
    "/health",
    summary="健康检查",
    description="检查旅行规划服务是否正常"
)
async def health_check():
    """健康检查"""
    try:
        agent = get_trip_planner_agent()

        return agent.get_health()
    except Exception as e:
        raise HTTPException(
            status_code=503,
            detail=str(e) if isinstance(e, TripPlannerUnavailableError) else "旅行规划服务不可用"
        ) from e
