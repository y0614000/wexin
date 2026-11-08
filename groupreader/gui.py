from __future__ import annotations

from datetime import date, datetime
import json
from pathlib import Path
import platform
import re
import sys
import threading
import uuid
from urllib.parse import urlsplit

from PySide6.QtCore import Qt, QDate, QRectF, QStandardPaths, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QFont, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QHBoxLayout, QVBoxLayout, QLabel,
    QLineEdit, QPushButton, QComboBox, QSpinBox, QDoubleSpinBox, QFormLayout,
    QFrame, QTableWidget, QTableWidgetItem, QHeaderView, QPlainTextEdit,
    QTabWidget, QSplitter, QMessageBox, QFileDialog, QDateEdit, QCheckBox,
    QAbstractItemView, QScrollArea,
)

from .model import Message, markdown
from . import WINDOWS_BUILD
from .native import NativeError
from .storage import SessionStore
from .worker import Events, capture
from .deepseek import (
    DEFAULT_BASE_URL, DEFAULT_MODEL, DeepSeekClient, analyze, default_config,
)


STYLE = """
QWidget { color: #20332f; font-size: 13px; }
QWidget#workspace { background: #f2f6f4; }
QFrame#card { background: #ffffff; border: 1px solid #dce6e1; border-radius: 12px; }
QFrame#guide { background: #e7f1ec; border: 1px solid #dce9e2; border-radius: 10px; }
QLabel#title { font-size: 25px; font-weight: 700; }
QLabel#heading { font-size: 16px; font-weight: 700; }
QLabel#field { font-size: 12px; font-weight: 600; color: #40574f; }
QLabel#muted { color: #526b62; font-size: 12px; }
QLabel#badge { background: #e6f2ed; color: #245f4f; border-radius: 10px;
               padding: 4px 10px; font-size: 12px; font-weight: 600; }
QTabWidget::pane { border: 0; }
QTabBar::tab { color: #506760; padding: 8px 17px; margin: 0 6px 8px 0;
               border: 1px solid transparent; border-radius: 8px; font-weight: 600; }
QTabBar::tab:hover { background: #e9f0ed; }
QTabBar::tab:selected { background: #ffffff; color: #195c49; border-color: #d9e5df; }
QTabBar::tab:focus { border-color: #5a9685; }
QScrollArea, QWidget#rail { background: transparent; }
QLineEdit, QComboBox, QAbstractSpinBox, QPlainTextEdit {
    background: #ffffff; border: 1px solid #d5e1db; border-radius: 7px;
    padding: 5px 8px; placeholder-text-color: #60766d;
    selection-background-color: #cbe8dd; selection-color: #163b30;
}
QLineEdit:focus, QComboBox:focus, QAbstractSpinBox:focus, QPlainTextEdit:focus {
    border-color: #568f7e;
}
QLineEdit:disabled, QComboBox:disabled, QAbstractSpinBox:disabled {
    background: #f3f6f4; color: #677970;
}
QComboBox QAbstractItemView {
    background: #ffffff; border: 1px solid #d5e1db;
    selection-background-color: #dcefe6; selection-color: #19523f;
}
QCheckBox { spacing: 6px; color: #40574f; }
QPushButton {
    background: #ffffff; border: 1px solid #d5e1db; border-radius: 7px;
    padding: 6px 10px; font-weight: 600;
}
QPushButton:hover { background: #f0f7f3; border-color: #97b8ab; }
QPushButton:pressed { background: #e4f0ea; }
QPushButton:focus { border-color: #568f7e; }
QPushButton#primary { background: #17634f; color: #ffffff; border-color: #17634f; }
QPushButton#primary:hover { background: #125641; }
QPushButton#primary:pressed { background: #0f4738; }
QPushButton#primary:focus { border-color: #74bba4; }
QPushButton#danger { color: #9a413f; }
QPushButton#quiet { font-size: 12px; }
QPushButton:disabled, QPushButton#primary:disabled, QPushButton#danger:disabled {
    color: #677970; background: #f0f4f2; border-color: #e4ebe7;
}
QTableWidget {
    background: #ffffff; alternate-background-color: #f5f8f6;
    border: 1px solid #e1e9e5; border-radius: 7px;
    selection-background-color: #d9eee5; selection-color: #173d31;
}
QTableWidget::item { padding: 0 8px; }
QHeaderView::section { background: #edf3f0; border: 0; border-bottom: 1px solid #e1e9e5;
                       color: #40574f; padding: 7px 8px; font-weight: 600; }
QPlainTextEdit#document { background: #f9fbfa; padding: 8px 10px; }
QSplitter::handle { background: #ffffff; }
QSplitter::handle:hover { background: #dcece5; }
QScrollBar:vertical { width: 8px; background: transparent; }
QScrollBar::handle:vertical { min-height: 24px; border-radius: 4px; background: #bdcfc6; }
QScrollBar::handle:vertical:hover { background: #8dab9d; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QToolTip { background: #ffffff; color: #20332f; border: 1px solid #c4d7ce; padding: 4px; }
"""


