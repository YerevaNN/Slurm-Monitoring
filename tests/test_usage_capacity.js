'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const elements = new Map();
const context = vm.createContext({
  document: {
    addEventListener() {},
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, {});
      return elements.get(id);
    },
  },
});
vm.runInContext(fs.readFileSync(path.join(__dirname, '../static/usage.js'), 'utf8'), context);
const run = code => vm.runInContext(code, context);
run(`state.data = {
  buckets: [0, 600000], bucket_size_ms: 600000, bucket_durations_ms: [600000, 300000],
  users: {alice: [12, 12]}, cpu_users: {alice: [192, 192]},
  installed_gpu_hours: 11/3, installed_cpu_hours: 176/3,
  capacity_coverage_complete: true,
  capacity_points: [
    {timestamp_ms: 0, gpus: 12, cpus: 192, drained_gpus: 0, drained_cpus: 0},
    {timestamp_ms: 300000, gpus: 16, cpus: 256, drained_gpus: 4, drained_cpus: 64},
    {timestamp_ms: 900000, gpus: 16, cpus: 256, drained_gpus: 4, drained_cpus: 64}
  ],
  capacity_buckets: {gpus: [14, 16], cpus: [224, 256]}
};
state.allUsers = ['alice']; state.visibleUsers = new Set(['alice']);
state.userColors = {alice: '#123456'};`);
assert.equal(run('resourceHours(state.data.users.alice)'), 3);
run('renderMetrics()');
assert.equal(elements.get('gpu-util-pct').textContent, '82%');
assert.equal(run('buildCapacityDatasets("gpus").length'), 2);
assert.equal(run('buildCapacityDatasets("gpus")[0].stepped'), 'before');
assert.equal(run('chartMaximum("gpus", state.data.users)'), 16);
const footer = run(`makeChartOptions(16, 'gpus').plugins.tooltip.callbacks.footer([
  {dataset: {isCapacity: true}, parsed: {y: 16}, dataIndex: 1},
  {dataset: {label: 'alice'}, parsed: {y: 12}, dataIndex: 1}
])`);
assert.equal(footer, 'Allocated: 12.0 / 16.0 (75.0%)');
run(`state.gpuChart = {data: {}, update() {}}; state.cpuChart = {data: {}, update() {}};
     state.visibleUsers.clear(); updateChartsVisibility();`);
assert.equal(run('state.gpuChart.data.datasets.length'), 2, 'capacity lines survive user deselection');
run('state.data.capacity_coverage_complete = false; renderMetrics()');
assert.equal(elements.get('gpu-util-pct').textContent, '—');
assert.equal(run('fmtSidebarPct(allocationPercent(3, installedHours("gpus")))'), '—');
console.log('Usage capacity calculations, tooltips, and filtering passed');
