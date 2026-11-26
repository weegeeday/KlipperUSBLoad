import test from "node:test";
import assert from "node:assert/strict";

import {
  buildSettingsPayload,
  classifyPortStatus,
  formatSyncSummary,
  isDashboardRoute,
  parseExtensionsInput,
  resolveApiBase,
  shouldActivateMainsailPlugin,
  sortPorts,
  summarizeDriveStatus,
} from "../web/state.js";

test("resolveApiBase handles moonraker query override and default root path", () => {
  assert.equal(resolveApiBase("", ""), "");
  assert.equal(resolveApiBase("http://printer.local", ""), "http://printer.local");
  assert.equal(
    resolveApiBase("", "?moonraker=http://192.168.1.50:7125/"),
    "http://192.168.1.50:7125",
  );
});

test("sortPorts orders physical USB port paths numerically", () => {
  const sorted = sortPorts([
    { port_id: "2-1" },
    { port_id: "1-1.10" },
    { port_id: "1-1.2" },
    { port_id: "1-1.1" },
  ]);
  assert.deepEqual(
    sorted.map((p) => p.port_id),
    ["1-1.1", "1-1.2", "1-1.10", "2-1"],
  );
});

test("classifyPortStatus distinguishes mounted, blocked, non-storage, and empty", () => {
  assert.equal(classifyPortStatus({ connected: false }).key, "empty");
  assert.equal(classifyPortStatus({ connected: true, mounted: true }).key, "mounted");
  assert.equal(
    classifyPortStatus({ connected: true, device_class: "non_storage" }).key,
    "non_storage",
  );
  assert.equal(
    classifyPortStatus({ connected: true, device_class: "storage", allowed: false }).key,
    "blocked",
  );
});

test("formatSyncSummary reports indexing and warning states clearly", () => {
  const syncing = formatSyncSummary(
    { state: "syncing", total_files: 10, seen_files: 7, metadata_ready_files: 4 },
    true,
  );
  assert.match(syncing, /7\/10 listed, 4\/10 metadata ready/);

  const ready = formatSyncSummary({ state: "ready", total_files: 10 }, true);
  assert.match(ready, /Ready \(10\/10 files indexed\)/);

  const warn = formatSyncSummary(
    { state: "warning", total_files: 10, missing_files: ["a.gcode", "b.gcode"] },
    true,
  );
  assert.match(warn, /8\/10 ready, 2 timed out/);
});

test("parseExtensionsInput and buildSettingsPayload validate inputs", () => {
  assert.deepEqual(parseExtensionsInput("gcode, .GCO, .g"), [".gcode", ".gco", ".g"]);
  assert.throws(() => parseExtensionsInput(""), /At least one/);

  const payload = buildSettingsPayload({
    port_policy: "allowlist",
    allowed_ports: ["1-1.2", "1-1.2"],
    port_labels: { "1-1.2": "Front USB" },
    max_scan_depth: 2,
    wait_for_metadata: true,
    sync_timeout_seconds: 45,
    gcode_extensions: ".gcode,.gco",
    lcd_notifications: true,
  });
  assert.deepEqual(payload.allowed_ports, ["1-1.2"]);
  assert.equal(payload.port_labels["1-1.2"], "Front USB");
  assert.throws(() =>
    buildSettingsPayload({
      max_scan_depth: 99,
      sync_timeout_seconds: 30,
      gcode_extensions: ".gcode",
    }),
  );
});

test("Mainsail route guards and drive status summaries work cleanly", () => {
  assert.equal(shouldActivateMainsailPlugin("/"), true);
  assert.equal(shouldActivateMainsailPlugin("/files"), true);
  assert.equal(shouldActivateMainsailPlugin("/server/klipper_usb_load/ui/"), false);

  assert.equal(isDashboardRoute("/", ""), true);
  assert.equal(isDashboardRoute("/index.html", ""), true);
  assert.equal(isDashboardRoute("/", "#usb-load"), false);
  assert.equal(isDashboardRoute("/console", ""), false);

  assert.equal(summarizeDriveStatus({ drives: [] }).state, "idle");
  const syncing = summarizeDriveStatus({
    settings: { wait_for_metadata: true },
    drives: [
      { sync_progress: { state: "syncing", total_files: 5, seen_files: 4, metadata_ready_files: 2 } },
    ],
  });
  assert.equal(syncing.state, "syncing");
  assert.equal(syncing.pillLabel, "Loading 2/5");
  assert.equal(syncing.badgeText, "2/5");

  const ready = summarizeDriveStatus({
    drives: [{ sync_progress: { state: "ready" } }],
    newest_file: { display_name: "benchy.gcode" },
  });
  assert.equal(ready.state, "ready");
  assert.equal(ready.pillLabel, "Ready");
});
