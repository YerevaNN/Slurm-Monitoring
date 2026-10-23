'use strict';

const ADMIN_TOKEN_STORAGE_KEY = 'slurm-monitor-admin-token';

function apiUrl(path) {
  return new URL(String(path).replace(/^\/+/, ''), document.baseURI).toString();
}

async function adminFetch(path, options = {}) {
  for (let attempt = 0; attempt < 2; attempt++) {
    let token = sessionStorage.getItem(ADMIN_TOKEN_STORAGE_KEY);
    if (!token) {
      token = window.prompt('Admin token required to change units and assignments:');
      if (!token) return null;
      sessionStorage.setItem(ADMIN_TOKEN_STORAGE_KEY, token);
    }

    const headers = new Headers(options.headers || {});
    headers.set('X-Slurm-Monitor-Admin-Token', token);
    const response = await fetch(apiUrl(path), { ...options, headers });
    if (response.status === 401) {
      sessionStorage.removeItem(ADMIN_TOKEN_STORAGE_KEY);
      if (attempt === 0) alert('That admin token was not accepted.');
      continue;
    }
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      alert(data.error || `Request failed (HTTP ${response.status})`);
      return null;
    }
    return response;
  }
  return null;
}

const PALETTE = [
  '#3b82f6','#10b981','#f59e0b','#ef4444','#8b5cf6',
  '#ec4899','#06b6d4','#84cc16','#f97316','#6366f1',
  '#14b8a6','#eab308','#f43f5e','#a855f7','#22c55e',
  '#0ea5e9','#d946ef','#fb923c','#4ade80','#60a5fa',
];

const state = {
  data: null,
  waitData: null,
  visibleUsers: new Set(),
  allUsers: [],
  userColors: {},
  gpuChart: null,
  cpuChart: null,
  waitChart: null,
  queueChart: null,
  viewMode: 'users',   // 'users' | 'projects' | 'units'
  _lastFetchedMode: null,
  unitsData: null,     // cached GET /api/units response
};

// ── Date helpers ─────────────────────────────────────────────

function getPresetRange(preset) {
  const now = Date.now();
  const d = new Date();
  switch (preset) {
    case 'last1':       return { from: now - 1  * 86400000, to: now };
    case 'last2':       return { from: now - 2  * 86400000, to: now };
    case 'last4':       return { from: now - 4  * 86400000, to: now };
    case 'last7':       return { from: now - 7  * 86400000, to: now };
    case 'last30':      return { from: now - 30 * 86400000, to: now };
    case 'last3months': return { from: now - 90 * 86400000, to: now };
    case 'last6months': return { from: now - 180 * 86400000, to: now };
    case 'lastyear':    return { from: now - 365 * 86400000, to: now };
    case 'thismonth':
      return { from: new Date(d.getFullYear(), d.getMonth(), 1).getTime(), to: now };
    case 'lastmonth': {
      const y = d.getMonth() === 0 ? d.getFullYear() - 1 : d.getFullYear();
      const m = d.getMonth() === 0 ? 11 : d.getMonth() - 1;
      return {
        from: new Date(y, m, 1).getTime(),
        to: new Date(y, m + 1, 1, 0, 0, 0, -1).getTime(),
      };
    }
    default: return { from: now - 7 * 86400000, to: now };
  }
}

function msToDateInput(ms) {
  const d = new Date(ms);
  const y = d.getFullYear();
  const mo = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  const h = String(d.getHours()).padStart(2, '0');
  const min = String(d.getMinutes()).padStart(2, '0');
  return `${y}-${mo}-${day}T${h}:${min}`;
}

function msToDateStr(ms) {
  if (ms == null) return '';
  const d = new Date(ms);
  return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
}

function dateStrToMs(str) {
  if (!str) return null;
  const [y, m, d] = str.split('-').map(Number);
  return new Date(y, m - 1, d, 0, 0, 0, 0).getTime();
}

function dateInputToMsStart(val) {
  if (!val) return NaN;
  const [date, time] = val.includes('T') ? val.split('T') : [val, '00:00'];
  const [y, mo, d] = date.split('-').map(Number);
  const [h, min] = time.split(':').map(Number);
  return new Date(y, mo - 1, d, h, min, 0, 0).getTime();
}

function dateInputToMsEnd(val) {
  if (!val) return NaN;
  const [date, time] = val.includes('T') ? val.split('T') : [val, '23:59'];
  const [y, mo, d] = date.split('-').map(Number);
  const [h, min] = time.split(':').map(Number);
  return new Date(y, mo - 1, d, h, min, 59, 999).getTime();
}

// ── Formatting ───────────────────────────────────────────────

function hexToRgba(hex, alpha) {
  const r = parseInt(hex.slice(1, 3), 16);
  const g = parseInt(hex.slice(3, 5), 16);
  const b = parseInt(hex.slice(5, 7), 16);
  return `rgba(${r},${g},${b},${alpha})`;
}

function fmtHours(h) {
  return Math.round(h).toLocaleString();
}

function fmtWait(h) {
  if (h < 1 / 60) return '< 1m';
  if (h < 1) return `${Math.round(h * 60)}m`;
  const hh = Math.floor(h);
  const mm = Math.round((h - hh) * 60);
  return mm > 0 ? `${hh}h ${mm}m` : `${hh}h`;
}

// ── GPU/CPU chart helpers ─────────────────────────────────────

function buildDatasets(userMap) {
  return state.allUsers
    .filter(u => state.visibleUsers.has(u))
    .map(u => {
      const color = state.userColors[u] || '#888';
      return {
        label: u,
        data: (userMap[u] || []).map((v, i) => ({
          x: state.data.buckets[i],
          y: v,
        })),
        fill: true,
        borderColor: color,
        backgroundColor: hexToRgba(color, 1.0),
        borderWidth: 1,
        pointRadius: 0,
        tension: 0.2,
        stack: 'stack',
      };
    });
}

