"""Qt dock widget for napari-hardware-monitor."""

from __future__ import annotations

from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from typing import List, Optional

from qtpy.QtCore import QElapsedTimer, QRectF, QSize, Qt, QTimer
from qtpy.QtGui import QColor, QFont, QPainter, QPen
from qtpy.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .hardware import (
    HardwareSnapshot,
    napariHealthStats,
    get_hardware_snapshot,
    snapshot_to_text,
)


CARD_STYLE = """
QWidget {
    background: #111418;
    color: #e8edf3;
    font-size: 12px;
}
QFrame#metricCard {
    background: #202327;
    border: 1px solid #343941;
    border-radius: 8px;
}
QLabel#titleLabel {
    color: #f1f5f9;
    font-weight: 700;
}
QLabel#subtleLabel {
    color: #9aa4af;
}
QPushButton, QComboBox {
    background: #2c323a;
    color: #e8edf3;
    border: 1px solid #48505a;
    border-radius: 5px;
    padding: 4px 8px;
}
QPushButton:hover, QComboBox:hover {
    border-color: #6fb6ff;
}
QToolButton {
    background: #1a1e23;
    color: #dbe3ec;
    border: 1px solid #343941;
    border-radius: 5px;
    padding: 4px 6px;
    text-align: left;
}
QToolButton:hover {
    border-color: #6fb6ff;
}
"""


def _clamp_percent(value: Optional[float]) -> float:
    if value is None:
        return 0.0
    return max(0.0, min(100.0, float(value)))


def _format_gb(used: Optional[float], total: Optional[float]) -> str:
    if used is None or total is None or total <= 0:
        return "N/A"
    return f"{used:.1f} / {total:.1f} GB"


class GaugeWidget(QWidget):
    """Compact semicircle gauge drawn with Qt primitives."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.value = 0.0
        self.center_text = "0%"
        self.setMinimumSize(112, 76)

    def set_metric(self, value: float, center_text: str) -> None:
        self.value = _clamp_percent(value)
        self.center_text = center_text
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        rect = QRectF(8, 10, self.width() - 16, (self.height() - 10) * 1.65)
        pen_width = 7

        painter.setPen(QPen(QColor("#3a3d42"), pen_width, Qt.SolidLine, Qt.FlatCap))
        painter.drawArc(rect, 180 * 16, -180 * 16)

        if self.value < 70:
            color = QColor("#76d100")
        elif self.value < 90:
            color = QColor("#ff9f1a")
        else:
            color = QColor("#ff3b30")

        painter.setPen(QPen(color, pen_width, Qt.SolidLine, Qt.FlatCap))
        painter.drawArc(rect, 180 * 16, int(-180 * 16 * (self.value / 100.0)))

        painter.setPen(QColor("#f4f7fb"))
        font = QFont()
        font.setBold(True)
        font.setPointSize(16)
        painter.setFont(font)
        painter.drawText(
            self.rect().adjusted(0, 22, 0, -4),
            Qt.AlignCenter,
            self.center_text,
        )


class SparklineWidget(QWidget):
    """Small history chart for a metric percent series."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.values: list[float] = []
        self.setMinimumSize(112, 46)

    def set_values(self, values) -> None:
        self.values = [_clamp_percent(value) for value in values]
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        area = self.rect().adjusted(5, 4, -5, -5)

        grid_pen = QPen(QColor("#353a40"), 1)
        painter.setPen(grid_pen)
        for i in range(1, 4):
            y = area.top() + area.height() * i / 4
            painter.drawLine(area.left(), int(y), area.right(), int(y))

        if len(self.values) < 2:
            return

        points = []
        count = len(self.values)
        for index, value in enumerate(self.values):
            x = area.left() + area.width() * index / max(1, count - 1)
            y = area.bottom() - area.height() * value / 100.0
            points.append((int(x), int(y)))

        painter.setPen(QPen(QColor("#14a9ff"), 2))
        for start, end in zip(points, points[1:]):
            painter.drawLine(start[0], start[1], end[0], end[1])


