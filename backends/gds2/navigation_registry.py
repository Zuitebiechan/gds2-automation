from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


DEFAULT_SEED_PATH = Path(__file__).with_name("navigation_registry_seed.json")
DEFAULT_REGISTRY_PATH = Path("data/gds2_navigation_registry.sqlite")
ALLOWED_ROUTE_KINDS = {"button", "device", "list_item"}
ALLOWED_ROUTE_TRANSITIONS = {"clear_dtcs_selection_state"}
ALLOWED_POLICY_ACTION_KEYS = {
    "click_enter",
    "return_to_common_ancestor",
    "restart_gds2",
    "select_device_and_continue",
    "soft_ok_then_backtrack",
    "wait",
}


@dataclass(frozen=True)
class NavigationRegistryEntry:
    page_key: str
    title: str
    category: str
    page_kind: str
    canonical_path: list[str]
    route_steps: list[dict[str, Any]]
    target_action: dict[str, Any] | None
    expected_page_id: str | None
    expected_actions: list[dict[str, Any]]
    success_criteria: dict[str, Any]
    fallback_strategy: list[str]
    confidence: str
    source: str
    notes: str
    aliases: list[str]


@dataclass(frozen=True)
class PageStateEntry:
    state_key: str
    page_id: str
    signals: dict[str, Any]
    action_hint: str
    confidence: str
    source: str
    notes: str


@dataclass(frozen=True)
class RecoveryPolicyEntry:
    policy_key: str
    applies_to: list[str]
    action_key: str
    params: dict[str, Any]
    max_attempts: int | None
    timeout_sec: float | None
    resume_original_task: bool
    source: str
    notes: str


def load_seed(path: str | Path = DEFAULT_SEED_PATH) -> dict[str, Any]:
    seed_path = Path(path)
    return json.loads(seed_path.read_text(encoding="utf-8"))


def connect_registry(path: str | Path = DEFAULT_REGISTRY_PATH) -> sqlite3.Connection:
    connection = sqlite3.connect(Path(path))
    connection.row_factory = sqlite3.Row
    return connection


