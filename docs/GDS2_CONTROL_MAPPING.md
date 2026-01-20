# GDS2 Control Mapping Document

Generated: 2026-01-15

## Overview

This document maps all UI controls discovered in GDS2 diagnostic software through automated exploration.
GDS2 is a JavaFX application, so all auto_ids follow the pattern `JavaFXNNN`.

**Important Notes:**
- Auto IDs are dynamic and change between sessions - use title/control_type for reliable selection
- Use `invoke()` pattern for buttons instead of `click_input()` for more reliable operation
- Always check for loading indicators before proceeding

---

## Navigation Flow

```
Main Menu
├── Diagnostics
│   ├── Module Diagnostics → Module List → Module Functions
│   ├── Vehicle Diagnostics → Module List → Module Functions
│   ├── System Diagnostics
│   └── Session Manager
├── Preferences
├── Review Stored Data
├── Release Notes
└── Language
```

---

## Page Structures

### 1. Main Menu

**Entry:** Launch GDS2 or click Home button

**Buttons:**
| Title | Description | Notes |
|-------|-------------|-------|
| Diagnostics | Enter diagnostics mode | Main entry point |
| Preferences | Settings | |
| Review Stored Data | View saved sessions | |
| Release Notes | Version info | |
| Language | Language settings | |

---

### 2. Vehicle Selection Page

**Indicators:** "Vehicle data is loaded", "Press ENTER to start Diagnostics"

**Buttons:**
| Title | Description | Notes |
|-------|-------------|-------|
| Enter | Proceed to diagnostics | Click after vehicle selection |
| Back | Return to previous | May be disabled |
| Home | Return to main menu | May be disabled |

**Tables:**
- Vehicle history table with VIN, Make, Model, Year

---

### 3. Diagnostics Main Menu

**Entry:** Click Diagnostics from Main Menu or Enter from Vehicle Selection

**List Items (JavaFX716):**
| Item | Description |
|------|-------------|
| Module Diagnostics | Direct module access |
| Vehicle Diagnostics | Module list by system |
| System Diagnostics | System-level diagnostics |
| Session Manager | Manage diagnostic sessions |

**Buttons:**
| Title | Description | Notes |
|-------|-------------|-------|
| Back | Return to previous | Disabled on main page |
| Home | Return to main menu | |
| Vehicle Menu | Vehicle options | May be disabled |
| Enter | Select highlighted item | |

---

### 4. Module List (Vehicle Diagnostics)

**Entry:** Select "Vehicle Diagnostics" from Diagnostics Main

**List Items:** 32 modules identified
| Code | Module Name |
|------|-------------|
| [K20] | Engine Control Module |
| [K71] | Transmission Control Module |
| [K17] | Electronic Brake Control Module |
| [K9] | Body Control Module |
| [K38] | Chassis Control Module |
| [K36] | Inflatable Restraint Sensing and Diagnostic Module |
| [K26] | Headlamp Control Module |
| [K33] | HVAC Control Module |
| [P16] | Instrument Cluster |
| [A11] | Radio |
| [K73] | Telematics Communication Interface Control Module |
| ... | (32 total modules) |

**Navigation:**
1. Select module from list
2. Click Enter to access module functions

---

### 5. Module Functions Page

**Entry:** Select module from list and click Enter

**List Items (JavaFX1164):**
| Item | Auto ID Pattern | Description |
|------|-----------------|-------------|
| Diagnostic Trouble Codes (DTC) | JavaFX1177 | Read/Clear DTCs |
| Identification Information | JavaFX1180 | Module info |
| Data Display | JavaFX1183 | Live data (PIDs) |
| Control Functions | JavaFX1186 | Active tests |
| Configuration/Reset Functions | JavaFX1189 | Resets/configs |
| Inspection/Maintenance System Info | (ECM only) | I/M status |

**Tables:**
- Selected Vehicle (JavaFX1228): Make, Model, Year
- Vehicle Configuration (JavaFX1319): Engine, Transmission, etc.
- Navigation Path (JavaFX1444): Current location breadcrumb

---

### 6. DTC Sub-Menu (ECM)

**Entry:** Select "Diagnostic Trouble Codes (DTC)" from module functions

**List Items:**
| Item | Description |
|------|-------------|
| DTC Display | View all DTCs |
| Specific DTC | Query specific code |
| Diagnostic Test Status: This Ignition Cycle | Current tests |
| Diagnostic Test Status: Since DTC Clear | Historical tests |
| Freeze Frame/Failure Records | Snapshot data |

---

### 7. DTC Display Page (Main DTC View)

