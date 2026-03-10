package com.gds2.agent;

import com.google.gson.Gson;
import com.google.gson.GsonBuilder;

import java.io.*;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.HashMap;
import java.util.Map;
import java.util.logging.Level;
import java.util.logging.Logger;

/**
 * Monitors a command JSON file and dispatches commands to NavigationController.
 *
 * Protocol:
 * - Python writes command to {dataDir}/command.json
 * - This monitor reads it, dispatches, writes result to {dataDir}/result.json
 * - Command format: {"id": "abc", "action": "get_page_id", "params": {}}
 * - Result format: {"id": "abc", "success": true, "message": "...", "data": {...}}
 *
 * MODIFIED: Added "get_page_id" action that delegates to PageIdentifier.
 */
public class CommandMonitor implements Runnable {

    private static final Logger LOGGER = Logger.getLogger(CommandMonitor.class.getName());

    private final Path commandFile;
    private final Path resultFile;
    private final NavigationController navController;
    private final PageIdentifier pageIdentifier;  // NEW
    private final Gson gson;
    private volatile boolean running;
    private String lastProcessedId;

    public CommandMonitor(String dataDir) {
        this.running = true;
        this.lastProcessedId = null;
        this.commandFile = Paths.get(dataDir, "command.json");
        this.resultFile = Paths.get(dataDir, "result.json");
        this.navController = new NavigationController();
        this.pageIdentifier = new PageIdentifier();  // NEW
        this.gson = new GsonBuilder().setPrettyPrinting().create();

        LOGGER.info("CommandMonitor initialized");
        LOGGER.info("  Command file: " + commandFile);
        LOGGER.info("  Result file: " + resultFile);
    }

    @Override
    public void run() {
        LOGGER.info("CommandMonitor started - watching for commands");
        while (running) {
            try {
                if (Files.exists(commandFile)) {
                    processCommand();
                }
                Thread.sleep(50);
            } catch (InterruptedException e) {
                LOGGER.info("CommandMonitor interrupted");
                break;
            } catch (Exception e) {
                LOGGER.log(Level.WARNING, "Error in command monitor loop", e);
                try {
                    Thread.sleep(100);
                } catch (InterruptedException ie) {
                    break;
                }
            }
        }
        LOGGER.info("CommandMonitor stopped");
    }

    public void stop() {
        this.running = false;
    }

    @SuppressWarnings("unchecked")
    private void processCommand() {
        try {
            String content = new String(Files.readAllBytes(commandFile), "UTF-8");
            Map<String, Object> command = gson.fromJson(content, Map.class);

            if (command == null) return;

            String id = (String) command.get("id");
            if (id == null || id.equals(lastProcessedId)) return;

            String action = (String) command.get("action");
            Map<String, Object> params = (Map<String, Object>) command.get("params");
            if (params == null) params = new HashMap<>();

            LOGGER.info("Processing command: id=" + id + ", action=" + action);

            NavigationController.NavResult navResult = executeCommand(action, params);

            // Build result map
            Map<String, Object> result = new HashMap<>();
            result.put("id", id);
            result.put("success", navResult.success);
            result.put("message", navResult.message);
            result.put("data", navResult.data);
            result.put("timestamp", System.currentTimeMillis());

            // Write result
            try (Writer writer = new OutputStreamWriter(
                    new FileOutputStream(resultFile.toFile()), "UTF-8")) {
                gson.toJson(result, writer);
            }

            // Clean up command file
            Files.delete(commandFile);
            lastProcessedId = id;

            LOGGER.info("Command completed: " + action + " -> "
                    + (navResult.success ? "SUCCESS" : "FAILED"));

        } catch (Exception e) {
            LOGGER.log(Level.SEVERE, "Error processing command", e);
            try {
                Map<String, Object> errorResult = new HashMap<>();
                errorResult.put("id", "error");
                errorResult.put("success", false);
                errorResult.put("message", "Error: " + e.getMessage());
                errorResult.put("timestamp", System.currentTimeMillis());

                try (Writer writer = new OutputStreamWriter(
                        new FileOutputStream(resultFile.toFile()), "UTF-8")) {
                    gson.toJson(errorResult, writer);
                }
                Files.deleteIfExists(commandFile);
            } catch (Exception writeError) {
                LOGGER.log(Level.SEVERE, "Error writing error result", writeError);
            }
        }
    }

    private NavigationController.NavResult executeCommand(String action, Map<String, Object> params) {
        if (action == null) {
            return new NavigationController.NavResult(false, "No action specified");
        }

        switch (action) {
            case "click_button": {
                String text = (String) params.get("text");
                if (text == null) {
                    return new NavigationController.NavResult(false, "Missing 'text' parameter");
                }
                return navController.clickButton(text);
            }

            case "select_list": {
                int listIndex = getIntParam(params, "list_index", 0);
                boolean doubleClick = getBoolParam(params, "double_click", true);

                if (params.containsKey("item_index")) {
                    int itemIndex = getIntParam(params, "item_index", 0);
                    return navController.selectListItem(listIndex, itemIndex, doubleClick);
                } else if (params.containsKey("item_text")) {
                    String itemText = (String) params.get("item_text");
                    return navController.selectListItemByText(listIndex, itemText, doubleClick);
                } else {
                    return new NavigationController.NavResult(false,
                            "Missing 'item_index' or 'item_text' parameter");
                }
            }

            case "get_list": {
                int listIndex = getIntParam(params, "list_index", 0);
                return navController.getListItems(listIndex);
            }

            case "get_buttons":
                return navController.getVisibleButtons();

            case "get_window":
                return navController.getWindowInfo();

            case "wait": {
                int ms = getIntParam(params, "ms", 1000);
                try {
                    Thread.sleep(ms);
                    return new NavigationController.NavResult(true, "Waited " + ms + "ms");
                } catch (InterruptedException e) {
                    return new NavigationController.NavResult(false, "Wait interrupted");
                }
            }

            case "inspect": {
                int maxDepth = getIntParam(params, "max_depth", 0);
                return navController.inspectControls(maxDepth);
            }

            case "inspect_swing":
                return navController.inspectSwingControls();

            case "click_swing_button": {
                String text = (String) params.get("text");
                if (text == null) {
                    return new NavigationController.NavResult(false, "Missing 'text' parameter");
                }
                return navController.clickSwingButton(text);
            }

            case "select_swing_table_row": {
                int tableIndex = getIntParam(params, "table_index", 0);
                int rowIndex = getIntParam(params, "row_index", 0);
                return navController.selectSwingTableRow(tableIndex, rowIndex);
            }

            case "get_swing_table": {
                int tableIndex = getIntParam(params, "table_index", 0);
                return navController.getSwingTableItems(tableIndex);
            }

            // ============== NEW COMMAND ==============
            case "get_page_id":
                return pageIdentifier.identifyPage();

            default:
                return new NavigationController.NavResult(false, "Unknown action: " + action);
        }
    }

    private int getIntParam(Map<String, Object> params, String key, int defaultVal) {
        Object val = params.get(key);
        if (val instanceof Number) {
            return ((Number) val).intValue();
        }
        return defaultVal;
    }

    private boolean getBoolParam(Map<String, Object> params, String key, boolean defaultVal) {
        Object val = params.get(key);
        if (val instanceof Boolean) {
            return (Boolean) val;
        }
        return defaultVal;
    }
}
