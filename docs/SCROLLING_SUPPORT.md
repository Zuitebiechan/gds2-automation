# Scrolling Support Documentation

## Overview

The V2 architecture includes automatic scrolling support to handle UI elements that are not immediately visible on screen.

## Key Features

### 1. Extensibility ✅

The workflow accepts parameters for any module and data category:

**Command Line:**
```bash
# Default
python scripts/run_demo.py

# Custom module and data
python scripts/run_demo.py --module "Transmission Control Module" --data "Transmission Data"
python scripts/run_demo.py --module "Body Control Module" --data "Body Data"
python scripts/run_demo.py --module "Electronic Brake Control Module" --data "Brake Data"
```

**Programmatic:**
```python
from src.workflows import ReadDataDisplayWorkflow

workflow = ReadDataDisplayWorkflow()

# Any module, any data category
result = workflow.execute(
    vci_device="SM2 USB",
    target_module="Transmission Control Module",
    data_category="Transmission Data",
)

if result["success"]:
    print(f"Report: {result['report_path']}")
```

### 2. Automatic Scrolling ✅

The workflow automatically scrolls to find elements not visible on screen.

## Scrolling Methods

### Base Methods (in `BaseWorkflowV2`)

| Method | Description | Parameters |
|--------|-------------|------------|
| `scroll(amount)` | Scroll by specified amount | `amount`: negative=down, positive=up |
| `scroll_down(amount)` | Scroll down | `amount`: units to scroll (default: 3) |
| `scroll_up(amount)` | Scroll up | `amount`: units to scroll (default: 3) |
| `scroll_to_top()` | Scroll to top of list | `max_scrolls`: max attempts (default: 10) |

### Smart Scrolling with VLM

| Method | Description |
|--------|-------------|
| `click_text_vlm_with_scroll()` | Find text with VLM, scroll if not visible, then click |

**Parameters:**
- `target_text`: Text to find (e.g., "Transmission Control Module")
- `apply_offset`: Whether to apply Y coordinate offset (page-dependent)
- `max_scrolls`: Maximum scroll attempts (default: 5)
- `scroll_amount`: Scroll distance each attempt (default: -3)
- `scroll_pause`: Pause between scrolls in seconds (default: 0.5)

## How Scrolling Works

### Algorithm

1. **Try without scrolling first**
   - Uses VLM to find the text on current screen
   - If found, clicks immediately and returns

2. **If not found, scroll and retry**
   - Scrolls down by specified amount
   - Waits for UI to settle
   - Uses VLM to search again
   - Repeats up to `max_scrolls` times

3. **Smart starting position**
   - Workflows call `scroll_to_top()` before searching
   - Ensures consistent behavior regardless of initial scroll position

### Example: Module Selection

```python
def _select_module(self, target_module: str) -> bool:
    # Click on list to ensure focus
    pyautogui.click(300, 400)
    self.wait(0.5)

    # Scroll to top for consistent starting position
    self.scroll_to_top()

    # Find and click with scrolling support (up to 15 scroll attempts)
    if self.click_text_vlm_with_scroll(
        target_module,
        apply_offset=True,
        max_scrolls=15
    ):
        return True
    return False
```

## Usage Examples

### Example 1: Module at Top of List

```bash
python scripts/run_demo.py --module "Engine Control Module"
```

**Behavior:**
- Scrolls to top
- VLM finds "Engine Control Module" immediately (no scrolling needed)
- Clicks and continues

### Example 2: Module in Middle of List

```bash
python scripts/run_demo.py --module "Transmission Control Module"
```

**Behavior:**
- Scrolls to top
- VLM searches, doesn't find it
- Scrolls down 3-5 times
- VLM finds "Transmission Control Module"
- Clicks and continues

### Example 3: Module at Bottom of List

```bash
python scripts/run_demo.py --module "Body Control Module"
```

**Behavior:**
- Scrolls to top
- VLM searches, doesn't find it
- Scrolls down 10-15 times progressively
- VLM finds "Body Control Module" near bottom
- Clicks and continues

### Example 4: Module Not in Vehicle

```bash
python scripts/run_demo.py --module "Video Processing Control Module"
```

**Behavior:**
- Scrolls to top
- VLM searches, doesn't find it
- Scrolls down 15 times (max_scrolls)
- Still not found after all attempts
- Returns error: "Could not select module: Video Processing Control Module"

## Configuration

### Adjusting Scroll Behavior

In `src/workflows/read_data_display.py`:

```python
# Increase max scrolls for long lists
if self.click_text_vlm_with_scroll(
    target_module,
    apply_offset=True,
    max_scrolls=20,  # Try more scrolls
    scroll_amount=-5,  # Scroll more each time
    scroll_pause=0.8,  # Wait longer between scrolls
):
    return True
```

### Different Offset Settings

```python
# Module list: needs Y offset
self.click_text_vlm_with_scroll(target_module, apply_offset=True)

# Data category list: no offset needed
self.click_text_vlm_with_scroll(data_category, apply_offset=False)
```

## Testing Scrolling

### Test Script

Run `scripts/test_scrolling.py` to verify scrolling works:

```bash
# Basic scrolling test
python scripts/test_scrolling.py

# Test VLM + scrolling with specific text
python scripts/test_scrolling.py --text "Transmission Control Module" --offset --scrolls 10
```

## Architecture Benefits

| Feature | Benefit |
|---------|---------|
| **Automatic scrolling** | Handles modules/data at any position in list |
| **Configurable** | Adjust scroll speed, distance, max attempts |
| **Robust** | Always scrolls to top first for consistency |
| **Smart** | Tries without scrolling first (fast path) |
| **Extensible** | Works with any module or data category name |

## Troubleshooting

### Element Not Found After Scrolling

**Possible causes:**
1. Element name doesn't exactly match (case-sensitive)
2. Element doesn't exist in current vehicle
3. Need more scroll attempts (increase `max_scrolls`)
4. Need to click on list first to focus

**Solutions:**
```python
# 1. Increase max_scrolls
max_scrolls=20

# 2. Focus list first
pyautogui.click(300, 400)  # Click list area
self.wait(0.5)

# 3. Adjust scroll amount
scroll_amount=-5  # Scroll more each time
```

### Scrolling Too Fast

**Solution:** Increase pause between scrolls
```python
scroll_pause=1.0  # Wait 1 second between scrolls
```

### Scrolling Not Working

**Check:**
1. List area has focus (click on it first)
2. VLM is properly initialized
3. Target text spelling is correct

## Summary

The V2 architecture provides:

✅ **Full extensibility** - any module, any data category via parameters
✅ **Automatic scrolling** - handles elements anywhere in the list
✅ **Robust VLM integration** - finds variable text regardless of position
✅ **Configurable behavior** - adjust scrolling speed, distance, attempts
✅ **Production-ready** - handles real-world scenarios with long lists
