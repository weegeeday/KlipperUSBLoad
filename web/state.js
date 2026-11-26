/**
 * Pure state, formatting, and validation helpers for the KlipperUSBLoad Settings UI.
 */

export function resolveApiBase(origin = "", search = "") {
  if (search) {
    try {
      const params = new URLSearchParams(search);
      const override = params.get("moonraker");
      if (override) {
        return override.replace(/\/+$/, "");
      }
    } catch {
      // Ignore malformed query strings
    }
  }
  return origin ? origin.replace(/\/+$/, "") : "";
}

export function resolveWebSocketUrl(locationObj) {
  if (!locationObj || !locationObj.host) {
    return "";
  }
  const proto = locationObj.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${locationObj.host}/websocket`;
}

export function parsePortSortKey(portId) {
  const match = /^(\d+)-(\d+(?:\.\d+)*)$/.exec(String(portId || ""));
  if (!match) {
    return { bus: 9999, chain: [], raw: String(portId || "") };
  }
  return {
    bus: Number(match[1]),
    chain: match[2].split(".").map((n) => Number(n)),
    raw: portId,
  };
}

export function sortPorts(ports) {
  return [...(ports || [])].sort((a, b) => {
    const ka = parsePortSortKey(a.port_id);
    const kb = parsePortSortKey(b.port_id);
    if (ka.bus !== kb.bus) {
      return ka.bus - kb.bus;
    }
    const len = Math.max(ka.chain.length, kb.chain.length);
    for (let i = 0; i < len; i += 1) {
      const av = ka.chain[i] ?? -1;
      const bv = kb.chain[i] ?? -1;
      if (av !== bv) {
        return av - bv;
      }
    }
    return ka.raw.localeCompare(kb.raw);
  });
}

export function classifyPortStatus(port) {
  if (!port || !port.connected) {
    return { key: "empty", label: "Empty", tone: "muted" };
  }
  if (port.mounted) {
    return { key: "mounted", label: "Mounted USB", tone: "success" };
  }
  if (port.device_class === "non_storage") {
    return { key: "non_storage", label: "Non-Storage (MCU/Other)", tone: "info" };
  }
  if (port.device_class === "storage" && !port.allowed) {
    return { key: "blocked", label: "Connected (Blocked)", tone: "warning" };
  }
  if (port.device_class === "storage") {
    return { key: "connected", label: "USB Storage Detected", tone: "accent" };
  }
  return { key: "empty", label: "Empty", tone: "muted" };
}

export function formatSyncSummary(progress, waitForMetadata = true) {
  if (!progress) {
    return "Idle";
  }
  const total = Number(progress.total_files || 0);
  const seen = Number(progress.seen_files || 0);
  const meta = Number(progress.metadata_ready_files || 0);

  if (progress.state === "scanning") {
    return progress.message || "Scanning USB drive...";
  }
  if (progress.state === "syncing") {
    if (waitForMetadata) {
      return `Indexing ${seen}/${total} listed, ${meta}/${total} metadata ready...`;
    }
    return `Loading ${seen}/${total} files into Moonraker...`;
  }
  if (progress.state === "ready") {
    if (total === 0) {
      return "Mounted (0 G-code files found)";
    }
    return `Ready (${total}/${total} files indexed)`;
  }
  if (progress.state === "warning") {
    const missing = Array.isArray(progress.missing_files) ? progress.missing_files.length : 0;
    return `Partial load (${total - missing}/${total} ready, ${missing} timed out)`;
  }
  if (progress.state === "error") {
    return progress.warning || progress.message || "Sync error";
  }
  return progress.message || "Idle";
}

export function formatTimestamp(epochSeconds) {
  if (!epochSeconds || Number.isNaN(Number(epochSeconds))) {
    return "—";
  }
  const date = new Date(Number(epochSeconds) * 1000);
  return date.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

export function formatBytes(bytes) {
  const num = Number(bytes || 0);
  if (num < 1024) {
    return `${num} B`;
  }
  if (num < 1024 * 1024) {
    return `${(num / 1024).toFixed(1)} KB`;
  }
  return `${(num / (1024 * 1024)).toFixed(2)} MB`;
}

export function parseExtensionsInput(raw) {
  const tokens = String(raw || "")
    .split(/[,\s]+/)
    .map((item) => item.trim().toLowerCase())
    .filter(Boolean)
    .map((item) => (item.startsWith(".") ? item : `.${item}`));

  const unique = [];
  for (const ext of tokens) {
    if (!/^\.[a-z0-9]{1,16}$/.test(ext)) {
      throw new Error(`Invalid extension "${ext}". Use alphanumeric extensions like .gcode`);
    }
    if (!unique.includes(ext)) {
      unique.push(ext);
    }
  }
  if (unique.length === 0) {
    throw new Error("At least one G-code file extension is required.");
  }
  return unique;
}

export function buildSettingsPayload(draft) {
  const depth = Number(draft.max_scan_depth);
  if (!Number.isInteger(depth) || depth < 0 || depth > 10) {
    throw new Error("Subdirectory scan depth must be an integer between 0 and 10.");
  }
  const timeout = Number(draft.sync_timeout_seconds);
  if (Number.isNaN(timeout) || timeout < 2 || timeout > 600) {
    throw new Error("Indexing timeout must be between 2 and 600 seconds.");
  }
  const extensions = Array.isArray(draft.gcode_extensions)
    ? parseExtensionsInput(draft.gcode_extensions.join(","))
    : parseExtensionsInput(draft.gcode_extensions);

  const policy = draft.port_policy === "allowlist" ? "allowlist" : "allow_all";
  const allowedPorts = Array.isArray(draft.allowed_ports)
    ? [...new Set(draft.allowed_ports.map((p) => String(p).trim()).filter(Boolean))]
    : [];

  const portLabels = {};
  if (draft.port_labels && typeof draft.port_labels === "object") {
    for (const [k, v] of Object.entries(draft.port_labels)) {
      const label = String(v || "").trim();
      if (k && label) {
        portLabels[k] = label.slice(0, 64);
      }
    }
  }

  return {
    port_policy: policy,
    allowed_ports: allowedPorts,
    port_labels: portLabels,
    max_scan_depth: depth,
    wait_for_metadata: Boolean(draft.wait_for_metadata),
    sync_timeout_seconds: timeout,
    gcode_extensions: extensions,
    lcd_notifications: Boolean(draft.lcd_notifications),
  };
}

export function shouldActivateMainsailPlugin(pathname = "") {
  const normalized = String(pathname || "");
  if (normalized.startsWith("/server/klipper_usb_load/ui")) {
    return false;
  }
  return true;
}

export function isDashboardRoute(pathname = "", hash = "") {
  if (String(hash || "") === "#usb-load") {
    return false;
  }
  const cleanPath = String(pathname || "").replace(/\/+$/, "") || "/";
  return cleanPath === "/" || cleanPath === "/index.html";
}

export function summarizeDriveStatus(status) {
  const drives = Array.isArray(status?.drives) ? status.drives : [];
  if (drives.length === 0) {
    return {
      state: "idle",
      pillLabel: "No USB",
      pillTone: "muted",
      badgeText: "",
      badgeTone: "ready",
    };
  }

  const syncingDrive = drives.find((d) => d.sync_progress?.state === "syncing");
  if (syncingDrive) {
    const p = syncingDrive.sync_progress || {};
    const total = Number(p.total_files || 0);
    const waitMeta = Boolean(status?.settings?.wait_for_metadata);
    const done = waitMeta ? Number(p.metadata_ready_files || 0) : Number(p.seen_files || 0);
    return {
      state: "syncing",
      pillLabel: `Loading ${done}/${total}`,
      pillTone: "info",
      badgeText: `${done}/${total}`,
      badgeTone: "syncing",
    };
  }

  const warningDrive = drives.find((d) => d.sync_progress?.state === "warning");
  if (warningDrive) {
    return {
      state: "warning",
      pillLabel: "Partial Load",
      pillTone: "warning",
      badgeText: "!",
      badgeTone: "warn",
    };
  }

  if (status?.newest_file) {
    return {
      state: "ready",
      pillLabel: "Ready",
      pillTone: "success",
      badgeText: String(drives.length),
      badgeTone: "ready",
    };
  }

  return {
    state: "mounted",
    pillLabel: "Mounted (0 files)",
    pillTone: "accent",
    badgeText: String(drives.length),
    badgeTone: "ready",
  };
}
