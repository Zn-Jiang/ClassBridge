from __future__ import annotations

import ctypes
import logging
import sys
import threading
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple

from PyQt6.QtCore import QEasingCurve, QParallelAnimationGroup, QPropertyAnimation, QThread, QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QAction, QCloseEvent, QIcon
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLineEdit,
    QMenu,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    ComboBox,
    FluentIcon as FIF,
    FluentWindow,
    IndeterminateProgressRing,
    InfoBar,
    InfoBarPosition,
    LineEdit,
    PrimaryPushButton,
    PushButton,
    ScrollArea,
    SpinBox,
    SubtitleLabel,
    MessageBoxBase,
)

from shared.config import ClientConfig, save_client_config
from shared.protocol import ClientMode, MessageStatus

from .models import ClientMessage, ClientSnapshot
from .ntp import TimeSyncResult, get_network_time
from .schedule_loader import (
    ScheduleSource,
    list_schedule_sources,
    load_schedule_break_ranges,
    resolve_schedule_source,
    validate_schedule_file,
)
from .security import Challenge, verify_with_challenge
from .websocket_worker import ClientWorker


CLIENT_DIR = Path(__file__).resolve().parent
ICON_ICO_PATH = CLIENT_DIR / "icon.ico"
ICON_PNG_PATH = CLIENT_DIR / "icon.png"
RETENTION_OPTIONS = {"1月": 30, "3月": 90, "1年": 365, "永久": 0}
logger = logging.getLogger("kg.client.main_window")


class FlatMessageWidget(QWidget):
    def __init__(
        self,
        message: ClientMessage,
        *,
        show_read_button: bool,
        pending_read: bool = False,
        on_mark_read: Optional[Callable[[int], None]] = None,
    ) -> None:
        super().__init__()
        self.message = message
        self._show_read_button = show_read_button
        self._pending_read = pending_read
        self._on_mark_read = on_mark_read
        self.setObjectName(f"message_widget_{message.db_id}")
        self._build_ui()
        self.update_message(message)
        self.set_pending_read(pending_read)

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.card = QFrame(self)
        self.card.setObjectName("messageCard")
        content_layout = QVBoxLayout(self.card)
        content_layout.setContentsMargins(16, 16, 16, 16)
        content_layout.setSpacing(12)

        header = QHBoxLayout()
        header.setSpacing(12)
        self.sender_label = SubtitleLabel("", self.card)
        self.time_label = CaptionLabel("", self.card)
        self.time_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        self.time_label.setWordWrap(True)
        header.addWidget(self.sender_label, 1)
        header.addWidget(self.time_label)
        content_layout.addLayout(header)

        self.body_label = BodyLabel("", self.card)
        self.body_label.setWordWrap(True)
        content_layout.addWidget(self.body_label)

        footer = QHBoxLayout()
        footer.setSpacing(10)
        self.footer_label = CaptionLabel("", self.card)
        footer.addWidget(self.footer_label)
        footer.addStretch(1)

        self._action_host = QWidget(self.card)
        action_layout = QHBoxLayout(self._action_host)
        action_layout.setContentsMargins(0, 0, 0, 0)
        action_layout.setSpacing(8)

        self.spinner = IndeterminateProgressRing(self._action_host)
        self.spinner.setFixedSize(18, 18)
        self.spinner.hide()
        action_layout.addWidget(self.spinner)

        self.read_button: Optional[PrimaryPushButton] = None
        if self._show_read_button:
            self.read_button = PrimaryPushButton("已读", self._action_host)
            self.read_button.clicked.connect(self._handle_mark_read)
            action_layout.addWidget(self.read_button)

        footer.addWidget(self._action_host)
        content_layout.addLayout(footer)
        root.addWidget(self.card)

    def update_message(self, message: ClientMessage) -> None:
        self.message = message
        self.sender_label.setText(message.sender_name)
        self.time_label.setText(message.latest_time_text)

        body_text = message.content
        if message.status == MessageStatus.RECALLED:
            body_text = "（家长已撤回该消息）"
        self.body_label.setText(body_text)
        self.footer_label.setText(self._footer_text())
        self.card.setStyleSheet(self._card_qss())
        self.body_label.setStyleSheet(self._body_qss())
        self.footer_label.setStyleSheet(self._footer_qss())

        if self.read_button is not None:
            self.read_button.setEnabled(
                message.status == MessageStatus.UNREAD and not self._pending_read
            )

    def set_pending_read(self, pending_read: bool) -> None:
        self._pending_read = pending_read
        if self.read_button is None:
            return
        self.spinner.setVisible(pending_read)
        self.read_button.setVisible(not pending_read)
        self.read_button.setEnabled(self.message.status == MessageStatus.UNREAD and not pending_read)

    def _card_qss(self) -> str:
        if self.message.status == MessageStatus.RECALLED:
            return (
                "QFrame#messageCard {"
                "background: rgba(255, 255, 255, 0.92);"
                "border: 1px solid rgba(0, 0, 0, 0.06);"
                "border-radius: 18px;"
                "}"
            )
        if self.message.is_urgent:
            return (
                "QFrame#messageCard {"
                "background: #ffffff;"
                "border: 1px solid rgba(196, 43, 28, 0.22);"
                "border-left: 5px solid #c42b1c;"
                "border-radius: 18px;"
                "}"
            )
        return (
            "QFrame#messageCard {"
            "background: #ffffff;"
            "border: 1px solid rgba(15, 118, 110, 0.16);"
            "border-left: 5px solid rgba(15, 118, 110, 0.82);"
            "border-radius: 18px;"
            "}"
        )

    def _body_qss(self) -> str:
        if self.message.status == MessageStatus.RECALLED:
            return "color: #6b6b6b; font-size: 15px; background: transparent;"
        if self.message.is_urgent:
            return "color: #8f1d12; font-size: 19px; font-weight: 600; background: transparent;"
        return "color: #1f1f1f; font-size: 15px; background: transparent;"

    def _footer_qss(self) -> str:
        if self.message.is_urgent and self.message.status != MessageStatus.RECALLED:
            return "color: #c42b1c;"
        return "color: #606060;"

    def _footer_text(self) -> str:
        tags = ["紧急" if self.message.is_urgent else "普通"]
        if self.message.resend_count > 0:
            tags.append(f"重发 {self.message.resend_count} 次")
        if self.message.status == MessageStatus.READ:
            tags.append("已读")
        elif self.message.status == MessageStatus.RECALLED:
            tags.append("已撤回")
        else:
            tags.append("未读")
        return " | ".join(tags)

    def _handle_mark_read(self) -> None:
        if self._on_mark_read and self.message.db_id:
            self._on_mark_read(self.message.db_id)


