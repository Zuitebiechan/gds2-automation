package com.gds2.agent;

import javafx.application.Platform;
import javafx.collections.ObservableList;
import javafx.scene.Node;
import javafx.scene.Parent;
import javafx.scene.Scene;
import javafx.scene.control.Button;
import javafx.scene.control.Label;
import javafx.scene.control.ListView;
import javafx.stage.Stage;

import java.util.*;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.logging.Level;
import java.util.logging.Logger;

/**
 * Identifies the current GDS2 page by inspecting the JavaFX scene graph.
 *
 * This replaces the fragile Python-side button-based heuristic with
 * reliable Java-side scene graph analysis. Since we run inside the
 * GDS2 JVM (via -javaagent), we have direct access to all JavaFX
 * nodes, their properties, visibility, and enabled state.
 *
 * Page identification strategy (in priority order):
 * 1. Window title keywords ("Data Display", "DTC", etc.)
 * 2. Unique button presence ("Create Report" -> DATA_DISPLAY,
 *    "Diagnostics" + "Update" -> MAIN_MENU)
 * 3. List content patterns ("Data Display" item -> MODULE_SUBMENU,
 *    "Module Diagnostics" item -> DIAGNOSTICS_MENU,
 *    items with "[K20]" brackets -> MODULE_LIST)
 * 4. Button combination + list emptiness heuristics
 */
public class PageIdentifier {

    private static final Logger LOGGER = Logger.getLogger(PageIdentifier.class.getName());

    // Page ID constants - must match Python GDS2Page enum values exactly
    public static final String PAGE_UNKNOWN = "unknown";
    public static final String PAGE_MAIN_MENU = "main_menu";
    public static final String PAGE_DEVICE_EXPLORER = "device_explorer";
    public static final String PAGE_VEHICLE_SELECTION = "vehicle_selection";
    public static final String PAGE_DIAGNOSTICS_MENU = "diagnostics_menu";
    public static final String PAGE_MODULE_LIST = "module_list";
    public static final String PAGE_MODULE_SUBMENU = "module_submenu";
    public static final String PAGE_DATA_LIST = "data_list";
    public static final String PAGE_SUB_DATA_LIST = "sub_data_list";
    public static final String PAGE_DATA_DISPLAY = "data_display";
    public static final String PAGE_LOADING = "loading";
    public static final String PAGE_J2534_DISCONNECT = "j2534_disconnect";

    /**
     * Identify the current GDS2 page.
     *
     * Must be called from the JavaFX Application Thread (via Platform.runLater).
     *
     * @return NavResult with page_id, page_title, confidence, and evidence
     */
    public NavigationController.NavResult identifyPage() {
        CountDownLatch latch = new CountDownLatch(1);
        NavigationController.NavResult[] resultHolder = new NavigationController.NavResult[1];

        Platform.runLater(() -> {
            try {
                resultHolder[0] = identifyPageOnFxThread();
            } catch (Exception e) {
                LOGGER.log(Level.WARNING, "Page identification failed", e);
                resultHolder[0] = new NavigationController.NavResult(false, "Error: " + e.getMessage());
            } finally {
                latch.countDown();
            }
        });

        try {
            if (!latch.await(5, TimeUnit.SECONDS)) {
                return new NavigationController.NavResult(false, "Timeout identifying page");
            }
        } catch (InterruptedException e) {
            return new NavigationController.NavResult(false, "Interrupted");
        }

        return resultHolder[0] != null
                ? resultHolder[0]
                : new NavigationController.NavResult(false, "Unknown error");
    }