function makeChartOptions(yMax) {
  const gridColor = '#2a2a30';
  const tickColor = '#71717a';
  const fontOpts = { family: '"JetBrains Mono","Fira Code",ui-monospace,monospace', size: 11 };
  return {
    responsive: true,
    maintainAspectRatio: false,
    animation: false,
    interaction: { mode: 'index', intersect: false },
    plugins: {
      legend: { display: false },
      tooltip: {
        backgroundColor: '#18181c',
        borderColor: '#2a2a30',
        borderWidth: 1,
        titleFont: fontOpts,
        bodyFont: fontOpts,
        footerFont: { ...fontOpts, weight: '600' },
        callbacks: {
          label(ctx) {
            if (ctx.parsed.y === 0) return null;
            return ` ${ctx.dataset.label}: ${ctx.parsed.y.toFixed(1)}`;
          },
          footer(items) {
            const total = items.reduce((s, i) => s + i.parsed.y, 0);
            return `Total: ${total.toFixed(1)}`;
          },
        },
      },
    },
    scales: {
      x: {
        type: 'time',
        time: { displayFormats: { hour: 'ha', day: 'MMM d' } },
        ticks: { major: { enabled: true }, color: tickColor, font: fontOpts, maxRotation: 0 },
        grid: { color: gridColor },
      },
      y: {
        stacked: true,
        max: yMax,
        min: 0,
        ticks: { color: tickColor, font: fontOpts },
        grid: { color: gridColor },
      },
    },
  };
}

function renderCharts() {
  const { data } = state;
  const gpuMax = data.total_gpus || data.node_count * data.gpus_per_node;
  const cpuMax = data.total_cpus || data.node_count * data.cpus_per_node;

  const gpuDatasets = buildDatasets(data.users);
  const cpuDatasets = buildDatasets(data.cpu_users);

  if (state.gpuChart) {
    state.gpuChart.data.datasets = gpuDatasets;
    state.gpuChart.data.labels = data.buckets;
    state.gpuChart.options.scales.y.max = gpuMax;
    state.gpuChart.update();
  } else {
    state.gpuChart = new Chart(
      document.getElementById('gpu-chart'),
      { type: 'line', data: { datasets: gpuDatasets }, options: makeChartOptions(gpuMax) }
    );
  }

  if (state.cpuChart) {
    state.cpuChart.data.datasets = cpuDatasets;
    state.cpuChart.data.labels = data.buckets;
    state.cpuChart.options.scales.y.max = cpuMax;
    state.cpuChart.update();
  } else {
    state.cpuChart = new Chart(
      document.getElementById('cpu-chart'),
      { type: 'line', data: { datasets: cpuDatasets }, options: makeChartOptions(cpuMax) }
    );
  }
}

function updateChartsVisibility() {
  if (state.gpuChart) {
    state.gpuChart.data.datasets = buildDatasets(state.data.users);
    state.gpuChart.update('none');
  }
  if (state.cpuChart) {
    state.cpuChart.data.datasets = buildDatasets(state.data.cpu_users);
    state.cpuChart.update('none');
  }
  if (state.waitChart) {
    state.waitChart.data.datasets = buildWaitTimeDatasets();
    state.waitChart.update('none');
  }
  if (state.queueChart) {
    state.queueChart.data.datasets = buildQueueDatasets();
    state.queueChart.update('none');
  }
  renderWaitMetrics();
}

// ── Metrics ──────────────────────────────────────────────────

function renderMetrics() {
  const { data, visibleUsers } = state;
  const bucketHours = data.bucket_size_ms / 3_600_000;

  const totalAvailGPU = (data.total_gpus || data.node_count * data.gpus_per_node) * data.buckets.length * bucketHours;
  const usedGPU = [...visibleUsers].reduce((s, u) => {
    return s + (data.users[u] || []).reduce((a, v) => a + v, 0) * bucketHours;
  }, 0);
  const gpuPct = totalAvailGPU > 0 ? (usedGPU / totalAvailGPU * 100) : 0;

  const totalAvailCPU = (data.total_cpus || data.node_count * data.cpus_per_node) * data.buckets.length * bucketHours;
  const usedCPU = [...visibleUsers].reduce((s, u) => {
    return s + (data.cpu_users[u] || []).reduce((a, v) => a + v, 0) * bucketHours;
  }, 0);
  const cpuPct = totalAvailCPU > 0 ? (usedCPU / totalAvailCPU * 100) : 0;

  document.getElementById('gpu-util-pct').textContent = `${Math.round(gpuPct)}%`;
  document.getElementById('gpu-util-hours').innerHTML =
    `<span>${fmtHours(usedGPU)}</span><span class="hours-sep">/ ${fmtHours(totalAvailGPU)}</span><span class="hours-unit">GPU-hours</span>`;

  document.getElementById('cpu-util-pct').textContent = `${Math.round(cpuPct)}%`;
  document.getElementById('cpu-util-hours').innerHTML =
    `<span>${fmtHours(usedCPU)}</span><span class="hours-sep">/ ${fmtHours(totalAvailCPU)}</span><span class="hours-unit">CPU-hours</span>`;
}

// ── Wait time chart & metrics ─────────────────────────────────

function buildQueueDatasets() {
  const { waitData, allUsers, visibleUsers, userColors } = state;
  if (!waitData) return [];
  return allUsers
    .filter(u => visibleUsers.has(u) && waitData.users[u])
    .map(u => {
      const color = userColors[u] || '#888';
      return {
        label: u,
        data: waitData.users[u].map((v, i) => ({ x: waitData.buckets[i], y: v ?? 0 })),
        backgroundColor: hexToRgba(color, 0.35),
        borderColor: color,
        borderWidth: 0,
        stack: 'wait',
        barPercentage: 1.0,
        categoryPercentage: 1.0,
      };
    });
}

