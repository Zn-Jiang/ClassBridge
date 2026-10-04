from __future__ import annotations

import contextlib
import ctypes
import logging
import sys
import threading
import time
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
    QLabel,
    QLineEdit,
    QMenu,
    QScrollArea,
    QStackedWidget,
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
    MessageBox,
    PrimaryPushButton,
    PushButton,
    RadioButton,
    ScrollArea,
    SegmentedWidget,
    SpinBox,
    SubtitleLabel,
    MessageBoxBase,
)

from shared.config import ClientConfig, save_client_config
from shared.protocol import ClientMode, MessageStatus

from .ci_watchdog import CiProcessWatcher
from .cib_supervisor import BridgeRevival, CibSupervisor
from .classisland_monitor import ClassIslandMonitor
from .classisland_import import ClassIslandConfigParser
from .database import ClientDatabase, message_to_cache_dict
from .import_wizard import ScheduleImportWizard
from .models import ClientMessage, ClientSnapshot
from .ntp import NtpSyncThread, TimeSyncResult, get_network_time
from .pending_reads import PendingReadsStore
from .schedule_store import ScheduleStore, StoredSchedule
from .scroll_utils import install_auto_hide_scrollbars
from . import cib_daemon
from .schedule_loader import (
    CLASSISLAND_SOURCE_KEY,
    ScheduleSource,
    is_classisland_source,
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
APP_NAME = "ClassBridge 客户端"
RETENTION_OPTIONS = {"1月": 30, "3月": 90, "1年": 365, "永久": 0}
#: How long to wait for the break state / first snapshot before deciding the
#: start-up visibility (the window stays hidden when the wait times out).
_STARTUP_DECISION_TIMEOUT_MS = 12000
#: Delay before comparing the ClassIsland timetable with the imported copy, so
#: the start-up visibility decision is never delayed by file parsing.
_SCHEDULE_CHECK_DELAY_MS = 2000
logger = logging.getLogger("kg.client.main_window")

# ---------------------------------------------------------------------------
# UI responsiveness watch-dog
#
# The user reported an intermittent hitch ("clicking a settings sub-page takes a
# moment before it switches").  The switch itself measures ~1ms, so the delay
# comes from *other* work occupying the UI thread.  A cheap heartbeat names the
# culprit in the log instead of leaving it to guesswork.
# ---------------------------------------------------------------------------
_UI_STALL_CHECK_MS = 250
_UI_STALL_WARN_MS = 600
#: Identical InfoBars (e.g. a reconnect warning every few seconds) are collapsed
#: instead of stacking up animated widgets, which is itself a source of lag.
_INFO_DEDUPE_SECONDS = 10.0
#: Minimum gap between two automatic bridge restarts (a zombie bridge needs at
#: most one; a tighter loop would keep killing processes).
_BRIDGE_REVIVAL_COOLDOWN_SECONDS = 300.0


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
    popup_requested = pyqtSignal(str, int, bool)
    break_state_changed = pyqtSignal(bool)  # True = in break, False = in class

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
        self._last_in_break = False
        #: Whether the current state has been reported at least once (the first
        #: pass always reports, even when the state is "in class").
        self._state_reported = False

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
                last_in_break = self._last_in_break

            popup_break_key: Optional[str] = None
            popup_unread_count = 0
            # 课间期间新到的消息要立刻弹窗（不受“下课延时”影响）
            popup_immediate = False
            current_break_key = self._current_break_key(schedule_ranges, sync_time, sync_anchor)
            current_in_break = current_break_key is not None

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
                    popup_immediate = True

                if current_in_break != last_in_break:
                    self._last_in_break = current_in_break

            if current_in_break != last_in_break or not self._state_reported:
                # The first pass always reports, so the window knows the
                # starting state instead of assuming "in class".
                self._state_reported = True
                self.break_state_changed.emit(current_in_break)

            if popup_break_key is not None:
                self.popup_requested.emit(popup_break_key, popup_unread_count, popup_immediate)

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
    """设置页。

    内部用 ``SegmentedWidget`` + ``QStackedWidget`` 分成两个子页，方便后续
    继续追加子页面：

    - **通知时机**：时间表来源模式（CI 优先 / 仅本地）、下课弹窗延时、
      ClassIsland 课表导入。
    - **通用设置**：考试模式、服务器地址、NTP 校时、历史消息保留。
    """

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
        on_schedule_mode_changed: Callable[[str], None],
        on_break_delay_changed: Callable[[int], None],
        on_import_schedule: Callable[[], None],
        on_use_cib_schedule_changed: Callable[[bool], None],
        on_auto_refresh_schedule_changed: Callable[[bool], None],
        on_restart_bridge: Optional[Callable[[], None]] = None,
        on_sync_requested: Optional[Callable[[], None]] = None,
        on_view_schedule: Optional[Callable[[], None]] = None,
        on_reimport_schedule: Optional[Callable[[], None]] = None,
    ) -> None:
        super().__init__()
        self._config = config
        self._on_exam_mode_changed = on_exam_mode_changed
        self._on_server_url_changed = on_server_url_changed
        self._on_ntp_server_changed = on_ntp_server_changed
        self._on_retention_changed = on_retention_changed
        self._on_schedule_source_changed = on_schedule_source_changed
        self._on_reload_schedules = on_reload_schedules
        self._on_schedule_mode_changed = on_schedule_mode_changed
        self._on_break_delay_changed = on_break_delay_changed
        self._on_import_schedule = on_import_schedule
        self._on_use_cib_schedule_changed = on_use_cib_schedule_changed
        self._on_auto_refresh_schedule_changed = on_auto_refresh_schedule_changed
        self._on_restart_bridge = on_restart_bridge or (lambda: None)
        self._on_sync_requested = on_sync_requested or (lambda: None)
        self._on_view_schedule_requested = on_view_schedule or (lambda: None)
        self._on_reimport_requested = on_reimport_schedule or (lambda: None)
        self.setObjectName("settings_page")
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)
        layout.addWidget(SubtitleLabel("设置"))

        # 子页切换结构（后续新增子页只需再加一个 item + 一个 widget）
        # 每个子页各自套一层滚动区：设置项再多也不会把窗口顶出屏幕。
        self.pivot = SegmentedWidget(self)
        self.stack = QStackedWidget(self)
        self.timing_page = self._build_timing_page()
        self.general_page = self._build_general_page()
        self.timing_scroll = self._wrap_in_scroll_area(self.timing_page)
        self.general_scroll = self._wrap_in_scroll_area(self.general_page)
        self.stack.addWidget(self.timing_scroll)
        self.stack.addWidget(self.general_scroll)

        self.pivot.addItem(routeKey="timing", text="通知时机", onClick=lambda: self._switch_page(0))
        self.pivot.addItem(routeKey="general", text="通用设置", onClick=lambda: self._switch_page(1))
        self.pivot.setCurrentItem("timing")
        self._switch_page(0)

        layout.addWidget(self.pivot)
        layout.addWidget(self.stack, 1)

    def _wrap_in_scroll_area(self, page: QWidget) -> QScrollArea:
        """Put a sub-page inside a vertical scroll area.

        The scroll area reports a small size hint, so the window's minimum
        height stays modest no matter how many options a page accumulates.
        """
        area = QScrollArea(self)
        area.setObjectName(f"{page.objectName() or 'page'}_scroll")
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        area.setWidget(page)
        area.viewport().setAutoFillBackground(False)
        page.setAutoFillBackground(False)
        return area

    def _switch_page(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        # Always start a freshly opened page at the top.
        area = self.stack.currentWidget()
        if isinstance(area, QScrollArea):
            area.verticalScrollBar().setValue(0)

    # -- 状态卡（当前弹出方式 / 桥接器状态灯 / 重启） ---------------------

    def _build_status_card(self) -> QWidget:
        """Live view of what currently decides break popups.

        Answers the two questions users actually ask: "who decides it is a
        break right now?" and "is the ClassIsland bridge healthy?".
        """
        card = QFrame(self)
        card.setObjectName("status_card")
        card.setFrameShape(QFrame.Shape.NoFrame)
        card.setStyleSheet(
            "#status_card {"
            "  border: 1px solid rgba(120, 120, 140, 0.35);"
            "  border-radius: 10px;"
            "  background: rgba(120, 120, 140, 0.06);"
            "}"
        )
        layout = QVBoxLayout(card)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(6)

        source_row = QHBoxLayout()
        source_row.setSpacing(8)
        source_title = CaptionLabel("当前弹出方式")
        self.source_label = BodyLabel("检测中…")
        self.source_label.setObjectName("source_label")
        source_row.addWidget(source_title)
        source_row.addWidget(self.source_label)
        source_row.addStretch(1)
        layout.addLayout(source_row)

        self.source_hint = CaptionLabel("正在确定课间状态的来源…")
        self.source_hint.setWordWrap(True)
        layout.addWidget(self.source_hint)

        bridge_row = QHBoxLayout()
        bridge_row.setSpacing(8)
        bridge_title = CaptionLabel("桥接器状态")
        self.bridge_indicator = QLabel("●")
        self.bridge_indicator.setObjectName("bridge_indicator")
        self.bridge_indicator.setStyleSheet("color: #9ca3af; font-size: 15px;")
        self.bridge_label = BodyLabel("检测中…")
        self.bridge_label.setObjectName("bridge_label")
        self.restart_bridge_button = PushButton("重启桥接器")
        self.restart_bridge_button.setObjectName("restart_bridge_button")
        self.restart_bridge_button.clicked.connect(lambda: self._on_restart_bridge())
        bridge_row.addWidget(bridge_title)
        bridge_row.addWidget(self.bridge_indicator)
        bridge_row.addWidget(self.bridge_label)
        bridge_row.addStretch(1)
        bridge_row.addWidget(self.restart_bridge_button)
        layout.addLayout(bridge_row)

        self.bridge_hint = CaptionLabel("")
        self.bridge_hint.setWordWrap(True)
        layout.addWidget(self.bridge_hint)
        return card

    #: Indicator colours for :meth:`set_bridge_status`.
    _BRIDGE_COLORS = {
        "ok": "#10b981",
        "zombie": "#f59e0b",
        "stopped": "#ef4444",
        "blocked": "#ef4444",
        "unknown": "#9ca3af",
    }

    def set_schedule_status(self, source: str, hint: str) -> None:
        """Show which source currently triggers break popups."""
        self.source_label.setText(source)
        self.source_hint.setText(hint)

    def set_bridge_status(self, state: str, text: str, hint: str = "") -> None:
        """Update the bridge indicator (``ok``/``zombie``/``stopped``/``blocked``/``unknown``)."""
        key = str(state or "unknown").lower()
        color = self._BRIDGE_COLORS.get(key, self._BRIDGE_COLORS["unknown"])
        self.bridge_indicator.setStyleSheet(f"color: {color}; font-size: 15px;")
        self.bridge_label.setText(text)
        self.bridge_hint.setText(hint)

    def set_restart_bridge_enabled(self, enabled: bool, text: str = "重启桥接器") -> None:
        """Enable/disable the restart button (e.g. while a restart runs)."""
        self.restart_bridge_button.setEnabled(bool(enabled))
        self.restart_bridge_button.setText(text)

    # -- 通知时机子页 ------------------------------------------------------

    def _build_timing_page(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(12)

        # 0) 运行状态：当前弹出方式 + 桥接器状态灯 + 重启按钮
        layout.addWidget(self._build_status_card())

        # 1) 时间表来源：二选一（帮助标志放在两个选项右边，跟随当前选择）
        layout.addWidget(BodyLabel("时间表来源"))
        self.ci_mode_radio = RadioButton(
            "优先 ClassIsland 联动（未运行或连接失败时自动降级本地课表）"
        )
        self.local_mode_radio = RadioButton("仅使用 CBC 本地课表")

        if str(self._config.schedule_mode).lower() == "local":
            self.local_mode_radio.setChecked(True)
        else:
            self.ci_mode_radio.setChecked(True)
        self.ci_mode_radio.toggled.connect(self._change_schedule_mode)

        # 帮助标志放在两个来源选项的**右边**：无论选哪个都能在同一位置看到说明
        mode_row = QWidget()
        mode_row_layout = QHBoxLayout(mode_row)
        mode_row_layout.setContentsMargins(0, 0, 0, 0)
        mode_row_layout.setSpacing(6)
        radio_column = QVBoxLayout()
        radio_column.setContentsMargins(0, 0, 0, 0)
        radio_column.setSpacing(2)
        radio_column.addWidget(self.ci_mode_radio)
        radio_column.addWidget(self.local_mode_radio)
        mode_row_layout.addLayout(radio_column)
        self.schedule_mode_help = _help_icon("", mode_row)
        mode_row_layout.addWidget(self.schedule_mode_help, 0, Qt.AlignmentFlag.AlignVCenter)
        mode_row_layout.addStretch(1)
        layout.addWidget(mode_row)
        self._refresh_schedule_mode_hint()

        # 2) 本地课表（ClassIsland 导入）
        layout.addWidget(BodyLabel("本地课表"))
        self.import_button = PrimaryPushButton("从 ClassIsland 配置文件导入课表")
        self.import_button.clicked.connect(lambda: self._on_import_schedule())
        self.view_schedule_button = PushButton("查看当前课表")
        self.view_schedule_button.setObjectName("view_schedule_button")
        self.view_schedule_button.clicked.connect(lambda: self._on_view_schedule())
        # 仅当本机 ClassIsland 正在使用的课表与本地导入的不一致时才出现
        self.reimport_button = PrimaryPushButton("重新导入时间表")
        self.reimport_button.setObjectName("reimport_button")
        self.reimport_button.clicked.connect(lambda: self._on_reimport_schedule())
        self.reimport_button.setVisible(False)
        self.local_schedule_label = CaptionLabel("尚未导入本地课表")
        self.local_schedule_label.setWordWrap(True)
        import_row = QHBoxLayout()
        import_row.addWidget(self.import_button)
        import_row.addWidget(self.view_schedule_button)
        import_row.addWidget(self.reimport_button)
        import_row.addWidget(self.local_schedule_label, 1)
        layout.addLayout(import_row)

        # 2b) 是否允许用 CIB 时间表作为降级来源
        self.use_cib_schedule_checkbox = QCheckBox("启用 CIB 时间表")
        self.use_cib_schedule_checkbox.setChecked(bool(self._config.use_cib_schedule))
        self.use_cib_schedule_checkbox.stateChanged.connect(self._change_use_cib_schedule)
        cib_row = _labeled_row(self.use_cib_schedule_checkbox, self._use_cib_schedule_help())
        self.use_cib_schedule_help = getattr(cib_row, "help_icon", None)
        layout.addWidget(cib_row)
        self._refresh_use_cib_schedule_hint()

        # 2c) 课表变更时是否自动重新导入（否则启动时询问）
        self.auto_refresh_checkbox = QCheckBox("ClassIsland 课表变更时自动重新导入")
        self.auto_refresh_checkbox.setChecked(bool(self._config.auto_refresh_schedule))
        self.auto_refresh_checkbox.stateChanged.connect(self._change_auto_refresh_schedule)
        layout.addWidget(
            _labeled_row(
                self.auto_refresh_checkbox,
                "每次启动都会比对本机 ClassIsland 当前启用的课表与本地已导入的课表；"
                "不一致时提示你重新导入，勾选此项则直接自动更新、不再询问。",
            )
        )

        # 3) 下课延时
        layout.addWidget(BodyLabel("下课弹窗延时"))
        self.delay_spin = SpinBox()
        self.delay_spin.setRange(0, 300)
        self.delay_spin.setValue(int(self._config.break_popup_delay_seconds))
        self.delay_spin.setEnabled(True)
        self.delay_spin.valueChanged.connect(self._change_break_delay)
        layout.addWidget(
            _labeled_row(
                _field_row("下课延时", self.delay_spin, CaptionLabel("秒（0 = 下课立即弹窗）")),
                "仅作用于“下课那一刻”：收到下课信号后等这么久再弹窗（避免老师拖堂时打断课堂）。"
                "课间期间新到的消息会立刻弹窗，不受此延时影响。",
            )
        )

        # 4) 本地 JSON 时间表文件（作为降级/本地模式的兜底数据源）
        self.schedule_box = ComboBox()
        self.schedule_box.currentTextChanged.connect(self._change_schedule_source)
        self.schedule_refresh_button = PushButton("刷新时间表列表")
        self.schedule_refresh_button.clicked.connect(lambda: self._on_reload_schedules())
        layout.addWidget(
            _labeled_row(
                _field_row("降级用 JSON 时间表文件", self.schedule_box, self.schedule_refresh_button),
                "仅在上方选择“仅使用 CBC 本地课表”、或 ClassIsland 不可用降级时使用；"
                "已导入的课表优先于此文件。",
            )
        )

        layout.addStretch(1)
        return page

    def _use_cib_schedule_help(self) -> str:
        """“启用 CIB 时间表”的悬浮帮助文案（随开关状态变化）。"""
        if self.use_cib_schedule_checkbox.isChecked():
            return (
                "ClassIsland/CIB 失效时：优先用导入的课表触发课间弹窗，"
                "其次才用下面的 JSON 文件。"
            )
        return "已关闭：降级时忽略导入的课表，直接使用下面的本地 JSON 时间表文件。"

    def _refresh_schedule_mode_hint(self) -> None:
        """更新“时间表来源”的悬浮帮助（跟随当前选择）。"""
        if self.ci_mode_radio.isChecked():
            text = (
                "运行时后台检测 ClassIsland.Desktop.exe：进程不存在或桥接器连续失败时，"
                "自动改用下方导入的本地课表触发课间弹窗。"
            )
        else:
            text = "始终使用本地保存的课表，不连接 ClassIsland（适合未安装 ClassIsland 的教室）。"
        icon = getattr(self, "schedule_mode_help", None)
        if icon is not None:
            icon.setToolTip(_help_tooltip(text))

    # -- 通用设置子页 ------------------------------------------------------

    def _build_general_page(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(12)

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
        self.sync_button.clicked.connect(lambda: self._on_sync_clicked())
        layout.addWidget(self.sync_button)
        self.retention_box = ComboBox()
        for label in RETENTION_OPTIONS:
            self.retention_box.addItem(label)
        self.retention_box.setCurrentText(_retention_label(self._config.history_retention_days))
        self.retention_box.currentTextChanged.connect(self._change_retention)
        layout.addWidget(_field_row("历史消息保留", self.retention_box))

        layout.addStretch(1)
        return page

    def set_exam_mode(self, enabled: bool) -> None:
        self.exam_checkbox.blockSignals(True)
        self.exam_checkbox.setChecked(enabled)
        self.exam_checkbox.blockSignals(False)

    def _change_schedule_mode(self, _checked: bool) -> None:
        mode = "local" if self.local_mode_radio.isChecked() else "auto"
        self._refresh_schedule_mode_hint()
        self._on_schedule_mode_changed(mode)

    def _change_break_delay(self, value: int) -> None:
        self._on_break_delay_changed(int(value))

    def _change_use_cib_schedule(self, _state: int) -> None:
        enabled = self.use_cib_schedule_checkbox.isChecked()
        self._refresh_use_cib_schedule_hint()
        self._on_use_cib_schedule_changed(enabled)

    def _refresh_use_cib_schedule_hint(self) -> None:
        """更新“启用 CIB 时间表”的悬浮帮助（不再占据版面）。"""
        if getattr(self, "use_cib_schedule_help", None) is not None:
            self.use_cib_schedule_help.setToolTip(_help_tooltip(self._use_cib_schedule_help()))

    def set_use_cib_schedule(self, enabled: bool) -> None:
        """Reflect the persisted value without re-emitting signals."""
        self.use_cib_schedule_checkbox.blockSignals(True)
        self.use_cib_schedule_checkbox.setChecked(bool(enabled))
        self.use_cib_schedule_checkbox.blockSignals(False)
        self._refresh_use_cib_schedule_hint()

    def _change_auto_refresh_schedule(self, state: int) -> None:
        self._on_auto_refresh_schedule_changed(self.auto_refresh_checkbox.isChecked())

    def set_sync_state(self, syncing: bool) -> None:
        """反映后台校时状态：只在按钮上体现，不写解释性文字。"""
        self.sync_button.setEnabled(not syncing)
        self.sync_button.setText("正在校时…" if syncing else "立即校时")

    def _on_sync_clicked(self) -> None:
        self._on_sync_requested()

    def _on_view_schedule(self) -> None:
        """「查看当前课表」按钮：交给主窗口弹出对话框。

        这里必须包一层：PyQt6 中槽函数抛出异常会直接 abort 整个进程，
        而按钮回调是最容易写错接线的地方（曾经把回调接到不存在的方法上）。
        """
        try:
            self._on_view_schedule_requested()
        except Exception:  # pragma: no cover - 防御性
            import logging

            logging.getLogger("kg.client.main_window").exception("打开课表查看窗口失败")

    def _on_reimport_schedule(self) -> None:
        """「重新导入时间表」按钮：课表与 ClassIsland 不一致时手动触发。"""
        try:
            self._on_reimport_requested()
        except Exception:  # pragma: no cover - 防御性
            import logging

            logging.getLogger("kg.client.main_window").exception("重新导入课表失败")

    def set_schedule_outdated(self, outdated: bool, detail: str = "") -> None:
        """课表与 ClassIsland 不一致时显示「重新导入时间表」按钮。"""
        self.reimport_button.setVisible(bool(outdated))
        if outdated:
            self.reimport_button.setToolTip(
                _help_tooltip(detail or "本机 ClassIsland 正在使用的课表与本地导入的不一致，点击重新导入。")
            )

    def set_auto_refresh_schedule(self, enabled: bool) -> None:
        """Reflect the persisted value without re-emitting signals."""
        self.auto_refresh_checkbox.blockSignals(True)
        self.auto_refresh_checkbox.setChecked(bool(enabled))
        self.auto_refresh_checkbox.blockSignals(False)

    def set_schedule_mode(self, mode: str) -> None:
        """Reflect the persisted schedule mode without re-emitting signals.

        All buttons are blocked first: ``QRadioButton`` auto-exclusivity makes
        checking one button *uncheck* its sibling, which would otherwise fire
        the callback while its sibling's signals were already unblocked.
        """
        target = self.local_mode_radio if str(mode).lower() == "local" else self.ci_mode_radio
        buttons = (self.ci_mode_radio, self.local_mode_radio)
        for button in buttons:
            button.blockSignals(True)
        for button in buttons:
            button.setChecked(button is target)
        for button in buttons:
            button.blockSignals(False)
        self._refresh_schedule_mode_hint()

    def set_local_schedule_summary(self, schedule: Optional[StoredSchedule]) -> None:
        """Show which local timetable is stored (or that none is)."""
        if schedule is None or schedule.is_empty:
            self.local_schedule_label.setText("尚未导入本地课表（ClassIsland 不可用时将无法弹窗）")
            self.local_schedule_label.setTextColor("#c42b1c", "#c42b1c")
            return
        imported = f"导入于 {schedule.imported_at}" if schedule.imported_at else "已导入"
        self.local_schedule_label.setText(f"已导入：{schedule.describe()}，{imported}")
        self.local_schedule_label.setTextColor("#0f766e", "#4cc2ff")

    def set_break_popup_delay(self, seconds: int) -> None:
        self.delay_spin.blockSignals(True)
        self.delay_spin.setValue(int(seconds))
        self.delay_spin.blockSignals(False)

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
    def __init__(self, config: ClientConfig, *, chunked_startup: bool = False):
        super().__init__()
        self._config = config
        self._worker: Optional[ClientWorker] = None
        self._snapshot: Optional[ClientSnapshot] = None
        # Signatures of the currently rendered message lists (see
        # _messages_signature) — used to skip needless widget rebuilds.
        self._unread_signature: Optional[Tuple] = None
        self._history_signature: Optional[Tuple] = None
        self._last_time_sync: Optional[TimeSyncResult] = None
        self._last_time_sync_anchor: Optional[datetime] = None
        #: Background NTP sync (never blocks the GUI thread).
        self._ntp_thread: Optional[NtpSyncThread] = None
        self._force_close = False
        #: Set once ``exit_app()`` ran: background callbacks that complete
        #: afterwards must not resurrect threads (a CIB probe finishing right
        #: after shutdown used to start an orphaned monitor thread).
        self._shutting_down = False
        self._last_connected_state: Optional[bool] = None
        self._notified_break_key: Optional[str] = None
        self._schedule_sources: List[ScheduleSource] = []
        self._schedule_source: Optional[ScheduleSource] = None
        self._schedule_ranges: List[Tuple] = []
        self._reminder_timers: Dict[int, QTimer] = {}
        self._pending_read_ids: Set[int] = set()
        self._settling_read_ids: Set[int] = set()
        # Disk-backed queue of read receipts awaiting sync — survives shutdown
        # while offline, so no read is lost when the machine is powered off.
        self._pending_reads = PendingReadsStore()
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
        self._break_monitor.break_state_changed.connect(self._on_break_state_changed)

        # Local SQLite cache (lives in %APPDATA%/ClassBridge/)
        self._local_db = ClientDatabase()
        self._local_db.open()

        # ClassIsland real-time schedule monitor (started on demand)
        self._classisland_monitor: Optional[ClassIslandMonitor] = None
        # Locally persisted timetable — the fallback source when ClassIsland
        # is missing, and the sole source in "local" mode.
        self._schedule_store = ScheduleStore()
        # ClassIsland process watchdog (started by _start_ci_watchdog)
        self._ci_watchdog: Optional[CiProcessWatcher] = None
        self._ci_alive: Optional[bool] = None
        # Shown in the tray tooltip so the client's belief is inspectable.
        self._connection_text = ""
        self._schedule_source_label = ""
        #: True while ClassIsland live events drive break detection (vs. a timetable).
        self._schedule_live = False
        # UI responsiveness watch-dog + InfoBar rate limiting.
        self._recent_messages: Dict[str, float] = {}
        self._ui_activity = ""
        self._ui_activity_at = time.perf_counter()
        self._ui_last_heartbeat = time.perf_counter()
        self._ui_stall_timer: Optional[QTimer] = None
        self._monitor_reap_timer: Optional[QTimer] = None
        # CIB (ClassIsland.WSBridge) supervision state.
        #   None = not checked yet, True = usable, False = known unavailable
        self._cib_ready: Optional[bool] = None
        self._cib_supervisor: Optional[CibSupervisor] = None
        self._cib_degrade_announced = False
        # Bridge revival (zombie CIB whose IPC died when ClassIsland restarted).
        self._bridge_revival: Optional[BridgeRevival] = None
        self._bridge_revival_at = 0.0
        self._bridge_revival_attempts = 0
        # Imported-timetable freshness check (see _check_schedule_freshness).
        self._pending_schedule_plan: Optional[Tuple[str, object]] = None
        self._declined_schedule_layouts: Set[str] = set()
        #: Monitor threads that were asked to stop but have not finished yet.
        self._retiring_monitors: List[ClassIslandMonitor] = []
        # Start-up splash (registered by client/app.py via attach_splash).
        self._splash = None
        self._startup_ready = False
        #: True while the splash is on screen (notices are parked meanwhile).
        self._splash_active = False
        self._held_notices: List[Tuple[str, str]] = []
        #: 本次课间已经提示过的未读消息 id（防止每个快照都重复弹窗）
        self._notified_break_ids: Set[int] = set()
        #: 考试静默模式（服务端/设置页设置）；开启期间不弹任何窗口
        self._exam_mode = False
        self._exam_notice_shown = False
        # True while the bridge is reachable but cannot report class/break
        # (no timetable loaded/enabled): breaks are then derived from a
        # timetable so the reported state is not stuck on "in class".
        self._ci_state_unusable = False
        # Deferred break popup (下课延时弹窗)
        self._break_popup_timer = QTimer(self)
        self._break_popup_timer.setSingleShot(True)
        self._break_popup_timer.timeout.connect(self._show_deferred_break_popup)
        self._deferred_break_popup: Optional[Tuple[str, int]] = None
        #: Next subject of the break being announced (shown in the info bar).
        self._pending_break_subject = ""
        # Break state (None = not determined yet) and the start-up visibility
        # decision: the window only appears on start-up when a break is running
        # *and* there are unread messages.
        self._break_state_known: Optional[bool] = None
        self._startup_decision_pending = True
        self._startup_timer = QTimer(self)
        self._startup_timer.setSingleShot(True)
        self._startup_timer.timeout.connect(lambda: self._decide_startup_visibility(timed_out=True))

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
            on_schedule_mode_changed=self._change_schedule_mode,
            on_break_delay_changed=self._change_break_delay,
            on_import_schedule=self._import_schedule_from_classisland,
            on_use_cib_schedule_changed=self._change_use_cib_schedule,
            on_auto_refresh_schedule_changed=self._change_auto_refresh_schedule,
            on_restart_bridge=self._restart_bridge_from_settings,
            on_sync_requested=self._sync_time,
            on_view_schedule=self._on_view_schedule,
            on_reimport_schedule=self._on_reimport_schedule,
        )

        self._init_window()
        self._init_tray()
        self.settings_page.set_local_schedule_summary(self._schedule_store.current())
        self._reload_schedule_sources()
        self._start_worker()
        self._sync_time()
        self._break_monitor.start()
        # Chunked start-up (used by client/app.py): the window shell exists
        # already, but the remaining work runs in separate event-loop turns so
        # the splash animation keeps rendering between slices.  Tests and other
        # callers keep the synchronous behaviour (everything ready on return).
        self._chunked_startup = bool(chunked_startup)
        if self._chunked_startup:
            logger.info("Chunked start-up enabled: remaining work is sliced")
            QTimer.singleShot(0, self._startup_step_schedule_sources)
            QTimer.singleShot(0, self._startup_step_watchdog)
            QTimer.singleShot(0, self._startup_step_time_sync)
        else:
            self._reload_schedule_sources()
            self._start_ci_watchdog()
            self._sync_time()
            installed = install_auto_hide_scrollbars(self)
            logger.debug("Auto-hiding scrollbars installed on %s scroll area(s)", installed)

    # -- chunked start-up slices ------------------------------------------

    def _startup_step_schedule_sources(self) -> None:
        """Slice 1: scan timetable sources and apply the initial mode."""
        if self._shutting_down:
            return
        try:
            self._reload_schedule_sources()
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("Start-up slice failed (schedule sources): %s", exc)

    def _startup_step_watchdog(self) -> None:
        """Slice 2: start the ClassIsland watchdog and install scrollbars."""
        if self._shutting_down:
            return
        try:
            self._start_ci_watchdog()
            install_auto_hide_scrollbars(self)
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("Start-up slice failed (watchdog): %s", exc)

    def _startup_step_time_sync(self) -> None:
        """Slice 3: kick off the (background) NTP sync."""
        if self._shutting_down:
            return
        try:
            self._sync_time()
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("Start-up slice failed (time sync): %s", exc)

    def _init_window(self) -> None:
        self.resize(1080, 760)
        # The settings sub-pages scroll, so the window no longer has to be tall
        # enough to show every option at once (it used to grow with each new
        # setting and could end up taller than the screen).
        self.setMinimumSize(880, 560)
        self.setWindowTitle("ClassBridge 客户端")
        self._apply_app_icon()
        if hasattr(self, "setMicaEffectEnabled"):
            try:
                self.setMicaEffectEnabled(True)
            except Exception:
                pass
        self.addSubInterface(self.unread_page, FIF.MAIL, "未读消息")
        self.addSubInterface(self.history_page, FIF.HISTORY, "历史消息")
        self.addSubInterface(self.settings_page, FIF.SETTING, "设置")
        if hasattr(self, "stackedWidget"):
            self.stackedWidget.currentChanged.connect(self._on_interface_changed)
        self._update_window_title(False, ClientMode.NORMAL)

    def _apply_app_icon(self) -> None:
        icon_path = ICON_ICO_PATH if ICON_ICO_PATH.exists() else ICON_PNG_PATH
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))

    def _init_tray(self) -> None:
        self.tray = QSystemTrayIcon(self.windowIcon(), self)
        self.tray.setToolTip(APP_NAME)
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
        self._start_ui_stall_watch()
        # Retired monitor threads are reaped here instead of being waited for on
        # the UI thread (that wait used to freeze the window for ~2s whenever
        # the timetable source changed).
        self._monitor_reap_timer = QTimer(self)
        self._monitor_reap_timer.setInterval(1500)
        self._monitor_reap_timer.timeout.connect(self._reap_retired_monitors)
        self._monitor_reap_timer.start()

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

    # ------------------------------------------------------------------
    # ClassIsland real-time schedule monitor
    # ------------------------------------------------------------------

    def _start_classisland_monitor(self) -> None:
        """Launch (or restart) the ClassIsland WebSocket monitor."""
        if self._shutting_down:
            logger.debug("Ignoring monitor start request: the window is shutting down")
            return
        self._stop_classisland_monitor(wait=False)
        self._classisland_monitor = ClassIslandMonitor(
            ws_url=self._config.classisland_ws_url,
            parent=self,
        )
        self._classisland_monitor.break_started.connect(self._on_classisland_break_started)
        self._classisland_monitor.class_started.connect(self._on_classisland_class_started)
        self._classisland_monitor.state_synced.connect(self._on_ci_state_synced)
        self._classisland_monitor.state_unavailable.connect(self._on_ci_state_unavailable)
        self._classisland_monitor.connection_changed.connect(self._on_classisland_connection_changed)
        self._classisland_monitor.error_occurred.connect(self._show_warning)
        self._classisland_monitor.fallback_needed.connect(self._on_classisland_fallback_needed)
        self._classisland_monitor.start()
        logger.info(
            "ClassIsland monitor started: %s",
            self._config.classisland_ws_url,
        )

    def _stop_classisland_monitor(self, *, wait: bool = True) -> None:
        """Stop the ClassIsland monitor if it is running.

        ``wait=False`` (the UI path) only *asks* the thread to stop: it is
        disconnected, parked in ``_retiring_monitors`` so Qt never destroys a
        running thread, and reaped by a timer.  Blocking here used to freeze the
        window for up to 3 s — the reported "switching the timetable source
        always stalls for ~2 s" and part of the drag stutter.
        """
        monitor = self._classisland_monitor
        if monitor is None:
            return
        self._classisland_monitor = None
        self._disconnect_monitor(monitor)
        monitor.stop()
        if not wait:
            self._retiring_monitors.append(monitor)
            self._reap_retired_monitors()
            return
        if not monitor.wait(3000):
            monitor.terminate()
            monitor.wait(1000)

    def _disconnect_monitor(self, monitor) -> None:
        """Detach a monitor's signals so a dying thread cannot touch the UI."""
        with contextlib.suppress(Exception):
            monitor.break_started.disconnect()
        with contextlib.suppress(Exception):
            monitor.class_started.disconnect()
        with contextlib.suppress(Exception):
            monitor.state_synced.disconnect()
        with contextlib.suppress(Exception):
            monitor.state_unavailable.disconnect()
        with contextlib.suppress(Exception):
            monitor.connection_changed.disconnect()
        with contextlib.suppress(Exception):
            monitor.error_occurred.disconnect()
        with contextlib.suppress(Exception):
            monitor.fallback_needed.disconnect()

    def _reap_retired_monitors(self) -> None:
        """Drop monitor threads that have finished (keeps references alive)."""
        pending = []
        for monitor in self._retiring_monitors:
            try:
                running = monitor.isRunning()
            except RuntimeError:
                # The C++ object is already gone.
                continue
            if running:
                pending.append(monitor)
            else:
                logger.debug("Retired ClassIsland monitor finished")
        self._retiring_monitors = pending

    # ------------------------------------------------------------------
    # ClassIsland 存活检测 + 课表降级
    # ------------------------------------------------------------------

    def _start_ci_watchdog(self) -> None:
        """Start the background ClassIsland process watchdog.

        Detection is deliberately left to the worker thread: enumerating every
        process with psutil can take 1-2 seconds, and doing it here used to
        freeze the UI during start-up.
        """
        if self._ci_watchdog is not None:
            return
        watchdog = CiProcessWatcher(parent=self)
        watchdog.alive_changed.connect(self._on_ci_alive_changed)
        self._ci_watchdog = watchdog
        watchdog.start()
        logger.info("ClassIsland watchdog started (first check runs in the background)")

    def _stop_ci_watchdog(self) -> None:
        if self._ci_watchdog is None:
            return
        self._ci_watchdog.stop()
        if not self._ci_watchdog.wait(3000):
            self._ci_watchdog.terminate()
            self._ci_watchdog.wait(1000)
        self._ci_watchdog = None

    def _stop_cib_supervisor(self) -> None:
        """Stop a pending CIB check (its confirmation prompt is auto-declined)."""
        revival = self._bridge_revival
        if revival is not None:
            if revival.isRunning():
                if not revival.wait(3000):
                    revival.terminate()
                    revival.wait(1000)
            self._bridge_revival = None
        supervisor = self._cib_supervisor
        if supervisor is None:
            return
        if supervisor.isRunning():
            # Unblock any pending confirmation wait so the thread can finish.
            supervisor.provide_confirmation(False)
            if not supervisor.wait(3000):
                supervisor.terminate()
                supervisor.wait(1000)
        self._cib_supervisor = None

    def _on_ci_alive_changed(self, alive: bool) -> None:
        """ClassIsland appeared/disappeared — re-evaluate the schedule source."""
        if self._ci_alive == alive:
            return
        self._ci_alive = alive
        logger.info("ClassIsland process alive=%s", alive)
        if str(self._config.schedule_mode).lower() == "local":
            return
        # Re-evaluate the bridge: ClassIsland may have restarted, or CIB may
        # now be launchable again.
        self._cib_ready = None
        self._cib_degrade_announced = False
        # A (re)started ClassIsland gets a fresh chance to report its state.
        self._ci_state_unusable = False
        reason = (
            "ClassIsland 已启动，恢复实时联动"
            if alive
            else "检测到 ClassIsland 未运行，已自动降级为本地课表"
        )
        self._apply_schedule_mode(announce=True, reason=reason)

    def _apply_schedule_mode(self, *, announce: bool = False, reason: str = "") -> None:
        """Single entry point deciding between CI events and a local timetable.

        Priority:
        1. ``local`` mode → always the locally saved timetable.
        2. ClassIsland process known to be missing → local timetable.
        3. Otherwise → live ClassIsland events (via the CIB bridge), which
           themselves fall back to the local timetable if CIB is unavailable.

        The "本地 JSON 时间表文件" dropdown only selects *which* file backs the
        local timetable (used by ``local`` mode or by a degradation); it never
        overrides the mode the user picked.
        """
        self._note_ui_activity("apply-schedule")
        mode = str(self._config.schedule_mode).lower()

        if mode == "local":
            use_ci = False
            ranges, detail = self._local_fallback_ranges()
            reason = reason or "仅使用本地课表"
        elif self._ci_alive is False:
            use_ci = False
            ranges, detail = self._local_fallback_ranges()
            reason = reason or "ClassIsland 未运行，已自动降级为本地课表"
        else:
            use_ci = True
            if self._ci_state_unusable:
                # The bridge is reachable but cannot report a class/break state
                # (no timetable loaded/enabled, or an unusable CurrentState).
                # Keep it connected for events, but drive the state from a
                # timetable so breaks are still detected and reported.
                ranges, detail = self._local_fallback_ranges()
                reason = reason or "ClassIsland 未加载课表，已按课表推算课间状态"
            else:
                ranges = []
                detail = "ClassIsland 实时联动"

        self._schedule_ranges = list(ranges)
        self._schedule_source_label = detail
        self._schedule_live = bool(use_ci) and not self._ci_state_unusable
        self._refresh_tray_tooltip()
        self._break_monitor.update_schedule_ranges(self._schedule_ranges)
        self._refresh_settings_status()

        if use_ci:
            # Live events need the CIB bridge up; that check runs in the
            # background and falls back to the local timetable when it fails.
            self._start_cib_then_monitor()
        else:
            self._stop_classisland_monitor(wait=False)

        logger.info(
            "Schedule source applied: mode=%s use_ci=%s ranges=%s detail=%s",
            mode,
            use_ci,
            len(self._schedule_ranges),
            detail,
        )
        if announce:
            self._announce_schedule_source(
                use_ci=use_ci, detail=detail, ranges=self._schedule_ranges, reason=reason
            )

    # ------------------------------------------------------------------
    # CIB (ClassIsland.WSBridge) supervision
    # ------------------------------------------------------------------

    def _start_cib_then_monitor(self) -> None:
        """Enable live ClassIsland events once CIB is verified/launched."""
        if self._shutting_down:
            return
        if self._cib_ready is False:
            # Already known to be unavailable — stay degraded.
            self._degrade_to_local_schedule()
            return

        if self._cib_ready is True:
            self._start_classisland_monitor()
            return

        if self._cib_supervisor is not None and self._cib_supervisor.isRunning():
            logger.debug("CIB supervision already in progress")
            return

        logger.info("Checking ClassIsland bridge (CIB) availability")
        supervisor = CibSupervisor(
            exe_path=self._config.cib_exe_path or None,
            url=self._config.classisland_ws_url,
            parent=self,
        )
        supervisor.confirm_kill_requested.connect(self._on_cib_confirm_kill)
        supervisor.completed.connect(self._on_cib_supervisor_finished)
        self._cib_supervisor = supervisor
        supervisor.start()

    def _on_cib_confirm_kill(self, process_name: str, pid: int) -> None:
        """Ask the user whether the process holding port 6614 may be killed."""
        text = (
            f"端口 {cib_daemon.CIB_PORT} 目前被进程 [{process_name} (PID: {pid})] 占用，"
            "是否强制结束该进程以启动 ClassIsland 桥接器？"
        )
        logger.warning("Port %s conflict: %s (PID %s)", cib_daemon.CIB_PORT, process_name, pid)
        try:
            box = MessageBox("端口占用", text, self)
            approved = box.exec() == QDialog.DialogCode.Accepted
        except Exception:
            logger.exception("Failed to show the port-conflict dialog; refusing")
            approved = False

        supervisor = self._cib_supervisor
        if supervisor is not None:
            supervisor.provide_confirmation(approved)

    def _on_cib_supervisor_finished(self, result) -> None:
        """Handle the CIB check result: enable live events or degrade."""
        self._cib_supervisor = None

        if self._shutting_down:
            logger.debug("CIB check finished after shutdown — nothing to apply")
            return

        if result is not None and result.ok:
            self._cib_ready = True
            logger.info("CIB ready: %s", result.message)
            self._refresh_settings_status()
            # The probe runs in the background, so the situation may have
            # changed while it was in flight: the watchdog (or the user) can
            # have decided to degrade in the meantime.  Starting live events
            # then would resurrect a monitor whose ClassIsland is gone — the
            # exact source of "bot says class while it is a break".
            if self._ci_alive is False or str(self._config.schedule_mode).lower() == "local":
                logger.info(
                    "CIB is ready but ClassIsland is unavailable (alive=%s) — staying degraded",
                    self._ci_alive,
                )
                self._degrade_to_local_schedule()
                return
            self._start_classisland_monitor()
            return

        self._cib_ready = False
        message = result.message if result is not None else "ClassIsland 桥接器不可用。"
        logger.warning("CIB unavailable: %s", message)

        if not self._cib_degrade_announced:
            self._cib_degrade_announced = True
            if isinstance(result, cib_daemon.CibEnsureResult):
                self._show_warning(cib_daemon.describe_degradation(result))
            else:
                self._show_warning(f"{message} 已降级为本地静态课表模式。")

        self._degrade_to_local_schedule()

    def _degrade_to_local_schedule(self) -> None:
        """Use the locally stored timetable without changing the saved mode."""
        self._stop_classisland_monitor(wait=False)
        ranges, detail = self._local_fallback_ranges()
        self._schedule_ranges = list(ranges)
        self._schedule_source_label = detail
        self._schedule_live = False
        self._refresh_tray_tooltip()
        self._break_monitor.update_schedule_ranges(self._schedule_ranges)
        self._refresh_settings_status()
        logger.info("Degraded to local timetable: %s (%s breaks)", detail, len(self._schedule_ranges))

    def _local_fallback_ranges(self) -> Tuple[List[Tuple], str]:
        """Break ranges for local operation.

        When ``use_cib_schedule`` is enabled the timetable imported from
        ClassIsland ("CIB 时间表") is preferred; disabling it skips straight to
        the explicitly selected JSON schedule file.

        导入的课表用 ``non_class_ranges()``（上课时段取补集），因此第一节课之前与
        最后一节课之后同样算课间、可以弹窗；纯 JSON 课表只有 break 列表，保持原样。
        """
        if self._config.use_cib_schedule and self._schedule_store.has_schedule:
            schedule = self._schedule_store.current()
            return (
                self._schedule_store.non_class_ranges(),
                schedule.describe() if schedule is not None else "CIB 时间表",
            )

        source = self._schedule_source
        if source is not None and not source.is_classisland:
            ranges, _ = validate_schedule_file(source.path)
            if ranges:
                return ranges, source.label

        # Nothing usable: if the CIB timetable exists but was disabled, say so
        # instead of leaving the user wondering why it is empty.
        if not self._config.use_cib_schedule and self._schedule_store.has_schedule:
            logger.info("CIB timetable disabled by configuration; no local source available")
        return [], "无可用课表"

    def _announce_schedule_source(
        self,
        *,
        use_ci: bool,
        detail: str,
        ranges: List[Tuple],
        reason: str,
    ) -> None:
        """Show the user which schedule source is now active."""
        if use_ci and not reason:
            return
        if use_ci:
            message = f"{reason}（课表来源：ClassIsland 实时联动）"
        elif ranges:
            message = f"{reason}（课表来源：{detail}，共 {len(ranges)} 个课间）"
        else:
            message = f"{reason}，但未找到可用课表，请先在“设置 → 通知时机”中导入课表。"
        if self._hold_notice(("info", message)):
            return
        InfoBar.info(
            title="课表来源",
            content=message,
            orient=Qt.Orientation.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP_RIGHT,
            duration=6000,
            parent=self,
        )

    def _change_schedule_mode(self, mode: str) -> None:
        mode = "local" if str(mode).lower() == "local" else "auto"
        if self._config.schedule_mode == mode:
            return
        self._config.schedule_mode = mode
        save_client_config(self._config)
        logger.info("Schedule mode changed to %s", mode)
        self._apply_schedule_mode(announce=True)

    def _change_break_delay(self, seconds: int) -> None:
        seconds = max(0, int(seconds))
        if self._config.break_popup_delay_seconds == seconds:
            return
        self._config.break_popup_delay_seconds = seconds
        save_client_config(self._config)
        logger.info("Break popup delay set to %ss", seconds)

        # A popup may be waiting on the *old* delay — re-apply it with the new
        # value so it can never fire at a stale moment.
        pending = self._deferred_break_popup
        if pending is not None and self._break_popup_timer.isActive():
            self._break_popup_timer.stop()
            if seconds <= 0:
                self._deferred_break_popup = None
                self._show_break_popup(*pending)
            else:
                self._break_popup_timer.start(seconds * 1000)

    def _change_use_cib_schedule(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if self._config.use_cib_schedule == enabled:
            return
        self._config.use_cib_schedule = enabled
        save_client_config(self._config)
        logger.info("use_cib_schedule set to %s", enabled)
        # Re-evaluate the active source: this setting changes which timetable
        # backs a degradation.
        self._apply_schedule_mode()

    def _change_auto_refresh_schedule(self, enabled: bool) -> None:
        """Persist the "refresh the imported timetable automatically" flag."""
        enabled = bool(enabled)
        if self._config.auto_refresh_schedule == enabled:
            return
        self._config.auto_refresh_schedule = enabled
        save_client_config(self._config)
        logger.info("auto_refresh_schedule set to %s", enabled)

    def _import_schedule_from_classisland(self) -> None:
        """Open the 3-step import wizard and persist the chosen timetable."""
        wizard = ScheduleImportWizard(parent=self)
        if wizard.exec() != QDialog.DialogCode.Accepted:
            logger.info("ClassIsland schedule import cancelled by user")
            return

        schedule = wizard.imported_schedule
        if schedule is None:
            return

        self._schedule_store.save(schedule)
        self.settings_page.set_local_schedule_summary(schedule)
        logger.info("Imported local schedule: %s", schedule.describe())

        if self._ci_alive is False or str(self._config.schedule_mode).lower() == "local":
            self._apply_schedule_mode(announce=True, reason="本地课表已更新")
        else:
            self._show_info(f"课表已导入：{schedule.describe()}")

    def _on_view_schedule(self) -> None:
        """查看当前导入的课表（只读对话框）。"""
        from .schedule_view import ScheduleViewDialog

        try:
            schedule = self._schedule_store.current()
            ranges = self._schedule_store.non_class_ranges() if schedule is not None else []
            dialog = ScheduleViewDialog(schedule, non_class_ranges=ranges, parent=self)
            dialog.exec()
        except Exception:
            # 槽函数里的异常会终止进程，这里兜住并提示用户
            logger.exception("打开「查看当前课表」失败")
            self._show_warning("打开课表查看窗口失败，详情见客户端日志。")

    def _on_classisland_break_started(self, next_subject: str) -> None:
        """Called when ClassIsland signals the start of a break.

        The popup goes through the *same* path as the local timetable monitor,
        so the "延时弹窗" setting now applies to ClassIsland events too (this
        handler used to call ``show_normal()`` immediately, which is why a 5 s
        delay appeared to do nothing in the classroom) and the window is no
        longer raised when there is nothing to read.
        """
        logger.info(
            "ClassIsland break started: next=%s active_window=%s visible=%s",
            next_subject,
            self.isActiveWindow(),
            self.isVisible(),
        )
        self._set_break_state(True)
        self._pending_break_subject = next_subject

        unread_count = self._current_unread_count()
        if unread_count <= 0:
            logger.info("Break started (%s) with no unread messages — staying in the tray", next_subject)
            with contextlib.suppress(Exception):
                self.tray.showMessage("课间休息", f"下节课：{next_subject}")
            return

        self._on_break_popup_requested(self._ci_break_key(), unread_count)

    def _ci_break_key(self) -> str:
        """Dedup key for a break announced by a ClassIsland event.

        Mirrors the local monitor's ``start-end`` key whenever the timetable
        also covers this break, so a break announced by *both* sources (which
        happens while a degraded timetable is active) is announced only once.
        """
        now = datetime.now()
        current = now.time()
        for start, end in self._schedule_ranges:
            if start <= current <= end:
                return f"{start.isoformat()}-{end.isoformat()}"
        return f"ci-{now.strftime('%Y-%m-%d %H:%M')}"

    def _on_classisland_class_started(self) -> None:
        """Called when ClassIsland signals the start of a class period."""
        logger.info("ClassIsland: class started")
        self._set_break_state(False)

    def _on_ci_state_synced(self, in_break: bool) -> None:
        """Authoritative state read from the bridge (``CurrentState``).

        Emitted on every calibration poll, so it also covers the case where the
        client starts *during* a break (no transition event ever arrives).
        """
        if self._ci_state_unusable:
            # The bridge answers again — hand class/break detection back to it.
            self._ci_state_unusable = False
            logger.info("ClassIsland bridge reports a usable state again; live events restored")
            self._apply_schedule_mode(announce=False)
        self._set_break_state(in_break)
        self._refresh_settings_status()

    def _on_ci_state_unavailable(self) -> None:
        """The bridge is up but cannot tell class from break.

        ClassIsland reports ``CurrentState=None`` whenever no timetable is
        loaded/enabled, which is *not* "in class".  Without this the client
        would silently keep its initial value forever, so the QQ bot would keep
        answering "当前正在上课" during breaks.  Degrade to a timetable instead.
        """
        if self._ci_state_unusable:
            return
        self._ci_state_unusable = True
        logger.warning(
            "ClassIsland bridge gave no usable class/break state; "
            "deriving break/class from the timetable instead"
        )
        self._apply_schedule_mode(
            announce=True, reason="ClassIsland 未加载课表，已按课表推算课间状态"
        )
        # The usual cause is a *zombie* bridge: ClassIsland restarted, the IPC
        # link died, and the bridge keeps answering with default values ever
        # after.  Restart it in the background so live events can come back.
        self._schedule_bridge_revival()

    # -- zombie bridge revival ---------------------------------------------

    def _refresh_settings_status(self) -> None:
        """Feed the settings page's status card (source + bridge health)."""
        page = getattr(self, "settings_page", None)
        if page is None:
            return
        mode = str(self._config.schedule_mode).lower()
        live = bool(self._schedule_live)
        ranges = len(self._schedule_ranges)
        detail = self._schedule_source_label or "未知"

        if mode == "local":
            source = "本地课表"
            hint = f"已选择「仅使用本地课表」，共 {ranges} 个课间（{detail}）。"
        elif live and not self._ci_state_unusable:
            source = "CI 实时联动"
            hint = "课间由 ClassIsland 事件/状态直接驱动，最准确。"
        elif self._ci_state_unusable:
            source = "CIB 导入课表"
            hint = (
                f"ClassIsland 当前读不到课程状态（桥接器可能已与 ClassIsland 脱钩），"
                f"已改用导入的课表推算，共 {ranges} 个课间（{detail}）。"
            )
        elif self._ci_alive is False:
            source = "本地课表"
            hint = f"ClassIsland 未运行，已降级为本地课表，共 {ranges} 个课间（{detail}）。"
        elif ranges:
            source = "本地课表"
            hint = f"已降级为本地课表，共 {ranges} 个课间（{detail}）。"
        else:
            source = "无可用课表"
            hint = "尚未导入课表，课间弹窗不会触发；请先导入 ClassIsland 课表。"

        self.settings_page.set_schedule_status(source, hint)
        state, text, bridge_hint = self._bridge_status_snapshot()
        self.settings_page.set_bridge_status(state, text, bridge_hint)

    def _bridge_status_snapshot(self) -> Tuple[str, str, str]:
        """Best-effort description of the bridge for the settings indicator."""
        if self._bridge_revival is not None and self._bridge_revival.isRunning():
            return "unknown", "正在重启…", "已结束旧进程，正在等待新桥接器应答。"

        process = cib_daemon.find_bridge_process()
        if self._cib_ready is False:
            return (
                "stopped",
                "不可用",
                "桥接器未运行或无法连接；可点击右侧按钮尝试启动/重启。",
            )
        if self._ci_state_unusable:
            where = f"（{process[0]} PID {process[1]}）" if process else ""
            return (
                "zombie",
                "读不到课表" + where,
                "桥接器在应答，但已与 ClassIsland 脱钩：可点击「重启桥接器」让它重新连接。",
            )
        if self._cib_ready:
            where = f"（{process[0]} PID {process[1]}）" if process else ""
            return "ok", "正常" + where, "桥接器工作正常，正在提供 ClassIsland 实时状态。"
        if process is not None:
            return (
                "unknown",
                f"检测中（{process[0]} PID {process[1]}）",
                "桥接器进程存在，正在探测其能力。",
            )
        return (
            "unknown",
            "未检测到桥接器进程",
            "客户端会按需自动启动桥接器；也可以点右侧按钮手动启动/重启。",
        )

    def _restart_bridge_from_settings(self) -> None:
        """Manual restart of the bridge, from the settings page button."""
        if self._shutting_down:
            return
        box = MessageBox(
            "重启桥接器",
            "将结束当前的桥接器进程并重新启动，使它与 ClassIsland 重新建立连接。\n\n"
            "注意：只会在识别出桥接器进程时执行，不会结束其它进程。",
            self,
        )
        box.yesButton.setText("重启")
        box.cancelButton.setText("取消")
        if box.exec() != QDialog.DialogCode.Accepted:
            return
        self.settings_page.set_restart_bridge_enabled(False, "正在重启…")
        # The cooldown exists to stop automatic retry loops; a manual click must
        # always be able to force one.
        self._schedule_bridge_revival(force=True)

    def _schedule_bridge_revival(self, *, force: bool = False) -> None:
        """Restart an unresponsive bridge (rate limited unless ``force``)."""
        if self._shutting_down:
            self.settings_page.set_restart_bridge_enabled(True)
            return
        if self._ci_alive is False:
            if force:
                self._show_warning("ClassIsland 未运行，重启桥接器也无法恢复实时联动")
            logger.info("Not reviving the bridge: ClassIsland itself is not running")
            self.settings_page.set_restart_bridge_enabled(True)
            return
        if self._bridge_revival is not None and self._bridge_revival.isRunning():
            logger.debug("Bridge revival already in progress")
            return
        now = time.monotonic()
        if not force and now - self._bridge_revival_at < _BRIDGE_REVIVAL_COOLDOWN_SECONDS:
            logger.info(
                "Bridge revival still cooling down (%.0fs left)",
                _BRIDGE_REVIVAL_COOLDOWN_SECONDS - (now - self._bridge_revival_at),
            )
            return
        self._bridge_revival_at = now
        self._bridge_revival_attempts += 1
        logger.warning(
            "Restarting the ClassIsland bridge to restore live events (attempt %s%s)",
            self._bridge_revival_attempts,
            ", manual" if force else "",
        )
        self._refresh_settings_status()
        revival = BridgeRevival(
            exe_path=self._config.cib_exe_path or None,
            url=self._config.classisland_ws_url,
            parent=self,
        )
        revival.completed.connect(self._on_bridge_revival_finished)
        self._bridge_revival = revival
        revival.start()

    def _on_bridge_revival_finished(self, result) -> None:
        """Handle the result of a bridge restart."""
        self._bridge_revival = None
        self.settings_page.set_restart_bridge_enabled(True)
        if result is None or not result.ok:
            message = result.message if result is not None else "未知错误"
            logger.warning("Bridge revival failed: %s", message)
            self._show_warning(f"桥接器重启失败：{message}")
            self._refresh_settings_status()
            return

        logger.info("Bridge revival succeeded: %s", result.message)
        self._show_info("ClassIsland 桥接器已重启，正在恢复实时联动")
        # Reconnect immediately instead of waiting for the monitor's back-off;
        # the state stays timetable-driven until a usable read arrives.
        if self._ci_alive is not False and not self._shutting_down:
            self._start_classisland_monitor()
        self._refresh_settings_status()

    def _on_classisland_connection_changed(self, connected: bool, text: str) -> None:
        """Update the window title with ClassIsland connection status."""
        if connected:
            logger.info("ClassIsland bridge connected")
        else:
            logger.warning("ClassIsland bridge disconnected: %s", text)
        self._refresh_settings_status()

    def _on_classisland_fallback_needed(self) -> None:
        """ClassIsland events are unusable — fall back to a local timetable.

        The imported timetable is preferred; if none was imported we keep the
        previous behaviour of switching to the first usable JSON schedule
        file.  Shows a persistent error bar the user must dismiss."""
        self._stop_classisland_monitor(wait=False)

        local_ranges, detail = self._local_fallback_ranges()
        if local_ranges:
            self._schedule_ranges = local_ranges
            self._break_monitor.update_schedule_ranges(local_ranges)
            logger.info("Fell back to local timetable: %s", detail)
            InfoBar.error(
                title="ClassIsland 连接失败",
                content=(
                    f"ClassIsland 桥接器连续 5 次连接失败，已自动回退至 {detail}"
                    f"（{len(local_ranges)} 个课间）。请检查 ClassIsland 及桥接器是否正常运行。"
                ),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP_RIGHT,
                duration=-1,  # never auto-close
                parent=self,
            )
            return

        # No imported timetable — find the first working JSON schedule source.
        fallback_source: Optional[ScheduleSource] = None
        for source in self._schedule_sources:
            if source.is_classisland:
                continue
            ranges, error = validate_schedule_file(source.path)
            if ranges:
                fallback_source = source
                break

        if fallback_source is not None:
            self._schedule_source = fallback_source
            self._schedule_ranges, _ = validate_schedule_file(fallback_source.path)
            self._break_monitor.update_schedule_ranges(self._schedule_ranges)
            self.settings_page.set_schedule_options(self._schedule_sources, fallback_source.key)
            self._persist_schedule_selection(fallback_source.key, mark_valid=True)
            logger.info(
                "Fell back to JSON schedule: %s (%s)",
                fallback_source.label,
                fallback_source.key,
            )

        # Persistent error — stays until the user clicks the close button.
        InfoBar.error(
            title="ClassIsland 连接失败",
            content=(
                "ClassIsland 桥接器连续 5 次连接失败，已自动回退至 "
                f"{fallback_source.label if fallback_source else 'JSON 时间表'}。"
                "请检查 ClassIsland 及桥接器是否正常运行。"
            ),
            orient=Qt.Orientation.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP_RIGHT,
            duration=-1,  # never auto-close
            parent=self,
        )

    # ------------------------------------------------------------------
    # local SQLite cache
    # ------------------------------------------------------------------

    def _cache_snapshot_to_local_db(self, snapshot: ClientSnapshot) -> None:
        """Write the current snapshot to the local SQLite cache."""
        try:
            all_messages = snapshot.unread_items + snapshot.history_items
            # Deduplicate by db_id (unread takes priority over history).
            seen: set[int] = set()
            deduped: list = []
            for msg in all_messages:
                if msg.db_id in seen:
                    continue
                seen.add(msg.db_id)
                deduped.append(message_to_cache_dict(msg))
            self._local_db.cache_messages(deduped)
        except Exception:
            logger.exception("Failed to cache snapshot to local database")

    def _load_cached_messages(self) -> List[Dict]:
        """Return cached messages from the local database (newest first)."""
        try:
            return self._local_db.get_cached_messages()
        except Exception:
            logger.exception("Failed to load cached messages from local database")
            return []

    def _reload_schedule_sources(self) -> None:
        self._schedule_sources = list_schedule_sources()
        selected, _, warning = self._select_working_schedule_source(
            preferred_key=self._config.schedule_source,
            fallback_key=self._config.last_valid_schedule_source,
            show_warning=False,
        )
        self._schedule_source = selected
        self.settings_page.set_schedule_options(self._schedule_sources, selected.key if selected else None)
        self.settings_page.set_local_schedule_summary(self._schedule_store.current())

        # The mode + ClassIsland liveness decide what is actually used.
        self._apply_schedule_mode()

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

        self._apply_schedule_mode()

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

            # ClassIsland is always valid — it provides live events.
            if is_classisland_source(source):
                self._schedule_ranges = []
                return source, requested, None

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
        # Persist to disk BEFORE handing off to the worker so that a shutdown
        # while offline (or a crashed send) never loses the read receipt.
        self._pending_reads.add(db_id)
        self._enqueue_read_sync(db_id)

    def _enqueue_read_sync(self, db_id: int) -> None:
        """Try to send a read receipt now; it stays in the disk queue until
        the server confirms (removed in _on_read_completed)."""
        if self._worker is not None:
            self._worker.mark_read(db_id)

    def _flush_pending_reads(self) -> None:
        """Replay every read receipt persisted while offline, oldest first.

        Called when the WebSocket reconnects.  Idempotent: items already
        in-flight are skipped via ``_pending_read_ids``.
        """
        for db_id in self._pending_reads.all():
            if db_id in self._pending_read_ids:
                continue
            self._pending_read_ids.add(db_id)
            self._enqueue_read_sync(db_id)
        self.unread_page.set_pending_read_ids(self._pending_read_ids)

    def _set_exam_mode(self, enabled: bool) -> None:
        self._exam_mode = bool(enabled)
        if self._worker is not None:
            self._worker.set_exam_mode(enabled)
        mode = ClientMode.EXAM if enabled else ClientMode.NORMAL
        self._update_window_title(self._snapshot.is_online if self._snapshot else True, mode)

    def _exam_silence(self) -> bool:
        """考试静默模式：服务端已告知家长"学生正在考试"，此时不弹任何窗口。"""
        return bool(self._exam_mode)

    def _note_exam_suppressed(self, what: str) -> None:
        """考试模式下只记日志 + 托盘气泡，不抢焦点。"""
        logger.info("Exam mode: %s suppressed (no popup)", what)
        if self._exam_notice_shown:
            return
        self._exam_notice_shown = True
        with contextlib.suppress(Exception):
            self.tray.showMessage(
                "考试静默模式",
                "已收到新消息，但当前处于考试模式，不会弹出窗口。",
            )

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
        self._connection_text = text
        self._refresh_tray_tooltip()
        if connected:
            # Reconnect succeeded — replay any read receipts persisted while
            # the link was down.
            self._flush_pending_reads()

    def _on_snapshot_received(self, snapshot: ClientSnapshot) -> None:
        previous_unread_map = {item.db_id: item for item in self._snapshot.unread_items} if self._snapshot else {}
        changed_unread_ids = self._changed_unread_ids(snapshot.unread_items, previous_unread_map)
        snapshot = self._apply_settling_reads(snapshot)
        snapshot.history_items = self._retained_history_items(snapshot.history_items)
        self._snapshot = snapshot
        # Rebuilding the message lists costs hundreds of milliseconds for a
        # full history (200 rows), and a snapshot arrives every ~2 seconds.
        # Compare a cheap signature first so unchanged lists are skipped — this
        # is what removes the periodic UI stutter.
        unread_signature = self._messages_signature(snapshot.unread_items)
        history_signature = self._messages_signature(snapshot.history_items)

        if unread_signature != self._unread_signature:
            self._unread_signature = unread_signature
            self.unread_page.set_messages(snapshot.unread_items)
            self.unread_page.set_pending_read_ids(self._pending_read_ids)
        if history_signature != self._history_signature:
            self._history_signature = history_signature
            self.history_page.set_messages(snapshot.history_items)

        self.settings_page.set_exam_mode(snapshot.mode == ClientMode.EXAM)
        # Title reflects the *local* WebSocket state, not the server-side
        # is_online flag: the latter can be stale (e.g. a previous connection's
        # close handler marked us offline right after a reconnect), which is
        # what made the client look "dead" while still receiving messages.
        connected = self._last_connected_state if self._last_connected_state is not None else snapshot.is_online
        self._update_window_title(connected, snapshot.mode)

        # Persist to local SQLite cache for offline resilience.
        self._cache_snapshot_to_local_db(snapshot)

        for message in snapshot.unread_items:
            if (
                message.is_urgent
                and message.db_id in changed_unread_ids
                and message.db_id not in self._pending_read_ids
            ):
                self._queue_urgent_message(message.db_id)

        self._show_next_urgent_popup()
        self._update_break_monitor_state(changed_unread_ids)
        # CI 实时联动模式下没有本地课表区间，所以这里补上「课间中新消息立刻弹窗」：
        # 本地课表模式由 BreakMonitorThread 负责，两者规则一致。
        self._popup_if_new_unread_during_break(snapshot)
        # 考试模式状态跟着快照走（服务端/设置页都可能改）
        exam_now = snapshot.mode == ClientMode.EXAM
        if exam_now != self._exam_mode:
            self._exam_mode = exam_now
            self._exam_notice_shown = False
        # The start-up decision needs both the break state and the first
        # snapshot (to know whether anything is unread).
        self._decide_startup_visibility()

    def _popup_if_new_unread_during_break(self, snapshot: ClientSnapshot) -> None:
        """课间期间**新到**的消息要立即弹窗（不受“下课延时”影响）。

        “下课延时”的设计初衷是：老师可能拖堂，刚下课就弹窗会打断课堂。因此延时
        只作用于「收到下课信号的那一刻」；此后课间里新到的消息属于用户主动想看的
        内容，应当立刻弹出。

        关键点是**记住本次课间已经弹过哪些消息**（``_notified_break_ids``）：
        否则每个快照（约 2 秒一次）都会因为“列表里有未读”而重复弹窗，并顺手把
        刚下课时排队的延时弹窗取消掉——这正是“没等到延时时间就弹、而且一直重复弹”
        的原因。
        """
        if not self._schedule_live or self._break_state_known is not True:
            return
        if self._exam_silence():
            self._note_exam_suppressed("break-time message popup")
            return
        unread_ids = {item.db_id for item in snapshot.unread_items}
        new_ids = unread_ids - self._notified_break_ids
        if not new_ids:
            # 没有新消息：既不要弹窗，也不要去动正在倒计时的延时弹窗
            return
        self._notified_break_ids |= new_ids
        # 课间中新消息比排队的延时弹窗更新，直接接管
        if self._break_popup_timer.isActive():
            self._break_popup_timer.stop()
            self._deferred_break_popup = None
        unread_count = self._current_unread_count()
        if unread_count <= 0:
            return
        logger.info(
            "Break-time message arrived (ids=%s); showing the popup immediately",
            sorted(new_ids),
        )
        self._show_break_popup(self._ci_break_key(), unread_count)

    def _mark_break_notified(self) -> None:
        """把当前未读消息标记为“本次课间已提示过”，避免重复弹窗。"""
        if self._snapshot is None:
            return
        self._notified_break_ids |= {item.db_id for item in self._snapshot.unread_items}

    @staticmethod
    def _messages_signature(items: List[ClientMessage]) -> Tuple:
        """Cheap identity of a message list.

        Used to skip rebuilding the list widgets when a snapshot carries exactly
        the same messages (which is the common case: a snapshot is fetched every
        ~2 seconds but only changes when something actually happens).
        """
        return tuple(
            (
                message.db_id,
                message.status.value if hasattr(message.status, "value") else message.status,
                message.resend_count,
                message.resend_time,
            )
            for message in items
        )

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

    def _show_deferred_break_popup(self) -> None:
        pending = self._deferred_break_popup
        self._deferred_break_popup = None
        if pending is None:
            return
        self._show_break_popup(*pending)

    def _on_break_popup_requested(self, break_key: str, unread_count: int, immediate: bool = False) -> None:
        """检测到课间：按需遵守“下课延时”再弹出。

        ``immediate=True`` 表示这是**课间期间新到的消息**——用户希望它立刻弹出，
        延时只用于“刚下课那一刻”（避免老师拖堂时打断课堂）。
        """
        delay_seconds = max(0, int(self._config.break_popup_delay_seconds))
        if self._exam_silence():
            # 考试模式下不弹窗（服务端已告知家长"正在考试"）
            self._note_exam_suppressed("break popup")
            return
        # 刚下课这一刻先把现有未读标记为“本次课间已提示”，这样延时期间到达的快照
        # 不会误判成“新消息”而提前弹窗
        self._mark_break_notified()
        if delay_seconds > 0 and not immediate:
            self._deferred_break_popup = (break_key, unread_count)
            self._break_popup_timer.start(delay_seconds * 1000)
            logger.info("Break popup deferred by %ss (break=%s)", delay_seconds, break_key)
            return
        if immediate and self._break_popup_timer.isActive():
            # 课间中新消息比排队的延时弹窗更“新”，直接取消排队并立刻显示
            self._break_popup_timer.stop()
            self._deferred_break_popup = None
        self._show_break_popup(break_key, unread_count)

    def _show_break_popup(self, break_key: str, unread_count: int) -> None:
        self._note_ui_activity("break-popup")
        logger.info(
            "Break popup: break=%s unread_count=%s active_window=%s visible=%s minimized=%s",
            break_key,
            unread_count,
            self.isActiveWindow(),
            self.isVisible(),
            self.isMinimized(),
        )
        self.show_normal(force_topmost=True, switch_to_unread=True)
        if self._notified_break_key != break_key:
            self._notified_break_key = break_key
            subject = self._pending_break_subject
            if subject:
                self._show_info(f"课间休息（下节：{subject}），有 {unread_count} 条未读消息")
            else:
                self._show_info(f"课间休息，有 {unread_count} 条未读消息")

    def _on_break_state_changed(self, in_break: bool) -> None:
        """Break state from the local timetable monitor."""
        self._set_break_state(in_break)

    def _set_break_state(self, in_break: bool) -> None:
        """Single entry point for every break-state source.

        Records the state, reports it to the server, cancels a pending deferred
        popup when the break ended, and lets the start-up decision proceed.
        All steps are idempotent, so polling-based refreshes are harmless.
        """
        self._note_ui_activity("break-state")
        state = bool(in_break)
        changed = self._break_state_known != state
        self._break_state_known = state
        if changed:
            # One line the user (or the next developer) can grep to see what the
            # client currently believes and which source told it so.
            logger.info(
                "Break state: %s (source=%s)",
                "课间" if state else "上课",
                self._schedule_source_label or "未知",
            )
        self._refresh_tray_tooltip()
        self._push_break_state_to_worker(state)

        if changed:
            # 进入下课 / 进入上课时清空“本次课间已提示过”的记录
            self._notified_break_ids = set()

        if not state and self._break_popup_timer.isActive():
            # The break ended before the deferred popup fired — drop it.
            self._break_popup_timer.stop()
            self._deferred_break_popup = None
            logger.info("Deferred break popup cancelled: break already ended")
        if not state:
            self._pending_break_subject = ""

        self._decide_startup_visibility()

    def _push_break_state_to_worker(self, in_break: bool) -> None:
        """Notify the server of the current class/break status."""
        if self._worker is not None:
            self._worker.set_is_in_break(in_break)

    def _refresh_tray_tooltip(self, extra: str = "") -> None:
        """Make the client's belief visible: connection + state + source.

        Hovering the tray icon is the fastest way for a user to answer "what
        does the client think it is right now, and who told it that?".
        """
        parts = [APP_NAME]
        if self._connection_text:
            parts.append(self._connection_text)
        if extra:
            parts.append(extra)
        if self._break_state_known is not None:
            parts.append("课间" if self._break_state_known else "上课")
        if self._schedule_source_label:
            parts.append(self._schedule_source_label)
        self.tray.setToolTip(" - ".join(parts))

    # ------------------------------------------------------------------
    # start-up visibility（启动时是否弹出主窗口）
    # ------------------------------------------------------------------

    def begin_startup(self) -> None:
        """Start the start-up visibility decision.

        Called by ``client/app.py`` instead of an unconditional ``show()``:
        the window only appears when the client is *in a break* and there are
        unread messages; otherwise it stays in the tray.  A timeout guarantees
        a decision even if the state never becomes available.
        """
        logger.info("Start-up visibility decision started (timeout=%sms)", _STARTUP_DECISION_TIMEOUT_MS)
        self._refresh_settings_status()
        self._startup_timer.start(_STARTUP_DECISION_TIMEOUT_MS)
        self._decide_startup_visibility()
        if not self._startup_decision_pending:
            # Already decided (state + snapshot were both in place) — make sure
            # the splash is told, whatever the ordering was.
            self.mark_startup_ready()
        # Compare the ClassIsland timetable in use today with the imported copy,
        # without delaying the start-up decision itself.
        QTimer.singleShot(_SCHEDULE_CHECK_DELAY_MS, self._check_schedule_freshness)

    # ------------------------------------------------------------------
    # imported timetable freshness
    # ------------------------------------------------------------------

    def _check_schedule_freshness(self) -> None:
        """Detect that ClassIsland switched to a different timetable.

        Follows ClassIsland's own rules: ``Settings.json → SelectedProfile``,
        then that profile's ``ClassPlans``, then the plan whose
        ``TimeRule.WeekDay`` matches today (0 = 周日 … 6 = 周六) → its
        ``TimeLayoutId``.  When that id differs from the one recorded at import
        time, the local fallback timetable is out of date, so the user is asked
        whether to re-import (or it is refreshed silently when
        ``auto_refresh_schedule`` is on).
        """
        if self._shutting_down or not self._config.use_cib_schedule:
            return

        schedule = self._schedule_store.current()
        if schedule is None or schedule.is_empty:
            self.settings_page.set_schedule_outdated(False)
            return

        parser = ClassIslandConfigParser.auto_detect()
        profile = schedule.profile_file or (parser.read_selected_profile() or "")
        plan = parser.active_class_plan(profile or None)
        if plan is None:
            logger.info("Timetable freshness check skipped: no class plan could be resolved")
            self.settings_page.set_schedule_outdated(False)
            return

        if schedule.layout_id and plan.layout_id == schedule.layout_id:
            logger.info("Local timetable is up to date (layout %s)", plan.layout_id[:8])
            self.settings_page.set_schedule_outdated(False)
            return

        logger.warning(
            "Timetable changed: saved layout=%s (%s), active layout=%s (%s)",
            (schedule.layout_id or "未记录")[:8],
            schedule.layout_name or "?",
            plan.layout_id[:8],
            plan.label,
        )
        # 不一致：设置页出现「重新导入时间表」按钮（用户点“暂不”后仍可手动触发）
        self.settings_page.set_schedule_outdated(
            True,
            f"ClassIsland 当前使用的是「{plan.label}」，与本地导入的"
            f"「{schedule.layout_name or '未知'}」不一致，点击可重新导入。",
        )

        if self._config.auto_refresh_schedule:
            logger.info("auto_refresh_schedule is on — re-importing silently")
            self._reimport_schedule(parser, profile, plan)
            return

        if plan.layout_id in self._declined_schedule_layouts:
            logger.info("User already declined re-importing layout %s", plan.layout_id[:8])
            return

        self._pending_schedule_plan = (profile, plan)
        if self.isVisible():
            self._ask_schedule_reimport()
        else:
            # Never steal focus during a lesson: tell the user via the tray and
            # ask again once the window is actually opened.
            with contextlib.suppress(Exception):
                self.tray.showMessage(
                    "课表可能有更新",
                    f"ClassIsland 当前课表为「{plan.label}」，与本地已导入的不同。打开主窗口可重新导入。",
                )

    def _on_reimport_schedule(self) -> None:
        """「重新导入时间表」按钮：手动把当前启用的 ClassIsland 课表导进来。"""
        parser = ClassIslandConfigParser.auto_detect()
        schedule = self._schedule_store.current()
        profile = (schedule.profile_file if schedule is not None else "") or (
            parser.read_selected_profile() or ""
        )
        plan = parser.active_class_plan(profile or None)
        if plan is None:
            self._show_warning("未找到 ClassIsland 当前启用的课表，无法重新导入")
            return
        logger.info("Manual timetable re-import requested for layout %s", plan.layout_id[:8])
        self._declined_schedule_layouts.discard(plan.layout_id)
        self._reimport_schedule(parser, profile, plan)

    def _ask_schedule_reimport(self) -> None:
        """Ask whether the changed timetable should be imported now."""
        pending = self._pending_schedule_plan
        if pending is None:
            return
        profile, plan = pending
        self._pending_schedule_plan = None

        current = self._schedule_store.current()
        box = MessageBox(
            "课表已更新",
            f"ClassIsland 当前启用的课表是「{plan.label}」，"
            f"与本地已导入的「{current.layout_name or '未知'}」不一致。\n\n"
            "是否现在重新导入本地课表？（降级到本地课表时会用到它）",
            self,
        )
        box.yesButton.setText("重新导入")
        box.cancelButton.setText("暂不")
        if box.exec() != QDialog.DialogCode.Accepted:
            logger.info("User declined re-importing the changed timetable")
            self._declined_schedule_layouts.add(plan.layout_id)
            return

        parser = ClassIslandConfigParser.auto_detect()
        self._reimport_schedule(parser, profile, plan)

    def _reimport_schedule(self, parser, profile: str, plan) -> None:
        """Import the layout of *plan* from *profile* without the wizard."""
        result = parser.load_profile(profile) if profile else None
        if result is None or not result.layouts:
            message = result.error if result is not None and result.error else "未找到可用的时间表"
            logger.warning("Silent timetable re-import failed: %s", message)
            self._show_warning(f"课表自动更新失败：{message}")
            return

        option = next(
            (layout for layout in result.layouts if layout.layout_id == plan.layout_id),
            None,
        )
        if option is None:
            logger.warning(
                "Layout %s is not present in %s (found %s)",
                plan.layout_id[:8],
                profile,
                [layout.layout_id[:8] for layout in result.layouts],
            )
            self._show_warning("课表自动更新失败：档案中找不到当前启用的时间表")
            return

        schedule = StoredSchedule.from_layout_option(option)
        self._schedule_store.save(schedule)
        self.settings_page.set_local_schedule_summary(schedule)
        # 导入成功后课表与 ClassIsland 一致，隐藏「重新导入时间表」按钮
        self.settings_page.set_schedule_outdated(False)
        logger.info("Local timetable refreshed silently: %s", schedule.describe())
        self._show_info(f"已更新本地课表：{schedule.describe()}")
        if self._ci_alive is False or str(self._config.schedule_mode).lower() == "local":
            self._apply_schedule_mode(announce=True, reason="本地课表已更新")

    def _decide_startup_visibility(self, *, timed_out: bool = False) -> None:
        if not self._startup_decision_pending:
            return

        state_known = self._break_state_known is not None
        snapshot_ready = self._snapshot is not None
        if not timed_out and (not state_known or not snapshot_ready):
            return

        self._startup_decision_pending = False
        self._startup_timer.stop()

        in_break = bool(self._break_state_known) if state_known else False
        unread = self._current_unread_count()

        if in_break and unread > 0:
            logger.info("Start-up: break with %s unread message(s) -> showing window", unread)
            self.show_normal(force_topmost=True, switch_to_unread=True)
            self.mark_startup_ready()
            return

        if not state_known:
            reason = "未获取到课间状态（按上课处理）"
        elif in_break:
            reason = "课间暂无未读消息"
        else:
            reason = "当前为上课时间"
        if timed_out and not snapshot_ready:
            reason += "（等待超时）"

        logger.info("Start-up: staying in the tray (%s)", reason)
        self._refresh_tray_tooltip(reason)
        with contextlib.suppress(Exception):
            self.tray.showMessage(APP_NAME, f"已在后台运行：{reason}")
        self.mark_startup_ready()

    # ------------------------------------------------------------------
    # start-up splash
    # ------------------------------------------------------------------

    def attach_splash(self, splash) -> None:
        """Register the start-up splash so it can be dismissed when ready."""
        self._splash = splash
        self._splash_active = True
        # Notices parked during start-up are replayed the moment it closes, so
        # their animations never compete with the start-up animation.
        with contextlib.suppress(Exception):
            splash.closed.connect(self._flush_held_notices)
        if self._startup_ready:
            # Start-up may already have finished before the splash was wired up
            # (the first snapshot can arrive while the window is still being
            # built), in which case nobody would ever tell the splash to go.
            logger.info("Splash attached after start-up finished; dismissing it now")
            with contextlib.suppress(Exception):
                splash.mark_ready()

    def mark_startup_ready(self) -> None:
        """Start-up finished: let the splash go (it keeps its minimum time)."""
        self._startup_ready = True
        splash = getattr(self, "_splash", None)
        if splash is None:
            # No splash at all: replay any parked notices straight away.
            self._flush_held_notices()
            return
        logger.info("Start-up finished; dismissing the splash screen")
        with contextlib.suppress(Exception):
            splash.mark_ready()
        # If the splash was already gone (or has no signal), replay now.
        if getattr(splash, "_closed_emitted", False):
            self._flush_held_notices()

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
        # Server confirmed — drop it from the persisted queue for good.
        self._pending_reads.discard(db_id)
        self._settling_read_ids.add(db_id)
        self.unread_page.set_pending_read_ids(self._pending_read_ids)
        self._clear_urgent_state(db_id)
        self._apply_local_read(db_id)

    def _on_read_failed(self, db_id: int, text: str) -> None:
        self._pending_read_ids.discard(db_id)
        # The server answered but rejected the receipt (e.g. message already
        # gone).  Keeping it would loop forever, so drop it and warn instead.
        self._pending_reads.discard(db_id)
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
        if self._exam_silence():
            # 考试静默模式：紧急消息同样不弹窗（消息仍保留在未读列表里）
            if self._queued_urgent_ids:
                self._note_exam_suppressed("urgent popup")
                self._queued_urgent_ids.clear()
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
        """Refresh the time reference from NTP **in the background**.

        The request blocks for ~100ms (up to 2s on a bad network) and doing it
        inline froze the start-up splash mid-animation, so it runs in a thread
        and the result is applied when it arrives.
        """
        current = self._ntp_thread
        if current is not None and current.isRunning():
            logger.debug("NTP sync already in progress")
            return
        if current is not None:
            self._ntp_thread = None
        thread = NtpSyncThread(self._config.ntp_server, parent=self)
        thread.synced.connect(self._on_time_synced)
        self._ntp_thread = thread
        self.settings_page.set_sync_state(True)
        thread.start()

    def _on_time_synced(self, result: TimeSyncResult) -> None:
        """Apply an NTP result that arrived from the worker thread."""
        self._ntp_thread = None
        self.settings_page.set_sync_state(False)
        if self._shutting_down or result is None:
            return
        self._last_time_sync = result
        self._last_time_sync_anchor = datetime.now()
        self._break_monitor.update_time_reference(result)
        self._show_info(
            f"时间同步完成，来源：{result.source}，时间：{result.current_time.strftime('%H:%M:%S')}"
        )

    def _retained_history_items(self, items: List[ClientMessage]) -> List[ClientMessage]:
        """Trim history to the locally configured retention window.

        This is about what the *client* keeps/shows; how much history the
        server is willing to hand out is enforced server-side
        (``[server] history_window_days``) and must not hide records the local
        database already has — like WeChat, which cannot fetch chats older than
        a few days but still displays the ones already stored locally.
        """
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
        if self._hold_notice(("info", text)):
            return
        if self._message_recently_shown(text):
            return
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
        if self._hold_notice(("warning", text)):
            return
        if self._message_recently_shown(text):
            logger.debug("Suppressed repeat InfoBar: %s", text[:80])
            return
        InfoBar.warning(
            title="连接状态",
            content=text,
            orient=Qt.Orientation.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP_RIGHT,
            duration=3200,
            parent=self,
        )

    # -- notification parking while the splash is on screen -----------------

    def _hold_notice(self, notice: Tuple[str, str]) -> bool:
        """Queue notices while the splash is animating.

        Each InfoBar is an animated widget; showing several of them while the
        start-up animation runs is what made the splash "stutter in the middle"
        (a burst of CIB-degradation and schedule-source notices right after the
        window is built).  They are replayed once the splash is gone.
        """
        if not self._splash_active:
            return False
        self._held_notices.append(notice)
        logger.debug("InfoBar held until the splash closes: %s", notice[1][:80])
        return True

    def _flush_held_notices(self) -> None:
        """Replay the notices that were parked while the splash was up."""
        self._splash_active = False
        notices = self._held_notices[-3:]   # never stack more than a few
        self._held_notices = []
        for kind, text in notices:
            if kind == "warning":
                self._show_warning(text)
            else:
                self._show_info(text)

    def _message_recently_shown(self, text: str) -> bool:
        """Rate-limit identical InfoBars.

        A flapping connection produces the same warning every few seconds;
        every InfoBar is an animated widget, so stacking them is itself a
        source of UI jank.
        """
        now = time.monotonic()
        shown_at = self._recent_messages.get(text)
        if shown_at is not None and now - shown_at < _INFO_DEDUPE_SECONDS:
            return True
        self._recent_messages[text] = now
        if len(self._recent_messages) > 40:
            # Keep the dict tiny: drop everything that is already expired.
            self._recent_messages = {
                key: value
                for key, value in self._recent_messages.items()
                if now - value < _INFO_DEDUPE_SECONDS
            }
        return False

    # -- UI responsiveness watch-dog ---------------------------------------

    def _start_ui_stall_watch(self) -> None:
        """Start the heartbeat that reports UI-thread stalls to the log."""
        self._ui_stall_timer = QTimer(self)
        self._ui_stall_timer.setInterval(_UI_STALL_CHECK_MS)
        self._ui_stall_timer.timeout.connect(self._check_ui_stall)
        self._ui_last_heartbeat = time.perf_counter()
        self._ui_stall_timer.start()

    def _check_ui_stall(self) -> None:
        now = time.perf_counter()
        elapsed_ms = (now - self._ui_last_heartbeat) * 1000
        self._ui_last_heartbeat = now
        if elapsed_ms < _UI_STALL_WARN_MS:
            return
        age_ms = (now - self._ui_activity_at) * 1000
        if age_ms > 2000:
            # Nothing of ours was running: the event loop was starved by
            # something else (a background thread, another process, the OS).
            logger.warning(
                "UI thread stalled %.0fms (no UI work of our own was running)",
                elapsed_ms,
            )
        else:
            logger.warning(
                "UI thread stalled %.0fms (recent UI work: %s, started %.0fms ago)",
                elapsed_ms,
                self._ui_activity or "idle",
                age_ms,
            )

    def _note_ui_activity(self, name: str) -> None:
        """Tag the UI work about to run, so a stall can be attributed."""
        self._ui_activity = name
        self._ui_activity_at = time.perf_counter()

    def _update_window_title(self, connected: bool, mode: ClientMode) -> None:
        status = "🟢 已连接" if connected else "🔴 离线"
        if mode == ClientMode.EXAM:
            status = "🌙 考试模式"
        self.setWindowTitle(f"{APP_NAME} - [{status}]")

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

        # A changed timetable found while the window was hidden is asked about
        # now that the user is actually looking at the app.
        if self._pending_schedule_plan is not None:
            QTimer.singleShot(200, self._ask_schedule_reimport)

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
            self.tray.showMessage(APP_NAME, "客户端已最小化到托盘。")
            event.ignore()
            return
        self.exit_app()
        event.accept()

    def exit_app(self) -> None:
        # Flip this *first*: a CIB check or watchdog callback that completes
        # after this point must not start any new thread.
        self._shutting_down = True
        if self._ntp_thread is not None:
            self._ntp_thread.cancel()
            if not self._ntp_thread.wait(3000):
                self._ntp_thread.terminate()
                self._ntp_thread.wait(1000)
            self._ntp_thread = None
        if self._ui_stall_timer is not None:
            self._ui_stall_timer.stop()
        if self._active_urgent_dialog is not None:
            self._active_urgent_dialog.close()
            self._active_urgent_dialog = None
        for timer in self._reminder_timers.values():
            timer.stop()
        self._reminder_timers.clear()
        self._break_popup_timer.stop()
        self._stop_break_monitor()
        self._stop_cib_supervisor()
        self._stop_ci_watchdog()
        self._stop_classisland_monitor()
        # Anything still retiring must be joined (or dropped) before the
        # interpreter tears Qt down: destroying a running QThread aborts.
        for monitor in list(self._retiring_monitors):
            with contextlib.suppress(Exception):
                self._disconnect_monitor(monitor)
                if not monitor.wait(2000):
                    monitor.terminate()
                    monitor.wait(1000)
        self._retiring_monitors = []
        if self._monitor_reap_timer is not None:
            self._monitor_reap_timer.stop()
        self._stop_worker()
        self._local_db.close()
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


def _help_tooltip(text: str) -> str:
    """把一段说明文字包成能在悬浮提示里换行显示的 HTML。"""
    escaped = (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\n", "<br>")
    )
    return f'<div style="max-width:320px; white-space:normal;">{escaped}</div>'


def _help_icon(text: str, parent: Optional[QWidget] = None) -> QLabel:
    """一个圆圈里带 i 的帮助标志：悬浮显示说明，替代整行解释文字。"""
    icon = QLabel("ⓘ", parent)
    icon.setObjectName("help_icon")
    icon.setCursor(Qt.CursorShape.WhatsThisCursor)
    icon.setStyleSheet("color: #8a93a5; font-size: 13px; padding: 0 2px;")
    icon.setToolTip(_help_tooltip(text))
    icon.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
    return icon


def _labeled_row(widget: QWidget, help_text: str) -> QWidget:
    """把控件与帮助标志排成一行，让解释文字不再占用整行版面。"""
    container = QWidget()
    layout = QHBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(4)
    layout.addWidget(widget)
    if help_text:
        container.help_icon = _help_icon(help_text, container)
        layout.addWidget(container.help_icon)
    layout.addStretch(1)
    return container


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
