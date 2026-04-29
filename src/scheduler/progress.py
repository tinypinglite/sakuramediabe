from __future__ import annotations

from typing import Any

from tqdm import tqdm


class TqdmProgressAdapter:
    """懒创建 tqdm 进度条，接收 progress_callback 格式的 payload 并驱动终端进度显示。"""

    def __init__(self) -> None:
        self._bar: tqdm | None = None
        self._last_current: int = 0

    def callback(self, payload: dict[str, Any]) -> None:
        total = payload.get("total")
        current = payload.get("current")
        text = payload.get("text")

        if self._bar is None and total is not None:
            self._bar = tqdm(total=total, unit="item", dynamic_ncols=True)

        if self._bar is None:
            return

        if total is not None and total != self._bar.total:
            self._bar.total = total
            self._bar.refresh()

        if text:
            self._bar.set_description(text)

        if current is not None:
            delta = current - self._last_current
            if delta > 0:
                self._bar.update(delta)
            self._last_current = current

    def close(self) -> None:
        if self._bar is not None:
            self._bar.close()