    /**
     * Core page identification logic. Runs on JavaFX Application Thread.
     */
    private NavigationController.NavResult identifyPageOnFxThread() {
        // Find the main GDS2 stage (not modal dialogs)
        Stage mainStage = null;
        Stage modalStage = null;

        ObservableList<Stage> stages = com.sun.javafx.stage.StageHelper.getStages();
        for (Stage stage : stages) {
            if (!stage.isShowing()) continue;

            if (stage.getOwner() != null || stage.getModality() != javafx.stage.Modality.NONE) {
                modalStage = stage;
            } else {
                String title = stage.getTitle();
                if (title != null && title.startsWith("GDS")) {
                    mainStage = stage;
                }
            }
        }

        // Fallback: if no GDS-titled stage found, use first non-modal
        if (mainStage == null) {
            for (Stage stage : stages) {
                if (stage.isShowing() && stage != modalStage) {
                    mainStage = stage;
                    break;
                }
            }
        }

        if (mainStage == null) {
            return new NavigationController.NavResult(true, "No GDS2 window found")
                    .withData("page_id", PAGE_UNKNOWN)
                    .withData("confidence", "none");
        }

        // Check for modal dialogs first (Device Explorer, warnings)
        if (modalStage != null) {
            // Check if it's an AWT Device Explorer dialog
            java.awt.Window[] windows = java.awt.Window.getWindows();
            for (java.awt.Window win : windows) {
                if (win.isShowing() && win instanceof java.awt.Dialog) {
                    java.awt.Dialog dlg = (java.awt.Dialog) win;
                    String dlgTitle = dlg.getTitle();
                    if (dlgTitle != null && dlgTitle.contains("Device")) {
                        return new NavigationController.NavResult(true, "Device Explorer dialog detected")
                                .withData("page_id", PAGE_DEVICE_EXPLORER)
                                .withData("confidence", "high")
                                .withData("evidence", "AWT dialog with title: " + dlgTitle);
                    }
                }
            }
        }

        // Get scene info from main stage
        Scene scene = mainStage.getScene();
        if (scene == null || scene.getRoot() == null) {
            return new NavigationController.NavResult(true, "No scene")
                    .withData("page_id", PAGE_UNKNOWN)
                    .withData("confidence", "none");
        }

        Parent root = scene.getRoot();
        String windowTitle = mainStage.getTitle();

        // Collect scene graph data for identification
        Set<String> buttonTexts = new HashSet<>();
        Map<String, Boolean> buttonEnabled = new HashMap<>();
        List<String> listItems = new ArrayList<>();
        List<String> labelTexts = new ArrayList<>();
        scanSceneGraph(root, buttonTexts, buttonEnabled, listItems, labelTexts);

        // ============================================================
        // IDENTIFICATION RULES (order matters - most specific first)
        // ============================================================

        String pageId;
        String confidence;
        String evidence;

        // Rule 1: Window title contains "Data Display"
        if (windowTitle != null && windowTitle.contains("Data Display")) {
            pageId = PAGE_DATA_DISPLAY;
            confidence = "high";
            evidence = "window_title contains 'Data Display'";
        }
        // Rule 2: "Create Report" button visible -> DATA_DISPLAY
        else if (buttonTexts.contains("Create Report")) {
            pageId = PAGE_DATA_DISPLAY;
            confidence = "high";
            evidence = "button 'Create Report' present";
        }
        // Rule 3: Window title contains "DTC"
        else if (windowTitle != null && (windowTitle.contains("DTC") || windowTitle.contains("Diagnostic Trouble"))) {
            pageId = PAGE_DATA_DISPLAY;
            confidence = "high";
            evidence = "window_title contains DTC indicator";
        }
        // Rule 4: "Diagnostics" + "Update" buttons -> MAIN_MENU
        else if (buttonTexts.contains("Diagnostics") && buttonTexts.contains("Update")) {
            pageId = PAGE_MAIN_MENU;
            confidence = "high";
            evidence = "buttons 'Diagnostics' + 'Update' present";
        }
        // Rule 5: Transitional loading states (no list content yet)
        else if (listItems.isEmpty() && buttonTexts.isEmpty()) {
            pageId = PAGE_LOADING;
            confidence = "high";
            evidence = "no buttons and no list items (transition/loading)";
        }
        // Rule 6: Transitional loading (stale deep-page toolbar + Enter)
        else if (listItems.isEmpty()
                && buttonTexts.contains("Enter")
                && (buttonTexts.contains("Back") || buttonTexts.contains("Vehicle Menu"))) {
            pageId = PAGE_LOADING;
            confidence = "high";
            evidence = "Enter + deep-page toolbar buttons with empty list (transition/loading)";
        }
        // Rule 7: Ambiguous toolbar-only deep page should default to LOADING,
        // not disconnect, to avoid false positives during screen repaint.
        else if (listItems.isEmpty()
                && buttonTexts.contains("Back")
                && buttonTexts.contains("Home")
                && !buttonTexts.contains("OK")
                && !buttonTexts.contains("Enter")
                && !buttonTexts.contains("Diagnostics")
                && !buttonTexts.contains("Update")
                && !buttonTexts.contains("Disconnect")
                && !buttonTexts.contains("Select Device")
                && !buttonTexts.contains("Create Report")) {
            pageId = PAGE_LOADING;
            confidence = "medium";
            evidence = "toolbar-only deep page with empty list (likely transient loading)";
        }
        // Rule 8: Lost communication page (J2534 disconnect)
        else if (listItems.isEmpty()
                && buttonTexts.contains("Back")
                && !buttonTexts.contains("Enter")
                && !buttonTexts.contains("Diagnostics")
                && !buttonTexts.contains("Update")
                && !buttonTexts.contains("Create Report")
                && (buttonTexts.contains("OK")
                    || containsAny(labelTexts, Arrays.asList(
                            "j2534", "disconnect", "communication", "lost", "connection")))) {
            pageId = PAGE_J2534_DISCONNECT;
            confidence = buttonTexts.contains("OK") ? "high" : "medium";
            evidence = buttonTexts.contains("OK")
                    ? "Back + OK with empty list and no navigation markers"
                    : "disconnect keywords detected in labels with Back + empty list";
        }
        // Rule 9: List contains "Data Display" item -> MODULE_SUBMENU
        else if (containsItem(listItems, "Data Display")) {
            pageId = PAGE_MODULE_SUBMENU;
            confidence = "high";
            evidence = "list contains 'Data Display' item";
        }
        // Rule 10: List contains "Module Diagnostics" -> DIAGNOSTICS_MENU
        else if (containsItem(listItems, "Module Diagnostics")) {
            pageId = PAGE_DIAGNOSTICS_MENU;
            confidence = "high";
            evidence = "list contains 'Module Diagnostics' item";
        }
        // Rule 11: List items contain brackets like [K20] -> MODULE_LIST
        else if (hasModulePattern(listItems)) {
            pageId = PAGE_MODULE_LIST;
            confidence = "high";
            evidence = "list items contain module code patterns [...]";
        }
        // Rule 12: Has list items + Back button (but no markers above) -> DATA_LIST
        else if (!listItems.isEmpty() && buttonTexts.contains("Back")) {
            pageId = PAGE_DATA_LIST;
            confidence = "medium";
            evidence = "has list items + Back button, no specific markers";
        }
        // Rule 13: "Enter" button, no list items, and specific button pattern -> VEHICLE_SELECTION
        //   Vehicle Selection has: Enter visible, possibly Back/Disconnect/Select Device
        //   Diagnostics Menu also has Enter, but it has list items (checked above)
        else if (buttonTexts.contains("Enter") && listItems.isEmpty()) {
            // Double-check: Vehicle Selection typically has Disconnect or Select Device
            boolean hasVehicleButtons = buttonTexts.contains("Disconnect")
                    || buttonTexts.contains("Select Device");
            // Or: has Enter but NOT Home+VehicleMenu (those appear on deeper pages)
            boolean hasDeepPageButtons = buttonTexts.contains("Home")
                    && buttonTexts.contains("Vehicle Menu");

            if (hasVehicleButtons || !hasDeepPageButtons) {
                pageId = PAGE_VEHICLE_SELECTION;
                confidence = hasVehicleButtons ? "high" : "medium";
                evidence = hasVehicleButtons
                        ? "Enter + vehicle-specific buttons present, no list items"
                        : "Enter button, no list items, no deep-page buttons";
            } else {
                pageId = PAGE_UNKNOWN;
                confidence = "low";
                evidence = "Enter button present but ambiguous context";
            }
        }
        // Rule 14: "Disconnect" or "Select Device" without Enter -> still VEHICLE_SELECTION
        else if (buttonTexts.contains("Disconnect") || buttonTexts.contains("Select Device")) {
            pageId = PAGE_VEHICLE_SELECTION;
            confidence = "high";
            evidence = "vehicle-specific button present";
        }
        // Fallback
        else {
            pageId = PAGE_UNKNOWN;
            confidence = "low";
            evidence = "no matching rule";
        }

        LOGGER.info(String.format("Page identified: %s (confidence=%s, evidence=%s)",
                pageId, confidence, evidence));

        NavigationController.NavResult result = new NavigationController.NavResult(
                true, "Page identified: " + pageId);
        result.withData("page_id", pageId);
        result.withData("confidence", confidence);
        result.withData("evidence", evidence);
        result.withData("window_title", windowTitle != null ? windowTitle : "");
        result.withData("buttons", new ArrayList<>(buttonTexts));
        result.withData("list_item_count", listItems.size());
        result.withData("has_modal", modalStage != null);

        return result;
    }

