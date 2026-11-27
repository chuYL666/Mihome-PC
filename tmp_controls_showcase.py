import os, sys
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, ".")
from pathlib import Path
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QImage, QPainter, QColor, QFontDatabase, QFont
from PySide6.QtCore import Qt
from app.ui.camera_canvas import CameraCanvas
from app.core import camera_ptz
from app.ui.si_theme import set_theme, set_accent

app = QApplication(sys.argv)
for f in ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/msyhbd.ttc"):
    QFontDatabase.addApplicationFont(f)
app.setFont(QFont("Microsoft YaHei UI", 10))
set_theme("dark"); set_accent("green")

probe = Path(r"C:/Users/储/WorkBuddy/2026-10-23-22-11-50/lens-probe/dual-ch0.png")
frame = QImage(str(probe))
if frame.isNull():
    frame = QImage(640, 360, QImage.Format_RGB32); frame.fill(QColor("#34465c"))

def card(w, h, compact, talking):
    c = CameraCanvas(compact_ptz=compact)
    c.resize(w, h)
    c.set_ptz_capability(camera_ptz.STEP)
    c.set_talk_supported(True)
    c.set_controls_enabled(True)
    c.set_talking(talking)
    c.set_frame(frame)
    c.show(); app.processEvents()
    return c.grab()

big = card(740, 370, False, False)
big_talk = card(740, 370, False, True)
small = card(300, 170, True, False)

W = 1520; H = 920
out = QImage(W, H, QImage.Format_RGB32); out.fill(QColor("#1e1f24"))
p = QPainter(out); p.setRenderHint(QPainter.Antialiasing)
p.setPen(QColor("#e8ecf3")); f = QFont("Microsoft YaHei UI", 13, QFont.Bold); p.setFont(f)
p.drawText(24, 34, "单摄像头窗口 · 云台方向盘 + 通话按钮")
f2 = QFont("Microsoft YaHei UI", 10); p.setFont(f2); p.setPen(QColor("#9aa3b2"))
p.drawText(24, 54, "右侧半透明方向盘可按住连发；底部麦克风开启对讲（绿色为通话中）")
p.drawPixmap(24, 68, big)
p.setPen(QColor("#e8ecf3")); p.setFont(f)
p.drawText(24, 470, "通话中（麦克风高亮）")
p.drawPixmap(24, 486, big_talk)
p.setPen(QColor("#e8ecf3")); p.setFont(f)
p.drawText(790, 34, "摄像头墙单格 · 紧凑方向盘")
p.setPen(QColor("#9aa3b2")); p.setFont(f2)
p.drawText(790, 54, "格子内可直接转向；通话统一用「大窗」，避免多路抢麦克风")
p.drawPixmap(790, 68, small)
p.setPen(QColor("#8b93a1")); p.setFont(f2)
p.drawText(790, 262, "云台机型自动识别：039a01 / ip029a / hlmax / 029a02 ...")
p.drawText(790, 284, "固定镜头或枪机副镜头不显示方向盘")
p.end()
out.save("功能补全-云台与通话.png")
print("ok", out.size())
