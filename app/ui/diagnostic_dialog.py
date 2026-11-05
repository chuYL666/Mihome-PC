# SPDX-License-Identifier: GPL-3.0-or-later
"""Preview and explicitly copy a single-device report for GitHub Issues."""
import shiboken6
from PySide6.QtCore import QTimer, Qt, QUrl
from PySide6.QtGui import QDesktopServices, QIcon
from PySide6.QtWidgets import (QApplication, QComboBox, QDialog, QHBoxLayout, QLabel,
                              QPlainTextEdit, QPushButton, QVBoxLayout)
from app import resource_path
from app.ui.si_theme import SiColors


class DiagnosticDialog(QDialog):
    def __init__(self, service, jobs, devices, parent=None, observation=None):
        super().__init__(parent)
        self.setWindowTitle('设备诊断报告 - MijiaDeck')
        self.setWindowIcon(QIcon(str(resource_path('app/ui/icon.png'))))
        self.setWindowModality(Qt.NonModal)
        self.resize(700, 570)
        self._service, self._jobs = service, jobs
        self._devices = list(devices)
        self._observation = observation
        self._generation = 0
        self._closed = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)
        self.selector = QComboBox()
        for device in self._devices:
            self.selector.addItem(f'{device.name} · {device.model}')
        layout.addWidget(self.selector)
        note = QLabel('先复现问题，再生成报告。复制后粘贴到 GitHub Issue，并补充操作步骤和实际现象。\n'
                      '仅采集所选设备的能力定义及连接诊断；不包含账号凭证、设备名称、房间、画面或当前传感器读数。')
        note.setWordWrap(True)
        layout.addWidget(note)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        layout.addWidget(self.preview, 1)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        row = QHBoxLayout()
        self.generate = QPushButton('重新生成')
        self.copy = QPushButton('复制诊断报告')
        self.copy.setEnabled(False)
        feedback = QPushButton('打开反馈页面')
        close = QPushButton('关闭')
        row.addWidget(self.generate)
        row.addWidget(self.copy)
        row.addStretch()
        row.addWidget(feedback)
        row.addWidget(close)
        layout.addLayout(row)
        self.generate.clicked.connect(self._generate)
        self.copy.clicked.connect(self._copy)
        feedback.clicked.connect(lambda: QDesktopServices.openUrl(QUrl('https://github.com/Songmanual/MijiaDeck/issues/new')))
        close.clicked.connect(self.close)
        self.selector.currentIndexChanged.connect(self._selection_changed)
        self.setStyleSheet(f'QDialog {{ background: {SiColors.WINDOW_BG}; color: {SiColors.TEXT_PRIMARY}; }}'
            f'QLabel {{ color: {SiColors.TEXT_PRIMARY}; }}'
            f'QPlainTextEdit, QComboBox {{ background: {SiColors.SURFACE}; color: {SiColors.TEXT_PRIMARY}; '
            f'border: 1px solid {SiColors.LINE}; border-radius: 8px; padding: 8px; }}'
            f'QPushButton {{ background: {SiColors.SURFACE}; color: {SiColors.TEXT_PRIMARY}; border: none; '
            f'border-radius: 8px; padding: 9px 12px; }}'
            f'QPushButton:disabled {{ color: {SiColors.TEXT_DISABLED}; }}')
        self.generate.setEnabled(bool(self._devices))
        QTimer.singleShot(0, self, self._generate)

    def _selection_changed(self):
        self._generation += 1
        self.preview.clear()
        self.copy.setEnabled(False)
        self.copy.setText('复制诊断报告')
        self.status.setText('点击“重新生成”获取所选设备的报告。')
        self.generate.setEnabled(bool(self._devices))

    def _generate(self):
        if self._closed or not self._devices:
            self.status.setText('当前没有可诊断的设备，请先登录并刷新设备。')
            return
        self._generation += 1
        generation = self._generation
        device = self._devices[self.selector.currentIndex()]
        observed = self._observation() if callable(self._observation) else None
        self.copy.setEnabled(False)
        self.copy.setText('复制诊断报告')
        self.preview.clear()
        self.generate.setEnabled(False)
        self.status.setText('正在获取设备能力定义，不会操作设备…')
        def alive():
            return shiboken6.isValid(self) and not self._closed and generation == self._generation
        def success(report):
            if alive():
                self.generate.setEnabled(True)
                self.preview.setPlainText(report)
                self.copy.setEnabled(True)
                self.status.setText('报告已生成。能力定义不代表功能已支持；未尝试的连接不会被记为成功。')
        def failure(_):
            if alive():
                self.generate.setEnabled(True)
                self.status.setText('报告生成失败，请重试。未复制任何内容。')
        self._jobs.submit(lambda: self._service.device_diagnostic_report(device, observed),
                          on_success=success, on_error=failure)

    def _copy(self):
        if self.copy.isEnabled() and self.preview.toPlainText():
            QApplication.clipboard().setText(self.preview.toPlainText())
            self.copy.setText('已复制')

    def done(self, result):
        self._closed = True
        self._generation += 1
        super().done(result)


def show_device_diagnostics(owner, service, jobs, devices, observation=None):
    # Keep a Python owner while a nonmodal report window is open.
    current = getattr(owner, '_diagnostic_window', None)
    if current is not None and shiboken6.isValid(current):
        current.show()
        current.raise_()
        current.activateWindow()
        return current
    dialog = DiagnosticDialog(service, jobs, devices, owner, observation)
    owner._diagnostic_window = dialog
    dialog.finished.connect(lambda _: setattr(owner, '_diagnostic_window', None))
    dialog.finished.connect(dialog.deleteLater)
    dialog.show()
    return dialog