function buildWaitTimeDatasets() {
  const { waitData, visibleUsers } = state;
  if (!waitData) return [];

  const buildSeries = prefix => {
    let previous = null;
    return waitData.buckets.map((bucket, i) => {
      let sum = 0;
      let count = 0;
      for (const key of visibleUsers) {
        const series = waitData.wait_time_buckets?.[key];
        if (!series) continue;
        sum += series[`${prefix}_sum`]?.[i] || 0;
        count += series[`${prefix}_count`]?.[i] || 0;
      }
      if (count > 0) previous = sum / count;
      return { x: bucket, y: previous };
    });
  };

  return [
    {
      label: 'CPU-only',
      data: buildSeries('cpu_only'),
      borderColor: '#10b981',
      backgroundColor: '#10b981',
      borderWidth: 2,
      pointRadius: 1.5,
      pointHoverRadius: 4,
      stepped: 'after',
      spanGaps: false,
    },
    {
      label: '1–7 GPUs',
      data: buildSeries('gpu_1_7'),
      borderColor: '#3b82f6',
      backgroundColor: '#3b82f6',
      borderWidth: 2,
      pointRadius: 1.5,
      pointHoverRadius: 4,
      stepped: 'after',
      spanGaps: false,
    },
    {
      label: '8+ GPUs',
      data: buildSeries('gte8'),
      borderColor: '#f59e0b',
      backgroundColor: '#f59e0b',
      borderWidth: 2,
      pointRadius: 1.5,
      pointHoverRadius: 4,
      stepped: 'after',
      spanGaps: false,
    },
  ];
}

function renderWaitChart() {
  const datasets = buildWaitTimeDatasets();
  const gridColor = '#2a2a30';
  const tickColor = '#71717a';
  const fontOpts = { family: '"JetBrains Mono","Fira Code",ui-monospace,monospace', size: 11 };

  const options = {
    responsive: true,
    maintainAspectRatio: false,
    animation: false,
    interaction: { mode: 'index', intersect: false },
    plugins: {
      legend: {
        display: true,
        position: 'top',
        align: 'end',
        labels: { color: tickColor, font: fontOpts, usePointStyle: true, boxWidth: 8 },
      },
      tooltip: {
        backgroundColor: '#18181c',
        borderColor: '#2a2a30',
        borderWidth: 1,
        titleFont: fontOpts,
        bodyFont: fontOpts,
        callbacks: {
          label(ctx) {
            if (ctx.parsed.y == null) return null;
            return ` ${ctx.dataset.label}: ${fmtWait(ctx.parsed.y)}`;
          },
        },
      },
    },
    scales: {
      x: {
        type: 'time',
        offset: false,
        time: { displayFormats: { hour: 'ha', day: 'MMM d' } },
        ticks: { major: { enabled: true }, color: tickColor, font: fontOpts, maxRotation: 0 },
        grid: { color: gridColor, offset: false },
      },
      y: {
        min: 0,
        suggestedMax: 72,
        ticks: {
          color: tickColor,
          font: fontOpts,
          stepSize: 24,
          callback: value => `${value}h`,
        },
        title: { display: true, text: 'Hours', color: tickColor, font: fontOpts },
        grid: {
          color: ctx => [24, 48, 72].includes(Number(ctx.tick.value)) ? '#52525b' : gridColor,
          lineWidth: ctx => [24, 48, 72].includes(Number(ctx.tick.value)) ? 1.5 : 1,
        },
      },
    },
  };

  if (state.waitChart) {
    state.waitChart.data.datasets = datasets;
    state.waitChart.update();
  } else {
    state.waitChart = new Chart(
      document.getElementById('wait-chart'),
      { type: 'line', data: { datasets }, options }
    );
  }
}

function renderQueueChart() {
  const datasets = buildQueueDatasets();
  const gridColor = '#2a2a30';
  const tickColor = '#71717a';
  const fontOpts = { family: '"JetBrains Mono","Fira Code",ui-monospace,monospace', size: 11 };

  const options = {
    responsive: true,
    maintainAspectRatio: false,
    animation: false,
    interaction: { mode: 'index', intersect: false },
    plugins: {
      legend: { display: false },
      tooltip: {
        backgroundColor: '#18181c',
        borderColor: '#2a2a30',
        borderWidth: 1,
        titleFont: fontOpts,
        bodyFont: fontOpts,
        footerFont: { ...fontOpts, weight: '600' },
        callbacks: {
          label(ctx) {
            if (ctx.parsed.y === 0) return null;
            return ` ${ctx.dataset.label}: ${ctx.parsed.y.toFixed(1)}`;
          },
          footer(items) {
            const total = items.reduce((s, i) => s + i.parsed.y, 0);
            return total > 0 ? `Total: ${total.toFixed(1)}` : null;
          },
        },
      },
    },
    scales: {
      x: {
        type: 'time',
        offset: false,
        time: { displayFormats: { hour: 'ha', day: 'MMM d' } },
        ticks: { major: { enabled: true }, color: tickColor, font: fontOpts, maxRotation: 0 },
        grid: { color: gridColor, offset: false },
      },
      y: {
        stacked: true,
        min: 0,
        ticks: { color: tickColor, font: fontOpts },
        grid: { color: gridColor },
      },
    },
  };

  if (state.queueChart) {
    state.queueChart.data.datasets = datasets;
    state.queueChart.update();
  } else {
    state.queueChart = new Chart(
      document.getElementById('queue-chart'),
      { type: 'bar', data: { datasets }, options }
    );
  }
}

