"""Overlay-style scrollbars: hidden until the user actually scrolls.

Qt keeps a scrollbar visible whenever the content overflows, which costs
horizontal space and looks noisy.  This module makes a scrollbar *invisible by
default* and shows its handle only while the area is being scrolled (or while
the pointer is over the bar itself, so it can still be grabbed).

Implementation notes
--------------------
* The bar keeps the layout slot (``ScrollBarAsNeeded``) and its handle is made
  transparent through a stylesheet, so showing/hiding never re-lays out the
  content (no jumping).
* Activity is detected from the scroll bar's own ``valueChanged`` (wheel, drag,
  keyboard, programmatic scrolling on a real scroll area) plus wheel events on
  the viewport, because a wheel event that has already hit the end of the range
  changes nothing.
* A single timer per bar hides the handle again after :data:`HIDE_DELAY_MS`.
"""

from __future__ import annotations

import logging
from typing import Dict, Optional

from PyQt6.QtCore import QEvent, QObject, QTimer, Qt
from PyQt6.QtWidgets import QAbstractScrollArea, QScrollBar, QWidget

logger = logging.getLogger("kg.client.scroll_utils")

#: How long the handle stays visible after the last scroll activity.
HIDE_DELAY_MS = 900
#: Thickness of the bar when it appears (it keeps this width while hidden, so
#: content never shifts; only the handle is transparent).
BAR_WIDTH = 10


def _bar_stylesheet() -> str:
    # Deliberately *no* width override: the bar keeps the width the style gives
    # it, so the scroll area's layout never changes when the handle appears.
    # Only the handle is styled (transparent by default, visible while active).
    return (
        "QScrollBar:vertical { background: transparent; margin: 2px 0; }"
        "QScrollBar::handle:vertical {"
        "  background: transparent; border-radius: 4px; min-height: 28px;"
        "}"
        'QScrollBar[scrolling="true"]::handle:vertical {'
        "  background: rgba(130, 140, 160, 0.55);"
        "}"
        'QScrollBar[scrolling="true"]::handle:vertical:hover {'
        "  background: rgba(130, 140, 160, 0.80);"
        "}"
        "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }"
        "QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }"
    )


class AutoHideScrollBar(QObject):
    """Hides *bar* until it is used (or hovered)."""

    def __init__(self, bar: QScrollBar, *, delay_ms: int = HIDE_DELAY_MS, parent=None) -> None:
        super().__init__(parent or bar)
        self._bar = bar
        self._visible = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(max(200, int(delay_ms)))
        self._timer.timeout.connect(self.hide_handle)

        bar.setStyleSheet(_bar_stylesheet())
        bar.setProperty("scrolling", "false")
        # Settle the (now thinner) bar geometry immediately: otherwise the first
        # reveal/hide would re-layout and shift the content by a few pixels.
        style = bar.style()
        style.unpolish(bar)
        style.polish(bar)
        bar.installEventFilter(self)
        bar.valueChanged.connect(self._on_scrolled)
        bar.rangeChanged.connect(self._on_scrolled)

    # ------------------------------------------------------------------
    # activity
    # ------------------------------------------------------------------

    def _on_scrolled(self, *_args) -> None:
        # A range change with no movement (e.g. the first layout) should not
        # flash the handle: only react to a different value.
        if self._bar.value() == getattr(self, "_last_value", None) and self._visible:
            self._timer.start()
            return
        self._last_value = self._bar.value()
        self.show_handle()

    def show_handle(self) -> None:
        self._timer.start()
        if self._visible:
            return
        self._visible = True
        self._set_property("true")

    def hide_handle(self) -> None:
        if not self._visible:
            return
        self._visible = False
        self._set_property("false")

    def _set_property(self, value: str) -> None:
        self._bar.setProperty("scrolling", value)
        style = self._bar.style()
        style.unpolish(self._bar)
        style.polish(self._bar)
        self._bar.update()

    # ------------------------------------------------------------------
    # hovering the bar keeps it visible
    # ------------------------------------------------------------------

    def eventFilter(self, obj, event):  # noqa: N802 - Qt naming
        if obj is self._bar:
            if event.type() == QEvent.Type.Enter:
                self.show_handle()
            elif event.type() == QEvent.Type.Leave:
                self._timer.start()
        return False


def _wheel_filter_target(scroll_area: QAbstractScrollArea) -> QWidget:
    viewport = scroll_area.viewport()
    return viewport if viewport is not None else scroll_area


class _WheelWatcher(QObject):
    """Shows the handle on wheel activity, even at the end of the range."""

    def __init__(self, bar: AutoHideScrollBar, viewport: QWidget) -> None:
        super().__init__(viewport)
        self._bar = bar
        self._viewport = viewport
        viewport.installEventFilter(self)

    def eventFilter(self, obj, event):  # noqa: N802 - Qt naming
        if event.type() == QEvent.Type.Wheel:
            self._bar.show_handle()
        elif event.type() in (QEvent.Type.Enter,):
            # Hovering the area does not reveal the bar: only scrolling should.
            pass
        return False


def enable_auto_hide(scroll_area: QAbstractScrollArea, *, delay_ms: int = HIDE_DELAY_MS) -> AutoHideScrollBar:
    """Attach auto-hiding behaviour to one scroll area's vertical bar."""
    bar = scroll_area.verticalScrollBar()
    controller = AutoHideScrollBar(bar, delay_ms=delay_ms, parent=bar)
    _WheelWatcher(controller, _wheel_filter_target(scroll_area))
    setattr(bar, "_auto_hide_controller", controller)
    return controller


def install_auto_hide_scrollbars(root: QWidget, *, delay_ms: int = HIDE_DELAY_MS) -> int:
    """Attach the behaviour to every scroll area under *root* (recursively).

    Returns how many scroll areas were (newly) configured.
    """
    count = 0
    for area in root.findChildren(QAbstractScrollArea):
        # Nested scroll bars (e.g. a spin box) are not interesting here; only
        # real scroll areas whose vertical bar can overflow.
        bar = area.verticalScrollBar()
        if bar is None or getattr(bar, "_auto_hide_controller", None) is not None:
            continue
        enable_auto_hide(area, delay_ms=delay_ms)
        count += 1
    if count:
        logger.debug("Auto-hiding scrollbars installed on %s scroll area(s)", count)
    return count


__all__ = [
    "AutoHideScrollBar",
    "HIDE_DELAY_MS",
    "enable_auto_hide",
    "install_auto_hide_scrollbars",
]