**Entry:** Select "DTC Display" from DTC Sub-Menu

**THIS IS THE KEY PAGE FOR DTC READING**

**Buttons:**
| Title | Auto ID Pattern | Description | State |
|-------|-----------------|-------------|-------|
| Add Bookmark | JavaFX2063 | Save bookmark | Enabled |
| Create Report | JavaFX2066 | Generate report | Enabled |
| **Clear DTCs** | JavaFX2073 | Clear all DTCs | Disabled when no comm |
| **Refresh** | JavaFX2076 | Re-read DTCs | Enabled |
| Back | JavaFX2309 | Return | Enabled |
| Home | JavaFX2312 | Main menu | Disabled |
| Vehicle Menu | JavaFX2315 | Vehicle options | Disabled |
| Enter | JavaFX2318 | Select | Disabled |

**Tables:**

**1. Module Status Table (JavaFX2085):**
| Column | Description |
|--------|-------------|
| Status | Communication status icon |
| Control Module Name | Module name |
| Control Module Status | "No Communication" or "OK" |
| DTC Count | Number of DTCs |
| DLC Pin | Connector pins |

**2. DTC Details Table (JavaFX2056):**
| Column | Description |
|--------|-------------|
| Control Module | Which module |
| Type | DTC type |
| **DTC** | DTC code (P0xxx, etc.) |
| Symptom Byte | Symptom code |
| Description | DTC description |
| Symptom Description | Symptom text |
| Status | Current/History |

**3. Info Table (JavaFX2259):**
| Column | Description |
|--------|-------------|
| Category | Info category |
| Decoded Value | Decoded data |

---

## Key Selectors for RPA

### Reliable Selectors (by title, not auto_id)

```python
# Buttons
back_btn = win.child_window(title="Back", control_type="Button")
home_btn = win.child_window(title="Home", control_type="Button")
enter_btn = win.child_window(title="Enter", control_type="Button")
clear_dtc_btn = win.child_window(title="Clear DTCs", control_type="Button")
refresh_btn = win.child_window(title="Refresh", control_type="Button")

# List items (by title)
dtc_item = win.child_window(title="Diagnostic Trouble Codes (DTC)", control_type="ListItem")
ecm_item = win.child_window(title_re=".*Engine Control Module.*", control_type="ListItem")

# Tables (use control_type="Table" and iterate children)
for tbl in win.descendants(control_type="Table"):
    for row in tbl.children():
        text = row.window_text()
```

---

## Loading/Wait Indicators

Always check for these before proceeding:
- "Loading"
- "Please wait"
- "Processing"
- "Reading"
- "Communicating"

```python
def wait_ready(win, timeout=30):
    loading_words = ["Loading", "Please wait", "Processing", "Reading", "Communicating"]
    start = time.time()
    while time.time() - start < timeout:
        loading = False
        for word in loading_words:
            try:
                elem = win.child_window(title_re=f".*{word}.*", timeout=0.3)
                if elem.exists(timeout=0.2):
                    loading = True
                    break
            except:
                pass
        if not loading:
            return True
        time.sleep(0.5)
    return False
```

---

## Best Practices

1. **Use invoke() for buttons:** More reliable than click_input()
   ```python
   btn.invoke()  # Better than btn.click_input()
   ```

2. **Use select() for list items:**
   ```python
   item.select()  # Better than item.click_input()
   ```

3. **Always wait for loading:** Check loading indicators after navigation

4. **Verify selection:** Check is_selected() after selecting items

5. **Check button state:** Verify is_enabled() before clicking

6. **Handle communication errors:** "No Communication" status means VCI disconnected

---

## Error Handling

| Error | Cause | Solution |
|-------|-------|----------|
| No Communication | VCI not connected | Check physical connection |
| Button disabled | Wrong page state | Navigate to correct page |
| Element not found | Page not loaded | Wait for loading, retry |
| Selection not working | Focus issue | Use invoke() or set_focus() first |

---

## Screenshots Reference

All screenshots saved to `exploration_results/`:
- `dtc_display_page.png` - DTC Display page
- `ecm_functions.png` - ECM module functions
- `ecm_dtc.png` - ECM DTC sub-menu
- `after_home.png` - Navigation states
- Various exploration snapshots

---

## Data Files Reference

All JSON data saved to `exploration_results/`:
- `dtc_display_data.json` - Complete DTC Display page data
- `ecm_dtc_data.json` - ECM DTC sub-menu data
- `full_page_controls.json` - Complete control hierarchy
- `dtc_scan_results.json` - DTC content scan results
