import base64
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app.config import APP_VERSION


PROJECT_ROOT = Path(__file__).resolve().parent.parent
EXTENSION_DIR = PROJECT_ROOT / "browser-extension"


def _extension_id(public_key: str) -> str:
    digest = hashlib.sha256(base64.b64decode(public_key)).digest()[:16]
    alphabet = "abcdefghijklmnop"
    return "".join(alphabet[byte >> 4] + alphabet[byte & 0x0F] for byte in digest)


def test_extension_manifest_uses_stable_streamdock_identity() -> None:
    manifest = json.loads((EXTENSION_DIR / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["manifest_version"] == 3
    assert manifest["name"] == "StreamDock"
    assert manifest["version"] == APP_VERSION
    assert manifest["background"]["service_worker"] == "background.js"
    assert manifest["action"]["default_popup"] == "popup.html"
    assert "nativeMessaging" in manifest["permissions"]
    assert "http://127.0.0.1:8765/*" in manifest["host_permissions"]
    assert _extension_id(manifest["key"]) == "pkodbfmcfgicbhcbmigbdkmkchppgeki"

    for size in (16, 32, 48, 128):
        assert (EXTENSION_DIR / manifest["icons"][str(size)]).is_file()


def test_extension_pages_reference_only_packaged_scripts_and_styles() -> None:
    popup = (EXTENSION_DIR / "popup.html").read_text(encoding="utf-8")
    progress = (EXTENSION_DIR / "progress.html").read_text(encoding="utf-8")

    assert '<script src="popup.js"></script>' in popup
    assert '<link rel="stylesheet" href="popup.css">' in popup
    assert '<script src="progress.js"></script>' in progress
    assert '<link rel="stylesheet" href="progress.css">' in progress
    assert "<script>" not in popup + progress
    assert "https://" not in popup + progress


def test_download_start_lives_in_service_worker_and_progress_page_is_persistent() -> None:
    background = (EXTENSION_DIR / "background.js").read_text(encoding="utf-8")
    popup = (EXTENSION_DIR / "popup.js").read_text(encoding="utf-8")
    progress = (EXTENSION_DIR / "progress.js").read_text(encoding="utf-8")

    assert "/api/extension/download" in background
    assert "/api/extension/download" not in popup
    assert "progress.html?job=" in background
    assert "chrome.storage.session" in background
    assert "chrome.storage.local" in background
    assert "ENSURE_JOB_STARTED" in background
    assert "CANCEL_JOB" in background
    assert "CANCEL_JOB" in progress
    assert "DELETE_JOB" in background
    assert "DELETE_JOB" not in progress
    assert 'method: "POST"' in background
    assert 'method: "DELETE"' in background
    assert 'method: "DELETE"' not in progress
    assert "/cancel" in background
    assert "/api/tasks/" in progress


def test_native_host_installer_matches_manifest_extension_id() -> None:
    installer = (PROJECT_ROOT / "scripts" / "install_native_host.ps1").read_text(encoding="utf-8")
    host_source = (PROJECT_ROOT / "scripts" / "native-host" / "StreamDockHost.cs").read_text(encoding="utf-8")

    assert 'extensionId = "pkodbfmcfgicbhcbmigbdkmkchppgeki"' in installer
    assert "com.streamdock.launcher" in installer
    assert 'case "start"' in host_source
    assert 'case "stop"' in host_source
    assert "JobObjectLimitKillOnJobClose" in host_source
    assert "CreateSuspended" in host_source
    assert 'ControlTokenResponseHeader = "X-StreamDock-Control-Token"' in host_source


def test_running_app_always_has_a_safe_stop_action() -> None:
    background = (EXTENSION_DIR / "background.js").read_text(encoding="utf-8")
    popup = (EXTENSION_DIR / "popup.js").read_text(encoding="utf-8")

    assert 'const CONTROL_TOKEN_RESPONSE_HEADER = "X-StreamDock-Control-Token"' in background
    assert "return stopHttpApp(httpStatus)" in background
    assert "stopButton.hidden = !appOnline" in popup


def test_download_click_opens_persistent_progress_page() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js не установлен")

    result = subprocess.run(
        [node, str(PROJECT_ROOT / "tests" / "extension_background_harness.js")],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "background download flow ok" in result.stdout
