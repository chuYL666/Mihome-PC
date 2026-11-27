import os, sys
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, ".")
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QImage, QPainter, QColor, QFont
from app.ui.camera_canvas import CameraCanvas

app = QApplication(sys.argv)
img = QImage(640, 360, QImage.Format_RGB32)
img.fill(QColor("#243447"))
p = QPainter(img)
p.setBrush(QColor("#e74c3c")); p.drawRect(0, 0, 60, 60); p.drawRect(580, 300, 60, 60)
p.setBrush(QColor("#2ecc71")); p.drawRect(580, 0, 60, 60); p.drawRect(0, 300, 60, 60)
p.setPen(QColor("white")); p.setFont(QFont("Arial", 22)); p.drawText(img.rect(), 0x84, "16:9 完整画面 / 四角标记")
p.end()

shots = []
for fill in (True, False):
    c = CameraCanvas(); c.resize(340, 150); c.set_fill(fill); c.set_frame(img)
    c.show(); app.processEvents(); shots.append(c.grab())

w = shots[0].width() * 2 + 30; h = shots[0].height() + 40
out = QImage(w, h, QImage.Format_RGB32); out.fill(QColor("#1e1f24"))
p = QPainter(out)
p.setPen(QColor("#cbd1da")); p.setFont(QFont("Arial", 12))
p.drawText(10, 20, "旧：铺满 + 居中裁剪（两侧被切）")
p.drawText(shots[0].width() + 40, 20, "新：完整显示（四角标记都在）")
p.drawPixmap(10, 30, shots[0]); p.drawPixmap(shots[0].width() + 40, 30, shots[1])
p.end(); out.save("tmp_crop_check.png")
print("ok", out.size())
