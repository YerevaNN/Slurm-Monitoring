(function () {
  "use strict";

  const GPUS_PER_NODE = 8;
  const CPUS_PER_NODE = 224;
  const BASE_ROW_HEIGHT = 72;
  /** Row labels: discovered nodes + any from job allocations + Pending. */
  function getRowLabels() {
    const fromJobs = new Set();
    (state.jobs || []).forEach((j) => {
      (j.allocations || []).forEach((a) => { if (a.node) fromJobs.add(a.node); });
    });
    const nodes = state.nodes || [];
    const all = [...new Set([...nodes, ...fromJobs])].sort();
    return all.concat("Pending");
  }
  const UNUSUAL_STATES = new Set(["FAILED", "TIMEOUT", "NODE_FAIL", "BOOT_FAIL", "PREEMPTED", "REVOKED", "SPECIAL_EXIT", "FINISHED_UNKNOWN"]);
  const BAD_NODE_STATES = new Set(["DOWN", "DRAIN", "DRNG", "MAINT", "NOT_RESPONDING"]);
  const UNUSUAL_EMOJI = "⚠️";
  const PALETTE = [
    "#d4a574", "#c9956a", "#e0b080", "#b88860", "#dcb090",
    "#c8a070", "#e8c090", "#b08058", "#d8a878", "#c09068",
  ];
  const DISK_PALETTE = [
    "#2563eb", "#0d9488", "#059669", "#ca8a04", "#c2410c",
    "#7c3aed", "#be185d", "#0369a1", "#0f766e", "#b45309",
  ];

  const DISK_STORAGE_LEFT = "slurm-disk-left";
  const DISK_STORAGE_RIGHT = "slurm-disk-right";

  let state = {
    jobs: [],
    nodes: [],
    node_states: {},
    node_reasons: {},
    gpusPerNode: GPUS_PER_NODE,
    cpusPerNode: CPUS_PER_NODE,
    timeMin: 0,
    timeMax: 0,
    refreshIntervalMs: 10000,
    refreshTimer: null,
    historyMode: false,
    diskSamples: [],
    diskList: [],
  };

  function nowMs() {
    return Date.now();
  }

  function setTimeRange(hours) {
    const now = nowMs();
    const half = hours * 60 * 60 * 1000;
    state.timeMin = now - half;
    state.timeMax = now + half;
  }

  function zoom(inOut) {
    const span = state.timeMax - state.timeMin;
    const half = span / 2;
    const center = state.timeMin + half;
    const newHalf = inOut > 0 ? Math.max(1 * 60 * 60 * 1000, half / 1.5) : Math.min(7 * 24 * 60 * 60 * 1000, half * 1.5);
    state.timeMin = center - newHalf;
    state.timeMax = center + newHalf;
    
    // Re-fetch data after zoom to get current data for the new window
    if (state.historyMode) {
      fetchJobs();
    } else {
      render();
    }
  }

  function timeToX(ms, width) {
    const t = (ms - state.timeMin) / (state.timeMax - state.timeMin);
    return Math.max(0, Math.min(1, t)) * width;
  }

  /** Format ms offset from now as relative time string (e.g. "-6h", "now", "+12h"). */
  function formatRelativeTime(ms) {
    const now = nowMs();
    const diff = ms - now;
    if (Math.abs(diff) < 60 * 1000) return "now";
    const h = diff / (60 * 60 * 1000);
    const d = diff / (24 * 60 * 60 * 1000);
    if (Math.abs(d) >= 1) return (diff > 0 ? "+" : "") + d.toFixed(1) + "d";
    if (Math.abs(h) >= 1) return (diff > 0 ? "+" : "") + h.toFixed(1) + "h";
    const m = diff / (60 * 1000);
    return (diff > 0 ? "+" : "") + m.toFixed(0) + "m";
  }

  /** Generate tick positions and labels (relative time). */
  function getTimeTicks(width) {
    const now = nowMs();
    const span = state.timeMax - state.timeMin;
    const ticks = [];
    const count = 6;
    for (let i = 0; i <= count; i++) {
      const t = state.timeMin + (span * i) / count;
      const x = timeToX(t, width);
      const label = formatRelativeTime(t);
      ticks.push({ x, label, t });
    }
    return ticks;
  }

  function renderTimeAxis(containerId, width) {
    const container = document.getElementById(containerId);
    if (!container) return;
    const axisHeight = 28;
    const rowEl = document.createElement("div");
    rowEl.className = "timeline-row time-axis-row";
    rowEl.innerHTML = `<span class="row-label"></span><div class="row-chart time-axis-chart"></div>`;
    const chartEl = rowEl.querySelector(".row-chart");
    chartEl.style.height = axisHeight + "px";
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", `0 0 ${width} ${axisHeight}`);
    svg.setAttribute("preserveAspectRatio", "none");
    svg.classList.add("time-axis");
    const ticks = getTimeTicks(width);
    ticks.forEach(({ x, label }) => {
      const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
      line.setAttribute("x1", x);
      line.setAttribute("y1", 0);
      line.setAttribute("x2", x);
      line.setAttribute("y2", 6);
      line.setAttribute("class", "time-axis-tick");
      line.setAttribute("stroke-width", 1);
      svg.appendChild(line);
      const text = document.createElementNS("http://www.w3.org/2000/svg", "text");
      text.setAttribute("x", x);
      text.setAttribute("y", axisHeight - 4);
      text.setAttribute("text-anchor", "middle");
      text.setAttribute("class", "time-axis-label");
      text.textContent = label;
      svg.appendChild(text);
    });
    chartEl.appendChild(svg);
    container.appendChild(rowEl);
  }

  function jobColor(jobId) {
    let h = 0;
    const s = String(jobId);
    for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
    return PALETTE[h % PALETTE.length];
  }

  function isUnusual(stateName) {
    return UNUSUAL_STATES.has(String(stateName).toUpperCase());
  }

  /** Hours running: from start to now (if running) or to end; pending = N/A (0). */
  function hoursRunning(job) {
    const startMs = toMs(job.start_time);
    if (startMs == null) return null;
    const now = nowMs();
    const endMs = toMs(job.end_time) || now;
    const ms = Math.min(endMs, now) - startMs;
    return Math.max(0, ms / (60 * 60 * 1000));
  }

  /** Normalize timestamp to ms (backend may send seconds). */
  function toMs(ts) {
    if (ts == null) return null;
    return ts < 1e12 ? ts * 1000 : ts;
  }

  /** Hours waited: for PENDING = time in queue (now - submit); else submit to start. Clamped >= 0. */
  function hoursWaited(job) {
    const submitMs = toMs(job.submit_time);
    if (submitMs == null) return 0;
    const now = nowMs();
    const toTime = job.state === "PENDING" ? now : (job.start_time != null ? toMs(job.start_time) : now);
    if (toTime == null) return Math.max(0, (now - submitMs) / (60 * 60 * 1000));
    const ms = toTime - submitMs;
    return Math.max(0, ms / (60 * 60 * 1000));
  }

  function buildTooltipHtml(job) {
    const gpus = job.allocations && job.allocations.length
      ? job.allocations.reduce((s, a) => s + (a.gpus || 0), 0) : (job.req_gpus || 0);
    const cpus = job.allocations && job.allocations.length
      ? job.allocations.reduce((s, a) => s + (a.cpus || 0), 0) : (job.req_cpus || 0);
    const arrayNote = job.array_task_count > 1 ? " (×" + job.array_task_count + " array)" : "";
    const runH = hoursRunning(job);
    const waitH = hoursWaited(job);
    const runStr = runH != null ? runH.toFixed(2) : "—";
    const waitLabel = job.state === "PENDING" ? "Time in queue (h)" : "Hours waited";
    const waitStr = waitH.toFixed(2);
    return `<table class="tooltip-table">
      <tr><th>ID</th><td>${escapeHtml(job.job_id)}</td></tr>
      <tr><th>User</th><td>${escapeHtml(job.user)}</td></tr>
      <tr><th>Task</th><td>${escapeHtml(job.job_name || job.job_id)}</td></tr>
      <tr><th>Priority</th><td>${job.priority != null ? job.priority : "—"}</td></tr>
      <tr><th>GPUs / CPUs</th><td>${gpus} / ${cpus}${escapeHtml(arrayNote)}</td></tr>
      <tr><th>Memory (MB)</th><td>${job.req_mem != null ? job.req_mem : "—"}</td></tr>
      <tr><th>Hours running</th><td>${runStr}</td></tr>
      <tr><th>${escapeHtml(waitLabel)}</th><td>${waitStr}</td></tr>
      <tr><th>State</th><td>${escapeHtml(job.state || "—")}</td></tr>
    </table>`;
  }

  function escapeHtml(s) {
    if (s == null) return "—";
    const div = document.createElement("div");
    div.textContent = s;
    return div.innerHTML;
  }

  /** One slot = one job on one row (main bar + optional wait bar), for stacking. */
  function buildRows(isGpu) {
    const rows = {};
    getRowLabels().forEach((label) => { rows[label] = []; });

    const now = nowMs();
    const perNode = isGpu ? (state.gpusPerNode || GPUS_PER_NODE) : (state.cpusPerNode || CPUS_PER_NODE);
    
    // Calculate total pending resources for proportional height in pending row
    let totalPendingRes = 0;
    state.jobs.forEach((j) => {
      if (j.state === "PENDING") {
        totalPendingRes += isGpu ? (j.req_gpus || 0) : (j.req_cpus || 0);
      }
    });
    totalPendingRes = Math.max(totalPendingRes, perNode); // At least one node's worth

    state.jobs.forEach((job) => {
      const unusual = isUnusual(job.state);
      const color = jobColor(job.job_id);
      const start = job.start_time;
      const end = job.end_time || (start ? start + 3600000 : now + 3600000);
      const submit = job.submit_time;

      if (job.allocations && job.allocations.length) {
        job.allocations.forEach((alloc) => {
          const node = alloc.node;
          if (!rows[node]) return;
          const size = isGpu ? (alloc.gpus || 0) : (alloc.cpus || 0);
          if (size <= 0) return; // Skip if no resources allocated
          const heightFrac = Math.min(1, size / perNode);
          const sortTime = start || submit || 0;
          rows[node].push({
            job,
            sortTime,
            mainStart: start,
            mainEnd: end,
            waitStart: submit != null && start != null && submit < start ? submit : null,
            waitEnd: submit != null && start != null && submit < start ? start : null,
            heightFrac,
            color,
            unusual,
          });
        });
      } else if (job.state === "PENDING") {
        const size = isGpu ? (job.req_gpus || 0) : (job.req_cpus || 0);
        // HeightFrac based on share of TOTAL pending resources (so all jobs fit in row)
        const heightFrac = size / totalPendingRes;
        const expectedMs = (job.time_limit_ms != null && job.time_limit_ms > 0) ? job.time_limit_ms : 3600000;
        rows["Pending"].push({
          job,
          sortTime: submit || now,
          mainStart: null,
          mainEnd: null,
          waitStart: submit || now,
          waitEnd: now + expectedMs,
          heightFrac,
          color,
          unusual,
        });
      }
    });

    Object.keys(rows).forEach((key) => {
      if (key === "Pending") {
        rows[key].sort((a, b) => {
          const pa = a.job.priority != null ? a.job.priority : 0;
          const pb = b.job.priority != null ? b.job.priority : 0;
          if (pb !== pa) return pb - pa;
          return a.sortTime - b.sortTime;
        });
      } else {
        rows[key].sort((a, b) => a.sortTime - b.sortTime);
      }
    });

    return rows;
  }

  /** Pending row height multiplier based on total pending resources. */
  function getPendingRowMultiplier() {
    let totalGpus = 0;
    let totalCpus = 0;
    state.jobs.forEach((job) => {
      if (job.state === "PENDING") {
        totalGpus += job.req_gpus || 0;
        totalCpus += job.req_cpus || 0;
      }
    });
    // Use actual resource demand, no artificial cap
    return Math.max(totalGpus / GPUS_PER_NODE, totalCpus / CPUS_PER_NODE, 1);
  }

  function getRowHeights() {
    const mult = getPendingRowMultiplier();
    const labels = getRowLabels();
    const nodeCount = labels.length - 1;
    return labels.map((_, i) => (i < nodeCount ? BASE_ROW_HEIGHT : BASE_ROW_HEIGHT * mult));
  }

  /** Check if two time intervals overlap. */
  function intervalsOverlap(start1, end1, start2, end2) {
    if (start1 == null || end1 == null || start2 == null || end2 == null) return false;
    return start1 < end2 && start2 < end1;
  }

  /** Assign lanes to slots based on temporal overlap. Modifies slots to add lane property. */
  function assignLanes(slots) {
    if (!slots || slots.length === 0) return { maxLanes: 1, lanesUsed: [] };
    
    // Sort slots by start time
    const sortedSlots = slots.slice().sort((a, b) => {
      const aStart = a.mainStart || a.waitStart || 0;
      const bStart = b.mainStart || b.waitStart || 0;
      return aStart - bStart;
    });
    
    // Track which lanes are occupied and when they'll be free
    const lanes = []; // Each entry: { endTime, heightFrac }
    
    sortedSlots.forEach((slot) => {
      if (!isFinite(slot.heightFrac) || slot.heightFrac <= 0) {
        slot.lane = 0;
        slot.laneHeightFrac = 0;
        return;
      }
      
      // For lane assignment, use main period (running jobs) or wait period (pending jobs)
      // Don't include waiting periods of finished jobs to avoid unnecessary lane creation
      const slotStart = slot.mainStart || slot.waitStart;
      const slotEnd = slot.mainEnd || slot.waitEnd;
      
      if (slotStart == null || slotEnd == null) {
        slot.lane = 0;
        slot.laneHeightFrac = slot.heightFrac;
        return;
      }
      
      // For finished jobs with both wait and main periods, only check main period overlap
      const useMainOnly = slot.mainStart != null && slot.mainEnd != null;
      const checkStart = useMainOnly ? slot.mainStart : slotStart;
      const checkEnd = useMainOnly ? slot.mainEnd : slotEnd;
      
      // Find first available lane (where all jobs have ended before this one starts)
      let assignedLane = -1;
      for (let i = 0; i < lanes.length; i++) {
        if (lanes[i].endTime <= checkStart) {
          assignedLane = i;
          break;
        }
      }
      
      // If no lane available, create a new one
      if (assignedLane === -1) {
        assignedLane = lanes.length;
        lanes.push({ endTime: checkEnd, heightFrac: slot.heightFrac });
      } else {
        lanes[assignedLane] = { endTime: checkEnd, heightFrac: slot.heightFrac };
      }
      
      // #region agent log
      if (slots.length > 1) {
        fetch('http://localhost:7242/ingest/0169ba02-75f6-4903-bc26-90121609c148',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({location:'app.js:assignLanes',message:'lane assigned',data:{jobId: slot.job?.job_id, checkStart, checkEnd, assignedLane, lanesLength: lanes.length}, timestamp: Date.now(), sessionId: 'debug-session', hypothesisId: 'C'})}).catch(()=>{});
      }
      // #endregion
      slot.lane = assignedLane;
      slot.laneHeightFrac = slot.heightFrac;
    });
    
    // Calculate total height needed for all lanes
    const totalHeightFrac = lanes.reduce((sum, lane) => sum + lane.heightFrac, 0);
    
    return { maxLanes: lanes.length, totalHeightFrac: totalHeightFrac || 1 };
  }

  function getDiskSelection(side) {
    const key = side === "left" ? DISK_STORAGE_LEFT : DISK_STORAGE_RIGHT;
    try {
      const raw = localStorage.getItem(key);
      if (raw) {
        const arr = JSON.parse(raw);
        if (Array.isArray(arr)) return arr;
      }
    } catch (e) { /* ignore */ }
    const list = state.diskList || [];
    const n = list.length;
    const half = Math.floor(n / 2);
    return side === "left" ? list.slice(0, half) : list.slice(half);
  }

  function setDiskSelection(side, mounts) {
    const key = side === "left" ? DISK_STORAGE_LEFT : DISK_STORAGE_RIGHT;
    try {
      localStorage.setItem(key, JSON.stringify(mounts));
    } catch (e) { /* ignore */ }
  }

  function buildDiskRow(containerId, width) {
    const isLeft = containerId.includes("gpu");
    const side = isLeft ? "left" : "right";
    const selected = getDiskSelection(side);
    const rowHeight = 100;
    const rowEl = document.createElement("div");
    rowEl.className = "timeline-row disk-row";
    rowEl.innerHTML = `<span class="row-label">Disk <button type="button" class="disk-customize-btn" data-side="${side}" aria-label="Customize disks"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg></button></span><div class="row-chart disk-chart"></div>`;
    const chartEl = rowEl.querySelector(".row-chart");
    chartEl.style.height = rowHeight + "px";

    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", `0 0 ${width} ${rowHeight}`);
    svg.setAttribute("preserveAspectRatio", "none");
    svg.setAttribute("data-disk-side", side);

    const now = nowMs();
    const timeMin = state.timeMin;
    const timeMax = state.timeMax;
    const span = timeMax - timeMin || 1;

    const ticks = getTimeTicks(width);
    ticks.forEach(({ x, label: tickLabel }) => {
      const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
      line.setAttribute("x1", x);
      line.setAttribute("y1", 0);
      line.setAttribute("x2", x);
      line.setAttribute("y2", rowHeight);
      line.setAttribute("class", tickLabel === "now" ? "guideline guideline-now" : "guideline");
      svg.appendChild(line);
    });

    const samples = (state.diskSamples || []).filter((s) => s.ts_ms <= now);
    if (samples.length === 0) {
      const text = document.createElementNS("http://www.w3.org/2000/svg", "text");
      text.setAttribute("x", width / 2);
      text.setAttribute("y", rowHeight / 2);
      text.setAttribute("text-anchor", "middle");
      text.setAttribute("fill", "var(--text-muted)");
      text.textContent = "No disk data";
      svg.appendChild(text);
    } else {
      selected.forEach((mount, idx) => {
        const color = DISK_PALETTE[idx % DISK_PALETTE.length];
        const points = samples.map((s) => {
          const x = ((s.ts_ms - timeMin) / span) * width;
          const pct = (s.disks[mount] && s.disks[mount].use_pct != null) ? s.disks[mount].use_pct : 0;
          const y = rowHeight - (pct / 100) * rowHeight;
          return `${x},${y}`;
        }).join(" ");
        if (points) {
          const poly = document.createElementNS("http://www.w3.org/2000/svg", "polyline");
          poly.setAttribute("points", points);
          poly.setAttribute("fill", "none");
          poly.setAttribute("stroke", color);
          poly.setAttribute("stroke-width", "1.5");
          poly.setAttribute("data-disk-mount", mount);
          svg.appendChild(poly);
        }
      });
    }

    chartEl.appendChild(svg);

    rowEl.querySelector(".disk-customize-btn").addEventListener("click", () => openDiskModal(side));
    attachDiskChartHover(chartEl, side, width, rowHeight);
    return rowEl;
  }

  function attachDiskChartHover(chartEl, side, width, rowHeight) {
    const samples = state.diskSamples || [];
    const selected = getDiskSelection(side);
    if (samples.length === 0) return;
    let tooltipEl = document.getElementById("disk-tooltip");
    if (!tooltipEl) {
      tooltipEl = document.createElement("div");
      tooltipEl.id = "disk-tooltip";
      tooltipEl.className = "disk-tooltip";
      tooltipEl.setAttribute("aria-hidden", "true");
      document.body.appendChild(tooltipEl);
      document.addEventListener("mousemove", function diskTooltipDocMove(ev) {
        const charts = document.querySelectorAll(".disk-chart");
        const overAny = Array.from(charts).some((el) => {
          const r = el.getBoundingClientRect();
          return ev.clientX >= r.left && ev.clientX <= r.right && ev.clientY >= r.top && ev.clientY <= r.bottom;
        });
        if (!overAny) {
          tooltipEl.classList.remove("visible");
          tooltipEl.setAttribute("aria-hidden", "true");
          document.querySelectorAll(".disk-chart .disk-highlight-line").forEach((el) => el.remove());
        }
      });
    }
    let highlightLine = null;

    chartEl.addEventListener("mousemove", (ev) => {
      const rect = chartEl.getBoundingClientRect();
      const x = ev.clientX - rect.left;
      const t = (x / width) * (state.timeMax - state.timeMin) + state.timeMin;
      let best = samples[0];
      let bestD = Math.abs(best.ts_ms - t);
      samples.forEach((s) => {
        const d = Math.abs(s.ts_ms - t);
        if (d < bestD) { bestD = d; best = s; }
      });
      if (!highlightLine) {
        highlightLine = document.createElementNS("http://www.w3.org/2000/svg", "line");
        highlightLine.setAttribute("class", "disk-highlight-line");
        highlightLine.setAttribute("stroke", "var(--accent)");
        highlightLine.setAttribute("stroke-width", "2");
        highlightLine.setAttribute("y1", 0);
        highlightLine.setAttribute("y2", rowHeight);
        chartEl.querySelector("svg").appendChild(highlightLine);
      }
      const xPos = ((best.ts_ms - state.timeMin) / (state.timeMax - state.timeMin)) * width;
      highlightLine.setAttribute("x1", xPos);
      highlightLine.setAttribute("x2", xPos);

      const items = selected.map((mount, idx) => {
        const d = best.disks[mount];
        const color = DISK_PALETTE[idx % DISK_PALETTE.length];
        const usePct = d && d.use_pct != null ? d.use_pct : -1;
        if (!d) return { color, text: mount + " —", usePct };
        const avail = d.avail_gb != null ? d.avail_gb : 0;
        const total = d.total_gb != null ? d.total_gb : 0;
        const totalStr = total >= 1000 ? (total / 1000).toFixed(1) + " TB" : total + " GB";
        return { color, text: mount + " " + avail + " GB free of " + totalStr, usePct };
      });
      items.sort((a, b) => b.usePct - a.usePct);
      tooltipEl.innerHTML = items.map((l) => `<div class="disk-tooltip-line"><span class="disk-tooltip-swatch" style="background-color:${l.color}"></span>${escapeHtml(l.text)}</div>`).join("");
      tooltipEl.style.left = (ev.clientX + 12) + "px";
      tooltipEl.style.top = (ev.clientY + 12) + "px";
      tooltipEl.classList.add("visible");
      tooltipEl.setAttribute("aria-hidden", "false");
    });
    chartEl.addEventListener("mouseleave", () => {
      if (highlightLine && highlightLine.parentNode) highlightLine.parentNode.removeChild(highlightLine);
      highlightLine = null;
      tooltipEl.classList.remove("visible");
      tooltipEl.setAttribute("aria-hidden", "true");
    });
  }

  function openDiskModal(side) {
    const list = state.diskList || [];
    const selected = getDiskSelection(side);
    const title = side === "left" ? "Disks for left chart" : "Disks for right chart";
    let modal = document.getElementById("disk-modal");
    if (!modal) {
      modal = document.createElement("div");
      modal.id = "disk-modal";
      modal.className = "disk-modal";
      modal.setAttribute("role", "dialog");
      modal.setAttribute("aria-hidden", "true");
      modal.innerHTML = `<div class="disk-modal-content"><h3 class="disk-modal-title"></h3><div class="disk-modal-list"></div><button type="button" class="disk-modal-apply">Apply</button></div>`;
      document.body.appendChild(modal);
      modal.querySelector(".disk-modal-apply").addEventListener("click", () => {
        const currentSide = modal.getAttribute("data-side");
        const checkboxes = modal.querySelectorAll('input[name="disk-sel"]:checked');
        const mounts = Array.from(checkboxes).map((cb) => cb.value);
        setDiskSelection(currentSide, mounts);
        modal.classList.remove("visible");
        modal.setAttribute("aria-hidden", "true");
        render();
      });
    }
    modal.setAttribute("data-side", side);
    modal.querySelector(".disk-modal-title").textContent = title;
    const listEl = modal.querySelector(".disk-modal-list");
    listEl.textContent = "";
    list.forEach((mount) => {
      const label = document.createElement("label");
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.name = "disk-sel";
      cb.value = mount;
      if (selected.indexOf(mount) >= 0) cb.checked = true;
      label.appendChild(cb);
      label.appendChild(document.createTextNode(" " + mount));
      listEl.appendChild(label);
    });
    modal.classList.add("visible");
    modal.setAttribute("aria-hidden", "false");
  }

  function stackAndRender(rows, containerId, widthHint, rowHeights, prependRow) {
    const container = document.getElementById(containerId);
    if (!container) return 0;
    container.textContent = "";
    if (prependRow) container.appendChild(prependRow);

    const width = widthHint || Math.max(400, container.offsetWidth || 400);
    const labels = getRowLabels();
    const heights = rowHeights || labels.map(() => BASE_ROW_HEIGHT);

    labels.forEach((label, rowIndex) => {
      const slots = rows[label] || [];
      const rowHeight = heights[rowIndex] || BASE_ROW_HEIGHT;
      const rowEl = document.createElement("div");
      rowEl.className = "timeline-row";
      rowEl.innerHTML = `<span class="row-label">${label}</span><div class="row-chart"></div>`;
      const chartEl = rowEl.querySelector(".row-chart");
      chartEl.style.height = rowHeight + "px";

      // Number of lanes = number of resources (GPUs or CPUs) - fixed, not calculated
      const isGpuSide = containerId.includes("gpu");
      const perNode = isGpuSide ? (state.gpusPerNode || GPUS_PER_NODE) : (state.cpusPerNode || CPUS_PER_NODE);
      const isPendingRow = label === "Pending";
      
      // For node rows, use lane-based y (assignLanes reuses lanes when timeline is free). Only Pending uses cumulative stacking.
      let useCumulativeStacking = isPendingRow;
      let laneInfo = { maxLanes: 1 };
      if (!isPendingRow) {
        laneInfo = assignLanes(slots);
      }
      const numLanes = useCumulativeStacking ? 1 : (laneInfo.maxLanes > 1 ? laneInfo.maxLanes : perNode);
      const laneHeight = rowHeight / numLanes;

      // #region agent log
      if (!isPendingRow && slots.length > 0) {
        fetch('http://localhost:7242/ingest/0169ba02-75f6-4903-bc26-90121609c148',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({location:'app.js:stackAndRender',message:'row lane config',data:{label, maxLanes: laneInfo.maxLanes, useCumulativeStacking, numLanes}, timestamp: Date.now(), sessionId: 'debug-session', hypothesisId: 'A'})}).catch(()=>{});
      }
      // #endregion

      const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      svg.setAttribute("viewBox", `0 0 ${width} ${rowHeight}`);
      svg.setAttribute("preserveAspectRatio", "none");

      const ticks = getTimeTicks(width);
      ticks.forEach(({ x, label: tickLabel }) => {
        const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
        line.setAttribute("x1", x);
        line.setAttribute("y1", 0);
        line.setAttribute("x2", x);
        line.setAttribute("y2", rowHeight);
        line.setAttribute("class", tickLabel === "now" ? "guideline guideline-now" : "guideline");
        svg.appendChild(line);
      });

      // For overlapping jobs: cumulative y stacking. For non-overlapping: lane-based positioning.
      let cumulativeY = 0;
      slots.forEach((slot, slotIndex) => {
        const lane = slot.lane || 0;
        const y = useCumulativeStacking ? cumulativeY : (lane * laneHeight);
        // #region agent log
        if (!isPendingRow && slots.length > 1) {
          fetch('http://localhost:7242/ingest/0169ba02-75f6-4903-bc26-90121609c148',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({location:'app.js:slot render',message:'slot y and lane',data:{label, jobId: slot.job?.job_id, slotIndex, lane, y, cumulativeYBefore: cumulativeY, useCumulativeStacking}, timestamp: Date.now(), sessionId: 'debug-session', hypothesisId: 'B'})}).catch(()=>{});
        }
        // #endregion
        // Height is based on resource usage relative to TOTAL row
        const h = Math.max(0, rowHeight * slot.heightFrac);
        const job = slot.job;
        const labelStr = "#" + job.job_id + " · P=" + (job.priority != null ? job.priority : "—") + " · " + job.user + " · " + (job.job_name || job.job_id);
        const prefix = slot.unusual ? UNUSUAL_EMOJI + " " : "";

        if (slot.waitStart != null && slot.waitEnd != null) {
          const x1 = timeToX(slot.waitStart, width);
          const x2 = timeToX(slot.waitEnd, width);
          const w = Math.max(1, x2 - x1);
          const rect = document.createElementNS("http://www.w3.org/2000/svg", "rect");
          rect.setAttribute("x", x1);
          rect.setAttribute("y", y);
          rect.setAttribute("width", w);
          rect.setAttribute("height", h);
          rect.setAttribute("fill", slot.color);
          rect.setAttribute("data-job-id", job.job_id);
          rect.classList.add("timeline-bar", "job-bar", "wait");
          if (slot.unusual) rect.classList.add("unusual");
          // Hide waiting period for finished jobs (show only on hover)
          const finishedStates = ["COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "COMPLETING", "NODE_FAIL", "PREEMPTED", "OUT_OF_MEMORY"];
          if (finishedStates.includes(job.state)) {
            rect.classList.add("wait-historic");
          }
          svg.appendChild(rect);
        }

        if (slot.mainStart != null && slot.mainEnd != null) {
          const x1 = timeToX(slot.mainStart, width);
          const x2 = timeToX(slot.mainEnd, width);
          const w = Math.max(1, x2 - x1);
          const rect = document.createElementNS("http://www.w3.org/2000/svg", "rect");
          rect.setAttribute("x", x1);
          rect.setAttribute("y", y);
          rect.setAttribute("width", w);
          rect.setAttribute("height", h);
          rect.setAttribute("fill", slot.color);
          rect.setAttribute("data-job-id", job.job_id);
          rect.classList.add("timeline-bar", "job-bar", "main");
          if (slot.unusual) rect.classList.add("unusual");
          svg.appendChild(rect);

          const text = document.createElementNS("http://www.w3.org/2000/svg", "text");
          text.setAttribute("x", x1 + 4);
          text.setAttribute("y", y + h / 2);
          text.setAttribute("class", "label timeline-bar main");
          if (slot.unusual) text.classList.add("unusual");
          text.textContent = prefix + labelStr;
          svg.appendChild(text);
        } else if (slot.waitStart != null && slot.waitEnd != null) {
          const x1 = timeToX(slot.waitStart, width);
          const pendingText = document.createElementNS("http://www.w3.org/2000/svg", "text");
          pendingText.setAttribute("x", x1 + 4);
          pendingText.setAttribute("y", y + h / 2);
          pendingText.setAttribute("class", "label timeline-bar wait");
          if (slot.unusual) pendingText.classList.add("unusual");
          pendingText.textContent = prefix + labelStr;
          svg.appendChild(pendingText);
        }
        
        // For cumulative stacking (pending or overlapping jobs), increment y
        if (useCumulativeStacking) cumulativeY += h;
      });

      chartEl.appendChild(svg);

      const nodeState = state.node_states && state.node_states[label];
      const parts = nodeState ? String(nodeState).split("+").map((s) => s.trim().toUpperCase().replace(/\*$/, "")) : [];
      const badState = label !== "Pending" && parts.some((p) => BAD_NODE_STATES.has(p));
      if (badState) {
        const overlay = document.createElement("div");
        overlay.className = "node-status-overlay";
        const statusText = (nodeState || "").replace(/\bDRNG\b/gi, "DRAINING");
        overlay.innerHTML = "<span class=\"node-status-label\">" + escapeHtml(statusText) + "</span>" +
          (state.node_reasons && state.node_reasons[label]
            ? "<span class=\"node-status-reason\">" + escapeHtml(state.node_reasons[label]) + "</span>"
            : "");
        chartEl.appendChild(overlay);
      }

      container.appendChild(rowEl);
    });
    return width;
  }

  function render() {
    const gpuRows = buildRows(true);
    const cpuRows = buildRows(false);
    const rowHeights = getRowHeights();
    const gpuContainer = document.getElementById("gpu-timelines");
    const cpuContainer = document.getElementById("cpu-timelines");
    const width = Math.max(
      400,
      (gpuContainer && gpuContainer.offsetWidth) || 0,
      (cpuContainer && cpuContainer.offsetWidth) || 0
    ) || 400;
    const diskLeftRow = buildDiskRow("gpu-timelines", width);
    const diskRightRow = buildDiskRow("cpu-timelines", width);
    stackAndRender(gpuRows, "gpu-timelines", width, rowHeights, diskLeftRow);
    stackAndRender(cpuRows, "cpu-timelines", width, rowHeights, diskRightRow);
    renderTimeAxis("gpu-timelines", width);
    renderTimeAxis("cpu-timelines", width);
    updateSummaryTitle();
    attachTooltipAndHighlight();
    attachJobEditPopup();
  }

  function updateSummaryTitle() {
    const totalNodes = Math.max((state.nodes || []).length, 1);
    const totalGpus = totalNodes * (state.gpusPerNode || GPUS_PER_NODE);
    const totalCpus = totalNodes * (state.cpusPerNode || CPUS_PER_NODE);
    const goodStates = new Set(["IDLE", "ALLOC", "MIX", "RESV", "COMP"]);
    let nodesUp = 0;
    (state.nodes || []).forEach((n) => {
      const s = (state.node_states && state.node_states[n]) ? String(state.node_states[n]).toUpperCase() : "IDLE";
      if (goodStates.has(s)) nodesUp++;
    });
    if (!state.nodes || state.nodes.length === 0) nodesUp = (state.nodes || []).length;

    let gpusUsed = 0, cpusUsed = 0, gpusQueued = 0, cpusQueued = 0;
    state.jobs.forEach((job) => {
      if (job.state === "PENDING") {
        gpusQueued += job.req_gpus || 0;
        cpusQueued += job.req_cpus || 0;
      } else if (job.state === "RUNNING" && job.allocations && job.allocations.length) {
        job.allocations.forEach((a) => {
          gpusUsed += a.gpus || 0;
          cpusUsed += a.cpus || 0;
        });
      }
    });

    const plain = `${nodesUp}/${totalNodes} up · ${gpusUsed}/${totalGpus} GPUs, ${cpusUsed}/${totalCpus} CPUs used · ${gpusQueued} GPUs, ${cpusQueued} CPUs queued`;
    const h1 = document.querySelector(".header h1");
    if (h1) {
      h1.innerHTML = nodesUp + "/<span class=\"title-total\">" + totalNodes + "</span> up · " +
        gpusUsed + "/<span class=\"title-total\">" + totalGpus + "</span> GPUs, " +
        cpusUsed + "/<span class=\"title-total\">" + totalCpus + "</span> CPUs used · " +
        gpusQueued + " GPUs, " + cpusQueued + " CPUs queued";
    }
    document.title = plain;
  }

  function attachJobEditPopup() {
    const popup = document.getElementById("job-edit-popup");
    const priorityInput = document.getElementById("edit-priority");
    const memoryInput = document.getElementById("edit-memory_mb");
    const cpusInput = document.getElementById("edit-num_cpus");
    const saveBtn = document.querySelector(".edit-save");
    if (!popup || !priorityInput || !memoryInput || !cpusInput || !saveBtn) return;

    let currentJobId = null;

    function open(jobId, job, ev) {
      currentJobId = jobId;
      priorityInput.value = job.priority != null ? job.priority : 0;
      memoryInput.value = job.req_mem != null ? job.req_mem : 0;
      cpusInput.value = job.req_cpus != null ? job.req_cpus : 0;
      popup.classList.add("visible");
      popup.setAttribute("aria-hidden", "false");
      const pad = 8;
      popup.style.left = (ev.clientX + pad) + "px";
      popup.style.top = (ev.clientY + pad) + "px";
      priorityInput.focus();
      setTimeout(() => {
        document.addEventListener("click", closeOnClickOutside);
      }, 0);
    }

    function close() {
      popup.classList.remove("visible");
      popup.setAttribute("aria-hidden", "true");
      currentJobId = null;
      document.removeEventListener("click", closeOnClickOutside);
    }

    function closeOnClickOutside(ev) {
      if (popup.contains(ev.target)) return;
      close();
    }

    const editSteps = { priority: 2, num_cpus: 10, memory_mb: 5000 };
    function stepInput(field, delta) {
      const input = document.getElementById("edit-" + field);
      if (!input) return;
      const step = editSteps[field] || 1;
      const v = parseInt(input.value, 10) || 0;
      input.value = Math.max(0, v + delta * step);
    }

    function save() {
      if (!currentJobId) return;
      const priority = parseInt(priorityInput.value, 10);
      const memory_mb = parseInt(memoryInput.value, 10);
      const num_cpus = parseInt(cpusInput.value, 10);
      const body = {};
      if (!isNaN(priority) && priority >= 0) body.priority = priority;
      if (!isNaN(memory_mb) && memory_mb >= 0) body.memory_mb = memory_mb;
      if (!isNaN(num_cpus) && num_cpus >= 0) body.num_cpus = num_cpus;
      if (Object.keys(body).length === 0) return;
      fetch("/api/job/" + encodeURIComponent(currentJobId) + "/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      })
        .then((r) => r.json())
        .then((data) => {
          if (data.ok) {
            close();
            fetchJobs();
          } else {
            alert(data.error || "Failed to update");
          }
        })
        .catch(() => alert("Failed to update"));
    }

    popup.querySelectorAll(".edit-minus").forEach((btn) => {
      btn.addEventListener("click", (ev) => {
        ev.stopPropagation();
        stepInput(btn.getAttribute("data-field"), -1);
      });
    });
    popup.querySelectorAll(".edit-plus").forEach((btn) => {
      btn.addEventListener("click", (ev) => {
        ev.stopPropagation();
        stepInput(btn.getAttribute("data-field"), 1);
      });
    });
    saveBtn.addEventListener("click", (ev) => {
      ev.stopPropagation();
      save();
    });
    [priorityInput, memoryInput, cpusInput].forEach((input) => {
      input.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter") {
          ev.preventDefault();
          ev.stopPropagation();
          save();
        }
      });
    });

    ["gpu-timelines", "cpu-timelines"].forEach((id) => {
      const el = document.getElementById(id);
      if (!el) return;
      el.removeEventListener("click", el._jobEditClick);
      el._jobEditClick = (ev) => {
        const t = ev.target;
        if (!t || !t.getAttribute || !t.getAttribute("data-job-id")) return;
        const jobId = t.getAttribute("data-job-id");
        const job = state.jobs.find((j) => j.job_id === jobId);
        if (!job || job.state !== "PENDING") return;
        ev.preventDefault();
        ev.stopPropagation();
        open(jobId, job, ev);
      };
      el.addEventListener("click", el._jobEditClick);
    });
  }

  function attachTooltipAndHighlight() {
    const tooltip = document.getElementById("job-tooltip");
    if (!tooltip) return;
    const containers = ["gpu-timelines", "cpu-timelines"];
    const show = (jobId, ev) => {
      const job = state.jobs.find((j) => j.job_id === jobId);
      if (!job) return;
      tooltip.innerHTML = buildTooltipHtml(job);
      tooltip.classList.add("visible");
      
      // Show historic waiting bars for this job
      document.querySelectorAll(`.wait-historic[data-job-id="${jobId}"]`).forEach((el) => {
        el.style.opacity = "0.5";
      });
      tooltip.setAttribute("aria-hidden", "false");
      const pad = 12;
      tooltip.style.left = (ev.clientX + pad) + "px";
      tooltip.style.top = (ev.clientY + pad) + "px";
      document.querySelectorAll('[data-job-id="' + CSS.escape(jobId) + '"]').forEach((el) => el.classList.add("highlight"));
    };
    const hide = (jobId) => {
      tooltip.classList.remove("visible");
      tooltip.setAttribute("aria-hidden", "true");
      if (jobId) document.querySelectorAll('[data-job-id="' + CSS.escape(jobId) + '"]').forEach((el) => el.classList.remove("highlight"));
      // Hide historic waiting bars again
      document.querySelectorAll(".wait-historic").forEach((el) => {
        el.style.opacity = "";
      });
    };
    containers.forEach((id) => {
      const el = document.getElementById(id);
      if (!el) return;
      el.removeEventListener("mouseover", el._jobBarOver);
      el.removeEventListener("mouseout", el._jobBarOut);
      el._jobBarOver = (ev) => {
        const t = ev.target;
        if (t && t.getAttribute && t.getAttribute("data-job-id")) {
          const jobId = t.getAttribute("data-job-id");
          show(jobId, ev);
          el._hoverJobId = jobId;
        }
      };
      el._jobBarOut = (ev) => {
        const t = ev.relatedTarget;
        const jobId = el._hoverJobId;
        const stillOverPartner = t && t.getAttribute && t.getAttribute("data-job-id") === jobId;
        if (!stillOverPartner) {
          hide(jobId);
          el._hoverJobId = null;
        }
      };
      el.addEventListener("mouseover", el._jobBarOver);
      el.addEventListener("mouseout", el._jobBarOut);
    });
  }

  function fetchJobs() {
    // Update time range first for live mode, preserving zoom level
    if (!state.historyMode) {
      const currentSpan = state.timeMax - state.timeMin;
      const defaultSpan = 12 * 60 * 60 * 1000;
      // Use current span if user has zoomed, otherwise use default
      const span = (currentSpan > 0 && currentSpan !== defaultSpan * 2) ? currentSpan : defaultSpan * 2;
      const now = nowMs();
      const half = span / 2;
      state.timeMin = now - half;
      state.timeMax = now + half;
    }
    const intervalSec = Math.floor(state.refreshIntervalMs / 1000);
    const from = Math.floor(state.timeMin);
    const to = Math.floor(state.timeMax);
    const jobsUrl = "/api/jobs?from=" + from + "&to=" + to + "&interval=" + intervalSec;
    const diskUrl = "/api/disk?from=" + from + "&to=" + to;
    Promise.all([fetch(jobsUrl).then((r) => r.json()), fetch(diskUrl).then((r) => r.json())])
      .then(([data, diskData]) => {
        state.jobs = data.jobs || [];
        state.nodes = data.nodes || [];
        state.node_states = data.node_states || {};
        state.node_reasons = data.node_reasons || {};
        state.gpusPerNode = data.gpus_per_node || GPUS_PER_NODE;
        state.cpusPerNode = data.cpus_per_node || CPUS_PER_NODE;
        state.diskSamples = diskData.samples || [];
        state.diskList = diskData.disks || [];
        render();
        document.getElementById("last-updated").textContent = "Updated " + new Date().toLocaleTimeString();
      })
      .catch((err) => {
        console.error("Fetch error:", err);
        document.getElementById("last-updated").textContent = "Update failed";
      });
  }

  function startRefresh() {
    if (state.refreshTimer) clearInterval(state.refreshTimer);
    state.refreshTimer = setInterval(fetchJobs, state.refreshIntervalMs);
  }

  function viewHistoryAt(timestampMs) {
    const halfWindow = 12 * 60 * 60 * 1000;
    state.historyMode = true;
    state.timeMin = timestampMs - halfWindow;
    state.timeMax = timestampMs + halfWindow;
    if (state.refreshTimer) {
      clearInterval(state.refreshTimer);
      state.refreshTimer = null;
    }
    fetchJobs();
  }

  function viewHistory() {
    const dateInput = document.getElementById("history-date");
    const timeInput = document.getElementById("history-time");
    
    if (!dateInput.value) {
      alert("Please select a date");
      return;
    }
    
    // Date input gives YYYY-MM-DD format
    const dateStr = dateInput.value;
    const timeStr = timeInput.value || "12:00"; // Default to noon if no time selected
    
    const picked = new Date(dateStr + "T" + timeStr);
    const pickedMs = picked.getTime();
    
    if (isNaN(pickedMs)) {
      alert("Invalid date/time");
      return;
    }
    
    viewHistoryAt(pickedMs);
  }

  function populateDateTimePickers() {
    const now = new Date();
    const dateInput = document.getElementById("history-date");
    const timeInput = document.getElementById("history-time");
    
    // Format: YYYY-MM-DD for date input
    const year = now.getFullYear();
    const month = String(now.getMonth() + 1).padStart(2, '0');
    const day = String(now.getDate()).padStart(2, '0');
    dateInput.value = `${year}-${month}-${day}`;
    
    // Format: HH:MM for time input
    const hours = String(now.getHours()).padStart(2, '0');
    const minutes = String(now.getMinutes()).padStart(2, '0');
    timeInput.value = `${hours}:${minutes}`;
  }

  function goLive() {
    state.historyMode = false;
    setTimeRange(12);
    populateDateTimePickers();
    fetchJobs();
    startRefresh();
  }

  document.getElementById("zoom-in").addEventListener("click", () => zoom(1));
  document.getElementById("zoom-out").addEventListener("click", () => zoom(-1));
  document.getElementById("view-history").addEventListener("click", viewHistory);
  document.getElementById("live-mode").addEventListener("click", goLive);
  document.getElementById("history-preset").addEventListener("change", (e) => {
    const hoursAgo = parseInt(e.target.value, 10);
    if (hoursAgo) {
      const timestampMs = Date.now() - (hoursAgo * 60 * 60 * 1000);
      viewHistoryAt(timestampMs);
      e.target.value = ""; // Reset dropdown
    }
  });
  document.getElementById("refresh-interval").addEventListener("change", (e) => {
    state.refreshIntervalMs = parseInt(e.target.value, 10) * 1000;
    startRefresh();
  });

  // Populate date/time inputs with current time on load
  populateDateTimePickers();
  
  setTimeRange(12);
  fetchJobs();
  startRefresh();
})();