def label(text, style):
    widget = QLabel(text)
    widget.setObjectName(style)
    return widget


def panel(style="card", padding=14, spacing=8):
    frame = QFrame()
    frame.setObjectName(style)
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(padding, padding, padding, padding)
    layout.setSpacing(spacing)
    return frame, layout


def scroll_rail(content, width):
    content.setObjectName("rail")
    scroll = QScrollArea()
    scroll.setWidget(content)
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    scroll.setFixedWidth(width)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    return scroll


def chat_mark(size, background, foreground):
    pixmap = QPixmap(size * 2, size * 2)
    pixmap.setDevicePixelRatio(2)
    pixmap.fill(Qt.GlobalColor.transparent)
    with QPainter(pixmap) as painter:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.scale(size / 44, size / 44)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(background))
        painter.drawRoundedRect(QRectF(0, 0, 44, 44), 12, 12)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        pen = QPen(QColor(foreground))
        pen.setWidthF(1.6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawRoundedRect(QRectF(11, 12, 22, 16), 4, 4)
        painter.drawLine(16, 28, 16, 32)
        painter.drawLine(16, 32, 21, 28)
        painter.drawLine(16, 18, 28, 18)
        painter.drawLine(16, 22, 24, 22)
    return pixmap


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        suffix = " · " + WINDOWS_BUILD if platform.system() == "Windows" else ""
        self.setWindowTitle("群聊记录助手 · Mac / Windows" + suffix)
        self.resize(1180, 820)
        self.setMinimumSize(960, 680)
        self.data_dir = Path(QStandardPaths.writableLocation(QStandardPaths.AppLocalDataLocation))
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.messages: list[Message] = []
        self.visible_messages: list[Message] = []
        self.session_path: Path | None = None
        self.record_group = ""
        self.stop_event = threading.Event()
        self.busy = False
        self.ai_result = ""
        self.events = Events()
        self.events.status.connect(self.set_status)
        self.events.messages.connect(self.set_messages)
        self.events.finished.connect(self.finished)
        self.events.failure.connect(self.failed)
        self.events.diagnostic_ready.connect(self.save_diagnostic)
        self.events.ai_ready.connect(self.show_ai)
        central = QWidget()
        central.setObjectName("workspace")
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(22, 16, 22, 12)
        outer.setSpacing(10)
        header = QHBoxLayout()
        header.setSpacing(12)
        icon = QLabel()
        icon.setPixmap(chat_mark(44, "#17634f", "#ffffff"))
        header.addWidget(icon)
        title = QVBoxLayout()
        title.setSpacing(1)
        title.addWidget(label("群聊记录助手", "title"))
        title.addWidget(label("企业微信会话采集与整理", "muted"))
        header.addLayout(title)
        header.addStretch()
        header.addWidget(label(WINDOWS_BUILD if platform.system() == "Windows" else "本地工作区", "badge"), alignment=Qt.AlignmentFlag.AlignVCenter)
        outer.addLayout(header)
        self.tabs = QTabWidget()
        outer.addWidget(self.tabs, 1)
        self.build_capture()
        self.build_ai()
        status_bar = QHBoxLayout()
        status_bar.setSpacing(8)
        status_bar.addWidget(label("状态", "badge"))
        self.status = label("就绪。先在企业微信打开目标群和“查找聊天记录 → 全部”。", "muted")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        status_bar.addWidget(self.status, 1)
        outer.addLayout(status_bar)
        self.restore_config()

    def build_capture(self):
        page = QWidget()
        layout = QHBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        sidebar = QWidget()
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(0, 0, 2, 0)
        side.setSpacing(9)
        settings, controls = panel()
        controls.addWidget(label("读取设置", "heading"))
        form = QFormLayout()
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(6)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.group = QLineEdit()
        self.group.setPlaceholderText("输入企业微信中的完整群名")
        self.reference = QDateEdit(QDate.currentDate())
        self.reference.setCalendarPopup(True)
        self.reference.setDisplayFormat("yyyy-MM-dd")
        self.reference.setToolTip("用于判断只显示月/日的消息年份。今天、昨天等按实际读取日期计算。")
        self.direction = QComboBox()
        self.direction.addItem("向前读取更早消息", "older")
        self.direction.addItem("向后读取更新消息", "newer")
        self.step = QSpinBox()
        self.step.setRange(60, 600)
        self.step.setValue(240)
        self.step.setSuffix(" px")
        self.step.setToolTip("Windows 按消息行的实际位移控制滚动；必要时缩小步长，保留两条重叠消息用于继续读取。")
        self.interval = QDoubleSpinBox()
        self.interval.setRange(.5, 5)
        self.interval.setValue(1.0)
        self.interval.setSingleStep(.2)
        self.interval.setSuffix(" 秒")
        self.pages = QSpinBox()
        self.pages.setRange(1, 10000)
        self.pages.setValue(1000)
        group_label = label("目标群名", "field")
        group_label.setBuddy(self.group)
        controls.addWidget(group_label)
        controls.addWidget(self.group)
        for name, widget in (("时间参考日", self.reference), ("读取方向", self.direction)):
            field_label = label(name, "field")
            field_label.setBuddy(widget)
            form.addRow(field_label, widget)
        controls.addLayout(form)
        tuning = QHBoxLayout()
        tuning.setSpacing(7)
        for name, widget in [("滚动步长", self.step), ("每屏间隔", self.interval), ("屏数上限", self.pages)]:
            column = QVBoxLayout()
            column.setSpacing(4)
            field_label = label(name, "field")
            field_label.setBuddy(widget)
            column.addWidget(field_label)
            column.addWidget(widget)
            tuning.addLayout(column, 1)
        controls.addLayout(tuning)
        self.auto_recover = QCheckBox("交界歧义时自动调整并继续")
        self.auto_recover.setChecked(True)
        self.auto_recover.setToolTip(
            "仅在两屏交界无法判断、画面没有重叠或界面未移动时启用。"
            "填写 API Key 后由 AI 选择反向微调或缩小步长；未填写时使用本地规则。"
            "发送的只有故障类型、步长、位置和可见行数，不含聊天正文或群名。")
        controls.addWidget(self.auto_recover)
        self.start = QPushButton("开始 / 继续读取")
        self.start.setObjectName("primary")
        self.start.clicked.connect(lambda: self.begin())
        self.once = QPushButton("仅读取当前屏")
        self.once.clicked.connect(lambda: self.begin(one_frame=True))
        self.pause = QPushButton("暂停")
        self.pause.setObjectName("danger")
        self.pause.setEnabled(False)
        self.pause.clicked.connect(self.stop_event.set)
        controls.addWidget(self.start)
        read_actions = QHBoxLayout()
        read_actions.addWidget(self.once, 1)
        read_actions.addWidget(self.pause, 1)
        controls.addLayout(read_actions)
        side.addWidget(settings)

        guide, guide_body = panel("guide", padding=11, spacing=4)
        guide_body.addWidget(label("读取前准备", "field"))
        steps = label("在企业微信打开目标群，进入“查找聊天记录 → 全部”，清空筛选并停在读取起点。采集时请暂时不要操作鼠标。", "muted")
        steps.setWordWrap(True)
        guide_body.addWidget(steps)
        side.addWidget(guide)

        tools, tools_body = panel(padding=11, spacing=6)
        tools_body.addWidget(label("会话与诊断", "field"))
        row = QHBoxLayout()
        self.demo = QPushButton("载入演示")
        self.demo.clicked.connect(self.load_demo)
        self.reset = QPushButton("新建会话")
        self.reset.clicked.connect(self.new_session)
        row.addWidget(self.demo, 1)
        row.addWidget(self.reset, 1)
        tools_body.addLayout(row)
        self.load = QPushButton("恢复本地会话…")
        self.load.clicked.connect(self.load_session)
        self.diag = QPushButton("导出界面结构诊断…")
        self.diag.clicked.connect(lambda: self.begin(diagnose=True))
        utilities = QHBoxLayout()
        for button in [self.load, self.diag]:
            button.setObjectName("quiet")
            utilities.addWidget(button, 1)
        tools_body.addLayout(utilities)
        permission = QPushButton("Mac 辅助功能设置")
        permission.setObjectName("quiet")
        permission.setVisible(platform.system() == "Darwin")
        permission.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(
            "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility")))
        tools_body.addWidget(permission)
        side.addWidget(tools)
        side.addStretch()
        layout.addWidget(scroll_rail(sidebar, 326))

        records, right = panel(padding=14, spacing=9)
        header = QHBoxLayout()
        header.addWidget(label("消息记录", "heading"))
        header.addStretch()
        self.count = label("0 条成员消息", "badge")
        header.addWidget(self.count)
        right.addLayout(header)
        filter_row = QHBoxLayout()
        filter_row.setSpacing(7)
        filter_row.addStretch()
        self.use_range = QCheckBox("筛选导出日期")
        self.use_range.toggled.connect(self.refresh_table)
        self.from_date = QDateEdit(QDate.currentDate().addMonths(-1))
        self.to_date = QDateEdit(QDate.currentDate())
        for widget in [self.from_date, self.to_date]:
            widget.setCalendarPopup(True)
            widget.setDisplayFormat("yyyy-MM-dd")
            widget.dateChanged.connect(self.refresh_table)
        filter_row.addWidget(self.use_range)
        filter_row.addWidget(self.from_date)
        filter_row.addWidget(label("至", "muted"))
        filter_row.addWidget(self.to_date)
        right.addLayout(filter_row)
        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.setHandleWidth(7)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["发送时间", "发送者", "类型", "消息预览"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(34)
        table_header = self.table.horizontalHeader()
        table_header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        table_header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(0, 145)
        self.table.setColumnWidth(1, 112)
        self.table.setColumnWidth(2, 64)
        self.table.itemSelectionChanged.connect(self.preview_selected)
        empty_layout = QVBoxLayout(self.table.viewport())
        self.empty_state = QWidget()
        self.empty_state.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        empty = QVBoxLayout(self.empty_state)
        empty.setSpacing(5)
        empty_icon = QLabel()
        empty_icon.setPixmap(chat_mark(40, "#e6f2ed", "#276b57"))
        empty.addWidget(empty_icon, alignment=Qt.AlignmentFlag.AlignHCenter)
        empty.addWidget(label("暂无消息", "heading"), alignment=Qt.AlignmentFlag.AlignHCenter)
        empty.addWidget(label("读取群聊、恢复会话或载入演示后，消息会显示在这里。", "muted"),
                        alignment=Qt.AlignmentFlag.AlignHCenter)
        empty_layout.addWidget(self.empty_state, alignment=Qt.AlignmentFlag.AlignCenter)

        preview_box = QWidget()
        preview_layout = QVBoxLayout(preview_box)
        preview_layout.setContentsMargins(0, 0, 0, 0)
        preview_layout.setSpacing(5)
        preview_layout.addWidget(label("消息原文", "field"))
        self.preview = QPlainTextEdit()
        self.preview.setObjectName("document")
        self.preview.setReadOnly(True)
        self.preview.setPlaceholderText("选中一条消息，查看完整原文和引用内容。")
        preview_layout.addWidget(self.preview)
        splitter.addWidget(self.table)
        splitter.addWidget(preview_box)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([344, 155])
        right.addWidget(splitter, 1)
        footer = QHBoxLayout()
        note = label("系统提示和非文本占位条目已自动排除。", "muted")
        footer.addWidget(note)
        footer.addStretch()
        export = QPushButton("导出 Markdown…")
        export.setObjectName("primary")
        export.clicked.connect(self.export)
        footer.addWidget(export)
        right.addLayout(footer)
        layout.addWidget(records, 1)
        self.tabs.addTab(page, "读取与预览")

    def build_ai(self):
        page = QWidget()
        layout = QHBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        sidebar = QWidget()
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(0, 0, 2, 0)
        side.setSpacing(9)
        config_card, config_body = panel(padding=14, spacing=8)
        config_body.addWidget(label("AI 模型接入", "heading"))
        config_body.addWidget(label(f"接口固定为 {DEFAULT_BASE_URL} · {DEFAULT_MODEL}", "muted"))
        self.key = QLineEdit()
        self.key.setEchoMode(QLineEdit.EchoMode.Password)
        self.key.setPlaceholderText("仅保存在本次运行内存中；填写即启用 AI 恢复")
        self.task = QPlainTextEdit("按时间整理关键事项，列出待办、负责人和未解决的问题，并标出原文时间。")
        self.task.setFixedHeight(86)
        for name, widget in [("API Key", self.key), ("分析要求", self.task)]:
            field_label = label(name, "field")
            field_label.setBuddy(widget)
            config_body.addWidget(field_label)
            config_body.addWidget(widget)
        side.addWidget(config_card)
        guide, guide_body = panel("guide", padding=11, spacing=4)
        guide_body.addWidget(label("发送范围", "field"))
        note = label("自动恢复只发送故障类型、步长、位置和可见行数等状态，不含聊天正文或群名。"
                     "主动分析时才会将所选聊天文字发送到上述固定接口；图片和语音文件不上传。", "muted")
        note.setWordWrap(True)
        guide_body.addWidget(note)
        side.addWidget(guide)
        side.addStretch()
        layout.addWidget(scroll_rail(sidebar, 326))

        results, results_body = panel(padding=14, spacing=9)
        results_body.addWidget(label("分析结果", "heading"))
        actions = QHBoxLayout()
        actions.setSpacing(7)
        self.test_api = QPushButton("测试接口（不发送聊天）")
        self.test_api.clicked.connect(self.test_connection)
        self.analyze_selected = QPushButton("分析所选消息")
        self.analyze_selected.clicked.connect(lambda: self.begin_ai(selected=True))
        self.analyze_all = QPushButton("分析当前筛选结果")
        self.analyze_all.setObjectName("primary")
        self.analyze_all.clicked.connect(lambda: self.begin_ai(selected=False))
        for button in [self.test_api, self.analyze_selected, self.analyze_all]:
            actions.addWidget(button)
        actions.addStretch()
        cancel = QPushButton("停止分析")
        cancel.clicked.connect(self.stop_event.set)
        actions.addWidget(cancel)
        results_body.addLayout(actions)
        self.ai_output = QPlainTextEdit()
        self.ai_output.setObjectName("document")
        self.ai_output.setReadOnly(True)
        self.ai_output.setPlaceholderText("分析结果会单独显示在这里，不会修改聊天原文。")
        results_body.addWidget(self.ai_output, 1)
        save_ai = QPushButton("另存分析结果…")
        save_ai.clicked.connect(self.export_ai)
        results_body.addWidget(save_ai, alignment=Qt.AlignmentFlag.AlignRight)
        layout.addWidget(results, 1)
        self.tabs.addTab(page, "AI 模型接入")

    def set_status(self, text):
        self.status.setText(text)

    def set_busy(self, value):
        self.busy = value
        for widget in (self.start, self.once, self.demo, self.reset, self.load, self.diag,
                       self.test_api, self.analyze_selected, self.analyze_all, self.group,
                       self.direction, self.reference, self.step, self.interval, self.pages,
                       self.auto_recover):
            widget.setEnabled(not value)
        self.pause.setEnabled(value)

    def finished(self, text):
        self.set_busy(False)
        self.set_status(text)

    def failed(self, text):
        self.set_busy(False)
        self.set_status("已暂停：" + text)
        title = "需要处理 · " + WINDOWS_BUILD if platform.system() == "Windows" else "需要处理"
        QMessageBox.warning(self, title, text)

    def begin(self, one_frame=False, diagnose=False):
        if self.busy:
            return
        group = self.group.text().strip()
        if not group and not diagnose:
            QMessageBox.information(self, "填写群名", "请填写当前企业微信打开的群的完整名称。")
            return
        if not diagnose and self.messages and self.record_group != group:
            QMessageBox.information(self, "请新建会话", "当前已有其他群的记录。请先导出，再点击“新建会话”。")
            return
        if not diagnose and self.session_path is None:
            self.session_path = self.data_dir / "sessions" / (datetime.now().strftime("%Y%m%d-%H%M%S-") + str(uuid.uuid4())[:8] + ".sqlite")
        if not diagnose:
            self.record_group = group
        self.save_config()
        self.stop_event = threading.Event()
        self.set_busy(True)
        self.set_status("正在连接企业微信。采集期间可点击本工具的“暂停”。")
        auto_recover = bool(not (one_frame or diagnose) and self.auto_recover.isChecked())
        options = dict(group=group, reference=self.reference.date().toPython(),
                       direction=self.direction.currentData(), pixels=self.step.value(),
                       interval=self.interval.value(), max_frames=self.pages.value(),
                       session_path=self.session_path, previous=list(self.messages),
                       one_frame=one_frame, diagnose=diagnose,
                       diagnostic_path=self.data_dir / "windows-last-error.txt",
                       auto_recover=auto_recover)
        if auto_recover:
            config = self.config()
            if config.key:
                options["recovery_client"] = DeepSeekClient(config, self.stop_event)
        threading.Thread(target=capture, args=(self.events, self.stop_event), kwargs=options, daemon=True).start()

    def set_messages(self, messages):
        self.messages = messages
        self.refresh_table()

    def refresh_table(self, *_):
        start = self.from_date.date().toString("yyyy-MM-dd")
        end = self.to_date.date().toString("yyyy-MM-dd")
        retained = [m for m in self.messages if not m.is_placeholder]
        self.visible_messages = sorted([m for m in retained if not self.use_range.isChecked()
                                       or start <= m.timestamp[:10] <= end], key=lambda m: m.timestamp)
        self.table.setRowCount(len(self.visible_messages))
        types = {"text": "文本", "file": "文件", "audio": "语音"}
        for row, message in enumerate(self.visible_messages):
            fields = [message.timestamp.replace("T", " "), message.sender,
                      types.get(message.kind, message.kind),
                      message.text.replace("\n", "  ")[:160]]
            for column, value in enumerate(fields):
                self.table.setItem(row, column, QTableWidgetItem(value))
        self.empty_state.setVisible(not self.visible_messages)
        self.count.setText(f"{len(retained)} 条成员消息 · 当前显示 {len(self.visible_messages)} 条")
        self.preview_selected()

    def preview_selected(self):
        selected = self.table.selectionModel().selectedRows()
        if selected and selected[0].row() < len(self.visible_messages):
            message = self.visible_messages[selected[0].row()]
            self.preview.setPlainText(markdown([message]))
        else:
            self.preview.clear()

    def new_session(self):
        if self.messages and QMessageBox.question(self, "新建会话", "当前会话已自动保存在本地。开始一个空会话？") != QMessageBox.StandardButton.Yes:
            return
        self.messages = []
        self.record_group = ""
        self.session_path = None
        self.preview.clear()
        self.refresh_table()
        self.set_status("新会话。请打开目标群并填写群名。")

    def load_demo(self):
        if self.messages:
            QMessageBox.information(self, "先新建会话", "请先点击“新建会话”，避免替换现有记录。")
            return
        today = date.today().isoformat()
        self.messages = [
            Message("小林", today + "T09:00", "上午好，这两票异常件请帮忙核实。"),
            Message("小陈", today + "T09:01", "收到，我先查轨迹。"),
            Message("小林", today + "T09:02", "[文件] 异常件清单.xlsx 6K", "file"),
            Message("小陈", today + "T09:03", "收到"),
            Message("小陈", today + "T09:03", "收到"),
            Message("小陈", today + "T09:04", "第一票地址需补充门牌号。", quote_sender="小林", quote="上午好，这两票异常件请帮忙核实。"),
            Message("小林", today + "T09:05", "[非文本消息]", "nontext"),
        ]
        self.group.setText("演示群")
        self.record_group = "演示群"
        self.session_path = self.data_dir / "sessions" / ("demo-" + str(uuid.uuid4())[:8] + ".sqlite")
        store = SessionStore(self.session_path)
        store.save(self.record_group, date.today().isoformat(), self.messages)
        store.close()
        self.refresh_table()
        self.set_status("演示数据已载入。连续两条“收到”会分别保留，可直接测试导出。")

    def load_session(self):
        path, _ = QFileDialog.getOpenFileName(self, "恢复本地会话", str(self.data_dir / "sessions"), "会话 (*.sqlite)")
        if not path:
            return
        try:
            store = SessionStore(Path(path))
            try:
                metadata, messages = store.load()
            finally:
                store.close()
            self.messages, self.session_path = messages, Path(path)
            self.record_group = metadata["group"]
            self.group.setText(self.record_group)
            self.reference.setDate(QDate.fromString(metadata["reference"], "yyyy-MM-dd"))
            self.refresh_table()
            self.set_status("会话已恢复。继续读取前，请把企业微信滚动到与已有记录重叠的位置。")
        except Exception as exc:
            self.failed("无法恢复会话：" + str(exc))

    def export(self):
        if not self.visible_messages:
            QMessageBox.information(self, "暂无记录", "请先读取消息、恢复会话或载入演示。")
            return
        safe = re.sub(r'[<>:"/\\|?*]', "_", self.record_group or "群聊")
        path, _ = QFileDialog.getSaveFileName(self, "导出逐条聊天记录", f"{safe}_逐条聊天记录.md", "Markdown (*.md)")
        if path:
            Path(path).write_text(markdown(self.visible_messages), encoding="utf-8")
            self.set_status(f"已导出 {len(self.visible_messages)} 条消息：{path}")

    def save_diagnostic(self, content):
        path, _ = QFileDialog.getSaveFileName(self, "保存本地结构诊断", "企业微信_结构诊断.txt", "Text (*.txt)")
        if path:
            Path(path).write_text(content, encoding="utf-8")

    def config(self):
        return default_config(self.key.text().strip())

    def test_connection(self):
        if self.busy:
            return
        try:
            config = self.config()
            config.endpoint
        except Exception as exc:
            self.failed(str(exc))
            return
        self.save_config()
        self.stop_event = threading.Event()
        self.set_busy(True)
        self.set_status("正在测试接口，仅发送一条测试文字，不发送聊天记录。")
        def run():
            try:
                response = DeepSeekClient(config, self.stop_event).complete("请只回复：连接成功")
                self.events.finished.emit("接口测试成功：" + response[:100])
            except Exception as exc:
                self.events.failure.emit(str(exc))
        threading.Thread(target=run, daemon=True).start()

    def begin_ai(self, selected):
        if self.busy:
            return
        rows = sorted(index.row() for index in self.table.selectionModel().selectedRows())
        messages = [self.visible_messages[i] for i in rows] if selected else list(self.visible_messages)
        if not messages:
            QMessageBox.information(self, "没有选中的消息", "请先读取记录，再选择需要分析的消息。")
            return
        try:
            config = self.config()
            endpoint = config.endpoint
        except Exception as exc:
            self.failed(str(exc))
            return
        destination = urlsplit(endpoint).netloc
        answer = QMessageBox.question(self, "发送所选文字进行分析", (
            f"将把 {len(messages)} 条消息的文字（约 {len(markdown(messages)):,} 字符，含引用、姓名和时间）"
            f"发送到：\n{endpoint}\n\n"
            "原文可能包含订单、联系方式和接口信息。请确认接收方和所选范围。\n"
            "图片和语音文件不会上传。是否开始？"))
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.save_config()
        self.stop_event = threading.Event()
        self.set_busy(True)
        self.set_status("正在连接 " + destination)
        task = self.task.toPlainText().strip() or "总结关键事项和待办。"
        def run():
            try:
                result = analyze(DeepSeekClient(config, self.stop_event), messages, task, self.events.status.emit)
                self.events.ai_ready.emit(result)
                self.events.finished.emit("分析完成，原始聊天记录未被修改。")
            except Exception as exc:
                self.events.failure.emit(str(exc))
        threading.Thread(target=run, daemon=True).start()

    def show_ai(self, result):
        self.ai_result = result
        self.ai_output.setPlainText(result)

    def export_ai(self):
        if not self.ai_result:
            return
        path, _ = QFileDialog.getSaveFileName(self, "另存分析结果", "聊天分析.md", "Markdown (*.md)")
        if path:
            Path(path).write_text(self.ai_result, encoding="utf-8")

    def save_config(self):
        # Explicitly exclude API keys, messages, and account credentials.
        value = {"auto_recover": self.auto_recover.isChecked()}
        (self.data_dir / "settings.json").write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def restore_config(self):
        try:
            value = json.loads((self.data_dir / "settings.json").read_text(encoding="utf-8"))
            self.auto_recover.setChecked(bool(value.get("auto_recover", True)))
        except (OSError, ValueError):
            pass

    def closeEvent(self, event):
        self.stop_event.set()
        self.key.clear()
        self.save_config()
        event.accept()


def main():
    app = QApplication(sys.argv)
    app.setOrganizationName("LocalTools")
    app.setApplicationName("GroupChatReader")
    app.setStyle("Fusion")
    app.setFont(QFont("PingFang SC" if platform.system() == "Darwin" else "Microsoft YaHei", 11))
    app.setStyleSheet(STYLE)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
