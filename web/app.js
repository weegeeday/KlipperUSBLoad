import {
  buildSettingsPayload,
  classifyPortStatus,
  formatBytes,
  formatSyncSummary,
  formatTimestamp,
  resolveApiBase,
  resolveWebSocketUrl,
  sortPorts,
} from "./state.js";

const apiBase = resolveApiBase("", window.location.search);

const state = {
  status: null,
  draft: null,
  dirty: false,
  wsConnected: false,
  pollTimer: null,
  detectTimer: null,
};

const els = {
  wsStatus: document.getElementById("ws-status"),
  globalWarning: document.getElementById("global-warning"),
  newestBody: document.getElementById("newest-body"),
  drivesList: document.getElementById("drives-list"),
  portsTbody: document.getElementById("ports-tbody"),
  detectBanner: document.getElementById("detect-banner"),
  policyAllowAll: document.getElementById("policy-allow-all"),
  policyAllowlist: document.getElementById("policy-allowlist"),
  optWaitMetadata: document.getElementById("opt-wait-metadata"),
  optLcdNotify: document.getElementById("opt-lcd-notify"),
  optTimeout: document.getElementById("opt-timeout"),
  optDepth: document.getElementById("opt-depth"),
  optExtensions: document.getElementById("opt-extensions"),
  dirtyIndicator: document.getElementById("dirty-indicator"),
  btnPrintNewest: document.getElementById("btn-print-newest"),
  btnRescan: document.getElementById("btn-rescan"),
  btnDetectPort: document.getElementById("btn-detect-port"),
  btnResetDraft: document.getElementById("btn-reset-draft"),
  settingsForm: document.getElementById("settings-form"),
  toast: document.getElementById("toast"),
};

let toastTimer = null;

function showToast(message, kind = "success") {
  els.toast.textContent = message;
  els.toast.className = `toast toast-${kind}`;
  if (toastTimer) {
    clearTimeout(toastTimer);
  }
  toastTimer = setTimeout(() => {
    els.toast.classList.add("hidden");
  }, 3200);
}

async function apiRequest(path, method = "GET", body = null) {
  const options = { method, headers: {} };
  if (body !== null) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const resp = await fetch(`${apiBase}${path}`, options);
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    const errMsg = data?.error?.message || data?.message || `HTTP ${resp.status}`;
    throw new Error(errMsg);
  }
  return data.result !== undefined ? data.result : data;
}

function cloneSettingsToDraft(settings) {
  return {
    port_policy: settings.port_policy,
    allowed_ports: [...(settings.allowed_ports || [])],
    port_labels: { ...(settings.port_labels || {}) },
    max_scan_depth: settings.max_scan_depth,
    wait_for_metadata: Boolean(settings.wait_for_metadata),
    sync_timeout_seconds: settings.sync_timeout_seconds,
    gcode_extensions: [...(settings.gcode_extensions || [".gcode", ".gco", ".g"])],
    lcd_notifications: Boolean(settings.lcd_notifications),
  };
}

function markDirty(isDirty = true) {
  state.dirty = isDirty;
  els.dirtyIndicator.textContent = isDirty
    ? "Unsaved changes."
    : "All changes saved.";
}

function syncFormInputsFromDraft() {
  if (!state.draft) {
    return;
  }
  els.policyAllowAll.checked = state.draft.port_policy === "allow_all";
  els.policyAllowlist.checked = state.draft.port_policy === "allowlist";
  els.optWaitMetadata.checked = Boolean(state.draft.wait_for_metadata);
  els.optLcdNotify.checked = Boolean(state.draft.lcd_notifications);
  els.optTimeout.value = String(state.draft.sync_timeout_seconds);
  els.optDepth.value = String(state.draft.max_scan_depth);
  els.optExtensions.value = (state.draft.gcode_extensions || []).join(", ");
}

