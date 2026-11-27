import os, sys
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, ".")
from pathlib import Path
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QImage, QPainter, QColor, QFont
from PySide6.QtCore import Qt
from app.core.models import DeviceInfo
from app.ui.si_theme import set_theme, set_accent
from app.ui import device_card as dc

app = QApplication(sys.argv)
set_theme("dark"); set_accent("green")
MODELS = ["chuangmi.camera.039a01", "mibx5.washer.v7", "miaomiaoce.sensor_ht.t2",
          "xiaomi.airc.h64x", "dmaker.fan.p28"]

def render(inner, sharpen, tile="#3c3f49"):
    imgs = []
    dc._ICON_CACHE.clear()
    old_inner = dc._ICON_INNER
    dc._ICON_INNER = inner
    orig = dc._sharpen
    if not sharpen:
        dc._sharpen = lambda im, amount=0: im
    try:
        for m in MODELS:
            dev = DeviceInfo(did=m, name=m, model=m, home_name="家", room_name="客厅", online=True)
            c = dc.DeviceCard(dev); c.resize(212, 92)
            p = Path("icons") / f"{m}.png"
            if p.is_file(): c.set_icon(p)
            c._icon_label.setStyleSheet(f"background: {tile}; border-radius: 13px; border: none;")
            c.show(); app.processEvents()
            pm = c.grab()
            im = pm.toImage()
            # 图标底块：x=24,y 居中，尺寸 ~ inner*dpr
            dpr = pm.devicePixelRatio()
            x = int((12 + (46 - inner) / 2 - 5) * dpr); y = int((92 - inner) / 2 - 5) * int(dpr)
            side = int((inner + 10) * dpr)
            imgs.append(im.copy(x, y, side, side))
    finally:
        dc._ICON_INNER = old_inner
        dc._sharpen = orig
    return imgs

old = render(30, False)  # 旧版：36 槽 / 30 内缩、单步缩放、无锐化
new = render(40, True)   # 新版：46 槽 / 40 内缩、分级缩放、轻锐化
ZOOM = 4
rows = []
for l, r in zip(old, new):
    l2 = l.scaled(l.width()*ZOOM, l.height()*ZOOM, Qt.KeepAspectRatio, Qt.FastTransformation)
    r2 = r.scaled(r.width()*ZOOM, r.height()*ZOOM, Qt.KeepAspectRatio, Qt.FastTransformation)
    rows.append((l2, r2))
W = sum(max(a.width(), b.width()) for a, b in rows) + 20 * len(rows)
H = max(max(a.height(), b.height()) for a, b in rows) * 2 + 60
out = QImage(W, H, QImage.Format_ARGB32); out.fill(QColor("#1e1f24"))
pt = QPainter(out); pt.setRenderHint(QPainter.Antialiasing)
pt.setPen(QColor("#9aa0aa")); f = QFont("Microsoft YaHei UI", 10); pt.setFont(f)
x = 0
for (a, b) in rows:
    pt.drawText(x, 22, "旧"); pt.drawImage(x, 32, a)
    pt.drawText(x, 40 + a.height(), "新"); pt.drawImage(x, 50 + a.height(), b)
    x += max(a.width(), b.width()) + 20
pt.end()
out.save("tmp_icon_zoom.png")
print("done", out.size())