function renderWaitMetrics() {
  const { waitData, visibleUsers } = state;
  const grid = document.getElementById('wait-stats-grid');
  if (!waitData || !grid) return;

  let sumWait = 0, countWait = 0, totalJobs = 0;
  let sumWait8Gpu = 0, countWait8Gpu = 0;
  const p90s = [];

  for (const u of visibleUsers) {
    const s = waitData.stats[u];
    if (s) {
      totalJobs += s.job_count;
      sumWait += s.avg * s.job_count;
      countWait += s.job_count;
      p90s.push(s.p90);
    }
    const large = waitData.stats_8gpu?.[u];
    if (large) {
      sumWait8Gpu += large.avg * large.job_count;
      countWait8Gpu += large.job_count;
    }
  }

  const overallAvg = countWait > 0 ? sumWait / countWait : null;
  const avg8Gpu = countWait8Gpu > 0 ? sumWait8Gpu / countWait8Gpu : null;
  const overallP90 = p90s.length ? Math.max(...p90s) : null;

  // Average queued GPUs = area under chart / window length = mean bar height
  const bucketHours = waitData.bucket_size_ms / 3_600_000;
  const windowHours = waitData.buckets.length * bucketHours;
  let gpuHrsInQueue = 0;
  for (const u of visibleUsers) {
    if (waitData.users[u]) {
      gpuHrsInQueue += waitData.users[u].reduce((s, v) => s + v, 0) * bucketHours;
    }
  }
  const avgQueued = windowHours > 0 ? gpuHrsInQueue / windowHours : null;

  const rows = [
    ['Avg wait',     overallAvg,                              false, false],
    ['Avg wait 8+ GPU', avg8Gpu,                              false, false],
    ['Worst 10%',    overallP90,                              false, false],
    ['Avg queued',   avgQueued > 0 ? avgQueued : null,        false, 'gpus'],
    ['Jobs',         totalJobs > 0 ? totalJobs : null,        false, true],
  ];

  grid.innerHTML = rows.map(([label, val, muted, type]) => {
    const display = val === null ? '—'
      : type === true   ? val.toLocaleString()
      : type === 'gpus' ? val.toFixed(1)
      : fmtWait(val);
    return `<span class="wait-stat-label">${label}</span>`
         + `<span class="wait-stat-value${muted ? ' muted' : ''}">${display}</span>`;
  }).join('');
}

// ── Sidebar ──────────────────────────────────────────────────

function fmtSidebarH(h) {
  if (h === 0) return '—';
  return Math.round(h).toLocaleString();
}

function fmtSidebarPct(p) {
  if (p === 0) return '—';
  return p < 0.1 ? '<0.1%' : p.toFixed(1) + '%';
}

function renderSidebar(filter = '') {
  const { data, allUsers, visibleUsers, userColors } = state;
  const list = document.getElementById('user-list');
  if (!data) return;

  const bucketHours = data.bucket_size_ms / 3_600_000;
  const totalAvailGPU = (data.total_gpus || data.node_count * data.gpus_per_node) * data.buckets.length * bucketHours;
  const totalAvailCPU = (data.total_cpus || data.node_count * data.cpus_per_node) * data.buckets.length * bucketHours;

  const lf = filter.toLowerCase();
  const filtered = filter ? allUsers.filter(u => u.toLowerCase().includes(lf)) : allUsers;

  const rowsHtml = filtered.map((u, i) => {
    const checked = visibleUsers.has(u);
    const color = userColors[u] || '#888';
    const gpuH = (data.users[u] || []).reduce((s, v) => s + v, 0) * bucketHours;
    const cpuH = ((data.cpu_users || {})[u] || []).reduce((s, v) => s + v, 0) * bucketHours;
    const gpuP = totalAvailGPU > 0 ? gpuH / totalAvailGPU * 100 : 0;
    const cpuP = totalAvailCPU > 0 ? cpuH / totalAvailCPU * 100 : 0;
    return `
      <tr class="user-row${checked ? ' checked' : ''}" data-idx="${i}">
        <td class="col-cb"><input type="checkbox" ${checked ? 'checked' : ''} /></td>
        <td class="col-sw"><span class="user-swatch" style="background:${color}"></span></td>
        <td class="col-name" title="${escHtml(u)}">${escHtml(u)}</td>
        <td class="col-num">${fmtSidebarH(gpuH)}</td>
        <td class="col-pct">${fmtSidebarPct(gpuP)}</td>
        <td class="col-num">${fmtSidebarH(cpuH)}</td>
        <td class="col-pct">${fmtSidebarPct(cpuP)}</td>
      </tr>`;
  }).join('');

  list.innerHTML = `
    <table class="sidebar-table">
      <thead>
        <tr>
          <th class="col-cb"></th>
          <th class="col-sw"></th>
          <th class="col-name">Name</th>
          <th class="col-num">GPU-h</th>
          <th class="col-pct">%</th>
          <th class="col-num">CPU-h</th>
          <th class="col-pct">%</th>
        </tr>
      </thead>
      <tbody>${rowsHtml}</tbody>
    </table>`;

  list.querySelectorAll('.user-row').forEach(tr => {
    const u = filtered[parseInt(tr.dataset.idx)];
    const cb = tr.querySelector('input[type="checkbox"]');
    cb.addEventListener('change', () => {
      if (cb.checked) state.visibleUsers.add(u);
      else state.visibleUsers.delete(u);
      tr.classList.toggle('checked', cb.checked);
      updateChartsVisibility();
      renderMetrics();
      updateSidebarCount();
    });
    tr.addEventListener('click', e => { if (e.target.tagName !== 'INPUT') cb.click(); });
  });

  updateSidebarCount();
}

function updateSidebarCount() {
  const vis = state.visibleUsers.size;
  const total = state.allUsers.length;
  document.getElementById('user-count').textContent = `(${vis} / ${total})`;
}

function updateSidebarLabel() {
  const labels = { users: 'Users', projects: 'Projects', units: 'Units' };
  document.getElementById('sidebar-label').textContent = labels[state.viewMode];
  const search = document.getElementById('user-search');
  search.placeholder = `Filter ${labels[state.viewMode].toLowerCase()}…`;
}

