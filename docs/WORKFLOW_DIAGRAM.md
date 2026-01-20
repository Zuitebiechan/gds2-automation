# GDS2 RPA Demo Workflow

**Complete Navigation Flow: Main Menu → DTC Data**

This document provides a comprehensive visual workflow of the GDS2 RPA automation demo, including the techniques used at each step and key code references.

---

## High-Level Flow Overview

```mermaid
flowchart TB
    subgraph Phase1["Phase 1: Common Navigation Flow"]
        A[Main Menu] -->|"click_button()"| B[Device Explorer Popup]
        B -->|"select_device()"| C[Vehicle Selection]
        C -->|"handle_vehicle_selection()"| D{Warning Dialog?}
        D -->|Yes| E[Dismiss Dialog]
        D -->|No| F[Loading Screen]
        E --> F
        F -->|"wait_for_ready()"| G[Diagnostics Menu]
    end

    subgraph Phase2["Phase 2: Task-Specific Navigation"]
        G -->|"keyboard navigation"| H[Vehicle Diagnostics]
        H -->|"select + Enter"| I[Vehicle DTC Information]
    end

    subgraph Phase3["Phase 3: Data Collection"]
        I -->|"wait_for_dtc_data_loaded()"| J[Data Ready]
        J -->|"click Create Report"| K[HTML Report Generated]
        K -->|"GDS2ReportParser"| L[Parsed DTC Data]
    end

    style Phase1 fill:#e1f5fe
    style Phase2 fill:#fff3e0
    style Phase3 fill:#e8f5e9
```

---

## Detailed Step-by-Step Flow

```mermaid
flowchart TD
    subgraph START["Start Point"]
        S1["GDS2 Application Open<br/>User logged in<br/>Vehicle connected"]
    end

    subgraph STEP1["Step 1: Click Diagnostics Button"]
        direction TB
        A1["Main Menu Page"]
        A2["Technique: invoke() method"]
        A3["Code: worker.click_button<br/>(selectors.DIAGNOSTICS_BUTTON)"]
        A1 --> A2 --> A3
    end

    subgraph STEP2["Step 2: Device Explorer Popup"]
        direction TB
        B1["Popup Window Appears"]
        B2["Technique: Desktop() + child_window()"]
        B3["Multi-strategy device selection:<br/>1. DataItem search<br/>2. Text element search<br/>3. ListItem search<br/>4. General element search"]
        B4["Code: device_window.child_window<br/>(title='Continue').click_input()"]
        B1 --> B2 --> B3 --> B4
    end

    subgraph STEP3["Step 3: Vehicle Selection Page"]
        direction TB
        C1["Vehicle Selection Page"]
        C2["Technique: invoke() for button click"]
        C3["Check: Module Diagnostics ListItem<br/>exists = already at menu"]
        C4["Code: enter_btn.invoke()"]
        C1 --> C2 --> C3 --> C4
    end

    subgraph STEP4["Step 4: Warning Dialog"]
        direction TB
        D1["Optional Warning Dialog"]
        D2["Technique: Desktop().windows() scan"]
        D3["Look for OK button in any<br/>GDS/Warning titled window"]
        D4["Code: ok_btn.invoke() or<br/>ok_btn.click_input()"]
        D1 --> D2 --> D3 --> D4
    end

    subgraph STEP5["Step 5: Wait for Loading"]
        direction TB
        E1["Loading Screen"]
        E2["Technique: Polling + text detection"]
        E3["Check for: Loading, Please wait,<br/>Processing, Reading, Communicating"]
        E4["Also check: ProgressBar control"]
        E1 --> E2 --> E3 --> E4
    end

    subgraph STEP6["Step 6: Diagnostics Menu"]
        direction TB
        F1["Diagnostics Menu Page"]
        F2["Technique: Keyboard Navigation"]
        F3["1. Click Module Diagnostics<br/>   to focus list<br/>2. send_keys('{DOWN}')<br/>3. send_keys('{ENTER}')"]
        F4["Why: JavaFX lists don't<br/>respond to select()/click()"]
        F1 --> F2 --> F3 --> F4
    end

    subgraph STEP7["Step 7: Vehicle Diagnostics Submenu"]
        direction TB
        G1["Vehicle Diagnostics Submenu"]
        G2["Technique: ListItem selection + Enter"]
        G3["1. Find 'Vehicle DTC Information'<br/>   in ListItems<br/>2. item.select() or click_input()<br/>3. Find Enter button<br/>4. enter_btn.invoke()"]
        G1 --> G2 --> G3
    end

    subgraph STEP8["Step 8: Wait for DTC Data"]
        direction TB
        H1["Vehicle DTC Information Page"]
        H2["Technique: Button state polling"]
        H3["Check: Clear DTCs button<br/>Disabled = Still loading<br/>Enabled = Data ready"]
        H4["Code: clear_btn.is_enabled()<br/>Timeout: 120 seconds"]
        H1 --> H2 --> H3 --> H4
    end

    subgraph STEP9["Step 9: Create Report"]
        direction TB
        I1["Click Create Report Button"]
        I2["Technique: invoke() + file monitoring"]
        I3["1. Get existing report files<br/>2. Click Create Report<br/>3. Wait for new file in<br/>   %LOCALAPPDATA%/Temp/GDS 2/"]
        I1 --> I2 --> I3
    end

    subgraph STEP10["Step 10: Parse HTML Report"]
        direction TB
        J1["HTML Report File"]
        J2["Technique: BeautifulSoup parsing"]
        J3["Extract:<br/>- VehicleInfo (VIN, Make, Model)<br/>- ModuleStatus (name, dtc_count)<br/>- DTCInfo (code, description)"]
        J4["Return: Structured dict<br/>with 31 DTCs"]
        J1 --> J2 --> J3 --> J4
    end

    S1 --> STEP1
    STEP1 --> STEP2
    STEP2 --> STEP3
    STEP3 --> STEP4
    STEP4 --> STEP5
    STEP5 --> STEP6
    STEP6 --> STEP7
    STEP7 --> STEP8
    STEP8 --> STEP9
    STEP9 --> STEP10

    style START fill:#f3e5f5
    style STEP1 fill:#e3f2fd
    style STEP2 fill:#e3f2fd
    style STEP3 fill:#e3f2fd
    style STEP4 fill:#e3f2fd
    style STEP5 fill:#e3f2fd
    style STEP6 fill:#fff8e1
    style STEP7 fill:#fff8e1
    style STEP8 fill:#e8f5e9
    style STEP9 fill:#e8f5e9
    style STEP10 fill:#e8f5e9
```

