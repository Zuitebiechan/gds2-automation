"""
Initialize GDS2 Knowledge Base with seed data.

Creates LanceDB tables and populates them with known GDS2 page patterns
derived from the existing NavigationController detection heuristics.

Usage:
    python scripts/init_knowledge_base.py [--db-path data/gds2_knowledge.lance]
"""

import argparse
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Seed data — page patterns extracted from NavigationController heuristics
# ---------------------------------------------------------------------------

SEED_PAGES: List[Dict[str, Any]] = [
    {
        "id": "page_main_menu",
        "page_type": "MAIN_MENU",
        "description": (
            "GDS2 main startup screen. Shows Diagnostics, Update, and other "
            "top-level buttons. No list items. Entry point for all workflows."
        ),
        "text_features": (
            "Buttons: Diagnostics, Update. "
            "No list items. "
            "This is the GDS2 startup landing page."
        ),
        "must_have_buttons": ["Diagnostics", "Update"],
        "usually_has_buttons": ["Review Data", "Preferences", "Language Selection"],
        "must_not_have_buttons": [],
        "list_pattern": "none",
        "list_count_range": "0",
        "detection_rule": "Has 'Diagnostics' AND 'Update' buttons",
        "deterministic_action": "click Diagnostics",
        "is_user_decision": False,
        "screenshot_paths": [],
    },
    {
        "id": "page_device_explorer",
        "page_type": "DEVICE_EXPLORER",
        "description": (
            "Win32 native device selection dialog. Not a JavaFX page — "
            "detected via pywinauto, not Java Agent. Lists VCI devices."
        ),
        "text_features": (
            "Win32 dialog window. Lists VCI devices including 'VCI Proxy (Remote)'. "
            "Has OK/Cancel buttons. Not visible to Java Agent."
        ),
        "must_have_buttons": [],
        "usually_has_buttons": ["OK", "Cancel"],
        "must_not_have_buttons": [],
        "list_pattern": "vci_devices",
        "list_count_range": "1-10",
        "detection_rule": "Win32 dialog detected by DeviceExplorerController.is_visible()",
        "deterministic_action": "select 'VCI Proxy (Remote)' and click OK",
        "is_user_decision": False,
        "screenshot_paths": [],
    },
    {
        "id": "page_vehicle_selection",
        "page_type": "VEHICLE_SELECTION",
        "description": (
            "Vehicle/VIN entry page. Has 'Enter' button but no list items. "
            "May also show 'Disconnect' or 'Select Device' buttons."
        ),
        "text_features": (
            "Buttons: Enter. No list items. "
            "May have Disconnect, Select Device buttons. "
            "No Back button (not a deep page). "
            "This page appears after device connection."
        ),
        "must_have_buttons": ["Enter"],
        "usually_has_buttons": ["Disconnect", "Select Device"],
        "must_not_have_buttons": ["Back", "Create Report"],
        "list_pattern": "none",
        "list_count_range": "0",
        "detection_rule": "Has 'Enter' button AND no list items",
        "deterministic_action": "click Enter (auto-detect vehicle)",
        "is_user_decision": False,
        "screenshot_paths": [],
    },
    {
        "id": "page_diagnostics_menu",
        "page_type": "DIAGNOSTICS_MENU",
        "description": (
            "Diagnostics type selection. List contains 'Module Diagnostics' "
            "and possibly other diagnostic types."
        ),
        "text_features": (
            "List items include: Module Diagnostics. "
            "May also have: Global Diagnostics, Vehicle DTC Info. "
            "Has Back button."
        ),
        "must_have_buttons": [],
        "usually_has_buttons": ["Back"],
        "must_not_have_buttons": [],
        "list_pattern": "contains_module_diagnostics",
        "list_count_range": "1-5",
        "detection_rule": "List items contain 'Module Diagnostics'",
        "deterministic_action": "click 'Module Diagnostics'",
        "is_user_decision": False,
        "screenshot_paths": [],
    },
    {
        "id": "page_module_list",
        "page_type": "MODULE_LIST",
        "description": (
            "ECU module selection page. Lists vehicle modules with bracket "
            "notation like [K20] Engine Control Module. User must choose."
        ),
        "text_features": (
            "List items contain module names with brackets, e.g. "
            "'[K20] Engine Control Module - 2.0L (LTG)'. "
            "Has Back button. Modules identified by [XXX] prefix pattern."
        ),
        "must_have_buttons": [],
        "usually_has_buttons": ["Back"],
        "must_not_have_buttons": [],
        "list_pattern": "bracket_prefixed_modules",
        "list_count_range": "3-50",
        "detection_rule": "List items contain '[' and ']' bracket patterns",
        "deterministic_action": None,
        "is_user_decision": True,
        "screenshot_paths": [],
    },
    {
        "id": "page_module_submenu",
        "page_type": "MODULE_SUBMENU",
        "description": (
            "Module function menu showing diagnostic functions for the selected "
            "module. Lists Data Display, Diagnostic Trouble Codes, Special Functions, etc. "
            "Must contain BOTH a display marker AND a required marker."
        ),
        "text_features": (
            "List items include: Data Display AND at least one of "
            "(Diagnostic Trouble Codes, DTC, Module Information, Special Functions). "
            "Has Back button. Items do NOT contain bracket [XXX] patterns."
        ),
        "must_have_buttons": [],
        "usually_has_buttons": ["Back"],
        "must_not_have_buttons": [],
        "list_pattern": "function_menu_with_display_and_dtc",
        "list_count_range": "2-8",
        "detection_rule": (
            "Has list items with Data Display marker AND required markers "
            "(DTC/Module Information/Special Functions). No bracket patterns."
        ),
        "deterministic_action": "click 'Data Display'",
        "is_user_decision": False,
        "screenshot_paths": [],
    },
    {
        "id": "page_data_list",
        "page_type": "DATA_LIST",
        "description": (
            "Data category selection page. Lists available data categories "
            "for the selected module (e.g. Engine Data 1, Engine Data 2). "
            "User must choose which data category to view."
        ),
        "text_features": (
            "List items are data categories like 'Engine Data 1', 'Engine Data 2', "
            "'Fuel System Data', 'Misfire Data'. Has Back button. "
            "No bracket patterns. No DTC/Special Functions markers."
        ),
        "must_have_buttons": ["Back"],
        "usually_has_buttons": [],
        "must_not_have_buttons": ["Create Report"],
        "list_pattern": "data_categories",
        "list_count_range": "1-30",
        "detection_rule": "Has list items AND Back button, but not MODULE_SUBMENU markers",
        "deterministic_action": None,
        "is_user_decision": True,
        "screenshot_paths": [],
    },
    {
        "id": "page_sub_data_list",
        "page_type": "SUB_DATA_LIST",
        "description": (
            "Sub-data category selection. Appears after selecting certain data "
            "categories that have sub-categories. User has never seen this page; "
            "its layout is unknown. The AI agent must handle it dynamically."
        ),
        "text_features": (
            "Sub-categories of a data category. Appears after DATA_LIST selection. "
            "Layout is unknown — may look similar to DATA_LIST with additional "
            "refinement options. Has Back button."
        ),
        "must_have_buttons": ["Back"],
        "usually_has_buttons": [],
        "must_not_have_buttons": ["Create Report"],
        "list_pattern": "sub_data_categories",
        "list_count_range": "1-20",
        "detection_rule": "Unknown — AI agent must classify dynamically",
        "deterministic_action": None,
        "is_user_decision": True,
        "screenshot_paths": [],
    },
    {
        "id": "page_data_display",
        "page_type": "DATA_DISPLAY",
        "description": (
            "Live data display page. Shows real-time sensor values for the "
            "selected data category. Has 'Create Report' button. "
            "This is the goal state for most diagnostic workflows."
        ),
        "text_features": (
            "Buttons: Create Report. "
            "Shows live PID data values, graphs, and gauges. "
            "This is the final destination for data viewing workflows."
        ),
        "must_have_buttons": ["Create Report"],
        "usually_has_buttons": ["Back"],
        "must_not_have_buttons": [],
        "list_pattern": "pid_values",
        "list_count_range": "0-100",
        "detection_rule": "Has 'Create Report' button",
        "deterministic_action": None,
        "is_user_decision": False,
        "screenshot_paths": [],
    },
    # --- Error / dynamic pages (unknown layout, AI agent must handle) ---
    {
        "id": "page_error_dialog",
        "page_type": "ERROR_DIALOG",
        "description": (
            "Error popup dialog. Can appear on any page during navigation. "
            "Typically has OK/Retry/Cancel buttons and an error message. "
            "Must be dismissed before continuing."
        ),
        "text_features": (
            "Error popup with message text. "
            "Buttons typically include: OK, Retry, Cancel, Close. "
            "May show connection errors, timeout errors, or module communication failures."
        ),
        "must_have_buttons": [],
        "usually_has_buttons": ["OK", "Retry", "Cancel", "Close"],
        "must_not_have_buttons": [],
        "list_pattern": "none",
        "list_count_range": "0",
        "detection_rule": "Error message visible, popup overlay detected",
        "deterministic_action": "click OK or Retry depending on error type",
        "is_user_decision": False,
        "screenshot_paths": [],
    },
    {
        "id": "page_sub_module_selection",
        "page_type": "SUB_MODULE_SELECTION",
        "description": (
            "Sub-module selection page. Appears for some modules that have "
            "sub-variants. User has never seen this page; its layout is unknown. "
            "The AI agent must handle it dynamically."
        ),
        "text_features": (
            "Sub-module variants for a selected module. "
            "May list options like 'ECM - Primary', 'ECM - Secondary'. "
            "Layout unknown — AI agent must classify dynamically. Has Back button."
        ),
        "must_have_buttons": [],
        "usually_has_buttons": ["Back"],
        "must_not_have_buttons": [],
        "list_pattern": "sub_modules",
        "list_count_range": "2-10",
        "detection_rule": "Unknown — AI agent must classify dynamically",
        "deterministic_action": None,
        "is_user_decision": True,
        "screenshot_paths": [],
    },
    {
        "id": "page_loading_progress",
        "page_type": "LOADING_PROGRESS",
        "description": (
            "Loading/progress screen. Shows while GDS2 is communicating with "
            "the vehicle module. Should auto-resolve; no action needed."
        ),
        "text_features": (
            "Progress bar or spinner visible. May show 'Please wait', "
            "'Communicating with module', or similar status text. "
            "No actionable buttons except possibly Cancel."
        ),
        "must_have_buttons": [],
        "usually_has_buttons": ["Cancel"],
        "must_not_have_buttons": ["Diagnostics", "Create Report"],
        "list_pattern": "none",
        "list_count_range": "0",
        "detection_rule": "Progress indicator visible, limited buttons",
        "deterministic_action": "wait for completion",
        "is_user_decision": False,
        "screenshot_paths": [],
    },
]