function renderNewestAndDrives(status) {
  const warning = status.warning || "";
  if (warning) {
    els.globalWarning.textContent = warning;
    els.globalWarning.classList.remove("hidden");
  } else {
    els.globalWarning.classList.add("hidden");
  }

  const newest = status.newest_file;
  const activePrint = status.active_print || { is_active: false, state: "standby" };
  const drives = status.drives || [];
  const anySyncing = drives.some((d) => d.sync_progress?.state === "syncing");

  els.btnPrintNewest.disabled = !newest || activePrint.is_active || anySyncing;

  if (!newest) {
    els.newestBody.innerHTML = anySyncing
      ? `<div class="newest-empty">Waiting for Moonraker to index all USB G-code files...</div>`
      : `<div class="newest-empty">No USB G-code file ready. Plug in an allowed USB flash drive.</div>`;
  } else {
    const label = state.status?.settings?.port_labels?.[newest.drive_port_id];
    const portDisplay = label ? `${label} (${newest.drive_port_id})` : newest.drive_port_id;
    els.newestBody.innerHTML = `
      <div class="newest-stat"><strong>File</strong><span>${escapeHtml(newest.display_name)}</span></div>
      <div class="newest-stat"><strong>G-Code Path</strong><span><code>${escapeHtml(newest.rel_path)}</code></span></div>
      <div class="newest-stat"><strong>Source Port</strong><span>${escapeHtml(portDisplay)}</span></div>
      <div class="newest-stat"><strong>Modified</strong><span>${escapeHtml(formatTimestamp(newest.mtime))}</span></div>
      <div class="newest-stat"><strong>Size</strong><span>${escapeHtml(formatBytes(newest.size))}</span></div>
    `;
  }

  if (drives.length === 0) {
    els.drivesList.innerHTML = "";
    return;
  }

  const waitMeta = Boolean(status.settings?.wait_for_metadata);
  els.drivesList.innerHTML = "";
  for (const drive of drives) {
    const progress = drive.sync_progress || {};
    const total = Number(progress.total_files || 0);
    const completed = waitMeta
      ? Number(progress.metadata_ready_files || 0)
      : Number(progress.seen_files || 0);
    const pct = total > 0 ? Math.min(100, Math.round((completed / total) * 100)) : 100;

    const row = document.createElement("div");
    row.className = "drive-row";

    const meta = document.createElement("div");
    meta.className = "drive-meta";
    meta.innerHTML = `
      <div>
        <strong>Port ${escapeHtml(drive.port_id)}</strong>
        <span class="muted">(${escapeHtml(drive.devnode)}${drive.fs_label ? ` • ${escapeHtml(drive.fs_label)}` : ""})</span>
      </div>
      <div class="muted">${escapeHtml(formatSyncSummary(progress, waitMeta))}</div>
      <div class="progress-track"><div class="progress-fill" style="width:${pct}%"></div></div>
    `;

    const actions = document.createElement("div");
    if (drive.in_use_by_print) {
      const lockBadge = document.createElement("span");
      lockBadge.className = "pill pill-warning";
      lockBadge.textContent = "In Use By Active Print";
      actions.appendChild(lockBadge);
    } else {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "btn btn-danger-outline";
      btn.textContent = "Safely Unmount";
      btn.addEventListener("click", () => handleUnmount(drive.port_id));
      actions.appendChild(btn);
    }

    row.appendChild(meta);
    row.appendChild(actions);
    els.drivesList.appendChild(row);
  }
}

function renderDetectBanner(status) {
  const detect = status.detect_mode || { active: false };
  if (state.detectTimer) {
    clearInterval(state.detectTimer);
    state.detectTimer = null;
  }

  if (!detect.active && !detect.detected_port_id) {
    els.detectBanner.classList.add("hidden");
    return;
  }

  els.detectBanner.classList.remove("hidden");
  if (detect.detected_port_id) {
    const portId = detect.detected_port_id;
    els.detectBanner.innerHTML = `
      <div>
        <strong>Detected USB port: <code>${escapeHtml(portId)}</code></strong>
        <div class="muted">Click "Allow &amp; Save Port" to add this slot to your allowlist.</div>
      </div>
      <div class="topbar-actions">
        <button id="btn-allow-detected" class="btn btn-primary" type="button">Allow &amp; Save Port</button>
        <button id="btn-dismiss-detect" class="btn btn-secondary" type="button">Dismiss</button>
      </div>
    `;
    document.getElementById("btn-allow-detected")?.addEventListener("click", async () => {
      if (!state.draft) {
        return;
      }
      if (!state.draft.allowed_ports.includes(portId)) {
        state.draft.allowed_ports.push(portId);
      }
      state.draft.port_policy = "allowlist";
      await saveDraftSettings();
      await apiRequest("/server/klipper_usb_load/detect-port/stop", "POST", {});
    });
    document.getElementById("btn-dismiss-detect")?.addEventListener("click", async () => {
      await apiRequest("/server/klipper_usb_load/detect-port/stop", "POST", {});
      await refreshStatus();
    });
    return;
  }

  const updateCountdown = () => {
    const remaining = Math.max(0, Math.ceil((detect.expires_at * 1000 - Date.now()) / 1000));
    const countEl = document.getElementById("detect-countdown");
    if (countEl) {
      countEl.textContent = `${remaining}s`;
    }
    if (remaining <= 0 && state.detectTimer) {
      clearInterval(state.detectTimer);
      state.detectTimer = null;
      refreshStatus();
    }
  };

  els.detectBanner.innerHTML = `
    <div>
      <strong>Waiting for USB flash drive... (<span id="detect-countdown">30s</span>)</strong>
      <div class="muted">Plug a USB drive into the physical port you want to identify.</div>
    </div>
    <button id="btn-cancel-detect" class="btn btn-secondary" type="button">Cancel</button>
  `;
  document.getElementById("btn-cancel-detect")?.addEventListener("click", async () => {
    await apiRequest("/server/klipper_usb_load/detect-port/stop", "POST", {});
    await refreshStatus();
  });
  updateCountdown();
  state.detectTimer = setInterval(updateCountdown, 500);
}

