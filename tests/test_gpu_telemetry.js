const assert = require('node:assert/strict');
const telemetry = require('../static/gpu-telemetry.js');
assert.notEqual(telemetry.color(null, 'gpu'), telemetry.color(0, 'gpu'));
assert.equal(telemetry.statistic({mean: 50, std: 12.3}), '50.0% ± 12.3 pp');
assert.match(telemetry.graph({points:[], raw_retained:false}), /expired/);
const graph = telemetry.graph({bucket_ms:15000, points:[{t:100000, gpu:10, vram:null, memory:0}, {t:200000, gpu:80, vram:null, memory:20}]});
assert.match(graph, /d="M[^L"]+M/); // A sampling outage must not become a continuous line.
assert.doesNotMatch(graph, /NaN/);
const job = {gpu_telemetry: {current: {gpu: 0}, lifetime: {gpu: {mean: 85}}}};
assert.equal(telemetry.averageActivity(job), 85);
assert.equal(telemetry.jobColor(job), telemetry.color(85, 'gpu'));
assert.equal(telemetry.jobColor({}), telemetry.color(null, 'gpu'));
console.log('GPU telemetry frontend tests passed');
