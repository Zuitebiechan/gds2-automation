# Archive - PyAutoGUI/OpenCV Legacy Code

This folder contains legacy code that used PyAutoGUI and OpenCV for UI automation.
These have been replaced by the Java Agent-based approach.

## Archived on: 2026-02-02

## Contents

### src/core/
- `driver.py` - GDS2Driver using pywinauto + PyAutoGUI
- `template_matcher.py` - Multi-scale template matching using OpenCV
- `locators.py` - UI element locator definitions

### src/workflows/
- `base_workflow.py` - Base class with PyAutoGUI template matching
- `read_data_display.py` - Legacy workflow using PyAutoGUI + keyboard navigation

### src/streaming/
- `realtime_collector.py` - Data collector using PyAutoGUI to click "Create Report" button

### images/
- `buttons/` - Button template images for template matching
- `devices/` - Device template images for Device Explorer
- `list_items/` - List item templates
- `pages/` - Page header templates
- `tabs/` - Tab templates
- `manifest.json` - Template image manifest

## Why Archived

The Java Agent approach provides:
1. **No screen dependency** - Works without GDS2 being visible
2. **Faster execution** - Direct API calls vs screenshot+template matching
3. **More reliable** - No issues with DPI scaling, window occlusion
4. **Cloud-ready** - Can be networked for remote deployment

## Current Implementation

See `src/workflows/read_data_display_agent.py` and `src/streaming/agent_navigator.py` for the current Agent-based implementation.