def create_tables(db, text_model):
    """Create LanceDB tables with seed data."""
    import pyarrow as pa

    logger.info(f"Embedding {len(SEED_PAGES)} seed pages...")

    # Compute text embeddings
    texts = [p["text_features"] for p in SEED_PAGES]
    embeddings = text_model.encode(texts, show_progress_bar=True)

    # Build records
    records = []
    now = datetime.now().isoformat()
    for page, emb in zip(SEED_PAGES, embeddings):
        records.append(
            {
                "id": page["id"],
                "page_type": page["page_type"],
                "description": page["description"],
                "text_features": page["text_features"],
                "text_embedding": emb.tolist(),
                "must_have_buttons": page["must_have_buttons"],
                "usually_has_buttons": page["usually_has_buttons"],
                "must_not_have_buttons": page.get("must_not_have_buttons", []),
                "list_pattern": page["list_pattern"],
                "list_count_range": page["list_count_range"],
                "detection_rule": page["detection_rule"],
                "deterministic_action": page.get("deterministic_action") or "",
                "is_user_decision": page["is_user_decision"],
                "screenshot_paths": page.get("screenshot_paths", []),
                "usage_count": 0,
                "accuracy_rate": 1.0,
                "created_at": now,
                "updated_at": now,
            }
        )

    # Define schema
    dim = len(records[0]["text_embedding"])
    logger.info(f"Embedding dimension: {dim}")

    schema = pa.schema(
        [
            pa.field("id", pa.string()),
            pa.field("page_type", pa.string()),
            pa.field("description", pa.string()),
            pa.field("text_features", pa.string()),
            pa.field("text_embedding", pa.list_(pa.float32(), dim)),
            pa.field("must_have_buttons", pa.list_(pa.string())),
            pa.field("usually_has_buttons", pa.list_(pa.string())),
            pa.field("must_not_have_buttons", pa.list_(pa.string())),
            pa.field("list_pattern", pa.string()),
            pa.field("list_count_range", pa.string()),
            pa.field("detection_rule", pa.string()),
            pa.field("deterministic_action", pa.string()),
            pa.field("is_user_decision", pa.bool_()),
            pa.field("screenshot_paths", pa.list_(pa.string())),
            pa.field("usage_count", pa.int32()),
            pa.field("accuracy_rate", pa.float32()),
            pa.field("created_at", pa.string()),
            pa.field("updated_at", pa.string()),
        ]
    )

    # Create table (overwrite if exists)
    table = db.create_table("gds2_pages", data=records, schema=schema, mode="overwrite")
    logger.info(f"Created 'gds2_pages' table with {table.count_rows()} rows")

    return table


