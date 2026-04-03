# GDS2 Java Agent - Modified Source

This directory contains the modified source files for the GDS2 Java Agent.

The original JAR is at `C:\tools\gds2-agent\gds2-agent.jar`.

## What Changed

### New File: `PageIdentifier.java`
Adds `get_page_id` command that identifies the current GDS2 page directly from
the JavaFX scene graph. This replaces the fragile Python-side button-based heuristic.

**Identification strategy (priority order):**
1. Clear-DTC dialog signatures (`Add All`, `Clear Records`, `DTC Clear`)
2. Window title keywords ("Data Display", "DTC")
3. Unique button presence (`Create Report` -> `data_display`)
4. List content patterns (`Module Diagnostics` -> `diagnostics_menu`)
5. Button combination + list emptiness heuristics

### Modified File: `CommandMonitor.java`
Adds the `"get_page_id"` case to the `executeCommand()` switch statement.
Delegates to `PageIdentifier.identifyPage()`.

## Protocol

**Command:**
```json
{"id": "abc12345", "action": "get_page_id", "params": {}}
```

**Response:**
```json
{
  "id": "abc12345",
  "success": true,
  "message": "Page identified: module_list",
  "data": {
    "page_id": "module_list",
    "confidence": "high",
    "evidence": "list items contain module code patterns [...]",
    "window_title": "GDS 2 - Module Diagnostics",
    "buttons": ["Back", "Home", "Vehicle Menu", "Enter"],
    "list_item_count": 42,
    "has_modal": false
  }
}
```

## Building

```bat
build.bat
```

Requires JDK 8+ and the original JAR at `C:\tools\gds2-agent\gds2-agent.jar`.
Output must target Java 8 (class version 52) since GDS2 ships with JRE 6/8.

## Page IDs

| Page ID | Python Enum | Description |
|---|---|---|
| `main_menu` | `GDS2Page.MAIN_MENU` | Diagnostics + Update buttons |
| `device_explorer` | `GDS2Page.DEVICE_EXPLORER` | Win32 device selection dialog |
| `vehicle_selection` | `GDS2Page.VEHICLE_SELECTION` | Enter/Disconnect, no list |
| `diagnostics_menu` | `GDS2Page.DIAGNOSTICS_MENU` | `Module Diagnostics` in list |
| `module_list` | `GDS2Page.MODULE_LIST` | Items with `[K20]`-style patterns |
| `module_submenu` | `GDS2Page.MODULE_SUBMENU` | `Data Display` in list |
| `data_list` | `GDS2Page.DATA_LIST` | List items + Back button |
| `data_display` | `GDS2Page.DATA_DISPLAY` | `Create Report` button |
| `clear_dtcs_selection` | `GDS2Page.CLEAR_DTCS_SELECTION` | Clear-DTC module selection page |
| `clear_dtcs_confirmation` | `GDS2Page.CLEAR_DTCS_CONFIRMATION` | Final DTC Clear confirmation page |
| `unknown` | `GDS2Page.UNKNOWN` | No matching rule |
