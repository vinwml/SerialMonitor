
import sys
import serial
import serial.tools.list_ports
import csv
import time
from collections import deque

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QPushButton, QLabel,
    QVBoxLayout, QWidget, QHBoxLayout, QComboBox, QTextEdit,
    QCheckBox, QSpinBox
)
from PyQt5.QtCore import QTimer, QDateTime
import pyqtgraph as pg


CSV_HEADER_FIELDS = ["t_us", "raw", "avg", "v_adc", "v_sensor"]

class SerialPlotApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Serial Plotter with Logger & Monitor (XIAO nRF52840)")
        self.setGeometry(100, 100, 1100, 750)

        # Runtime state
        self.serial = None
        self.csv_writer = None
        self.csv_file = None
        self.monitor_lines = []                # [(timestamp_str, line), ...]
        self.max_monitor_lines = 300

        # Data buffers (time in seconds from first sample t0; y-values depend on selection)
        self.t0_us = None                      # first t_us seen (for X axis zero)
        self.x_data = []                       # seconds
        self.y_data = []                       # selected field values
        self.last_fields = None                # last parsed field dict (for labels/logging)
        self.buffer_seconds = 10               # default buffer window
        self.max_points_cap = 2_000_000        # hard cap to avoid runaway memory
        self.default_sps_guess = 1000          # used to size buffers before we can estimate

        # --- Widgets ---
        # Plotting widget
        self.plot_widget = pg.PlotWidget()
        self.plot_widget.setBackground('w')
        self.plot_widget.showGrid(x=True, y=True, alpha=0.25)
        self.plot_widget.setLabel('bottom', 'Time', units='s')
        self.plot_widget.setLabel('left', 'Value')
        self.plot_curve = self.plot_widget.plot([], [], pen=pg.mkPen(width=2))
        # Optimize paint/update
        self.plot_widget.setClipToView(True)

        # Serial port selector
        self.port_selector = QComboBox()
        # Baud selector (largely ignored for USB CDC, but present for flexibility)
        self.baud_selector = QComboBox()
        self.baud_selector.addItems(["9600", "115200", "230400", "460800", "921600", "1000000", "2000000"])
        self.baud_selector.setCurrentText("115200")

        # Start/Stop
        self.start_button = QPushButton("Start")
        self.start_button.setCheckable(True)
        self.start_button.clicked.connect(self.toggle_plotting)

        # Controls: buffer duration
        self.duration_selector = QComboBox()
        self.duration_selector.addItems(["5 sec", "10 sec", "30 sec", "1 min", "5 min", "10 min"])
        self.duration_selector.setCurrentIndex(1)  # default: 10 sec
        self.duration_selector.currentIndexChanged.connect(self.update_buffer_size)

        # Field selector (what to plot on Y)
        self.field_selector = QComboBox()
        self.field_selector.addItems(["v_sensor", "v_adc", "avg", "raw"])
        self.field_selector.currentIndexChanged.connect(self.on_field_changed)

        # Auto Y-range
        self.auto_y_checkbox = QCheckBox("Auto Y")
        self.auto_y_checkbox.setChecked(True)
        self.auto_y_checkbox.stateChanged.connect(self.update_y_axis)

        # CSV Logging
        self.save_csv_checkbox = QCheckBox("Save to CSV")
        self.save_csv_checkbox.setChecked(True)

        # Labels
        self.label_value = QLabel("Value: --")
        self.label_status = QLabel("Status: Waiting...")

        # Timestamp toggle for monitor
        self.timestamp_checkbox = QCheckBox("Show Timestamps in Monitor")
        self.timestamp_checkbox.setChecked(True)
        self.timestamp_checkbox.stateChanged.connect(self.refresh_monitor_display)

        # Raw serial monitor
        self.serial_monitor = QTextEdit()
        self.serial_monitor.setReadOnly(True)
        self.serial_monitor.setFixedHeight(180)

        # Layout
        top_layout = QHBoxLayout()
        top_layout.addWidget(QLabel("Port:"))
        top_layout.addWidget(self.port_selector)
        top_layout.addWidget(QLabel("Baud:"))
        top_layout.addWidget(self.baud_selector)
        top_layout.addWidget(self.start_button)

        control_layout = QHBoxLayout()
        control_layout.addWidget(QLabel("Buffer:"))
        control_layout.addWidget(self.duration_selector)
        control_layout.addWidget(QLabel("Y:"))
        control_layout.addWidget(self.field_selector)
        control_layout.addWidget(self.auto_y_checkbox)
        control_layout.addStretch(1)
        control_layout.addWidget(self.save_csv_checkbox)

        label_layout = QHBoxLayout()
        label_layout.addWidget(self.label_value)
        label_layout.addStretch(1)
        label_layout.addWidget(self.label_status)

        main_layout = QVBoxLayout()
        main_layout.addLayout(top_layout)
        main_layout.addLayout(control_layout)
        main_layout.addWidget(self.plot_widget)
        main_layout.addLayout(label_layout)
        main_layout.addWidget(QLabel("Serial Monitor:"))
        main_layout.addWidget(self.serial_monitor)

        container = QWidget()
        container.setLayout(main_layout)
        self.setCentralWidget(container)

        # Timers
        self.plot_timer = QTimer()
        self.plot_timer.timeout.connect(self.update_plot)
        self.refresh_timer = QTimer()
        self.refresh_timer.timeout.connect(self.refresh_ports)
        self.refresh_timer.start(2000)  # Refresh every 2 seconds

        # Initial fill
        self.refresh_ports()
        self.update_buffer_size()
        self.update_y_axis()

    # ---------- UI callbacks ----------

    def refresh_ports(self):
        current_port = self.port_selector.currentText()
        ports = [port.device for port in serial.tools.list_ports.comports()]
        self.port_selector.blockSignals(True)
        self.port_selector.clear()
        self.port_selector.addItems(ports)
        if current_port in ports:
            index = self.port_selector.findText(current_port)
            self.port_selector.setCurrentIndex(index)
        self.port_selector.blockSignals(False)

    def toggle_plotting(self):
        if self.start_button.isChecked():
            selected_port = self.port_selector.currentText()
            if not selected_port:
                self.label_status.setText("Status: No serial port selected")
                self.start_button.setChecked(False)
                return
            try:
                baud = int(self.baud_selector.currentText())
            except ValueError:
                baud = 115200

            try:
                # Open serial; on USB CDC the baud is typically ignored by the device.
                self.serial = serial.Serial(selected_port, baudrate=baud, timeout=0.05)
                self.reset_session_state()

                # Prepare CSV log file if requested
                if self.save_csv_checkbox.isChecked():
                    timestamp = time.strftime("%Y%m%d-%H%M%S")
                    self.csv_file = open(f"serial_log_{timestamp}.csv", 'w', newline='')
                    self.csv_writer = csv.writer(self.csv_file)
                    self.csv_writer.writerow(["iso_time"] + CSV_HEADER_FIELDS)

                self.plot_timer.start(20)  # UI refresh ~50 Hz
                self.label_status.setText("Status: Running")
                self.start_button.setText("Stop")
            except serial.SerialException as e:
                self.label_status.setText(f"Status: Error - {str(e)}")
                self.start_button.setChecked(False)
        else:
            self.stop_plotting()

    def stop_plotting(self):
        self.plot_timer.stop()
        if self.serial:
            try:
                self.serial.close()
            except Exception:
                pass
            self.serial = None
        if self.csv_file:
            try:
                self.csv_file.close()
            except Exception:
                pass
            self.csv_file = None
            self.csv_writer = None
        self.label_status.setText("Status: Stopped")
        self.start_button.setText("Start")

    def update_buffer_size(self):
        duration_map = {
            "5 sec": 5,
            "10 sec": 10,
            "30 sec": 30,
            "1 min": 60,
            "5 min": 300,
            "10 min": 600,
        }
        selected = self.duration_selector.currentText()
        seconds = duration_map.get(selected, 10)
        self.buffer_seconds = seconds

    def on_field_changed(self):
        self.update_y_axis()

    def update_y_axis(self):
        field = self.field_selector.currentText()
        if self.auto_y_checkbox.isChecked():
            self.plot_widget.enableAutoRange(axis=pg.ViewBox.YAxis, enable=True)
        else:
            self.plot_widget.enableAutoRange(axis=pg.ViewBox.YAxis, enable=False)
            if field in ("raw", "avg"):
                self.plot_widget.setYRange(0, 4096)
            elif field in ("v_adc",):
                self.plot_widget.setYRange(0, 3.5)
            else:  # v_sensor (0..5 V typical in this project)
                self.plot_widget.setYRange(0, 5.2)

    # ---------- Session / parsing ----------

    def reset_session_state(self):
        self.monitor_lines.clear()
        self.x_data.clear()
        self.y_data.clear()
        self.t0_us = None
        self.last_fields = None
        self.refresh_monitor_display()

    def parse_line(self, line):
        """Parse one incoming line.
        Returns:
            dict with keys among CSV_HEADER_FIELDS, or {'val': float} for single-value lines,
            or None for headers/invalid lines.
        """
        s = line.strip()
        if not s:
            return None
        # Skip header line (starts with "t_us," etc.)
        if s.lower().startswith("t_us") or s.lower().startswith("t,") or s.lower().startswith("time_us"):
            return None

        parts = s.split(',')
        if len(parts) >= 5:
            try:
                t_us = int(parts[0])
                raw = int(float(parts[1]))  # allow "123.0"
                avg = float(parts[2])
                v_adc = float(parts[3])
                v_sensor = float(parts[4])
                return {"t_us": t_us, "raw": raw, "avg": avg, "v_adc": v_adc, "v_sensor": v_sensor}
            except Exception:
                pass

        # Fallback: single numeric value
        try:
            val = float(s)
            return {"val": val}
        except Exception:
            return None

    # ---------- I/O loop & plotting ----------

    def update_plot(self):
        if not self.serial:
            return

        # Read as many lines as available (cap to prevent UI starvation)
        lines_processed = 0
        max_lines_per_tick = 5000  # plenty of headroom at 1 kHz
        while self.serial.in_waiting and lines_processed < max_lines_per_tick:
            try:
                raw = self.serial.readline()
            except Exception:
                break
            if not raw:
                break
            line = raw.decode(errors='ignore').strip()
            if not line:
                continue

            # Serial monitor rolling buffer
            now_str = QDateTime.currentDateTime().toString("HH:mm:ss.zzz")
            self.monitor_lines.append((now_str, line))
            if len(self.monitor_lines) > self.max_monitor_lines:
                self.monitor_lines = self.monitor_lines[-self.max_monitor_lines:]
            lines_processed += 1

            parsed = self.parse_line(line)
            if parsed is None:
                continue

            # If the device sends single numeric lines (fallback mode)
            if "val" in parsed and "t_us" not in parsed:
                # In this mode we don't have t_us; use wall-clock relative time
                if self.t0_us is None:
                    self.t0_us = int(time.monotonic() * 1e6)
                t_sec = (int(time.monotonic() * 1e6) - self.t0_us) / 1e6
                y_val = parsed["val"]
                self.x_data.append(t_sec)
                self.y_data.append(y_val)
                self.last_fields = {"t_us": int(t_sec * 1e6), "raw": None, "avg": y_val, "v_adc": None, "v_sensor": None}
                self.trim_buffers()
                continue

            # Normal CSV mode: we have t_us and the fields
            t_us = parsed["t_us"]
            if self.t0_us is None:
                self.t0_us = t_us
            t_sec = (t_us - self.t0_us) / 1e6

            field = self.field_selector.currentText()
            if field == "raw":
                y_val = parsed["raw"]
            elif field == "avg":
                y_val = parsed["avg"]
            elif field == "v_adc":
                y_val = parsed["v_adc"]
            else:  # v_sensor
                y_val = parsed["v_sensor"]

            self.x_data.append(t_sec)
            self.y_data.append(y_val)
            self.last_fields = parsed

            # CSV logging (richer schema)
            if self.csv_writer:
                iso_now = QDateTime.currentDateTime().toString("yyyy-MM-dd HH:mm:ss.zzz")
                row = [iso_now] + [parsed.get(k, "") for k in CSV_HEADER_FIELDS]
                try:
                    self.csv_writer.writerow(row)
                except Exception:
                    pass

        # Update the monitor text area (only when new lines arrived)
        if lines_processed > 0:
            self.refresh_monitor_display()

        # Plot current window
        if self.x_data:
            self.trim_by_time_window()
            self.plot_curve.setData(self.x_data, self.y_data)
            self.update_labels()

    def trim_by_time_window(self):
        """Trim x/y lists to only include the last buffer_seconds worth of data, also cap max points."""
        if not self.x_data:
            return
        t_latest = self.x_data[-1]
        t_min = t_latest - float(self.buffer_seconds)

        # Find first index >= t_min
        # Linear scan is okay for modest sizes, but to be safe perform a while-pop from left.
        # We keep lists; popping front is O(n). To avoid that, find start index and slice once.
        start_idx = 0
        # Binary search would be better, but pyqtgraph + 1 kHz with 10 min buffer is manageable.
        for i in range(len(self.x_data)-1, -1, -1):
            if self.x_data[i] < t_min:
                start_idx = i + 1
                break
        if start_idx > 0:
            self.x_data = self.x_data[start_idx:]
            self.y_data = self.y_data[start_idx:]

        # Hard cap
        if len(self.x_data) > self.max_points_cap:
            cut = len(self.x_data) - self.max_points_cap
            self.x_data = self.x_data[cut:]
            self.y_data = self.y_data[cut:]

    def trim_buffers(self):
        """Trim only by max cap (used in fallback mode without time window)"""
        if len(self.x_data) > self.max_points_cap:
            cut = len(self.x_data) - self.max_points_cap
            self.x_data = self.x_data[cut:]
            self.y_data = self.y_data[cut:]

    def update_labels(self):
        # Display the most recent value
        if not self.y_data:
            self.label_value.setText("Value: --")
            return

        y = self.y_data[-1]
        field = self.field_selector.currentText()
        if field in ("raw", "avg"):
            self.label_value.setText(f"Value ({field}): {y:.2f}")
        else:
            # volts
            self.label_value.setText(f"Value ({field}): {y:.5f} V")

        self.label_status.setText(f"Status: Running — points: {len(self.x_data)}")

    def refresh_monitor_display(self):
        self.serial_monitor.clear()
        show_time = self.timestamp_checkbox.isChecked()
        # Only draw the last N lines kept in monitor_lines
        for timestamp, line in self.monitor_lines:
            if show_time:
                display = f"[{timestamp}] {line}"
            else:
                display = line
            self.serial_monitor.append(display)

    def closeEvent(self, event):
        self.stop_plotting()
        event.accept()


if __name__ == '__main__':
    app = QApplication(sys.argv)

    # Slightly nicer default plot antialiasing
    pg.setConfigOptions(antialias=True)

    window = SerialPlotApp()
    window.show()
    sys.exit(app.exec_())
