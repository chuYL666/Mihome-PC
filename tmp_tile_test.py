import os, sys
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, ".")
from pathlib import Path
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QImage, QPainter, QColor
from app.core.models import DeviceInfo
from app.ui.si_theme import set_theme, set_accent
import app.ui.device_card as dc

app = QApplication(sys.argv)
set_theme("dark"); set_accent("green")
MODELS = ["xiaomi.router.r3p", "xiaomi.wifispeaker.lx06", "mibx5.washer.v7",
          "chuangmi.camera.039a01", "zimi.powerstrip.v2"]
shots = []
for m in MODELS:
    p = Path("icons") / f"{m}.png"
    if not p.is_file():
        print("skip", m); continue
    dev = DeviceInfo(did=m, name=m, model=m, home_name="家", room_name="客厅", online=True)
    c = dc.DeviceCard(dev); c.resize(212, 92)
    c.set_icon(p); c.show(); app.processEvents()
    shots.append(c.grab())
w = max(s.width() for s in shots); h = sum(s.height() for s in shots) + 8 * (len(shots) - 1)
out = QImage(w, h, QImage.Format_ARGB32); out.fill(QColor("#1e1f24"))
pt = QPainter(out); y = 0
for s in shots:
    pt.drawPixmap(0, y, s); y += s.height() + 8
pt.end(); out.save("tmp_tile_cards.png")
print("ok", len(shots))
