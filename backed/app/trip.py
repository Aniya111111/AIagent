"""旅行规划API路由"""
from fastapi import APIRouter,HTTPException
from schemas import TripRequest,ErrorResponse,TripPlanResponse