class MessageListPage(QWidget):
    def __init__(
        self,
        title: str,
        *,
        show_read_button: bool,
        on_mark_read: Optional[Callable[[int], None]] = None,
    ) -> None:
        super().__init__()
        self.setObjectName(f"{title}_page")
        self._show_read_button = show_read_button
        self._on_mark_read = on_mark_read
        self._messages: List[ClientMessage] = []
        self._pending_read_ids: Set[int] = set()
        self._widgets: Dict[int, FlatMessageWidget] = {}
        self._removing_ids: Set[int] = set()
        self._animation_refs: List[QParallelAnimationGroup] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)
        layout.addWidget(SubtitleLabel(title))

        self._filter_box: Optional[ComboBox] = None
        if not show_read_button:
            row = QHBoxLayout()
            row.addWidget(CaptionLabel("筛选"))
            self._filter_box = ComboBox()
            self._filter_box.addItems(["全部", "普通", "紧急"])
            self._filter_box.currentTextChanged.connect(lambda _: self._sync_widgets())
            row.addWidget(self._filter_box)
            row.addStretch(1)
            layout.addLayout(row)

        self._scroll = ScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setStyleSheet("background: transparent; border: none;")
        try:
            self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        except Exception:
            pass

        self._content = QWidget()
        self._content.setStyleSheet("background: transparent;")
        self._content_layout = QVBoxLayout(self._content)
        self._content_layout.setContentsMargins(0, 0, 0, 0)
        self._content_layout.setSpacing(10)
        self._empty_label = BodyLabel("暂时没有消息哦")
        self._empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_label.hide()
        self._content_layout.addWidget(self._empty_label)
        self._content_layout.addStretch(1)
        self._scroll.setWidget(self._content)
        layout.addWidget(self._scroll)

    def set_messages(self, messages: List[ClientMessage]) -> None:
        self._messages = list(messages)
        self._sync_widgets()

    def set_pending_read_ids(self, pending_read_ids: Set[int]) -> None:
        self._pending_read_ids = set(pending_read_ids)
        for db_id, widget in self._widgets.items():
            widget.set_pending_read(db_id in self._pending_read_ids)

    def animate_remove(self, db_id: int, on_finished: Optional[Callable[[], None]] = None) -> None:
        widget = self._widgets.get(db_id)
        if widget is None:
            if on_finished is not None:
                on_finished()
            return
        if db_id in self._removing_ids:
            return

        self._removing_ids.add(db_id)
        effect = QGraphicsOpacityEffect(widget)
        widget.setGraphicsEffect(effect)

        opacity = QPropertyAnimation(effect, b"opacity", self)
        opacity.setDuration(180)
        opacity.setStartValue(1.0)
        opacity.setEndValue(0.0)

        height = QPropertyAnimation(widget, b"maximumHeight", self)
        height.setDuration(220)
        height.setStartValue(max(widget.height(), widget.sizeHint().height()))
        height.setEndValue(0)
        height.setEasingCurve(QEasingCurve.Type.InOutCubic)

        group = QParallelAnimationGroup(self)
        group.addAnimation(opacity)
        group.addAnimation(height)

        def cleanup() -> None:
            self._drop_widget(db_id)
            self._removing_ids.discard(db_id)
            if group in self._animation_refs:
                self._animation_refs.remove(group)
            if on_finished is not None:
                on_finished()

        group.finished.connect(cleanup)
        self._animation_refs.append(group)
        group.start()

    def _sync_widgets(self) -> None:
        filtered = self._filtered_messages()
        visible_ids = [item.db_id for item in filtered]
        visible_map = {item.db_id: item for item in filtered}

        for db_id in list(self._widgets):
            if db_id not in visible_map and db_id not in self._removing_ids:
                self._drop_widget(db_id)

        for index, message in enumerate(filtered):
            widget = self._widgets.get(message.db_id)
            if widget is None:
                widget = FlatMessageWidget(
                    message,
                    show_read_button=self._show_read_button,
                    pending_read=message.db_id in self._pending_read_ids,
                    on_mark_read=self._on_mark_read,
                )
                widget.setMaximumHeight(0)
                effect = QGraphicsOpacityEffect(widget)
                effect.setOpacity(0.0)
                widget.setGraphicsEffect(effect)
                self._widgets[message.db_id] = widget
                self._content_layout.insertWidget(index + 1, widget)
                self._animate_insert(widget)
            else:
                widget.update_message(message)
                widget.set_pending_read(message.db_id in self._pending_read_ids)
                self._content_layout.removeWidget(widget)
                self._content_layout.insertWidget(index + 1, widget)

        self._empty_label.setVisible(not visible_ids and not self._removing_ids)

    def _animate_insert(self, widget: FlatMessageWidget) -> None:
        effect = widget.graphicsEffect()
        if not isinstance(effect, QGraphicsOpacityEffect):
            effect = QGraphicsOpacityEffect(widget)
            widget.setGraphicsEffect(effect)
            effect.setOpacity(0.0)

        target_height = max(widget.sizeHint().height(), 1)

        opacity = QPropertyAnimation(effect, b"opacity", self)
        opacity.setDuration(220)
        opacity.setStartValue(0.0)
        opacity.setEndValue(1.0)

        height = QPropertyAnimation(widget, b"maximumHeight", self)
        height.setDuration(260)
        height.setStartValue(0)
        height.setEndValue(target_height)
        height.setEasingCurve(QEasingCurve.Type.OutCubic)

        group = QParallelAnimationGroup(self)
        group.addAnimation(opacity)
        group.addAnimation(height)

        def cleanup() -> None:
            widget.setMaximumHeight(16777215)
            widget.setGraphicsEffect(None)
            if group in self._animation_refs:
                self._animation_refs.remove(group)

        group.finished.connect(cleanup)
        self._animation_refs.append(group)
        group.start()

    def _drop_widget(self, db_id: int) -> None:
        widget = self._widgets.pop(db_id, None)
        if widget is None:
            return
        self._content_layout.removeWidget(widget)
        widget.deleteLater()
        self._empty_label.setVisible(not self._widgets and not self._removing_ids)

    def _filtered_messages(self) -> List[ClientMessage]:
        items = list(self._messages)
        if self._filter_box is not None:
            current = self._filter_box.currentText()
            if current == "普通":
                items = [item for item in items if not item.is_urgent]
            elif current == "紧急":
                items = [item for item in items if item.is_urgent]
        return sorted(items, key=lambda item: item.sort_key, reverse=True)