function renderPortsTable(status) {
  const ports = sortPorts(status.ports || []);
  const detectedPort = status.detect_mode?.detected_port_id;
  const isAllowlist = state.draft?.port_policy === "allowlist";

  if (ports.length === 0) {
    els.portsTbody.innerHTML = `
      <tr>
        <td colspan="5" class="muted center-cell">
          No physical USB ports reported by sysfs yet. Use "Detect Port" and plug in a USB drive.
        </td>
      </tr>
    `;
    return;
  }

  els.portsTbody.innerHTML = "";
  for (const port of ports) {
    const tr = document.createElement("tr");
    if (detectedPort && detectedPort === port.port_id) {
      tr.classList.add("row-highlighted");
    }

    const allowCell = document.createElement("td");
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.ariaLabel = `Allow USB port ${port.port_id}`;
    checkbox.checked = isAllowlist
      ? (state.draft?.allowed_ports || []).includes(port.port_id)
      : true;
    checkbox.disabled = !isAllowlist;
    checkbox.addEventListener("change", () => {
      if (!state.draft) {
        return;
      }
      const list = state.draft.allowed_ports;
      if (checkbox.checked && !list.includes(port.port_id)) {
        list.push(port.port_id);
      } else if (!checkbox.checked) {
        state.draft.allowed_ports = list.filter((p) => p !== port.port_id);
      }
      markDirty(true);
    });
    allowCell.appendChild(checkbox);

    const idCell = document.createElement("td");
    idCell.innerHTML = `<code>${escapeHtml(port.port_id)}</code>`;

    const labelCell = document.createElement("td");
    const labelInput = document.createElement("input");
    labelInput.type = "text";
    labelInput.className = "label-input";
    labelInput.placeholder = "e.g. Front Panel USB";
    labelInput.maxLength = 64;
    labelInput.value = state.draft?.port_labels?.[port.port_id] || "";
    labelInput.addEventListener("input", () => {
      if (!state.draft) {
        return;
      }
      const trimmed = labelInput.value.trim();
      if (trimmed) {
        state.draft.port_labels[port.port_id] = trimmed;
      } else {
        delete state.draft.port_labels[port.port_id];
      }
      markDirty(true);
    });
    labelCell.appendChild(labelInput);

    const statusCell = document.createElement("td");
    const badge = classifyPortStatus(port);
    statusCell.innerHTML = `<span class="pill pill-${badge.tone}">${escapeHtml(badge.label)}</span>`;

    const deviceCell = document.createElement("td");
    if (!port.connected) {
      deviceCell.innerHTML = `<span class="muted">No device attached</span>`;
    } else {
      const desc = [port.vendor, port.product].filter(Boolean).join(" ") || "USB Device";
      const extra = [port.devnode, port.fs_label].filter(Boolean).join(" • ");
      deviceCell.innerHTML = `
        <div>${escapeHtml(desc)}</div>
        ${extra ? `<div class="muted">${escapeHtml(extra)}</div>` : ""}
      `;
    }

    tr.appendChild(allowCell);
    tr.appendChild(idCell);
    tr.appendChild(labelCell);
    tr.appendChild(statusCell);
    tr.appendChild(deviceCell);
    els.portsTbody.appendChild(tr);
  }
}

function renderAll() {
  if (!state.status) {
    return;
  }
  renderNewestAndDrives(state.status);
  renderDetectBanner(state.status);
  renderPortsTable(state.status);
}

async function refreshStatus() {
  try {
    const status = await apiRequest("/server/klipper_usb_load/status");
    state.status = status;
    if (!state.dirty || !state.draft) {
      state.draft = cloneSettingsToDraft(status.settings);
      syncFormInputsFromDraft();
      markDirty(false);
    }
    renderAll();
  } catch (err) {
    showToast(`Failed to load status: ${err.message}`, "error");
  }
}

