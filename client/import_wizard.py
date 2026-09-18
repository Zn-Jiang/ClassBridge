"""Three-step wizard for importing a timetable from ClassIsland.

The wizard imports *everything* from :mod:`client.classisland_import` — it only
drives that parser and renders the result.  On success the caller can read
:attr:`ScheduleImportWizard.imported_schedule` (a
:class:`client.schedule_store.StoredSchedule`).

Steps
-----
1. **路径确认** — show/auto-fill the ClassIsland root, ``Settings.json`` and
   ``Profiles`` folder, each with a browse button.
2. **时间表选择** — list every timetable found in ``Profiles/*.json`` as
   ``[配置文件名] - 时间表名称 (包含 X 个课间)``, preselecting the one from
   ``SelectedProfile``.
3. **数据预览与确认** — show the parsed nodes (序号 | 类型 | 开始 | 结束),
   let the user tweak the times, then finish the import.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QStackedWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    ComboBox,
    LineEdit,
    PrimaryPushButton,
    PushButton,
    SubtitleLabel,
    TableWidget,
)

from .classisland_import import (
    ClassIslandConfigParser,
    TimeLayoutOption,
    normalize_clock,
)
from .schedule_store import (
    TYPE_BREAK,
    TYPE_CLASS,
    ScheduleStore,
    StoredEntry,
    StoredSchedule,
)

logger = logging.getLogger("kg.client.import_wizard")

_TOTAL_STEPS = 3


class ScheduleImportWizard(QDialog):
    """Modal 3-step import wizard.  Check ``imported_schedule`` after exec()."""

    def __init__(
        self,
        parser: Optional[ClassIslandConfigParser] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("从 ClassIsland 导入课表")
        self.setModal(True)
        self.resize(720, 520)

        self._parser = parser or ClassIslandConfigParser.auto_detect()
        self._options: List[TimeLayoutOption] = []
        self.imported_schedule: Optional[StoredSchedule] = None

        self._build_ui()
        self._prefill_paths()

    # ------------------------------------------------------------------
    # construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)

        self.step_label = CaptionLabel("")
        self.title_label = SubtitleLabel("")
        layout.addWidget(self.step_label)
        layout.addWidget(self.title_label)

        self.stack = QStackedWidget(self)
        self.stack.addWidget(self._build_path_page())
        self.stack.addWidget(self._build_select_page())
        self.stack.addWidget(self._build_preview_page())
        layout.addWidget(self.stack, 1)

        buttons = QHBoxLayout()
        self.cancel_button = PushButton("取消")
        self.cancel_button.clicked.connect(self.reject)
        self.back_button = PushButton("上一步")
        self.back_button.clicked.connect(self._go_back)
        self.next_button = PrimaryPushButton("下一步")
        self.next_button.clicked.connect(self._go_next)
        buttons.addWidget(self.cancel_button)
        buttons.addStretch(1)
        buttons.addWidget(self.back_button)
        buttons.addWidget(self.next_button)
        layout.addLayout(buttons)

        self._sync_step_ui()

    # -- step 1: paths ---------------------------------------------------

    def _build_path_page(self) -> QWidget:
        page = QWidget(self)
        grid = QGridLayout(page)
        grid.setContentsMargins(0, 8, 0, 0)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(14)

        self.root_edit = LineEdit()
        self.settings_edit = LineEdit()
        self.profiles_edit = LineEdit()

        rows = [
            ("ClassIsland 根目录", self.root_edit, self._browse_root),
            ("Settings.json 路径", self.settings_edit, self._browse_settings),
            ("Profiles 文件夹路径", self.profiles_edit, self._browse_profiles),
        ]
        for row, (label, edit, handler) in enumerate(rows):
            browse = PushButton("浏览")
            browse.clicked.connect(handler)
            grid.addWidget(BodyLabel(label), row, 0)
            grid.addWidget(edit, row, 1)
            grid.addWidget(browse, row, 2)

        self.path_hint = CaptionLabel("")
        grid.addWidget(self.path_hint, len(rows), 0, 1, 3)

        # Editing the root re-derives the two dependent paths.
        self.root_edit.editingFinished.connect(self._apply_root_dir)
        return page

    # -- step 2: timetable selection -------------------------------------

    def _build_select_page(self) -> QWidget:
        page = QWidget(self)
        box = QVBoxLayout(page)
        box.setContentsMargins(0, 8, 0, 0)
        box.setSpacing(12)

        row = QHBoxLayout()
        self.layout_box = ComboBox()
        self.layout_box.setMinimumWidth(460)
        self.refresh_button = PushButton("重新扫描")
        self.refresh_button.clicked.connect(self._scan_layouts)
        row.addWidget(BodyLabel("选择时间表"))
        row.addWidget(self.layout_box, 1)
        row.addWidget(self.refresh_button)
        box.addLayout(row)

        self.select_hint = CaptionLabel("")
        self.select_hint.setWordWrap(True)
        box.addWidget(self.select_hint)
        box.addStretch(1)
        return page

    # -- step 3: preview --------------------------------------------------

    def _build_preview_page(self) -> QWidget:
        page = QWidget(self)
        box = QVBoxLayout(page)
        box.setContentsMargins(0, 8, 0, 0)
        box.setSpacing(10)

        box.addWidget(BodyLabel("解析结果预览（可双击“开始/结束”单元格微调时间）"))

        self.preview_table = TableWidget()
        self.preview_table.setColumnCount(4)
        self.preview_table.setHorizontalHeaderLabels(["序号", "类型", "开始时间", "结束时间"])
        self.preview_table.verticalHeader().setVisible(False)
        self.preview_table.setBorderVisible(True)
        self.preview_table.setBorderRadius(8)
        header = self.preview_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        box.addWidget(self.preview_table, 1)

        self.preview_hint = CaptionLabel("")
        self.preview_hint.setWordWrap(True)
        box.addWidget(self.preview_hint)
        return page

    # ------------------------------------------------------------------
    # step navigation
    # ------------------------------------------------------------------

    @property
    def _step(self) -> int:
        return self.stack.currentIndex()

    def _sync_step_ui(self) -> None:
        titles = ["路径确认", "时间表选择", "数据预览与确认"]
        self.step_label.setText(f"第 {self._step + 1} 步，共 {_TOTAL_STEPS} 步")
        self.title_label.setText(titles[self._step])
        self.back_button.setEnabled(self._step > 0)
        is_last = self._step == _TOTAL_STEPS - 1
        self.next_button.setText("完成导入" if is_last else "下一步")

    def _go_back(self) -> None:
        if self._step > 0:
            self.stack.setCurrentIndex(self._step - 1)
            self._sync_step_ui()

    def _go_next(self) -> None:
        if self._step == 0:
            if not self._apply_paths():
                return
            self.stack.setCurrentIndex(1)
            self._sync_step_ui()
            self._scan_layouts()
            return
        if self._step == 1:
            self.stack.setCurrentIndex(2)
            self._sync_step_ui()
            self._render_preview()
            return
        self._finish_import()

    # ------------------------------------------------------------------
    # step 1 helpers
    # ------------------------------------------------------------------

    def _prefill_paths(self) -> None:
        paths = self._parser.resolved_paths()
        if paths.root_dir:
            self.root_edit.setText(str(paths.root_dir))
        if paths.settings_path:
            self.settings_edit.setText(str(paths.settings_path))
        if paths.profiles_dir:
            self.profiles_edit.setText(str(paths.profiles_dir))

        if paths.is_complete:
            self.path_hint.setText("已自动探测到 ClassIsland 安装路径，如不正确可手动修改。")
        else:
            self.path_hint.setText(
                "未能自动探测到 ClassIsland，请手动选择其安装目录（或 data 目录）。"
            )
        self.path_hint.setTextColor("#c42b1c" if not paths.is_complete else "#0f766e", "#c42b1c")

    def _browse_root(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "选择 ClassIsland 安装目录")
        if selected:
            self.root_edit.setText(selected)
            self._apply_root_dir()

    def _browse_settings(self) -> None:
        selected, _ = QFileDialog.getOpenFileName(
            self, "选择 Settings.json", self.settings_edit.text() or "", "JSON 文件 (*.json)"
        )
        if selected:
            self.settings_edit.setText(selected)
            self._apply_settings_path()

    def _browse_profiles(self) -> None:
        selected = QFileDialog.getExistingDirectory(
            self, "选择 Profiles 文件夹", self.profiles_edit.text() or ""
        )
        if selected:
            self.profiles_edit.setText(selected)
            self._apply_profiles_path()

    def _apply_root_dir(self) -> None:
        """Derive Settings/Profiles paths from the chosen root directory."""
        text = self.root_edit.text().strip()
        if not text:
            return
        data_dir = ClassIslandConfigParser.data_dir_from_root(Path(text))
        if data_dir is None:
            return
        self.settings_edit.setText(str(data_dir / "Settings.json"))
        self.profiles_edit.setText(str(data_dir / "Profiles"))

    def _apply_settings_path(self) -> None:
        text = self.settings_edit.text().strip()
        if not text:
            return
        data_dir = Path(text).parent
        self.profiles_edit.setText(str(data_dir / "Profiles"))

    def _apply_profiles_path(self) -> None:
        text = self.profiles_edit.text().strip()
        if not text:
            return
        path = Path(text)
        data_dir = path.parent if path.name.lower() == "profiles" else path
        self.settings_edit.setText(str(data_dir / "Settings.json"))

    def _apply_paths(self) -> bool:
        """Push the entered paths into the parser; returns False if unusable."""
        root_text = self.root_edit.text().strip()
        profiles_text = self.profiles_edit.text().strip()
        settings_text = self.settings_edit.text().strip()

        if root_text:
            self._parser.set_root_dir(Path(root_text))
        else:
            self._parser.set_root_dir(None)

        data_dir: Optional[Path] = None
        if profiles_text:
            candidate = Path(profiles_text)
            data_dir = candidate.parent if candidate.name.lower() == "profiles" else candidate
        elif settings_text:
            data_dir = Path(settings_text).parent
        if data_dir is not None:
            self._parser.set_data_dir(data_dir)

        resolved = self._parser.resolved_paths()
        if resolved.profiles_dir is None or not resolved.profiles_dir.is_dir():
            self.path_hint.setText(
                "未找到 Profiles 文件夹，请检查路径（通常位于 ClassIsland 的 data 目录下）。"
            )
            self.path_hint.setTextColor("#c42b1c", "#c42b1c")
            return False
        return True

    # ------------------------------------------------------------------
    # step 2 helpers
    # ------------------------------------------------------------------

    def _scan_layouts(self) -> None:
        result = self._parser.scan_result()
        self._options = result.options

        self.layout_box.blockSignals(True)
        self.layout_box.clear()
        for option in self._options:
            self.layout_box.addItem(option.label, userData=option.layout_id)
        self.layout_box.blockSignals(False)

        default = self._parser.default_layout(self._options)
        if default is not None:
            index = next(
                (i for i, item in enumerate(self._options) if item.layout_id == default.layout_id),
                0,
            )
            self.layout_box.setCurrentIndex(index)

        messages: List[str] = []
        messages.extend(result.warnings)
        messages.extend(result.errors)
        self.select_hint.setText("\n".join(messages))
        self.select_hint.setTextColor("#c42b1c" if result.errors else "#8a8886", "#c42b1c")
        logger.info(
            "ClassIsland scan: %s timetables, %s warnings, %s errors",
            len(result.options),
            len(result.warnings),
            len(result.errors),
        )

    def _current_option(self) -> Optional[TimeLayoutOption]:
        index = self.layout_box.currentIndex()
        if 0 <= index < len(self._options):
            return self._options[index]
        return None

    # ------------------------------------------------------------------
    # step 3 helpers
    # ------------------------------------------------------------------

    def _render_preview(self) -> None:
        option = self._current_option()
        self.preview_table.setRowCount(0)
        if option is None:
            self.preview_hint.setText("请返回上一步选择一个时间表。")
            self.preview_hint.setTextColor("#c42b1c", "#c42b1c")
            return

        self.preview_table.setRowCount(len(option.entries))
        for row, entry in enumerate(option.entries):
            self._set_cell(row, 0, str(row + 1), editable=False)
            self._set_cell(row, 1, entry.type_label, editable=False)
            self._set_cell(row, 2, entry.start, editable=True)
            self._set_cell(row, 3, entry.end, editable=True)

        self.preview_hint.setText(
            f"共 {len(option.entries)} 个节点，其中课间 {option.break_count} 个，"
            f"上课 {option.class_count} 个。确认无误后点击“完成导入”。"
        )
        self.preview_hint.setTextColor("#8a8886", "#c42b1c")

    def _set_cell(self, row: int, column: int, text: str, *, editable: bool) -> None:
        item = QTableWidgetItem(text)
        if not editable:
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.preview_table.setItem(row, column, item)

    def _collect_entries(self) -> Optional[List[StoredEntry]]:
        """Read the (possibly edited) table back into timetable entries."""
        entries: List[StoredEntry] = []
        for row in range(self.preview_table.rowCount()):
            type_item = self.preview_table.item(row, 1)
            start_item = self.preview_table.item(row, 2)
            end_item = self.preview_table.item(row, 3)
            if start_item is None or end_item is None:
                continue

            start = normalize_clock(start_item.text())
            end = normalize_clock(end_item.text())
            if start is None or end is None:
                self.preview_hint.setText(
                    f"第 {row + 1} 行时间格式不正确，请使用 HH:MM 或 HH:MM:SS。"
                )
                self.preview_hint.setTextColor("#c42b1c", "#c42b1c")
                self.preview_table.setCurrentCell(row, 2 if start is None else 3)
                return None

            label = type_item.text() if type_item is not None else "上课"
            entries.append(
                StoredEntry(
                    index=row + 1,
                    type=TYPE_BREAK if label == "课间" else TYPE_CLASS,
                    start=start,
                    end=end,
                )
            )

        if not entries:
            self.preview_hint.setText("没有可导入的时间节点。")
            self.preview_hint.setTextColor("#c42b1c", "#c42b1c")
            return None
        return entries

    def _finish_import(self) -> None:
        entries = self._collect_entries()
        if entries is None:
            return

        option = self._current_option()
        schedule = StoredSchedule.from_entries(
            entries,
            profile_file=option.profile_file if option else "",
            layout_name=option.name if option else "",
        )
        if schedule.break_count == 0:
            self.preview_hint.setText("警告：所选时间表不包含任何课间节点，导入后将不会弹窗。")
            self.preview_hint.setTextColor("#c42b1c", "#c42b1c")

        self.imported_schedule = schedule
        logger.info("Imported schedule: %s", schedule.describe())
        self.accept()


def run_import_wizard(
    store: ScheduleStore,
    parent: Optional[QWidget] = None,
) -> Optional[StoredSchedule]:
    """Show the wizard and persist the result on success.

    Returns the imported timetable, or ``None`` when the user cancelled.
    """
    wizard = ScheduleImportWizard(parent=parent)
    if wizard.exec() != QDialog.DialogCode.Accepted:
        return None
    schedule = wizard.imported_schedule
    if schedule is None:
        return None
    store.save(schedule)
    return schedule
