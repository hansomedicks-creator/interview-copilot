const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../app/static/job-assessment.js'), 'utf8');
const sandbox = {};
vm.createContext(sandbox);
vm.runInContext(source.slice(0, source.indexOf('const jobAssessmentEditor')), sandbox);
for (const input of [[70,30], [90,20,10], [1,1,1], [100,1,1], Array(20).fill(1)]) {
  const criteria = input.map(weight => ({weight}));
  const result = sandbox.balancedAssessmentWeights(criteria);
  assert.equal(result.reduce((a,b)=>a+b,0),100);
  assert(result.every(v=>Number.isInteger(v) && v>=1));
  assert.deepEqual(criteria.map(c=>c.weight),input, 'preview must not mutate edits');
}
assert.equal(JSON.stringify(sandbox.balancedAssessmentWeights([{weight:70},{weight:30}])),'[70,30]');
const app = fs.readFileSync(path.join(__dirname, '../app/static/app.js'), 'utf8');
const start = app.indexOf('function renderWeightedAssessment(');
const end = app.indexOf('function renderSidebarScore(', start);
sandbox.escapeHtml = value => String(value??'').replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
vm.runInContext(app.slice(start,end),sandbox);
const html = sandbox.renderWeightedAssessment({version:2,score:5,observed_weight:30,round_weight:100,provisional:true,policy:'未知不记零分',
  rows:[{name:'<script>bad()</script>',weight:70,critical:true,score:null,rationale:'未问',score_anchors:{'1':'差距','3':'胜任','5':'复杂场景'}}]});
assert(html.includes('暂定，不是整体达标'));
assert(html.includes('未知'));
assert(html.includes('&lt;script&gt;'));
assert(!html.includes('<script>'));
assert(html.includes('30 / 本轮 100'));
assert.equal(sandbox.renderWeightedAssessment(null),'');
console.log('22 weighted editor / renderer assertions passed');