class CpuCoresWidget(QFrame):
    """Compact per-core CPU bars for optional detail."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.values: List[float] = []
        self.setObjectName("metricCard")
        self.setMinimumHeight(86)

    def set_values(self, values: List[float]) -> None:
        self.values = [_clamp_percent(value) for value in values]
        rows = max(1, (len(self.values) + 1) // 2)
        self.setMinimumHeight(28 + rows * 16)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        area = self.rect().adjusted(10, 8, -10, -8)
        painter.setPen(QColor("#f1f5f9"))
        font = QFont()
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(area.left(), area.top() + 12, "CPU cores")

        if not self.values:
            painter.setPen(QColor("#9aa4af"))
            painter.drawText(area.left(), area.top() + 34, "Waiting for sample")
            return

        font.setBold(False)
        font.setPointSize(8)
        painter.setFont(font)
        columns = 2 if len(self.values) > 8 else 1
        rows = (len(self.values) + columns - 1) // columns
        col_width = area.width() / columns
        row_height = 16
        start_y = area.top() + 24

        for index, value in enumerate(self.values):
            col = index // rows
            row = index % rows
            x = int(area.left() + col * col_width)
            y = int(start_y + row * row_height)
            bar_x = x + 42
            bar_width = max(20, int(col_width) - 72)

            painter.setPen(QColor("#cbd5df"))
            painter.drawText(x, y + 10, f"CPU{index + 1}")

            painter.fillRect(bar_x, y + 2, bar_width, 8, QColor("#30363d"))
            if value < 70:
                color = QColor("#76d100")
            elif value < 90:
                color = QColor("#ff9f1a")
            else:
                color = QColor("#ff3b30")
            painter.fillRect(bar_x, y + 2, int(bar_width * value / 100.0), 8, color)
            painter.setPen(QColor("#9aa4af"))
            painter.drawText(bar_x + bar_width + 6, y + 10, f"{value:.0f}%")


class MetricCard(QFrame):
    """One dashboard card: label, gauge, and short trend."""

    def __init__(self, title: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("metricCard")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(5)

        self.title_label = QLabel(title)
        self.title_label.setObjectName("titleLabel")
        self.title_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.title_label)

        self.gauge = GaugeWidget()
        layout.addWidget(self.gauge)

        self.detail_label = QLabel("Waiting for sample")
        self.detail_label.setObjectName("subtleLabel")
        self.detail_label.setAlignment(Qt.AlignCenter)
        self.detail_label.setWordWrap(True)
        layout.addWidget(self.detail_label)

        self.sparkline = SparklineWidget()
        layout.addWidget(self.sparkline)

    def update_metric(self, value: float, center_text: str, detail: str, history) -> None:
        self.gauge.set_metric(value, center_text)
        self.detail_label.setText(detail)
        self.sparkline.set_values(history)


class HealthCard(QFrame):
    """Readable napari UI responsiveness status."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("metricCard")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(4)

        self.title_label = QLabel("napari Health")
        self.title_label.setObjectName("titleLabel")
        layout.addWidget(self.title_label)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(row)

        self.status_label = QLabel("Healthy")
        self.status_label.setStyleSheet("font-size: 21px; font-weight: 800; color: #76d100;")
        row.addWidget(self.status_label, 1)

        self.delay_label = QLabel("UI delay: 0 ms")
        self.delay_label.setObjectName("subtleLabel")
        self.delay_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        row.addWidget(self.delay_label)

        self.hint_label = QLabel("napari UI is responding normally.")
        self.hint_label.setObjectName("subtleLabel")
        self.hint_label.setWordWrap(True)
        layout.addWidget(self.hint_label)

        self.sparkline = SparklineWidget()
        self.sparkline.setMinimumHeight(30)
        layout.addWidget(self.sparkline)

    def update_health(self, health: napariHealthStats, history) -> None:
        self.status_label.setText(health.status)
        self.delay_label.setText(f"UI delay: {health.event_loop_delay_ms:.0f} ms")
        self.hint_label.setText(health.hint)
        self.sparkline.set_values(history)

        if health.status == "Healthy":
            color = "#76d100"
        elif health.status == "Busy":
            color = "#ffca3a"
        elif health.status == "Lagging":
            color = "#ff9f1a"
        else:
            color = "#ff3b30"
        self.status_label.setStyleSheet(
            f"font-size: 21px; font-weight: 800; color: {color};"
        )


