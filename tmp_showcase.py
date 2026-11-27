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
from PySide6.QtGui import QFontDatabase, QFont
for f in ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/msyhbd.ttc"):
    QFontDatabase.addApplicationFont(f)
app.setFont(QFont("Microsoft YaHei UI", 10))
set_theme("dark"); set_accent("green")
DEVS = [
    ("xiaomi.router.r3p", "小米路由器 Pro"),
    ("mibx5.washer.v7", "米家滚筒洗衣机"),
    ("xiaomi.wifispeaker.lx06", "小爱音箱 Play"),
    ("miaomiaoce.sensor_ht.t2", "米家温湿度计"),
    ("chuangmi.camera.039a01", "小米智能摄像机"),
    ("dmaker.fan.p28", "米家直流变频塔扇"),
]
CW, CH, GAP = 212, 92, 12
COLS = 2
shots = []
for m, label in DEVS:
    dev = DeviceInfo(did=m, name=label, model=m, home_name="家", room_name="客厅", online=True)
    c = dc.DeviceCard(dev); c.resize(CW, CH)
    p = Path("icons") / f"{m}.png"
    if p.is_file(): c.set_icon(p)
    c.show(); app.processEvents(); shots.append(c.grab())
rows = (len(shots) + COLS - 1) // COLS
W = COLS * CW + (COLS + 1) * GAP
H = rows * CH + (rows + 1) * GAP
out = QImage(W * 2, H * 2, QImage.Format_ARGB32); out.fill(QColor("#1e1f24"))
pt = QPainter(out); pt.setRenderHint(QPainter.Antialiasing)
for i, s in enumerate(shots):
    r, col = divmod(i, COLS)
    x = (GAP + col * (CW + GAP)) * 2
    y = (GAP + r * (CH + GAP)) * 2
    pt.drawPixmap(x, y, s)
pt.end(); out.save("tmp_showcase.png")
print("ok", out.size())
