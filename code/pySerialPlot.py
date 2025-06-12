import sys
import serial
import serial.tools.list_ports
import csv
import time
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QPushButton, QLabel,
    QVBoxLayout, QWidget, QHBoxLayout, QComboBox, QTextEdit,
    QCheckBox, QComboBox
)
import pyqtgraph as pg
from PyQt5.QtCore import QTimer, QDateTime

class SerialPlotApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Serial Plotter with Logger & Monitor")
        self.setGeometry(100, 100, 900, 700)

        self.serial = None
        self.data = []
        self.csv_writer = None
        self.csv_file = None
        self.max_data_points = 250 # default 5 sec * 50Hz
        self.monitor_lines = []  # stores (timestamp, line)
        self.max_monitor_lines = 100

        # Plotting widget
        self.plot_widget = pg.PlotWidget()
        self.plot_widget.setYRange(0, 4096)

        # Serial port selector
        self.port_selector = QComboBox()

        # Buttons and labels
        self.start_button = QPushButton("Start")
        self.start_button.setCheckable(True)
        self.start_button.clicked.connect(self.toggle_plotting)

        self.label_value = QLabel("Value: --")
        self.label_status = QLabel("Status: Waiting...")
        
        self.save_csv_checkbox = QCheckBox("Save to CSV")
        self.save_csv_checkbox.setChecked(True)  # Default: ON

        # Add buffer duration selector
        self.duration_selector = QComboBox()
        self.duration_selector.addItems(["5 sec", "10 sec", "30 sec", "1 min", "5 min", "10 min"])
        self.duration_selector.setCurrentIndex(0)  # Default: 1 min
        self.duration_selector.currentIndexChanged.connect(self.update_buffer_size)

        # Add checkbox for timestamp toggle in serial monitor
        self.timestamp_checkbox = QCheckBox("Show Timestamps in Monitor")
        self.timestamp_checkbox.setChecked(True)
        self.timestamp_checkbox.stateChanged.connect(self.refresh_monitor_display)

        # Raw serial monitor
        self.serial_monitor = QTextEdit()
        self.serial_monitor.setReadOnly(True)
        self.serial_monitor.setFixedHeight(150)

        # Layout
        top_layout = QHBoxLayout()
        top_layout.addWidget(QLabel("Serial Port:"))
        top_layout.addWidget(self.port_selector)
        top_layout.addWidget(self.start_button)

        # Add to layout (e.g., under top_layout or label_layout)
        control_layout = QHBoxLayout()
        control_layout.addWidget(QLabel("Buffer Duration:"))
        control_layout.addWidget(self.duration_selector)
        control_layout.addWidget(self.timestamp_checkbox)

        control_layout.addWidget(self.save_csv_checkbox)

        label_layout = QHBoxLayout()
        label_layout.addWidget(self.label_value)
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
        
        self.refresh_ports()

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
            try:
                self.serial = serial.Serial(selected_port, 9600, timeout=1)
                self.data = []
                if self.save_csv_checkbox.isChecked():
                    # Prepare CSV log file
                    timestamp = time.strftime("%Y%m%d-%H%M%S")
                    self.csv_file = open(f"serial_log_{timestamp}.csv", 'w', newline='')
                    self.csv_writer = csv.writer(self.csv_file)
                    self.csv_writer.writerow(["Timestamp", "Value"])
                else:
                    self.csv_writer = None

                self.plot_timer.start(1000 // 50)
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
            self.serial.close()
            self.serial = None
        if self.csv_file:
            self.csv_file.close()
            self.csv_file = None
        self.label_status.setText("Status: Stopped")
        self.start_button.setText("Start")

    def update_plot(self):
        if self.serial and self.serial.in_waiting:
            try:
                line = self.serial.readline().decode(errors='ignore').strip()
                if line:

                    now = QDateTime.currentDateTime().toString("HH:mm:ss.zzz")
                    self.monitor_lines.append((now, line))
                    if len(self.monitor_lines) > self.max_monitor_lines:
                        self.monitor_lines = self.monitor_lines[-self.max_monitor_lines:]

                    self.refresh_monitor_display()

                    # Keep Only Latest 100 Lines in Serial Monitor (optional)
                    if self.serial_monitor.document().blockCount() > 100:
                        cursor = self.serial_monitor.textCursor()
                        cursor.movePosition(cursor.Start)
                        cursor.select(cursor.LineUnderCursor)
                        cursor.removeSelectedText()
                        cursor.deleteChar()

                    value = float(line)
                    now = QDateTime.currentDateTime().toString("yyyy-MM-dd HH:mm:ss.zzz")
                    self.data.append((now, value))

                    if len(self.data) > self.max_data_points:
                        self.data = self.data[-self.max_data_points:]

                    values_only = [v for (_, v) in self.data]
                    self.plot_widget.plot(values_only, clear=True)
                    self.label_value.setText(f"Value: {value:.2f}")

                    # Log to CSV with timestamp
                    #now = QDateTime.currentDateTime().toString("yyyy-MM-dd HH:mm:ss.zzz")
                    if self.csv_writer:
                        self.csv_writer.writerow([now, value])
            except ValueError:
                self.label_status.setText("Status: Invalid data")

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
        seconds = duration_map.get(selected, 60)
        self.max_data_points = seconds * 50  # 50 Hz

    def refresh_monitor_display(self):
        self.serial_monitor.clear()
        show_time = self.timestamp_checkbox.isChecked()
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
    window = SerialPlotApp()
    window.show()
    sys.exit(app.exec_())