// ── Preset UI ────────────────────────────────────────────────

function applyPreset(preset) {
  const { from, to } = getPresetRange(preset);
  document.getElementById('from-date').value = msToDateInput(from);
  document.getElementById('to-date').value = msToDateInput(to);
}

// ── Fetch & render ───────────────────────────────────────────

async function fetchAndRender() {
  const overlay = document.getElementById('loading-overlay');
  overlay.classList.add('visible');

  const fromVal = document.getElementById('from-date').value;
  const toVal = document.getElementById('to-date').value;
  const from_ms = dateInputToMsStart(fromVal);
  const to_ms = dateInputToMsEnd(toVal);

  if (isNaN(from_ms) || isNaN(to_ms) || from_ms >= to_ms) {
    alert('Invalid date range. Please set a valid from/to date.');
    overlay.classList.remove('visible');
    return;
  }

  const groupBy = state.viewMode === 'users' ? 'user'
                : state.viewMode === 'projects' ? 'project' : 'unit';

  try {
    const [usageRes, waitRes] = await Promise.all([
      fetch(apiUrl(`api/usage?from=${from_ms}&to=${to_ms}&group_by=${groupBy}`)),
      fetch(apiUrl(`api/wait?from=${from_ms}&to=${to_ms}&group_by=${groupBy}`)),
    ]);
    if (!usageRes.ok) throw new Error(`HTTP ${usageRes.status}`);
    if (!waitRes.ok) throw new Error(`HTTP ${waitRes.status}`);
    [state.data, state.waitData] = await Promise.all([usageRes.json(), waitRes.json()]);
  } catch (err) {
    alert(`Failed to load usage data: ${err.message}`);
    overlay.classList.remove('visible');
    return;
  }

  const { data } = state;

  const userSet = new Set([
    ...Object.keys(data.users || {}),
    ...Object.keys(data.cpu_users || {}),
    ...Object.keys(state.waitData.users || {}),
  ]);
  // Sort by GPU usage descending
  state.allUsers = [...userSet].sort((a, b) => {
    const sumA = (data.users[a] || []).reduce((s, v) => s + v, 0);
    const sumB = (data.users[b] || []).reduce((s, v) => s + v, 0);
    return sumB - sumA;
  });

  // Assign colors: use server-provided colors for project/unit mode, PALETTE for user mode
  if (groupBy !== 'user' && data.colors && Object.keys(data.colors).length > 0) {
    state.allUsers.forEach(key => {
      state.userColors[key] = data.colors[key] || '#888888';
    });
  } else {
    state.allUsers.forEach((u, i) => {
      if (!state.userColors[u]) {
        state.userColors[u] = PALETTE[i % PALETTE.length];
      }
    });
  }

  // Reset visible selection when switching view modes
  if (state._lastFetchedMode !== state.viewMode) {
    state.visibleUsers = new Set(state.allUsers);
    state._lastFetchedMode = state.viewMode;
  } else {
    if (state.visibleUsers.size === 0) {
      state.visibleUsers = new Set(state.allUsers);
    } else {
      for (const u of [...state.visibleUsers]) {
        if (!userSet.has(u)) state.visibleUsers.delete(u);
      }
    }
  }

  updateSidebarLabel();
  renderSidebar(document.getElementById('user-search').value);
  renderCharts();
  renderMetrics();
  renderWaitChart();
  renderQueueChart();
  renderWaitMetrics();

  overlay.classList.remove('visible');
}

// ── Project/Unit editor ───────────────────────────────────────

function openEditor() {
  document.getElementById('editor-modal').hidden = false;
  loadEditorData();
}

function closeEditor() {
  document.getElementById('editor-modal').hidden = true;
}

async function loadEditorData() {
  try {
    const [unitsRes, usersRes, unassignedRes] = await Promise.all([
      fetch(apiUrl('api/units')),
      fetch(apiUrl('api/users')),
      fetch(apiUrl('api/unassigned-jobs')),
    ]);
    state.unitsData = await unitsRes.json();
    const knownUsers = await usersRes.json();
    const unassigned = await unassignedRes.json();

    // Populate datalist for assignment user autocomplete
    const dl = document.getElementById('known-users-list');
    dl.innerHTML = knownUsers.map(u => `<option value="${escHtml(u)}"></option>`).join('');

    renderUnitsTab();
    renderProjectsTab();
    renderUnassignedTab(unassigned);
  } catch (e) {
    console.error('Failed to load editor data', e);
  }
}

function escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// ── Units tab ─────────────────────────────────────────────────

