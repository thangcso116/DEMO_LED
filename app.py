import sys, asyncio, math, time
from collections import deque

from PySide6.QtCore import Qt, QTimer, Signal, QObject
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QListWidget, QSlider, QColorDialog, QComboBox, QGroupBox,
    QGridLayout, QLineEdit, QCheckBox, QSpinBox, QMessageBox
)

try:
    from bleak import BleakScanner, BleakClient
except Exception:
    BleakScanner = None
    BleakClient = None

try:
    import mss
except Exception:
    mss = None

try:
    import numpy as np
except Exception:
    np = None

try:
    import soundcard as sc
except Exception:
    sc = None

# BJ_LED / MohuanLED community protocol references. Hardware/firmware variants exist.
DEFAULT_CHARACTERISTIC = "0000ee01-0000-1000-8000-00805f9b34fb"


class Bridge(QObject):
    devices_found = Signal(list)
    status = Signal(str)
    connected = Signal(bool)
    color_sent = Signal(bool, str)


class MohuanController:
    def __init__(self, bridge):
        self.bridge = bridge
        self.client = None
        self.address = None
        self.characteristic = DEFAULT_CHARACTERISTIC
        self.protocol = "BJ_LED RGB (69 96 05 02)"
        self.lock = asyncio.Lock()
        self.last_packet = None

    async def scan(self):
        if BleakScanner is None:
            self.bridge.status.emit("Chưa cài Bleak. Chạy install.bat trước.")
            return
        self.bridge.status.emit("Đang quét Bluetooth BLE...")
        try:
            found = await BleakScanner.discover(timeout=7, return_adv=True)
            rows = []
            for address, pair in found.items():
                device, adv = pair
                name = device.name or adv.local_name or "(Không tên)"
                if "BJ_LED" in name.upper() or "MOHUAN" in name.upper() or "LED" in name.upper():
                    rows.append({"name": name, "address": address, "rssi": getattr(adv, "rssi", None)})
            self.bridge.devices_found.emit(rows)
            self.bridge.status.emit(f"Quét xong — tìm thấy {len(rows)} thiết bị có tên giống LED.")
        except Exception as e:
            self.bridge.status.emit(f"Lỗi quét BLE: {e}")

    async def connect(self, address, characteristic, protocol):
        if BleakClient is None:
            self.bridge.status.emit("Chưa cài Bleak. Chạy install.bat trước.")
            return
        await self.disconnect()
        self.address = address
        self.characteristic = characteristic.strip()
        self.protocol = protocol
        try:
            self.bridge.status.emit(f"Đang kết nối {address}...")
            self.client = BleakClient(address, timeout=15)
            await self.client.connect()
            if not self.client.is_connected:
                raise RuntimeError("Thiết bị không xác nhận kết nối.")
            try:
                services = self.client.services
                chars = []
                for s in services:
                    for c in s.characteristics:
                        if "write" in c.properties or "write-without-response" in c.properties:
                            chars.append(c.uuid)
                if chars:
                    self.bridge.status.emit("Đã kết nối. Characteristic ghi: " + ", ".join(chars))
                else:
                    self.bridge.status.emit("Đã kết nối nhưng chưa thấy characteristic ghi; kiểm tra UUID.")
            except Exception:
                pass
            self.bridge.connected.emit(True)
        except Exception as e:
            self.client = None
            self.bridge.connected.emit(False)
            self.bridge.status.emit(f"Kết nối thất bại: {e}. Đóng MohuanLED trên điện thoại rồi thử lại.")

    async def disconnect(self):
        if self.client:
            try:
                if self.client.is_connected:
                    await self.client.disconnect()
            except Exception:
                pass
        self.client = None
        self.bridge.connected.emit(False)

    def packet_for_color(self, r, g, b, brightness):
        r = int(r * brightness / 255)
        g = int(g * brightness / 255)
        b = int(b * brightness / 255)
        # Most documented BJ_LED variants use an 8-byte frame on EE01.
        # Some firmware/library variants use EE02 or a different frame; see README.
        if self.protocol == "BJ_LED RGB (69 96 05 02)":
            return bytes([0x69, 0x96, 0x05, 0x02, r, g, b, 0x00])
        # Alternate packet seen in some community implementations.
        return bytes([0x69, 0x96, 0x05, 0x02, r, g, b])

    async def set_color(self, r, g, b, brightness=255):
        if not self.client or not self.client.is_connected:
            self.bridge.color_sent.emit(False, "Chưa kết nối LED.")
            return
        try:
            async with self.lock:
                packet = self.packet_for_color(r, g, b, brightness)
                await self.client.write_gatt_char(self.characteristic, packet, response=False)
                self.last_packet = packet
            self.bridge.color_sent.emit(True, f"Đã gửi RGB({r},{g},{b})")
        except Exception as e:
            self.bridge.color_sent.emit(False, f"Ghi BLE thất bại: {e}")


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("MOHUANLED RGB STUDIO")
        self.resize(1080, 740)
        self.bridge = Bridge()
        self.controller = MohuanController(self.bridge)
        self.loop = asyncio.new_event_loop()
        self.worker = QTimer(self)
        self.worker.timeout.connect(self.pump_asyncio)
        self.worker.start(15)
        self.tasks = set()
        self.current_color = (80, 120, 255)
        self.connected_address = None
        self.effect_name = "Static"
        self.effect_tick = 0
        self.sync_mode = None
        self.last_sync = 0
        self.audio_recorder = None
        self.screen = mss.mss() if mss else None
        self.bridge.devices_found.connect(self.update_devices)
        self.bridge.status.connect(self.set_status)
        self.bridge.connected.connect(self.on_connected)
        self.bridge.color_sent.connect(self.on_color_sent)
        self.build_ui()
        self.apply_theme()

    def pump_asyncio(self):
        self.loop.call_soon(self.loop.stop)
        self.loop.run_forever()

    def spawn(self, coro):
        task = self.loop.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    def build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        main = QHBoxLayout(root)
        left = QVBoxLayout()
        right = QVBoxLayout()
        main.addLayout(left, 1)
        main.addLayout(right, 2)

        title = QLabel("MOHUANLED  /  RGB STUDIO")
        title.setObjectName("title")
        subtitle = QLabel("DESKTOP LIGHTING CONTROL • BLE")
        subtitle.setObjectName("subtitle")
        left.addWidget(title)
        left.addWidget(subtitle)

        device_box = QGroupBox("01  /  BLUETOOTH DEVICE")
        dv = QVBoxLayout(device_box)
        self.scan_btn = QPushButton("SCAN FOR DEVICES")
        self.scan_btn.clicked.connect(lambda: self.spawn(self.controller.scan()))
        dv.addWidget(self.scan_btn)
        self.devices = QListWidget()
        self.devices.setMinimumHeight(140)
        dv.addWidget(self.devices)
        connrow = QHBoxLayout()
        self.connect_btn = QPushButton("CONNECT")
        self.connect_btn.clicked.connect(self.connect_selected)
        self.disconnect_btn = QPushButton("DISCONNECT")
        self.disconnect_btn.clicked.connect(lambda: self.spawn(self.controller.disconnect()))
        connrow.addWidget(self.connect_btn)
        connrow.addWidget(self.disconnect_btn)
        dv.addLayout(connrow)
        left.addWidget(device_box)

        cfg = QGroupBox("BLE ADVANCED")
        cg = QGridLayout(cfg)
        cg.addWidget(QLabel("Write characteristic UUID"), 0, 0)
        self.uuid_edit = QLineEdit(DEFAULT_CHARACTERISTIC)
        cg.addWidget(self.uuid_edit, 0, 1)
        cg.addWidget(QLabel("Packet format"), 1, 0)
        self.protocol_combo = QComboBox()
        self.protocol_combo.addItems(["BJ_LED RGB (69 96 05 02)", "BJ_LED RGB 7-byte (test)"])
        cg.addWidget(self.protocol_combo, 1, 1)
        left.addWidget(cfg)

        self.status_label = QLabel("Status: Ready. Bật LED và nhấn Scan.")
        self.status_label.setWordWrap(True)
        self.status_label.setObjectName("status")
        left.addWidget(self.status_label)
        left.addStretch(1)

        # Color studio
        color_box = QGroupBox("02  /  COLOR STUDIO")
        cv = QVBoxLayout(color_box)
        self.color_preview = QLabel("RGB 080 / 120 / 255")
        self.color_preview.setAlignment(Qt.AlignCenter)
        self.color_preview.setMinimumHeight(82)
        self.color_preview.setObjectName("preview")
        cv.addWidget(self.color_preview)
        colorrow = QHBoxLayout()
        self.pick_btn = QPushButton("CHOOSE COLOR")
        self.pick_btn.clicked.connect(self.pick_color)
        self.send_btn = QPushButton("APPLY COLOR")
        self.send_btn.clicked.connect(self.send_current_color)
        self.off_btn = QPushButton("LIGHTS OFF")
        self.off_btn.clicked.connect(lambda: self.spawn(self.controller.set_color(0, 0, 0, 255)))
        colorrow.addWidget(self.pick_btn)
        colorrow.addWidget(self.send_btn)
        colorrow.addWidget(self.off_btn)
        cv.addLayout(colorrow)
        self.brightness = QSlider(Qt.Horizontal)
        self.brightness.setRange(1, 255)
        self.brightness.setValue(255)
        cv.addWidget(QLabel("Brightness"))
        cv.addWidget(self.brightness)
        right.addWidget(color_box)

        effects = QGroupBox("03  /  EFFECT ENGINE")
        eg = QGridLayout(effects)
        self.effect_combo = QComboBox()
        self.effect_combo.addItems(["Static", "Rainbow", "Breathing", "Color Wave", "Strobe", "Fade"])
        eg.addWidget(QLabel("Effect"), 0, 0)
        eg.addWidget(self.effect_combo, 0, 1)
        self.speed = QSlider(Qt.Horizontal)
        self.speed.setRange(1, 100)
        self.speed.setValue(35)
        eg.addWidget(QLabel("Speed"), 1, 0)
        eg.addWidget(self.speed, 1, 1)
        self.start_effect_btn = QPushButton("START EFFECT")
        self.start_effect_btn.clicked.connect(self.start_effect)
        self.stop_effect_btn = QPushButton("STOP EFFECT")
        self.stop_effect_btn.clicked.connect(self.stop_effect)
        eg.addWidget(self.start_effect_btn, 2, 0)
        eg.addWidget(self.stop_effect_btn, 2, 1)
        right.addWidget(effects)

        sync = QGroupBox("04  /  LIVE SYNC")
        sg = QGridLayout(sync)
        self.screen_btn = QPushButton("SCREEN SYNC")
        self.screen_btn.clicked.connect(self.toggle_screen_sync)
        self.music_btn = QPushButton("MUSIC SYNC")
        self.music_btn.clicked.connect(self.toggle_music_sync)
        self.sync_label = QLabel("Sync: Off")
        self.sync_label.setWordWrap(True)
        sg.addWidget(self.screen_btn, 0, 0)
        sg.addWidget(self.music_btn, 0, 1)
        sg.addWidget(self.sync_label, 1, 0, 1, 2)
        right.addWidget(sync)

        notes = QLabel("NOTE: Effects and sync are generated in software by sending changing RGB values. "
                       "This controls the whole strip as one color; per-zone effects require compatible firmware.")
        notes.setWordWrap(True)
        notes.setObjectName("note")
        right.addWidget(notes)
        right.addStretch(1)

        self.effect_timer = QTimer(self)
        self.effect_timer.timeout.connect(self.effect_step)
        self.effect_timer.start(80)
        self.sync_timer = QTimer(self)
        self.sync_timer.timeout.connect(self.sync_step)
        self.sync_timer.start(120)
        self.refresh_preview()

    def apply_theme(self):
        self.setStyleSheet("""
        QMainWindow, QWidget { background:#10131a; color:#e9edf5; font-family:Segoe UI; font-size:10pt; }
        QLabel#title { font-size:20pt; font-weight:800; color:#8d7cff; }
        QLabel#subtitle { color:#8b95a7; letter-spacing:2px; font-size:9pt; margin-bottom:12px; }
        QGroupBox { border:1px solid #303747; border-radius:10px; margin-top:12px; padding:12px; font-weight:700; color:#a9b4c8; }
        QGroupBox::title { subcontrol-origin:margin; left:12px; padding:0 5px; }
        QPushButton { background:#252b3a; border:1px solid #414b61; border-radius:7px; padding:10px; font-weight:700; }
        QPushButton:hover { background:#39335f; border-color:#8d7cff; }
        QPushButton:pressed { background:#6658d3; }
        QListWidget, QLineEdit, QComboBox { background:#171c26; border:1px solid #303747; border-radius:6px; padding:7px; }
        QSlider::groove:horizontal { height:6px; background:#303747; border-radius:3px; }
        QSlider::handle:horizontal { background:#8d7cff; width:16px; margin:-5px 0; border-radius:8px; }
        QLabel#status { color:#a4e8c0; background:#171c26; padding:10px; border-radius:6px; }
        QLabel#preview { border-radius:9px; border:1px solid #414b61; font-size:15pt; font-weight:800; }
        QLabel#note { color:#8b95a7; padding:8px; }
        """)

    def set_status(self, text):
        self.status_label.setText("Status: " + text)

    def update_devices(self, rows):
        self.devices.clear()
        for row in rows:
            rssi = row["rssi"]
            suffix = f"  |  RSSI {rssi} dBm" if rssi is not None else ""
            self.devices.addItem(f'{row["name"]}  |  {row["address"]}{suffix}')
        if not rows:
            self.status_label.setText("Status: Không thấy thiết bị phù hợp. Bật LED, đóng app điện thoại và thử lại.")

    def connect_selected(self):
        item = self.devices.currentItem()
        if not item:
            QMessageBox.information(self, "Select device", "Hãy quét và chọn BJ_LED_M trước.")
            return
        parts = item.text().split("|")
        address = parts[1].strip().split()[0]
        self.connected_address = address
        self.spawn(self.controller.connect(address, self.uuid_edit.text(), self.protocol_combo.currentText()))

    def on_connected(self, yes):
        self.connect_btn.setEnabled(not yes)
        self.disconnect_btn.setEnabled(yes)
        if yes:
            self.status_label.setText("Status: Đã kết nối BLE. Thử Apply Color.")
        else:
            self.status_label.setText("Status: Chưa kết nối.")

    def on_color_sent(self, ok, message):
        self.status_label.setText(("Status: " if ok else "Status: Lỗi — ") + message)

    def pick_color(self):
        r, g, b = self.current_color
        c = QColorDialog.getColor(QColor(r, g, b), self, "Choose RGB color")
        if c.isValid():
            self.current_color = (c.red(), c.green(), c.blue())
            self.refresh_preview()

    def refresh_preview(self):
        r, g, b = self.current_color
        self.color_preview.setText(f"RGB {r:03d} / {g:03d} / {b:03d}")
        self.color_preview.setStyleSheet(f"background:rgb({r},{g},{b}); color:{'#111' if (r+g+b)>380 else '#fff'};")

    def send_current_color(self):
        self.stop_sync()
        self.stop_effect()
        r, g, b = self.current_color
        self.spawn(self.controller.set_color(r, g, b, self.brightness.value()))

    def start_effect(self):
        self.stop_sync()
        self.effect_name = self.effect_combo.currentText()
        self.effect_tick = 0
        self.status_label.setText(f"Status: Hiệu ứng {self.effect_name} đang chạy.")

    def stop_effect(self):
        self.effect_name = "Static"

    def effect_step(self):
        if self.effect_name == "Static" or self.sync_mode:
            return
        self.effect_tick += 1
        t = self.effect_tick * (101 - self.speed.value()) / 400.0
        if self.effect_name == "Rainbow":
            r, g, b = self.hsv_rgb((t * 0.22) % 1, 1, 1)
        elif self.effect_name == "Breathing":
            v = int(20 + 235 * (0.5 + 0.5 * math.sin(t * 2.5)))
            cr, cg, cb = self.current_color
            r, g, b = cr*v//255, cg*v//255, cb*v//255
        elif self.effect_name == "Color Wave":
            r, g, b = self.hsv_rgb((t * 0.13) % 1, 0.9, 1)
        elif self.effect_name == "Strobe":
            v = 255 if (self.effect_tick % max(2, int(30 / max(1, self.speed.value()/8)))) < 1 else 0
            r, g, b = (self.current_color[0]*v//255, self.current_color[1]*v//255, self.current_color[2]*v//255)
        elif self.effect_name == "Fade":
            r, g, b = self.hsv_rgb((t * 0.08) % 1, 0.75, 0.9)
        else:
            return
        self.spawn(self.controller.set_color(r, g, b, self.brightness.value()))

    @staticmethod
    def hsv_rgb(h, s, v):
        import colorsys
        return tuple(int(x * 255) for x in colorsys.hsv_to_rgb(h, s, v))

    def stop_sync(self):
        self.sync_mode = None
        self.sync_label.setText("Sync: Off")

    def toggle_screen_sync(self):
        if self.sync_mode == "screen":
            self.stop_sync()
        else:
            self.stop_effect()
            self.sync_mode = "screen"
            self.sync_label.setText("Sync: Screen — sampling average desktop color")
            if mss is None or np is None:
                self.sync_label.setText("Thiếu mss/numpy. Chạy install.bat.")

    def toggle_music_sync(self):
        if self.sync_mode == "music":
            self.stop_sync()
        else:
            self.stop_effect()
            if sc is None or np is None:
                self.sync_label.setText("Thiếu soundcard/numpy. Chạy install.bat.")
                return
            self.sync_mode = "music"
            self.sync_label.setText("Sync: Music — thử thu âm từ microphone/loopback mặc định")

    def sync_step(self):
        if not self.sync_mode or time.time() - self.last_sync < 0.12:
            return
        self.last_sync = time.time()
        try:
            if self.sync_mode == "screen" and self.screen and np is not None:
                shot = self.screen.grab(self.screen.monitors[1])
                arr = np.asarray(shot)[:, :, :3]
                # MSS returns BGRA; use robust sampled average, not a saved screenshot.
                small = arr[::max(1, arr.shape[0]//45), ::max(1, arr.shape[1]//80), :]
                b, g, r = [int(x) for x in small.mean(axis=(0, 1))]
                self.spawn(self.controller.set_color(r, g, b, self.brightness.value()))
            elif self.sync_mode == "music" and sc is not None and np is not None:
                # Record short chunks from the default loopback if available, otherwise default microphone.
                speaker = sc.default_speaker()
                mic = sc.get_microphone(id=str(speaker.name), include_loopback=True)
                with mic.recorder(samplerate=22050) as recorder:
                    data = recorder.record(numframes=2048)
                level = float(np.sqrt(np.mean(np.square(data)))) if data.size else 0.0
                # Beat/volume pulse, with current color as the base palette.
                level = min(1.0, level * 9.0)
                gain = 0.12 + 0.88 * level
                r, g, b = self.current_color
                self.spawn(self.controller.set_color(int(r*gain), int(g*gain), int(b*gain), self.brightness.value()))
        except Exception as e:
            self.sync_label.setText(f"Sync tạm dừng/lỗi: {e}")
            self.stop_sync()

    def closeEvent(self, event):
        try:
            self.stop_sync()
            self.loop.run_until_complete(self.controller.disconnect())
            self.loop.stop()
            self.loop.close()
        except Exception:
            pass
        event.accept()


def main():
    app = QApplication(sys.argv)
    app.setFont(QFont("Segoe UI", 10))
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
