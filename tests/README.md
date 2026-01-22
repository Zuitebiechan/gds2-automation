# GDS2 RPA Test Suite

This directory contains comprehensive tests for the GDS2 RPA automation framework.

## Test Organization

```
tests/
├── __init__.py                        # Test package initialization
├── conftest.py                        # Shared pytest fixtures and configuration
├── test_navigation_to_data_display.py # Navigation tests (Main Menu → Data Display)
└── README.md                          # This file
```

## Running Tests

### Prerequisites

1. Install pytest:
   ```bash
   pip install pytest
   ```

2. For unit tests (mocked): No additional requirements
3. For integration tests (live): Requires GDS2 running with hardware connected

### Run All Tests (Unit Tests Only)

```bash
# Run from project root
cd RPA_demo
python -m pytest tests/ -v

# Or run specific test file
python -m pytest tests/test_navigation_to_data_display.py -v
```

### Run Live Integration Tests

Live tests require:
- GDS2 application running and at Main Menu
- SM2 USB VCI device connected
- Vehicle connected

```bash
# Run with --live flag
python -m pytest tests/ -v --live

# Specify custom VCI device
python -m pytest tests/ -v --live --vci-device "SM2 USB"
```

### Run Specific Tests

```bash
# Run specific test class
python -m pytest tests/test_navigation_to_data_display.py::TestNavigationToDataDisplay -v

# Run specific test method
python -m pytest tests/test_navigation_to_data_display.py::TestNavigationToDataDisplay::test_main_menu_click_diagnostics -v

# Run only live tests
python -m pytest tests/ -v --live -m live
```

## Test Categories

### 1. Navigation Tests (`test_navigation_to_data_display.py`)

Tests the complete navigation flow from Main Menu to Data Display page.

**Navigation Path:**
1. Main Menu → Click "Diagnostics"
2. Device Explorer → Select VCI device
3. Vehicle Selection → Click "Enter"
4. Diagnostics Menu → Select "Module Diagnostics"
5. Module List → Select a module
6. Module Options → Click "Data Display"
7. Data Display Page → Create report and parse data

**Test Classes:**

- `TestNavigationToDataDisplay`: Unit tests with mocked driver
  - Tests each navigation step independently
  - Tests page detection methods
  - Tests fluent navigation pattern
  - Tests full navigation flow (mocked)

- `TestLiveNavigationToDataDisplay`: Integration tests with real GDS2
  - `test_live_main_menu_detection`: Verify Main Menu detection
  - `test_live_navigation_to_diagnostics_menu`: Navigate to Diagnostics Menu
  - `test_live_navigation_to_module_diagnostics`: Navigate to Module Diagnostics
  - `test_live_full_navigation_to_data_display`: Complete end-to-end flow

- `TestNavigationPaths`: Path validation tests
  - Validates expected navigation path
  - Verifies page names are correct
  - Checks that all required locators exist

- `TestNavigationErrorHandling`: Error handling tests
  - Tests element not found scenarios
  - Tests timeout handling
  - Tests warning dialog dismissal

## Test Fixtures

Defined in `conftest.py`:

- `mock_driver`: Mock GDS2Driver for unit testing
- `live_driver`: Real GDS2Driver for integration testing (requires --live)
- `vci_device`: VCI device name from command line
- `main_menu_page`: Pre-configured MainMenuPage instance
- `diagnostics_menu_page`: Pre-configured DiagnosticsMenuPage instance
- `vehicle_selection_page`: Pre-configured VehicleSelectionPage instance
- `navigation_helper`: Helper class for navigation tests

## Command-Line Options

| Option | Description | Default |
|--------|-------------|---------|
| `--live` | Run live tests against real GDS2 | False |
| `--vci-device` | VCI device name for live tests | "SM2 USB" |
| `-v` | Verbose output | - |
| `-s` | Show print statements | - |
| `--tb=short` | Short traceback format | - |
| `-k KEYWORD` | Run tests matching keyword | - |
| `-m MARKER` | Run tests with marker | - |

## Test Markers

Tests are marked with pytest markers:

- `@pytest.mark.live`: Requires live GDS2 connection
- `@pytest.mark.slow`: Slow-running tests (navigation)

## Test Output

### Successful Test Run

