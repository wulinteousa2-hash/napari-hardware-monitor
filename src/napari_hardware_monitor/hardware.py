"""
Hardware polling utilities.

This module keeps hardware detection separate from the napari/Qt UI.

Phase 1:
- CPU and RAM through psutil
- NVIDIA GPU through nvidia-smi
- graceful fallback when no GPU is found
"""

from __future__ import annotations

import shutil
import subprocess
from csv import reader
from dataclasses import dataclass
from typing import List, Optional

import psutil


@dataclass
class CpuRamStats:
    cpu_percent: float
    cpu_per_core_percent: List[float]
    ram_used_gb: float
    ram_total_gb: float
    ram_percent: float


@dataclass
class GpuStats:
    available: bool
    name: str = "No GPU detected"
    gpu_count: int = 0
    gpu_util_percent: Optional[float] = None
    vram_used_gb: Optional[float] = None
    vram_total_gb: Optional[float] = None
    temperature_c: Optional[float] = None
    power_draw_w: Optional[float] = None
    error: Optional[str] = None


@dataclass
class HardwareSnapshot:
    cpu_ram: CpuRamStats
    gpu: GpuStats


@dataclass
class napariHealthStats:
    status: str
    event_loop_delay_ms: float
    hint: str


def get_cpu_ram_stats() -> CpuRamStats:
    """
    Return CPU and system RAM usage.

    psutil.virtual_memory().used includes memory currently used by the system.
    Values are converted to GB for display.
    """
    cpu_percent = psutil.cpu_percent(interval=None)
    cpu_per_core_percent = [
        float(value) for value in psutil.cpu_percent(interval=None, percpu=True)
    ]
    mem = psutil.virtual_memory()

    gb = 1024 ** 3

    return CpuRamStats(
        cpu_percent=float(cpu_percent),
        cpu_per_core_percent=cpu_per_core_percent,
        ram_used_gb=mem.used / gb,
        ram_total_gb=mem.total / gb,
        ram_percent=float(mem.percent),
    )


def get_nvidia_gpu_stats() -> GpuStats:
    """
    Query NVIDIA GPU information using nvidia-smi.

    This avoids adding NVIDIA Python package dependencies in Phase 1.
    If nvidia-smi is not available, return a safe fallback object.
    """
    if shutil.which("nvidia-smi") is None:
        return GpuStats(
            available=False,
            error="nvidia-smi not found",
        )

    query_fields = [
        "name",
        "utilization.gpu",
        "memory.used",
        "memory.total",
        "temperature.gpu",
        "power.draw",
    ]

    cmd = [
        "nvidia-smi",
        f"--query-gpu={','.join(query_fields)}",
        "--format=csv,noheader,nounits",
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
            timeout=1,
        )
    except Exception as exc:
        return GpuStats(
            available=False,
            error=str(exc),
        )

    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]

    if not lines:
        return GpuStats(
            available=False,
            error="nvidia-smi returned no GPU rows",
        )

    rows = [[part.strip() for part in row] for row in reader(lines)]
    if not rows or any(len(row) < 6 for row in rows):
        return GpuStats(
            available=False,
            error=f"Unexpected nvidia-smi output: {result.stdout.strip()}",
        )

    mb_to_gb = 1024
    names = [row[0] for row in rows]
    utils = [_safe_float(row[1]) for row in rows]
    mem_used = [_safe_float(row[2]) for row in rows]
    mem_total = [_safe_float(row[3]) for row in rows]
    temps = [_safe_float(row[4]) for row in rows]
    powers = [_safe_float(row[5]) for row in rows]
    gpu_count = len(rows)
    display_name = names[0] if gpu_count == 1 else f"{gpu_count} NVIDIA GPUs"

    return GpuStats(
        available=True,
        name=display_name,
        gpu_count=gpu_count,
        gpu_util_percent=max(utils, default=0.0),
        vram_used_gb=sum(mem_used) / mb_to_gb,
        vram_total_gb=sum(mem_total) / mb_to_gb,
        temperature_c=max(temps, default=0.0),
        power_draw_w=sum(powers),
    )


def get_hardware_snapshot() -> HardwareSnapshot:
    """
    Collect one complete hardware snapshot.

    The UI should call this function on each timer tick.
    """
    return HardwareSnapshot(
        cpu_ram=get_cpu_ram_stats(),
        gpu=get_nvidia_gpu_stats(),
    )


def snapshot_to_text(
    snapshot: HardwareSnapshot,
    health: Optional[napariHealthStats] = None,
) -> str:
    """
    Convert hardware snapshot to plain text for clipboard/debugging.
    """
    cpu_ram = snapshot.cpu_ram
    gpu = snapshot.gpu

    lines = [
        "napari-hardware-monitor snapshot",
        "",
        f"CPU: {cpu_ram.cpu_percent:.1f}%",
        f"CPU Cores: {_format_cpu_cores(cpu_ram.cpu_per_core_percent)}",
        f"RAM: {cpu_ram.ram_used_gb:.1f} / {cpu_ram.ram_total_gb:.1f} GB ({cpu_ram.ram_percent:.1f}%)",
    ]

    if health is not None:
        lines.extend(
            [
                "",
                f"napari Health: {health.status}",
                f"UI Delay: {health.event_loop_delay_ms:.0f} ms",
                f"Health Hint: {health.hint}",
            ]
        )

    if gpu.available:
        vram_text = "N/A"
        if (
            gpu.vram_used_gb is not None
            and gpu.vram_total_gb
            and gpu.vram_total_gb > 0
        ):
            vram_text = f"{gpu.vram_used_gb:.1f} / {gpu.vram_total_gb:.1f} GB"
        lines.extend(
            [
                f"GPU: {gpu.name}",
                f"GPU Count: {gpu.gpu_count}",
                f"GPU Load: {gpu.gpu_util_percent:.1f}%",
                f"VRAM: {vram_text}",
                f"Temperature: {gpu.temperature_c:.0f} C",
                f"Power Draw: {gpu.power_draw_w:.1f} W",
            ]
        )
    else:
        lines.extend(
            [
                "GPU: not available",
                f"GPU Error: {gpu.error or 'unknown'}",
            ]
        )

    return "\n".join(lines)


def _safe_float(value: str) -> float:
    """
    Convert nvidia-smi string fields to float.

    Handles values like:
    - "87"
    - "68"
    - "[Not Supported]"
    """
    try:
        return float(value)
    except Exception:
        return 0.0


def _format_cpu_cores(values: List[float]) -> str:
    if not values:
        return "N/A"
    return ", ".join(f"{index + 1}:{value:.0f}%" for index, value in enumerate(values))
