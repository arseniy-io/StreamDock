import signal

from fastapi.testclient import TestClient

from app.api import control
from app.main import app
from app.services import app_control


LOCAL_CLIENT = TestClient(app, client=("127.0.0.1", 51000))
REMOTE_CLIENT = TestClient(app, client=("192.168.1.50", 51000))


def test_shutdown_requires_real_local_client(monkeypatch) -> None:
    monkeypatch.setenv(app_control.CONTROL_TOKEN_ENV, "test-secret")
    monkeypatch.setattr(
        control.task_manager,
        "cancel_all",
        lambda: (_ for _ in ()).throw(AssertionError("Задачи не должны отменяться")),
    )

    response = REMOTE_CLIENT.post(
        "/api/control/shutdown",
        headers={
            app_control.CONTROL_TOKEN_HEADER: "test-secret",
            "X-Forwarded-For": "127.0.0.1",
        },
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Команда доступна только локальному помощнику"


def test_manual_start_uses_runtime_secret_for_safe_shutdown(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.delenv(app_control.CONTROL_TOKEN_ENV, raising=False)
    monkeypatch.setattr(control.task_manager, "cancel_all", lambda: 0)
    monkeypatch.setattr(
        app_control,
        "signal_process_shutdown",
        lambda: calls.append("signal"),
    )

    response = LOCAL_CLIENT.post(
        "/api/control/shutdown",
        headers={app_control.CONTROL_TOKEN_HEADER: app_control.get_control_token()},
    )

    assert response.status_code == 202
    assert response.json() == {"status": "stopping", "cancelled_tasks": 0}
    assert calls == ["signal"]


def test_health_exposes_runtime_secret_only_to_local_client(monkeypatch) -> None:
    monkeypatch.delenv(app_control.CONTROL_TOKEN_ENV, raising=False)

    local_response = LOCAL_CLIENT.get("/api/health")
    remote_response = REMOTE_CLIENT.get("/api/health")

    assert local_response.headers[app_control.CONTROL_TOKEN_RESPONSE_HEADER] == app_control.get_control_token()
    assert app_control.CONTROL_TOKEN_RESPONSE_HEADER not in remote_response.headers


def test_shutdown_rejects_invalid_secret(monkeypatch) -> None:
    monkeypatch.setenv(app_control.CONTROL_TOKEN_ENV, "correct-secret")

    response = LOCAL_CLIENT.post(
        "/api/control/shutdown",
        headers={app_control.CONTROL_TOKEN_HEADER: "wrong-secret"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Команда управления отклонена"


def test_shutdown_cancels_tasks_and_signals_process_after_response(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setenv(app_control.CONTROL_TOKEN_ENV, "test-secret")
    monkeypatch.setattr(control.task_manager, "cancel_all", lambda: 3)
    monkeypatch.setattr(
        app_control,
        "signal_process_shutdown",
        lambda: calls.append("signal"),
    )

    response = LOCAL_CLIENT.post(
        "/api/control/shutdown",
        headers={app_control.CONTROL_TOKEN_HEADER: "test-secret"},
    )

    assert response.status_code == 202
    assert response.json() == {"status": "stopping", "cancelled_tasks": 3}
    assert calls == ["signal"]


def test_shutdown_service_uses_sigterm(monkeypatch) -> None:
    received_signals: list[int] = []
    monkeypatch.setattr(signal, "raise_signal", received_signals.append)

    app_control.signal_process_shutdown()

    assert received_signals == [signal.SIGTERM]
