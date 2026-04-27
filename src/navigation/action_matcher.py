from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence


@dataclass(frozen=True)
class ActionMatch:
    matched: bool
    label: str | None = None
    index: int | None = None
    resolution: str = "not_found"
    candidate_labels: tuple[str, ...] = ()

    @property
    def ambiguous(self) -> bool:
        return self.resolution == "ambiguous"

    def diagnostics(
        self,
        *,
        target_label: str,
        action_kind: str | None,
        owner: str,
    ) -> dict[str, Any]:
        return {
            "target_label": str(target_label or ""),
            "action_kind": str(action_kind or ""),
            "owner": owner,
            "selected_policy": self.resolution,
            "candidate_labels": list(self.candidate_labels),
            "resolution": self.resolution,
        }


class ActionMatchError(RuntimeError):
    def __init__(self, message: str, match_diagnostics: Sequence[dict[str, Any]]) -> None:
        super().__init__(message)
        self.match_diagnostics = [dict(item) for item in match_diagnostics]


def normalize_label(label: str) -> str:
    return " ".join(str(label or "").casefold().strip().split())


def find_label_match(
    target_label: str,
    candidate_labels: Sequence[str],
    *,
    allow_contains: bool,
) -> ActionMatch:
    target = str(target_label or "").strip()
    candidates = [str(label or "").strip() for label in candidate_labels]
    non_empty_candidates = tuple(label for label in candidates if label)
    if not target:
        return ActionMatch(False, candidate_labels=non_empty_candidates)

    for index, label in enumerate(candidates):
        if label == target:
            return ActionMatch(
                True,
                label=label,
                index=index,
                resolution="exact",
                candidate_labels=non_empty_candidates,
            )

    normalized_target = normalize_label(target)
    normalized_matches = [
        (index, label)
        for index, label in enumerate(candidates)
        if label and normalize_label(label) == normalized_target
    ]
    if len(normalized_matches) == 1:
        index, label = normalized_matches[0]
        return ActionMatch(
            True,
            label=label,
            index=index,
            resolution="normalized_exact",
            candidate_labels=non_empty_candidates,
        )
    if len(normalized_matches) > 1:
        return ActionMatch(
            False,
            resolution="ambiguous",
            candidate_labels=tuple(label for _index, label in normalized_matches),
        )

    if allow_contains:
        normalized_contains_matches = [
            (index, label)
            for index, label in enumerate(candidates)
            if label
            and normalized_target
            and (
                normalized_target in normalize_label(label)
                or normalize_label(label) in normalized_target
            )
        ]
        if len(normalized_contains_matches) == 1:
            index, label = normalized_contains_matches[0]
            return ActionMatch(
                True,
                label=label,
                index=index,
                resolution="unique_contains",
                candidate_labels=non_empty_candidates,
            )
        if len(normalized_contains_matches) > 1:
            return ActionMatch(
                False,
                resolution="ambiguous",
                candidate_labels=tuple(label for _index, label in normalized_contains_matches),
            )

    return ActionMatch(False, candidate_labels=non_empty_candidates)


def find_list_item_match(target_label: str, items: Sequence[str]) -> ActionMatch:
    return find_label_match(target_label, items, allow_contains=True)


def find_action_match(
    actions: Sequence[dict[str, Any]],
    target_label: str,
    *,
    kind: str | None,
    allow_list_contains: bool = True,
) -> ActionMatch:
    filtered_actions = [
        action
        for action in actions
        if kind is None or str(action.get("kind") or "") == kind
    ]
    labels = [str(action.get("label") or "") for action in filtered_actions]
    match = find_label_match(
        target_label,
        labels,
        allow_contains=bool(kind == "list_item" and allow_list_contains),
    )
    if match.index is None:
        return match

    action = filtered_actions[match.index]
    source_index = match.index
    if "list_index" in action:
        try:
            source_index = int(action.get("list_index") or 0)
        except (TypeError, ValueError):
            source_index = match.index

    return ActionMatch(
        matched=match.matched,
        label=match.label,
        index=source_index,
        resolution=match.resolution,
        candidate_labels=match.candidate_labels,
    )