function renderUnitsTab() {
  const { units } = state.unitsData;
  const list = document.getElementById('units-list');
  list.innerHTML = '';

  if (units.length === 0) {
    list.innerHTML = '<div class="editor-empty">No units yet.</div>';
  }

  units.forEach(unit => {
    const row = document.createElement('div');
    row.className = 'editor-row';
    row.dataset.id = unit.id;
    row.innerHTML = `
      <span class="editor-color-dot" style="background:${unit.color || '#888'}"></span>
      <span class="editor-row-title">${escHtml(unit.title)}</span>
      <input type="text" class="editor-inline-input" value="${escHtml(unit.title)}" style="display:none" />
      <input type="color" class="editor-inline-color" value="${unit.color || '#3b82f6'}" style="display:none" title="Pick color" />
      <div class="editor-row-actions">
        <button class="editor-btn edit-unit-btn">Edit</button>
        <button class="editor-btn save-unit-btn" style="display:none">Save</button>
        <button class="editor-btn cancel-unit-btn" style="display:none">Cancel</button>
        <button class="editor-btn danger-btn delete-unit-btn">Delete</button>
      </div>
    `;

    // Edit / save / cancel
    const dot = row.querySelector('.editor-color-dot');
    const titleSpan = row.querySelector('.editor-row-title');
    const titleInput = row.querySelector('.editor-inline-input');
    const colorInput = row.querySelector('.editor-inline-color');
    const editBtn = row.querySelector('.edit-unit-btn');
    const saveBtn = row.querySelector('.save-unit-btn');
    const cancelBtn = row.querySelector('.cancel-unit-btn');

    editBtn.addEventListener('click', () => {
      titleSpan.style.display = 'none';
      dot.style.display = 'none';
      titleInput.style.display = '';
      colorInput.style.display = '';
      editBtn.style.display = 'none';
      saveBtn.style.display = '';
      cancelBtn.style.display = '';
      titleInput.focus();
    });

    cancelBtn.addEventListener('click', () => {
      titleSpan.style.display = '';
      dot.style.display = '';
      titleInput.style.display = 'none';
      colorInput.style.display = 'none';
      editBtn.style.display = '';
      saveBtn.style.display = 'none';
      cancelBtn.style.display = 'none';
    });

    saveBtn.addEventListener('click', async () => {
      const newTitle = titleInput.value.trim();
      const newColor = colorInput.value;
      if (!newTitle) return;
      const response = await adminFetch(`api/units/${unit.id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title: newTitle, color: newColor }),
      });
      if (!response) return;
      await loadEditorData();
    });

    row.querySelector('.delete-unit-btn').addEventListener('click', async () => {
      if (!confirm(`Delete unit "${unit.title}"? Its projects will become unaffiliated.`)) return;
      const response = await adminFetch(`api/units/${unit.id}`, { method: 'DELETE' });
      if (!response) return;
      await loadEditorData();
    });

    list.appendChild(row);
  });

  // Set color picker to the next unused palette color
  const usedColors = new Set((state.unitsData.units || []).map(u => u.color).filter(Boolean));
  const nextColor = PALETTE.find(c => !usedColors.has(c)) || PALETTE[0];
  document.getElementById('new-unit-color').value = nextColor;

  // Add unit
  document.getElementById('add-unit-btn').onclick = async () => {
    const titleEl = document.getElementById('new-unit-title');
    const colorEl = document.getElementById('new-unit-color');
    const title = titleEl.value.trim();
    if (!title) { titleEl.focus(); return; }
    const response = await adminFetch('api/units', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title, color: colorEl.value }),
    });
    if (!response) return;
    titleEl.value = '';
    await loadEditorData();  // re-renders tab, which will advance the color picker again
  };
}

// ── Projects tab ──────────────────────────────────────────────

function renderProjectsTab() {
  const { units, unaffiliated } = state.unitsData;
  const list = document.getElementById('projects-list');
  list.innerHTML = '';

  // Update unit dropdown in "add project" form
  const unitSelect = document.getElementById('new-project-unit');
  unitSelect.innerHTML = '<option value="">— No unit —</option>'
    + units.map(u => `<option value="${u.id}">${escHtml(u.title)}</option>`).join('');

  // Build a flat list of all projects, grouped by unit then unaffiliated
  const allProjectGroups = [
    ...units.map(u => ({ label: u.title, color: u.color, projects: u.projects })),
    ...(unaffiliated.length > 0 ? [{ label: 'Unaffiliated', color: null, projects: unaffiliated }] : []),
  ];

  if (allProjectGroups.every(g => g.projects.length === 0)) {
    list.innerHTML = '<div class="editor-empty">No projects yet.</div>';
  }

  allProjectGroups.forEach(group => {
    if (group.projects.length === 0) return;

    const header = document.createElement('div');
    header.className = 'editor-group-header';
    header.innerHTML = group.color
      ? `<span class="editor-color-dot" style="background:${group.color}"></span>${escHtml(group.label)}`
      : `<span class="editor-group-label-plain">${escHtml(group.label)}</span>`;
    list.appendChild(header);

    group.projects.forEach(proj => {
      const row = document.createElement('div');
      row.className = 'editor-row project-row';
      row.dataset.id = proj.id;

      const aCount = proj.assignments.length;
      row.innerHTML = `
        <span class="editor-row-title">${escHtml(proj.title)}</span>
        <input type="text" class="editor-inline-input" value="${escHtml(proj.title)}" style="display:none" />
        <select class="editor-inline-select editor-unit-select" style="display:none">
          <option value="">— No unit —</option>
          ${units.map(u => `<option value="${u.id}"${u.id === proj.unit_id ? ' selected' : ''}>${escHtml(u.title)}</option>`).join('')}
        </select>
        <div class="editor-row-actions">
          <button class="editor-btn toggle-assign-btn">Assignments (${aCount})</button>
          <button class="editor-btn edit-proj-btn">Edit</button>
          <button class="editor-btn save-proj-btn" style="display:none">Save</button>
          <button class="editor-btn cancel-proj-btn" style="display:none">Cancel</button>
          <button class="editor-btn danger-btn delete-proj-btn">Delete</button>
        </div>
        <div class="assignments-panel" hidden></div>
      `;

      const titleSpan = row.querySelector('.editor-row-title');
      const titleInput = row.querySelector('.editor-inline-input');
      const unitSel = row.querySelector('.editor-unit-select');
      const editBtn = row.querySelector('.edit-proj-btn');
      const saveBtn = row.querySelector('.save-proj-btn');
      const cancelBtn = row.querySelector('.cancel-proj-btn');
      const assignPanel = row.querySelector('.assignments-panel');
      const toggleBtn = row.querySelector('.toggle-assign-btn');

      editBtn.addEventListener('click', () => {
        titleSpan.style.display = 'none';
        titleInput.style.display = '';
        unitSel.style.display = '';
        editBtn.style.display = 'none';
        saveBtn.style.display = '';
        cancelBtn.style.display = '';
        titleInput.focus();
      });

      cancelBtn.addEventListener('click', () => {
        titleSpan.style.display = '';
        titleInput.style.display = 'none';
        unitSel.style.display = 'none';
        editBtn.style.display = '';
        saveBtn.style.display = 'none';
        cancelBtn.style.display = 'none';
      });

      saveBtn.addEventListener('click', async () => {
        const newTitle = titleInput.value.trim();
        if (!newTitle) return;
        const newUnitId = unitSel.value ? parseInt(unitSel.value) : null;
        const response = await adminFetch(`api/projects/${proj.id}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ title: newTitle, unit_id: newUnitId }),
        });
        if (!response) return;
        await loadEditorData();
      });

      row.querySelector('.delete-proj-btn').addEventListener('click', async () => {
        if (!confirm(`Delete project "${proj.title}" and all its assignments?`)) return;
        const response = await adminFetch(`api/projects/${proj.id}`, { method: 'DELETE' });
        if (!response) return;
        await loadEditorData();
      });

      toggleBtn.addEventListener('click', () => {
        assignPanel.hidden = !assignPanel.hidden;
        if (!assignPanel.hidden) renderAssignments(assignPanel, proj);
      });

      list.appendChild(row);
    });
  });

  // Add project
  document.getElementById('add-project-btn').onclick = async () => {
    const titleEl = document.getElementById('new-project-title');
    const unitEl = document.getElementById('new-project-unit');
    const title = titleEl.value.trim();
    if (!title) { titleEl.focus(); return; }
    const unit_id = unitEl.value ? parseInt(unitEl.value) : null;
    const response = await adminFetch('api/projects', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title, unit_id }),
    });
    if (!response) return;
    titleEl.value = '';
    await loadEditorData();
  };
}

