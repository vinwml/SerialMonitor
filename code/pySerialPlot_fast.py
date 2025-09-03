# Serial Plotter — Downsampled Display, Full Logging, with PSI conversion (Qt5/Qt6 compatible)
# FIXES:
# - Robustly detects CSV format:
#     OLD floats: t_us,raw,avg,v_adc,v_sensor
#     NEW ints  : t_us,raw,avg_mcounts,v_adc_uV,v_sensor_uV  (avg/1000, volts = uV/1e6)
# - Computes psi correctly; no false 5 psi clamp.
# - Optionally saves processed CSV: iso_time,t_us,raw,avg,v_adc,v_sensor,psi
#
# Deps: pip install pyqt5 pyqtgraph pyserial   (or use PyQt6)

import sys, time
import serial, serial.tools.list_ports
from collections import deque

# --- Sensor conversion constants (ABPDANT005PGAA5) ---
FS_PSI   = 5.0   # full-scale pressure (psi)
VS_VOLTS = 5.0   # sensor supply voltage used for conversion (Volts)

# ---- Qt compatibility (PyQt6 preferred, fallback to PyQt5) ----
PYQT_VER = 0
def _import_qt():
    global PYQT_VER
    try:
        from PyQt6.QtWidgets import (
            QApplication, QMainWindow, QPushButton, QLabel,
            QVBoxLayout, QWidget, QHBoxLayout, QComboBox,
            QPlainTextEdit, QCheckBox, QSpinBox
        )
        from PyQt6.QtCore import QTimer, QDateTime
        PYQT_VER = 6
        return (QApplication, QMainWindow, QPushButton, QLabel,
                QVBoxLayout, QWidget, QHBoxLayout, QComboBox,
                QPlainTextEdit, QCheckBox, QSpinBox, QTimer, QDateTime)
    except Exception:
        from PyQt5.QtWidgets import (
            QApplication, QMainWindow, QPushButton, QLabel,
            QVBoxLayout, QWidget, QHBoxLayout, QComboBox,
            QPlainTextEdit, QCheckBox, QSpinBox
        )
        from PyQt5.QtCore import QTimer, QDateTime
        PYQT_VER = 5
        return (QApplication, QMainWindow, QPushButton, QLabel,
                QVBoxLayout, QWidget, QHBoxLayout, QComboBox,
                QPlainTextEdit, QCheckBox, QSpinBox, QTimer, QDateTime)

(QApplication, QMainWindow, QPushButton, QLabel,
 QVBoxLayout, QWidget, QHBoxLayout, QComboBox,
 QPlainTextEdit, QCheckBox, QSpinBox, QTimer, QDateTime) = _import_qt()

import pyqtgraph as pg


CSV_HEADER_FIELDS = ["t_us", "raw", "avg", "v_adc", "v_sensor", "psi"]


class SerialPlotApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Serial Plotter — Downsampled Display, Full Logging (with PSI) — FIXED")
        self.setGeometry(80, 80, 1150, 760)

        # --- Runtime state ---
        self.serial = None
        self.raw_file = None         # raw lines as-is
        self.proc_file = None        # processed CSV with units
        self.logging_raw = False
        self.logging_proc = False

        # RX buffer for partial lines
        self._rx_buf = bytearray()

        # Monitor (throttled)
        self.monitor_update_ms = 300
        self.monitor_last_update = 0
        self.monitor_new_lines = []

        # Data buffers
        self.buffer_seconds = 10
        self.max_points_cap = 2_000_000
        self.t0_us = None
        self.x = deque()   # seconds
        self.y = deque()   # selected field

        # --- Widgets ---
        self.plot_widget = pg.PlotWidget()
        self.plot_widget.setBackground('w')
        self.plot_widget.showGrid(x=True, y=True, alpha=0.25)
        self.plot_widget.setLabel('bottom', 'Time', units='s')
        self.plot_widget.setLabel('left', 'Value')
        self.curve = self.plot_widget.plot([], [], pen=pg.mkPen(width=2))
        self.plot_widget.setClipToView(True)

        self.port_selector = QComboBox()
        self.baud_selector = QComboBox()
        self.baud_selector.addItems(["9600", "115200", "230400", "460800", "921600", "1000000", "2000000"])
        self.baud_selector.setCurrentText("115200")

        self.start_button = QPushButton("Start")
        self.start_button.setCheckable(True)
        self.start_button.clicked.connect(self.toggle_plotting)

        self.duration_selector = QComboBox()
        self.duration_selector.addItems(["5 sec", "10 sec", "30 sec", "1 min", "5 min", "10 min"])
        self.duration_selector.setCurrentIndex(1)
        self.duration_selector.currentIndexChanged.connect(self.update_buffer_size)

        self.field_selector = QComboBox()
        self.field_selector.addItems(["psi", "v_sensor", "v_adc", "avg", "raw"])
        self.field_selector.currentIndexChanged.connect(self.update_y_axis)

        self.auto_y_checkbox = QCheckBox("Auto Y")
        self.auto_y_checkbox.setChecked(False)
        self.auto_y_checkbox.stateChanged.connect(self.update_y_axis)

        self.pause_plot_checkbox = QCheckBox("Pause plot (keep logging)")
        self.pause_plot_checkbox.setChecked(False)

        self.save_raw_checkbox = QCheckBox("Save raw CSV")
        self.save_raw_checkbox.setChecked(True)

        self.save_proc_checkbox = QCheckBox("Save processed CSV")
        self.save_proc_checkbox.setChecked(True)

        self.ds_label = QLabel("Plot every N-th:")
        self.ds_spin = QSpinBox()
        self.ds_spin.setRange(1, 1000)
        self.ds_spin.setValue(10)

        self.maxpoints_label = QLabel("Max plot points:")
        self.maxpoints_spin = QSpinBox()
        self.maxpoints_spin.setRange(200, 200000)
        self.maxpoints_spin.setValue(2000)

        self.label_value = QLabel("Value: --")
        self.label_status = QLabel("Status: Waiting...")

        self.serial_monitor = QPlainTextEdit()
        self.serial_monitor.setReadOnly(True)
        self.serial_monitor.setMaximumBlockCount(300)
        self.serial_monitor.setFixedHeight(160)

        # Layouts
        top = QHBoxLayout()
        top.addWidget(QLabel("Port:"))
        top.addWidget(self.port_selector)
        top.addWidget(QLabel("Baud:"))
        top.addWidget(self.baud_selector)
        top.addWidget(self.start_button)

        controls = QHBoxLayout()
        controls.addWidget(QLabel("Buffer:"))
        controls.addWidget(self.duration_selector)
        controls.addWidget(QLabel("Y:"))
        controls.addWidget(self.field_selector)
        controls.addWidget(self.auto_y_checkbox)
        controls.addStretch(1)
        controls.addWidget(self.pause_plot_checkbox)
        controls.addStretch(1)
        controls.addWidget(self.ds_label)
        controls.addWidget(self.ds_spin)
        controls.addWidget(self.maxpoints_label)
        controls.addWidget(self.maxpoints_spin)
        controls.addStretch(1)
        controls.addWidget(self.save_raw_checkbox)
        controls.addWidget(self.save_proc_checkbox)

        labels = QHBoxLayout()
        labels.addWidget(self.label_value)
        labels.addStretch(1)
        labels.addWidget(self.label_status)

        main = QVBoxLayout()
        main.addLayout(top)
        main.addLayout(controls)
        main.addWidget(self.plot_widget)
        main.addLayout(labels)
        main.addWidget(QLabel("Serial Monitor (throttled):"))
        main.addWidget(self.serial_monitor)

        container = QWidget()
        container.setLayout(main)
        self.setCentralWidget(container)

        # Timers
        self.poll_timer = QTimer(); self.poll_timer.timeout.connect(self.poll_serial); self.poll_timer.start(5)
        self.ui_timer   = QTimer(); self.ui_timer.timeout.connect(self.update_ui);   self.ui_timer.start(50)
        self.refresh_timer = QTimer(); self.refresh_timer.timeout.connect(self.refresh_ports); self.refresh_timer.start(2000)

        self.refresh_ports()
        self.update_buffer_size()
        self.update_y_axis()

    # ---------- Helpers ----------
    def refresh_ports(self):
        current = self.port_selector.currentText()
        ports = [p.device for p in serial.tools.list_ports.comports()]
        self.port_selector.blockSignals(True)
        self.port_selector.clear()
        self.port_selector.addItems(ports)
        if current in ports:
            self.port_selector.setCurrentIndex(self.port_selector.findText(current))
        self.port_selector.blockSignals(False)

    def toggle_plotting(self):
        if self.start_button.isChecked():
            port = self.port_selector.currentText()
            if not port:
                self.label_status.setText("Status: No serial port selected")
                self.start_button.setChecked(False); return
            try:
                baud = int(self.baud_selector.currentText())
            except ValueError:
                baud = 115200
            try:
                self.serial = serial.Serial(port, baudrate=baud, timeout=0)
                self.reset_session_state()

                self.logging_raw  = self.save_raw_checkbox.isChecked()
                self.logging_proc = self.save_proc_checkbox.isChecked()
                ts = time.strftime("%Y%m%d-%H%M%S")
                if self.logging_raw:
                    self.raw_file = open(f"serial_raw_{ts}.csv", "a", buffering=1)
                if self.logging_proc:
                    self.proc_file = open(f"serial_proc_{ts}.csv", "a", buffering=1)
                    self.proc_file.write("iso_time,t_us,raw,avg,v_adc,v_sensor,psi\n")

                self.label_status.setText("Status: Running")
                self.start_button.setText("Stop")
            except serial.SerialException as e:
                self.label_status.setText(f"Status: Error - {e}")
                self.start_button.setChecked(False)
        else:
            self.stop_plotting()

    def stop_plotting(self):
        if self.serial:
            try: self.serial.close()
            except: pass
            self.serial = None
        if self.raw_file:
            try: self.raw_file.close()
            except: pass
            self.raw_file = None
        if self.proc_file:
            try: self.proc_file.close()
            except: pass
            self.proc_file = None
        self.label_status.setText("Status: Stopped")
        self.start_button.setText("Start")

    def update_buffer_size(self):
        m = {"5 sec":5,"10 sec":10,"30 sec":30,"1 min":60,"5 min":300,"10 min":600}
        self.buffer_seconds = m.get(self.duration_selector.currentText(), 10)

    def reset_session_state(self):
        self._rx_buf.clear()
        self.monitor_new_lines.clear()
        self.serial_monitor.clear()
        self.x.clear(); self.y.clear()
        self.t0_us = None
        self.curve.setData([], [])

    def update_y_axis(self):
        field = self.field_selector.currentText()
        if self.auto_y_checkbox.isChecked():
            self.plot_widget.enableAutoRange(axis=pg.ViewBox.YAxis, enable=True)
        else:
            self.plot_widget.enableAutoRange(axis=pg.ViewBox.YAxis, enable=False)
            if field in ("raw","avg"):
                self.plot_widget.setYRange(0, 4096)
            elif field == "v_adc":
                self.plot_widget.setYRange(0, 3.3)
            elif field == "v_sensor":
                self.plot_widget.setYRange(0, 5.0)
            elif field == "psi":
                self.plot_widget.setYRange(0, FS_PSI)

    # ---------- Parsing ----------
    @staticmethod
    def _is_intlike_big(s):
        # True if no decimal and magnitude typical for microvolts (or large integers)
        if '.' in s or 'e' in s or 'E' in s: return False
        try:
            return abs(int(s)) > 20000  # >20k => likely uV or mcounts
        except: return False

    def parse_line(self, s):
        s = s.strip()
        if not s: return None
        if s.lower().startswith(("t_us","t,","time_us")): return None

        parts = s.split(',')
        if len(parts) >= 5:
            # Heuristic: int-only format if v_adc & v_sensor look like big integers with no decimal
            int_like = self._is_intlike_big(parts[3]) and self._is_intlike_big(parts[4])

            if int_like:
                # NEW int-only format
                try:
                    t_us = int(parts[0])
                    raw  = int(parts[1])
                    avg_mcounts = int(parts[2])
                    v_adc_uV    = int(parts[3])
                    v_sensor_uV = int(parts[4])

                    avg = avg_mcounts / 1000.0
                    v_adc = v_adc_uV / 1e6
                    v_sensor = v_sensor_uV / 1e6
                except Exception:
                    return None
            else:
                # OLD float format
                try:
                    t_us = int(parts[0])
                    raw  = int(float(parts[1]))
                    avg  = float(parts[2])
                    v_adc = float(parts[3])
                    v_sensor = float(parts[4])
                except Exception:
                    return None

            # PSI conversion (ratiometric 10–90% of Vs)
            psi = FS_PSI * ((v_sensor / VS_VOLTS) - 0.10) / 0.80
            # Only clamp softly to keep within 0..FS range
            if psi < 0.0: psi = 0.0
            if psi > FS_PSI: psi = FS_PSI

            return {"t_us": t_us, "raw": raw, "avg": avg, "v_adc": v_adc, "v_sensor": v_sensor, "psi": psi}

        # Fallback: single numeric line
        try: return {"val": float(s)}
        except: return None

    # ---------- I/O + UI ----------
    def poll_serial(self):
        if not self.serial: return
        try:
            n = self.serial.in_waiting
            if n:
                chunk = self.serial.read(n)
                self._rx_buf.extend(chunk)
                while True:
                    idx = self._rx_buf.find(b'\n')
                    if idx < 0: break
                    raw_bytes = self._rx_buf[:idx]; del self._rx_buf[:idx+1]

                    # RAW logging first (exactly as received)
                    if self.logging_raw and self.raw_file:
                        try: self.raw_file.write(raw_bytes.decode(errors='ignore') + '\n')
                        except: pass

                    line = raw_bytes.decode(errors='ignore').rstrip('\r')
                    # Light monitor
                    now_str = QDateTime.currentDateTime().toString("HH:mm:ss.zzz")
                    self.monitor_new_lines.append(f"[{now_str}] {line}")

                    parsed = self.parse_line(line)
                    if parsed is None: continue

                    # Processed logging (converted units)
                    if self.logging_proc and self.proc_file:
                        try:
                            iso_now = QDateTime.currentDateTime().toString("yyyy-MM-dd HH:mm:ss.zzz")
                            row = f"{iso_now},{parsed['t_us']},{parsed['raw']},{parsed['avg']:.3f},{parsed['v_adc']:.6f},{parsed['v_sensor']:.6f},{parsed['psi']:.4f}\n"
                            self.proc_file.write(row)
                        except: pass

                    # X/Y buffers
                    if self.t0_us is None: self.t0_us = parsed['t_us']
                    t_sec = (parsed['t_us'] - self.t0_us) / 1e6

                    field = self.field_selector.currentText()
                    if   field == "raw":      y_val = parsed["raw"]
                    elif field == "avg":      y_val = parsed["avg"]
                    elif field == "v_adc":    y_val = parsed["v_adc"]
                    elif field == "v_sensor": y_val = parsed["v_sensor"]
                    elif field == "psi":      y_val = parsed["psi"]
                    else:                     y_val = parsed["v_sensor"]

                    self.x.append(t_sec); self.y.append(y_val)

                    # Trim by time window
                    t_min = t_sec - float(self.buffer_seconds)
                    while self.x and self.x[0] < t_min:
                        self.x.popleft(); self.y.popleft()

                    # Hard cap
                    if len(self.x) > self.max_points_cap:
                        drop = len(self.x) - self.max_points_cap
                        for _ in range(drop): self.x.popleft(); self.y.popleft()

        except Exception as e:
            self.label_status.setText(f"Status: Serial error - {e}")

    def update_ui(self):
        # Plot (decimated draw) + status + monitor
        if self.x and not self.pause_plot_checkbox.isChecked():
            n = len(self.x)
            ds = max(1, self.ds_spin.value())
            maxp = max(200, self.maxpoints_spin.value())
            step = max(ds, n // maxp) if n > maxp else ds

            xs = list(self.x)[::step]
            ys = list(self.y)[::step]
            self.curve.setData(xs, ys)

            # Labels
            y_last = self.y[-1]
            field = self.field_selector.currentText()
            if field in ("raw","avg"):
                self.label_value.setText(f"Value ({field}): {y_last:.2f}")
            elif field == "psi":
                self.label_value.setText(f"Value (psi): {y_last:.3f} psi")
            else:
                self.label_value.setText(f"Value ({field}): {y_last:.5f} V")

            sps = (len(self.x) - 1) / (self.x[-1] - self.x[0]) if len(self.x) > 1 and (self.x[-1] - self.x[0]) > 0 else 0.0
            span = self.x[-1] - self.x[0] if len(self.x) > 1 else 0.0
            self.label_status.setText(f"Status: Running — points: {len(self.x)} | rate: {sps:.0f} sps | span: {span:.2f}s | draw step: {step}")

        # Monitor (throttled)
        now_ms = int(time.time() * 1000)
        if now_ms - self.monitor_last_update >= self.monitor_update_ms and self.monitor_new_lines:
            self.serial_monitor.appendPlainText("\n".join(self.monitor_new_lines))
            self.serial_monitor.verticalScrollBar().setValue(self.serial_monitor.verticalScrollBar().maximum())
            self.monitor_new_lines.clear()
            self.monitor_last_update = now_ms

    def closeEvent(self, event):
        self.stop_plotting(); event.accept()


def app_exec(app):
    return app.exec() if PYQT_VER >= 6 else app.exec_()


if __name__ == '__main__':
    app = QApplication(sys.argv)
    # For absolute max draw speed:
    # pg.setConfigOptions(antialias=False)
    pg.setConfigOptions(antialias=True)
    window = SerialPlotApp()
    window.show()
    sys.exit(app_exec(app))
