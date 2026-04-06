from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Sequence


MIN_PYTHON = (3, 10)
REQUIRED_PACKAGES = ("openai", "tqdm", "httpx")
DEPENDENCY_CHECK = "import openai, tqdm, httpx"


def _version_tuple(version_info: Sequence[int] | object) -> tuple[int, int]:
    if hasattr(version_info, "major") and hasattr(version_info, "minor"):
        return int(version_info.major), int(version_info.minor)
    return int(version_info[0]), int(version_info[1])


def ensure_supported_python(version_info: Sequence[int] | object | None = None) -> None:
    active_version = version_info or sys.version_info
    if _version_tuple(active_version) < MIN_PYTHON:
        raise RuntimeError("Python 3.10+ is required to run this project.")


def get_base_dir(base_dir: Path | str | None = None) -> Path:
    if base_dir is not None:
        return Path(base_dir)
    return Path(__file__).resolve().parent


def get_venv_dir(base_dir: Path) -> Path:
    return base_dir / ".venv"


def get_venv_python(base_dir: Path) -> Path:
    if os.name == "nt":
        return get_venv_dir(base_dir) / "Scripts" / "python.exe"
    return get_venv_dir(base_dir) / "bin" / "python"


def run_command(args: Sequence[str], cwd: Path, check: bool) -> subprocess.CompletedProcess[object]:
    return subprocess.run(list(args), cwd=str(cwd), check=check)


def ensure_venv(base_dir: Path, command_runner=run_command) -> Path:
    venv_dir = get_venv_dir(base_dir)
    venv_python = get_venv_python(base_dir)
    if venv_python.exists():
        return venv_python

    print("[INFO] Creating virtual environment in .venv ...")
    command_runner([sys.executable, "-m", "venv", str(venv_dir)], base_dir, True)
    return venv_python


def dependencies_ready(venv_python: Path, base_dir: Path, command_runner=run_command) -> bool:
    result = command_runner([str(venv_python), "-c", DEPENDENCY_CHECK], base_dir, False)
    return result.returncode == 0


def ensure_dependencies(venv_python: Path, base_dir: Path, command_runner=run_command) -> None:
    if dependencies_ready(venv_python, base_dir, command_runner):
        return

    print("[INFO] Installing required packages: openai tqdm httpx ...")
    command_runner([str(venv_python), "-m", "pip", "install", *REQUIRED_PACKAGES], base_dir, True)


def run_main(venv_python: Path, base_dir: Path, command_runner=run_command) -> int:
    result = command_runner([str(venv_python), "main.py"], base_dir, False)
    return int(result.returncode)


def bootstrap(
    base_dir: Path | str | None = None,
    version_info: Sequence[int] | object | None = None,
    command_runner=run_command,
) -> int:
    ensure_supported_python(version_info)
    project_dir = get_base_dir(base_dir)
    venv_python = ensure_venv(project_dir, command_runner)
    ensure_dependencies(venv_python, project_dir, command_runner)
    return run_main(venv_python, project_dir, command_runner)


def main() -> int:
    try:
        return bootstrap()
    except RuntimeError as exc:
        print(f"[ERROR] {exc}")
        return 1
    except subprocess.CalledProcessError as exc:
        return int(exc.returncode or 1)


if __name__ == "__main__":
    raise SystemExit(main())
