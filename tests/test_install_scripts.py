from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _read(relative_path: str) -> str:
    return (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")


def _load_script(module_name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(module_name, PROJECT_ROOT / relative_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_batch_files_call_only_their_fixed_local_scripts() -> None:
    expected = {
        "install.bat": "scripts\\install.ps1",
        "start.bat": "scripts\\start.ps1",
        "update.bat": "scripts\\update.ps1",
        "download_models.bat": "scripts\\download_models.py",
        "uninstall.bat": "scripts\\uninstall_native_host.ps1",
    }
    for filename, target in expected.items():
        script = _read(filename)
        assert target in script
        assert "shell=True" not in script


def test_batch_files_switch_to_utf8_for_russian_messages() -> None:
    for filename in ("install.bat", "start.bat", "update.bat", "download_models.bat", "uninstall.bat"):
        assert "chcp 65001 >nul" in _read(filename)


def test_install_uses_venv_requirements_and_release_constraints() -> None:
    script = _read("scripts/install.ps1")
    assert '"-m", "venv"' in script
    assert '"-r", $requirements, "-c", $constraints' in script
    assert '"-m", "pip", "check"' in script
    assert 'foreach ($directoryName in @("downloads", "models", "logs"))' in script
    assert "requirements-dev.txt" not in script


def test_start_is_bound_to_loopback_and_prevents_known_duplicates() -> None:
    script = _read("scripts/start.ps1")
    assert "--host 127.0.0.1 --port 8765" in script
    assert "0.0.0.0" not in script
    assert "Test-StreamDockOnline" in script
    assert "Test-LocalPortOpen" in script
    assert "Вторая копия не создаётся" in script


def test_update_reuses_constrained_installer_and_requires_stopped_app() -> None:
    script = _read("scripts/update.ps1")
    assert "Test-StreamDockOnline" in script
    assert "Test-LocalPortOpen" in script
    assert "-UpgradeDependencies" in script
    assert 'Join-Path $PSScriptRoot "install.ps1"' in script


def test_system_check_version_parser() -> None:
    module = _load_script("streamdock_system_check", "scripts/system_check.py")
    assert module.parse_version("v22.11.0") == (22, 11, 0)
    assert module.parse_version("ffmpeg version 8.1") == (8, 1, 0)
    assert module.parse_version("unknown") is None


def test_system_check_contains_only_runtime_distributions() -> None:
    module = _load_script("streamdock_system_check_runtime", "scripts/system_check.py")
    assert "pytest" not in module.RUNTIME_DISTRIBUTIONS
    assert "httpx" not in module.RUNTIME_DISTRIBUTIONS
    assert {"yt-dlp", "faster-whisper", "onnx-asr", "sherpa-onnx"} <= set(module.RUNTIME_DISTRIBUTIONS)


def test_model_download_defaults_to_hybrid_models_and_project_cache() -> None:
    script = _read("scripts/download_models.py")
    assert 'args.whisper_models or ["large-v3"]' in script
    assert 'onnx_asr.load_model("gigaam-v3-e2e-rnnt", quantization="int8")' in script
    assert 'MODELS_DIR = PROJECT_ROOT / "models"' in script
    assert 'os.environ["HF_HOME"] = str(HUGGINGFACE_HOME)' in script
    assert "include_speaker_models = not args.skip_speaker_models and not args.gigaam_only" in script
    assert "ensure_speaker_models(report, Event(), models_directory=SPEAKER_MODELS_DIR)" in script


def test_powershell_scripts_use_windows_compatible_utf8_bom() -> None:
    for relative_path in ("scripts/install.ps1", "scripts/start.ps1", "scripts/update.ps1"):
        assert (PROJECT_ROOT / relative_path).read_bytes().startswith(b"\xef\xbb\xbf")
