from napari_hardware_monitor.hardware import (
    CpuRamStats,
    GpuStats,
    HardwareSnapshot,
    get_cpu_ram_stats,
    get_nvidia_gpu_stats,
    snapshot_to_text,
)


def test_get_cpu_ram_stats_returns_valid_values():
    stats = get_cpu_ram_stats()

    assert stats.cpu_percent >= 0
    assert stats.ram_used_gb > 0
    assert stats.ram_total_gb > 0
    assert 0 <= stats.ram_percent <= 100


def test_snapshot_to_text_cpu_only():
    snapshot = HardwareSnapshot(
        cpu_ram=CpuRamStats(
            cpu_percent=12.5,
            cpu_per_core_percent=[10.0, 15.0],
            ram_used_gb=8.0,
            ram_total_gb=32.0,
            ram_percent=25.0,
        ),
        gpu=GpuStats(
            available=False,
            error="nvidia-smi not found",
        ),
    )

    text = snapshot_to_text(snapshot)

    assert "CPU: 12.5%" in text
    assert "CPU Cores: 1:10%, 2:15%" in text
    assert "RAM: 8.0 / 32.0 GB" in text
    assert "GPU: not available" in text


def test_get_nvidia_gpu_stats_aggregates_multiple_gpus(monkeypatch):
    class Result:
        stdout = (
            "NVIDIA A100, 20, 10240, 40960, 42, 80\n"
            "NVIDIA A100, 65, 20480, 40960, 50, 120\n"
        )

    monkeypatch.setattr(
        "napari_hardware_monitor.hardware.shutil.which",
        lambda _: "/usr/bin/nvidia-smi",
    )
    monkeypatch.setattr(
        "napari_hardware_monitor.hardware.subprocess.run",
        lambda *_, **__: Result(),
    )

    stats = get_nvidia_gpu_stats()

    assert stats.available is True
    assert stats.name == "2 NVIDIA GPUs"
    assert stats.gpu_count == 2
    assert stats.gpu_util_percent == 65
    assert stats.vram_used_gb == 30
    assert stats.vram_total_gb == 80
    assert stats.temperature_c == 50
    assert stats.power_draw_w == 200