def create_error_patterns_table(db, text_model):
    """Create error patterns table for known GDS2 errors."""

    error_patterns = [
        {
            "id": "err_connection_timeout",
            "error_type": "connection_timeout",
            "error_message_pattern": "Connection timed out|Communication timeout|No response from module",
            "description": "VCI communication timeout. Module not responding.",
            "text_features": "Connection timeout error. Module not responding. VCI communication failed.",
            "recommended_action": "click_retry",
            "action_detail": "Click Retry button. If fails 3 times, go back and re-select module.",
            "success_rate": 0.7,
        },
        {
            "id": "err_lost_communication",
            "error_type": "lost_communication",
            "error_message_pattern": "Lost communication|Communication lost|Connection dropped",
            "description": "Lost communication with vehicle module during operation.",
            "text_features": "Lost communication error. Connection dropped during diagnostic session.",
            "recommended_action": "click_ok_and_retry",
            "action_detail": "Click OK to dismiss, then navigate back and reconnect.",
            "success_rate": 0.6,
        },
        {
            "id": "err_module_not_equipped",
            "error_type": "module_not_equipped",
            "error_message_pattern": "Module not equipped|Not available|Not supported",
            "description": "Selected module is not present on this vehicle.",
            "text_features": "Module not equipped error. Selected module not available on vehicle.",
            "recommended_action": "click_ok_and_go_back",
            "action_detail": "Click OK, go back to module list, select different module.",
            "success_rate": 1.0,
        },
        {
            "id": "err_generic",
            "error_type": "generic_error",
            "error_message_pattern": "Error|Exception|Failed",
            "description": "Generic error dialog. Dismiss and retry.",
            "text_features": "Generic error dialog. Unknown error. Click OK to dismiss.",
            "recommended_action": "click_ok",
            "action_detail": "Click OK to dismiss the error dialog.",
            "success_rate": 0.8,
        },
    ]

    texts = [e["text_features"] for e in error_patterns]
    embeddings = text_model.encode(texts, show_progress_bar=True)

    records = []
    now = datetime.now().isoformat()
    for err, emb in zip(error_patterns, embeddings):
        records.append(
            {
                "id": err["id"],
                "error_type": err["error_type"],
                "error_message_pattern": err["error_message_pattern"],
                "description": err["description"],
                "text_features": err["text_features"],
                "text_embedding": emb.tolist(),
                "recommended_action": err["recommended_action"],
                "action_detail": err["action_detail"],
                "success_rate": err["success_rate"],
                "created_at": now,
                "updated_at": now,
            }
        )

    import pyarrow as pa
    dim = len(records[0]["text_embedding"])

    schema = pa.schema(
        [
            pa.field("id", pa.string()),
            pa.field("error_type", pa.string()),
            pa.field("error_message_pattern", pa.string()),
            pa.field("description", pa.string()),
            pa.field("text_features", pa.string()),
            pa.field("text_embedding", pa.list_(pa.float32(), dim)),
            pa.field("recommended_action", pa.string()),
            pa.field("action_detail", pa.string()),
            pa.field("success_rate", pa.float32()),
            pa.field("created_at", pa.string()),
            pa.field("updated_at", pa.string()),
        ]
    )


    table = db.create_table(
        "gds2_error_patterns", data=records, schema=schema, mode="overwrite"
    )
    logger.info(f"Created 'gds2_error_patterns' table with {table.count_rows()} rows")

    return table