function renderAssignments(panel, proj) {
  panel.innerHTML = '';

  proj.assignments.forEach(a => {
    const endLabel = a.end_time_ms == null ? '∞' : msToDateStr(a.end_time_ms);
    const row = document.createElement('div');
    row.className = 'assignment-row';
    row.innerHTML = `
      <span class="assign-user">${escHtml(a.user)}</span>
      <span class="assign-dates">${msToDateStr(a.start_time_ms)} → ${endLabel}</span>
      <div class="assignment-edit-form" hidden>
        <input type="text" class="editor-input assign-edit-user" value="${escHtml(a.user)}" list="known-users-list" />
        <input type="date" class="editor-input assign-edit-start" value="${msToDateStr(a.start_time_ms)}" />
        <label class="open-ended-label">
          <input type="checkbox" class="assign-open-ended" ${a.end_time_ms == null ? 'checked' : ''} />
          Open-ended
        </label>
        <input type="date" class="editor-input assign-edit-end" value="${msToDateStr(a.end_time_ms)}" ${a.end_time_ms == null ? 'disabled' : ''} />
      </div>
      <div class="assign-actions">
        <button class="editor-btn edit-assign-btn">Edit</button>
        <button class="editor-btn save-assign-btn" style="display:none">Save</button>
        <button class="editor-btn cancel-assign-btn" style="display:none">Cancel</button>
        <button class="editor-btn danger-btn delete-assign-btn">&#10005;</button>
      </div>
    `;

    const display = row.querySelector('.assign-user');
    const dates = row.querySelector('.assign-dates');
    const editForm = row.querySelector('.assignment-edit-form');
    const editBtn = row.querySelector('.edit-assign-btn');
    const saveBtn = row.querySelector('.save-assign-btn');
    const cancelBtn = row.querySelector('.cancel-assign-btn');
    const openEnded = row.querySelector('.assign-open-ended');
    const endInput = row.querySelector('.assign-edit-end');

    openEnded.addEventListener('change', () => {
      endInput.disabled = openEnded.checked;
      if (openEnded.checked) endInput.value = '';
    });

    editBtn.addEventListener('click', () => {
      display.style.display = 'none';
      dates.style.display = 'none';
      editForm.hidden = false;
      editBtn.style.display = 'none';
      saveBtn.style.display = '';
      cancelBtn.style.display = '';
    });

    cancelBtn.addEventListener('click', () => {
      display.style.display = '';
      dates.style.display = '';
      editForm.hidden = true;
      editBtn.style.display = '';
      saveBtn.style.display = 'none';
      cancelBtn.style.display = 'none';
    });

    saveBtn.addEventListener('click', async () => {
      const user = row.querySelector('.assign-edit-user').value.trim();
      const startStr = row.querySelector('.assign-edit-start').value;
      const endStr = openEnded.checked ? null : endInput.value;
      const start_time_ms = dateStrToMs(startStr);
      const end_time_ms = endStr ? dateStrToMs(endStr) : null;
      if (!user || !start_time_ms) return;
      const response = await adminFetch(`api/projects/${proj.id}/assignments/${a.id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ user, start_time_ms, end_time_ms }),
      });
      if (!response) return;
      await loadEditorData();
    });

    row.querySelector('.delete-assign-btn').addEventListener('click', async () => {
      const response = await adminFetch(`api/projects/${proj.id}/assignments/${a.id}`, { method: 'DELETE' });
      if (!response) return;
      await loadEditorData();
    });

    panel.appendChild(row);
  });

  // Add assignment form
  const addRow = document.createElement('div');
  addRow.className = 'assignment-add-row';
  addRow.innerHTML = `
    <input type="text" class="editor-input" id="new-assign-user-${proj.id}" placeholder="Username…" list="known-users-list" />
    <input type="date" class="editor-input" id="new-assign-start-${proj.id}" value="2026-01-01" />
    <label class="open-ended-label">
      <input type="checkbox" id="new-assign-open-${proj.id}" checked /> Open-ended
    </label>
    <input type="date" class="editor-input" id="new-assign-end-${proj.id}" disabled />
    <button class="editor-add-btn" id="new-assign-btn-${proj.id}">+ Add</button>
  `;

  addRow.querySelector(`#new-assign-open-${proj.id}`).addEventListener('change', e => {
    addRow.querySelector(`#new-assign-end-${proj.id}`).disabled = e.target.checked;
  });

  addRow.querySelector(`#new-assign-btn-${proj.id}`).addEventListener('click', async () => {
    const user = addRow.querySelector(`#new-assign-user-${proj.id}`).value.trim();
    const startStr = addRow.querySelector(`#new-assign-start-${proj.id}`).value;
    const openEnded = addRow.querySelector(`#new-assign-open-${proj.id}`).checked;
    const endStr = openEnded ? null : addRow.querySelector(`#new-assign-end-${proj.id}`).value;
    const start_time_ms = dateStrToMs(startStr);
    const end_time_ms = endStr ? dateStrToMs(endStr) : null;
    if (!user || !start_time_ms) {
      addRow.querySelector(`#new-assign-user-${proj.id}`).focus();
      return;
    }
    const response = await adminFetch(`api/projects/${proj.id}/assignments`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ user, start_time_ms, end_time_ms }),
    });
    if (!response) return;
    await loadEditorData();
  });

  panel.appendChild(addRow);
}