class HardwareMonitorWidget(QWidget):
    """Main napari dock widget."""

    def __init__(self, napari_viewer=None):
        super().__init__()

        self.viewer = napari_viewer
        self._compact_max_height = 640
        self._expanded_max_height = 880
        self._health_interval_ms = 500
        self._health_clock = QElapsedTimer()
        self.current_health = napariHealthStats(
            status="Healthy",
            event_loop_delay_ms=0.0,
            hint="napari UI is responding normally.",
        )
        self.current_snapshot: Optional[HardwareSnapshot] = None
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="napari-hardware-monitor",
        )
        self._poll_future: Optional[Future] = None
        self._histories = {
            "cpu": deque(maxlen=90),
            "ram": deque(maxlen=90),
            "gpu": deque(maxlen=90),
            "vram": deque(maxlen=90),
            "health": deque(maxlen=90),
        }

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.request_update)
        self.health_timer = QTimer(self)
        self.health_timer.timeout.connect(self._sample_napari_health)

        self.setMinimumWidth(360)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        self._build_ui()
        self._health_clock.start()
        self.health_timer.start(self._health_interval_ms)
        self.start_monitoring()

    def sizeHint(self) -> QSize:  # noqa: N802
        expanded = hasattr(self, "cores_panel") and self.cores_panel.isVisible()
        return QSize(390, self._expanded_max_height if expanded else self._compact_max_height)

    def _build_ui(self) -> None:
        """Build a compact dashboard UI."""
        self.setStyleSheet(CARD_STYLE)
        self.setMaximumHeight(self._compact_max_height)

        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(8, 8, 8, 8)
        main_layout.setSpacing(8)
        self.setLayout(main_layout)

        title = QLabel("Hardware Monitor")
        title.setStyleSheet("font-weight: 700; font-size: 14px;")
        main_layout.addWidget(title)

        self.gpu_label = QLabel("GPU: checking")
        self.gpu_label.setObjectName("subtleLabel")
        self.gpu_label.setWordWrap(True)
        main_layout.addWidget(self.gpu_label)

        self.health_card = HealthCard()
        main_layout.addWidget(self.health_card)

        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(8)
        main_layout.addLayout(grid)

        self.cpu_card = MetricCard("CPU")
        self.ram_card = MetricCard("System Memory")
        self.gpu_card = MetricCard("GPU Utilization")
        self.vram_card = MetricCard("GPU Memory")

        grid.addWidget(self.ram_card, 0, 0)
        grid.addWidget(self.cpu_card, 0, 1)
        grid.addWidget(self.gpu_card, 1, 0)
        grid.addWidget(self.vram_card, 1, 1)

        self.cores_toggle = QToolButton()
        self.cores_toggle.setText("CPU cores")
        self.cores_toggle.setCheckable(True)
        self.cores_toggle.setToolTip("Show per-core CPU usage without cluttering the main dashboard.")
        self.cores_toggle.toggled.connect(self._toggle_cpu_cores)
        main_layout.addWidget(self.cores_toggle)

        self.cores_panel = CpuCoresWidget()
        self.cores_panel.setVisible(False)
        main_layout.addWidget(self.cores_panel)

        controls = QHBoxLayout()
        main_layout.addLayout(controls)

        self.start_button = QPushButton("Start")
        self.start_button.setToolTip("Start automatic hardware polling.")
        self.start_button.clicked.connect(self.start_monitoring)
        controls.addWidget(self.start_button)

        self.pause_button = QPushButton("Pause")
        self.pause_button.setToolTip("Pause automatic polling. The last values stay visible.")
        self.pause_button.clicked.connect(self.pause_monitoring)
        controls.addWidget(self.pause_button)

        self.refresh_combo = QComboBox()
        self.refresh_combo.addItem("1s", 1000)
        self.refresh_combo.addItem("2s", 2000)
        self.refresh_combo.addItem("5s", 5000)
        self.refresh_combo.setCurrentText("2s")
        self.refresh_combo.setToolTip("Polling interval. Use 2s or 5s for less overhead during heavy workflows.")
        self.refresh_combo.currentIndexChanged.connect(self._on_refresh_changed)
        controls.addWidget(self.refresh_combo)

        self.float_button = QPushButton("Float")
        self.float_button.setToolTip("Detach this monitor dock into a floating window.")
        self.float_button.clicked.connect(self.float_dock)
        controls.addWidget(self.float_button)

        self.copy_button = QPushButton("Copy")
        self.copy_button.setToolTip("Copy the current CPU, RAM, GPU, and VRAM values.")
        self.copy_button.clicked.connect(self.copy_snapshot)
        controls.addWidget(self.copy_button)

        self.status_label = QLabel("Paused")
        self.status_label.setObjectName("subtleLabel")
        main_layout.addWidget(self.status_label)

    def start_monitoring(self) -> None:
        """Start periodic polling."""
        interval_ms = self.refresh_combo.currentData()
        self.timer.start(interval_ms)
        self.status_label.setText(f"Monitoring every {self.refresh_combo.currentText()}")
        self.request_update()

    def pause_monitoring(self) -> None:
        """Stop periodic polling."""
        self.timer.stop()
        self.status_label.setText("Paused")

    def request_update(self) -> None:
        """Request one hardware sample without blocking the Qt UI."""
        if self._poll_future is not None and not self._poll_future.done():
            self.status_label.setText("Previous sample still running")
            return

        self._poll_future = self._executor.submit(get_hardware_snapshot)
        QTimer.singleShot(25, self._finish_update)

    def _finish_update(self) -> None:
        if self._poll_future is None:
            return
        if not self._poll_future.done():
            QTimer.singleShot(25, self._finish_update)
            return
        try:
            snapshot = self._poll_future.result()
        except Exception as exc:
            self.status_label.setText(f"Sample failed: {exc}")
            return
        self.update_stats(snapshot)

    def update_stats(self, snapshot: HardwareSnapshot) -> None:
        """Update dashboard cards from a completed hardware snapshot."""
        self.current_snapshot = snapshot

        cpu_ram = snapshot.cpu_ram
        gpu = snapshot.gpu

        cpu = _clamp_percent(cpu_ram.cpu_percent)
        ram = _clamp_percent(cpu_ram.ram_percent)
        gpu_util = _clamp_percent(gpu.gpu_util_percent if gpu.available else None)
        vram_percent = 0.0
        if gpu.available and gpu.vram_total_gb and gpu.vram_total_gb > 0:
            vram_percent = _clamp_percent(100 * (gpu.vram_used_gb or 0) / gpu.vram_total_gb)

        self._append_history("cpu", cpu)
        self._append_history("ram", ram)
        self._append_history("gpu", gpu_util)
        self._append_history("vram", vram_percent)

        self.cpu_card.update_metric(
            cpu,
            f"{cpu:.0f}%",
            f"{_load_hint(cpu)} load",
            self._histories["cpu"],
        )
        self.cores_panel.set_values(cpu_ram.cpu_per_core_percent)
        self.ram_card.update_metric(
            ram,
            f"{cpu_ram.ram_used_gb:.1f} GB",
            f"{cpu_ram.ram_total_gb:.1f} GB total",
            self._histories["ram"],
        )

        if gpu.available:
            gpu_detail = f"{gpu.temperature_c:.0f} C, {gpu.power_draw_w:.0f} W"
            self.gpu_label.setText(f"GPU: {gpu.name}")
            self.gpu_card.update_metric(
                gpu_util,
                f"{gpu_util:.0f}%",
                gpu_detail,
                self._histories["gpu"],
            )
            self.vram_card.update_metric(
                vram_percent,
                f"{vram_percent:.0f}%",
                _format_gb(gpu.vram_used_gb, gpu.vram_total_gb),
                self._histories["vram"],
            )
        else:
            self.gpu_label.setText(f"GPU: not available ({gpu.error or 'unknown'})")
            self.gpu_card.update_metric(0, "N/A", "No NVIDIA GPU", self._histories["gpu"])
            self.vram_card.update_metric(0, "N/A", "No VRAM sample", self._histories["vram"])

        if self.timer.isActive():
            self.status_label.setText(f"Last sample updated; next in {self.refresh_combo.currentText()}")

        self._update_health_display()

    def _append_history(self, name: str, value: float) -> None:
        self._histories[name].append(_clamp_percent(value))

    def copy_snapshot(self) -> None:
        """Copy the current hardware snapshot to the system clipboard."""
        if self.current_snapshot is None:
            self.request_update()
            return

        clipboard = QApplication.clipboard()
        clipboard.setText(snapshot_to_text(self.current_snapshot, self.current_health))

        self.status_label.setText("Snapshot copied to clipboard")

    def float_dock(self) -> None:
        """Ask the containing napari dock widget to float when available."""
        parent = self.parent()
        while parent is not None:
            if hasattr(parent, "setFloating"):
                parent.setFloating(True)
                parent.resize(self.sizeHint())
                self.status_label.setText("Detached as floating dock")
                return
            parent = parent.parent()
        self.status_label.setText("Use the dock title-bar button to detach")

    def _toggle_cpu_cores(self, checked: bool) -> None:
        self.cores_panel.setVisible(checked)
        self.cores_toggle.setText("CPU cores expanded" if checked else "CPU cores")
        self.setMaximumHeight(self._expanded_max_height if checked else self._compact_max_height)
        self.updateGeometry()

    def _on_refresh_changed(self) -> None:
        """Apply new refresh interval if monitoring is already active."""
        if self.timer.isActive():
            self.start_monitoring()

    def _sample_napari_health(self) -> None:
        """Measure Qt event-loop delay as a napari responsiveness signal."""
        elapsed_ms = max(0, self._health_clock.restart())
        delay_ms = max(0.0, float(elapsed_ms - self._health_interval_ms))
        self.current_health = self._make_health_stats(delay_ms)
        self._append_history("health", min(100.0, delay_ms / 30.0))
        self._update_health_display()

    def _make_health_stats(self, delay_ms: float) -> napariHealthStats:
        if delay_ms < 150:
            status = "Healthy"
        elif delay_ms < 750:
            status = "Busy"
        elif delay_ms < 2000:
            status = "Lagging"
        else:
            status = "Frozen recently"

        return napariHealthStats(
            status=status,
            event_loop_delay_ms=delay_ms,
            hint=self._health_hint(status),
        )

    def _health_hint(self, status: str) -> str:
        if status == "Healthy":
            return "napari UI is responding normally."

        if self.current_snapshot is None:
            return "napari UI is delayed; waiting for hardware sample."

        cpu_ram = self.current_snapshot.cpu_ram
        gpu = self.current_snapshot.gpu
        if cpu_ram.ram_percent >= 90:
            return "System memory is nearly full; napari may pause while data swaps or allocates."
        if gpu.available and gpu.vram_total_gb and gpu.vram_total_gb > 0:
            vram_percent = 100 * (gpu.vram_used_gb or 0) / gpu.vram_total_gb
            if vram_percent >= 90:
                return "GPU memory is nearly full; image or model work may stall."
        if cpu_ram.cpu_percent >= 90:
            return "CPU is saturated; a long calculation may be slowing napari."
        if gpu.available and (gpu.gpu_util_percent or 0) >= 90:
            return "GPU is heavily used; rendering or model inference may be busy."
        return "Hardware is not maxed out; a plugin or task may be blocking the Qt main thread."

    def _update_health_display(self) -> None:
        self.current_health.hint = self._health_hint(self.current_health.status)
        self.health_card.update_health(self.current_health, self._histories["health"])

    def closeEvent(self, event) -> None:  # noqa: N802
        self.timer.stop()
        self.health_timer.stop()
        self._executor.shutdown(wait=False, cancel_futures=True)
        super().closeEvent(event)


def _load_hint(value: float) -> str:
    """Short qualitative status for CPU load."""
    if value < 40:
        return "low"
    if value < 80:
        return "active"
    return "high"
