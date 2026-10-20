const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const assert = require('node:assert/strict');
const { test } = require('node:test');
const source = fs.readFileSync(path.join(__dirname, '../app/static/app.js'), 'utf8');
const start = source.indexOf('function preparationQuestionCard(');
const end = source.indexOf('\nfunction ', source.indexOf('function renderPlan('));

function renderer() {
  const nodes = new Map();
  const sandbox = {
    state: {}, document: { querySelectorAll: () => [] },
    escapeHtml: value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),
    $: id => {
      if (!nodes.has(id)) nodes.set(id, {innerHTML:'', textContent:'', style:{}, classList:{toggle(){}}});
      return nodes.get(id);
    },
  };
  vm.createContext(sandbox);
  vm.runInContext(source.slice(start, end), sandbox);
  return {sandbox, nodes};
}

test('specific questions come before collapsed optional general questions', () => {
  const {sandbox,nodes} = renderer();
  sandbox.renderPlan({questions:[
    {id:'general', source:'round_standard', required:true, question:'通用开场', competency_name:'通用'},
    {id:'probe', source:'resume_jd_match', generation_mode:'llm_semantic', question:'租户校验为什么要放在读写入口？', competency_name:'权限处理', rationale:'区分身份认证与数据授权', listen_for:'能说明租户身份与工单归属', job_requirement_quote:'本人处理权限故障'},
  ]});
  const html=nodes.get('question-list').innerHTML;
  assert(html.indexOf('租户校验') < html.indexOf('通用开场'));
  assert(html.includes('<details class="standard-question-reference">'));
  assert(!html.includes('<details open'));
  assert(html.includes('为什么建议问'));
  assert(html.includes('想听清什么'));
  assert(!html.includes('可选追问：undefined'));
});

test('resume clues are not counted or styled as answered/unanswered questions', () => {
  const {sandbox,nodes} = renderer();
  sandbox.renderPlan({questions:[{id:'clue', source:'resume_jd_match', is_preparation_reference:true,
    question:'<script>test</script>', competency_name:'岗位线索'}],semantic_question_assistance:{status:'fallback_preserved'}});
  const html=nodes.get('question-list').innerHTML;
  assert(html.includes('简历切入线索 · 非生成问题'));
  assert(!html.includes('<span class="answer-state'));
  assert(!html.includes('<script>'));
  assert(html.includes('&lt;script&gt;'));
  assert.equal(nodes.get('question-count').textContent,0);
  assert(nodes.get('question-mix').textContent.includes('不是生成问题'));
});
