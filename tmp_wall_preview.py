import os, sys
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, ".")
from pathlib import Path
from unittest.mock import patch
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QImage, QFontDatabase, QFont
from app.ui.camera_wall import CameraWallDialog, _CameraTile
from app.ui.si_theme import set_theme, set_accent

app = QApplication(sys.argv)
for f in ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/msyhbd.ttc"):
    QFontDatabase.addApplicationFont(f)
app.setFont(QFont("Microsoft YaHei UI", 10))
set_theme("dark"); set_accent("green")

class Dev:
    def __init__(self, did, model, name):
        self.did, self.model, self.name = did, model, name
        self.online, self.room_name = True, "院子"
class Jobs:
    def submit(self, fn, *a, **k): return fn()
class Svc:
    def prepare_camera_source(self, d): return {"did": d.did}
    def read_prop_raw(self, *a): return True

devs = [Dev("1", "chuangmi.camera.039a01", "冰箱"),
        Dev("2", "chuangmi.camera.039a01", "二楼"),
        Dev("3", "chuangmi.camera.ip029a", "出租屋"),
        Dev("4", "chuangmi.camera.039a01", "堂屋"),
        Dev("5", "isa.camera.hlmax", "鸡圈菜地双摄")]

probe = Path(r"C:/Users/储/WorkBuddy/2026-10-23-22-11-50/lens-probe")
ch0 = QImage(str(probe / "dual-ch0.png"))
ch1 = QImage(str(probe / "dual-ch1.png"))
blank = QImage(640, 360, QImage.Format_RGB32)
from PySide6.QtGui import QPainter, QColor
p = QPainter(blank); blank.fill(QColor("#3d4757"))
p.setPen(QColor("#9fa8b5")); p.drawText(blank.rect(), 0x84, "其他摄像头画面"); p.end()

with patch.object(_CameraTile, "start", lambda self: None):
    wall = CameraWallDialog(Svc(), Jobs(), devs)
    wall.resize(1120, 720)
    wall.show(); app.processEvents()
    for key, tile in wall._tiles.items():
        img = ch0 if key == "5" else (ch1 if key == "5#1" else blank)
        tile._canvas.set_frame(img)
    app.processEvents()
    wall.grab().save("tmp_wall_after.png")
    print("格子数", len(wall._tiles), "标题", wall._title.text())
    wall.close()
