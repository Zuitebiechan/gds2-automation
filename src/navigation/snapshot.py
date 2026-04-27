from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable


def _clean_strings(items: Iterable[Any]) -> tuple[str, ...]:
    return tuple(str(item).strip() for item in items if str(item).strip())


@dataclass(frozen=True)
class ControllerSnapshot:
    raw_page_id: str
    buttons: tuple[str, ...] = ()
    list_items: tuple[str, ...] = ()
    navigation_path: tuple[str, ...] = ()
    context: dict[str, Any] = field(default_factory=dict)
    capture_source: str = "controller"
    capture_mode: str = "legacy_dict"
    captured_at: float | None = None

    @property
    def raw_visible_buttons(self) -> tuple[str, ...]:
        return self.buttons

    @property
    def raw_list_items(self) -> tuple[str, ...]:
        return self.list_items

    @classmethod
    def from_legacy_dict(
        cls,
        snapshot: dict[str, Any],
        *,
        navigation_path: Iterable[Any] | None = None,
        capture_source: str = "controller",
        capture_mode: str = "legacy_dict",
        captured_at: float | None = None,
    ) -> "ControllerSnapshot":
        raw_page = snapshot.get("raw_page_id")
        if not raw_page:
            page_value = snapshot.get("page")
            if isinstance(page_value, dict):
                raw_page = page_value.get("page_id")
            else:
                raw_page = page_value
        path_value = snapshot.get("navigation_path") if navigation_path is None else navigation_path
        return cls(
            raw_page_id=str(raw_page or "").strip(),
            buttons=_clean_strings(snapshot.get("buttons") or []),
            list_items=_clean_strings(snapshot.get("lists") or snapshot.get("list_items") or []),
            navigation_path=_clean_strings(path_value or []),
            context=dict(snapshot.get("context") or {}),
            capture_source=str(capture_source or "controller"),
            capture_mode=str(capture_mode or "legacy_dict"),
            captured_at=captured_at,
        )

    def with_navigation_path(self, navigation_path: Iterable[Any]) -> "ControllerSnapshot":
        return ControllerSnapshot(
            raw_page_id=self.raw_page_id,
            buttons=self.buttons,
            list_items=self.list_items,
            navigation_path=_clean_strings(navigation_path),
            context=dict(self.context),
            capture_source=self.capture_source,
            capture_mode=self.capture_mode,
            captured_at=self.captured_at,
        )

    def to_legacy_dict(self) -> dict[str, Any]:
        return {
            "page": self.raw_page_id,
            "buttons": list(self.buttons),
            "lists": list(self.list_items),
            "context": dict(self.context),
            "navigation_path": list(self.navigation_path),
        }