async function saveDraftSettings() {
  if (!state.draft) {
    return;
  }
  try {
    state.draft.max_scan_depth = Number(els.optDepth.value);
    state.draft.sync_timeout_seconds = Number(els.optTimeout.value);
    state.draft.wait_for_metadata = els.optWaitMetadata.checked;
    state.draft.lcd_notifications = els.optLcdNotify.checked;
    state.draft.gcode_extensions = els.optExtensions.value;

    const payload = buildSettingsPayload(state.draft);
    const updated = await apiRequest("/server/klipper_usb_load/settings", "POST", payload);
    state.status = updated;
    state.draft = cloneSettingsToDraft(updated.settings);
    syncFormInputsFromDraft();
    markDirty(false);
    renderAll();
    showToast("Settings saved", "success");
  } catch (err) {
    showToast(err.message, "error");
  }
}

async function handleUnmount(portId) {
  try {
    const updated = await apiRequest("/server/klipper_usb_load/unmount", "POST", { port_id: portId });
    state.status = updated;
    renderAll();
    showToast(`Unmounted port ${portId}`, "success");
  } catch (err) {
    showToast(err.message, "error");
  }
}

function connectWebSocket() {
  const wsUrl = resolveWebSocketUrl(window.location);
  if (!wsUrl) {
    return;
  }

  try {
    const ws = new WebSocket(wsUrl);
    ws.addEventListener("open", () => {
      state.wsConnected = true;
      els.wsStatus.textContent = "Live Connected";
      els.wsStatus.className = "pill pill-success";
      if (state.pollTimer) {
        clearInterval(state.pollTimer);
        state.pollTimer = null;
      }
    });
    ws.addEventListener("message", (event) => {
      try {
        const msg = JSON.parse(event.data);
        if (
          msg.method === "klipper_usb_load:status_changed" ||
          msg.method === "notify_klipper_usb_load:status_changed" ||
          msg.method === "notify_filelist_changed"
        ) {
          refreshStatus();
        }
      } catch {
        // Ignore non-JSON frames
      }
    });
    ws.addEventListener("close", () => {
      state.wsConnected = false;
      els.wsStatus.textContent = "Polling Mode";
      els.wsStatus.className = "pill pill-muted";
      if (!state.pollTimer) {
        state.pollTimer = setInterval(refreshStatus, 4000);
      }
      setTimeout(connectWebSocket, 5000);
    });
  } catch {
    els.wsStatus.textContent = "Polling Mode";
    els.wsStatus.className = "pill pill-muted";
    if (!state.pollTimer) {
      state.pollTimer = setInterval(refreshStatus, 4000);
    }
  }
}

function escapeHtml(text) {
  return String(text ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function wireEvents() {
  els.policyAllowAll.addEventListener("change", () => {
    if (state.draft && els.policyAllowAll.checked) {
      state.draft.port_policy = "allow_all";
      markDirty(true);
      renderPortsTable(state.status);
    }
  });

  els.policyAllowlist.addEventListener("change", () => {
    if (state.draft && els.policyAllowlist.checked) {
      state.draft.port_policy = "allowlist";
      markDirty(true);
      renderPortsTable(state.status);
    }
  });

  for (const input of [
    els.optWaitMetadata,
    els.optLcdNotify,
    els.optTimeout,
    els.optDepth,
    els.optExtensions,
  ]) {
    input.addEventListener("input", () => markDirty(true));
    input.addEventListener("change", () => markDirty(true));
  }

  els.settingsForm.addEventListener("submit", (event) => {
    event.preventDefault();
    saveDraftSettings();
  });

  els.btnResetDraft.addEventListener("click", () => {
    if (!state.status) {
      return;
    }
    state.draft = cloneSettingsToDraft(state.status.settings);
    syncFormInputsFromDraft();
    markDirty(false);
    renderPortsTable(state.status);
  });

  els.btnRescan.addEventListener("click", async () => {
    try {
      const updated = await apiRequest("/server/klipper_usb_load/rescan", "POST", {});
      state.status = updated;
      renderAll();
      showToast("Rescan complete", "success");
    } catch (err) {
      showToast(err.message, "error");
    }
  });

  els.btnDetectPort.addEventListener("click", async () => {
    try {
      await apiRequest("/server/klipper_usb_load/detect-port/start", "POST", {
        duration_seconds: 30,
      });
      await refreshStatus();
    } catch (err) {
      showToast(err.message, "error");
    }
  });

  els.btnPrintNewest.addEventListener("click", async () => {
    try {
      const res = await apiRequest("/server/klipper_usb_load/print-newest", "POST", {});
      showToast(`Started print: ${res.file?.display_name || "USB file"}`, "success");
      await refreshStatus();
    } catch (err) {
      showToast(err.message, "error");
    }
  });
}

wireEvents();
refreshStatus();
connectWebSocket();
