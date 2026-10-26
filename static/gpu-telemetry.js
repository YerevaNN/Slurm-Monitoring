(function (root) {
  "use strict";
  const metrics = [["gpu", "GPU activity"], ["vram", "VRAM occupancy"], ["memory", "Memory activity"]];
  const SERIES_COLORS = ["#38bdf8", "#a78bfa", "#fbbf24"];
  const valid = (v) => typeof v === "number" && Number.isFinite(v);
  const pct = (v) => valid(v) ? v.toFixed(1) + "%" : "—";
  function color(value) {
    if (!valid(value)) return "#64748b";
    // One shared low-to-high scale for every metric and the timeline bars.
    return value < 20 ? "#b91c1c" : value < 40 ? "#c2410c" : value < 60 ? "#a16207" : value < 80 ? "#4d7c0f" : "#15803d";
  }
  function escape(value) {
    return String(value ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"}[c]));
  }
  function badge(value, key) {
    return `<span class="gpu-metric-badge" style="background:${color(value, key)}">${pct(value)}</span>`;
  }
  function statistic(s) {
    return s && valid(s.mean) ? pct(s.mean) + " ± " + s.std.toFixed(1) + " pp" : "—";
  }
  function tooltip(job) {
    const data = job.gpu_telemetry;
    if (!data) return "";
    const running = job.state === "RUNNING";
    return metrics.map(([key, label]) => `<tr><th>${label}${running ? " · now" : " · mean"}</th><td>${badge(running ? data.current[key] : data.lifetime[key].mean, key)} · mean ${statistic(data.lifetime[key])}</td></tr>`).join("");
  }
  function graph(data) {
    if (!data.points.length) return '<p>15-second history has expired or has not been collected. Lifetime statistics remain available.</p>';
    const first = data.points[0].t, last = data.points[data.points.length - 1].t;
    const x = (t) => 48 + (t - first) / Math.max(1, last - first) * 900;
    const y = (v) => 150 - v * 1.3;
    let svg = '<svg viewBox="0 0 980 195" role="img" aria-label="GPU activity, VRAM occupancy and memory activity over time">';
    // Value ranges belong to the background; each line keeps its series identity.
    for (let low = 0; low < 100; low += 20) {
      svg += `<rect x="48" y="${y(low + 20)}" width="902" height="26" fill="${color(low)}" opacity=".07"/>`;
    }
    [0, 50, 100].forEach((v) => { svg += `<line x1="48" x2="950" y1="${y(v)}" y2="${y(v)}" stroke="#64748b" opacity=".3"/><text x="5" y="${y(v) + 4}">${v}%</text>`; });
    metrics.forEach(([key], index) => {
      let path = "", previous = null;
      data.points.forEach((p) => {
        if (!valid(p[key])) { previous = null; return; }
        const connected = previous !== null && p.t - previous <= Math.max(45000, data.bucket_ms * 2);
        path += `${connected ? "L" : "M"}${x(p.t).toFixed(2)},${y(p[key]).toFixed(2)} `;
        previous = p.t;
      });
      svg += `<path d="${path}" fill="none" stroke="${SERIES_COLORS[index]}" stroke-width="2"/>`;
      // Individual points remain visible before enough samples exist for a line.
      data.points.forEach((p) => { if (valid(p[key])) svg += `<circle cx="${x(p.t).toFixed(2)}" cy="${y(p[key]).toFixed(2)}" r="1.5" fill="${SERIES_COLORS[index]}"/>`; });
    });
    svg += '<line class="gpu-chart-cursor" x1="48" x2="48" y1="20" y2="150" stroke="#cbd5e1" stroke-width="1" style="display:none" pointer-events="none"/>';
    metrics.forEach(([key], i) => { svg += `<circle class="gpu-chart-cursor-point" data-metric="${key}" r="3.5" fill="${SERIES_COLORS[i]}" stroke="#fff" stroke-width="1" style="display:none" pointer-events="none"/>`; });
    svg += `<text x="48" y="185">${escape(new Date(first).toLocaleString())}</text><text x="948" y="185" text-anchor="end">${escape(new Date(last).toLocaleString())}</text></svg>`;
    const legend = metrics.map(([, label], i) => `<span style="--series-color:${SERIES_COLORS[i]}">${label}</span>`).join("");
    return `<p class="gpu-graph-legend">${legend}</p>${svg}<p>Faint background bands mark percentage ranges from red to green. Job average across sampled GPUs${data.bucket_ms > 15000 ? "; chart points grouped for display" : "; 15-second readings"}. Gaps indicate missing samples.</p>`;
  }
  function deviceTable(data) {
    return '<div class="gpu-table-scroll"><table class="gpu-device-table"><thead><tr><th>GPU</th><th>GPU mean ± std</th><th>VRAM mean ± std</th><th>Memory activity mean ± std</th><th>VRAM used / total · peak</th><th>Samples</th></tr></thead><tbody>' + data.devices.map((d) => {
      const current = d.current;
      const peak = d.lifetime.used_mib.peak;
      return `<tr><td title="${escape(d.uuid)}">${escape(d.node)} · GPU ${escape(d.minor)}</td>${metrics.map(([key]) => `<td>${statistic(d.lifetime[key])}${current ? '<br>now ' + pct(current[key]) : ''}</td>`).join("")}<td>${current && valid(current.used_mib) ? (current.used_mib / 1024).toFixed(1) + " / " : ""}${valid(d.total_mib) ? (d.total_mib / 1024).toFixed(1) + " GiB" : "—"}<br>peak ${valid(peak) ? (peak / 1024).toFixed(1) + " GiB" : "—"}</td><td>${d.lifetime.gpu.n}</td></tr>`;
    }).join("") + '</tbody></table></div>';
  }
  let selected = null;
  let dialog = null;
  let chartRequest = 0;
  function hideChartHover(chart) {
    if (chart) {
      chart._gpuPointer = null;
      chart.querySelectorAll('.gpu-chart-cursor, .gpu-chart-cursor-point').forEach(el => { el.style.display = "none"; });
    }
    const tip = dialog?.querySelector(".gpu-chart-tooltip");
    if (tip) tip.hidden = true;
  }
  function showChartHover(chart, pointer) {
    const svg = chart.querySelector("svg"), data = chart._gpuTrend;
    if (!svg || !data?.points.length) return hideChartHover(chart);
    const bounds = svg.getBoundingClientRect();
    if (!bounds.width || !bounds.height) return hideChartHover(chart);
    const px = (pointer.x - bounds.left) * 980 / bounds.width;
    const py = (pointer.y - bounds.top) * 195 / bounds.height;
    if (px < 48 || px > 948 || py < 20 || py > 150) return hideChartHover(chart);
    const points = data.points, first = points[0].t, last = points[points.length - 1].t;
    const time = first + (px - 48) / 900 * (last - first);
    let low = 0, high = points.length - 1;
    while (low < high) {
      const middle = Math.floor((low + high) / 2);
      if (points[middle].t < time) low = middle + 1;
      else high = middle;
    }
    const point = low > 0 && Math.abs(points[low - 1].t - time) < Math.abs(points[low].t - time) ? points[low - 1] : points[low];
    // Do not invent a reading in the middle of an extended collection gap.
    if (Math.abs(point.t - time) > Math.max(45000, data.bucket_ms * 2)) return hideChartHover(chart);
    chart._gpuPointer = pointer;
    const at = 48 + (point.t - first) / Math.max(1, last - first) * 900;
    const cursor = svg.querySelector(".gpu-chart-cursor");
    cursor.setAttribute("x1", at); cursor.setAttribute("x2", at); cursor.style.display = "block";
    svg.querySelectorAll(".gpu-chart-cursor-point").forEach(marker => {
      const value = point[marker.dataset.metric];
      marker.style.display = valid(value) ? "block" : "none";
      if (valid(value)) { marker.setAttribute("cx", at); marker.setAttribute("cy", 150 - value * 1.3); }
    });
    const tip = dialog.querySelector(".gpu-chart-tooltip");
    const date = new Date(point.t).toLocaleString(undefined, {year:"numeric", month:"short", day:"2-digit", hour:"2-digit", minute:"2-digit", second:"2-digit", timeZoneName:"short"});
    const signature = JSON.stringify([point.t, ...metrics.map(([key]) => point[key]), data.bucket_ms]);
    if (tip._gpuSignature !== signature) {
      tip.innerHTML = `<strong>${escape(date)}</strong>${metrics.map(([key, label], i) => `<div><span style="color:${SERIES_COLORS[i]}">${label}</span><b>${pct(point[key])}</b></div>`).join("")}<small>${data.bucket_ms > 15000 ? "Chart-bin averages" : "Average across sampled GPUs"}</small>`;
      tip._gpuSignature = signature;
    }
    tip.dataset.pointTime = point.t;
    tip.hidden = false;
    const box = tip.getBoundingClientRect(), modal = dialog.getBoundingClientRect();
    const left = Math.max(modal.left + 8, Math.min(pointer.x + 14, modal.right - box.width - 8));
    const top = Math.max(modal.top + 8, pointer.y + 14 + box.height <= modal.bottom - 8 ? pointer.y + 14 : pointer.y - box.height - 14);
    tip.style.left = left + "px"; tip.style.top = top + "px";
  }
  function bindChartHover(chart) {
    if (chart._gpuHoverBound) return;
    chart._gpuHoverBound = true;
    chart.addEventListener("pointermove", event => {
      if (!event.target.closest("svg")) return hideChartHover(chart);
      showChartHover(chart, {x:event.clientX, y:event.clientY});
    });
    chart.addEventListener("pointerleave", () => hideChartHover(chart));
  }
  function averageActivity(job) {
    const value = job.gpu_telemetry?.lifetime?.gpu?.mean;
    return valid(value) ? value : null;
  }
  function jobColor(job) {
    return color(averageActivity(job), "gpu");
  }
  function ensureDialog() {
    if (dialog) return dialog;
    dialog = document.createElement("dialog");
    dialog.className = "gpu-job-dialog";
    dialog.setAttribute("aria-labelledby", "gpu-job-dialog-title");
    dialog.innerHTML = '<div class="gpu-dialog-heading"><h2 id="gpu-job-dialog-title"></h2><button type="button" class="gpu-dialog-close" aria-label="Close job details">Close ×</button></div><div class="gpu-dialog-content"></div><div class="gpu-chart-tooltip" role="tooltip" hidden></div>';
    dialog.querySelector(".gpu-dialog-close").addEventListener("click", () => dialog.close());
    dialog.addEventListener("close", () => {
      selected = null;
      chartRequest++;
      hideChartHover(dialog.querySelector(".gpu-job-chart"));
      document.dispatchEvent(new Event("gpu-details-closed"));
    });
    dialog.addEventListener("click", (event) => { if (event.target === dialog) dialog.close(); });
    dialog.addEventListener("scroll", () => hideChartHover(dialog.querySelector(".gpu-job-chart")), true);
    document.body.appendChild(dialog);
    return dialog;
  }
  function patchMarkup(container, markup) {
    // Keep live nodes, focus, selection and scroll containers intact on refresh.
    const template = document.createElement("template");
    template.innerHTML = markup;
    function patch(target, source, root = false) {
      if (target.nodeType === 3) {
        if (target.nodeValue !== source.nodeValue) target.nodeValue = source.nodeValue;
        return;
      }
      if (target.nodeType === 1 && !root) {
        for (const attribute of Array.from(target.attributes)) {
          if (!source.hasAttribute(attribute.name)) target.removeAttribute(attribute.name);
        }
        for (const attribute of Array.from(source.attributes)) {
          if (target.getAttribute(attribute.name) !== attribute.value) target.setAttribute(attribute.name, attribute.value);
        }
      }
      const wanted = Array.from(source.childNodes);
      wanted.forEach((child, i) => {
        const existing = target.childNodes[i];
        if (!existing) target.appendChild(child.cloneNode(true));
        else if (existing.nodeType !== child.nodeType || existing.nodeName !== child.nodeName) existing.replaceWith(child.cloneNode(true));
        else patch(existing, child);
      });
      while (target.childNodes.length > wanted.length) target.lastChild.remove();
    }
    patch(container, template.content, true);
  }
  async function updateDetails(job, url) {
    const modal = ensureDialog();
    const d = job.gpu_telemetry;
    const title = `#${job.job_id} · ${job.job_name || "Job details"}`;
    if (modal.querySelector("h2").textContent !== title) modal.querySelector("h2").textContent = title;
    const body = modal.querySelector(".gpu-dialog-content");
    const identity = `<p>${escape(job.user)} · ${escape(job.state)} · ${escape(job.partition || "")}</p>`;
    if (!d) {
      patchMarkup(body, identity + '<p>No GPU measurements available for this job. Measurements begin when collection observes a running allocation.</p>');
      body.dataset.job = "";
      return;
    }
    const running = job.state === "RUNNING";
    const cells = metrics.map(([key, label]) => `<div class="gpu-detail-metric"><strong>${label}</strong>${badge(running ? d.current[key] : d.lifetime[key].mean, key)}<small>Observed mean ${statistic(d.lifetime[key])}</small></div>`).join("");
    const coverage = valid(d.coverage_pct) && d.coverage_pct > 0 && d.coverage_pct < 0.1 ? "&lt;0.1%" : pct(d.coverage_pct);
    const jobKey = job.job_id + ":" + job.submit_time;
    if (body.dataset.job !== jobKey) {
      body.innerHTML = '<div class="gpu-dialog-summary"></div><div class="gpu-job-chart">Loading chart…</div><p>Mean ± population std describes observed GPU samples. Current averages cover fresh GPUs only. Raw readings remain while running and for 24 hours after completion; lifetime summaries remain. Memory activity measures time reading/writing VRAM, not theoretical bandwidth saturation.</p>';
      body.dataset.job = jobKey;
    }
    patchMarkup(body.querySelector(".gpu-dialog-summary"), identity + `<div class="gpu-detail-metrics">${cells}</div><p>Bar color: observed average GPU activity ${pct(averageActivity(job))}. ${running ? `${d.fresh_gpus}/${d.expected_gpus} GPUs have fresh readings.` : ""} Observed since ${escape(new Date(d.first_ms).toLocaleString())}; last reading ${escape(new Date(d.last_ms).toLocaleString())}. Approximate sampled coverage of this run: ${coverage}.</p>${deviceTable(d)}`);
    const chart = body.querySelector(".gpu-job-chart");
    bindChartHover(chart);
    const requestId = ++chartRequest;
    try {
      const response = await fetch(url("api/job/" + encodeURIComponent(job.job_id) + "/gpu?submit=" + job.submit_time));
      if (!response.ok) throw new Error("unavailable");
      const data = await response.json();
      if (requestId === chartRequest && modal.open) {
        patchMarkup(chart, graph(data));
        chart.dataset.loaded = "true";
        chart._gpuTrend = data;
        if (chart._gpuPointer) showChartHover(chart, chart._gpuPointer);
      }
    } catch (_) {
      if (requestId === chartRequest && !chart.dataset.loaded) chart.textContent = "Chart unavailable; it will retry on the next dashboard refresh.";
    }
  }
  function open(job, url) {
    const modal = ensureDialog();
    selected = {job_id: job.job_id, submit_time: job.submit_time};
    document.getElementById("job-tooltip")?.classList.remove("visible");
    if (!modal.open) modal.showModal();
    updateDetails(job, url);
  }
  function render(jobs, url) {
    if (!selected || !dialog?.open) return;
    const job = jobs.find(j => j.job_id === selected.job_id && j.submit_time === selected.submit_time);
    if (job) updateDetails(job, url);
  }
  const api = {render, open, tooltip, color, statistic, graph, averageActivity, jobColor, isOpen: () => Boolean(dialog?.open)};
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.GpuTelemetry = api;
})(typeof window !== "undefined" ? window : globalThis);