def create_icon_catalog_table(db, applet_data_dir: str):
    """
    Catalog GDS2 AppletData icons into a lookup table.

    These are small UI icons (64x64 px) used for icon-based page element
    identification, NOT full page screenshots.
    """
    icon_dir = Path(applet_data_dir)
    if not icon_dir.exists():
        logger.warning(f"AppletData directory not found: {applet_data_dir}")
        return None

    records = []
    now = datetime.now().isoformat()

    # Categorize icons
    categories = {
        "navigation": ["btnIconNavigationBar_"],
        "data_display": ["btnIconDataDisplay_"],
        "screen": ["scr"],
        "component": ["IconComponent_"],
        "status": ["iconStatusBar", "iconTISConnection"],
        "action": ["btnIcon", "IconAction"],
        "other": [],
    }

    for img_file in sorted(icon_dir.iterdir()):
        if not img_file.is_file():
            continue
        if img_file.suffix.lower() not in (".gif", ".png", ".bmp", ".jpg", ".jpeg"):
            continue

        # Determine category
        cat = "other"
        for category, prefixes in categories.items():
            if any(img_file.name.startswith(p) for p in prefixes):
                cat = category
                break

        # Extract semantic name from filename
        name = img_file.stem
        # Remove common prefixes for cleaner naming
        for prefix in ["btnIcon", "IconComponent_", "IconAction", "icon", "scr"]:
            if name.startswith(prefix):
                name = name[len(prefix) :]
                break
        # Clean up underscores and numbers
        semantic_name = name.replace("_", " ").strip()
        if semantic_name and semantic_name[0].isdigit():
            # Strip leading number + space (e.g. "26 DATA DISPLAY")
            parts = semantic_name.split(" ", 1)
            if len(parts) > 1:
                semantic_name = parts[1]

        records.append(
            {
                "filename": img_file.name,
                "filepath": str(img_file),
                "category": cat,
                "semantic_name": semantic_name,
                "file_size_bytes": img_file.stat().st_size,
                "created_at": now,
            }
        )

    if not records:
        logger.warning("No icon files found")
        return None

    import pyarrow as pa

    schema = pa.schema(
        [
            pa.field("filename", pa.string()),
            pa.field("filepath", pa.string()),
            pa.field("category", pa.string()),
            pa.field("semantic_name", pa.string()),
            pa.field("file_size_bytes", pa.int64()),
            pa.field("created_at", pa.string()),
        ]
    )

    table = db.create_table("gds2_icons", data=records, schema=schema, mode="overwrite")
    logger.info(f"Created 'gds2_icons' table with {table.count_rows()} icons")

    # Log category breakdown
    from collections import Counter

    cat_counts = Counter(r["category"] for r in records)
    for cat, count in sorted(cat_counts.items()):
        logger.info(f"  {cat}: {count} icons")

    return table


