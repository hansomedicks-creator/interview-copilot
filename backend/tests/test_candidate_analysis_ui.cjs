// Renderer contract test; does not automate a browser or access candidate data.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../app/static/app.js'), 'utf8');
const start = source.indexOf('function renderCandidateAnalysis(');
const end = source.indexOf('function renderSidebarScore(', start);
assert(start > 0 && end > start);
const node = { innerHTML: '' };
const sandbox = { $: () => node, escapeHtml: value => String(value ?? '').replace(/[&<>"']/g,
  char => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[char])) };
vm.createContext(sandbox);
vm.runInContext(source.slice(start, end), sandbox);
sandbox.renderCandidateAnalysis({ recommendation: {
  ai_recommendation: {decision:'advance', overall_score:3.8, confidence:.85},
  candidate_analysis: {status:'complete', summary:'说明了独立部署步骤', strengths:['接口开发'],
    details:[{title:'对话分析 1', analysis:'能解释部署，但权限细节待核实', quotes:[{quote:'<script>bad()</script>'}]}]},
  conversation_assessment:{total_batches:3, completed_batches:3},
}});
assert(node.innerHTML.includes('建议通过本轮'));
assert(node.innerHTML.includes('3.8 / 5'));
assert(node.innerHTML.includes('展开完整 AI 分析与评分标准'));
assert(node.innerHTML.includes('<details class="candidate-analysis-details">'));
assert(!node.innerHTML.includes('<details open'));
assert(node.innerHTML.includes('&lt;script&gt;'));
assert(!node.innerHTML.includes('<script>'));
assert(node.innerHTML.includes('不会扣分或阻止分析'));
sandbox.renderCandidateAnalysis({recommendation:{model_assistance:{status:'degraded'},
  ai_recommendation:{overall_score:4, label:'补充验证', process_warning:'必问题未覆盖'}}});
assert(!node.innerHTML.includes('4 / 5'));
assert(!node.innerHTML.includes('必问题未覆盖'));
assert(node.innerHTML.includes('不是候选人不通过'));
assert(node.innerHTML.includes('未评分'));
console.log('Candidate analysis renderer: 12 checks passed');
