"""视图联动使用的轻量坐标对象（GUI 层）。"""

from __future__ import annotations

from dataclasses import dataclass

from core.formats.bt656 import PixelLocation


@dataclass(frozen=True)
class ViewFocus:
    """Image Preview 与 Hex Viewer 共享的「当前关注点」。"""

    kind: str = "none"          # none / row / group / byte / sync
    y: int | None = None
    x: int | None = None
    offset: int | None = None
    component: str | None = None
    line_index: int | None = None
    group: int | None = None
    group_span: int = 1
    label: str = ""

    @property
    def active(self) -> bool:
        return self.kind != "none"

    @property
    def is_pixel_level(self) -> bool:
        return self.kind in ("group", "byte") and self.y is not None

    def describe(self) -> str:
        parts: list[str] = []
        if self.y is not None:
            parts.append(f"Y={self.y}")
        if self.x is not None:
            parts.append(f"X={self.x}")
        if self.group is not None:
            parts.append(f"Group={self.group}")
        if self.component:
            parts.append(self.component)
        if self.offset is not None:
            parts.append(f"Offset=0x{self.offset:08X}")
        if self.line_index is not None:
            parts.append(f"Line={self.line_index}")
        if self.label:
            parts.append(self.label)
        return "  ".join(parts) if parts else "—"


def focus_from_location(location: PixelLocation | None, kind: str | None = None) -> ViewFocus:
    if location is None or location.y is None:
        return ViewFocus()
    if kind is None:
        kind = "byte" if location.component else "row"
    return ViewFocus(
        kind=kind,
        y=location.y,
        x=location.x,
        offset=location.offset,
        component=location.component,
        line_index=location.line_index,
        group=location.pixel_group_index,
        group_span=location.shared_pixels,
    )