def verify_knowledge_base(db):
    """Verify knowledge base was created correctly."""
    logger.info("--- Verification ---")

    # Check pages table
    pages = db.open_table("gds2_pages")
    page_count = pages.count_rows()
    logger.info(f"gds2_pages: {page_count} rows")

    # Test vector search
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer("all-MiniLM-L6-v2")
    query = "Data Display button with DTC options"
    query_emb = model.encode(query)

    results = pages.search(query_emb, vector_column_name="text_embedding").limit(3).to_list()
    logger.info(f"Search '{query}' returned {len(results)} results:")
    for i, r in enumerate(results, 1):
        logger.info(f"  {i}. {r['page_type']} (dist={r.get('_distance', '?'):.4f})")

    # Check error patterns
    errors = db.open_table("gds2_error_patterns")
    logger.info(f"gds2_error_patterns: {errors.count_rows()} rows")

    # Check icons (if exists)
    try:
        icons = db.open_table("gds2_icons")
        logger.info(f"gds2_icons: {icons.count_rows()} rows")
    except Exception:
        logger.info("gds2_icons: not created (no AppletData found)")

    logger.info("--- Verification complete ---")


def main():
    parser = argparse.ArgumentParser(description="Initialize GDS2 Knowledge Base")
    parser.add_argument(
        "--db-path",
        default="data/gds2_knowledge.lance",
        help="Path to LanceDB database (default: data/gds2_knowledge.lance)",
    )
    parser.add_argument(
        "--applet-data",
        default=r"C:\ProgramData\GDS 2\AppData\AppletData",
        help="Path to GDS2 AppletData directory",
    )
    parser.add_argument(
        "--skip-icons",
        action="store_true",
        help="Skip icon cataloging",
    )
    args = parser.parse_args()

    # Resolve db path relative to project root
    project_root = Path(__file__).resolve().parent.parent
    db_path = Path(args.db_path)
    if not db_path.is_absolute():
        db_path = project_root / db_path

    # Ensure parent directory exists
    db_path.parent.mkdir(parents=True, exist_ok=True)

    logger.info(f"Initializing knowledge base at: {db_path}")

    import lancedb
    from sentence_transformers import SentenceTransformer

    # Connect to LanceDB
    db = lancedb.connect(str(db_path))

    # Load text embedding model
    logger.info("Loading text embedding model (all-MiniLM-L6-v2)...")
    t0 = time.time()
    text_model = SentenceTransformer("all-MiniLM-L6-v2")
    logger.info(f"Model loaded in {time.time() - t0:.1f}s")

    # Create tables
    create_tables(db, text_model)
    create_error_patterns_table(db, text_model)

    # Catalog icons
    if not args.skip_icons:
        create_icon_catalog_table(db, args.applet_data)

    # Verify
    verify_knowledge_base(db)

    logger.info("Knowledge base initialization complete!")


if __name__ == "__main__":
    main()
