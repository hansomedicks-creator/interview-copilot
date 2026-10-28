const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const assert = require('node:assert/strict');
const {test} = require('node:test');
const source = fs.readFileSync(path.join(__dirname, '../app/static/app.js'), 'utf8');

function renderer() {
  const nodes = new Map();
  const sandbox = {
    state: {interview: {status:'in_progress'}}, renderPlan(){}, updateSuggestionStatus(){}, reviewEvidence(){},
    CSS:{escape:v=>v}, document:{querySelector:()=>null,querySelectorAll:()=>[]}, escapeHtml:v=>String(v??''),
    $: id=> {
      if(!nodes.has(id)) nodes.set(id,{textContent:'',innerHTML:'',title:'',className:'',
        classList:{toggle(){}},querySelectorAll:()=>[],querySelector:()=>null});
      return nodes.get(id);
    },
  };
  vm.createContext(sandbox);
  vm.runInContext(source.slice(source.indexOf('function semanticFailureReason('),source.indexOf('\nfunction activateUser(')),sandbox);
  vm.runInContext(source.slice(source.indexOf('function renderAnalysis('),source.indexOf('\nasync function updateSuggestionStatus(')),sandbox);
  return {sandbox,nodes};
}

test('failure reason is visible without hovering; no false retry or rule promise',()=>{
  const {sandbox,nodes}=renderer();
  sandbox.renderLlmStatus({status:'degraded',error_code:'timeout',elapsed_ms:12020,consecutive_failures:3});
  const badge=nodes.get('llm-badge');
  assert(badge.textContent.includes('模型响应超时'));
  assert(!badge.textContent.includes('规则建议继续'));
  assert(badge.title.includes('12.0 秒'));
  assert(badge.title.includes('3 次'));
  sandbox.renderLlmStatus({status:'ready'});
  assert(!badge.textContent.includes('在线'));
  assert(badge.textContent.includes('待分析'));
});

test('waiting and failed refresh preserve current card while displaying limitation',()=>{
  const {sandbox,nodes}=renderer();
  const card={id:'s',status:'active',question:'工单归属在哪里检查？',source_question_text:'如何隔离数据？',
    reason:'登录不等于授权',basis_quote:'登录就能查看所有工单',question_id:'adhoc:q'};
  const base={suggestions:[],suggestion_history:[card],current_suggestion_ids:['s']};
  sandbox.renderAnalysis({...base,availability:'semantic_unavailable',model_assistance:{status:'degraded',error_code:'rate_limited'}});
  assert(nodes.get('followup-status').textContent.includes('限流'));
  assert(nodes.get('followup-status').textContent.includes('不用模板替代'));
  assert.equal(nodes.get('urgent-followup-question').textContent,card.question);
  sandbox.renderAnalysis({...base,availability:'semantic_analysis_pending'});
  assert(nodes.get('followup-status').textContent.includes('等待'));
  assert.equal(nodes.get('urgent-followup-question').textContent,card.question);
  sandbox.renderAnalysis({...base,availability:'semantic_ready',model_assistance:{status:'active'}});
  assert.equal(nodes.get('followup-status').textContent,'');
});

test('actionable error reasons do not pretend an unrecognized error is a timeout',()=>{
  const {sandbox}=renderer();
  for(const code of ['authentication_error','insufficient_balance']) {
    assert(sandbox.semanticFailureReason({error_code:code}).includes('管理员'));
  }
  assert(sandbox.semanticFailureReason({error_code:'unknown'}).includes('尚未确认'));
});