---

## Technique Summary by Step

```mermaid
flowchart LR
    subgraph Techniques["UI Automation Techniques Used"]
        T1["invoke()"]
        T2["click_input()"]
        T3["send_keys()"]
        T4["child_window()"]
        T5["Desktop()"]
        T6["is_enabled()"]
        T7["wait()"]
    end

    subgraph Steps["Applied In Steps"]
        S1["Buttons: Diagnostics,<br/>Enter, OK, Create Report"]
        S2["Device Selection,<br/>List Items"]
        S3["JavaFX List<br/>Navigation"]
        S4["Element Finding<br/>in Windows"]
        S5["Popup Window<br/>Handling"]
        S6["Data Loading<br/>Detection"]
        S7["Element<br/>Existence Check"]
    end

    T1 --> S1
    T2 --> S2
    T3 --> S3
    T4 --> S4
    T5 --> S5
    T6 --> S6
    T7 --> S7

    style Techniques fill:#e1f5fe
    style Steps fill:#fff3e0
```

---

## Decision Points Flow

```mermaid
flowchart TD
    A[Start Navigation] --> B{GDS2 Running?}
    B -->|No| C[Connect to GDS2]
    B -->|Yes| D[Find Main Window]
    C --> D

    D --> E{At Main Menu?}
    E -->|No| F[Error: Wrong State]
    E -->|Yes| G[Click Diagnostics]

    G --> H{Device Explorer<br/>Appeared?}
    H -->|No, timeout| I[Check if already<br/>at Diagnostics Menu]
    H -->|Yes| J[Select VCI Device]

    J --> K{Device Found?}
    K -->|No| L[Try alternative<br/>selection methods]
    K -->|Yes| M[Click Continue]
    L --> K

    M --> N{Enter Button<br/>Visible?}
    N -->|No| O[Already at<br/>Diagnostics Menu]
    N -->|Yes| P[Click Enter]

    P --> Q{Warning Dialog?}
    Q -->|Yes| R[Click OK]
    Q -->|No| S[Wait for Loading]
    R --> S

    S --> T{Loading Complete?}
    T -->|No, timeout| U[Continue Anyway]
    T -->|Yes| V[At Diagnostics Menu]
    U --> V

    V --> W[Navigate to<br/>Vehicle Diagnostics]
    W --> X[Navigate to<br/>Vehicle DTC Info]

    X --> Y{Clear DTCs<br/>Enabled?}
    Y -->|No| Z[Poll every 2s]
    Z --> Y
    Y -->|Yes, timeout| AA[Data Ready]

    AA --> BB[Create Report]
    BB --> CC{New Report<br/>File Found?}
    CC -->|No| DD[Use Latest Report]
    CC -->|Yes| EE[Parse HTML]
    DD --> EE

    EE --> FF[Return DTC Data]

    style A fill:#e8f5e9
    style FF fill:#e8f5e9
    style F fill:#ffcdd2
```