def initialize_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        PRAGMA foreign_keys = ON;

        CREATE TABLE IF NOT EXISTS registry_metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS navigation_pages (
            page_key TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            category TEXT NOT NULL,
            page_kind TEXT NOT NULL,
            canonical_path_json TEXT NOT NULL,
            route_steps_json TEXT NOT NULL,
            target_action_kind TEXT,
            target_action_label TEXT,
            expected_page_id TEXT,
            expected_actions_json TEXT NOT NULL,
            success_criteria_json TEXT NOT NULL DEFAULT '{}',
            fallback_strategy_json TEXT NOT NULL,
            confidence TEXT NOT NULL,
            source TEXT NOT NULL,
            notes TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS navigation_aliases (
            alias TEXT PRIMARY KEY,
            page_key TEXT NOT NULL REFERENCES navigation_pages(page_key) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_navigation_pages_category
            ON navigation_pages(category);
        CREATE INDEX IF NOT EXISTS idx_navigation_pages_target_action
            ON navigation_pages(target_action_kind, target_action_label);

        CREATE TABLE IF NOT EXISTS page_states (
            state_key TEXT PRIMARY KEY,
            page_id TEXT NOT NULL,
            signals_json TEXT NOT NULL,
            action_hint TEXT NOT NULL DEFAULT '',
            confidence TEXT NOT NULL,
            source TEXT NOT NULL,
            notes TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_page_states_page_id
            ON page_states(page_id);

        CREATE TABLE IF NOT EXISTS recovery_policies (
            policy_key TEXT PRIMARY KEY,
            applies_to_json TEXT NOT NULL,
            action_key TEXT NOT NULL,
            params_json TEXT NOT NULL,
            max_attempts INTEGER,
            timeout_sec REAL,
            resume_original_task INTEGER NOT NULL DEFAULT 1,
            source TEXT NOT NULL,
            notes TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )


def rebuild_registry_database(
    *,
    output_path: str | Path = DEFAULT_REGISTRY_PATH,
    seed_path: str | Path = DEFAULT_SEED_PATH,
) -> Path:
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        output.unlink()

    seed = load_seed(seed_path)
    with connect_registry(output) as connection:
        initialize_schema(connection)
        write_seed(connection, seed)
        connection.commit()
    return output


def write_seed(connection: sqlite3.Connection, seed: dict[str, Any]) -> None:
    validate_seed(seed)
    now = datetime.now().isoformat(timespec="seconds")
    metadata = {
        "schema_version": str(seed.get("schema_version", 1)),
        "backend": str(seed.get("backend", "gds2")),
        "vehicle_profile": json.dumps(seed.get("vehicle_profile") or {}, ensure_ascii=False, sort_keys=True),
        "generated_from": str(seed.get("generated_from") or "navigation_registry_seed.json"),
        "generated_at": now,
    }
    connection.executemany(
        "INSERT OR REPLACE INTO registry_metadata(key, value) VALUES (?, ?)",
        sorted(metadata.items()),
    )

    for entry in _normalize_entries(seed.get("entries") or []):
        target_action = entry.target_action or {}
        connection.execute(
            """
            INSERT INTO navigation_pages(
                page_key,
                title,
                category,
                page_kind,
                canonical_path_json,
                route_steps_json,
                target_action_kind,
                target_action_label,
                expected_page_id,
                expected_actions_json,
                success_criteria_json,
                fallback_strategy_json,
                confidence,
                source,
                notes,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry.page_key,
                entry.title,
                entry.category,
                entry.page_kind,
                json.dumps(entry.canonical_path, ensure_ascii=False),
                json.dumps(entry.route_steps, ensure_ascii=False),
                target_action.get("kind"),
                target_action.get("label"),
                entry.expected_page_id,
                json.dumps(entry.expected_actions, ensure_ascii=False),
                json.dumps(entry.success_criteria, ensure_ascii=False),
                json.dumps(entry.fallback_strategy, ensure_ascii=False),
                entry.confidence,
                entry.source,
                entry.notes,
                now,
                now,
            ),
        )
        alias_rows = [(alias.lower(), entry.page_key) for alias in entry.aliases]
        connection.executemany(
            "INSERT OR REPLACE INTO navigation_aliases(alias, page_key) VALUES (?, ?)",
            alias_rows,
        )

    for state in _normalize_page_states(seed.get("page_states") or []):
        connection.execute(
            """
            INSERT INTO page_states(
                state_key,
                page_id,
                signals_json,
                action_hint,
                confidence,
                source,
                notes,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                state.state_key,
                state.page_id,
                json.dumps(state.signals, ensure_ascii=False),
                state.action_hint,
                state.confidence,
                state.source,
                state.notes,
                now,
                now,
            ),
        )

    for policy in _normalize_recovery_policies(seed.get("recovery_policies") or []):
        connection.execute(
            """
            INSERT INTO recovery_policies(
                policy_key,
                applies_to_json,
                action_key,
                params_json,
                max_attempts,
                timeout_sec,
                resume_original_task,
                source,
                notes,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                policy.policy_key,
                json.dumps(policy.applies_to, ensure_ascii=False),
                policy.action_key,
                json.dumps(policy.params, ensure_ascii=False),
                policy.max_attempts,
                policy.timeout_sec,
                1 if policy.resume_original_task else 0,
                policy.source,
                policy.notes,
                now,
                now,
            ),
        )


def validate_seed(seed: dict[str, Any]) -> None:
    state_keys = {
        str(state.get("state_key") or "").strip()
        for state in seed.get("page_states") or []
    }
    state_keys.discard("")

    for entry in seed.get("entries") or []:
        page_key = str(entry.get("page_key") or "<unknown>")
        for step in entry.get("route_steps") or []:
            kind = str(step.get("kind") or "").strip()
            if kind and kind not in ALLOWED_ROUTE_KINDS:
                raise ValueError(f"{page_key}: unsupported route step kind '{kind}'")
            transition = str(step.get("transition") or "").strip()
            if transition and transition not in ALLOWED_ROUTE_TRANSITIONS:
                raise ValueError(f"{page_key}: unsupported route step transition '{transition}'")

    for policy in seed.get("recovery_policies") or []:
        policy_key = str(policy.get("policy_key") or "<unknown>")
        action_key = str(policy.get("action_key") or "").strip()
        if action_key not in ALLOWED_POLICY_ACTION_KEYS:
            raise ValueError(f"{policy_key}: unsupported recovery action_key '{action_key}'")
        timeout = policy.get("timeout_sec")
        if timeout is not None and float(timeout) < 0:
            raise ValueError(f"{policy_key}: timeout_sec must be non-negative")
        attempts = policy.get("max_attempts")
        if attempts is not None and int(attempts) < 0:
            raise ValueError(f"{policy_key}: max_attempts must be non-negative")
        for state_key in policy.get("applies_to") or []:
            if str(state_key) not in state_keys:
                raise ValueError(f"{policy_key}: applies_to references unknown page state '{state_key}'")


def lookup_entry(connection: sqlite3.Connection, key_or_alias: str) -> dict[str, Any] | None:
    key = str(key_or_alias).strip()
    if not key:
        return None

    row = connection.execute(
        "SELECT * FROM navigation_pages WHERE page_key = ?",
        (key,),
    ).fetchone()
    if row is None:
        alias = connection.execute(
            """
            SELECT p.*
            FROM navigation_aliases a
            JOIN navigation_pages p ON p.page_key = a.page_key
            WHERE a.alias = ?
            """,
            (key.lower(),),
        ).fetchone()
        row = alias
    if row is None:
        return None
    return _row_to_entry_dict(row, aliases=list_aliases(connection, str(row["page_key"])))


def list_entries(connection: sqlite3.Connection, *, category: str | None = None) -> list[dict[str, Any]]:
    if category:
        rows = connection.execute(
            "SELECT * FROM navigation_pages WHERE category = ? ORDER BY page_key",
            (category,),
        ).fetchall()
    else:
        rows = connection.execute("SELECT * FROM navigation_pages ORDER BY page_key").fetchall()
    return [
        _row_to_entry_dict(row, aliases=list_aliases(connection, str(row["page_key"])))
        for row in rows
    ]


def list_aliases(connection: sqlite3.Connection, page_key: str) -> list[str]:
    rows = connection.execute(
        "SELECT alias FROM navigation_aliases WHERE page_key = ? ORDER BY alias",
        (page_key,),
    ).fetchall()
    return [str(row["alias"]) for row in rows]


def lookup_page_state(connection: sqlite3.Connection, state_key: str) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT * FROM page_states WHERE state_key = ?",
        (str(state_key),),
    ).fetchone()
    if row is None:
        return None
    return _row_to_page_state_dict(row)


def list_page_states(connection: sqlite3.Connection, *, page_id: str | None = None) -> list[dict[str, Any]]:
    if page_id:
        rows = connection.execute(
            "SELECT * FROM page_states WHERE page_id = ? ORDER BY state_key",
            (page_id,),
        ).fetchall()
    else:
        rows = connection.execute("SELECT * FROM page_states ORDER BY state_key").fetchall()
    return [_row_to_page_state_dict(row) for row in rows]


def lookup_recovery_policy(connection: sqlite3.Connection, policy_key: str) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT * FROM recovery_policies WHERE policy_key = ?",
        (str(policy_key),),
    ).fetchone()
    if row is None:
        return None
    return _row_to_recovery_policy_dict(row)


def list_recovery_policies(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = connection.execute("SELECT * FROM recovery_policies ORDER BY policy_key").fetchall()
    return [_row_to_recovery_policy_dict(row) for row in rows]


def _normalize_entries(entries: Iterable[dict[str, Any]]) -> list[NavigationRegistryEntry]:
    normalized: list[NavigationRegistryEntry] = []
    seen: set[str] = set()
    for raw in entries:
        page_key = str(raw["page_key"]).strip()
        if not page_key:
            raise ValueError("navigation registry entry missing page_key")
        if page_key in seen:
            raise ValueError(f"duplicate navigation registry page_key: {page_key}")
        seen.add(page_key)

        aliases = sorted(
            {
                page_key,
                *(str(alias).strip() for alias in raw.get("aliases") or [] if str(alias).strip()),
            },
            key=str.lower,
        )
        normalized.append(
            NavigationRegistryEntry(
                page_key=page_key,
                title=str(raw.get("title") or page_key),
                category=str(raw.get("category") or "uncategorized"),
                page_kind=str(raw.get("page_kind") or "unknown"),
                canonical_path=[str(item).strip() for item in raw.get("canonical_path") or [] if str(item).strip()],
                route_steps=[
                    {str(key): value for key, value in dict(step).items()}
                    for step in raw.get("route_steps") or []
                ],
                target_action=dict(raw["target_action"]) if raw.get("target_action") else None,
                expected_page_id=str(raw.get("expected_page_id") or "") or None,
                expected_actions=[
                    {str(key): value for key, value in dict(action).items()}
                    for action in raw.get("expected_actions") or []
                ],
                success_criteria=dict(raw.get("success_criteria") or {}),
                fallback_strategy=[
                    str(item).strip() for item in raw.get("fallback_strategy") or [] if str(item).strip()
                ],
                confidence=str(raw.get("confidence") or "draft"),
                source=str(raw.get("source") or "manual_seed"),
                notes=str(raw.get("notes") or ""),
                aliases=aliases,
            )
        )
    return normalized


def _normalize_page_states(entries: Iterable[dict[str, Any]]) -> list[PageStateEntry]:
    normalized: list[PageStateEntry] = []
    seen: set[str] = set()
    for raw in entries:
        state_key = str(raw["state_key"]).strip()
        if not state_key:
            raise ValueError("page state entry missing state_key")
        if state_key in seen:
            raise ValueError(f"duplicate page state key: {state_key}")
        seen.add(state_key)
        normalized.append(
            PageStateEntry(
                state_key=state_key,
                page_id=str(raw.get("page_id") or "").strip(),
                signals=dict(raw.get("signals") or {}),
                action_hint=str(raw.get("action_hint") or ""),
                confidence=str(raw.get("confidence") or "draft"),
                source=str(raw.get("source") or "manual_seed"),
                notes=str(raw.get("notes") or ""),
            )
        )
    return normalized


def _normalize_recovery_policies(entries: Iterable[dict[str, Any]]) -> list[RecoveryPolicyEntry]:
    normalized: list[RecoveryPolicyEntry] = []
    seen: set[str] = set()
    for raw in entries:
        policy_key = str(raw["policy_key"]).strip()
        if not policy_key:
            raise ValueError("recovery policy entry missing policy_key")
        if policy_key in seen:
            raise ValueError(f"duplicate recovery policy key: {policy_key}")
        seen.add(policy_key)
        timeout = raw.get("timeout_sec")
        max_attempts = raw.get("max_attempts")
        normalized.append(
            RecoveryPolicyEntry(
                policy_key=policy_key,
                applies_to=[str(item).strip() for item in raw.get("applies_to") or [] if str(item).strip()],
                action_key=str(raw.get("action_key") or "").strip(),
                params=dict(raw.get("params") or {}),
                max_attempts=int(max_attempts) if max_attempts is not None else None,
                timeout_sec=float(timeout) if timeout is not None else None,
                resume_original_task=bool(raw.get("resume_original_task", True)),
                source=str(raw.get("source") or "manual_seed"),
                notes=str(raw.get("notes") or ""),
            )
        )
    return normalized


def _row_to_entry_dict(row: sqlite3.Row, *, aliases: list[str]) -> dict[str, Any]:
    target_action = None
    if row["target_action_kind"] or row["target_action_label"]:
        target_action = {
            "kind": row["target_action_kind"],
            "label": row["target_action_label"],
        }
    return {
        "page_key": row["page_key"],
        "title": row["title"],
        "category": row["category"],
        "page_kind": row["page_kind"],
        "canonical_path": json.loads(row["canonical_path_json"]),
        "route_steps": json.loads(row["route_steps_json"]),
        "target_action": target_action,
        "expected_page_id": row["expected_page_id"],
        "expected_actions": json.loads(row["expected_actions_json"]),
        "success_criteria": json.loads(row["success_criteria_json"]),
        "fallback_strategy": json.loads(row["fallback_strategy_json"]),
        "confidence": row["confidence"],
        "source": row["source"],
        "notes": row["notes"],
        "aliases": aliases,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _row_to_page_state_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "state_key": row["state_key"],
        "page_id": row["page_id"],
        "signals": json.loads(row["signals_json"]),
        "action_hint": row["action_hint"],
        "confidence": row["confidence"],
        "source": row["source"],
        "notes": row["notes"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _row_to_recovery_policy_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "policy_key": row["policy_key"],
        "applies_to": json.loads(row["applies_to_json"]),
        "action_key": row["action_key"],
        "params": json.loads(row["params_json"]),
        "max_attempts": row["max_attempts"],
        "timeout_sec": row["timeout_sec"],
        "resume_original_task": bool(row["resume_original_task"]),
        "source": row["source"],
        "notes": row["notes"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
