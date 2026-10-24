"""Liveness and readiness checks."""

from typing import Annotated, Literal

import redis
from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.settings import Settings, get_settings
from db.session import get_engine

router = APIRouter(tags=["health"])

CheckResult = Literal["ok", "unavailable"]


class Health(BaseModel):
    status: Literal["ok"] = "ok"


class Readiness(BaseModel):
    status: Literal["ok", "degraded"]
    database: CheckResult
    timescaledb: CheckResult
    redis: CheckResult


def check_database() -> tuple[CheckResult, CheckResult]:
    """Return (database, timescaledb) status."""
    try:
        with get_engine().connect() as conn:
            has_timescale = conn.execute(
                text("SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'")
            ).scalar()
    except SQLAlchemyError:
        return "unavailable", "unavailable"
    return "ok", "ok" if has_timescale else "unavailable"


def check_redis(settings: Annotated[Settings, Depends(get_settings)]) -> CheckResult:
    try:
        redis.Redis.from_url(settings.redis_url, socket_timeout=1).ping()
    except redis.RedisError:
        return "unavailable"
    return "ok"


@router.get("/health")
def health() -> Health:
    """Liveness: the process is up. Does not touch dependencies."""
    return Health()


@router.get("/health/ready")
def ready(
    response: Response,
    database: Annotated[tuple[CheckResult, CheckResult], Depends(check_database)],
    redis_status: Annotated[CheckResult, Depends(check_redis)],
) -> Readiness:
    """Readiness: Postgres, the TimescaleDB extension and Redis are reachable."""
    db_status, timescale_status = database
    healthy = db_status == timescale_status == redis_status == "ok"
    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return Readiness(
        status="ok" if healthy else "degraded",
        database=db_status,
        timescaledb=timescale_status,
        redis=redis_status,
    )