    /**
     * Recursively scan the scene graph collecting buttons, lists, and labels.
     */
    private void scanSceneGraph(Node node,
                                Set<String> buttonTexts,
                                Map<String, Boolean> buttonEnabled,
                                List<String> listItems,
                                List<String> labelTexts) {
        if (node == null) return;

        // Collect buttons
        if (node instanceof Button) {
            Button btn = (Button) node;
            String text = btn.getText();
            if (text != null && !text.trim().isEmpty() && btn.isVisible()) {
                // Only include enabled buttons (matches existing Python behavior)
                if (!btn.isDisabled()) {
                    buttonTexts.add(text.trim());
                }
                buttonEnabled.put(text.trim(), !btn.isDisabled());
            }
        }

        // Collect list items (from first visible ListView)
        if (node instanceof ListView && listItems.isEmpty()) {
            ListView<?> listView = (ListView<?>) node;
            if (listView.isVisible()) {
                ObservableList<?> items = listView.getItems();
                for (Object item : items) {
                    if (item != null) {
                        listItems.add(item.toString());
                    }
                }
            }
        }

        // Collect labels (for additional context)
        if (node instanceof Label) {
            Label label = (Label) node;
            String text = label.getText();
            if (text != null && !text.trim().isEmpty() && label.isVisible()) {
                labelTexts.add(text.trim());
            }
        }

        // Recurse into children
        if (node instanceof Parent) {
            for (Node child : ((Parent) node).getChildrenUnmodifiable()) {
                scanSceneGraph(child, buttonTexts, buttonEnabled, listItems, labelTexts);
            }
        }
    }

    /**
     * Check if any list item contains the given text (case-insensitive contains).
     */
    private boolean containsItem(List<String> items, String text) {
        for (String item : items) {
            if (item.contains(text)) return true;
        }
        return false;
    }

    /**
     * Check if list items contain module code patterns like [K20], [P16], etc.
     */
    private boolean hasModulePattern(List<String> items) {
        for (String item : items) {
            if (item.contains("[") && item.contains("]")) return true;
        }
        return false;
    }

    /**
     * Check whether any label contains one of the marker substrings (case-insensitive).
     */
    private boolean containsAny(List<String> labels, List<String> markers) {
        for (String label : labels) {
            String normalized = label.toLowerCase(Locale.ROOT);
            for (String marker : markers) {
                if (normalized.contains(marker.toLowerCase(Locale.ROOT))) {
                    return true;
                }
            }
        }
        return false;
    }
}