class BreakMonitorThread(QThread):
    popup_requested = pyqtSignal(str, int)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._lock = threading.Lock()
        self._running = True
        self._schedule_ranges: List[Tuple] = []
        self._sync_time: Optional[datetime] = None
        self._sync_anchor: Optional[datetime] = None
        self._unread_count = 0
        self._unread_revision = 0
        self._last_break_key: Optional[str] = None
        self._last_popup_revision = -1

    def stop(self) -> None:
        with self._lock:
            self._running = False

    def update_schedule_ranges(self, ranges: List[Tuple]) -> None:
        with self._lock:
            self._schedule_ranges = list(ranges)
            self._last_break_key = None
            self._last_popup_revision = -1

    def update_time_reference(self, sync_result: Optional[TimeSyncResult]) -> None:
        with self._lock:
            self._sync_time = None if sync_result is None else sync_result.current_time
            self._sync_anchor = datetime.now()

    def update_unread_state(self, unread_count: int, unread_revision: int) -> None:
        with self._lock:
            self._unread_count = unread_count
            self._unread_revision = unread_revision

    def run(self) -> None:
        while True:
            with self._lock:
                if not self._running:
                    return
                schedule_ranges = list(self._schedule_ranges)
                sync_time = self._sync_time
                sync_anchor = self._sync_anchor
                unread_count = self._unread_count
                unread_revision = self._unread_revision
                last_break_key = self._last_break_key
                last_popup_revision = self._last_popup_revision

            popup_break_key: Optional[str] = None
            popup_unread_count = 0
            current_break_key = self._current_break_key(schedule_ranges, sync_time, sync_anchor)

            with self._lock:
                if current_break_key is None:
                    self._last_break_key = None
                    self._last_popup_revision = -1
                elif current_break_key != last_break_key:
                    self._last_break_key = current_break_key
                    self._last_popup_revision = unread_revision
                    if unread_count > 0:
                        popup_break_key = current_break_key
                        popup_unread_count = unread_count
                elif unread_count > 0 and unread_revision != last_popup_revision:
                    self._last_popup_revision = unread_revision
                    popup_break_key = current_break_key
                    popup_unread_count = unread_count

            if popup_break_key is not None:
                self.popup_requested.emit(popup_break_key, popup_unread_count)

            self.msleep(1000)

    def _current_break_key(
        self,
        schedule_ranges: List[Tuple],
        sync_time: Optional[datetime],
        sync_anchor: Optional[datetime],
    ) -> Optional[str]:
        current_dt = self._current_reference_time(sync_time, sync_anchor)
        current_time = current_dt.time()
        for start, end in schedule_ranges:
            if start <= current_time <= end:
                return f"{start.isoformat()}-{end.isoformat()}"
        return None

    def _current_reference_time(
        self,
        sync_time: Optional[datetime],
        sync_anchor: Optional[datetime],
    ) -> datetime:
        if sync_time is None or sync_anchor is None:
            return datetime.now()
        elapsed = datetime.now() - sync_anchor
        return sync_time + elapsed


class ChallengeDialog(QDialog):
    def __init__(
        self,
        challenge: Challenge,
        *,
        verify_url: str = "http://127.0.0.1:1002/verify",
        fallback_answer: str = "change-me",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._challenge = challenge
        self._verify_url = verify_url
        self._fallback_answer = fallback_answer
        self.setWindowTitle("验证访问")
        self.resize(420, 220)
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)
        layout.addWidget(SubtitleLabel("身份验证"))
        layout.addWidget(BodyLabel(self._challenge.question))
        self.answer_edit = LineEdit(self)
        self.answer_edit.setPlaceholderText("请输入答案")
        self.answer_edit.setEchoMode(QLineEdit.EchoMode.Password)
        layout.addWidget(self.answer_edit)
        self.status_label = CaptionLabel("")
        layout.addWidget(self.status_label)

        row = QHBoxLayout()
        row.addStretch(1)
        cancel_button = PushButton("取消")
        verify_button = PrimaryPushButton("验证")
        cancel_button.clicked.connect(self.reject)
        verify_button.clicked.connect(self._verify)
        row.addWidget(cancel_button)
        row.addWidget(verify_button)
        layout.addLayout(row)

    def _verify(self) -> None:
        answer = self.answer_edit.text().strip()
        if not answer:
            self.status_label.setText("请输入答案。")
            return
        ok, message = verify_with_challenge(
            self._challenge, answer,
            verify_url=self._verify_url,
            fallback_answer=self._fallback_answer,
        )
        if ok:
            self.accept()
            return
        self.status_label.setText(message)


