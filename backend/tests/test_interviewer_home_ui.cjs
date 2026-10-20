// UI contracts only; fixtures are synthetic, no live account or candidate access.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const { test } = require('node:test');
const root = path.join(__dirname, '../app/static');
const html = fs.readFileSync(path.join(root, 'index.html'), 'utf8');
const css = fs.readFileSync(path.join(root, 'styles.css'), 'utf8');
const js = fs.readFileSync(path.join(root, 'app.js'), 'utf8');
const home = html.slice(html.indexOf('<section id="welcome"'), html.indexOf('<section id="task-creator"'));

test('interviewer home is task-oriented, without the evidence slogan or principle cards', () => {
  assert(home.includes('<h1>面试工作台</h1>'));
  assert(home.includes('专注和候选人交流'));
  assert(!home.includes('EVIDENCE FIRST'));
  assert(!home.includes('回到证据'));
  assert(!home.includes('principle-stack'));
  assert(!html.includes('证据优先<br />'));
});

test('existing entry points and loading/empty states remain accessible', () => {
  for (const id of ['today-btn', 'admin-open-btn', 'agenda-empty-state', 'my-action-center', 'my-action-list', 'my-action-summary', 'my-action-boundary']) {
    assert.equal((home.match(new RegExp(`id="${id}"`, 'g')) || []).length, 1);
  }
  assert(home.includes('id="my-action-center" class="my-action-center"'));
  assert(home.includes('正在加载面试待办'));
  assert(js.includes('$("today-btn").addEventListener("click", enterTodayInterviews)'));
});

function renderer(payload) {
  const nodes = new Map();
  const calls = [];
  const sandbox = {
    state: {}, api: async () => payload,
    $: id => {
      if (!nodes.has(id)) nodes.set(id, { innerHTML: '', textContent: '' });
      return nodes.get(id);
    },
    escapeHtml: value => String(value ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])),
    document: { querySelectorAll: selector => { calls.push(selector); return []; } },
  };
  vm.createContext(sandbox);
  vm.runInContext(js.slice(js.indexOf('async function loadPersonalActionCenter()'), js.indexOf('function setSidebarActive(')), sandbox);
  return { sandbox, nodes, calls };
}

test('no assignments shows a neutral empty state rather than hiding the work area', async () => {
  const { sandbox, nodes } = renderer({ items: [], summary: { today_interviews: 0, feedback_due: 0, upcoming_7_days: 0 }, boundary: '仅展示本人安排' });
  await sandbox.loadPersonalActionCenter();
  assert(nodes.get('my-action-list').innerHTML.includes('暂时没有待办'));
  assert(nodes.get('my-action-summary').textContent.includes('今日 0'));
  assert.equal(nodes.get('my-action-boundary').textContent, '仅展示本人安排');
});

test('assigned cards retain entry and dismiss actions with HTML escaping', async () => {
  const { sandbox, nodes, calls } = renderer({
    items: [{ type: 'feedback_due', priority: 'high', title: '提交评价', candidate: { display_name: '<script>test</script>' }, job: { title: '测试岗位' }, detail: '测试安排', interview_id: 'synthetic-round', action_label: '完成评价' }],
    summary: { today_interviews: 0, feedback_due: 1, upcoming_7_days: 0 }, boundary: '',
  });
  await sandbox.loadPersonalActionCenter();
  const card = nodes.get('my-action-list').innerHTML;
  assert(card.includes('&lt;script&gt;'));
  assert(!card.includes('<script>'));
  assert(card.includes('data-my-action="synthetic-round"'));
  assert(card.includes('data-dismiss-feedback="synthetic-round"'));
  assert(!card.includes('暂时没有待办'));
  assert.equal(calls.length, 2);
});

test('home has a compact heading and narrow-screen action layout', () => {
  assert(css.includes('.welcome-card h1 { margin: 0;'));
  assert(css.includes('font-size: 28px; line-height: 1.4'));
  assert(css.includes('.interviewer-home-header { align-items: flex-start; flex-direction: column;'));
  assert(css.includes('.interviewer-home-header .welcome-actions, .interviewer-home-header #today-btn { width: 100%; }'));
  assert(!css.includes('.principle-stack'));
});

test('HR/admin continue using their existing recruitment workspace', () => {
  assert(css.includes('body.role-hr #welcome .welcome-card,\nbody.role-admin #welcome .welcome-card { display: none; }') || css.includes('body.role-hr #welcome .welcome-card,\r\nbody.role-admin #welcome .welcome-card { display: none; }'));
  assert(js.includes('openAdminPanel("home")'));
  assert(html.includes('id="admin-panel-title">招聘工作台'));
});
