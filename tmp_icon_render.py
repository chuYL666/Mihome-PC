import os, sys
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, ".")
from pathlib import Path
from PySide6.QtWidgets import QApplication
from app.core.models import DeviceInfo
from app.ui.device_card import DeviceCard

app = QApplication(sys.argv)
models = ["chuangmi.camera.039a01", "mibx5.washer.v7", "miaomiaoce.sensor_ht.t2",
          "xiaomi.airc.h64h00", "dmaker.fan.p28"]
cards = []
for m in models:
    dev = DeviceInfo(did=m, name=m.split('.')[0], model=m, home_name="家", room_name="客厅", online=True)
    c = DeviceCard(dev)
    c.resize(212, 92)
    c.set_icon(Path("icons") / f"{m}.png")
    c.show()
    cards.append(c)
app.processEvents()
for i, c in enumerate(cards):
    pm = c.grab()
    pm.save(f"tmp_card_{i}.png")
    print("saved", models[i], pm.size(), pm.devicePixelRatio())
