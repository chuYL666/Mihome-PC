import os, sys
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, ".")
from pathlib import Path
from PySide6.QtWidgets import QApplication
from app.core.models import DeviceInfo
from app.ui.si_theme import set_theme, set_accent
from app.ui.device_card import DeviceCard, _ICON_RADIUS

app = QApplication(sys.argv)
set_theme("dark"); set_accent("green")

MODELS = [("chuangmi.camera.039a01", "小米智能摄像机"),
          ("mibx5.washer.v7", "米家滚筒洗衣机"),
          ("miaomiaoce.sensor_ht.t2", "米家温湿度计"),
          ("xiaomi.airc.h64x", "米家空调"),
          ("dmaker.fan.p28", "米家直流变频塔扇")]

VARIANTS = {
    "dark_tile": None,
    "light_tile": "#eef1f6",
}

for name, tile in VARIANTS.items():
    shots = []
    for m, label in MODELS:
        dev = DeviceInfo(did=m, name=label, model=m, home_name="家", room_name="客厅", online=True)
        c = DeviceCard(dev)
        c.resize(212, 92)
        p = Path("icons") / f"{m}.png"
        if p.is_file():
            c.set_icon(p)
        if tile:
            c._icon_label.setStyleSheet(
                f"background: {tile}; border-radius: {_ICON_RADIUS}px; border: none;")
        c.show(); app.processEvents()
        shots.append(c.grab())
    print(name, "ok", [s.size() for s in shots[:1]])
    # 纵向拼接
    from PySide6.QtGui import QImage, QPainter, QColor
    w = max(s.width() for s in shots); h = sum(s.height() for s in shots) + 8 * (len(shots)-1)
    out = QImage(w, h, QImage.Format_ARGB32)
    out.fill(QColor("#1e1f24"))
    pt = QPainter(out); y = 0
    for s in shots:
        pt.drawPixmap(0, y, s); y += s.height() + 8
    pt.end()
    out.save(f"tmp_cmp_{name}.png")
