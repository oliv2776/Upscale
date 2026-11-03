"""Détection du GPU Vulkan (Video2X --list-devices) et statistiques en direct (sysfs amdgpu)."""
from __future__ import annotations

import subprocess
import threading
from pathlib import Path
from typing import Any

from . import config, db, runner

gpu_state: dict[str, Any] = {"status": "never"}
_lock = threading.Lock()


def check_command() -> list[str]:
    command, _ = runner.video2x_command("gpucheck", ["--list-devices"])
    if config.RUNNER != "local":
        # Pas de nom fixe : plusieurs tests peuvent se suivre, et --rm nettoie.
        name_index = command.index("--name")
        del command[name_index:name_index + 2]
    return command


def _run_check() -> None:
    try:
        check = subprocess.run(check_command(), capture_output=True, text=True, timeout=1800, cwd=runner.video2x_cwd())
        lines = [line.strip() for line in check.stdout.splitlines() if line.strip()]
        result = {"status": "ok" if check.returncode == 0 and lines else "error", "devices": lines,
                  "error": None if check.returncode == 0 else (check.stderr or check.stdout).strip()[-700:]}
    except Exception as exc:  # noqa: BLE001
        result = {"status": "error", "devices": [], "error": str(exc)}
    with _lock:
        gpu_state.clear()
        gpu_state.update(result, checked_at=db.now())


def start_check() -> bool:
    with _lock:
        if gpu_state.get("status") == "running":
            return False
        gpu_state.clear()
        gpu_state.update(status="running", started_at=db.now())
    threading.Thread(target=_run_check, daemon=True).start()
    return True


def get_state() -> dict[str, Any]:
    with _lock:
        return dict(gpu_state)


def _read_int(path: Path) -> int | None:
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def stats() -> list[dict[str, Any]]:
    """Charge, VRAM, température, puissance et ventilateur des cartes amdgpu."""
    cards = []
    root = config.SYSFS_DRM
    if not root.is_dir():
        return cards
    for card in sorted(root.glob("card[0-9]*")):
        if "-" in card.name:
            continue
        device = card / "device"
        try:
            driver = (device / "driver").resolve().name
        except OSError:
            continue
        if driver != "amdgpu":
            continue
        info: dict[str, Any] = {
            "card": card.name,
            "busy_percent": _read_int(device / "gpu_busy_percent"),
            "vram_used": _read_int(device / "mem_info_vram_used"),
            "vram_total": _read_int(device / "mem_info_vram_total"),
        }
        hwmon = next(iter(sorted((device / "hwmon").glob("hwmon*"))), None) if (device / "hwmon").is_dir() else None
        if hwmon:
            temp = _read_int(hwmon / "temp1_input")
            power = _read_int(hwmon / "power1_average") or _read_int(hwmon / "power1_input")
            info.update(temperature_c=temp / 1000 if temp is not None else None,
                        power_w=power / 1_000_000 if power is not None else None,
                        fan_rpm=_read_int(hwmon / "fan1_input"))
        cards.append(info)
    return cards