// ── Unassigned tab ────────────────────────────────────────────

function renderUnassignedTab(rows) {
  const list = document.getElementById('unassigned-list');
  const badge = document.getElementById('unassigned-badge');

  badge.textContent = rows.length > 0 ? rows.length : '';

  if (rows.length === 0) {
    list.innerHTML = '<div class="editor-empty">All jobs are assigned to a project.</div>';
    return;
  }

  const { units, unaffiliated } = state.unitsData;
  const allProjects = [...units.flatMap(u => u.projects), ...unaffiliated];

  const projOptions = allProjects.length
    ? allProjects.map(p => `<option value="${p.id}">${escHtml(p.title)}</option>`).join('')
    : '<option value="" disabled>— no projects —</option>';

  const rowsHtml = rows.map((r, i) => `
    <tr data-idx="${i}">
      <td class="user-col">${escHtml(r.user || '')}</td>
      <td class="num-col">${r.job_count.toLocaleString()}</td>
      <td class="num-col">${r.gpu_hours.toLocaleString()}</td>
      <td class="num-col">${msToDateStr(r.earliest_ms)}</td>
      <td class="assign-quick-cell">
        <select class="editor-select quick-proj-select">
          <option value="">— pick project —</option>
          ${projOptions}
        </select>
        <button class="editor-add-btn quick-assign-btn" disabled>Assign</button>
      </td>
    </tr>
  `).join('');

  // Set innerHTML on the container div (not on a table element) to avoid
  // browser quirks with innerHTML on table elements.
  list.innerHTML = `
    <table class="unassigned-table">
      <thead>
        <tr>
          <th>User</th>
          <th class="num-col">Jobs</th>
          <th class="num-col">GPU-hours</th>
          <th>Earliest job</th>
          <th>Quick assign</th>
        </tr>
      </thead>
      <tbody>${rowsHtml}</tbody>
    </table>
  `;

  list.querySelectorAll('tr[data-idx]').forEach(tr => {
    const r = rows[parseInt(tr.dataset.idx)];
    const sel = tr.querySelector('.quick-proj-select');
    const btn = tr.querySelector('.quick-assign-btn');

    sel.addEventListener('change', () => { btn.disabled = !sel.value; });

    btn.addEventListener('click', async () => {
      const response = await adminFetch(`api/projects/${parseInt(sel.value)}/assignments`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ user: r.user, start_time_ms: r.earliest_ms, end_time_ms: null }),
      });
      if (!response) return;
      await loadEditorData();
    });
  });
}

// ── Bootstrap ────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
  applyPreset('last7');

  document.querySelectorAll('.preset-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.preset === 'last7');
  });

  document.getElementById('apply-btn').addEventListener('click', fetchAndRender);

  document.querySelectorAll('.preset-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.preset-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      applyPreset(btn.dataset.preset);
      fetchAndRender();
    });
  });

  // View toggle
  document.querySelectorAll('.view-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.view-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      state.viewMode = btn.dataset.view;
      fetchAndRender();
    });
  });

  document.getElementById('user-search').addEventListener('input', e => {
    renderSidebar(e.target.value);
  });

  document.getElementById('select-matching').addEventListener('click', () => {
    const filter = document.getElementById('user-search').value.toLowerCase();
    const matching = filter
      ? state.allUsers.filter(u => u.toLowerCase().includes(filter))
      : state.allUsers;
    matching.forEach(u => state.visibleUsers.add(u));
    renderSidebar(document.getElementById('user-search').value);
    updateChartsVisibility();
    renderMetrics();
  });

  document.getElementById('deselect-matching').addEventListener('click', () => {
    const filter = document.getElementById('user-search').value.toLowerCase();
    const matching = filter
      ? state.allUsers.filter(u => u.toLowerCase().includes(filter))
      : state.allUsers;
    matching.forEach(u => state.visibleUsers.delete(u));
    renderSidebar(document.getElementById('user-search').value);
    updateChartsVisibility();
    renderMetrics();
  });

  // Editor modal
  document.getElementById('open-editor').addEventListener('click', openEditor);
  document.getElementById('close-editor').addEventListener('click', closeEditor);
  document.getElementById('editor-modal').addEventListener('click', e => {
    if (e.target === e.currentTarget) closeEditor();
  });

  // Editor tabs
  document.querySelectorAll('.modal-tab').forEach(tab => {
    tab.addEventListener('click', () => {
      document.querySelectorAll('.modal-tab').forEach(t => t.classList.remove('active'));
      tab.classList.add('active');
      const target = tab.dataset.tab;
      document.getElementById('tab-units').hidden = target !== 'units';
      document.getElementById('tab-projects').hidden = target !== 'projects';
      document.getElementById('tab-unassigned').hidden = target !== 'unassigned';
    });
  });

  fetchAndRender();
});
