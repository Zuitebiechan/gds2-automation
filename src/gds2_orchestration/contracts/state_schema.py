from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class UIState:
    current_page: str
    visible_buttons: List[str] = field(default_factory=list)
    list_items: List[str] = field(default_factory=list)
    context: Dict[str, Any] = field(default_factory=dict)
    recent_actions: List[str] = field(default_factory=list)

    def __post_init__(self):
        if not self.current_page:
            raise ValueError("current_page cannot be empty")

    def has_button(self, name: str) -> bool:
        return name in self.visible_buttons

    def has_list_item(self, name: str) -> bool:
        return name in self.list_items

    def to_dict(self) -> Dict[str, Any]:
        return {
            "current_page": self.current_page,
            "visible_buttons": self.visible_buttons,
            "list_items": self.list_items,
            "context": self.context,
            "recent_actions": self.recent_actions,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "UIState":
        return cls(**data)
