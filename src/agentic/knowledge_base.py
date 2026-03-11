"""
Knowledge base interface using LanceDB.

Provides operations for storing and querying GDS2 page knowledge,
error patterns, navigation traces, and icon catalogs.

Improvements over v1:
- #1: Rich _format_snapshot() matching seed data text_features format
- #2: query_error_patterns() exposed for agent error recovery
- #3: Similarity threshold filtering + tool-call suggestions
- #4: Auto-learning via add_example() + record_navigation_trace()
- #5: Navigation trace persistence (gds2_navigation_traces table)
- #6: get_tool_suggestion() for RAG-guided tool-calling
- #7: Icon query support for visual page identification
"""

import logging
import re
from datetime import datetime
from typing import List, Dict, Any, Optional

import os

logger = logging.getLogger(__name__)

# Similarity threshold: distances above this are "low confidence"
# LanceDB uses L2 distance by default; lower = more similar
SIMILARITY_THRESHOLD = 1.5  # Tuned for all-MiniLM-L6-v2 embedding space


class GDS2KnowledgeBase:
    """
    Interface to LanceDB knowledge base.

    Stores and retrieves GDS2 page patterns, error handling rules,
    navigation traces, and icon catalogs.
    """

    def __init__(self, db_path: Optional[str] = None):
        """
        Initialize knowledge base.

        Args:
            db_path: Path to LanceDB database (defaults to data/gds2_knowledge.lance)
        """
        self.db_path = db_path or os.getenv("KNOWLEDGE_BASE_PATH", "data/gds2_knowledge.lance")
        self._db = None
        self._text_model = None
        self._pages_table = None
        self._errors_table = None
        self._traces_table = None
        self._icons_table = None

    def _ensure_initialized(self):
        """Lazy initialization of database and models."""
        if self._db is not None:
            return

        try:
            import lancedb
            from sentence_transformers import SentenceTransformer

            logger.info(f"Initializing knowledge base at {self.db_path}")

            # Use HF mirror for China mainland if not set
            if not os.environ.get("HF_ENDPOINT"):
                os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
                logger.info("Set HF_ENDPOINT=https://hf-mirror.com (China mirror)")

            self._db = lancedb.connect(self.db_path)
            self._text_model = SentenceTransformer('all-MiniLM-L6-v2')

            # Try to open tables
            for table_name, attr_name in [
                ("gds2_pages", "_pages_table"),
                ("gds2_error_patterns", "_errors_table"),
                ("gds2_navigation_traces", "_traces_table"),
                ("gds2_icons", "_icons_table"),
            ]:
                try:
                    table = self._db.open_table(table_name)
                    setattr(self, attr_name, table)
                    logger.info(f"{table_name}: {table.count_rows()} entries")
                except Exception as e:
                    logger.debug(f"{table_name} not found: {e}")
                    setattr(self, attr_name, None)

        except ImportError as e:
            logger.error(f"Failed to import dependencies: {e}")
            logger.error("Install: pip install lancedb sentence-transformers")
            raise

    # -----------------------------------------------------------------------
    # #1: Rich snapshot formatting (matches seed data text_features format)
    # -----------------------------------------------------------------------

    def _format_snapshot(self, snapshot: Dict[str, Any]) -> str:
        """
        Format snapshot for text embedding.

        Produces text that matches the style of seed data text_features,
        ensuring query embeddings are in the same semantic space as stored data.

        Example output:
            "Buttons: Diagnostics, Update. No list items.
             This page has 2 buttons and no lists."
        """
        buttons = snapshot.get("buttons", [])
        lists = snapshot.get("lists", [])
        context = snapshot.get("context", {})
        page = snapshot.get("page", "unknown")

        parts = []

        # Button description
        if buttons:
            parts.append(f"Buttons: {', '.join(buttons)}.")
        else:
            parts.append("No buttons visible.")

        # List description with pattern analysis
        if lists:
            sample = lists[:10]
            parts.append(f"List items include: {', '.join(sample)}.")

            # Detect bracket pattern (module list signature)
            bracket_items = [item for item in lists if "[" in item and "]" in item]
            if bracket_items:
                parts.append(f"Items have bracket notation like '{bracket_items[0]}'.")

            # Detect data category patterns
            data_keywords = ["Data", "Engine", "Fuel", "Misfire", "Transmission"]
            data_items = [item for item in lists if any(kw in item for kw in data_keywords)]
            if data_items and not bracket_items:
                parts.append("Items appear to be data categories.")

            # Detect diagnostic function menu
            diag_markers = ["Data Display", "Diagnostic Trouble Codes", "DTC",
                            "Module Information", "Special Functions"]
            diag_items = [item for item in lists if any(m in item for m in diag_markers)]
            if diag_items:
                parts.append(f"Diagnostic functions detected: {', '.join(diag_items)}.")

            parts.append(f"Total list items: {len(lists)}.")
        else:
            parts.append("No list items.")

        # Page context (if available)
        if page and page != "unknown":
            parts.append(f"Detected page type: {page}.")

        # Extra context from controller
        if context:
            module = context.get("module")
            if module:
                parts.append(f"Current module: {module}.")
            device = context.get("device")
            if device:
                parts.append(f"Connected device: {device}.")

        return " ".join(parts)

    # -----------------------------------------------------------------------
    # Page queries (with similarity threshold)
    # -----------------------------------------------------------------------

    def query_similar_pages(
        self,
        snapshot: Dict[str, Any],
        top_k: int = 3,
        threshold: Optional[float] = None,
    ) -> List[Dict]:
        """
        Find similar pages based on current snapshot.

        Args:
            snapshot: Page snapshot with buttons, lists, etc.
            top_k: Number of similar pages to return
            threshold: Max L2 distance (None = use default SIMILARITY_THRESHOLD)

        Returns:
            List of similar page dictionaries with metadata and _distance scores.
            Results above threshold are marked with is_confident=False.
        """
        self._ensure_initialized()

        if self._pages_table is None:
            logger.warning("Pages table not initialized, returning empty results")
            return []

        threshold = threshold if threshold is not None else SIMILARITY_THRESHOLD

        try:
            text_query = self._format_snapshot(snapshot)
            query_embedding = self._text_model.encode(text_query)

            results = (
                self._pages_table
                .search(query_embedding, vector_column_name="text_embedding")
                .limit(top_k)
                .to_list()
            )

            # Annotate with confidence
            for r in results:
                distance = r.get("_distance", float("inf"))
                r["is_confident"] = distance <= threshold

            logger.debug(
                f"Found {len(results)} similar pages "
                f"({sum(1 for r in results if r.get('is_confident'))} confident)"
            )
            return results

        except Exception as e:
            logger.exception(f"Error querying similar pages: {e}")
            return []

    # -----------------------------------------------------------------------
    # #2: Error pattern queries
    # -----------------------------------------------------------------------

    def query_error_patterns(self, error_text: str, top_k: int = 3) -> List[Dict]:
        """
        Find matching error patterns for an error message.

        Args:
            error_text: Error message text to match
            top_k: Number of patterns to return

        Returns:
            List of error pattern dictionaries with recommended actions
        """
        self._ensure_initialized()

        if self._errors_table is None:
            logger.debug("Error patterns table not initialized")
            return []

        try:
            query_embedding = self._text_model.encode(error_text)

            results = (
                self._errors_table
                .search(query_embedding, vector_column_name="text_embedding")
                .limit(top_k)
                .to_list()
            )

            logger.debug(f"Found {len(results)} error patterns")
            return results

        except Exception as e:
            logger.exception(f"Error querying error patterns: {e}")
            return []

    # -----------------------------------------------------------------------
    # #3: Tool-call suggestion from RAG results
    # -----------------------------------------------------------------------

    def get_tool_suggestion(self, snapshot: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Get a tool-call suggestion based on RAG results.

        If the top-1 similar page is confident AND has a deterministic_action,
        return a structured suggestion the agent can directly use.

        Args:
            snapshot: Current page snapshot

        Returns:
            Dict with tool_name + args, or None if no confident suggestion.
            Example: {"tool_name": "click_button", "args": {"button_text": "Data Display"},
                      "source": "MODULE_SUBMENU", "confidence": "high"}
        """
        results = self.query_similar_pages(snapshot, top_k=1)
        if not results:
            return None

        top = results[0]
        if not top.get("is_confident"):
            return None

        action = top.get("deterministic_action", "")
        if not action:
            return None

        # Parse deterministic_action string into tool name + args
        # Formats: "click Diagnostics", "click 'Data Display'",
        #          "select 'VCI Proxy (Remote)' and click OK",
        #          "wait for completion"
        suggestion = self._parse_deterministic_action(action)
        if suggestion:
            suggestion["source"] = top.get("page_type", "unknown")
            suggestion["confidence"] = "high"
            suggestion["distance"] = top.get("_distance", None)
            return suggestion

        return None

    @staticmethod
    def _parse_deterministic_action(action_str: str) -> Optional[Dict[str, Any]]:
        """Parse a deterministic_action string into tool name + args."""
        action_str = action_str.strip()

        if not action_str or action_str == "None":
            return None

        # "click 'Button Text'" or "click Button Text"
        click_match = re.match(r"click\s+'?([^']+?)'?\s*$", action_str, re.IGNORECASE)
        if click_match:
            return {
                "tool_name": "click_button",
                "args": {"button_text": click_match.group(1).strip()},
            }

        # "select 'Item Text'"
        select_match = re.match(r"select\s+'([^']+)'", action_str, re.IGNORECASE)
        if select_match:
            return {
                "tool_name": "select_list_item",
                "args": {"item_text": select_match.group(1).strip()},
            }

        # "wait for completion"
        if "wait" in action_str.lower():
            return {
                "tool_name": "get_current_snapshot",
                "args": {},
            }

        return None

    # -----------------------------------------------------------------------
    # #4: Auto-learning (add_example)
    # -----------------------------------------------------------------------

    def add_example(
        self,
        snapshot: Dict[str, Any],
        classification: str,
        confidence: float,
        action_taken: Optional[str] = None,
        action_succeeded: bool = True,
        user_correction: Optional[str] = None,
    ):
        """
        Add a new example to knowledge base (learning from experience).

        Args:
            snapshot: Page snapshot
            classification: Agent's classification (page type)
            confidence: Agent's confidence score (0.0-1.0)
            action_taken: The action that was executed (e.g., "click_button('Data Display')")
            action_succeeded: Whether the action succeeded
            user_correction: User's correction if agent was wrong
        """
        self._ensure_initialized()

        if self._pages_table is None:
            logger.warning("Pages table not initialized, cannot add example")
            return

        try:
            text_features = self._format_snapshot(snapshot)
            text_embedding = self._text_model.encode(text_features).tolist()

            final_classification = user_correction or classification

            record = {
                "id": f"learned_{final_classification}_{datetime.now().strftime('%Y%m%d%H%M%S')}",
                "page_type": final_classification,
                "description": f"Learned from observation (confidence: {confidence:.2f})",
                "text_features": text_features,
                "text_embedding": text_embedding,
                "must_have_buttons": snapshot.get("buttons", []),
                "usually_has_buttons": [],
                "must_not_have_buttons": [],
                "list_pattern": "learned",
                "list_count_range": str(len(snapshot.get("lists", []))),
                "detection_rule": f"Learned from observation (was: {classification})",
                "deterministic_action": action_taken or "",
                "is_user_decision": False,
                "screenshot_paths": [],
                "usage_count": 0,
                "accuracy_rate": 1.0 if action_succeeded else 0.0,
                "created_at": datetime.now().isoformat(),
                "updated_at": datetime.now().isoformat(),
            }

            self._pages_table.add([record])
            logger.info(
                f"Added learned example: {final_classification} "
                f"(confidence: {confidence:.2f}, action: {action_taken})"
            )

            if user_correction:
                logger.info(f"User corrected: {classification} -> {user_correction}")

        except Exception as e:
            logger.exception(f"Error adding example: {e}")

    # -----------------------------------------------------------------------
    # #5: Navigation trace persistence
    # -----------------------------------------------------------------------

    def record_navigation_trace(
        self,
        steps: List[Dict[str, Any]],
        goal: str,
        success: bool,
        total_steps: int,
        user_selections: Optional[Dict[str, Any]] = None,
    ):
        """
        Record a complete navigation trace for future reference.

        Args:
            steps: List of navigation_history entries from the session
            goal: The navigation goal (e.g., "Navigate to Data Display")
            success: Whether the goal was reached
            total_steps: Total number of steps taken
            user_selections: User's selections during the session
        """
        self._ensure_initialized()

        if self._db is None:
            logger.warning("Database not initialized, cannot record trace")
            return

        try:
            import json

            # Build a text summary for embedding
            step_actions = [s.get("action", "unknown") for s in steps]
            pages_visited = []
            for s in steps:
                from_page = s.get("from_page", "")
                to_page = s.get("to_page", "")
                if from_page:
                    pages_visited.append(from_page)
                if to_page:
                    pages_visited.append(to_page)
            # Deduplicate while preserving order
            seen = set()
            unique_pages = []
            for p in pages_visited:
                if p not in seen:
                    seen.add(p)
                    unique_pages.append(p)

            trace_text = (
                f"Goal: {goal}. "
                f"Pages: {' -> '.join(unique_pages)}. "
                f"Steps: {total_steps}. "
                f"{'Success' if success else 'Failed'}."
            )
            trace_embedding = self._text_model.encode(trace_text).tolist()

            record = {
                "id": f"trace_{datetime.now().strftime('%Y%m%d%H%M%S_%f')}",
                "goal": goal,
                "success": success,
                "total_steps": total_steps,
                "pages_visited": unique_pages,
                "step_actions": json.dumps(step_actions),
                "user_selections": json.dumps(user_selections or {}),
                "trace_text": trace_text,
                "trace_embedding": trace_embedding,
                "created_at": datetime.now().isoformat(),
            }

            # Create table if not exists
            if self._traces_table is None:
                import pyarrow as pa
                dim = len(trace_embedding)
                schema = pa.schema([
                    pa.field("id", pa.string()),
                    pa.field("goal", pa.string()),
                    pa.field("success", pa.bool_()),
                    pa.field("total_steps", pa.int32()),
                    pa.field("pages_visited", pa.list_(pa.string())),
                    pa.field("step_actions", pa.string()),
                    pa.field("user_selections", pa.string()),
                    pa.field("trace_text", pa.string()),
                    pa.field("trace_embedding", pa.list_(pa.float32(), dim)),
                    pa.field("created_at", pa.string()),
                ])
                self._traces_table = self._db.create_table(
                    "gds2_navigation_traces", data=[record], schema=schema
                )
                logger.info("Created gds2_navigation_traces table")
            else:
                self._traces_table.add([record])

            logger.info(
                f"Recorded navigation trace: {goal} "
                f"({'success' if success else 'failed'}, {total_steps} steps)"
            )

        except Exception as e:
            logger.exception(f"Error recording navigation trace: {e}")

    def query_similar_traces(
        self,
        goal: str,
        current_page: str = "",
        top_k: int = 3,
        success_only: bool = True,
    ) -> List[Dict]:
        """
        Find similar navigation traces.

        Args:
            goal: Current navigation goal
            current_page: Current page (for context)
            top_k: Number of traces to return
            success_only: Only return successful traces

        Returns:
            List of trace dictionaries
        """
        self._ensure_initialized()

        if self._traces_table is None:
            return []

        try:
            query_text = f"Goal: {goal}. Current page: {current_page}."
            query_embedding = self._text_model.encode(query_text)

            results = (
                self._traces_table
                .search(query_embedding, vector_column_name="trace_embedding")
                .limit(top_k * 2)  # Over-fetch for filtering
                .to_list()
            )

            if success_only:
                results = [r for r in results if r.get("success", False)]

            return results[:top_k]

        except Exception as e:
            logger.exception(f"Error querying navigation traces: {e}")
            return []

    # -----------------------------------------------------------------------
    # #7: Icon queries
    # -----------------------------------------------------------------------

    def query_icons(self, category: Optional[str] = None, name_pattern: Optional[str] = None) -> List[Dict]:
        """
        Query the icon catalog.

        Args:
            category: Filter by category (navigation, data_display, screen, component, status, action, other)
            name_pattern: Filter by semantic name substring

        Returns:
            List of icon records
        """
        self._ensure_initialized()

        if self._icons_table is None:
            return []

        try:
            # LanceDB doesn't have SQL-like WHERE, so we fetch and filter
            all_icons = self._icons_table.to_pandas()

            if category:
                all_icons = all_icons[all_icons["category"] == category]

            if name_pattern:
                all_icons = all_icons[
                    all_icons["semantic_name"].str.contains(name_pattern, case=False, na=False)
                ]

            return all_icons.to_dict("records")

        except Exception as e:
            logger.exception(f"Error querying icons: {e}")
            return []

    # -----------------------------------------------------------------------
    # Stats
    # -----------------------------------------------------------------------

    def get_stats(self) -> Dict[str, Any]:
        """Get knowledge base statistics."""
        self._ensure_initialized()

        stats: Dict[str, Any] = {
            "initialized": self._pages_table is not None,
            "db_path": self.db_path,
        }

        try:
            stats["page_count"] = self._pages_table.count_rows() if self._pages_table else 0
            stats["error_pattern_count"] = self._errors_table.count_rows() if self._errors_table else 0
            stats["trace_count"] = self._traces_table.count_rows() if self._traces_table else 0
            stats["icon_count"] = self._icons_table.count_rows() if self._icons_table else 0
        except Exception as e:
            logger.exception(f"Error getting stats: {e}")
            stats["error"] = str(e)

        return stats


# ---------------------------------------------------------------------------
# Global instance (singleton pattern)
# ---------------------------------------------------------------------------

_kb_instance: Optional[GDS2KnowledgeBase] = None


def get_knowledge_base() -> GDS2KnowledgeBase:
    """Get global knowledge base instance."""
    global _kb_instance
    if _kb_instance is None:
        _kb_instance = GDS2KnowledgeBase()
    return _kb_instance


def preload_knowledge_base():
    """Pre-initialize knowledge base in a background thread.

    Call this early (e.g., at graph creation time) so that the
    SentenceTransformer model download and LanceDB init happen
    before the agent node needs them.  Safe to call multiple times.
    """
    import threading

    def _init():
        try:
            kb = get_knowledge_base()
            kb._ensure_initialized()
            logger.info("Knowledge base pre-initialized successfully")
        except Exception as e:
            logger.warning(f"Knowledge base pre-init failed (non-fatal): {e}")

    t = threading.Thread(target=_init, daemon=True, name="kb-preload")
    t.start()
    return t


# ---------------------------------------------------------------------------
# Convenience functions
# ---------------------------------------------------------------------------

def query_similar_pages(snapshot: Dict[str, Any], top_k: int = 3) -> List[Dict]:
    """Convenience function to query similar pages."""
    return get_knowledge_base().query_similar_pages(snapshot, top_k)


def query_error_patterns(error_text: str, top_k: int = 3) -> List[Dict]:
    """Convenience function to query error patterns."""
    return get_knowledge_base().query_error_patterns(error_text, top_k)


def get_tool_suggestion(snapshot: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Convenience function to get RAG-based tool suggestion."""
    return get_knowledge_base().get_tool_suggestion(snapshot)


def record_navigation_trace(
    steps: List[Dict[str, Any]],
    goal: str,
    success: bool,
    total_steps: int,
    user_selections: Optional[Dict[str, Any]] = None,
):
    """Convenience function to record a navigation trace."""
    return get_knowledge_base().record_navigation_trace(
        steps, goal, success, total_steps, user_selections
    )


def query_similar_traces(
    goal: str,
    current_page: Optional[str] = None,
    top_k: int = 3,
    success_only: bool = True,
) -> List[Dict]:
    """Convenience function to query similar navigation traces."""
    return get_knowledge_base().query_similar_traces(
        goal, current_page, top_k, success_only
    )
