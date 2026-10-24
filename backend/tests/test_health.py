import json
import logging

from fastapi.testclient import TestClient

from app.health import check_database, check_redis
from app.logging import JsonFormatter
from app.main import create_app
from app.settings import Settings


def test_health_returns_ok(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def _client_with_checks(database: tuple[str, str], redis_status: str) -> TestClient:
    app = create_app()
    app.dependency_overrides[check_database] = lambda: database
    app.dependency_overrides[check_redis] = lambda: redis_status
    return TestClient(app)


def test_ready_when_all_dependencies_up() -> None:
    response = _client_with_checks(("ok", "ok"), "ok").get("/health/ready")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "database": "ok",
        "timescaledb": "ok",
        "redis": "ok",
    }


def test_ready_reports_missing_timescaledb() -> None:
    response = _client_with_checks(("ok", "unavailable"), "ok").get("/health/ready")
    assert response.status_code == 503
    assert response.json()["status"] == "degraded"
    assert response.json()["timescaledb"] == "unavailable"


def test_ready_reports_redis_down() -> None:
    response = _client_with_checks(("ok", "ok"), "unavailable").get("/health/ready")
    assert response.status_code == 503
    assert response.json()["redis"] == "unavailable"


def test_settings_defaults_match_claude_md() -> None:
    settings = Settings(_env_file=None)
    assert settings.daily_credit_budget == 5000
    assert [(t.min_hours_to_kickoff, t.interval_minutes) for t in settings.poll_tiers] == [
        (72, 720),
        (24, 180),
        (3, 60),
        (0, 15),
    ]
    assert settings.alert_threshold_pts == 2.0


def test_json_formatter_includes_extra_fields() -> None:
    record = logging.makeLogRecord(
        {"name": "ingest", "levelname": "INFO", "msg": "run done", "credits_used": 128}
    )
    payload = json.loads(JsonFormatter().format(record))
    assert payload["message"] == "run done"
    assert payload["credits_used"] == 128
    assert payload["level"] == "INFO"
