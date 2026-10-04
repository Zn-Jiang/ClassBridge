"""查看当前导入的课表（只读对话框）。

数据来自本地已导入的课表快照（``imported_schedule.json``）：
- 顶部显示来源档案、时间表名称、时间表 ID、导入时间；
- 中间用表格列出每个节点（序号 / 类型 / 开始 / 结束）；
- 类型为「上课 / 课间」，并说明课间窗口是如何推导的。

之所以单独做成一个模块，是为了让设置页只负责弹窗，逻辑仍集中在
``client/schedule_store`` 提供的数据上。
"""

from __future__ import annotations

import logging
from typing import List, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)
from qfluentwidgets import BodyLabel, CaptionLabel, PrimaryPushButton, SubtitleLabel

from .schedule_store import StoredSchedule, TYPE_BREAK

logger = logging.getLogger("kg.client.schedule_view")


class ScheduleViewDialog(QDialog):
    """只读展示当前导入的课表内容。"""

    def __init__(self, schedule: Optional[StoredSchedule], *, non_class_ranges=None, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("schedule_view_dialog")
        self.setWindowTitle("当前导入的课表")
        self.resize(720, 620)
        self._schedule = schedule
        self._non_class_ranges = list(non_class_ranges or [])
        self._build_ui()

    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)

        layout.addWidget(SubtitleLabel("当前导入的课表"))

        if self._schedule is None or self._schedule.is_empty:
            empty = BodyLabel("尚未导入课表。请点击「从 ClassIsland 配置文件导入课表」。")
            empty.setWordWrap(True)
            layout.addWidget(empty)
            layout.addStretch(1)
            layout.addLayout(self._build_buttons())
            return

        layout.addWidget(self._build_meta())
        layout.addWidget(self._build_table(), 1)
        layout.addWidget(self._build_footer())
        layout.addLayout(self._build_buttons())

    def _build_meta(self) -> BodyLabel:
        schedule = self._schedule
        assert schedule is not None
        lines = [
            f"来源档案：{schedule.profile_file or '（未记录）'}",
            f"时间表名称：{schedule.layout_name or '（未记录）'}",
            f"时间表 ID：{schedule.layout_id or '（旧版本导入，未记录）'}",
            f"导入时间：{schedule.imported_at or '（未记录）'}",
            f"节点数：{len(schedule.entries)}（其中课间 {schedule.break_count} 个）",
        ]
        label = BodyLabel("\n".join(lines))
        label.setWordWrap(True)
        label.setObjectName("schedule_view_meta")
        return label

    def _build_table(self) -> QTableWidget:
        schedule = self._schedule
        assert schedule is not None
        table = QTableWidget(len(schedule.entries), 4, self)
        table.setObjectName("schedule_view_table")
        table.setHorizontalHeaderLabels(["序号", "类型", "开始", "结束"])
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        table.setAlternatingRowColors(True)

        for row, entry in enumerate(schedule.entries):
            is_break = entry.type == TYPE_BREAK
            values = [
                str(row + 1),
                "课间" if is_break else "上课",
                entry.start or "",
                entry.end or "",
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                table.setItem(row, column, item)

        header = table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        return table

    def _build_footer(self) -> CaptionLabel:
        """说明课间窗口是怎么算出来的（含首尾的补足规则）。"""
        count = len(self._non_class_ranges)
        text = (
            "课间判定用得是「非上课时段」：课上以外的时间（包括第一节上课前、"
            "最后一节下课后）都算课间，可以弹窗。"
        )
        if count:
            first = self._non_class_ranges[0]
            last = self._non_class_ranges[-1]
            text += (
                f"\n当前共 {count} 个可弹窗时段，"
                f"例如 {first[0].strftime('%H:%M')}–{first[1].strftime('%H:%M')} 与 "
                f"{last[0].strftime('%H:%M')}–{last[1].strftime('%H:%M')}。"
            )
        label = CaptionLabel(text)
        label.setWordWrap(True)
        label.setObjectName("schedule_view_footer")
        return label

    def _build_buttons(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.addStretch(1)
        close_button = PrimaryPushButton("关闭")
        close_button.clicked.connect(self.accept)
        row.addWidget(close_button)
        return row


__all__ = ["ScheduleViewDialog"]