---

## Code Architecture Flow

```mermaid
flowchart TB
    subgraph API["API Layer"]
        A1["routes.py<br/>POST /api/v1/diagnostic/read-dtc"]
    end

    subgraph Task["Task Layer"]
        B1["ReadVehicleDTCTask"]
        B2["execute(worker)"]
        B3["_create_and_parse_report()"]
    end

    subgraph Worker["Worker Layer"]
        C1["GDS2Worker"]
        C2["navigate_to_diagnostics_menu()"]
        C3["navigate_to_vehicle_diagnostics()"]
        C4["navigate_to_vehicle_dtc_info()"]
        C5["wait_for_dtc_data_loaded()"]
        C6["click_button()"]
    end

    subgraph Parser["Report Parser"]
        D1["GDS2ReportParser"]
        D2["find_new_report()"]
        D3["parse_dtc_report()"]
    end

    subgraph Vision["Vision Layer (Optional)"]
        E1["OpenCVScreenshotComparator"]
        E2["wait_for_ui_change()"]
        E3["capture_and_wait_for_change()"]
    end

    subgraph UI["UI Automation"]
        F1["pywinauto"]
        F2["Application(backend='uia')"]
        F3["child_window() / invoke()"]
    end

    A1 --> B1
    B1 --> B2
    B2 --> C2
    B2 --> C3
    B2 --> C4
    B2 --> C5
    B2 --> B3
    B3 --> D1
    D1 --> D2
    D2 --> D3

    C2 --> C6
    C3 --> C6
    C4 --> C6
    C6 --> F1
    F1 --> F2
    F2 --> F3

    C5 -.-> E1
    E1 --> E2
    E1 --> E3

    style API fill:#e1f5fe
    style Task fill:#fff3e0
    style Worker fill:#e8f5e9
    style Parser fill:#f3e5f5
    style Vision fill:#fce4ec
    style UI fill:#f5f5f5
```

---

## Data Flow

```mermaid
flowchart LR
    subgraph Input["Input"]
        I1["VCI Device Name<br/>'SM2 USB'"]
    end

    subgraph Process["Processing Steps"]
        P1["UI Navigation"]
        P2["Data Loading"]
        P3["HTML Generation"]
        P4["File Parsing"]
    end

    subgraph Output["Output"]
        O1["DTCInfo[]"]
        O2["ModuleStatus[]"]
        O3["VehicleInfo"]
    end

    I1 --> P1
    P1 -->|"pywinauto"| P2
    P2 -->|"GDS2 Internal"| P3
    P3 -->|"BeautifulSoup"| P4
    P4 --> O1
    P4 --> O2
    P4 --> O3

    style Input fill:#e3f2fd
    style Process fill:#fff8e1
    style Output fill:#e8f5e9
```

---

## Key Code Snippets

