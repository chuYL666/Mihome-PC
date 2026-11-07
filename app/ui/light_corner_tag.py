# SPDX-License-Identifier: GPL-3.0-or-later
"""Small rainbow LED label for lights with adjustable controls."""
from PySide6.QtGui import QBrush, QColor, QLinearGradient
from app.ui.tray.camera_corner_tag import CameraCornerTag


class LightCornerTag(CameraCornerTag):
    def __init__(self, parent, *, corner_radius=14, width=32, height=14, font_size=6):
        super().__init__(parent, corner_radius=corner_radius, width=width, height=height, font_size=font_size)
        self._text = 'LED'
        self.setObjectName('lightCornerTag')
        self.setAccessibleName('可调灯光 LED')

    def _background(self):
        gradient = QLinearGradient(0, 0, self.width(), self.height())
        for stop, color in [(0, '#854bb8'), (.25, '#3a65c7'), (.5, '#168d87'),
                            (.75, '#b57528'), (1, '#bd456e')]:
            gradient.setColorAt(stop, QColor(color))
        return QBrush(gradient)