```
============================= test session starts =============================
platform win32 -- Python 3.11.9, pytest-9.0.2, pluggy-1.6.0
collected 24 items

test_navigation_to_data_display.py::TestNavigationToDataDisplay::test_main_menu_is_displayed PASSED [  4%]
test_navigation_to_data_display.py::TestNavigationToDataDisplay::test_main_menu_click_diagnostics PASSED [  8%]
...
test_navigation_to_data_display.py::TestNavigationToDataDisplay::test_full_navigation_flow_mocked PASSED [ 58%]
test_navigation_to_data_display.py::TestLiveNavigationToDataDisplay::test_live_main_menu_detection SKIPPED [ 62%]
...

================== 20 passed, 4 skipped in 103.90s (0:01:43) ==================
```

### Live Test Run

```bash
$ python -m pytest tests/test_navigation_to_data_display.py --live -v

# Live tests will execute actual GDS2 navigation
# Logs show navigation progress:
# Step 1: Verifying Main Menu...
#   ✓ At Main Menu
# Step 2: Clicking Diagnostics...
#   ✓ Device Explorer appeared
# ...
# ✓ Full navigation to Data Display completed successfully!
```

## Mock Pages

The test file includes mock implementations for pages that haven't been implemented yet:

- `MockModuleListPage`: Simulates Module List page
- `MockModuleOptionsPage`: Simulates Module Options page
- `MockDataSelectionPage`: Simulates Data Selection page
- `MockDataDisplayPage`: Simulates Data Display page

These mocks allow tests to run even before the actual page classes are implemented. When real pages are created, the tests automatically use them instead of the mocks.

## Writing New Tests

### Unit Test Example (Mocked Driver)

```python
def test_my_feature(mock_driver):
    """Test description."""
    # Setup
    mock_driver.element_exists = Mock(return_value=True)

    # Action
    page = MyPage(mock_driver)
    result = page.my_method()

    # Assert
    assert result is not None
    mock_driver.click_button.assert_called()
```

### Integration Test Example (Live Driver)

```python
@pytest.mark.live
def test_my_feature_live(live_driver):
    """Test description."""
    if not request.config.getoption("--live"):
        pytest.skip("Requires --live flag")

    # Test with real GDS2
    page = MyPage(live_driver)
    result = page.my_method()

    assert result is not None
```

## Troubleshooting

### Tests Fail to Import

```bash
# Ensure you're in the project root
cd RPA_demo

# Verify Python path
python -c "import sys; print(sys.path)"
```

### Live Tests Don't Run

```bash
# Make sure --live flag is passed
python -m pytest tests/ --live -v

# Check that GDS2 is running
python -c "from src.core.driver import GDS2Driver; d = GDS2Driver(); d.connect()"
```

### Slow Test Execution

```bash
# Run only fast tests (exclude slow navigation tests)
python -m pytest tests/ -v -m "not slow"

# Run tests in parallel (requires pytest-xdist)
pip install pytest-xdist
python -m pytest tests/ -v -n auto
```

## CI/CD Integration

For continuous integration, run unit tests only:

```bash
# CI pipeline command (no live tests)
python -m pytest tests/ -v --tb=short --junit-xml=test-results.xml
```

For nightly integration tests with hardware:

```bash
# Nightly build command (with live tests)
python -m pytest tests/ -v --live --junit-xml=test-results.xml
```

## Coverage Reports

Generate test coverage reports:

```bash
# Install pytest-cov
pip install pytest-cov

# Run tests with coverage
python -m pytest tests/ -v --cov=src --cov-report=html

# View coverage report
# Open htmlcov/index.html in browser
```

## Test Maintenance

When adding new pages or workflows:

1. Add corresponding tests in `test_navigation_to_data_display.py` or create new test file
2. Update mock pages if needed
3. Add new fixtures to `conftest.py` if shared across tests
4. Update this README with new test information

When locators change:

1. Update `src/core/locators.py`
2. Run tests to verify they still pass
3. Update test assertions if behavior changed

## Best Practices

1. **Use fixtures**: Leverage shared fixtures from `conftest.py`
2. **Mock when possible**: Use mocked driver for unit tests
3. **Mark live tests**: Use `@pytest.mark.live` for integration tests
4. **Descriptive names**: Test names should describe what they test
5. **One assert per test**: Focus each test on one specific behavior
6. **Fast unit tests**: Keep unit tests fast by mocking I/O
7. **Comprehensive live tests**: Integration tests should test real scenarios

## Related Documentation

- [Project README](../README.md)
- [CLAUDE.md](../docs/CLAUDE.md) - Project architecture and design
- [GDS2 Control Mapping](../docs/GDS2_CONTROL_MAPPING.md) - UI element reference
- [Module Data Display Plan](../docs/MODULE_DATA_DISPLAY_PLAN.md) - Navigation flow details
