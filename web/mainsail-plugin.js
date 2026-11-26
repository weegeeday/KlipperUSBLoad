import {
  buildSettingsPayload,
  classifyPortStatus,
  formatBytes,
  formatSyncSummary,
  formatTimestamp,
  isDashboardRoute,
  resolveWebSocketUrl,
  shouldActivateMainsailPlugin,
  sortPorts,
  summarizeDriveStatus,
} from "./state.js";

const API_ROOT = "/server/klipper_usb_load";
const CSS_URL = `${API_ROOT}/ui/mainsail-plugin.css`;
const HASH_ROUTE = "#usb-load";

const USB_ICON_PATH =
  "M15 7V11H16V13H13V5H15L12 1L9 5H11V13H8V10.93C8.7 10.56 9.2 9.85 9.2 9C9.2 7.78 8.22 6.8 7 6.8S4.8 7.78 4.8 9C4.8 9.85 5.3 10.56 6 10.93V13C6 14.1 6.9 15 8 15H11V18.05C10.29 18.41 9.8 19.15 9.8 20C9.8 21.21 10.79 22.2 12 22.2S14.2 21.21 14.2 20C14.2 19.15 13.71 18.41 13 18.05V15H16C17.1 15 18 14.1 18 13V11H19V7H15Z";

if (typeof window !== "undefined" && shouldActivateMainsailPlugin(window.location.pathname)) {
  initMainsailPlugin();
}