class UrgentMessageDialog(MessageBoxBase):
    def __init__(self, message: ClientMessage, default_minutes: int, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.message = message
        self.remind_later = False
        self.remind_minutes = default_minutes
        self._build_ui()

    def _build_ui(self) -> None:
        self.title_label = SubtitleLabel("紧急消息", self)
        self.sender_label = CaptionLabel(f"来自：{self.message.sender_name}", self)
        self.body_label = BodyLabel(self.message.content, self)
        self.body_label.setWordWrap(True)
        self.body_label.setStyleSheet("font-size: 20px; font-weight: 600; color: #8f1d12; background: transparent;")

        self.body_scroll = ScrollArea(self)
        self.body_scroll.setWidgetResizable(True)
        self.body_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.body_scroll.setStyleSheet("background: transparent; border: none;")
        try:
            self.body_scroll.setFrameShape(QFrame.Shape.NoFrame)
        except Exception:
            pass

        body_host = QWidget(self)
        body_layout = QVBoxLayout(body_host)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.addWidget(self.body_label)
        body_layout.addStretch(1)
        self.body_scroll.setWidget(body_host)
        self.body_scroll.setFixedHeight(self._body_scroll_height())

        self.spin_label = CaptionLabel("延时提醒（分钟）", self)
        self.spin_box = SpinBox(self)
        self.spin_box.setRange(1, 120)
        self.spin_box.setValue(max(1, self.remind_minutes))

        spin_row = QHBoxLayout()
        spin_row.addWidget(self.spin_label)
        spin_row.addStretch(1)
        spin_row.addWidget(self.spin_box)

        self.viewLayout.addWidget(self.title_label)
        self.viewLayout.addWidget(self.sender_label)
        self.viewLayout.addWidget(self.body_scroll)
        self.viewLayout.addLayout(spin_row)

        self.yesButton.setText("我知道了（已读）")
        self.cancelButton.setText("延时提醒")
        self.cancelButton.clicked.disconnect()
        self.cancelButton.clicked.connect(self._remind_later)
        self.widget.setStyleSheet(
            "background: #ffffff;"
            "border-radius: 18px;"
        )

    def _remind_later(self) -> None:
        self.remind_later = True
        self.remind_minutes = self.spin_box.value()
        self.reject()

    def _body_scroll_height(self) -> int:
        self.body_label.setMaximumWidth(420)
        self.body_label.adjustSize()
        content_height = self.body_label.sizeHint().height()
        return min(max(content_height + 16, 72), 220)


class SettingsPage(QWidget):
    def __init__(
        self,
        config: ClientConfig,
        *,
        on_exam_mode_changed: Callable[[bool], None],
        on_server_url_changed: Callable[[str], None],
        on_ntp_server_changed: Callable[[str], None],
        on_retention_changed: Callable[[int], None],
        on_schedule_source_changed: Callable[[str], None],
        on_reload_schedules: Callable[[], None],
    ) -> None:
        super().__init__()
        self._config = config
        self._on_exam_mode_changed = on_exam_mode_changed
        self._on_server_url_changed = on_server_url_changed
        self._on_ntp_server_changed = on_ntp_server_changed
        self._on_retention_changed = on_retention_changed
        self._on_schedule_source_changed = on_schedule_source_changed
        self._on_reload_schedules = on_reload_schedules
        self.setObjectName("settings_page")
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(18)
        layout.addWidget(SubtitleLabel("设置"))

        self.exam_checkbox = QCheckBox("考试模式")
        self.exam_checkbox.stateChanged.connect(
            lambda state: self._on_exam_mode_changed(state == Qt.CheckState.Checked.value)
        )
        layout.addWidget(self.exam_checkbox)

        self.server_edit = LineEdit()
        self.server_edit.setText(self._config.resolved_client_ws_url())
        self.server_edit.setReadOnly(True)
        self.server_edit.setEnabled(False)
        self.server_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.server_unlock_button = PushButton("查看/修改服务器地址")
        self.server_unlock_button.clicked.connect(self._unlock_server_url)
        self.server_save_button = PrimaryPushButton("保存")
        self.server_save_button.setEnabled(False)
        self.server_save_button.clicked.connect(self._save_server_url)
        layout.addWidget(
            _field_row(
                "服务器地址",
                self.server_edit,
                self.server_unlock_button,
                self.server_save_button,
            )
        )

        self.ntp_edit = LineEdit()
        self.ntp_edit.setText(self._config.ntp_server)
        self.ntp_edit.setReadOnly(True)
        self.ntp_edit.setEnabled(False)
        self.ntp_unlock_button = PushButton("修改 NTP 服务器")
        self.ntp_unlock_button.clicked.connect(self._unlock_ntp_server)
        self.ntp_save_button = PrimaryPushButton("保存")
        self.ntp_save_button.setEnabled(False)
        self.ntp_save_button.clicked.connect(self._save_ntp_server)
        layout.addWidget(
            _field_row(
                "NTP 服务器",
                self.ntp_edit,
                self.ntp_unlock_button,
                self.ntp_save_button,
            )
        )

        self.sync_button = PushButton("立即校时")
        layout.addWidget(self.sync_button)

        self.schedule_box = ComboBox()
        self.schedule_box.currentTextChanged.connect(self._change_schedule_source)
        self.schedule_refresh_button = PushButton("刷新时间表列表")
        self.schedule_refresh_button.clicked.connect(self._on_reload_schedules)
        layout.addWidget(_field_row("时间表来源", self.schedule_box, self.schedule_refresh_button))

        self.retention_box = ComboBox()
        for label in RETENTION_OPTIONS:
            self.retention_box.addItem(label)
        self.retention_box.setCurrentText(_retention_label(self._config.history_retention_days))
        self.retention_box.currentTextChanged.connect(self._change_retention)
        layout.addWidget(_field_row("历史消息保留", self.retention_box))

        layout.addStretch(1)

    def set_exam_mode(self, enabled: bool) -> None:
        self.exam_checkbox.blockSignals(True)
        self.exam_checkbox.setChecked(enabled)
        self.exam_checkbox.blockSignals(False)

    def set_schedule_options(self, sources: List[ScheduleSource], current_key: Optional[str]) -> None:
        self.schedule_box.blockSignals(True)
        self.schedule_box.clear()
        for source in sources:
            self.schedule_box.addItem(source.label, userData=source.key)
        if sources and current_key:
            index = next((i for i, item in enumerate(sources) if item.key == current_key), 0)
            self.schedule_box.setCurrentIndex(index)
        self.schedule_box.setEnabled(bool(sources))
        if not sources:
            self.schedule_box.setPlaceholderText("未找到时间表")
        self.schedule_box.blockSignals(False)

    def relock_sensitive_inputs(self) -> None:
        self.server_edit.setEnabled(False)
        self.server_edit.setReadOnly(True)
        self.server_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.server_save_button.setEnabled(False)
        self.ntp_edit.setEnabled(False)
        self.ntp_edit.setReadOnly(True)
        self.ntp_save_button.setEnabled(False)

    def hideEvent(self, event) -> None:
        self.relock_sensitive_inputs()
        super().hideEvent(event)

    def _verify_access(self) -> bool:
        config = self._config
        challenge = Challenge.fetch(
            config.challenge_url,
            fallback_question=config.fallback_question,
            fallback_answer=config.fallback_answer,
        )
        return (
            ChallengeDialog(
                challenge,
                verify_url=config.verify_url,
                fallback_answer=config.fallback_answer,
                parent=self,
            ).exec()
            == QDialog.DialogCode.Accepted
        )

    def _unlock_server_url(self) -> None:
        if self._verify_access():
            self.server_edit.setEnabled(True)
            self.server_edit.setReadOnly(False)
            self.server_edit.setEchoMode(QLineEdit.EchoMode.Normal)
            self.server_save_button.setEnabled(True)

    def _unlock_ntp_server(self) -> None:
        if self._verify_access():
            self.ntp_edit.setEnabled(True)
            self.ntp_edit.setReadOnly(False)
            self.ntp_save_button.setEnabled(True)

    def _save_server_url(self) -> None:
        value = self.server_edit.text().strip()
        if value:
            self._on_server_url_changed(value)
        self.relock_sensitive_inputs()

    def _save_ntp_server(self) -> None:
        value = self.ntp_edit.text().strip()
        if value:
            self._on_ntp_server_changed(value)
        self.relock_sensitive_inputs()

    def _change_retention(self, label: str) -> None:
        self._on_retention_changed(RETENTION_OPTIONS[label])

    def _change_schedule_source(self, _: str) -> None:
        key = self.schedule_box.currentData()
        if key:
            self._on_schedule_source_changed(str(key))


class MainWindow(FluentWindow):
    def __init__(self, config: ClientConfig):
        super().__init__()
        self._config = config
        self._worker: Optional[ClientWorker] = None
        self._snapshot: Optional[ClientSnapshot] = None
        self._last_time_sync: Optional[TimeSyncResult] = None
        self._last_time_sync_anchor: Optional[datetime] = None
        self._force_close = False
        self._last_connected_state: Optional[bool] = None
        self._notified_break_key: Optional[str] = None
        self._schedule_sources: List[ScheduleSource] = []
        self._schedule_source: Optional[ScheduleSource] = None
        self._schedule_ranges: List[Tuple] = []
        self._reminder_timers: Dict[int, QTimer] = {}
        self._pending_read_ids: Set[int] = set()
        self._settling_read_ids: Set[int] = set()
        self._queued_urgent_ids: List[int] = []
        self._active_urgent_db_id: Optional[int] = None
        self._active_urgent_dialog: Optional[UrgentMessageDialog] = None
        self._urgent_parent_topmost = False
        self._break_unread_revision = 0
        self._foreground_restore_timer = QTimer(self)
        self._foreground_restore_timer.setSingleShot(True)
        self._foreground_restore_timer.timeout.connect(self._restore_transient_topmost)
        self._break_monitor = BreakMonitorThread(self)
        self._break_monitor.popup_requested.connect(self._on_break_popup_requested)

        self.unread_page = MessageListPage("未读消息", show_read_button=True, on_mark_read=self._mark_read)
        self.history_page = MessageListPage("历史消息", show_read_button=False)
        self.settings_page = SettingsPage(
            config,
            on_exam_mode_changed=self._set_exam_mode,
            on_server_url_changed=self._change_server_url,
            on_ntp_server_changed=self._change_ntp_server,
            on_retention_changed=self._change_history_retention,
            on_schedule_source_changed=self._change_schedule_source,
            on_reload_schedules=self._reload_schedule_sources,
        )

        self._init_window()
        self._init_tray()
        self._reload_schedule_sources()
        self._start_worker()
        self._sync_time()
        self._break_monitor.start()

    def _init_window(self) -> None:
        self.resize(1080, 760)
        self.setWindowTitle("家校沟通客户端")
        self._apply_app_icon()
        if hasattr(self, "setMicaEffectEnabled"):
            try:
                self.setMicaEffectEnabled(True)
            except Exception:
                pass
        self.addSubInterface(self.unread_page, FIF.MAIL, "未读消息")
        self.addSubInterface(self.history_page, FIF.HISTORY, "历史消息")
        self.addSubInterface(self.settings_page, FIF.SETTING, "设置")
        self.settings_page.sync_button.clicked.connect(self._sync_time)
        if hasattr(self, "stackedWidget"):
            self.stackedWidget.currentChanged.connect(self._on_interface_changed)
        self._update_window_title(False, ClientMode.NORMAL)

    def _apply_app_icon(self) -> None:
        icon_path = ICON_ICO_PATH if ICON_ICO_PATH.exists() else ICON_PNG_PATH
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))

    def _init_tray(self) -> None:
        self.tray = QSystemTrayIcon(self.windowIcon(), self)
        self.tray.setToolTip("家校沟通客户端")
        menu = QMenu(self)
        show_action = QAction("显示主界面", self)
        show_action.triggered.connect(self.show_normal)
        exit_action = QAction("退出", self)
        exit_action.triggered.connect(self.exit_app)
        menu.addAction(show_action)
        menu.addAction(exit_action)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()

    def _start_worker(self) -> None:
        self._stop_worker()
        self._worker = ClientWorker(self._config)
        self._worker.connection_changed.connect(self._on_connection_changed)
        self._worker.snapshot_received.connect(self._on_snapshot_received)
        self._worker.read_completed.connect(self._on_read_completed)
        self._worker.read_failed.connect(self._on_read_failed)
        self._worker.error_message.connect(self._show_warning)
        self._worker.start()

    def _stop_worker(self) -> None:
        if self._worker is None:
            return
        self._worker.stop()
        if not self._worker.wait(5000):
            self._worker.terminate()
            self._worker.wait(1000)
        self._worker = None

    def _stop_break_monitor(self) -> None:
        if self._break_monitor is None:
            return
        self._break_monitor.stop()
        if not self._break_monitor.wait(3000):
            self._break_monitor.terminate()
            self._break_monitor.wait(1000)

    def _reload_schedule_sources(self) -> None:
        self._schedule_sources = list_schedule_sources()
        selected, _, warning = self._select_working_schedule_source(
            preferred_key=self._config.schedule_source,
            fallback_key=self._config.last_valid_schedule_source,
            show_warning=False,
        )
        self._schedule_source = selected
        self.settings_page.set_schedule_options(self._schedule_sources, selected.key if selected else None)
        self._break_monitor.update_schedule_ranges(self._schedule_ranges)
        if selected is not None:
            self._persist_schedule_selection(selected.key, mark_valid=True)
        if warning:
            self._show_warning(warning)

    def _change_schedule_source(self, schedule_key: str) -> None:
        if self._config.schedule_source == schedule_key:
            return
        selected, requested, warning = self._select_working_schedule_source(
            preferred_key=schedule_key,
            fallback_key=self._config.last_valid_schedule_source,
            show_warning=True,
        )
        self._schedule_source = selected
        self.settings_page.set_schedule_options(self._schedule_sources, selected.key if selected else None)
        if requested is not None and selected is not None and requested.key != selected.key:
            self._show_warning(
                self._format_schedule_fallback_warning(
                    requested_label=requested.label,
                    selected_label=selected.label,
                    error_message=warning,
                )
            )
        elif warning:
            self._show_warning(warning)
        if selected is not None:
            self._persist_schedule_selection(selected.key, mark_valid=True)
            self._show_info(f"时间表已切换为：{selected.label}")
        else:
            self._persist_schedule_selection(schedule_key, mark_valid=False)
            self.settings_page.set_schedule_options(self._schedule_sources, schedule_key)

    def _select_working_schedule_source(
        self,
        *,
        preferred_key: Optional[str],
        fallback_key: Optional[str],
        show_warning: bool,
    ) -> Tuple[Optional[ScheduleSource], Optional[ScheduleSource], Optional[str]]:
        source_by_key = {item.key: item for item in self._schedule_sources}
        requested = source_by_key.get(preferred_key) if preferred_key else None

        candidate_keys: List[str] = []
        for key in (preferred_key, fallback_key):
            if key and key not in candidate_keys:
                candidate_keys.append(key)
        for item in self._schedule_sources:
            if item.key not in candidate_keys:
                candidate_keys.append(item.key)

        first_error: Optional[str] = None
        for key in candidate_keys:
            source = source_by_key.get(key)
            if source is None:
                continue
            ranges, error = validate_schedule_file(source.path)
            if ranges:
                self._schedule_ranges = ranges
                return source, requested, first_error if requested and requested.key != source.key else None
            if requested is not None and key == requested.key and error:
                first_error = f"时间表 {requested.label} 不可用：{error}"
                logger.warning(first_error)

        self._schedule_ranges = []
        if show_warning:
            return None, requested, first_error or "没有可用时间表。"
        return None, requested, first_error or "没有可用时间表，课间自动弹出将暂时停用。"

    def _persist_schedule_selection(self, selected_key: Optional[str], *, mark_valid: bool) -> None:
        changed = False
        if self._config.schedule_source != selected_key:
            self._config.schedule_source = selected_key
            changed = True
        if mark_valid and self._config.last_valid_schedule_source != selected_key:
            self._config.last_valid_schedule_source = selected_key
            changed = True
        if changed:
            save_client_config(self._config)

    def _format_schedule_fallback_warning(
        self,
        *,
        requested_label: str,
        selected_label: str,
        error_message: Optional[str],
    ) -> str:
        if error_message:
            return f"{error_message} 已自动切换到 {selected_label}。"
        return f"时间表 {requested_label} 不可用，已自动切换到 {selected_label}。"

    def _mark_read(self, db_id: int) -> None:
        if db_id in self._pending_read_ids:
            return
        self._pending_read_ids.add(db_id)
        self.unread_page.set_pending_read_ids(self._pending_read_ids)
        if self._worker is not None:
            self._worker.mark_read(db_id)
            return
        self._pending_read_ids.discard(db_id)
        self.unread_page.set_pending_read_ids(self._pending_read_ids)
        self._show_warning("客户端未连接，无法同步已读。")

    def _set_exam_mode(self, enabled: bool) -> None:
        if self._worker is not None:
            self._worker.set_exam_mode(enabled)
        mode = ClientMode.EXAM if enabled else ClientMode.NORMAL
        self._update_window_title(self._snapshot.is_online if self._snapshot else True, mode)

    def _change_server_url(self, value: str) -> None:
        self._config.server_ws_url = value
        save_client_config(self._config)
        self._show_info("服务器地址已保存，正在重连。")
        self._start_worker()

    def _change_ntp_server(self, value: str) -> None:
        self._config.ntp_server = value
        save_client_config(self._config)
        self._show_info("NTP 服务器已保存。")

    def _change_history_retention(self, days: int) -> None:
        self._config.history_retention_days = days
        save_client_config(self._config)
        if self._snapshot is not None:
            self.history_page.set_messages(self._retained_history_items(self._snapshot.history_items))

    def _on_connection_changed(self, connected: bool, text: str) -> None:
        if self._last_connected_state is None or self._last_connected_state != connected:
            QApplication.beep()
        self._last_connected_state = connected
        mode = self._snapshot.mode if self._snapshot else ClientMode.NORMAL
        self._update_window_title(connected, mode)
        self.tray.setToolTip(f"家校沟通客户端 - {text}")

    def _on_snapshot_received(self, snapshot: ClientSnapshot) -> None:
        previous_unread_map = {item.db_id: item for item in self._snapshot.unread_items} if self._snapshot else {}
        changed_unread_ids = self._changed_unread_ids(snapshot.unread_items, previous_unread_map)
        snapshot = self._apply_settling_reads(snapshot)
        snapshot.history_items = self._retained_history_items(snapshot.history_items)
        self._snapshot = snapshot
        self.unread_page.set_messages(snapshot.unread_items)
        self.unread_page.set_pending_read_ids(self._pending_read_ids)
        self.history_page.set_messages(snapshot.history_items)
        self.settings_page.set_exam_mode(snapshot.mode == ClientMode.EXAM)
        self._update_window_title(snapshot.is_online, snapshot.mode)

        for message in snapshot.unread_items:
            if (
                message.is_urgent
                and message.db_id in changed_unread_ids
                and message.db_id not in self._pending_read_ids
            ):
                self._queue_urgent_message(message.db_id)

        self._show_next_urgent_popup()
        self._update_break_monitor_state(changed_unread_ids)

    def _changed_unread_ids(
        self,
        unread_items: List[ClientMessage],
        previous_unread_map: Dict[int, ClientMessage],
    ) -> Set[int]:
        changed_ids: Set[int] = set()
        for item in unread_items:
            previous = previous_unread_map.get(item.db_id)
            if previous is None:
                changed_ids.add(item.db_id)
                continue
            if previous.resend_count != item.resend_count:
                changed_ids.add(item.db_id)
                continue
            if previous.resend_time != item.resend_time:
                changed_ids.add(item.db_id)
                continue
            if previous.timestamp != item.timestamp:
                changed_ids.add(item.db_id)
        return changed_ids

    def _update_break_monitor_state(self, changed_unread_ids: Set[int]) -> None:
        unread_count = self._current_unread_count()
        if changed_unread_ids:
            self._break_unread_revision += 1
        self._break_monitor.update_unread_state(unread_count, self._break_unread_revision)

    def _on_break_popup_requested(self, break_key: str, unread_count: int) -> None:
        logger.info(
            "Break monitor requested popup: break=%s unread_count=%s active_window=%s visible=%s minimized=%s",
            break_key,
            unread_count,
            self.isActiveWindow(),
            self.isVisible(),
            self.isMinimized(),
        )
        self.show_normal(force_topmost=True, switch_to_unread=True)
        if self._notified_break_key != break_key:
            self._notified_break_key = break_key
            self._show_info(f"课间休息，有 {unread_count} 条未读消息")

    def _apply_settling_reads(self, snapshot: ClientSnapshot) -> ClientSnapshot:
        if not self._settling_read_ids:
            return snapshot

        local_history = {item.db_id: item for item in self._snapshot.history_items} if self._snapshot else {}
        unread_items: List[ClientMessage] = []
        history_items: List[ClientMessage] = []
        seen_history_ids: Set[int] = set()
        next_settling: Set[int] = set()
        unread_ids = {item.db_id for item in snapshot.unread_items}

        for item in snapshot.unread_items:
            if item.db_id in self._settling_read_ids:
                next_settling.add(item.db_id)
                continue
            unread_items.append(item)

        for item in snapshot.history_items:
            if item.db_id in self._settling_read_ids:
                fixed = local_history.get(item.db_id) or replace(item, status=MessageStatus.READ)
                history_items.append(replace(fixed, status=MessageStatus.READ))
                seen_history_ids.add(item.db_id)
                continue
            history_items.append(item)
            seen_history_ids.add(item.db_id)

        for db_id in self._settling_read_ids:
            if db_id not in unread_ids and db_id not in seen_history_ids:
                local = local_history.get(db_id)
                if local is not None:
                    history_items.append(replace(local, status=MessageStatus.READ))

        self._settling_read_ids = next_settling
        return ClientSnapshot(
            unread_items=sorted(unread_items, key=lambda item: item.sort_key, reverse=True),
            history_items=sorted(history_items, key=lambda item: item.sort_key, reverse=True),
            client_name=snapshot.client_name,
            is_online=snapshot.is_online,
            mode=snapshot.mode,
            updated_at=snapshot.updated_at,
        )

    def _on_read_completed(self, db_id: int) -> None:
        self._pending_read_ids.discard(db_id)
        self._settling_read_ids.add(db_id)
        self.unread_page.set_pending_read_ids(self._pending_read_ids)
        self._clear_urgent_state(db_id)
        self._apply_local_read(db_id)

    def _on_read_failed(self, db_id: int, text: str) -> None:
        self._pending_read_ids.discard(db_id)
        self.unread_page.set_pending_read_ids(self._pending_read_ids)
        self._show_warning(text)

    def _apply_local_read(self, db_id: int) -> None:
        if self._snapshot is None:
            return

        target = next((item for item in self._snapshot.unread_items if item.db_id == db_id), None)
        if target is None:
            target = next((item for item in self._snapshot.history_items if item.db_id == db_id), None)
        if target is None:
            return

        read_message = replace(target, status=MessageStatus.READ)
        self._snapshot.unread_items = [item for item in self._snapshot.unread_items if item.db_id != db_id]

        updated_history = []
        found = False
        for item in self._snapshot.history_items:
            if item.db_id == db_id:
                updated_history.append(read_message)
                found = True
            else:
                updated_history.append(item)
        if not found:
            updated_history.append(read_message)
        self._snapshot.history_items = sorted(updated_history, key=lambda item: item.sort_key, reverse=True)

        self.history_page.set_messages(self._retained_history_items(self._snapshot.history_items))
        self.unread_page.animate_remove(db_id)
        self._update_break_monitor_state(set())

    def _queue_urgent_message(self, db_id: int) -> None:
        if db_id in self._queued_urgent_ids:
            return
        if db_id == self._active_urgent_db_id:
            return
        if db_id in self._reminder_timers:
            return
        self._queued_urgent_ids.append(db_id)

    def _show_next_urgent_popup(self) -> None:
        if self._active_urgent_dialog is not None or self._snapshot is None:
            return

        while self._queued_urgent_ids:
            db_id = self._queued_urgent_ids.pop(0)
            message = next((item for item in self._snapshot.unread_items if item.db_id == db_id), None)
            if message is None or not message.is_urgent or db_id in self._pending_read_ids:
                continue

            dialog = UrgentMessageDialog(
                message,
                default_minutes=self._config.urgent_remind_default_minutes,
                parent=self,
            )
            self._bring_parent_for_urgent()
            dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
            dialog.finished.connect(lambda result, dlg=dialog, msg_id=db_id: self._on_urgent_finished(msg_id, dlg, result))
            self._active_urgent_db_id = db_id
            self._active_urgent_dialog = dialog
            dialog.open()
            QTimer.singleShot(0, lambda dlg=dialog: self._raise_dialog(dlg))
            return

    def _on_urgent_finished(self, db_id: int, dialog: UrgentMessageDialog, result: int) -> None:
        accepted = result == QDialog.DialogCode.Accepted
        remind_later = dialog.remind_later
        remind_minutes = dialog.remind_minutes

        self._active_urgent_db_id = None
        self._active_urgent_dialog = None
        dialog.deleteLater()
        self._restore_parent_after_urgent()

        if accepted:
            self._mark_read(db_id)
        elif remind_later:
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(lambda msg_id=db_id: self._show_reminder_again(msg_id))
            timer.start(remind_minutes * 60 * 1000)
            self._reminder_timers[db_id] = timer

        QTimer.singleShot(0, self._show_next_urgent_popup)

    def _raise_dialog(self, dialog: QDialog) -> None:
        self._bring_to_foreground()
        dialog.raise_()
        dialog.activateWindow()

    def _bring_parent_for_urgent(self) -> None:
        self._bring_to_foreground()
        self._urgent_parent_topmost = True

    def _restore_parent_after_urgent(self) -> None:
        if not self._urgent_parent_topmost:
            return
        self._set_window_topmost(False)
        self._urgent_parent_topmost = False

    def _show_reminder_again(self, db_id: int) -> None:
        self._reminder_timers.pop(db_id, None)
        self._queue_urgent_message(db_id)
        self._show_next_urgent_popup()

    def _clear_urgent_state(self, db_id: int) -> None:
        timer = self._reminder_timers.pop(db_id, None)
        if timer is not None:
            timer.stop()
        self._queued_urgent_ids = [item for item in self._queued_urgent_ids if item != db_id]
        if self._active_urgent_db_id == db_id and self._active_urgent_dialog is not None:
            self._active_urgent_dialog.close()

    def _current_unread_count(self) -> int:
        if self._snapshot is None:
            return 0
        return sum(
            1
            for item in self._snapshot.unread_items
            if item.db_id not in self._pending_read_ids
        )

    def _sync_time(self) -> None:
        self._last_time_sync = get_network_time(self._config.ntp_server)
        self._last_time_sync_anchor = datetime.now()
        self._break_monitor.update_time_reference(self._last_time_sync)
        self._show_info(
            f"时间同步完成，来源：{self._last_time_sync.source}，时间：{self._last_time_sync.current_time.strftime('%H:%M:%S')}"
        )

    def _retained_history_items(self, items: List[ClientMessage]) -> List[ClientMessage]:
        days = self._config.history_retention_days
        if days <= 0:
            return sorted(items, key=lambda item: item.sort_key, reverse=True)
        cutoff = datetime.now() - timedelta(days=days)
        return sorted(
            [item for item in items if item.sort_key[0] >= cutoff],
            key=lambda item: item.sort_key,
            reverse=True,
        )

    def _show_info(self, text: str) -> None:
        InfoBar.success(
            title="提示",
            content=text,
            orient=Qt.Orientation.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP_RIGHT,
            duration=2000,
            parent=self,
        )

    def _show_warning(self, text: str) -> None:
        InfoBar.warning(
            title="连接状态",
            content=text,
            orient=Qt.Orientation.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP_RIGHT,
            duration=3200,
            parent=self,
        )

    def _update_window_title(self, connected: bool, mode: ClientMode) -> None:
        status = "🟢 已连接" if connected else "🔴 离线"
        if mode == ClientMode.EXAM:
            status = "🌙 考试模式"
        self.setWindowTitle(f"家校沟通客户端 - [{status}]")

    def _on_interface_changed(self, _: int) -> None:
        current = self.stackedWidget.currentWidget() if hasattr(self, "stackedWidget") else None
        if current is not self.settings_page:
            self.settings_page.relock_sensitive_inputs()

    def show_normal(self, force_topmost: bool = False, switch_to_unread: bool = False) -> None:
        logger.info(
            "show_normal called: force_topmost=%s switch_to_unread=%s minimized=%s visible=%s active=%s",
            force_topmost,
            switch_to_unread,
            self.isMinimized(),
            self.isVisible(),
            self.isActiveWindow(),
        )
        self.showNormal()
        self.show()
        if switch_to_unread:
            self._switch_to_unread_page()
        if force_topmost:
            self._bring_to_foreground()
            self._foreground_restore_timer.start(1200)
        else:
            self.raise_()
            self.activateWindow()

    def _switch_to_unread_page(self) -> None:
        try:
            if hasattr(self, "switchTo"):
                self.switchTo(self.unread_page)
            elif hasattr(self, "stackedWidget"):
                self.stackedWidget.setCurrentWidget(self.unread_page)
        except Exception as exc:
            logger.warning("Failed to switch to unread page: %s", exc)

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.show_normal()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._force_close:
            event.accept()
            return
        if self._config.close_to_tray:
            self.hide()
            self.tray.showMessage("家校沟通客户端", "客户端已最小化到托盘。")
            event.ignore()
            return
        self.exit_app()
        event.accept()

    def exit_app(self) -> None:
        if self._active_urgent_dialog is not None:
            self._active_urgent_dialog.close()
            self._active_urgent_dialog = None
        for timer in self._reminder_timers.values():
            timer.stop()
        self._reminder_timers.clear()
        self._stop_break_monitor()
        self._stop_worker()
        self.tray.hide()
        self._force_close = True
        self.close()
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def _set_window_topmost(self, enabled: bool) -> None:
        if sys.platform != "win32":
            return
        hwnd = int(self.winId())
        HWND_TOPMOST = -1
        HWND_NOTOPMOST = -2
        SWP_NOMOVE = 0x0002
        SWP_NOSIZE = 0x0001
        SWP_SHOWWINDOW = 0x0040
        ctypes.windll.user32.SetWindowPos(
            hwnd,
            HWND_TOPMOST if enabled else HWND_NOTOPMOST,
            0,
            0,
            0,
            0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW,
        )

    def _restore_transient_topmost(self) -> None:
        if self._urgent_parent_topmost:
            return
        logger.debug("Restoring transient topmost state.")
        self._set_window_topmost(False)

    def _bring_to_foreground(self) -> None:
        self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()
        if sys.platform != "win32":
            return
        hwnd = int(self.winId())
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        SW_RESTORE = 9
        SW_SHOW = 5
        attached_foreground = False
        attached_target = False
        foreground_thread_id = 0
        target_thread_id = 0
        current_thread_id = 0
        try:
            foreground_hwnd = user32.GetForegroundWindow()
            current_thread_id = kernel32.GetCurrentThreadId()
            target_thread_id = user32.GetWindowThreadProcessId(hwnd, None)
            foreground_thread_id = (
                user32.GetWindowThreadProcessId(foreground_hwnd, None)
                if foreground_hwnd
                else target_thread_id
            )

            logger.info(
                "Attempting foreground activation: hwnd=%s foreground_hwnd=%s target_thread=%s foreground_thread=%s",
                hwnd,
                foreground_hwnd,
                target_thread_id,
                foreground_thread_id,
            )

            user32.ShowWindow(hwnd, SW_RESTORE if user32.IsIconic(hwnd) else SW_SHOW)
            self._set_window_topmost(True)
            user32.AllowSetForegroundWindow(-1)
            if foreground_thread_id and foreground_thread_id != current_thread_id:
                user32.AttachThreadInput(foreground_thread_id, current_thread_id, True)
                attached_foreground = True
            if target_thread_id and target_thread_id != current_thread_id:
                user32.AttachThreadInput(target_thread_id, current_thread_id, True)
                attached_target = True
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
            user32.SetActiveWindow(hwnd)
            user32.SetFocus(hwnd)
            user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0002 | 0x0001 | 0x0040)
            user32.SetWindowPos(hwnd, -2, 0, 0, 0, 0, 0x0002 | 0x0001 | 0x0040)
        except Exception as exc:
            logger.warning("Failed to bring main window to foreground: %s", exc)
        finally:
            if attached_foreground:
                user32.AttachThreadInput(foreground_thread_id, current_thread_id, False)
            if attached_target:
                user32.AttachThreadInput(target_thread_id, current_thread_id, False)


def _field_row(label_text: str, *widgets: QWidget) -> QWidget:
    container = QWidget()
    layout = QHBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(10)
    layout.addWidget(CaptionLabel(label_text))
    for index, widget in enumerate(widgets):
        stretch = 1 if index == 0 else 0
        layout.addWidget(widget, stretch)
    return container


def _retention_label(days: int) -> str:
    for label, value in RETENTION_OPTIONS.items():
        if value == days:
            return label
    return "永久"
