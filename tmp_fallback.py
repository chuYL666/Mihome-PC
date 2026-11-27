import os, sys
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, ".")
from PySide6.QtWidgets import QApplication
from app.core.models import DeviceInfo
from app.ui.device_card import DeviceCard

app = QApplication(sys.argv)
for i, m in enumerate(["mibx5.washer.v7", "xiaomi.wifispeaker.x08c", "dmaker.fan.p28"]):
    dev = DeviceInfo(did=m, name="测试设备", model=m, home_name="家", room_name="客厅", online=True)
    c = DeviceCard(dev); c.resize(212, 92); c.show(); app.processEvents()
    c.grab().save(f"tmp_fb_{i}.png")
print("ok")