function initMainsailPlugin() {
  ensureStylesheet();

  const state = {
    status: null,
    draft: null,
    dirty: false,
    pluginActive: window.location.hash === HASH_ROUTE,
    detectTimer: null,
    pollTimer: null,
    toastTimer: null,
    syncScheduled: false,
  };

  function escapeHtml(text) {
    return String(text ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function showToast(message, kind = "success") {
    let toast = document.getElementById("klipper-usb-load-toast");
    if (!toast) {
      toast = document.createElement("div");
      toast.id = "klipper-usb-load-toast";
      document.body.appendChild(toast);
    }
    toast.textContent = message;
    toast.className = `kusb-toast kusb-toast-${kind}`;
    if (state.toastTimer) {
      clearTimeout(state.toastTimer);
    }
    state.toastTimer = setTimeout(() => {
      toast.classList.add("kusb-hidden");
    }, 3200);
  }

  async function apiRequest(path, method = "GET", body = null) {
    const options = { method, headers: {} };
    if (body !== null) {
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(body);
    }
    const resp = await fetch(`${API_ROOT}${path}`, options);
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

  function setPluginActive(active, { updateHash = true } = {}) {
    state.pluginActive = Boolean(active);
    if (updateHash) {
      if (state.pluginActive && window.location.hash !== HASH_ROUTE) {
        window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}${HASH_ROUTE}`);
      } else if (!state.pluginActive && window.location.hash === HASH_ROUTE) {
        window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}`);
      }
    }
    syncDom();
  }

  function ensureSidebarItem() {
    const navList = document.querySelector("nav.v-navigation-drawer .v-list");
    if (!navList) {
      return;
    }

    if (!navList.dataset.kusbClickBound) {
      navList.dataset.kusbClickBound = "1";
      navList.addEventListener("click", (event) => {
        const clickedOwn = event.target?.closest?.("#klipper-usb-load-nav-item");
        if (!clickedOwn && state.pluginActive) {
          setPluginActive(false);
        }
      });
    }

    let item = document.getElementById("klipper-usb-load-nav-item");
    if (!item) {
      item = document.createElement("a");
      item.id = "klipper-usb-load-nav-item";
      item.href = HASH_ROUTE;
      item.className = "v-list-item v-list-item--link theme--dark";
      item.setAttribute("role", "listitem");
      item.innerHTML = `
        <div class="v-list-item__icon">
          <span class="v-icon notranslate theme--dark" aria-hidden="true" style="width:24px;height:24px;">
            <svg viewBox="0 0 24 24" width="24" height="24" fill="currentColor">
              <path d="${USB_ICON_PATH}"></path>
            </svg>
          </span>
        </div>
        <div class="v-list-item__content">
          <div class="v-list-item__title">USB Load</div>
        </div>
        <span id="klipper-usb-load-nav-badge" class="kusb-nav-badge kusb-hidden"></span>
      `;
      item.addEventListener("click", (event) => {
        event.preventDefault();
        setPluginActive(true);
      });

      const items = navList.querySelectorAll(":scope > .v-list-item, :scope > a.v-list-item");
      const lastItem = items.length > 0 ? items[items.length - 1] : null;
      if (lastItem && lastItem.parentElement === navList) {
        navList.insertBefore(item, lastItem);
      } else {
        navList.appendChild(item);
      }
    }

    if (state.pluginActive) {
      item.classList.add("v-list-item--active", "primary--text");
    } else {
      item.classList.remove("v-list-item--active", "primary--text");
    }

    const badge = document.getElementById("klipper-usb-load-nav-badge");
    if (badge && state.status) {
      const summary = summarizeDriveStatus(state.status);
      if (summary.badgeText) {
        badge.textContent = summary.badgeText;
        badge.className = `kusb-nav-badge kusb-badge-${summary.badgeTone}`;
      } else {
        badge.className = "kusb-nav-badge kusb-hidden";
      }
    }
  }

  function syncMainVisibility(mainWrap) {
    const pageEl = document.getElementById("klipper-usb-load-page");
    for (const child of Array.from(mainWrap.children)) {
      if (child.id === "klipper-usb-load-page") {
        continue;
      }
      if (state.pluginActive) {
        if (!child.dataset.kusbPrevDisplay) {
          child.dataset.kusbPrevDisplay = child.style.display || "";
        }
        child.style.display = "none";
      } else if ("kusbPrevDisplay" in child.dataset) {
        child.style.display = child.dataset.kusbPrevDisplay;
        delete child.dataset.kusbPrevDisplay;
      }
    }
    if (pageEl) {
      pageEl.classList.toggle("kusb-hidden", !state.pluginActive);
    }
  }

  function ensureDashboardPanel(mainWrap) {
    const existing = document.getElementById("klipper-usb-load-dashboard-panel");
    const onDash = !state.pluginActive && isDashboardRoute(window.location.pathname, window.location.hash);

    if (!onDash) {
      if (existing) {
        existing.remove();
      }
      return;
    }

    const targetCol =
      mainWrap.querySelector(".container > .row > div[class*='col']") ||
      mainWrap.querySelector(".row > div[class*='col']");
    if (!targetCol) {
      return;
    }

    let panel = existing;
    if (!panel || panel.parentElement !== targetCol) {
      if (panel) {
        panel.remove();
      }
      panel = document.createElement("div");
      panel.id = "klipper-usb-load-dashboard-panel";
      panel.className = "kusb-panel-card panel v-card v-sheet theme--dark";
      targetCol.prepend(panel);
    }
    renderDashboardPanel(panel);
  }

  function renderDashboardPanel(panel) {
    if (!state.status) {
      panel.innerHTML = `
        <div class="kusb-panel-toolbar">
          <div class="kusb-panel-title">USB G-Code</div>
          <span class="kusb-pill kusb-pill-muted">Loading...</span>
        </div>
        <div class="kusb-panel-body"><div class="kusb-muted">Connecting to KlipperUSBLoad...</div></div>
      `;
      return;
    }

    const status = state.status;
    const summary = summarizeDriveStatus(status);
    const newest = status.newest_file;
    const activePrint = status.active_print || { is_active: false };
    const drives = status.drives || [];
    const primaryDrive = drives[0] || null;
    const waitMeta = Boolean(status.settings?.wait_for_metadata);

    const warnings = [];
    if (status.warning) {
      warnings.push(status.warning);
    }
    if (status.klipper_macros_loaded === false) {
      warnings.push("Add [include klipper_usb_load.cfg] to printer.cfg and restart Klipper.");
    }

    let progressHtml = "";
    if (primaryDrive && primaryDrive.sync_progress?.state === "syncing") {
      const p = primaryDrive.sync_progress;
      const total = Number(p.total_files || 0);
      const done = waitMeta ? Number(p.metadata_ready_files || 0) : Number(p.seen_files || 0);
      const pct = total > 0 ? Math.min(100, Math.round((done / total) * 100)) : 0;
      progressHtml = `
        <div class="kusb-muted">${escapeHtml(formatSyncSummary(p, waitMeta))}</div>
        <div class="kusb-progress-track"><div class="kusb-progress-fill" style="width:${pct}%"></div></div>
      `;
    }

    let fileHtml = `<div class="kusb-muted">No USB G-code ready. Plug an allowed USB drive into the host.</div>`;
    if (newest) {
      const label = status.settings?.port_labels?.[newest.drive_port_id];
      const portText = label ? `${label} (${newest.drive_port_id})` : newest.drive_port_id;
      fileHtml = `
        <div class="kusb-dash-file">
          <div class="kusb-dash-filename">${escapeHtml(newest.display_name)}</div>
          <div class="kusb-muted">Port ${escapeHtml(portText)} • ${escapeHtml(formatBytes(newest.size))} • ${escapeHtml(formatTimestamp(newest.mtime))}</div>
        </div>
      `;
    }

    const canPrint = Boolean(newest) && !activePrint.is_active && summary.state !== "syncing";
    panel.innerHTML = `
      <div class="kusb-panel-toolbar">
        <div class="kusb-panel-title">
          <svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor" aria-hidden="true">
            <path d="${USB_ICON_PATH}"></path>
          </svg>
          <span>USB G-Code</span>
          <span class="kusb-pill kusb-pill-${summary.pillTone}">${escapeHtml(summary.pillLabel)}</span>
        </div>
        <div class="kusb-toolbar-actions">
          <button type="button" class="kusb-icon-btn" data-kusb-action="rescan" title="Rescan USB drives">Rescan</button>
          <button type="button" class="kusb-icon-btn" data-kusb-action="open-settings" title="USB Port &amp; Sync Settings">Settings</button>
        </div>
      </div>
      <div class="kusb-panel-body">
        ${warnings.length ? `<div class="kusb-alert">${escapeHtml(warnings.join(" • "))}</div>` : ""}
        ${progressHtml}
        <div class="kusb-dash-summary">
          ${fileHtml}
          <div class="kusb-toolbar-actions">
            ${
              primaryDrive && !primaryDrive.in_use_by_print
                ? `<button type="button" class="kusb-btn kusb-btn-danger" data-kusb-action="unmount" data-port-id="${escapeHtml(primaryDrive.port_id)}">Unmount</button>`
                : ""
            }
            <button type="button" class="kusb-btn kusb-btn-primary" data-kusb-action="print-newest" ${canPrint ? "" : "disabled"}>
              Print Newest
            </button>
          </div>
        </div>
      </div>
    `;

    panel.querySelector('[data-kusb-action="rescan"]')?.addEventListener("click", handleRescan);
    panel.querySelector('[data-kusb-action="open-settings"]')?.addEventListener("click", () => {
      setPluginActive(true);
    });
    panel.querySelector('[data-kusb-action="print-newest"]')?.addEventListener("click", handlePrintNewest);
    panel.querySelector('[data-kusb-action="unmount"]')?.addEventListener("click", (ev) => {
      const portId = ev.currentTarget?.getAttribute("data-port-id");
      if (portId) {
        handleUnmount(portId);
      }
    });
  }

  function ensureSettingsPage(mainWrap) {
    let page = document.getElementById("klipper-usb-load-page");
    if (!page) {
      page = document.createElement("div");
      page.id = "klipper-usb-load-page";
      page.className = "kusb-hidden";
      mainWrap.appendChild(page);
    }
    if (state.pluginActive) {
      renderSettingsPage(page);
    }
  }

  function renderSettingsPage(page) {
    if (!state.status || !state.draft) {
      page.innerHTML = `
        <div class="kusb-panel-card">
          <div class="kusb-panel-toolbar"><div class="kusb-panel-title">KlipperUSBLoad</div></div>
          <div class="kusb-panel-body"><div class="kusb-muted">Loading USB configuration...</div></div>
        </div>
      `;
      return;
    }

    const status = state.status;
    const draft = state.draft;
    const newest = status.newest_file;
    const activePrint = status.active_print || { is_active: false };
    const drives = status.drives || [];
    const anySyncing = drives.some((d) => d.sync_progress?.state === "syncing");
    const waitMeta = Boolean(status.settings?.wait_for_metadata);
    const ports = sortPorts(status.ports || []);
    const detect = status.detect_mode || { active: false };

    const warnings = [];
    if (status.warning) {
      warnings.push(status.warning);
    }
    if (status.klipper_macros_loaded === false) {
      warnings.push(
        "Klipper macros not loaded: add [include klipper_usb_load.cfg] to printer.cfg and restart Klipper to enable PRINT_NEWEST_USB and LCD menu integration.",
      );
    }

    const newestSection = newest
      ? `
        <div class="kusb-grid-stats">
          <div class="kusb-stat"><strong>File</strong><span>${escapeHtml(newest.display_name)}</span></div>
          <div class="kusb-stat"><strong>G-Code Path</strong><span><code>${escapeHtml(newest.rel_path)}</code></span></div>
          <div class="kusb-stat"><strong>Source Port</strong><span>${escapeHtml(
            status.settings?.port_labels?.[newest.drive_port_id]
              ? `${status.settings.port_labels[newest.drive_port_id]} (${newest.drive_port_id})`
              : newest.drive_port_id,
          )}</span></div>
          <div class="kusb-stat"><strong>Modified</strong><span>${escapeHtml(formatTimestamp(newest.mtime))}</span></div>
          <div class="kusb-stat"><strong>Size</strong><span>${escapeHtml(formatBytes(newest.size))}</span></div>
        </div>
      `
      : `<div class="kusb-muted">${
          anySyncing
            ? "Waiting for Moonraker to index all USB G-code files..."
            : "No USB G-code file ready. Plug in an allowed USB flash drive."
        }</div>`;

    const drivesHtml = drives
      .map((drive) => {
        const p = drive.sync_progress || {};
        const total = Number(p.total_files || 0);
        const done = waitMeta ? Number(p.metadata_ready_files || 0) : Number(p.seen_files || 0);
        const pct = total > 0 ? Math.min(100, Math.round((done / total) * 100)) : 100;
        return `
          <div class="kusb-drive-row">
            <div style="flex:1 1 260px;display:flex;flex-direction:column;gap:4px;">
              <div><strong>Port ${escapeHtml(drive.port_id)}</strong> <span class="kusb-muted">(${escapeHtml(drive.devnode)}${drive.fs_label ? ` • ${escapeHtml(drive.fs_label)}` : ""})</span></div>
              <div class="kusb-muted">${escapeHtml(formatSyncSummary(p, waitMeta))}</div>
              <div class="kusb-progress-track" style="max-width:340px;"><div class="kusb-progress-fill" style="width:${pct}%"></div></div>
            </div>
            <div>
              ${
                drive.in_use_by_print
                  ? `<span class="kusb-pill kusb-pill-warning">In Use By Active Print</span>`
                  : `<button type="button" class="kusb-btn kusb-btn-danger" data-kusb-unmount="${escapeHtml(drive.port_id)}">Safely Unmount</button>`
              }
            </div>
          </div>
        `;
      })
      .join("");

    let detectHtml = "";
    if (detect.detected_port_id) {
      detectHtml = `
        <div class="kusb-detect-banner">
          <div>
            <strong>Detected USB port: <code>${escapeHtml(detect.detected_port_id)}</code></strong>
            <div class="kusb-muted">Allow this slot to auto-mount USB drives plugged into it.</div>
          </div>
          <div class="kusb-toolbar-actions">
            <button type="button" id="kusb-allow-detected" class="kusb-btn kusb-btn-primary">Allow &amp; Save Port</button>
            <button type="button" id="kusb-stop-detect" class="kusb-btn kusb-btn-secondary">Dismiss</button>
          </div>
        </div>
      `;
    } else if (detect.active) {
      const rem = Math.max(0, Math.ceil((detect.expires_at * 1000 - Date.now()) / 1000));
      detectHtml = `
        <div class="kusb-detect-banner">
          <div>
            <strong>Waiting for USB flash drive... (<span id="kusb-detect-rem">${rem}s</span>)</strong>
            <div class="kusb-muted">Plug a USB drive into the physical port you want to identify.</div>
          </div>
          <button type="button" id="kusb-stop-detect" class="kusb-btn kusb-btn-secondary">Cancel</button>
        </div>
      `;
    }

    const isAllowlist = draft.port_policy === "allowlist";
    const rowsHtml = ports.length
      ? ports
          .map((port) => {
            const badge = classifyPortStatus(port);
            const checked = isAllowlist ? draft.allowed_ports.includes(port.port_id) : true;
            const desc = port.connected
              ? [port.vendor, port.product].filter(Boolean).join(" ") || "USB Device"
              : "No device attached";
            const extra = port.connected ? [port.devnode, port.fs_label].filter(Boolean).join(" • ") : "";
            const hl = detect.detected_port_id === port.port_id ? "kusb-row-highlight" : "";
            return `
              <tr class="${hl}">
                <td>
                  <input type="checkbox" data-kusb-allow-port="${escapeHtml(port.port_id)}" ${checked ? "checked" : ""} ${isAllowlist ? "" : "disabled"} />
                </td>
                <td><code>${escapeHtml(port.port_id)}</code></td>
                <td>
                  <input type="text" class="kusb-input" maxlength="64" placeholder="e.g. Front Panel USB" value="${escapeHtml(draft.port_labels[port.port_id] || "")}" data-kusb-label-port="${escapeHtml(port.port_id)}" />
                </td>
                <td><span class="kusb-pill kusb-pill-${badge.tone}">${escapeHtml(badge.label)}</span></td>
                <td>
                  <div>${escapeHtml(desc)}</div>
                  ${extra ? `<div class="kusb-muted">${escapeHtml(extra)}</div>` : ""}
                </td>
              </tr>
            `;
          })
          .join("")
      : `<tr><td colspan="5" class="kusb-muted">No physical USB ports reported by sysfs yet. Use "Detect Port" and plug in a drive.</td></tr>`;

    page.innerHTML = `
      ${warnings.length ? `<div class="kusb-alert">${escapeHtml(warnings.join(" • "))}</div>` : ""}

      <div class="kusb-panel-card">
        <div class="kusb-panel-toolbar">
          <div class="kusb-panel-title">USB Load — Newest Ready G-Code</div>
          <div class="kusb-toolbar-actions">
            <button type="button" id="kusb-btn-rescan" class="kusb-btn kusb-btn-secondary">Rescan Drives</button>
            <button type="button" id="kusb-btn-print" class="kusb-btn kusb-btn-primary" ${!newest || activePrint.is_active || anySyncing ? "disabled" : ""}>Print Newest USB</button>
          </div>
        </div>
        <div class="kusb-panel-body">
          ${newestSection}
          ${drivesHtml}
        </div>
      </div>

      <div class="kusb-panel-card">
        <div class="kusb-panel-toolbar">
          <div class="kusb-panel-title">USB Port Allowlist</div>
          <button type="button" id="kusb-btn-detect" class="kusb-btn kusb-btn-accent">Detect Port (Plug-In Helper)</button>
        </div>
        <div class="kusb-panel-body">
          ${detectHtml}
          <div class="kusb-policy-bar">
            <label class="kusb-policy-option">
              <input type="radio" name="kusb_policy" value="allow_all" ${!isAllowlist ? "checked" : ""} />
              <span><strong>Allow all USB ports</strong><small>Any USB flash drive plugged into any port mounts automatically.</small></span>
            </label>
            <label class="kusb-policy-option">
              <input type="radio" name="kusb_policy" value="allowlist" ${isAllowlist ? "checked" : ""} />
              <span><strong>Only allow checked ports</strong><small>Only physical USB ports checked below will auto-mount.</small></span>
            </label>
          </div>
          <div class="kusb-table-wrap">
            <table class="kusb-table">
              <thead>
                <tr><th>Allow</th><th>Physical Port</th><th>Custom Label</th><th>Status</th><th>Attached Device</th></tr>
              </thead>
              <tbody>${rowsHtml}</tbody>
            </table>
          </div>
        </div>
      </div>

      <div class="kusb-panel-card">
        <div class="kusb-panel-toolbar">
          <div class="kusb-panel-title">Indexing Barrier &amp; Scan Settings</div>
        </div>
        <form id="kusb-settings-form" class="kusb-panel-body kusb-settings-grid">
          <label class="kusb-toggle-field">
            <input type="checkbox" id="kusb-wait-meta" ${draft.wait_for_metadata ? "checked" : ""} />
            <span><strong>Wait for full metadata indexing</strong><small>Wait until every USB file is listed AND metadata parsing finishes before picking newest.</small></span>
          </label>
          <label class="kusb-toggle-field">
            <input type="checkbox" id="kusb-lcd-notify" ${draft.lcd_notifications ? "checked" : ""} />
            <span><strong>Show LCD notifications</strong><small>Post loading progress and ready/unplug alerts to Klipper's M117 line.</small></span>
          </label>
          <label class="kusb-input-field">
            <span>Indexing timeout (seconds)</span>
            <input type="number" class="kusb-input" id="kusb-timeout" min="2" max="600" step="1" value="${escapeHtml(draft.sync_timeout_seconds)}" />
            <small>Fallback timeout for slow thumbnail/metadata parsing.</small>
          </label>
          <label class="kusb-input-field">
            <span>Subdirectory scan depth</span>
            <input type="number" class="kusb-input" id="kusb-depth" min="0" max="10" step="1" value="${escapeHtml(draft.max_scan_depth)}" />
            <small>0 = USB root only; 2 = root + up to 2 nested folders.</small>
          </label>
          <label class="kusb-input-field kusb-full-span">
            <span>G-code file extensions</span>
            <input type="text" class="kusb-input" id="kusb-exts" value="${escapeHtml((draft.gcode_extensions || []).join(", "))}" />
            <small>Comma-separated extensions scanned on inserted USB drives.</small>
          </label>
          <div class="kusb-footer kusb-full-span">
            <span class="kusb-muted">${state.dirty ? "Unsaved changes." : "All changes saved."}</span>
            <div class="kusb-toolbar-actions">
              <button type="button" id="kusb-btn-reset" class="kusb-btn kusb-btn-secondary">Reset</button>
              <button type="submit" class="kusb-btn kusb-btn-primary">Save Settings</button>
            </div>
          </div>
        </form>
      </div>
    `;

    wireSettingsPageEvents(page);
  }

  function wireSettingsPageEvents(page) {
    page.querySelector("#kusb-btn-rescan")?.addEventListener("click", handleRescan);
    page.querySelector("#kusb-btn-print")?.addEventListener("click", handlePrintNewest);
    page.querySelector("#kusb-btn-detect")?.addEventListener("click", async () => {
      try {
        await apiRequest("/detect-port/start", "POST", { duration_seconds: 30 });
        await refreshStatus();
      } catch (err) {
        showToast(err.message, "error");
      }
    });
    page.querySelector("#kusb-stop-detect")?.addEventListener("click", async () => {
      await apiRequest("/detect-port/stop", "POST", {});
      await refreshStatus();
    });
    page.querySelector("#kusb-allow-detected")?.addEventListener("click", async () => {
      const pid = state.status?.detect_mode?.detected_port_id;
      if (pid && state.draft) {
        if (!state.draft.allowed_ports.includes(pid)) {
          state.draft.allowed_ports.push(pid);
        }
        state.draft.port_policy = "allowlist";
        await saveDraftSettings();
        await apiRequest("/detect-port/stop", "POST", {});
        await refreshStatus();
      }
    });

    for (const btn of page.querySelectorAll("[data-kusb-unmount]")) {
      btn.addEventListener("click", () => handleUnmount(btn.getAttribute("data-kusb-unmount")));
    }

    for (const radio of page.querySelectorAll('input[name="kusb_policy"]')) {
      radio.addEventListener("change", () => {
        if (state.draft && radio.checked) {
          state.draft.port_policy = radio.value;
          state.dirty = true;
          renderSettingsPage(page);
        }
      });
    }

    for (const cb of page.querySelectorAll("[data-kusb-allow-port]")) {
      cb.addEventListener("change", () => {
        const pid = cb.getAttribute("data-kusb-allow-port");
        if (!pid || !state.draft) {
          return;
        }
        if (cb.checked && !state.draft.allowed_ports.includes(pid)) {
          state.draft.allowed_ports.push(pid);
        } else if (!cb.checked) {
          state.draft.allowed_ports = state.draft.allowed_ports.filter((p) => p !== pid);
        }
        state.dirty = true;
      });
    }

    for (const inp of page.querySelectorAll("[data-kusb-label-port]")) {
      inp.addEventListener("input", () => {
        const pid = inp.getAttribute("data-kusb-label-port");
        if (!pid || !state.draft) {
          return;
        }
        const trimmed = inp.value.trim();
        if (trimmed) {
          state.draft.port_labels[pid] = trimmed;
        } else {
          delete state.draft.port_labels[pid];
        }
        state.dirty = true;
      });
    }

    page.querySelector("#kusb-btn-reset")?.addEventListener("click", () => {
      if (state.status) {
        state.draft = cloneSettingsToDraft(state.status.settings);
        state.dirty = false;
        renderSettingsPage(page);
      }
    });

    page.querySelector("#kusb-settings-form")?.addEventListener("submit", (ev) => {
      ev.preventDefault();
      saveDraftSettings();
    });
  }

  async function saveDraftSettings() {
    if (!state.draft) {
      return;
    }
    const page = document.getElementById("klipper-usb-load-page");
    if (page) {
      const waitMeta = page.querySelector("#kusb-wait-meta");
      const lcdNotify = page.querySelector("#kusb-lcd-notify");
      const timeout = page.querySelector("#kusb-timeout");
      const depth = page.querySelector("#kusb-depth");
      const exts = page.querySelector("#kusb-exts");
      if (waitMeta) state.draft.wait_for_metadata = waitMeta.checked;
      if (lcdNotify) state.draft.lcd_notifications = lcdNotify.checked;
      if (timeout) state.draft.sync_timeout_seconds = Number(timeout.value);
      if (depth) state.draft.max_scan_depth = Number(depth.value);
      if (exts) state.draft.gcode_extensions = exts.value;
    }

    try {
      const payload = buildSettingsPayload(state.draft);
      const updated = await apiRequest("/settings", "POST", payload);
      state.status = updated;
      state.draft = cloneSettingsToDraft(updated.settings);
      state.dirty = false;
      syncDom();
      showToast("Settings saved", "success");
    } catch (err) {
      showToast(err.message, "error");
    }
  }

  async function handleRescan() {
    try {
      state.status = await apiRequest("/rescan", "POST", {});
      syncDom();
      showToast("USB rescan complete", "success");
    } catch (err) {
      showToast(err.message, "error");
    }
  }

  async function handlePrintNewest() {
    try {
      const res = await apiRequest("/print-newest", "POST", {});
      showToast(`Started print: ${res.file?.display_name || "USB file"}`, "success");
      await refreshStatus();
    } catch (err) {
      showToast(err.message, "error");
    }
  }

  async function handleUnmount(portId) {
    try {
      state.status = await apiRequest("/unmount", "POST", { port_id: portId });
      syncDom();
      showToast(`Unmounted port ${portId}`, "success");
    } catch (err) {
      showToast(err.message, "error");
    }
  }

  async function refreshStatus() {
    try {
      const status = await apiRequest("/status");
      state.status = status;
      if (!state.dirty || !state.draft) {
        state.draft = cloneSettingsToDraft(status.settings);
        state.dirty = false;
      }
      syncDom();
    } catch {
      // Ignore transient refresh errors during reconnect
    }
  }

  function syncDom() {
    ensureSidebarItem();
    const mainWrap = document.querySelector("main.v-main .v-main__wrap");
    if (!mainWrap) {
      return;
    }
    ensureSettingsPage(mainWrap);
    syncMainVisibility(mainWrap);
    ensureDashboardPanel(mainWrap);

    if (state.status?.detect_mode?.active && !state.detectTimer) {
      state.detectTimer = setInterval(() => {
        const remEl = document.getElementById("kusb-detect-rem");
        const exp = state.status?.detect_mode?.expires_at || 0;
        const rem = Math.max(0, Math.ceil((exp * 1000 - Date.now()) / 1000));
        if (remEl) {
          remEl.textContent = `${rem}s`;
        }
        if (rem <= 0) {
          clearInterval(state.detectTimer);
          state.detectTimer = null;
          refreshStatus();
        }
      }, 500);
    } else if (!state.status?.detect_mode?.active && state.detectTimer) {
      clearInterval(state.detectTimer);
      state.detectTimer = null;
    }
  }

  function scheduleSyncDom() {
    if (state.syncScheduled) {
      return;
    }
    state.syncScheduled = true;
    requestAnimationFrame(() => {
      state.syncScheduled = false;
      syncDom();
    });
  }

  function connectWebSocket() {
    const wsUrl = resolveWebSocketUrl(window.location);
    if (!wsUrl) {
      return;
    }
    try {
      const ws = new WebSocket(wsUrl);
      ws.addEventListener("open", () => {
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
        if (!state.pollTimer) {
          state.pollTimer = setInterval(refreshStatus, 4000);
        }
        setTimeout(connectWebSocket, 5000);
      });
    } catch {
      if (!state.pollTimer) {
        state.pollTimer = setInterval(refreshStatus, 4000);
      }
    }
  }

  window.addEventListener("hashchange", () => {
    setPluginActive(window.location.hash === HASH_ROUTE, { updateHash: false });
  });
  window.addEventListener("popstate", () => {
    setPluginActive(window.location.hash === HASH_ROUTE, { updateHash: false });
  });

  const observer = new MutationObserver(() => {
    scheduleSyncDom();
  });
  observer.observe(document.body, { childList: true, subtree: true });

  refreshStatus();
  connectWebSocket();
  scheduleSyncDom();
}

function ensureStylesheet() {
  if (document.getElementById("klipper-usb-load-plugin-css")) {
    return;
  }
  const link = document.createElement("link");
  link.id = "klipper-usb-load-plugin-css";
  link.rel = "stylesheet";
  link.href = CSS_URL;
  document.head.appendChild(link);
}
