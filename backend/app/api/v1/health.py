from fastapi import APIRouter
from app.core.database import check_db_connection
from app.core.redis import redis_manager

router = APIRouter(tags=["Health"])

@router.get("/health")
async def health_check():
    db_ok = await check_db_connection()
    redis_ok = await redis_manager.ping()
    overall_status = "healthy" if db_ok and redis_ok else "degraded"
    return {
        "status": overall_status,
        "services": {
            "api": "healthy",
            "postgres": "healthy" if db_ok else "unreachable",
            "redis": "healthy" if redis_ok else "unreachable",
        },
    }