### Step 1: Click Diagnostics Button
```python
# Location: worker.py:935
self.click_button(self.selectors.DIAGNOSTICS_BUTTON, wait_after=2)
# Uses invoke() method for reliable JavaFX button clicks
```

### Step 2: Select VCI Device
```python
# Location: worker.py:600-695
# Multi-strategy selection (4 methods attempted in order):
# 1. DataItem search
device_window.descendants(control_type="DataItem")
# 2. Text element search
device_window.descendants(control_type="Text")
# 3. ListItem search
device_window.descendants(control_type="ListItem")
# 4. General element search
device_window.descendants()
```

### Step 6: Keyboard Navigation (JavaFX Lists)
```python
# Location: worker.py:1077-1099
from pywinauto.keyboard import send_keys

# Focus the list by clicking first item
module_diag = window.child_window(title="Module Diagnostics", control_type="ListItem")
module_diag.click_input()

# Navigate using keyboard
send_keys("{DOWN}")   # Move to Vehicle Diagnostics
send_keys("{ENTER}")  # Activate selection
```

### Step 8: Data Loading Detection
```python
# Location: worker.py:217-259
def wait_for_dtc_data_loaded(self, timeout=120):
    while time.time() - start < timeout:
        clear_btn = window.child_window(title="Clear DTCs", control_type="Button")
        if clear_btn.exists(timeout=2):
            if clear_btn.is_enabled():  # Key check!
                return True  # Data loaded
        time.sleep(2)
```

### Step 10: HTML Report Parsing
```python
# Location: report_parser.py
def parse_dtc_report(self, report_path):
    soup = BeautifulSoup(html_content, "html.parser")

    # Extract vehicle info from header
    vehicle_info = self._parse_vehicle_info(soup)

    # Extract module status from summary table
    module_status = self._parse_module_status(soup)

    # Extract individual DTCs
    dtc_list = self._parse_dtc_details(soup)

    return {
        "vehicle_info": vehicle_info,
        "module_status": module_status,
        "dtc_list": dtc_list,
    }
```

---

## Vision-Enhanced Flow (Optional)

When `enable_screenshot_validation=True`:

```mermaid
flowchart TD
    A[Before Click] -->|"take_screenshot()"| B[Baseline Screenshot]
    B --> C[Perform Click Action]
    C --> D[Poll Loop Start]
    D -->|"take_screenshot()"| E[Current Screenshot]
    E -->|"cv2.calcHist()"| F[Calculate Histograms]
    F -->|"cv2.compareHist()"| G{Similarity < 0.95?}
    G -->|No| H[UI Changed!]
    G -->|Yes, keep polling| D
    H --> I[Continue Navigation]

    style B fill:#e3f2fd
    style E fill:#e3f2fd
    style F fill:#fff3e0
    style G fill:#f3e5f5
```

---

## Summary

| Step | Page | Technique | Key Method |
|------|------|-----------|------------|
| 1 | Main Menu | `invoke()` | `click_button()` |
| 2 | Device Explorer | `Desktop()` + multi-strategy | `select_device()` |
| 3 | Vehicle Selection | `invoke()` | `handle_vehicle_selection()` |
| 4 | Warning Dialog | `Desktop().windows()` scan | `dismiss_warning_dialog()` |
| 5 | Loading | Text/ProgressBar polling | `wait_for_ready()` |
| 6 | Diagnostics Menu | `send_keys()` keyboard nav | `navigate_to_vehicle_diagnostics()` |
| 7 | Vehicle Diagnostics | `select()` + `invoke()` | `navigate_to_vehicle_dtc_info()` |
| 8 | Vehicle DTC Info | `is_enabled()` polling | `wait_for_dtc_data_loaded()` |
| 9 | Create Report | `invoke()` + file watch | `_create_and_parse_report()` |
| 10 | Parse HTML | BeautifulSoup | `GDS2ReportParser.parse_dtc_report()` |

---

## Files Involved

```
src/workers/gds2/
├── worker.py           # Main UI automation logic
├── selectors.py        # UI element locators
├── tasks.py            # Task definitions (ReadVehicleDTCTask)
└── report_parser.py    # HTML report parsing

src/vision/
└── screenshot_comparator.py  # Optional: OpenCV-based UI change detection
```
