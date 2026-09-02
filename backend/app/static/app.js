const state = {
  interviewId: null,
  rounds: [],
  interview: null,
  ws: null,
  audioWs: null,
  mediaStream: null,
  audioContext: null,
  audioSource: null,
  audioProcessor: null,
  audioStartedAt: 0,
  audioDurationTimer: null,
  audioReady: false,
  startedAt: 0,
  sampleIndex: 0,
  capabilities: null,
  currentUser: null,
  todayInterviews: [],
  questionProgress: null,
  questionCoverage: [],
  scorecard: null,
  finalReview: null,
  finalReviewApplicationId: null,
  assignableUsers: [],
  importJobs: [],
  importBatch: null,
  importItems: [],
  jobCenterJobs: [],
  currentJobId: null,
  taskJobs: [],
  schedulingApplicationId: null,
  knowledgeStatus: null,
  knowledgeProposals: [],
  systemDocsStatus: null,
  profileJobs: [],
  profileCenter: null,
  companyProfileCenter: null,
  historicalPreview: null,
  qualityOverview: null,
  qualityJobs: [],
  governanceCenter: null,
  notificationCenter: null,
  readinessCenter: null,
  readinessResults: {},
  personalActions: null,
  hrActions: null,
  adminTasks: [],
  taskDeletion: null,
  reportCenter: null,
  report: null,
  reportApplicationId: null,
  reportAudience: "management",
  sharedReportOpened: false,
  reportReturnView: "welcome",
  speakerMappings: [],
  speakerConfirmationInFlight: false,
  lastUrgentSuggestionId: null,
  sidebarView: "home",
};

const samples = [
  "我选择这个岗位主要是希望继续做业务增长。上一份工作里我负责一个新用户项目，通过分析流失环节，把次月留存提升了 8 个百分点。",
  "当时产品和销售对优先级有冲突，我先分别沟通双方的目标，再用用户反馈和收入影响做了一版共同指标，最后推动大家按两周一个阶段上线。",
  "项目初期我的判断有错误，只关注拉新没有关注激活。复盘数据后我改变方案，增加新手引导和分层触达，最终目标才完成。",
  "我负责结果闭环，每周跟踪指标并主动协调资源。最终提前一周交付，但其中有一次失败实验，我保留了复盘记录。",
];

const $ = (id) => document.getElementById(id);

function setLargeText(enabled) {
  document.body.classList.toggle("large-text", enabled);
  $("font-toggle").setAttribute("aria-pressed", String(enabled));
  $("font-toggle").textContent = enabled ? "标准字号" : "大字模式";
  localStorage.setItem("interview-large-text", enabled ? "1" : "0");
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try { message = (await response.json()).detail || message; } catch (_) {}
    throw new Error(message);
  }
  return response.status === 204 ? null : response.json();
}

async function importDocument(fileInputId, targetFieldName) {
  const input = $(fileInputId);
  const file = input.files?.[0];
  if (!file) return;
  try {
    const response = await fetch("/api/v1/document-text", {
      method: "POST",
      headers: { "Content-Type": "application/octet-stream", "X-Filename": encodeURIComponent(file.name) },
      body: file,
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "文件解析失败");
    $("task-form").elements[targetFieldName].value = data.text;
    toast(`已从 ${data.filename} 提取 ${data.character_count} 个字符，请检查内容`);
  } catch (error) {
    input.value = "";
    toast(error.message, true);
  }
}

function toast(message, error = false) {
  const el = $("toast");
  el.textContent = message;
  el.className = `toast${error ? " error" : ""}`;
  setTimeout(() => el.classList.add("hidden"), 3200);
}

async function checkHealth() {
  try {
    const [health, capabilities, auth] = await Promise.all([
      api("/api/v1/health"),
      api("/api/v1/capabilities"),
      api("/api/v1/auth/status"),
    ]);
    state.capabilities = capabilities;
    $("api-status").textContent = `API ${health.version} 在线`;
    $("api-status").classList.remove("runtime-alert");
    document.querySelector(".status-dot").classList.add("ok");
    renderLlmStatus(capabilities.llm);
    renderAsrStatus(capabilities.asr);
    $("feishu-login-btn").classList.toggle("disabled", !auth.feishu_configured);
    $("oauth-config-note").textContent = auth.feishu_configured ? "将跳转至飞书完成安全授权" : "飞书 App ID、Secret 和回调地址尚未配置；本地可先预览角色流程";
    $("dev-login-panel").classList.toggle("hidden", !auth.development_login_available);
    if (auth.authenticated) activateUser(auth.user);
  } catch (error) {
    $("api-status").textContent = "API 不可用";
    $("api-status").classList.add("runtime-alert");
    toast(error.message, true);
  }
}

function renderLlmStatus(llm = {}) {
  const badge = $("llm-badge");
  const status = llm.status || "mock_rules";
  const healthy = ["ready", "active"].includes(status);
  badge.className = `pill ${healthy ? "ready" : status === "degraded" ? "degraded" : "warning"}`;
  badge.classList.toggle("runtime-alert", !healthy && status !== "mock_rules");
  const labels = {
    ready: "AI 语义分析在线",
    active: "AI 语义分析在线",
    recovering: "AI 语义分析重试中",
    degraded: "AI 语义暂不可用 · 规则建议继续",
    fallback: "AI 规则保障中",
    not_configured: "AI 未配置",
    mock_rules: "AI 规则模式",
  };
  badge.textContent = labels[status] || "AI 规则保障中";
  const explanations = {
    ready: "语义模型在线；本地规则同时作为安全保障。",
    active: "语义模型已理解最新回答并参与追问与证据分析。",
    recovering: "单次模型请求超时或服务波动，系统正在重试。实时字幕、录音和本地浅答提醒不受影响。",
    degraded: "连续多次语义请求失败。实时字幕、录音和本地追问仍继续，但语义理解、证据提取与建议精度会暂时降低。",
    fallback: "语义模型暂不可用，当前由本地规则继续保障基本追问。",
    not_configured: "尚未配置语义模型；字幕与录音可以工作，但追问主要来自本地规则。",
    mock_rules: "当前使用本地规则分析，未调用真实语义模型。",
  };
  const errorLabels = {
    connection_error: "网络连接或请求超时",
    upstream_error: "模型鉴权、余额、限流或上游服务异常",
    insufficient_balance: "模型账户余额不足",
    authentication_error: "模型密钥无效或权限不足",
    rate_limited: "模型请求过于频繁",
    invalid_response: "模型返回为空或未通过结构化校验",
  };
  badge.title = `${explanations[status] || explanations.fallback}${llm.error_code ? ` 原因类型：${errorLabels[llm.error_code] || llm.error_code}。` : ""}`;
}

function activateUser(user) {
  state.currentUser = user;
  document.body.classList.add("authenticated");
  document.body.classList.remove("role-hr", "role-admin", "role-interviewer");
  document.body.classList.add(`role-${user.role}`);
  $("auth-gate").classList.add("hidden");
  $("welcome").classList.remove("hidden");
  $("app-sidebar").classList.remove("hidden");
  const roleLabel = user.role === "interviewer" ? "面试官" : user.role === "hr" ? "招聘 HR" : "管理员";
  $("current-user").textContent = `${user.display_name} · ${roleLabel}`;
  $("sidebar-user-role").textContent = `${user.display_name} · ${roleLabel}`;
  $("logout-btn").classList.remove("hidden");
  document.querySelectorAll(".hr-only").forEach((item) => item.classList.toggle("hidden", !["hr", "admin"].includes(user.role)));
  document.querySelectorAll(".interviewer-only").forEach((item) => item.classList.toggle("hidden", user.role !== "interviewer"));
  document.querySelectorAll(".admin-only").forEach((item) => item.classList.toggle("hidden", user.role !== "admin"));
  const sharedReport = new URLSearchParams(window.location.search).has("report");
  if (["hr", "admin"].includes(user.role) && !sharedReport) {
    openAdminPanel("home").catch((error) => toast(error.message, true));
  } else {
    setSidebarActive("home");
    loadPersonalActionCenter().catch((error) => toast(error.message, true));
  }
  openSharedReportIfRequested().catch((error) => toast(error.message, true));
}

async function loadPersonalActionCenter() {
  state.personalActions = await api("/api/v1/me/action-center");
  const center = state.personalActions;
  const summary = center.summary;
  const visibleItems = center.items.slice(0, 4);
  $("my-action-center").classList.toggle("hidden", center.items.length === 0);
  $("my-action-summary").textContent = `今日 ${summary.today_interviews} · 待评价 ${summary.feedback_due} · 七天内 ${summary.upcoming_7_days}`;
  $("my-action-boundary").textContent = center.boundary;
  $("my-action-list").innerHTML = visibleItems.map((item) => `
    <article class="my-action-card ${escapeHtml(item.priority)}">
      <div><h3>${escapeHtml(item.title)} · ${escapeHtml(item.candidate.display_name)}</h3><p>${escapeHtml(item.job.title)} · ${item.scheduled_at ? new Date(item.scheduled_at).toLocaleString("zh-CN") : "时间待定"}</p><small>${escapeHtml(item.detail)}</small></div>
      <div class="my-action-buttons"><button class="${item.type === "feedback_due" ? "primary" : "secondary"} compact" data-my-action="${escapeHtml(item.interview_id)}">${escapeHtml(item.action_label)}</button>${item.type === "feedback_due" ? `<button class="secondary compact todo-remove" data-dismiss-feedback="${escapeHtml(item.interview_id)}">移出待评价</button>` : ""}</div>
    </article>`).join("");
  document.querySelectorAll("[data-my-action]").forEach((button) => button.addEventListener("click", () => openInterviewFromAction(button.dataset.myAction)));
  document.querySelectorAll("#my-action-list [data-dismiss-feedback]").forEach((button) => button.addEventListener("click", dismissFeedbackTodo));
}

function setSidebarActive(view) {
  state.sidebarView = view;
  document.querySelectorAll("[data-app-nav]").forEach((button) => button.classList.toggle("active", button.dataset.appNav === view));
}

async function showHomeView() {
  if (state.currentUser && ["hr", "admin"].includes(state.currentUser.role)) {
    await openAdminPanel("home");
    return;
  }
  closeSocket();
  $("workspace").classList.add("hidden");
  $("workspace").classList.remove("evaluation-mode");
  $("welcome").classList.remove("hidden");
  ["task-creator", "admin-panel", "job-center-panel", "resume-import-panel", "final-review-panel", "knowledge-panel", "talent-profile-panel", "company-profile-panel", "quality-dashboard-panel", "report-panel", "governance-panel", "notification-panel", "readiness-panel"].forEach((id) => $(id)?.classList.add("hidden"));
  $("welcome").querySelector(".welcome-card").classList.remove("hidden");
  setSidebarActive("home");
  loadPersonalActionCenter().catch(() => {});
}

async function showInterviewView() {
  setSidebarActive("interviews");
  if (!state.interviewId) return enterTodayInterviews();
  $("welcome").classList.add("hidden");
  $("workspace").classList.remove("hidden", "evaluation-mode");
  if (!state.interview) await loadInterview();
}

async function showEvaluationView() {
  if (!state.interviewId || state.interview?.status !== "completed") {
    toast("结束本轮面试后，AI 会自动生成评分与进入下一轮建议", true);
    return;
  }
  if (!state.scorecard) await tryLoadScorecard();
  if (!state.scorecard) return toast("评价草稿尚未生成，请稍后重试", true);
  $("welcome").classList.add("hidden");
  $("workspace").classList.remove("hidden");
  $("workspace").classList.add("evaluation-mode");
  setSidebarActive("evaluation");
  window.scrollTo({ top: 0, behavior: "smooth" });
}

async function dismissFeedbackTodo(event) {
  const interviewId = event.currentTarget.dataset.dismissFeedback;
  if (!confirm("确认将这场面试移出“待评价”吗？录音、逐字稿和 AI 评分仍会保留，之后仍可从面试汇总查看。")) return;
  try {
    await api(`/api/v1/interviews/${interviewId}/scorecard/dismiss`, {
      method: "POST",
      body: JSON.stringify({ dismissed_by: state.currentUser?.display_name || "当前用户", reason: "用户从行动中心移除" }),
    });
    await loadPersonalActionCenter();
    if (state.currentUser && ["hr", "admin"].includes(state.currentUser.role) && !$("admin-panel").classList.contains("hidden")) await loadAdminTasks();
    toast("已移出待评价；录音、逐字稿和 AI 草稿均未删除");
  } catch (error) { toast(error.message, true); }
}

async function devLogin(openId) {
  const data = await api("/api/v1/auth/dev-login", { method: "POST", body: JSON.stringify({ open_id: openId }) });
  activateUser(data.user);
}

async function logout() {
  await api("/api/v1/auth/logout", { method: "POST" });
  location.reload();
}

function renderAsrStatus(asr = {}) {
  const status = asr.status || "not_configured";
  const provider = asr.provider === "tencent" ? "腾讯云" : "ASR";
  const badge = $("asr-badge");
  badge.className = `pill ${status === "ready" ? "ready" : status === "degraded" ? "degraded" : "warning"}`;
  badge.classList.toggle("runtime-alert", status !== "ready");
  badge.textContent = status === "ready" ? `${provider} ASR 就绪` : status === "recovering" ? `${provider} ASR 重连中` : status === "degraded" ? `${provider} ASR 降级` : "ASR 未配置";
  $("mode-hint").textContent = status === "ready"
    ? `麦克风真实收音 · ${provider}实时字幕 · 文字输入可作降级测试`
    : status === "recovering" || status === "degraded"
      ? "录音持续保存 · 实时字幕连接正在恢复 · 文字输入可作临时补充"
      : "麦克风真实收音 · ASR 未配置 · 文字输入用于降级测试";
  $("asr-boundary").className = `asr-boundary${status === "ready" ? " ready" : status === "degraded" ? " degraded" : ""}`;
  $("asr-boundary-title").textContent = status === "ready"
    ? `当前状态：${provider}实时 ASR 已接入。`
    : status === "recovering" ? `${provider}实时字幕连接短暂波动，正在自动重连；录音没有中断。`
      : status === "degraded" ? `${provider}实时字幕连续重连失败，录音仍在继续。` : "当前状态：已具备收音与录音能力，ASR 尚未配置。";
  $("asr-boundary-detail").textContent = status === "ready"
    ? (asr.speaker_diarization === false
      ? "稳定结果会写入逐字稿；当前引擎未开启话者分离，声音身份需要人工确认。"
      : "稳定结果会写入逐字稿；系统自动区分声源并判断角色，低置信度内容不会进入候选人证据。")
    : status === "recovering"
      ? "重连期间声音仍写入 WAV；连接恢复后继续生成实时字幕。短暂断线区间可能需要会后补转写。"
      : "声音仍会进入 Pipecat 音频帧边界并保存为 WAV；实时语义追问会失去这段字幕上下文，手动逐字稿可作为临时补充。";
}

function localDateTimeValue(date) {
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60000);
  return local.toISOString().slice(0, 16);
}

function taskRoundCards() {
  return [...document.querySelectorAll("#task-form [data-round-form]")];
}

function updateTaskRoundFlow() {
  const cards = taskRoundCards();
  const enabled = cards.filter((card) => card.querySelector("[data-round-enabled]").checked);
  cards.forEach((card) => {
    const isEnabled = enabled.includes(card);
    card.classList.toggle("disabled", !isEnabled);
    card.querySelectorAll("select, input[type='datetime-local']").forEach((field) => {
      field.disabled = !isEnabled;
      field.required = isEnabled;
    });
    card.querySelector(".round-no").textContent = isEnabled ? String(enabled.indexOf(card) + 1) : "—";
    const previous = enabled[enabled.indexOf(card) - 1];
    const next = enabled[enabled.indexOf(card) + 1];
    card.querySelector('[data-round-move="up"]').disabled = !isEnabled || !previous;
    card.querySelector('[data-round-move="down"]').disabled = !isEnabled || !next;
  });
  const labels = enabled.map((card) => roundLabel(card.dataset.roundForm));
  $("task-flow-summary").textContent = labels.length
    ? `当前流程：${labels.join(" → ")}（${labels.length} 轮）`
    : "请至少启用一个面试轮次";
}

function normalizeTaskRoundTimes() {
  const enabled = taskRoundCards().filter((card) => card.querySelector("[data-round-enabled]").checked);
  const values = enabled.map((card) => card.querySelector("input[type='datetime-local']").value).filter(Boolean).sort();
  enabled.forEach((card, index) => {
    if (values[index]) card.querySelector("input[type='datetime-local']").value = values[index];
  });
}

function applyTaskRoundFlow(roundOrder) {
  const grid = document.querySelector("#task-form .schedule-grid");
  const cards = taskRoundCards();
  const validOrder = [...new Set((roundOrder || []).filter((item) => ["business", "hr", "ceo"].includes(item)))];
  const effectiveOrder = validOrder.length ? validOrder : ["business", "hr", "ceo"];
  [...effectiveOrder, ...["business", "hr", "ceo"].filter((item) => !effectiveOrder.includes(item))].forEach((roundType) => {
    const card = cards.find((item) => item.dataset.roundForm === roundType);
    if (card) grid.appendChild(card);
  });
  taskRoundCards().forEach((card) => {
    card.querySelector("[data-round-enabled]").checked = effectiveOrder.includes(card.dataset.roundForm);
  });
  updateTaskRoundFlow();
  normalizeTaskRoundTimes();
}

function moveTaskRound(card, direction) {
  const enabled = taskRoundCards().filter((item) => item.querySelector("[data-round-enabled]").checked);
  const index = enabled.indexOf(card);
  if (direction === "up" && index > 0) {
    card.parentElement.insertBefore(card, enabled[index - 1]);
  } else if (direction === "down" && index >= 0 && index < enabled.length - 1) {
    const next = enabled[index + 1];
    card.parentElement.insertBefore(next, card);
  }
  normalizeTaskRoundTimes();
  updateTaskRoundFlow();
}

function presetTaskSchedule() {
  const base = new Date();
  base.setMinutes(Math.ceil(base.getMinutes() / 15) * 15, 0, 0);
  taskRoundCards().filter((card) => card.querySelector("[data-round-enabled]").checked).forEach((card, index) => {
    const time = new Date(base.getTime() + index * 24 * 60 * 60 * 1000);
    card.querySelector("input[type='datetime-local']").value = localDateTimeValue(time);
  });
}

async function loadAssignableUsers() {
  state.assignableUsers = await api("/api/v1/admin/users");
  const preferredUser = (roundType) => {
    if (roundType === "hr") return state.assignableUsers.find((user) => ["hr", "admin"].includes(user.role));
    if (roundType === "ceo") return state.assignableUsers.find((user) => /CEO|总裁|总经理|董事长|创始人|陈总|总$/.test(user.display_name));
    return state.assignableUsers.find((user) => user.role === "interviewer" && !/CEO|总裁|总经理|董事长|创始人|总$/.test(user.display_name));
  };
  ["business", "hr", "ceo"].forEach((roundType) => {
    const selected = preferredUser(roundType)?.open_id || state.assignableUsers[0]?.open_id;
    $("task-form").elements[`${roundType}_interviewer`].innerHTML = state.assignableUsers.map((user) => `<option value="${escapeHtml(user.open_id)}" ${user.open_id === selected ? "selected" : ""}>${escapeHtml(user.display_name)} · ${escapeHtml(user.role)}</option>`).join("");
  });
}

async function openTaskCreator() {
  setSidebarActive("admin");
  state.schedulingApplicationId = null;
  [state.taskJobs] = await Promise.all([
    api("/api/v1/admin/jobs"),
    loadAssignableUsers(),
  ]);
  $("workspace").classList.add("hidden");
  $("welcome").classList.remove("hidden");
  $("admin-panel").classList.add("hidden");
  $("final-review-panel").classList.add("hidden");
  $("knowledge-panel").classList.add("hidden");
  $("talent-profile-panel").classList.add("hidden");
  $("company-profile-panel").classList.add("hidden");
  $("job-center-panel").classList.add("hidden");
  $("quality-dashboard-panel").classList.add("hidden");
  $("report-panel").classList.add("hidden");
  $("governance-panel").classList.add("hidden");
  $("notification-panel").classList.add("hidden");
  $("readiness-panel").classList.add("hidden");
  $("resume-import-panel").classList.add("hidden");
  $("welcome").querySelector(".welcome-card").classList.add("hidden");
  $("task-creator").classList.remove("hidden");
  applyTaskRoundFlow(["business", "hr", "ceo"]);
  renderTaskJobOptions();
  presetTaskSchedule();
}

async function scheduleImportedCandidate(task) {
  await openTaskCreator();
  state.schedulingApplicationId = task.task_id;
  const form = $("task-form");
  form.elements.candidate_name.value = task.candidate.display_name;
  form.elements.resume_text.value = task.candidate.resume_text || "简历已批量导入";
  form.elements.job_title.value = task.job.title;
  form.elements.source_job_code.value = task.job.source_job_code || "";
  form.elements.jd_text.value = task.job.jd_text || "岗位要求待 HR 补充";
  form.elements.job_id.value = task.job.id;
  syncTaskJobFields();
  form.elements.candidate_name.readOnly = true;
  form.elements.resume_text.readOnly = true;
  form.elements.job_title.readOnly = true;
  toast(`正在为 ${task.candidate.display_name} 配置岗位面试流程`);
}

function renderTaskJobOptions(preferredJobId = null) {
  const select = $("task-job-select");
  const activeJobs = state.taskJobs.filter((job) => job.status !== "paused");
  select.innerHTML = '<option value="">请选择已建立岗位</option>' + activeJobs.map((job) =>
    `<option value="${escapeHtml(job.id)}">${escapeHtml(job.title)}${job.source_job_code ? ` · ${escapeHtml(job.source_job_code)}` : ""}</option>`
  ).join("");
  if (preferredJobId && activeJobs.some((job) => job.id === preferredJobId)) select.value = preferredJobId;
  syncTaskJobFields();
}

function syncTaskJobFields() {
  const form = $("task-form");
  const job = state.taskJobs.find((item) => item.id === form.elements.job_id.value);
  form.elements.job_title.value = job?.title || "";
  form.elements.source_job_code.value = job?.source_job_code || "";
  form.elements.jd_text.value = job?.jd_text || "";
  applyTaskRoundFlow(job?.semantic_profile?.interview_flow?.round_order || ["business", "hr", "ceo"]);
  presetTaskSchedule();
}

async function closeTaskCreator() {
  if (state.currentUser && ["hr", "admin"].includes(state.currentUser.role)) {
    await openAdminPanel("home");
    return;
  }
  $("task-creator").classList.add("hidden");
  $("welcome").querySelector(".welcome-card").classList.remove("hidden");
}

async function openAdminPanel(view = "admin") {
  const copy = {
    home: ["招聘工作台", "今天的面试、待补评价和终审安排集中在这里。"],
    admin: ["面试任务", "管理候选人的岗位流程、面试官分配和任务状态。"],
    "final-review": ["待终审", "查看已完成面试的证据汇总，并由 HR 作出最终流程决定。"],
  }[view] || ["招聘工作台", "今天的面试、待补评价和终审安排集中在这里。"];
  setSidebarActive(view);
  $("workspace").classList.add("hidden");
  $("welcome").classList.remove("hidden");
  $("welcome").querySelector(".welcome-card").classList.add("hidden");
  $("task-creator").classList.add("hidden");
  $("resume-import-panel").classList.add("hidden");
  $("knowledge-panel").classList.add("hidden");
  $("talent-profile-panel").classList.add("hidden");
  $("company-profile-panel").classList.add("hidden");
  $("job-center-panel").classList.add("hidden");
  $("quality-dashboard-panel").classList.add("hidden");
  $("report-panel").classList.add("hidden");
  $("governance-panel").classList.add("hidden");
  $("notification-panel").classList.add("hidden");
  $("readiness-panel").classList.add("hidden");
  $("final-review-panel").classList.add("hidden");
  $("admin-panel").classList.remove("hidden");
  $("admin-panel").dataset.view = view;
  $("admin-panel").classList.toggle("review-queue", view === "final-review");
  $("admin-panel-title").textContent = copy[0];
  $("admin-panel-subtitle").textContent = copy[1];
  await Promise.all([loadAssignableUsers(), loadAdminTasks()]);
  if (view === "final-review") window.setTimeout(() => $("admin-action-center")?.scrollIntoView({ behavior: "smooth", block: "start" }), 0);
}

async function openJobCenter(preferredJobId = null) {
  setSidebarActive("jobs");
  ["admin-panel", "task-creator", "resume-import-panel", "talent-profile-panel", "company-profile-panel", "quality-dashboard-panel", "knowledge-panel", "governance-panel", "notification-panel", "readiness-panel", "report-panel", "final-review-panel"].forEach((id) => $(id).classList.add("hidden"));
  $("job-center-panel").classList.remove("hidden");
  try {
    await loadJobCenter(preferredJobId);
  } catch (error) { toast(error.message, true); }
}

async function closeJobCenter() {
  await openAdminPanel("home");
}

async function loadJobCenter(preferredJobId = null) {
  state.jobCenterJobs = await api("/api/v1/admin/jobs");
  const activeCount = state.jobCenterJobs.filter((job) => job.status !== "paused").length;
  const jdReadyCount = state.jobCenterJobs.filter((job) => job.jd_character_count >= 20).length;
  const profileReadyCount = state.jobCenterJobs.filter((job) => job.profile?.state === "active").length;
  const waitingReviewCount = state.jobCenterJobs.filter((job) => job.profile?.draft_version).length;
  $("job-center-summary").innerHTML = `
    <article><small>岗位总数</small><strong>${state.jobCenterJobs.length}</strong><span>${activeCount} 个招聘中</span></article>
    <article><small>JD 已建立</small><strong>${jdReadyCount}</strong><span>可用于生成面试重点</span></article>
    <article><small>画像已生效</small><strong>${profileReadyCount}</strong><span>已由 HR 审核</span></article>
    <article><small>画像待审核</small><strong>${waitingReviewCount}</strong><span>不会自动生效</span></article>`;
  if (preferredJobId && state.jobCenterJobs.some((job) => job.id === preferredJobId)) state.currentJobId = preferredJobId;
  else if (state.currentJobId && !state.jobCenterJobs.some((job) => job.id === state.currentJobId)) state.currentJobId = null;
  renderJobList();
  if (state.currentJobId) selectJob(state.currentJobId);
  else if (!state.jobCenterJobs.length) startNewJob();
}

function renderJobList() {
  const query = $("job-search-input").value.trim().toLowerCase();
  const jobs = state.jobCenterJobs.filter((job) => !query || `${job.title} ${job.source_job_code || ""}`.toLowerCase().includes(query));
  const profileLabel = (job) => job.profile?.state === "active" ? "画像已生效" : job.profile?.draft_version ? "画像待审核" : "画像未生成";
  $("job-list").innerHTML = jobs.map((job) => `
    <button class="job-list-card ${job.id === state.currentJobId ? "active" : ""} ${job.status === "paused" ? "paused" : ""}" data-job-select="${escapeHtml(job.id)}">
      <span class="job-list-card-head"><strong>${escapeHtml(job.title)}</strong><span class="job-profile-badge ${job.profile?.state === "active" ? "active" : ""}">${profileLabel(job)}</span></span>
      <small>${escapeHtml(job.source_job_code || "未设置岗位编号")} · ${job.status === "paused" ? "暂停招聘" : "招聘中"}</small>
      <span class="job-list-card-meta"><span>JD ${job.jd_character_count} 字</span><span>${job.application_count} 位候选人</span></span>
    </button>`).join("") || '<div class="empty-state"><strong>没有匹配岗位</strong><span>换个关键词，或点击“新建岗位”。</span></div>';
  document.querySelectorAll("[data-job-select]").forEach((button) => button.addEventListener("click", () => selectJob(button.dataset.jobSelect)));
}

function selectJob(jobId) {
  const job = state.jobCenterJobs.find((item) => item.id === jobId);
  if (!job) return;
  state.currentJobId = job.id;
  $("job-editor-empty").classList.add("hidden");
  $("job-editor-form").classList.remove("hidden");
  $("job-editor-mode").textContent = "EDIT JOB";
  $("job-editor-title").textContent = `维护 ${job.title}`;
  $("job-title-input").value = job.title;
  $("job-code-input").value = job.source_job_code || "";
  $("job-status-input").value = job.status === "paused" ? "paused" : "active";
  $("job-jd-input").value = job.jd_text || "";
  const profileBadge = $("job-editor-profile-state");
  profileBadge.className = `pill ${job.profile?.state === "active" ? "ready" : "warning"}`;
  profileBadge.textContent = job.profile?.state === "active" ? `${job.profile.active_version} 已生效` : job.profile?.draft_version ? `${job.profile.draft_version} 待审核` : "尚未生成画像";
  $("job-open-profile-btn").disabled = false;
  $("job-import-candidates-btn").disabled = job.status === "paused";
  updateJobJdCount();
  renderJobSemanticSummary(job.semantic_profile);
  renderJobList();
}

function renderJobSemanticSummary(profile = {}) {
  const panel = $("job-semantic-summary");
  const dimensions = Array.isArray(profile.interview_dimensions) ? profile.interview_dimensions : [];
  if (!profile.role_mission && !dimensions.length) {
    panel.classList.add("hidden");
    return;
  }
  panel.classList.remove("hidden");
  const isModel = profile.analysis_mode === "llm_semantic";
  const isHybrid = profile.analysis_mode === "hybrid_semantic";
  $("job-semantic-mode").className = `pill ${isModel ? "ready" : "warning"}`;
  $("job-semantic-mode").textContent = isModel ? "AI 已理解岗位" : isHybrid ? "AI 理解 + 本地校验" : "本地结构化保障";
  $("job-semantic-mission").textContent = profile.role_mission || "已按岗位职责建立证据验证结构。";
  $("job-semantic-outcomes").innerHTML = (profile.business_outcomes || []).map((item) => `<span>${escapeHtml(item)}</span>`).join("") || "<small>尚未提炼</small>";
  const roundLabels = { business: "业务面", hr: "HR 面", ceo: "CEO 面" };
  $("job-semantic-dimensions").innerHTML = dimensions.map((item) => `<span>${roundLabels[item.round_type] || "面试"} · ${escapeHtml(item.name || "岗位职责核实")}</span>`).join("") || "<small>尚未生成</small>";
  const exclusions = Array.isArray(profile.excluded_non_job_factors) ? profile.excluded_non_job_factors : [];
  const exclusionPanel = $("job-semantic-exclusions");
  exclusionPanel.classList.toggle("hidden", exclusions.length === 0);
  exclusionPanel.innerHTML = exclusions.length ? `<strong>已排除 ${exclusions.length} 项非岗位因素</strong><span>${exclusions.map((item) => `${escapeHtml(item.text)}（${escapeHtml(item.reason)}）`).join("、")}不会进入提问或评价。</span>` : "";
}

function startNewJob() {
  state.currentJobId = null;
  $("job-editor-empty").classList.add("hidden");
  const form = $("job-editor-form");
  form.classList.remove("hidden");
  form.reset();
  $("job-editor-mode").textContent = "NEW JOB";
  $("job-editor-title").textContent = "新建岗位";
  $("job-editor-profile-state").className = "pill warning";
  $("job-editor-profile-state").textContent = "保存后生成画像草稿";
  $("job-open-profile-btn").disabled = true;
  $("job-import-candidates-btn").disabled = true;
  $("job-jd-file").value = "";
  $("job-semantic-summary").classList.add("hidden");
  updateJobJdCount();
  renderJobList();
  $("job-title-input").focus();
}

function updateJobJdCount() {
  const count = $("job-jd-input").value.trim().length;
  $("job-jd-count").textContent = `${count} 个字符${count < 20 ? " · 至少需要 20 个字符" : " · 可生成画像"}`;
}

async function importJobJdFile() {
  const input = $("job-jd-file");
  const file = input.files?.[0];
  if (!file) return;
  try {
    const response = await fetch("/api/v1/document-text", {
      method: "POST",
      headers: { "Content-Type": "application/octet-stream", "X-Filename": encodeURIComponent(file.name) },
      body: file,
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "JD 文件解析失败");
    $("job-jd-input").value = data.text;
    updateJobJdCount();
    toast(`已从 ${data.filename} 提取 ${data.character_count} 个字符，请检查后保存`);
  } catch (error) {
    input.value = "";
    toast(error.message, true);
  }
}

async function saveJobDefinition(event) {
  event.preventDefault();
  const title = $("job-title-input").value.trim();
  const jdText = $("job-jd-input").value.trim();
  if (jdText.length < 20) return toast("请补充完整 JD，至少需要 20 个字符", true);
  const button = $("job-save-btn");
  button.disabled = true;
  const originalLabel = button.textContent;
  button.textContent = "正在理解完整 JD 并生成分轮问题…";
  try {
    const path = state.currentJobId ? `/api/v1/admin/jobs/${state.currentJobId}` : "/api/v1/admin/jobs";
    const result = await api(path, {
      method: state.currentJobId ? "PUT" : "POST",
      body: JSON.stringify({
        title,
        source_job_code: $("job-code-input").value.trim() || null,
        status: $("job-status-input").value,
        jd_text: jdText,
      }),
    });
    state.currentJobId = result.job.id;
    const refreshText = result.refreshed_interviews ? `，并刷新 ${result.refreshed_interviews} 场尚未开始的面试` : "";
    const frozenText = result.frozen_in_progress ? `；${result.frozen_in_progress} 场进行中面试保持原标准` : "";
    const mode = result.job.semantic_profile?.analysis_mode;
    const semanticMode = mode === "llm_semantic" ? "AI 已完成岗位语义理解" : mode === "hybrid_semantic" ? "AI 理解结果已由本地规则补全" : "已启用本地结构化保障";
    toast(`${title} 已保存，${semanticMode}；${result.talent_profile_draft.version_label} 等待 HR 审核${refreshText}${frozenText}`);
    await loadJobCenter(state.currentJobId);
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; button.textContent = originalLabel; }
}

async function openSelectedJobProfile() {
  if (!state.currentJobId) return;
  await openTalentProfilePanel(state.currentJobId);
}

async function openSelectedJobResumeImport() {
  if (!state.currentJobId) return;
  await openResumeImport(state.currentJobId);
}

async function openResumeImport(preferredJobId = null) {
  setSidebarActive("admin");
  $("admin-panel").classList.add("hidden");
  $("report-panel").classList.add("hidden");
  $("governance-panel").classList.add("hidden");
  $("notification-panel").classList.add("hidden");
  $("readiness-panel").classList.add("hidden");
  $("quality-dashboard-panel").classList.add("hidden");
  $("task-creator").classList.add("hidden");
  $("job-center-panel").classList.add("hidden");
  $("final-review-panel").classList.add("hidden");
  $("resume-import-panel").classList.remove("hidden");
  state.importBatch = null;
  state.importItems = [];
  $("import-review").classList.add("hidden");
  $("import-progress").classList.add("hidden");
  $("resume-batch-files").value = "";
  state.importJobs = await api("/api/v1/admin/jobs");
  const activeJobs = state.importJobs.filter((job) => job.status !== "paused");
  $("import-job-select").innerHTML = '<option value="">请选择已建立岗位</option>' + activeJobs.map((job) => `<option value="${escapeHtml(job.id)}">${escapeHtml(job.title)}${job.source_job_code ? ` · ${escapeHtml(job.source_job_code)}` : ""}</option>`).join("");
  if (preferredJobId && activeJobs.some((job) => job.id === preferredJobId)) $("import-job-select").value = preferredJobId;
  renderImportJobContext();
}

async function closeResumeImport() {
  await openAdminPanel("admin");
}

function renderImportJobContext() {
  const job = state.importJobs.find((item) => item.id === $("import-job-select").value);
  $("import-job-context").innerHTML = job
    ? `<strong>${escapeHtml(job.title)}</strong><br>JD ${job.jd_character_count} 字 · ${job.profile?.state === "active" ? `${escapeHtml(job.profile.active_version)} 已生效` : job.profile?.draft_version ? `${escapeHtml(job.profile.draft_version)} 待 HR 审核` : "画像尚未生成"}`
    : "选择岗位后，这里会显示 JD 和画像状态。";
}

async function beginResumeUpload(files) {
  const accepted = [...files].filter((file) => [".pdf", ".docx", ".txt", ".md"].some((suffix) => file.name.toLowerCase().endsWith(suffix)));
  if (!accepted.length) return toast("请选择 PDF、Word、TXT 或 Markdown 简历", true);
  if (accepted.length > 50) return toast("为便于核对，一次最多导入 50 份简历", true);
  const jobId = $("import-job-select").value;
  if (!jobId) return toast("请先选择岗位；新岗位请到“岗位与 JD”中建立", true);
  try {
    state.importBatch = await api("/api/v1/admin/resume-imports", {
      method: "POST",
      body: JSON.stringify({ job_id: jobId }),
    });
    state.importItems = [];
    $("import-progress").classList.remove("hidden");
    $("import-review").classList.remove("hidden");
    $("import-progress-bar").max = accepted.length;
    for (let index = 0; index < accepted.length; index += 1) {
      const file = accepted[index];
      $("import-progress-title").textContent = `正在识别：${file.name}`;
      $("import-progress-detail").textContent = `${index} / ${accepted.length}`;
      try {
        const response = await fetch(`/api/v1/admin/resume-imports/${state.importBatch.id}/items`, {
          method: "POST", headers: { "Content-Type": "application/octet-stream", "X-Filename": encodeURIComponent(file.name) }, body: file,
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || "简历解析失败");
        state.importItems.push(data);
      } catch (error) {
        state.importItems.push({ id: `error-${index}`, filename: file.name, status: "error", error_message: error.message, recognized: { fields: {}, confidence: {}, evidence: {}, warnings: [error.message] } });
      }
      $("import-progress-bar").value = index + 1;
      $("import-progress-detail").textContent = `${index + 1} / ${accepted.length}`;
      renderResumeImportItems();
    }
    $("import-progress-title").textContent = "识别完成，请集中校对";
    toast(`已处理 ${accepted.length} 份简历，请核对标记项`);
  } catch (error) {
    toast(error.message, true);
  }
}

function confidenceLabel(value) {
  if (value >= .9) return "高置信";
  if (value >= .7) return "建议核对";
  return "待人工补充";
}

function renderResumeImportItems() {
  $("resume-import-rows").innerHTML = state.importItems.map((item) => {
    const f = item.recognized?.fields || {};
    const c = item.recognized?.confidence || {};
    const warnings = item.recognized?.warnings || [];
    const failed = item.status === "error";
    const needsReview = failed || warnings.length > 0;
    return `<tr data-import-item="${escapeHtml(item.id)}">
      <td><input data-import-select type="checkbox" ${failed ? "disabled" : "checked"} /></td>
      <td><div class="resume-file-cell"><strong>${escapeHtml(item.filename)}</strong><span class="import-status ${needsReview ? "review" : ""}">${failed ? "解析失败" : needsReview ? "请核对" : "已识别"}</span><small>${escapeHtml(warnings.join("；") || "资料识别完成")}</small></div></td>
      <td><div class="field-stack"><input data-field="name" value="${escapeHtml(f.name || "")}" placeholder="必填" ${failed ? "disabled" : ""}/><span class="confidence-note">${confidenceLabel(c.name || 0)}</span></div></td>
      <td><div class="field-stack"><input data-field="phone" value="${escapeHtml(f.phone || "")}" placeholder="手机号" ${failed ? "disabled" : ""}/><input data-field="email" value="${escapeHtml(f.email || "")}" placeholder="邮箱" ${failed ? "disabled" : ""}/></div></td>
      <td><div class="field-stack"><input data-field="years_experience" type="number" min="0" max="80" value="${f.years_experience ?? ""}" placeholder="工作年限" ${failed ? "disabled" : ""}/><input data-field="highest_education" value="${escapeHtml(f.highest_education || "")}" placeholder="最高学历" ${failed ? "disabled" : ""}/></div></td>
      <td><div class="field-stack"><input data-field="current_company" value="${escapeHtml(f.current_company || "")}" placeholder="当前公司" ${failed ? "disabled" : ""}/><input data-field="current_title" value="${escapeHtml(f.current_title || "")}" placeholder="当前职位" ${failed ? "disabled" : ""}/><input data-field="location" value="${escapeHtml(f.location || "")}" placeholder="所在城市" ${failed ? "disabled" : ""}/></div></td>
      <td><details><summary>查看依据</summary><small>${Object.entries(item.recognized?.evidence || {}).map(([key, value]) => `${escapeHtml(key)}：${escapeHtml(value)}`).join("<br>") || "暂无自动识别依据，请人工填写"}</small></details></td>
      <td><button type="button" class="danger compact" data-delete-import-item="${escapeHtml(item.id)}">删除</button></td>
    </tr>`;
  }).join("");
  updateImportSummary();
  document.querySelectorAll("[data-import-select]").forEach((input) => input.addEventListener("change", updateImportSummary));
  document.querySelectorAll("[data-delete-import-item]").forEach((button) => button.addEventListener("click", deleteResumeImportItem));
}

async function deleteResumeImportItem(event) {
  const itemId = event.currentTarget.dataset.deleteImportItem;
  const item = state.importItems.find((candidate) => candidate.id === itemId);
  if (!item || !confirm(`确认删除“${item.filename}”吗？这份简历不会进入待排期列表。`)) return;
  try {
    if (!String(itemId).startsWith("error-") && state.importBatch?.id) {
      await api(`/api/v1/admin/resume-imports/${state.importBatch.id}/items/${itemId}`, { method: "DELETE" });
    }
    state.importItems = state.importItems.filter((candidate) => candidate.id !== itemId);
    renderResumeImportItems();
    toast("已删除误导入的简历");
  } catch (error) { toast(error.message, true); }
}

function updateImportSummary() {
  const selected = document.querySelectorAll("[data-import-select]:checked").length;
  $("import-selected-count").textContent = selected;
  $("import-success-count").textContent = state.importItems.filter((item) => item.status !== "error").length;
  $("import-review-count").textContent = state.importItems.filter((item) => item.status === "error" || (item.recognized?.warnings || []).length).length;
  $("import-duplicate-count").textContent = state.importItems.filter((item) => item.duplicate_candidate_id).length;
}

async function commitResumeImport() {
  const rows = [...document.querySelectorAll("[data-import-item]")].filter((row) => row.querySelector("[data-import-select]")?.checked);
  if (!rows.length) return toast("请至少选择一位候选人", true);
  const button = $("import-commit-btn");
  button.disabled = true;
  try {
    for (const row of rows) {
      const value = (field) => row.querySelector(`[data-field="${field}"]`).value.trim();
      if (!value("name")) throw new Error("所选候选人都需要填写姓名");
      await api(`/api/v1/admin/resume-imports/${state.importBatch.id}/items/${row.dataset.importItem}`, {
        method: "PATCH", body: JSON.stringify({
          name: value("name"), phone: value("phone") || null, email: value("email") || null,
          years_experience: value("years_experience") === "" ? null : Number(value("years_experience")),
          highest_education: value("highest_education") || null, current_company: value("current_company") || null,
          current_title: value("current_title") || null, location: value("location") || null,
        }),
      });
    }
    const result = await api(`/api/v1/admin/resume-imports/${state.importBatch.id}/commit`, {
      method: "POST", body: JSON.stringify({ item_ids: rows.map((row) => row.dataset.importItem), retention_days: 120 }),
    });
    toast(`已将 ${result.created.length} 位候选人加入待排期列表`);
    await openAdminPanel();
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
}

async function closeAdminPanel() {
  if (state.currentUser && ["hr", "admin"].includes(state.currentUser.role)) {
    await showHomeView();
    return;
  }
  $("admin-panel").classList.add("hidden");
  $("welcome").querySelector(".welcome-card").classList.remove("hidden");
  setSidebarActive("home");
}

async function openReadinessCenter() {
  setSidebarActive("readiness");
  ["admin-panel", "quality-dashboard-panel", "knowledge-panel", "talent-profile-panel", "company-profile-panel", "job-center-panel", "governance-panel", "notification-panel", "report-panel", "resume-import-panel", "task-creator", "final-review-panel"].forEach((id) => $(id).classList.add("hidden"));
  $("readiness-panel").classList.remove("hidden");
  await loadReadinessCenter();
}

async function closeReadinessCenter() {
  await openAdminPanel("home");
}

async function loadReadinessCenter() {
  try {
    state.readinessCenter = await api("/api/v1/admin/readiness");
    renderReadinessCenter();
  } catch (error) { toast(error.message, true); }
}

function readinessBytes(value) {
  if (value == null) return null;
  if (value < 1024 ** 3) return `${Math.round(value / 1024 / 1024)} MB`;
  return `${(value / 1024 / 1024 / 1024).toFixed(1)} GB`;
}

function renderReadinessCenter() {
  const center = state.readinessCenter;
  if (!center) return;
  const overall = center.overall;
  const statusLabels = { ready: "已就绪", attention: "需上线调整", not_configured: "未配置", deferred: "已滞后", error: "检查失败" };
  const categoryLabels = {
    production_foundation: ["PRODUCTION FOUNDATION", "上线基础"],
    identity_and_collaboration: ["IDENTITY & COLLABORATION", "身份与协同"],
    ai_pipeline: ["AI PIPELINE", "实时智能链路"],
    knowledge_and_data: ["KNOWLEDGE & DATA", "知识与数据"],
  };
  $("readiness-hero").className = `readiness-hero ${escapeHtml(overall.status)}`;
  $("readiness-hero").innerHTML = `
    <div><span class="kicker">CURRENT STAGE</span><h3>${escapeHtml(overall.label)}</h3><p>${overall.status === "ready" ? "关键链路已达到正式试点要求，仍建议先做内部模拟面试。" : "当前本地演示不受影响；完成阻断项后再安排真实候选人试点。"}</p></div>
    <div class="readiness-progress-wrap"><strong>${overall.progress_percent}%</strong><span>正式试点完成度</span><div class="readiness-progress"><i style="width:${Number(overall.progress_percent)}%"></i></div></div>`;
  $("readiness-summary").innerHTML = `
    <article class="ready"><small>已就绪</small><strong>${center.summary.ready}</strong><span>可以继续使用</span></article>
    <article class="blocking"><small>试点阻断项</small><strong>${center.summary.blockers}</strong><span>真实试点前必须处理</span></article>
    <article class="attention"><small>上线调整项</small><strong>${center.summary.attention}</strong><span>当前可演示，生产需调整</span></article>
    <article><small>已滞后</small><strong>${center.summary.deferred}</strong><span>不阻塞首轮试点</span></article>`;
  $("readiness-role-note").textContent = center.viewer.can_run_tests
    ? "当前为管理员：可以执行无候选人数据的连接测试。"
    : "当前为 HR：可以查看就绪状态，连接测试和服务器配置由管理员完成。";
  $("readiness-actions").innerHTML = center.recommended_actions.length
    ? center.recommended_actions.map((item, index) => `<article><span>${index + 1}</span><div><strong>${escapeHtml(item.title)}</strong><p>${escapeHtml(item.action)}</p></div></article>`).join("")
    : '<div class="empty-state"><strong>当前没有阻断事项</strong><span>建议进入内部模拟面试验收。</span></div>';

  const groups = Object.entries(categoryLabels).map(([category, labels]) => {
    const items = center.checks.filter((item) => item.category === category);
    if (!items.length) return "";
    return `<section class="readiness-group">
      <div class="evaluation-heading"><div><span class="kicker">${escapeHtml(labels[0])}</span><h3>${escapeHtml(labels[1])}</h3></div><small>${items.filter((item) => item.status === "ready").length} / ${items.length} 已就绪</small></div>
      <div class="readiness-group-list">${items.map((item) => {
        const testResult = state.readinessResults[item.id];
        const storageNote = item.id === "recording_storage" ? readinessBytes(item.metadata?.free_bytes) : null;
        const fields = item.configuration.map((field) => `<span class="readiness-field ${field.configured ? "configured" : "missing"}">${escapeHtml(field.key)} · ${field.configured ? "已配置" : "缺失"}${field.secret ? " · 密钥不回显" : ""}</span>`).join("");
        return `<article class="readiness-check ${escapeHtml(item.status)}">
          <div class="readiness-check-head"><div><span class="readiness-dot"></span><div><h4>${escapeHtml(item.title)}</h4><p>${escapeHtml(item.summary)}${storageNote ? ` · 可用空间 ${escapeHtml(storageNote)}` : ""}</p></div></div><span class="readiness-status">${escapeHtml(statusLabels[item.status] || item.status)}</span></div>
          <div class="readiness-check-body"><p><strong>影响：</strong>${escapeHtml(item.impact)}</p><p><strong>下一步：</strong>${escapeHtml(item.next_action)}</p></div>
          ${fields ? `<div class="readiness-fields">${fields}</div>` : ""}
          <div class="readiness-check-actions">
            ${item.can_test ? `<button class="secondary compact" data-readiness-test="${escapeHtml(item.id)}">${item.test_kind === "connection" ? "运行连接测试" : "验证当前配置"}</button>` : `<small>${escapeHtml(item.permission_note)}</small>`}
            ${testResult ? `<span class="readiness-test-result ${escapeHtml(testResult.status)}">${escapeHtml(testResult.message)}</span>` : ""}
          </div>
        </article>`;
      }).join("")}</div>
    </section>`;
  }).join("");
  $("readiness-checks").innerHTML = groups;
  document.querySelectorAll("[data-readiness-test]").forEach((button) => button.addEventListener("click", () => testReadinessCheck(button)));
}

async function testReadinessCheck(button) {
  const checkId = button.dataset.readinessTest;
  button.disabled = true;
  const original = button.textContent;
  button.textContent = "检查中…";
  try {
    const response = await api(`/api/v1/admin/readiness/checks/${encodeURIComponent(checkId)}/test`, { method: "POST" });
    state.readinessCenter = response.readiness;
    state.readinessResults[checkId] = response.result;
    renderReadinessCenter();
    const failed = ["failed", "action_required"].includes(response.result.status);
    toast(response.result.message, failed);
  } catch (error) {
    button.disabled = false;
    button.textContent = original;
    toast(error.message, true);
  }
}

async function openQualityDashboard() {
  setSidebarActive("quality");
  $("admin-panel").classList.add("hidden");
  $("readiness-panel").classList.add("hidden");
  $("report-panel").classList.add("hidden");
  $("knowledge-panel").classList.add("hidden");
  $("talent-profile-panel").classList.add("hidden");
  $("company-profile-panel").classList.add("hidden");
  $("job-center-panel").classList.add("hidden");
  $("governance-panel").classList.add("hidden");
  $("notification-panel").classList.add("hidden");
  $("final-review-panel").classList.add("hidden");
  $("quality-dashboard-panel").classList.remove("hidden");
  try {
    state.qualityJobs = await api("/api/v1/admin/jobs");
    const select = $("quality-job-select");
    const previous = select.value;
    select.innerHTML = '<option value="">全部岗位</option>' + state.qualityJobs.map((job) => `<option value="${escapeHtml(job.id)}">${escapeHtml(job.title)}${job.source_job_code ? ` · ${escapeHtml(job.source_job_code)}` : ""}</option>`).join("");
    if (state.qualityJobs.some((job) => job.id === previous)) select.value = previous;
    await loadQualityDashboard();
  } catch (error) { toast(error.message, true); }
}

async function closeQualityDashboard() {
  await openAdminPanel("home");
}

async function loadQualityDashboard() {
  const jobId = $("quality-job-select").value;
  try {
    state.qualityOverview = await api(`/api/v1/admin/interviewer-quality/overview${jobId ? `?job_id=${encodeURIComponent(jobId)}` : ""}`);
    renderQualityDashboard();
  } catch (error) { toast(error.message, true); }
}

function qualityPercent(value) {
  return value == null ? "待积累" : `${Math.round(value * 100)}%`;
}

function renderQualityDashboard() {
  const overview = state.qualityOverview;
  if (!overview) return;
  const summary = overview.summary;
  $("quality-dashboard-summary").innerHTML = `
    <article><small>已完成面试</small><strong>${summary.completed_interviews}</strong><span>当前筛选范围</span></article>
    <article><small>完成质量复盘</small><strong>${qualityPercent(summary.review_completion_rate)}</strong><span>${summary.reviewed_interviews} 场已人工复核</span></article>
    <article><small>统一问题覆盖</small><strong>${qualityPercent(summary.average_required_question_coverage)}</strong><span>岗位流程标准执行情况</span></article>
    <article><small>候选人表达占比</small><strong>${qualityPercent(summary.average_candidate_talk_share)}</strong><span>需先确认说话人准确性</span></article>
    <article><small>异常信号场次</small><strong>${summary.flagged_interviews}</strong><span>${summary.interviewer_count} 位面试官有样本</span></article>`;

  const riskLabels = { insufficient_sample: "样本不足", needs_attention: "建议辅导", stable: "趋势稳定" };
  const ratingLabels = { preparation: "准备", question_quality: "提问", listening: "倾听", fairness: "公平" };
  $("quality-interviewer-list").innerHTML = overview.interviewers.length
    ? overview.interviewers.map((item) => {
        const ratings = Object.entries(item.ai_rating_averages || {}).map(([key, value]) => `${ratingLabels[key] || key} ${value}`).join(" · ");
        const coaching = item.coaching_signals.length ? item.coaching_signals.join(" ") : item.sample_warning || "当前未发现集中流程异常，继续结合招聘结果观察。";
        const riskClass = item.risk_level === "needs_attention" ? "attention" : item.risk_level === "insufficient_sample" ? "observe" : "";
        return `<article class="quality-interviewer-card ${item.risk_level}">
          <div class="quality-interviewer-head"><div><h4>${escapeHtml(item.display_name)}</h4><small>${escapeHtml(Object.entries(item.round_distribution).map(([round, count]) => `${({ business: "业务面", hr: "HR 面", ceo: "CEO 面" })[round] || round} ${count} 场`).join(" · "))}</small></div><span class="quality-risk ${riskClass}">${escapeHtml(riskLabels[item.risk_level])}</span></div>
          <div class="quality-kpis"><div><small>面试样本</small><strong>${item.interview_count}</strong></div><div><small>必问题覆盖</small><strong>${qualityPercent(item.average_required_question_coverage)}</strong></div><div><small>候选人表达</small><strong>${qualityPercent(item.average_candidate_talk_share)}</strong></div><div><small>证据密度</small><strong>${item.average_evidence_density ?? "待积累"}</strong></div><div><small>HR 已复核</small><strong>${item.reviewed_count}</strong></div></div>
          ${ratings ? `<div class="quality-rating-meta">AI 质量评分均值：${escapeHtml(ratings)}</div>` : ""}
          <div class="quality-coaching ${item.risk_level === "stable" ? "stable" : ""}"><strong>${item.risk_level === "needs_attention" ? "建议动作：" : "观察说明："}</strong>${escapeHtml(coaching)}</div>
        </article>`;
      }).join("")
    : '<div class="empty-state"><strong>还没有已完成面试样本</strong><span>完成面试并进行质量复盘后，这里会形成面试官趋势。</span></div>';

  $("quality-job-list").innerHTML = overview.jobs.length
    ? overview.jobs.map((item) => `<article class="quality-job-card">
        <div><h4>${escapeHtml(item.job_title)}</h4><p>${item.application_count} 位候选人 · ${item.completed_interviews} 场已完成面试</p></div>
        <div class="quality-job-stats"><span>质量复盘 ${item.reviewed_interviews}</span><span>异常场次 ${item.flagged_interviews}</span><span>必问题覆盖 ${qualityPercent(item.average_required_question_coverage)}</span><span>录用审批 ${item.offer_approval_count}</span></div>
        <div><p class="quality-diagnosis">${escapeHtml(item.diagnosis)}</p>${overview.filters.job_id ? "" : `<button class="text-button" data-quality-job="${escapeHtml(item.job_id)}">只看该岗位</button>`}</div>
      </article>`).join("")
    : '<div class="empty-state"><strong>暂无可诊断岗位</strong><span>至少完成一场非演示岗位面试后开始积累。</span></div>';
  document.querySelectorAll("[data-quality-job]").forEach((button) => button.addEventListener("click", async () => { $("quality-job-select").value = button.dataset.qualityJob; await loadQualityDashboard(); }));
}

async function openKnowledgePanel() {
  setSidebarActive("knowledge");
  $("admin-panel").classList.add("hidden");
  $("readiness-panel").classList.add("hidden");
  $("quality-dashboard-panel").classList.add("hidden");
  $("report-panel").classList.add("hidden");
  $("governance-panel").classList.add("hidden");
  $("notification-panel").classList.add("hidden");
  $("final-review-panel").classList.add("hidden");
  $("knowledge-panel").classList.remove("hidden");
  await loadKnowledgeCenter();
}

async function closeKnowledgePanel() {
  await openAdminPanel("home");
}

async function openTalentProfilePanel(preferredJobId = null) {
  setSidebarActive("talent-profile");
  $("admin-panel").classList.add("hidden");
  $("readiness-panel").classList.add("hidden");
  $("knowledge-panel").classList.add("hidden");
  $("quality-dashboard-panel").classList.add("hidden");
  $("report-panel").classList.add("hidden");
  $("governance-panel").classList.add("hidden");
  $("notification-panel").classList.add("hidden");
  $("job-center-panel").classList.add("hidden");
  $("final-review-panel").classList.add("hidden");
  $("talent-profile-panel").classList.remove("hidden");
  $("company-profile-panel").classList.add("hidden");
  try {
    state.profileJobs = await api("/api/v1/admin/jobs");
    const select = $("profile-job-select");
    const previous = select.value;
    select.innerHTML = state.profileJobs.map((job) => `<option value="${escapeHtml(job.id)}">${escapeHtml(job.title)}${job.source_job_code ? ` · ${escapeHtml(job.source_job_code)}` : ""}</option>`).join("");
    if (preferredJobId && state.profileJobs.some((job) => job.id === preferredJobId)) select.value = preferredJobId;
    else if (state.profileJobs.some((job) => job.id === previous)) select.value = previous;
    if (select.value) await loadTalentProfileCenter();
    else $("profile-content").innerHTML = '<div class="empty-state"><strong>还没有可维护的岗位</strong><span>请先到“岗位与 JD”建立新岗位并保存 JD。</span></div>';
  } catch (error) { toast(error.message, true); }
}

async function closeTalentProfilePanel() {
  clearHistoricalImport();
  await openAdminPanel("home");
}

async function openGovernanceCenter() {
  setSidebarActive("governance");
  ["admin-panel", "readiness-panel", "knowledge-panel", "talent-profile-panel", "company-profile-panel", "job-center-panel", "quality-dashboard-panel", "notification-panel", "report-panel", "resume-import-panel", "task-creator", "final-review-panel"].forEach((id) => $(id).classList.add("hidden"));
  $("governance-panel").classList.remove("hidden");
  await loadGovernanceCenter();
}

async function closeGovernanceCenter() {
  await openAdminPanel("home");
}

async function loadGovernanceCenter() {
  try {
    state.governanceCenter = await api("/api/v1/admin/governance");
    renderGovernanceCenter();
  } catch (error) { toast(error.message, true); }
}

function governanceBytes(value) {
  if (!value) return "0 KB";
  if (value < 1024 * 1024) return `${Math.max(1, Math.round(value / 1024))} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}

function updateGovernanceCleanupState() {
  const selected = document.querySelectorAll("[data-governance-candidate]:checked").length;
  $("governance-cleanup-btn").disabled = !$("governance-confirm").checked || selected === 0;
}

function renderGovernanceCenter() {
  const center = state.governanceCenter;
  const summary = center.summary;
  const policy = center.policy;
  $("governance-policy-hint").textContent = `默认保留 ${policy.default_retention_days} 天，最长 ${policy.maximum_retention_days} 天；首版不启用自动清理。`;
  $("governance-delete-scope").textContent = policy.deleted_scope.join("、");
  $("governance-preserve-scope").textContent = policy.preserved_scope.join("、");
  $("governance-summary").innerHTML = `
    <article><small>已建档候选人</small><strong>${summary.candidate_count}</strong><span>不含演示数据</span></article>
    <article class="${summary.cleanup_due ? "attention" : ""}"><small>当前待清理</small><strong>${summary.cleanup_due}</strong><span>候选人材料包或录音到期</span></article>
    <article><small>30 天内到期</small><strong>${summary.expiring_soon}</strong><span>可提前安排核对</span></article>
    <article><small>待释放录音空间</small><strong>${governanceBytes(summary.sensitive_bytes_due)}</strong><span>仅统计已到期录音</span></article>
    <article><small>已完成清理</small><strong>${summary.cleaned}</strong><span>结构化结果仍保留</span></article>`;

  const visibleItems = center.items.filter((item) => item.status !== "active");
  const statusLabels = { expired: "材料包已到期", recording_due: "录音已到期", expiring_soon: "30 天内到期", cleaned: "已清理" };
  $("governance-items").innerHTML = visibleItems.length ? visibleItems.map((item) => `
    <article class="governance-item ${item.can_cleanup ? "due" : item.status === "expiring_soon" ? "soon" : ""}">
      <input data-governance-candidate type="checkbox" value="${escapeHtml(item.candidate_id)}" ${item.can_cleanup ? "checked" : "disabled"} aria-label="选择 ${escapeHtml(item.candidate_name)}" />
      <div><h4>${escapeHtml(item.candidate_name)}</h4><p>${escapeHtml(item.job_titles.join("、") || "岗位待补充")} · ${item.round_count} 轮面试</p><small>档案期限：${new Date(item.retention_until).toLocaleString("zh-CN")}</small></div>
      <div class="governance-artifacts"><span>录音 ${item.recording_count}</span><span>已到期录音 ${item.expired_recording_count}</span><span>逐字稿 ${item.transcript_segment_count} 段</span><span>证据 ${item.evidence_count} 条</span></div>
      <span class="governance-status ${escapeHtml(item.status)}">${escapeHtml(statusLabels[item.status] || item.status)}</span>
    </article>`).join("") : '<div class="empty-state"><strong>目前没有到期或临期材料</strong><span>系统不会自动删除；到期后会先出现在这里等待 HR 核对。</span></div>';

  const actionLabels = {
    "job.created_from_jd": "新建岗位并保存 JD",
    "job.jd_updated": "更新岗位 JD",
    "application.final_decision": "确认候选人最终流程决定",
    "report.locked": "锁定面试报告版本",
    "retention.cleanup_executed": "执行到期材料清理",
    "retention.sensitive_artifacts_cleaned": "清理候选人敏感原始材料",
    "retention.recordings_cleaned": "清理到期录音",
    "transcript.downloaded": "下载逐字稿",
    "recording.downloaded": "下载面试录音",
    "notification.dispatch_executed": "发送飞书通知",
    "system_docs.synced": "同步系统使用与运维文档",
    "company_profile.draft_saved": "保存公司基础画像草稿",
    "company_profile.activated": "启用公司基础人才画像",
    "readiness.connection_tested": "执行试点就绪连接测试",
  };
  $("governance-audit-list").innerHTML = center.audit_events.length ? center.audit_events.map((event) => `
    <article class="governance-audit"><div><strong>${escapeHtml(actionLabels[event.action] || event.action)}</strong><span>${escapeHtml(event.actor.display_name)} · ${escapeHtml(event.actor.role)}</span></div><small>${escapeHtml(event.resource_type)} · ${escapeHtml(event.resource_id)}</small><time>${new Date(event.created_at).toLocaleString("zh-CN")}</time></article>`).join("") : '<div class="empty-state"><strong>还没有审计记录</strong><span>报告锁定、最终决定、录音/逐字稿下载和清理操作会记录在这里。</span></div>';
  $("governance-confirm").checked = false;
  document.querySelectorAll("[data-governance-candidate]").forEach((input) => input.addEventListener("change", updateGovernanceCleanupState));
  updateGovernanceCleanupState();
}

async function executeGovernanceCleanup() {
  const candidateIds = [...document.querySelectorAll("[data-governance-candidate]:checked")].map((input) => input.value);
  if (!candidateIds.length) return toast("请先选择需要清理的到期材料", true);
  if (!$("governance-confirm").checked) return toast("请先完成 HR 人工核对确认", true);
  const button = $("governance-cleanup-btn");
  button.disabled = true;
  try {
    const result = await api("/api/v1/admin/governance/retention/execute", {
      method: "POST",
      body: JSON.stringify({ candidate_ids: candidateIds, confirmed_by_hr: true }),
    });
    state.governanceCenter = result.governance;
    renderGovernanceCenter();
    toast(`清理完成：${result.summary.recordings_deleted} 份录音、${result.summary.transcript_segments_deleted} 段逐字稿；评分与人工决定已保留`);
  } catch (error) { toast(error.message, true); updateGovernanceCleanupState(); }
}

async function openNotificationCenter() {
  setSidebarActive("notifications");
  ["admin-panel", "readiness-panel", "knowledge-panel", "talent-profile-panel", "company-profile-panel", "job-center-panel", "quality-dashboard-panel", "governance-panel", "report-panel", "resume-import-panel", "task-creator", "final-review-panel"].forEach((id) => $(id).classList.add("hidden"));
  $("notification-panel").classList.remove("hidden");
  await syncNotificationQueue();
}

async function closeNotificationCenter() {
  await openAdminPanel("home");
}

async function syncNotificationQueue() {
  try {
    const result = await api("/api/v1/admin/notifications/sync", { method: "POST" });
    state.notificationCenter = result.queue;
    renderNotificationCenter();
    if (result.created) toast(`已新增 ${result.created} 条通知草稿，尚未发送`);
  } catch (error) { toast(error.message, true); }
}

async function loadNotificationCenter() {
  try {
    state.notificationCenter = await api("/api/v1/admin/notifications");
    renderNotificationCenter();
  } catch (error) { toast(error.message, true); }
}

function updateNotificationSendState() {
  const selected = document.querySelectorAll("[data-notification-select]:checked").length;
  const ready = state.notificationCenter?.integration?.sending_enabled;
  $("notification-send-btn").disabled = !ready || !$("notification-confirm").checked || selected === 0;
}

function renderNotificationCenter() {
  const center = state.notificationCenter;
  const ready = center.integration.sending_enabled;
  const status = $("notification-integration-status");
  status.className = `pill ${ready ? "ready" : "warning"}`;
  status.textContent = ready ? "飞书消息已就绪" : "飞书消息未配置 · 仅预览";
  $("notification-summary").innerHTML = `
    <article class="${center.summary.due ? "attention" : ""}"><small>当前待发送</small><strong>${center.summary.due}</strong><span>已到发送时间</span></article>
    <article><small>预约发送</small><strong>${center.summary.scheduled}</strong><span>尚未到提醒时间</span></article>
    <article><small>发送成功</small><strong>${center.summary.sent}</strong><span>飞书已返回消息编号</span></article>
    <article><small>发送失败</small><strong>${center.summary.failed}</strong><span>可核对配置后重试</span></article>`;
  const labels = {
    interview_assigned: "新排期", interview_reminder: "面试提醒", feedback_due: "待补评价",
    final_review_ready: "待 HR 终审", knowledge_approval: "知识待审批",
  };
  const statusLabels = { queued: "待发送", sent: "已发送", failed: "发送失败" };
  $("notification-items").innerHTML = center.items.length ? center.items.map((item) => {
    const canSelect = item.status !== "sent" && item.is_due;
    return `<article class="notification-item ${item.status === "failed" ? "failed" : canSelect ? "due" : ""}">
      <input data-notification-select type="checkbox" value="${escapeHtml(item.id)}" ${canSelect ? "checked" : "disabled"} aria-label="选择通知：${escapeHtml(item.title)}" />
      <div class="notification-recipient"><span class="pill">${escapeHtml(labels[item.notification_type] || item.notification_type)}</span><h4>${escapeHtml(item.recipient.display_name)}</h4><small>${escapeHtml(item.recipient.role)} · ${new Date(item.scheduled_for).toLocaleString("zh-CN")}</small></div>
      <div><h4>${escapeHtml(item.title)}</h4><p>${escapeHtml(item.message)}</p>${item.error_message ? `<small class="notification-error">${escapeHtml(item.error_message)}</small>` : ""}</div>
      <span class="notification-status ${escapeHtml(item.status)}">${item.status === "queued" && !item.is_due ? "预约中" : escapeHtml(statusLabels[item.status] || item.status)}</span>
    </article>`;
  }).join("") : '<div class="empty-state"><strong>没有需要通知的事项</strong><span>新排期、面试提醒、待补评价、HR 终审和知识审批会自动形成草稿。</span></div>';
  $("notification-confirm").checked = false;
  document.querySelectorAll("[data-notification-select]").forEach((input) => input.addEventListener("change", updateNotificationSendState));
  updateNotificationSendState();
}

async function sendSelectedNotifications() {
  const ids = [...document.querySelectorAll("[data-notification-select]:checked")].map((input) => input.value);
  if (!ids.length) return toast("请选择已经到发送时间的通知", true);
  if (!$("notification-confirm").checked) return toast("请先核对并确认发送", true);
  const button = $("notification-send-btn");
  button.disabled = true;
  try {
    const result = await api("/api/v1/admin/notifications/dispatch", {
      method: "POST",
      body: JSON.stringify({ notification_ids: ids, confirmed_by_hr: true }),
    });
    state.notificationCenter = result.queue;
    renderNotificationCenter();
    toast(`飞书发送完成：成功 ${result.sent} 条，失败 ${result.failed} 条`);
  } catch (error) { toast(error.message, true); updateNotificationSendState(); }
}

function companyLines(value) {
  return String(value || "").split(/[\n；;]+/).map((item) => item.trim()).filter(Boolean);
}

function companyKeywords(value) {
  return String(value || "").split(/[\n,，、；;]+/).map((item) => item.trim()).filter(Boolean);
}

function blankCompanyCompetency() {
  return {
    competency_id: null,
    name: "",
    definition: "",
    positive_evidence: [""],
    risk_signals: [""],
    required_question: "",
    follow_up: "",
    primary_round: "hr",
    keywords: [],
    score_anchors: { "1": "", "3": "", "5": "" },
  };
}

function renderCompanyCompetencyEditor(items) {
  $("company-competency-list").innerHTML = items.map((item, index) => `
    <article class="company-competency-card" data-company-competency data-competency-id="${escapeHtml(item.competency_id || "")}">
      <div class="company-competency-card-head"><div><b>${index + 1}</b><strong>${escapeHtml(item.name || "待填写能力")}</strong></div><button class="text-button" type="button" data-company-competency-remove="${index}" ${items.length <= 3 ? "disabled" : ""}>移除此项</button></div>
      <div class="company-competency-grid">
        <label>能力名称<input data-company-field="name" maxlength="64" value="${escapeHtml(item.name || "")}" placeholder="例如：责任担当" required /></label>
        <label>主要验证轮次<select data-company-field="primary_round"><option value="business" ${item.primary_round === "business" ? "selected" : ""}>业务面</option><option value="hr" ${item.primary_round === "hr" ? "selected" : ""}>HR 面</option><option value="ceo" ${item.primary_round === "ceo" ? "selected" : ""}>CEO 面</option></select></label>
        <label class="wide">能力定义<textarea data-company-field="definition" rows="2" placeholder="描述与工作成功有关的可观察行为" required>${escapeHtml(item.definition || "")}</textarea></label>
        <label>正向证据 <span class="field-hint">每行一条</span><textarea data-company-field="positive_evidence" rows="3" required>${escapeHtml((item.positive_evidence || []).join("\n"))}</textarea></label>
        <label>风险信号 <span class="field-hint">每行一条</span><textarea data-company-field="risk_signals" rows="3" required>${escapeHtml((item.risk_signals || []).join("\n"))}</textarea></label>
        <label class="wide">所有候选人的统一问题<textarea data-company-field="required_question" rows="2" required>${escapeHtml(item.required_question || "")}</textarea></label>
        <label>证据型追问<textarea data-company-field="follow_up" rows="2" required>${escapeHtml(item.follow_up || "")}</textarea></label>
        <label>识别关键词 <span class="field-hint">逗号或换行分隔</span><textarea data-company-field="keywords" rows="2">${escapeHtml((item.keywords || []).join("、"))}</textarea></label>
        <div class="company-score-anchors">
          <label>1分表现<input data-company-field="score_1" value="${escapeHtml(item.score_anchors?.["1"] || "")}" required /></label>
          <label>3分表现<input data-company-field="score_3" value="${escapeHtml(item.score_anchors?.["3"] || "")}" required /></label>
          <label>5分表现<input data-company-field="score_5" value="${escapeHtml(item.score_anchors?.["5"] || "")}" required /></label>
        </div>
      </div>
    </article>`).join("");
  document.querySelectorAll("[data-company-competency-remove]").forEach((button) => button.addEventListener("click", () => {
    const current = readCompanyCompetencies(false);
    if (current.length <= 3) return toast("公司通用能力至少保留 3 项", true);
    current.splice(Number(button.dataset.companyCompetencyRemove), 1);
    renderCompanyCompetencyEditor(current);
  }));
}

function readCompanyCompetencies(validate = true) {
  const items = [...document.querySelectorAll("[data-company-competency]")].map((card) => {
    const value = (name) => card.querySelector(`[data-company-field="${name}"]`).value.trim();
    return {
      competency_id: card.dataset.competencyId || null,
      name: value("name"),
      definition: value("definition"),
      positive_evidence: companyLines(value("positive_evidence")),
      risk_signals: companyLines(value("risk_signals")),
      required_question: value("required_question"),
      follow_up: value("follow_up"),
      primary_round: value("primary_round"),
      keywords: companyKeywords(value("keywords")),
      score_anchors: { "1": value("score_1"), "3": value("score_3"), "5": value("score_5") },
    };
  });
  if (validate) {
    if (items.length < 3 || items.length > 8) throw new Error("请设置 3–8 项公司通用能力");
    items.forEach((item, index) => {
      if (!item.name || !item.definition || !item.required_question || !item.follow_up) throw new Error(`请补全第 ${index + 1} 项能力的名称、定义、统一问题和追问`);
      if (!item.positive_evidence.length || !item.risk_signals.length) throw new Error(`请补全“${item.name}”的正向证据和风险信号`);
      if (!item.score_anchors["1"] || !item.score_anchors["3"] || !item.score_anchors["5"]) throw new Error(`请补全“${item.name}”的 1、3、5 分标准`);
    });
  }
  return items;
}

async function openCompanyProfilePanel() {
  setSidebarActive("company-profile");
  ["admin-panel", "readiness-panel", "knowledge-panel", "talent-profile-panel", "job-center-panel", "quality-dashboard-panel", "governance-panel", "notification-panel", "report-panel", "resume-import-panel", "task-creator", "final-review-panel"].forEach((id) => $(id).classList.add("hidden"));
  $("company-profile-panel").classList.remove("hidden");
  await loadCompanyProfileCenter();
}

async function closeCompanyProfilePanel() {
  await openAdminPanel("home");
}

async function loadCompanyProfileCenter() {
  try {
    state.companyProfileCenter = await api("/api/v1/admin/company-profile");
    renderCompanyProfileCenter();
  } catch (error) { toast(error.message, true); }
}

function renderCompanyProfileCenter() {
  const center = state.companyProfileCenter;
  const active = center.active_version;
  const draft = center.draft_version;
  const editor = center.editor_payload;
  const activeCompetencies = active?.profile_payload?.competencies || [];
  const roundCounts = { business: 0, hr: 0, ceo: 0 };
  activeCompetencies.forEach((item) => { roundCounts[item.primary_round] = (roundCounts[item.primary_round] || 0) + 1; });
  $("company-profile-status").innerHTML = `
    <article class="knowledge-status-card ${active ? "ready" : "warning"}"><small>当前生效版本</small><strong>${escapeHtml(active?.version_label || "尚未建立")}</strong><span>${active ? escapeHtml(active.company_name) : "先编辑并保存首版草稿"}</span></article>
    <article class="knowledge-status-card ${draft ? "warning" : ""}"><small>待审核草稿</small><strong>${escapeHtml(draft?.version_label || "无")}</strong><span>${draft ? "保存不会自动生效" : "当前没有待审批更新"}</span></article>
    <article class="knowledge-status-card"><small>通用能力</small><strong>${activeCompetencies.length || editor.competencies.length}</strong><span>建议 5–7 项</span></article>
    <article class="knowledge-status-card"><small>轮次分工</small><strong>${roundCounts.business || 0} / ${roundCounts.hr || 0} / ${roundCounts.ceo || 0}</strong><span>业务面 / HR 面 / CEO 面</span></article>`;

  $("company-profile-name").value = editor.company_name || "";
  $("company-profile-purpose").value = editor.profile_purpose || "";
  $("company-profile-red-lines").value = (editor.red_lines || []).join("\n");
  $("company-profile-change-summary").value = draft?.change_summary || (active ? `基于 ${active.version_label} 更新公司通用标准` : "根据 HR 用人原则建立首版公司基础人才画像");
  renderCompanyCompetencyEditor(editor.competencies || []);

  const card = (version, isDraft) => {
    if (!version) return `<article class="profile-version empty"><strong>${isDraft ? "暂无待审核草稿" : "暂无生效画像"}</strong><span>${isDraft ? "在上方完成编辑并保存。" : "只有 HR 审核生效后，才会进入岗位画像与面试题。"}</span></article>`;
    const profile = version.profile_payload;
    return `<article class="profile-version company-version-card ${version.status}">
      <div class="profile-version-head"><div><span class="pill">${escapeHtml(version.version_label)}</span><h3>${isDraft ? "待审核公司画像" : "当前公司基础画像"}</h3></div><strong>${version.source_mode === "hr_manual" ? "HR 首版" : "HR 修订"}</strong></div>
      <p>${escapeHtml(profile.profile_purpose || "")}</p>
      <div class="profile-change"><strong>本版说明</strong><span>${escapeHtml(version.change_summary)}</span></div>
      <div class="profile-competencies">${(profile.competencies || []).map((item) => `<span><small>${escapeHtml(({ business: "业务面", hr: "HR 面", ceo: "CEO 面" })[item.primary_round])}</small>${escapeHtml(item.name)}</span>`).join("")}</div>
      <div class="company-version-meta"><span>${(profile.competencies || []).length} 项通用能力</span><span>${(profile.red_lines || []).length} 条公司红线</span><span>${version.approved_by ? `审批人：${escapeHtml(version.approved_by)}` : "等待 HR 审批"}</span></div>
      <div class="profile-actions">
        ${isDraft ? `<button class="primary compact" data-company-profile-activate="${escapeHtml(version.id)}">审核并设为公司标准</button>` : ""}
        ${version.publication.status === "failed" ? `<button class="secondary compact" data-company-profile-publish="${escapeHtml(version.id)}">重试发布到 Obsidian</button><small class="knowledge-error">${escapeHtml(version.publication.error_message)}</small>` : ""}
        ${version.publication.obsidian_uri ? `<a class="secondary compact link-button" href="${escapeHtml(version.publication.obsidian_uri)}">在 Obsidian 查看</a>` : ""}
      </div>
    </article>`;
  };
  $("company-profile-versions").innerHTML = `
    <div class="profile-columns">${card(active, false)}${card(draft, true)}</div>
    <section class="profile-history"><div class="evaluation-heading"><div><span class="kicker">AUDIT TRAIL</span><h3>版本记录</h3></div></div>${center.versions.map((item) => `<div><strong>${escapeHtml(item.version_label)}</strong><span>${escapeHtml(({ draft: "待审核", active: "当前生效", superseded: "历史版本" })[item.status] || item.status)}</span><small>${escapeHtml(item.change_summary)}</small></div>`).join("") || '<p class="muted">保存第一版草稿后，这里会保留版本记录。</p>'}</section>`;
  document.querySelectorAll("[data-company-profile-activate]").forEach((button) => button.addEventListener("click", () => activateCompanyProfile(button.dataset.companyProfileActivate)));
  document.querySelectorAll("[data-company-profile-publish]").forEach((button) => button.addEventListener("click", () => retryCompanyProfilePublish(button.dataset.companyProfilePublish)));
}

async function saveCompanyProfileDraft(event) {
  event.preventDefault();
  try {
    const companyName = $("company-profile-name").value.trim();
    const profilePurpose = $("company-profile-purpose").value.trim();
    const changeSummary = $("company-profile-change-summary").value.trim();
    if (!companyName || profilePurpose.length < 10 || changeSummary.length < 5) throw new Error("请填写公司名称、画像用途和本次版本说明");
    const version = await api("/api/v1/admin/company-profile/draft", {
      method: "PUT",
      body: JSON.stringify({
        company_name: companyName,
        profile_purpose: profilePurpose,
        competencies: readCompanyCompetencies(true),
        red_lines: companyLines($("company-profile-red-lines").value),
        change_summary: changeSummary,
      }),
    });
    toast(`${version.version_label} 已保存为草稿，尚未影响正式面试`);
    await loadCompanyProfileCenter();
  } catch (error) { toast(error.message, true); }
}

async function activateCompanyProfile(versionId) {
  if (!window.confirm("确认将这份草稿设为全公司的当前人才标准？尚未开始的面试将刷新公司统一问题，进行中和已完成面试保持原标准；所有岗位画像会生成继承更新草稿。")) return;
  try {
    const result = await api(`/api/v1/admin/company-profile/versions/${versionId}/activate`, {
      method: "POST",
      body: JSON.stringify({ confirmed_by_hr: true }),
    });
    const publishText = result.publication.status === "published" ? "并已发布到 Obsidian" : "，Obsidian 发布待重试";
    toast(`${result.version_label} 已生效${publishText}；刷新 ${result.refreshed_interviews} 场待进行面试`);
    await loadCompanyProfileCenter();
  } catch (error) { toast(error.message, true); }
}

async function retryCompanyProfilePublish(versionId) {
  try {
    const result = await api(`/api/v1/admin/company-profile/versions/${versionId}/publish`, { method: "POST" });
    toast(result.publication.status === "published" ? "公司基础画像已发布到 Obsidian" : result.publication.error_message, result.publication.status !== "published");
    await loadCompanyProfileCenter();
  } catch (error) { toast(error.message, true); }
}

async function loadTalentProfileCenter() {
  const jobId = $("profile-job-select").value;
  if (!jobId) return;
  try {
    state.profileCenter = await api(`/api/v1/admin/jobs/${jobId}/talent-profile`);
    renderTalentProfileCenter();
  } catch (error) { toast(error.message, true); }
}

function renderTalentProfileCenter() {
  const center = state.profileCenter;
  const samples = center.outcome_samples;
  const active = center.active_version;
  const draft = center.draft_version;
  $("profile-status").innerHTML = `
    <article class="knowledge-status-card ${active ? "ready" : "warning"}"><small>当前生效版本</small><strong>${escapeHtml(active?.version_label || "尚未建立")}</strong><span>${active ? "已由 HR 审核" : "建议先生成 JD 基线"}</span></article>
    <article class="knowledge-status-card"><small>有效画像样本</small><strong>${samples.eligible_offer_samples} / ${samples.minimum_outcome_samples}</strong><span>${samples.threshold_met ? "达到更新建议门槛" : "继续积累，不自动调整画像"}</span></article>
    <article class="knowledge-status-card"><small>历史成功样本</small><strong>${samples.historical_positive_samples || 0}</strong><span>${samples.performance_validated_samples || 0} 份已有试用期验证</span></article>
    <article class="knowledge-status-card"><small>版本记录</small><strong>${center.versions.length}</strong><span>草稿、生效与历史版本均保留</span></article>`;

  const renderProfile = (version, isDraft) => {
    if (!version) return `<article class="profile-version empty"><strong>${isDraft ? "暂无待审核草稿" : "暂无生效画像"}</strong><span>${isDraft ? "点击上方按钮生成第一版。" : "首版必须由 HR 人工确认后才能生效。"}</span></article>`;
    const profile = version.profile_payload;
    const mustHave = profile.must_have || [];
    const companyFoundation = profile.company_foundation;
    const activationBlocked = version.source_mode === "outcome_aggregation" && !version.evidence_summary.threshold_met;
    return `<article class="profile-version ${version.status}">
      <div class="profile-version-head"><div><span class="pill">${escapeHtml(version.version_label)}</span><h3>${isDraft ? "待审核画像草稿" : "当前生效画像"}</h3></div><strong>${version.source_mode === "jd_baseline" ? "JD 基线" : version.source_mode === "jd_revision" ? "JD 更新" : version.source_mode === "company_inheritance" ? "公司标准继承" : "样本更新"}</strong></div>
      <p>${escapeHtml(profile.summary || "")}</p>
      <div class="profile-change"><strong>本版说明</strong><span>${escapeHtml(version.change_summary)}</span></div>
      ${companyFoundation ? `<div class="company-profile-path"><strong>继承公司标准</strong><span>${escapeHtml(companyFoundation.company_name)} · ${escapeHtml(companyFoundation.version_label)} · ${(companyFoundation.competencies || []).length} 项公司通用能力</span></div>` : '<div class="knowledge-boundary"><strong>尚未继承</strong><span>公司基础画像尚未生效；岗位画像当前只使用 JD 与分轮能力项。</span></div>'}
      <div class="profile-competencies">${mustHave.slice(0, 8).map((item) => `<span><small>${escapeHtml(({ business: "业务面", hr: "HR 面", ceo: "CEO 面", custom: "自定义" })[item.round_type] || item.round_type)}</small>${escapeHtml(item.competency_name)}</span>`).join("")}</div>
      <div class="profile-outcomes"><strong>岗位成功结果</strong><ul>${(profile.success_outcomes || []).map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul></div>
      ${profile.observed_signals?.length ? `<div class="profile-observed"><strong>达到门槛后的样本观察</strong>${profile.observed_signals.map((item) => `<span>${escapeHtml(item.competency_name)} · ${item.observation_count} 次观察${item.average_human_score == null ? "" : ` · 人工均分 ${item.average_human_score}`}${item.historical_signal_count ? ` · 历史信号 ${item.historical_signal_count}` : ""}</span>`).join("")}</div>` : ""}
      <div class="profile-actions">
        ${isDraft ? `<button class="primary compact" data-profile-activate="${escapeHtml(version.id)}" ${activationBlocked ? "disabled" : ""}>审核并生效</button>${activationBlocked ? `<small>还需 ${Math.max(0, version.evidence_summary.minimum_outcome_samples - version.evidence_summary.eligible_offer_samples)} 份有效录用/历史样本</small>` : ""}` : ""}
        ${version.publication.status === "failed" ? `<button class="secondary compact" data-profile-publish="${escapeHtml(version.id)}">重试发布到 Obsidian</button><small class="knowledge-error">${escapeHtml(version.publication.error_message)}</small>` : ""}
        ${version.publication.obsidian_uri ? `<a class="secondary compact link-button" href="${escapeHtml(version.publication.obsidian_uri)}">在 Obsidian 查看</a>` : ""}
      </div>
    </article>`;
  };
  $("profile-content").innerHTML = `
    <div class="profile-columns">${renderProfile(active, false)}${renderProfile(draft, true)}</div>
    <section class="profile-history"><div class="evaluation-heading"><div><span class="kicker">AUDIT TRAIL</span><h3>版本记录</h3></div></div>${center.versions.map((item) => `<div><strong>${escapeHtml(item.version_label)}</strong><span>${escapeHtml(({ draft: "待审核", active: "当前生效", superseded: "历史版本" })[item.status] || item.status)}</span><small>${escapeHtml(item.change_summary)}</small></div>`).join("") || '<p class="muted">暂无版本记录。</p>'}</section>`;
  document.querySelectorAll("[data-profile-activate]").forEach((button) => button.addEventListener("click", () => activateTalentProfile(button.dataset.profileActivate)));
  document.querySelectorAll("[data-profile-publish]").forEach((button) => button.addEventListener("click", () => retryTalentProfilePublish(button.dataset.profilePublish)));
}

async function generateTalentProfileDraft() {
  const jobId = $("profile-job-select").value;
  if (!jobId) return;
  try {
    const result = await api(`/api/v1/admin/jobs/${jobId}/talent-profile/draft`, { method: "POST" });
    toast(result.draft_changed ? `${result.version_label} 草稿已生成，等待 HR 审核` : "样本没有变化，已保留现有草稿");
    await loadTalentProfileCenter();
  } catch (error) { toast(error.message, true); }
}

function clearHistoricalImport() {
  state.historicalPreview = null;
  const input = $("profile-history-file");
  if (input) input.value = "";
  $("historical-import-review")?.classList.add("hidden");
}

async function beginHistoricalImport(file) {
  const jobId = $("profile-job-select").value;
  if (!jobId || !file) return;
  try {
    const response = await fetch(`/api/v1/admin/historical-samples/preview?job_id=${encodeURIComponent(jobId)}`, {
      method: "POST",
      headers: { "Content-Type": "application/octet-stream", "X-Filename": encodeURIComponent(file.name) },
      body: file,
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "历史样本解析失败");
    state.historicalPreview = data;
    renderHistoricalImport();
    toast(`已完成 ${data.summary.total_rows} 行脱敏预览，请核对后确认`);
  } catch (error) {
    clearHistoricalImport();
    toast(error.message, true);
  }
}

function renderHistoricalImport() {
  const preview = state.historicalPreview;
  if (!preview) return;
  $("historical-import-review").classList.remove("hidden");
  $("historical-import-summary").innerHTML = `
    <div><small>文件数据</small><strong>${preview.summary.total_rows}</strong></div>
    <div><small>默认有效</small><strong>${preview.summary.eligible_rows}</strong></div>
    <div><small>需要核对</small><strong>${preview.summary.needs_review}</strong></div>
    <div><small>忽略敏感列</small><strong>${preview.summary.ignored_pii_columns}</strong></div>`;
  const mapping = preview.mapping;
  $("historical-import-mapping").innerHTML = `
    <span>结果列：${escapeHtml(mapping.outcome || "未识别")}</span>
    <span>姓名列：${escapeHtml(mapping.name || "无（不影响导入）")}</span>
    <span>评价列：${escapeHtml((mapping.evaluation_columns || []).join("、") || "无")}</span>
    <span>已忽略：${escapeHtml((mapping.ignored_pii_columns || []).join("、") || "无敏感列")}</span>`;
  $("historical-import-privacy").textContent = preview.privacy;
  $("historical-import-rows").innerHTML = preview.items.map((item, index) => {
    const signals = item.competency_signals.length
      ? item.competency_signals.map((signal) => `<span class="${signal.direction === "negative" ? "negative" : ""}">${escapeHtml(signal.competency_name)} · ${escapeHtml(({ positive: "正向", negative: "风险", mentioned: "提及" })[signal.direction])}</span>`).join("")
      : '<span class="historical-quality">未识别到能力项</span>';
    return `<tr>
      <td><input type="checkbox" data-historical-select="${index}" ${item.eligible_for_profile ? "checked" : ""} /></td>
      <td><strong>${escapeHtml(item.display_ref)}</strong><small class="historical-quality">原姓名不会入库</small></td>
      <td><span class="import-status ${item.eligible_for_profile ? "" : "review"}">${escapeHtml(item.outcome_label)}</span></td>
      <td><div class="historical-signals">${signals}</div></td>
      <td><span class="historical-quality">${escapeHtml(item.quality_flags.join("；") || "可计入画像样本")}</span></td>
    </tr>`;
  }).join("");
  $("historical-select-all").checked = preview.items.every((item) => item.eligible_for_profile);
}

async function commitHistoricalImport() {
  const preview = state.historicalPreview;
  if (!preview) return;
  const selected = [...document.querySelectorAll("[data-historical-select]:checked")]
    .map((input) => preview.items[Number(input.dataset.historicalSelect)]);
  if (!selected.length) {
    toast("请至少选择一条历史样本", true);
    return;
  }
  try {
    const result = await api("/api/v1/admin/historical-samples/commit", {
      method: "POST",
      body: JSON.stringify({
        job_id: preview.job.id,
        filename: preview.filename,
        file_hash: preview.file_hash,
        total_rows: preview.summary.total_rows,
        samples: selected.map((item) => ({
          row_number: item.row_number,
          record_hash: item.record_hash,
          outcome: item.outcome,
          competency_signals: item.competency_signals,
          quality_flags: item.quality_flags,
        })),
      }),
    });
    clearHistoricalImport();
    toast(`已导入 ${result.imported_rows} 条，跳过 ${result.skipped_duplicates} 条重复记录；${result.talent_profile_update.version_label} 等待 HR 审核`);
    await loadTalentProfileCenter();
  } catch (error) { toast(error.message, true); }
}

async function activateTalentProfile(versionId) {
  if (!window.confirm("确认将该画像设为当前岗位标准？旧版本会保留为历史记录，并尝试同步到 Obsidian。")) return;
  const jobId = $("profile-job-select").value;
  try {
    const result = await api(`/api/v1/admin/jobs/${jobId}/talent-profile/versions/${versionId}/activate`, { method: "POST", body: JSON.stringify({ confirmed_by_hr: true }) });
    toast(result.publication.status === "published" ? `${result.version_label} 已生效并发布到 Obsidian` : `${result.version_label} 已生效，但知识文件发布待重试`);
    await loadTalentProfileCenter();
  } catch (error) { toast(error.message, true); }
}

async function retryTalentProfilePublish(versionId) {
  const jobId = $("profile-job-select").value;
  try {
    const result = await api(`/api/v1/admin/jobs/${jobId}/talent-profile/versions/${versionId}/publish`, { method: "POST" });
    toast(result.publication.status === "published" ? "岗位画像已发布到 Obsidian" : result.publication.error_message, result.publication.status !== "published");
    await loadTalentProfileCenter();
  } catch (error) { toast(error.message, true); }
}

async function loadKnowledgeCenter() {
  try {
    [state.knowledgeStatus, state.knowledgeProposals, state.systemDocsStatus] = await Promise.all([
      api("/api/v1/admin/knowledge/status"),
      api("/api/v1/knowledge/proposals"),
      api("/api/v1/admin/knowledge/system-docs"),
    ]);
    renderKnowledgeCenter();
  } catch (error) {
    toast(error.message, true);
  }
}

function renderKnowledgeCenter() {
  const status = state.knowledgeStatus;
  const vault = status.vault;
  const counts = status.counts;
  $("knowledge-status").innerHTML = `
    <article class="knowledge-status-card ${vault.writable ? "ready" : "warning"}"><small>Obsidian 连接</small><strong>${vault.writable ? "已连接" : "需要配置"}</strong><span>${escapeHtml(vault.message)}</span></article>
    <article class="knowledge-status-card"><small>Vault 路径</small><strong class="path-value">${escapeHtml(vault.path || "未配置")}</strong><span>${escapeHtml(vault.name)}</span></article>
    <article class="knowledge-status-card"><small>待 HR 审批</small><strong>${counts.pending}</strong><span>AI 不能自动发布</span></article>
    <article class="knowledge-status-card"><small>已发布知识</small><strong>${counts.published}</strong><span>${counts.publication_failed ? `${counts.publication_failed} 条发布失败待处理` : "当前无发布错误"}</span></article>`;
  const openLink = $("knowledge-open-vault");
  openLink.classList.toggle("hidden", !vault.open_uri);
  if (vault.open_uri) openLink.href = vault.open_uri;

  renderSystemDocsStatus();

  const typeLabels = { competency: "能力模型", question: "面试题目", follow_up_rule: "追问规则", profile: "人才画像" };
  const statusLabels = { pending: "待审批", approved_for_publish: "已批准·待发布", published: "已发布", rejected: "已驳回" };
  $("knowledge-proposals").innerHTML = state.knowledgeProposals.map((proposal) => {
    const publication = proposal.publication;
    const summary = proposal.payload.question || proposal.payload.title || proposal.payload.competency_name || proposal.payload.name || proposal.payload.summary || "结构化知识提案";
    const publishFailed = publication?.status === "failed";
    const occurrenceCount = proposal.payload._occurrence_count || 1;
    const sourceLabel = proposal.payload._auto_generated ? `AI 自动提案 · 累计 ${occurrenceCount} 场` : "HR 手工提案";
    return `<article class="knowledge-proposal ${proposal.status}">
      <div class="knowledge-proposal-head"><div><span class="pill">${escapeHtml(typeLabels[proposal.proposal_type] || proposal.proposal_type)}</span><h3>${escapeHtml(summary)}</h3></div><strong>${escapeHtml(statusLabels[proposal.status] || proposal.status)}</strong></div>
      <p>${escapeHtml(proposal.rationale)}</p>
      <div class="knowledge-proposal-meta"><span>${escapeHtml(sourceLabel)}</span><span>来源轮次：${escapeHtml(proposal.source_round_id)}</span>${publication ? `<span>版本：${escapeHtml(publication.release_version)}</span>` : ""}</div>
      ${publishFailed ? `<div class="knowledge-error">发布未完成：${escapeHtml(publication.error_message)}</div>` : ""}
      <div class="knowledge-actions">
        ${proposal.status === "pending" ? `<button class="primary compact" data-knowledge-approve="${escapeHtml(proposal.id)}">批准并发布</button><button class="danger compact" data-knowledge-reject="${escapeHtml(proposal.id)}">驳回</button>` : ""}
        ${proposal.status === "approved_for_publish" ? `<button class="primary compact" data-knowledge-publish="${escapeHtml(proposal.id)}">重新发布</button>` : ""}
        ${publication?.obsidian_uri ? `<a class="secondary compact link-button" href="${escapeHtml(publication.obsidian_uri)}">在 Obsidian 查看</a>` : ""}
      </div>
    </article>`;
  }).join("") || '<div class="empty-state"><strong>暂时没有知识提案</strong><span>面试结束后，AI 发现重复性问题或知识缺口时会在这里生成待审草案。</span></div>';

  document.querySelectorAll("[data-knowledge-approve]").forEach((button) => button.addEventListener("click", () => reviewKnowledgeProposal(button.dataset.knowledgeApprove, "approved")));
  document.querySelectorAll("[data-knowledge-reject]").forEach((button) => button.addEventListener("click", () => reviewKnowledgeProposal(button.dataset.knowledgeReject, "rejected")));
  document.querySelectorAll("[data-knowledge-publish]").forEach((button) => button.addEventListener("click", () => retryKnowledgePublish(button.dataset.knowledgePublish)));
}

function updateSystemDocsSyncState() {
  const docs = state.systemDocsStatus;
  const canSync = Boolean(
    docs?.target?.writable
    && docs?.summary?.pending
    && $("system-docs-confirm").checked
  );
  $("system-docs-sync-btn").disabled = !canSync;
}

function renderSystemDocsStatus() {
  const docs = state.systemDocsStatus;
  if (!docs) return;
  const summary = docs.summary;
  const policy = docs.policy;
  const target = docs.target;
  $("system-docs-status").innerHTML = `
    <article><small>系统文档</small><strong>${summary.total}</strong><span>产品、操作、配置与运维说明</span></article>
    <article class="${summary.in_sync ? "ready" : "attention"}"><small>同步状态</small><strong>${summary.in_sync ? "已是最新版" : `${summary.pending} 份待同步`}</strong><span>${summary.synced} 份已与项目一致</span></article>
    <article><small>知识隔离</small><strong>${policy.rag_scope === "excluded" ? "不参与面试分析" : "请检查"}</strong><span>不含候选人数据与密钥</span></article>`;

  const statusLabels = { synced: "已同步", outdated: "有新版本", missing: "尚未同步" };
  $("system-docs-list").innerHTML = docs.items.map((item) => `
    <article class="system-doc-item">
      <div><strong>${escapeHtml(item.title)}</strong><small>${escapeHtml(item.target_path)}</small></div>
      <span class="system-doc-state ${escapeHtml(item.status)}">${escapeHtml(statusLabels[item.status] || item.status)}</span>
    </article>`).join("") || '<div class="empty-state"><strong>没有找到系统文档</strong><span>请检查项目内的文档源目录。</span></div>';

  const openLink = $("system-docs-open");
  openLink.classList.toggle("hidden", !target.open_uri);
  if (target.open_uri) openLink.href = target.open_uri;
  $("system-docs-confirm").checked = false;
  $("system-docs-confirm").disabled = !target.writable || !summary.pending;
  $("system-docs-sync-btn").textContent = summary.in_sync ? "当前已同步" : "同步系统文档";
  updateSystemDocsSyncState();
}

async function syncSystemDocs() {
  if (!$("system-docs-confirm").checked) return toast("请先完成 HR 同步确认", true);
  const button = $("system-docs-sync-btn");
  button.disabled = true;
  try {
    const result = await api("/api/v1/admin/knowledge/system-docs/sync", {
      method: "POST",
      body: JSON.stringify({ confirmed_by_hr: true }),
    });
    state.systemDocsStatus = result.system_docs;
    renderSystemDocsStatus();
    toast(result.written.length ? `已将 ${result.written.length} 份系统文档同步到 Obsidian` : "系统文档已经是最新版");
  } catch (error) {
    updateSystemDocsSyncState();
    toast(error.message, true);
  }
}

async function reviewKnowledgeProposal(proposalId, decision) {
  try {
    const result = await api(`/api/v1/knowledge/proposals/${proposalId}`, {
      method: "PATCH",
      body: JSON.stringify({ decision, reviewed_by: state.currentUser?.display_name || "当前 HR" }),
    });
    if (decision === "rejected") toast("提案已驳回，不会进入正式知识库");
    else if (result.status === "published") toast("审批完成，知识已发布到 Obsidian");
    else toast(result.publication?.error_message || "审批已保存，但发布尚未完成", true);
    await loadKnowledgeCenter();
  } catch (error) { toast(error.message, true); }
}

async function retryKnowledgePublish(proposalId) {
  try {
    const result = await api(`/api/v1/knowledge/proposals/${proposalId}/publish`, { method: "POST" });
    if (result.status === "published") toast("知识已成功发布到 Obsidian");
    else toast(result.publication?.error_message || "发布仍未完成", true);
    await loadKnowledgeCenter();
  } catch (error) { toast(error.message, true); }
}

async function enterTodayInterviews() {
  $("today-btn").disabled = true;
  $("agenda-empty-state").classList.add("hidden");
  try {
    await loadTodayInterviews();
    if (!state.todayInterviews.length) {
      if (["hr", "admin"].includes(state.currentUser.role)) await openAdminPanel();
      else $("agenda-empty-state").classList.remove("hidden");
      toast("未来 7 天没有分配给当前飞书账号的面试；已创建的任务可能分配给了其他面试官");
      return;
    }
    const first = state.todayInterviews[0];
    state.interviewId = first.interview_id;
    state.rounds = state.todayInterviews.filter((item) => item.candidate.id === first.candidate.id && item.job.id === first.job.id).map((item) => ({ id: item.interview_id, round_type: item.round_type }));
    $("welcome").classList.add("hidden");
    $("workspace").classList.remove("hidden");
    $("workspace").classList.remove("evaluation-mode");
    setSidebarActive("interviews");
    await loadInterview();
  } catch (error) {
    toast(error.message, true);
  } finally {
    $("today-btn").disabled = false;
  }
}

async function createInterviewTask(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const submit = $("task-submit-btn");
  submit.disabled = true;
  const value = (name) => form.elements[name].value.trim();
  try {
    const enabledCards = taskRoundCards().filter((card) => card.querySelector("[data-round-enabled]").checked);
    if (!enabledCards.length) throw new Error("请至少启用一个面试轮次");
    const data = await api("/api/v1/interview-tasks", {
      method: "POST",
      body: JSON.stringify({
        application_id: state.schedulingApplicationId,
        job_id: value("job_id") || null,
        candidate_name: value("candidate_name"),
        resume_text: value("resume_text"),
        job_title: value("job_title"),
        source_job_code: value("source_job_code") || null,
        jd_text: value("jd_text"),
        retention_days: 120,
        screening_payload: { source: "hr_manual_task", needs_human_review: true },
        rounds: enabledCards.map((card) => {
          const roundType = card.dataset.roundForm;
          const interviewer = state.assignableUsers.find((user) => user.open_id === value(`${roundType}_interviewer`));
          return {
          round_type: roundType,
          interview_mode: form.elements[`${roundType}_mode`].value,
          interviewer_open_ids: [value(`${roundType}_interviewer`)],
          interviewer_names: [interviewer?.display_name || "待确认面试官"],
          scheduled_at: form.elements[`${roundType}_time`].value,
          meeting_source: form.elements[`${roundType}_source`].value,
          };
        }),
      }),
    });
    state.rounds = data.rounds;
    state.interviewId = data.active_interview_id;
    form.reset();
    state.schedulingApplicationId = null;
    form.elements.candidate_name.readOnly = false;
    form.elements.resume_text.readOnly = false;
    form.elements.job_title.readOnly = false;
    $("welcome").classList.add("hidden");
    $("task-creator").classList.add("hidden");
    $("welcome").querySelector(".welcome-card").classList.remove("hidden");
    $("workspace").classList.remove("hidden");
    await loadTodayInterviews();
    await loadInterview();
    toast(`已创建 ${data.candidate.display_name} 的 ${data.rounds.length} 轮面试任务`);
  } catch (error) {
    toast(error.message, true);
  } finally {
    submit.disabled = false;
  }
}

async function loadTodayInterviews() {
  state.todayInterviews = await api("/api/v1/me/interviews/today?days=7");
  $("today-list").innerHTML = state.todayInterviews.map((item) => {
    const scheduled = item.scheduled_at ? new Date(item.scheduled_at) : null;
    const time = scheduled ? scheduled.toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "待定";
    const dossier = item.candidate_dossier || {};
    const report = dossier.management_report;
    const dossierHint = report ? "已锁定面试汇总" : dossier.prior_round_count ? `${dossier.prior_round_count} 轮前轮材料` : "简历与岗位重点";
    return `<article class="today-card ${item.interview_id === state.interviewId ? "active" : ""}">
      <button class="today-card-main" data-today-interview="${item.interview_id}">
        <span class="today-time">${time}</span>
        <span class="today-person"><strong>${escapeHtml(item.candidate.display_name)}</strong><small>${escapeHtml(item.job.title)}</small></span>
        <span class="today-round">${roundLabel(item.round_type)} · ${item.interview_mode === "conversation" ? "自由对话" : "结构化"}</span>
      </button>
      <button class="today-dossier-btn" data-today-dossier="${item.interview_id}">查看候选人档案<small>${dossierHint}</small></button>
    </article>`;
  }).join("") || '<p class="muted">未来 7 天没有分配给当前飞书账号的面试。HR 可在招聘任务后台核对面试时间和面试官。</p>';
  document.querySelectorAll("[data-today-interview]").forEach((button) => button.addEventListener("click", async () => {
    state.interviewId = button.dataset.todayInterview;
    $("scorecard").classList.add("hidden");
    $("interviewer-review").classList.add("hidden");
    await loadInterview();
    await loadTodayInterviews();
  }));
  document.querySelectorAll("[data-today-dossier]").forEach((button) => button.addEventListener("click", async () => {
    const item = state.todayInterviews.find((candidate) => candidate.interview_id === button.dataset.todayDossier);
    if (!item) return;
    state.interviewId = item.interview_id;
    const report = item.candidate_dossier?.management_report;
    if (report) {
      await openInterviewerReport(report.id);
      return;
    }
    await loadInterview();
    await loadTodayInterviews();
    const detail = $("prep-prior-detail");
    if (!detail.classList.contains("hidden")) detail.open = true;
    $("preparation-brief").scrollIntoView({ behavior: "smooth", block: "start" });
  }));
}

function userOptions(selectedOpenId) {
  return state.assignableUsers.map((user) => `<option value="${escapeHtml(user.open_id)}" ${user.open_id === selectedOpenId ? "selected" : ""}>${escapeHtml(user.display_name)}</option>`).join("");
}

function renderHrActionCenter(center) {
  const summary = center.summary;
  $("admin-action-summary").innerHTML = `
    <article><small>今日面试</small><strong>${summary.today_interviews}</strong></article>
    <article><small>待补评价</small><strong>${summary.missing_scorecards}</strong></article>
    <article><small>可进入终审</small><strong>${summary.ready_for_decision}</strong></article>
    <article><small>待排期</small><strong>${summary.unscheduled}</strong></article>`;
  $("admin-action-boundary").textContent = center.boundary;
  $("admin-action-list").innerHTML = center.items.length ? center.items.slice(0, 10).map((item) => {
    const subject = item.candidate ? `${item.candidate.display_name} · ${item.job.title}` : "人才知识库";
    const timing = item.scheduled_at ? new Date(item.scheduled_at).toLocaleString("zh-CN") : item.round_label || "";
    return `<article class="admin-action-card ${escapeHtml(item.priority)}">
      <div><h4>${escapeHtml(item.title)}</h4><p>${escapeHtml(subject)}</p></div>
      <div class="admin-action-card-meta">${escapeHtml(item.detail)}${timing ? `<br><small>${escapeHtml(timing)}</small>` : ""}</div>
      <div class="admin-action-buttons"><button class="secondary compact" data-admin-action="${escapeHtml(item.action)}" data-application-id="${escapeHtml(item.application_id || "")}" data-interview-id="${escapeHtml(item.interview_id || "")}">${escapeHtml(item.action_label)}</button>${item.type === "missing_scorecard" ? `<button class="secondary compact todo-remove" data-dismiss-feedback="${escapeHtml(item.interview_id)}">移出待评价</button>` : ""}</div>
    </article>`;
  }).join("") : '<p class="muted">当前没有需要 HR 处理的事项。</p>';
  document.querySelectorAll("[data-admin-action]").forEach((button) => button.addEventListener("click", () => handleAdminAction(button)));
  document.querySelectorAll("#admin-action-list [data-dismiss-feedback]").forEach((button) => button.addEventListener("click", dismissFeedbackTodo));
}

async function handleAdminAction(button) {
  const action = button.dataset.adminAction;
  if (action === "schedule_application") {
    const task = state.adminTasks.find((item) => item.task_id === button.dataset.applicationId);
    if (task) await scheduleImportedCandidate(task);
  } else if (action === "open_interview") {
    await openInterviewFromAction(button.dataset.interviewId);
  } else if (action === "open_final_review") {
    await openFinalReview(button.dataset.applicationId);
  } else if (action === "open_knowledge") {
    await openKnowledgePanel();
  }
}

async function openInterviewFromAction(interviewId) {
  if (!interviewId) return;
  state.interviewId = interviewId;
  $("welcome").classList.add("hidden");
  $("admin-panel").classList.add("hidden");
  $("final-review-panel").classList.add("hidden");
  $("knowledge-panel").classList.add("hidden");
  $("talent-profile-panel").classList.add("hidden");
  $("company-profile-panel").classList.add("hidden");
  $("job-center-panel").classList.add("hidden");
  $("quality-dashboard-panel").classList.add("hidden");
  $("report-panel").classList.add("hidden");
  $("governance-panel").classList.add("hidden");
  $("notification-panel").classList.add("hidden");
  $("readiness-panel").classList.add("hidden");
  $("workspace").classList.remove("hidden");
  $("workspace").classList.remove("evaluation-mode");
  setSidebarActive("interviews");
  await loadInterview();
  if (state.interview?.status === "completed") await showEvaluationView();
}

async function loadAdminTasks() {
  const [tasks, actionCenter] = await Promise.all([
    api("/api/v1/admin/interview-tasks"),
    api("/api/v1/admin/action-center"),
  ]);
  state.adminTasks = tasks;
  state.hrActions = actionCenter;
  renderHrActionCenter(actionCenter);
  const adminView = $("admin-panel")?.dataset.view || "home";
  const visibleTasks = adminView === "final-review"
    ? tasks.filter((task) => task.current_stage === "final_review")
    : tasks;
  const taskRows = visibleTasks.map((task) => `
    <article class="admin-task" data-admin-task="${escapeHtml(task.task_id)}">
      <div class="admin-task-head"><div><h3>${escapeHtml(task.candidate.display_name)}</h3><small>${escapeHtml(task.job.title)}</small></div><div class="admin-task-head-actions">${task.rounds.length ? `<button class="secondary compact" data-final-review="${escapeHtml(task.task_id)}">查看面试汇总</button>` : `<button class="primary compact" data-schedule-application="${escapeHtml(task.task_id)}">配置面试流程</button>`}<span class="pill">${task.current_stage === "interview_to_schedule" ? "待排期" : escapeHtml(task.current_stage)}</span><details class="task-actions-menu"><summary aria-label="更多任务操作">···</summary><div class="task-actions-popover"><button type="button" data-task-action="view" data-application-id="${escapeHtml(task.task_id)}">${task.rounds.length ? "查看详情" : "配置面试流程"}</button><button type="button" data-task-action="adjust" data-application-id="${escapeHtml(task.task_id)}">调整安排</button><button type="button" data-task-action="delete" data-application-id="${escapeHtml(task.task_id)}">删除</button></div></details></div></div>
      <div class="admin-rounds">${task.rounds.map((round) => {
        const selected = round.assignments[0]?.open_id || "";
        return `<section class="admin-round" data-admin-round="${round.id}">
          <div class="admin-round-head"><strong>${roundLabel(round.round_type)}</strong><span class="${round.status === "cancelled" ? "status-cancelled" : ""}">${escapeHtml(round.status)}</span></div>
          <label>面试时间<input data-manage-time type="datetime-local" value="${round.scheduled_at ? String(round.scheduled_at).slice(0,16) : ""}" ${round.status === "cancelled" ? "disabled" : ""} /></label>
          <label>面试官<select data-manage-user ${round.status === "cancelled" ? "disabled" : ""}>${userOptions(selected)}</select></label>
          <label>面试方式<select data-manage-mode ${round.status === "cancelled" ? "disabled" : ""}><option value="structured" ${round.interview_mode !== "conversation" ? "selected" : ""}>固定问题 + 证据评分</option><option value="conversation" ${round.interview_mode === "conversation" ? "selected" : ""}>自由对话分析</option></select></label>
          <label>场景<select data-manage-source ${round.status === "cancelled" ? "disabled" : ""}><option value="offline" ${round.meeting_source === "offline" ? "selected" : ""}>线下面试</option><option value="feishu" ${round.meeting_source === "feishu" ? "selected" : ""}>飞书会议</option></select></label>
          <div class="admin-round-actions"><button class="secondary compact" data-save-round ${round.status === "cancelled" ? "disabled" : ""}>保存调整</button><button class="danger compact" data-cancel-round ${round.status === "cancelled" ? "disabled" : ""}>取消本轮</button></div>
        </section>`;
      }).join("")}</div>
    </article>`).join("");
  const listTitle = adminView === "final-review" ? "等待 HR 终审" : "最近的面试任务";
  const listHint = adminView === "final-review" ? "仅显示已完成所需轮次、可进入 HR 决定的候选人" : "按候选人、岗位和当前阶段快速查看";
  $("admin-task-list").innerHTML = `<div class="admin-list-heading"><div><h3>${listTitle}</h3><span>${listHint}</span></div><span>${visibleTasks.length} 项</span></div>${taskRows || (adminView === "final-review" ? '<p class="muted">当前没有满足终审条件的候选人。</p>' : '<p class="muted">还没有真实面试任务，点击“创建面试任务”开始。</p>')}`;
  document.querySelectorAll("[data-save-round]").forEach((button) => button.addEventListener("click", saveManagedRound));
  document.querySelectorAll("[data-cancel-round]").forEach((button) => button.addEventListener("click", cancelManagedRound));
  document.querySelectorAll("[data-schedule-application]").forEach((button) => button.addEventListener("click", () => scheduleImportedCandidate(tasks.find((task) => task.task_id === button.dataset.scheduleApplication))));
  document.querySelectorAll("[data-final-review]").forEach((button) => button.addEventListener("click", () => openFinalReview(button.dataset.finalReview)));
  document.querySelectorAll("[data-task-action]").forEach((button) => button.addEventListener("click", handleTaskAction));
}

async function handleTaskAction(event) {
  const button = event.currentTarget;
  const task = state.adminTasks.find((item) => item.task_id === button.dataset.applicationId);
  if (!task) return;
  button.closest("details")?.removeAttribute("open");
  if (button.dataset.taskAction === "delete") return openTaskDeleteDialog(task);
  if (button.dataset.taskAction === "view") {
    if (task.rounds.length) return openFinalReview(task.task_id);
    return scheduleImportedCandidate(task);
  }
  const card = document.querySelector(`[data-admin-task="${CSS.escape(task.task_id)}"]`);
  if (!task.rounds.length) return scheduleImportedCandidate(task);
  card?.scrollIntoView({ behavior: "smooth", block: "center" });
  card?.querySelector("[data-manage-time]")?.focus({ preventScroll: true });
  toast("已定位到该任务的面试安排，可直接调整时间、面试官或面试方式");
}

function openTaskDeleteDialog(task) {
  const deletion = task.deletion || {
    allowed: true,
    mode: "hard_delete",
    preserves_history: false,
    reason: "任务尚未开始且未产生正式面试数据，可以安全删除。",
  };
  state.taskDeletion = { task, deletion };
  const isArchive = deletion.mode === "archive" || deletion.preserves_history;
  $("task-delete-title").textContent = "删除这条面试任务？";
  $("task-delete-copy").textContent = isArchive
    ? "删除后，该任务将从“最近的面试任务”中移除。\n已经产生的面试记录、评价和历史数据仍会保留，以后仍可在候选人历史记录中查看。"
    : "删除后，该任务将不再出现在面试任务列表中。";
  $("task-delete-candidate").textContent = task.candidate.display_name;
  $("task-delete-job").textContent = task.job.title;
  $("task-delete-feedback").textContent = "";
  $("task-delete-feedback").classList.add("hidden");
  const confirmButton = $("task-delete-confirm");
  confirmButton.disabled = false;
  $("task-delete-confirm").classList.remove("hidden");
  $("task-delete-cancel").textContent = "取消";
  confirmButton.textContent = "删除";
  const dialog = $("task-delete-dialog");
  if (typeof dialog.showModal === "function") dialog.showModal();
  else dialog.setAttribute("open", "");
}

function closeTaskDeleteDialog() {
  const dialog = $("task-delete-dialog");
  if (dialog.open && typeof dialog.close === "function") dialog.close();
  else dialog.removeAttribute("open");
  state.taskDeletion = null;
}

async function confirmTaskDeletion() {
  const current = state.taskDeletion;
  if (!current?.deletion.allowed) return closeTaskDeleteDialog();
  const confirmButton = $("task-delete-confirm");
  confirmButton.disabled = true;
  try {
    const result = await api(`/api/v1/admin/applications/${current.task.task_id}?confirmed=true`, { method: "DELETE" });
    const card = document.querySelector(`[data-admin-task="${CSS.escape(current.task.task_id)}"]`);
    card?.remove();
    state.adminTasks = state.adminTasks.filter((item) => item.task_id !== current.task.task_id);
    confirmButton.disabled = false;
    closeTaskDeleteDialog();
    await loadAdminTasks();
    toast(result?.mode === "archived" ? "已从最近任务中移除，历史数据已保留" : "已从最近任务中移除");
  } catch (error) {
    confirmButton.disabled = false;
    $("task-delete-feedback").textContent = error.message;
    $("task-delete-feedback").classList.remove("hidden");
  }
}

async function openFinalReview(applicationId) {
  try {
    setSidebarActive("final-review");
    state.finalReviewApplicationId = applicationId;
    state.finalReview = await api(`/api/v1/admin/applications/${applicationId}/final-review`);
    $("admin-panel").classList.add("hidden");
    $("report-panel").classList.add("hidden");
    $("governance-panel").classList.add("hidden");
    $("notification-panel").classList.add("hidden");
    $("readiness-panel").classList.add("hidden");
    $("final-review-panel").classList.remove("hidden");
    renderFinalReview(state.finalReview);
  } catch (error) { toast(error.message, true); }
}

async function closeFinalReview() {
  await openAdminPanel("final-review");
}

function renderFinalReview(review) {
  const decisionLabels = { offer_approval: "进入录用审批", supplementary_interview: "安排补充面试", hold: "暂缓决定", reject: "不录用并关闭流程", advance: "建议进入下一轮" };
  const stageLabels = { business_interview: "业务面阶段", hr_interview: "HR 面阶段", ceo_interview: "CEO 面阶段", final_review: "等待 HR 终审", offer_approval: "录用审批", supplementary_interview: "补充面试", on_hold: "暂缓", closed_rejected: "已关闭·不录用" };
  $("final-review-title").textContent = `${review.candidate.display_name} · 岗位面试汇总`;
  $("final-review-subtitle").textContent = `${review.job.title}${review.job.source_job_code ? ` · ${review.job.source_job_code}` : ""} · 当前阶段：${stageLabels[review.current_stage] || review.current_stage}`;
  const readiness = review.readiness;
  $("final-readiness").innerHTML = `
    <article class="readiness-card ${readiness.status === "ready_for_hr_decision" ? "ready" : "warning"}"><small>材料状态</small><strong>${readiness.status === "ready_for_hr_decision" ? "可供 HR 决策" : "尚未完备"}</strong></article>
    <article class="readiness-card"><small>已完成面试</small><strong>${readiness.rounds_completed} / ${readiness.rounds_total}</strong></article>
    <article class="readiness-card"><small>已提交人工评价</small><strong>${readiness.scorecards_submitted} / ${readiness.rounds_total}</strong></article>
    <article class="readiness-card"><small>待验证问题</small><strong>${readiness.open_question_count}</strong></article>
    <article class="readiness-card"><small>知识库待审批</small><strong>${readiness.pending_knowledge_approvals}</strong></article>
    <div class="readiness-missing">${readiness.missing_steps.length ? `仍需完成：${escapeHtml(readiness.missing_steps.join("；"))}` : escapeHtml(readiness.policy)}</div>`;

  const dialogueReview = review.cross_round_ai_assessment || {};
  $("final-ai-dialogue-summary").innerHTML = `
    <article class="final-ai-overall"><small>全部对话综合参考分</small><strong>${dialogueReview.overall_score == null ? "暂不可评" : `${dialogueReview.overall_score} / 5`}</strong><small>${escapeHtml(dialogueReview.method || "等待各轮形成有效证据")}</small></article>
    ${(dialogueReview.rounds || []).map((item) => `<article class="final-ai-round"><small>${escapeHtml(item.round_label)} · ${escapeHtml(item.interviewer_names.join("、") || "待确认面试官")}</small><strong>${item.score} / 5</strong><span>${item.assessed_dimensions}/${item.total_dimensions} 项有证据 · ${item.evidence_count} 条引用 · ${item.transcript_count} 段对话</span></article>`).join("")}
    ${(dialogueReview.rounds || []).length ? "" : '<article class="final-ai-round"><small>尚无可汇总轮次</small><span>面试结束后，系统会按各轮职责分析全部真实问答。</span></article>'}
    <div class="readiness-missing">${escapeHtml(dialogueReview.policy || "综合分仅作参考，不自动改变候选人阶段。")}</div>`;

  $("final-rounds").innerHTML = review.rounds.map((round) => {
    const scorecard = round.scorecard;
    const humanDecision = scorecard?.human_decision;
    const flags = round.interviewer_quality?.metrics?.flags || [];
    const questionSummary = scorecard?.question_evidence_summary || {};
    const aiRecommendation = scorecard?.ai_recommendation || {};
    const evaluationScope = scorecard?.evaluation_scope || {};
    return `<article class="final-round-card ${round.status}">
      <div class="final-round-card-head"><strong>${escapeHtml(round.round_label)}</strong><span>${escapeHtml(round.status)}</span></div>
      <div class="final-round-meta">
        <span>面试官：${escapeHtml(round.interviewer_names.join("、") || "待分配")}</span>
        <span>时间：${round.scheduled_at ? new Date(round.scheduled_at).toLocaleString("zh-CN") : "待安排"}</span>
        <span>场景：${round.meeting_source === "feishu" ? "飞书会议" : "线下面试"} · 逐字稿 ${round.transcript_count} 段</span>
        <span>证据：${round.evidence_summary.confirmed} 条已确认 / ${round.evidence_summary.pending} 条待确认</span>
      </div>
      <div class="artifact-links">
        <a href="${round.transcript_url}" target="_blank">逐字稿</a>
        ${round.recordings.map((item, index) => `<a href="${item.download_url}" target="_blank">录音 ${index + 1}</a>`).join("") || '<span class="muted">暂无录音</span>'}
        <button type="button" data-review-round="${round.id}">进入本轮详情</button>
      </div>
      <div class="round-evaluation">
        <strong>${scorecard ? humanDecision ? `面试官结论：${escapeHtml(decisionLabels[humanDecision.decision] || humanDecision.decision)}` : "评价草稿尚未提交" : "尚未生成评价"}</strong>
        ${scorecard ? `<small>问题证据：${questionSummary.evidenced || 0} 道充分 · ${questionSummary.shallow || 0} 道较浅 · ${questionSummary.unanswered || 0} 道未验证</small>` : ""}
        ${aiRecommendation.overall_score == null ? "" : `<small>AI 岗位证据分：${aiRecommendation.overall_score} / 5 · ${(evaluationScope.dimensions || []).length} 项${escapeHtml(evaluationScope.round_label || round.round_label)}专属维度 · 全部真实问答</small>`}
        ${flags.length ? `<small>面试质量提示：${escapeHtml(flags.map((item) => item.message).join("；"))}</small>` : '<small>暂未发现明显面试流程异常。</small>'}
      </div>
    </article>`;
  }).join("") || '<p class="muted">尚未安排面试。</p>';

  $("final-competencies").innerHTML = review.competency_summary.map((item) => `
    <article class="final-competency"><div><b>${escapeHtml(item.competency_name)}</b><small>${item.round_count} 轮人工确认 · ${item.evidence_count} 条引用证据</small></div><strong>${item.average_human_score} / 5</strong></article>
  `).join("") || '<p class="muted">还没有基于已确认证据提交的人工能力评分。</p>';

  $("final-open-questions").innerHTML = review.outstanding_questions.slice(0, 8).map((item) => `
    <article class="final-question"><b>${escapeHtml(item.question)}</b><small>${escapeHtml(item.source_round_label)} · ${escapeHtml(item.reason)}</small></article>
  `).join("") || '<p class="muted">最新一轮评价中没有遗留待验证问题。</p>';

  const details = review.final_decision_details;
  $("final-decision").querySelector('[value="offer_approval"]').disabled = readiness.status !== "ready_for_hr_decision";
  $("final-decision").querySelector('[value="reject"]').disabled = readiness.scorecards_submitted === 0;
  $("final-decision").value = details?.decision || "";
  $("final-decision-notes").value = details?.notes || "";
  $("final-decision-confirm").checked = false;
  $("final-decision-form").querySelector("button").textContent = details ? "更新 HR 决定并变更阶段" : "确认并变更阶段";
  document.querySelectorAll("[data-review-round]").forEach((button) => button.addEventListener("click", () => openRoundFromFinalReview(button.dataset.reviewRound)));
}

async function openRoundFromFinalReview(roundId) {
  state.rounds = state.finalReview.rounds.map((item) => ({ id: item.id, round_type: item.round_type }));
  state.interviewId = roundId;
  $("final-review-panel").classList.add("hidden");
  $("admin-panel").classList.add("hidden");
  $("welcome").classList.add("hidden");
  $("workspace").classList.remove("hidden");
  await loadInterview();
}

async function submitFinalDecision(event) {
  event.preventDefault();
  const decision = $("final-decision").value;
  const notes = $("final-decision-notes").value.trim();
  if (!decision || notes.length < 5) return toast("请选择决定并填写至少 5 个字的依据", true);
  if (!$("final-decision-confirm").checked) return toast("请确认这是 HR 人工决定，并理解候选人阶段会发生变化", true);
  try {
    state.finalReview = await api(`/api/v1/admin/applications/${state.finalReviewApplicationId}/final-decision`, {
      method: "POST",
      body: JSON.stringify({
        decision,
        decided_by: state.currentUser?.display_name || "当前 HR",
        notes,
        confirmed_by_hr: true,
      }),
    });
    renderFinalReview(state.finalReview);
    await loadAdminTasks();
    toast(state.finalReview.talent_profile_update?.status === "draft_ready" ? `候选人阶段已更新，并形成 ${state.finalReview.talent_profile_update.version_label} 画像草稿` : "HR 决定已保存，候选人阶段已更新");
  } catch (error) { toast(error.message, true); }
}

async function openSharedReportIfRequested() {
  const params = new URLSearchParams(location.search);
  const reportId = params.get("report");
  const interviewId = params.get("interview");
  const finalReviewId = params.get("final_review");
  const knowledge = params.get("knowledge");
  if (interviewId) {
    await openInterviewFromAction(interviewId);
    return;
  }
  if (finalReviewId && ["hr", "admin"].includes(state.currentUser?.role)) {
    await openFinalReview(finalReviewId);
    return;
  }
  if (knowledge && ["hr", "admin"].includes(state.currentUser?.role)) {
    await openKnowledgePanel();
    return;
  }
  if (!reportId || state.sharedReportOpened) return;
  state.sharedReportOpened = true;
  state.reportApplicationId = null;
  state.reportAudience = "management";
  state.reportReturnView = "welcome";
  $("report-close-btn").textContent = "返回首页";
  await showReportPanel();
  state.report = await api(`/api/v1/reports/${encodeURIComponent(reportId)}`);
  state.reportCenter = { versions: [state.report] };
  renderReportCenter(true);
}

async function openInterviewerReport(reportId) {
  state.reportApplicationId = null;
  state.reportAudience = "management";
  state.reportReturnView = "workspace";
  $("report-close-btn").textContent = "返回当前面试";
  await showReportPanel();
  state.report = await api(`/api/v1/reports/${encodeURIComponent(reportId)}`);
  state.reportCenter = { versions: [state.report] };
  renderReportCenter(true);
}

async function openReportCenter(applicationId = state.finalReviewApplicationId) {
  if (!applicationId) return toast("请先打开候选人的面试汇总", true);
  state.reportApplicationId = applicationId;
  state.reportAudience = "management";
  state.reportReturnView = "final-review";
  $("report-close-btn").textContent = "返回面试汇总";
  await showReportPanel();
  await loadReportCenter();
}

async function showReportPanel() {
  $("workspace").classList.add("hidden");
  $("welcome").classList.remove("hidden");
  $("welcome").querySelector(".welcome-card").classList.add("hidden");
  ["admin-panel", "readiness-panel", "final-review-panel", "knowledge-panel", "talent-profile-panel", "company-profile-panel", "job-center-panel", "quality-dashboard-panel", "governance-panel", "notification-panel", "resume-import-panel", "task-creator"].forEach((id) => $(id).classList.add("hidden"));
  $("report-panel").classList.remove("hidden");
}

async function loadReportCenter(preferredReportId = null) {
  state.reportCenter = await api(`/api/v1/admin/applications/${state.reportApplicationId}/reports`);
  if (!state.reportCenter.versions.length) {
    const draft = await api(`/api/v1/admin/applications/${state.reportApplicationId}/reports/draft`, { method: "POST" });
    preferredReportId = draft.id;
    state.reportCenter = await api(`/api/v1/admin/applications/${state.reportApplicationId}/reports`);
  }
  const selectedId = preferredReportId
    || state.reportCenter.current_draft_id
    || state.reportCenter.current_locked_id
    || state.reportCenter.versions[0].id;
  $("report-version-select").innerHTML = state.reportCenter.versions.map((item) => `<option value="${escapeHtml(item.id)}" ${item.id === selectedId ? "selected" : ""}>${escapeHtml(item.version_label)} · ${escapeHtml(({ draft: "草稿", locked: "已锁定", superseded: "历史版本" })[item.status] || item.status)}</option>`).join("");
  await loadReportVersion(selectedId);
}

async function loadReportVersion(reportId) {
  state.report = await api(`/api/v1/reports/${encodeURIComponent(reportId)}?audience=${encodeURIComponent(state.reportAudience)}`);
  renderReportCenter(false);
}

function reportList(items, emptyText) {
  return items?.length ? `<ul>${items.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>` : `<p class="muted">${escapeHtml(emptyText)}</p>`;
}

function renderReportCenter(sharedView) {
  const report = state.report;
  if (!report) return;
  const identity = report.identity;
  const content = report.content;
  const summary = content.executive_summary || {};
  const statusLabels = { draft: "待 HR 核对", locked: "已锁定，可内部分享", superseded: "历史版本" };
  $("report-title").textContent = `${identity.candidate_name} · 岗位面试报告`;
  $("report-subtitle").textContent = `${identity.job_title}${identity.source_job_code ? ` · ${identity.source_job_code}` : ""} · ${report.audience === "management" ? "管理层摘要" : "HR 完整档案"}`;
  $("report-status-strip").innerHTML = `
    <span class="${report.status === "locked" ? "locked" : ""}">${escapeHtml(statusLabels[report.status] || report.status)}</span>
    <span>${escapeHtml(report.version_label)}</span>
    <span>生成：${escapeHtml(report.created_by)}</span>
    <span>${report.locked_by ? `锁定：${escapeHtml(report.locked_by)}` : "内容仍可刷新"}</span>`;

  const competencies = (content.competencies || []).map((item) => `<div class="report-competency"><div><b>${escapeHtml(item.competency_name)}</b><small>${item.round_count} 轮人工确认 · ${item.evidence_count} 条证据</small></div><div class="report-score-bar"><i style="width:${Math.max(0, Math.min(100, item.average_human_score * 20))}%"></i></div><strong>${item.average_human_score} / 5</strong></div>`).join("") || '<p class="muted">暂无已确认能力评分。</p>';
  const rounds = (content.rounds || []).map((item) => `<article class="report-round"><header><b>${escapeHtml(item.round_label)}</b><span>${escapeHtml(item.human_decision_label || "待提交评价")}</span></header><p>面试官：${escapeHtml((item.interviewer_names || []).join("、") || "待分配")}</p><small>${escapeHtml(item.human_notes || "暂无评价说明")}</small></article>`).join("") || '<p class="muted">暂无面试轮次。</p>';
  const evidence = (content.key_evidence || []).map((item) => `<div class="report-evidence"><blockquote>“${escapeHtml(item.quote)}”</blockquote><small>${escapeHtml(item.round_label)} · ${escapeHtml(item.competency_name || item.competency_id)}</small></div>`).join("") || '<p class="muted">暂无人工确认的逐字稿证据。</p>';
  const appendix = content.hr_appendix;
  const appendixHtml = appendix ? `<section class="report-section report-hr-appendix"><h2>HR 内部附录</h2><div class="final-two-column"><div><h3>逐字稿与录音</h3><div class="report-artifact-list">${(appendix.artifacts || []).map((item) => `<div class="report-artifact"><b>${escapeHtml(item.round_label)}</b><div><a href="${escapeHtml(item.transcript_url)}" target="_blank">逐字稿</a>${(item.recordings || []).map((recording, index) => `<a href="${escapeHtml(recording.download_url)}" target="_blank">录音 ${index + 1}</a>`).join("") || '<span class="muted">暂无录音</span>'}</div></div>`).join("")}</div></div><div><h3>面试官质量复盘</h3><div class="report-quality-list">${(appendix.interviewer_quality || []).map((item) => `<div class="report-quality-item"><b>${escapeHtml(item.round_label)}</b><p>${escapeHtml(item.status === "reviewed" ? "已完成人工复盘" : "待招聘负责人复盘")} · 质量提示 ${(item.metrics?.flags || []).length} 项</p></div>`).join("")}</div></div></div><div class="report-internal-note">仅供 HR 流程治理；管理层摘要和内部分享链接不会包含本附录。</div></section>` : "";

  $("report-document").innerHTML = `
    <header class="report-cover"><span class="kicker">INTERVIEW REPORT · ${report.audience === "management" ? "MANAGEMENT" : "HR ARCHIVE"}</span><h1>${escapeHtml(identity.candidate_name)}</h1><p>${escapeHtml(identity.job_title)} · ${escapeHtml(report.version_label)}</p><div class="report-conclusion"><div><small>当前流程结论</small><strong>${escapeHtml(summary.conclusion_label || "等待 HR 确认")}</strong></div><p>${escapeHtml(summary.ai_guidance || report.governance.policy)}</p></div></header>
    <section class="report-section"><h2>管理层摘要</h2><div class="report-three"><div class="report-summary-box"><h3>关键优势</h3>${reportList(summary.strengths, "当前没有达到稳定结论的优势项。")}</div><div class="report-summary-box"><h3>主要风险</h3>${reportList(summary.risks, "当前没有形成明确的风险结论。")}</div><div class="report-summary-box"><h3>评价分歧</h3>${reportList(summary.disagreements, "各轮评价暂未发现明显分歧。")}</div></div></section>
    <section class="report-section"><h2>能力项人工评分</h2><div class="report-competency-list">${competencies}</div></section>
    <section class="report-section"><h2>各轮人工评价</h2><div class="report-round-grid">${rounds}</div></section>
    <section class="report-section"><h2>关键证据摘录</h2>${evidence}</section>
    ${appendixHtml}`;

  const isHr = ["hr", "admin"].includes(state.currentUser?.role);
  const toolbar = $("report-toolbar");
  toolbar.classList.toggle("shared", sharedView || !isHr);
  $("report-version-select").closest("label").classList.toggle("hidden", sharedView || !isHr);
  $("report-audience-select").closest("label").classList.toggle("hidden", !isHr);
  $("report-audience-select").value = report.audience;
  $("report-refresh-btn").classList.toggle("hidden", sharedView || !isHr);
  $("report-lock-confirm-wrap").classList.toggle("hidden", sharedView || !isHr || report.status !== "draft");
  $("report-lock-btn").classList.toggle("hidden", sharedView || !isHr || report.status !== "draft");
  $("report-lock-confirm").checked = false;
  $("report-copy-link-btn").classList.toggle("hidden", sharedView || !isHr);
  $("report-copy-link-btn").disabled = report.status !== "locked";
  $("report-print-link").href = `/api/v1/reports/${encodeURIComponent(report.id)}/print?audience=${encodeURIComponent(report.audience)}`;
}

async function refreshReportDraft() {
  const report = await api(`/api/v1/admin/applications/${state.reportApplicationId}/reports/draft`, { method: "POST" });
  await loadReportCenter(report.id);
  toast(report.status === "draft" ? `${report.version_label} 已按最新面试材料刷新` : "报告草稿已生成");
}

async function lockCurrentReport() {
  if (!$("report-lock-confirm").checked) return toast("请先确认当前报告内容可以锁定", true);
  try {
    const report = await api(`/api/v1/admin/reports/${state.report.id}/lock`, { method: "POST", body: JSON.stringify({ confirmed_by_hr: true }) });
    await loadReportCenter(report.id);
    toast(`${report.version_label} 已锁定，可以复制内部链接`);
  } catch (error) { toast(error.message, true); }
}

async function copyReportLink() {
  if (!state.report?.share_path) return toast("请先由 HR 锁定报告版本", true);
  const link = `${location.origin}${state.report.share_path}`;
  await navigator.clipboard.writeText(link);
  toast("管理层摘要内部链接已复制；打开者仍需使用公司账号登录");
}

function closeReportPanel() {
  $("report-panel").classList.add("hidden");
  const url = new URL(location.href);
  if (url.searchParams.has("report")) {
    url.searchParams.delete("report");
    history.replaceState({}, "", `${url.pathname}${url.search}${url.hash}`);
  }
  if (state.reportReturnView === "workspace" && state.interviewId) {
    $("welcome").classList.add("hidden");
    $("workspace").classList.remove("hidden");
  } else if (state.finalReviewApplicationId && ["hr", "admin"].includes(state.currentUser?.role)) {
    $("final-review-panel").classList.remove("hidden");
  } else {
    $("welcome").querySelector(".welcome-card").classList.remove("hidden");
  }
  state.reportReturnView = "welcome";
}

async function saveManagedRound(event) {
  const container = event.currentTarget.closest("[data-admin-round]");
  const openId = container.querySelector("[data-manage-user]").value;
  const user = state.assignableUsers.find((item) => item.open_id === openId);
  await api(`/api/v1/admin/interviews/${container.dataset.adminRound}`, {
    method: "PATCH",
    body: JSON.stringify({
      scheduled_at: container.querySelector("[data-manage-time]").value,
      interviewer_open_ids: [openId],
      interviewer_names: [user.display_name],
      interview_mode: container.querySelector("[data-manage-mode]").value,
      meeting_source: container.querySelector("[data-manage-source]").value,
    }),
  });
  await loadAdminTasks();
  toast("面试安排与分析方式已更新，面试官的未来 7 天任务会同步变化");
}

async function cancelManagedRound(event) {
  const container = event.currentTarget.closest("[data-admin-round]");
  await api(`/api/v1/admin/interviews/${container.dataset.adminRound}/cancel`, { method: "POST" });
  await loadAdminTasks();
  toast("本轮面试已取消");
}

async function loadInterview() {
  closeSocket();
  const data = await api(`/api/v1/interviews/${state.interviewId}`);
  state.interview = data.interview;
  state.rounds = data.rounds || state.rounds;
  $("candidate-name").textContent = data.candidate.display_name;
  $("job-title").textContent = data.job.title;
  $("meeting-source").textContent = data.interview.meeting_source === "offline" ? "线下面试" : "飞书会议";
  const currentAgenda = state.todayInterviews.find((item) => item.interview_id === state.interviewId);
  $("system-route").textContent = roundLabel(data.routing?.round_type || data.interview.round_type);
  $("route-reason").textContent = `调用题库：${data.routing?.question_bank_version || data.interview.plan_payload.question_bank_version || "默认题库"} · 岗位模型 ${data.routing?.competency_model_version || data.job.competency_model_version}`;
  renderPreparationBrief(data.interview.plan_payload);
  renderRoundTabs();
  await refreshQuestionProgress();
  renderStatus();
  await refreshTranscript();
  await refreshLiveState();
  if (state.interview.status === "completed") {
    await tryLoadScorecard();
    await loadInterviewerReview();
  }
}

function renderPreparationBrief(plan = {}) {
  const context = plan.preparation_context || {};
  const facts = context.candidate_facts || [];
  const focus = context.job_focus_terms || [];
  const prior = plan.prior_round_context || [];
  $("prep-status").textContent = context.personalization_status === "ready" ? "资料可用" : "资料较少";
  $("prep-status").className = `pill ${context.personalization_status === "ready" ? "ready" : "warning"}`;
  $("prep-facts").innerHTML = facts.length ? facts.map((item) => `<span class="prep-chip">${escapeHtml(item.label)}：${escapeHtml(item.value)}</span>`).join("") : '<span class="prep-empty">暂未识别可展示事实，可继续使用统一题库</span>';
  $("prep-focus").innerHTML = focus.length ? focus.map((item) => `<span class="prep-chip focus">${escapeHtml(item)}</span>`).join("") : '<span class="prep-empty">JD 信息较少，暂未提取岗位重点</span>';
  const evidenceCount = prior.reduce((total, item) => total + (item.confirmed_evidence || []).length, 0);
  const questionCount = prior.reduce((total, item) => total + (item.unverified_questions || []).length, 0);
  $("prep-prior").textContent = prior.length ? `${prior.length} 轮 · ${evidenceCount} 条已确认事实 · ${questionCount} 个待验证问题` : "首轮面试，暂无前轮信息";
  const priorDetail = $("prep-prior-detail");
  priorDetail.classList.toggle("hidden", !prior.length);
  if (prior.length) {
    $("prep-prior-content").innerHTML = prior.map((item) => {
      const evidence = (item.confirmed_evidence || []).map((entry) => `<blockquote>“${escapeHtml(entry.quote)}”</blockquote>`).join("") || '<p>暂无人工确认的原话证据。</p>';
      const questions = (item.unverified_questions || []).map((entry) => `<li>${escapeHtml(typeof entry === "string" ? entry : entry.question || "待补充核实")}</li>`).join("");
      return `<article class="prep-prior-round"><h4>${roundLabel(item.source_round_type)} · 已确认材料</h4>${evidence}${questions ? `<p><b>本轮可继续核实：</b></p><ul>${questions}</ul>` : ""}</article>`;
    }).join("") + '<div class="prep-prior-note">为避免先入为主，这里只展示经面试官确认的证据和待核实问题，不展示前轮分数或录用建议。</div>';
  }
  const currentAgenda = state.todayInterviews.find((item) => item.interview_id === state.interviewId);
  const managementReport = currentAgenda?.candidate_dossier?.management_report;
  $("prep-report-btn").classList.toggle("hidden", !managementReport);
  $("prep-report-btn").dataset.reportId = managementReport?.id || "";
  $("prep-boundary").textContent = context.boundary || "仅用于生成核实性问题，不代表候选人已具备或不具备相关能力。";
}

function roundLabel(roundType) {
  return ({ hr: "HR 面", business: "业务面", ceo: "CEO 面", custom: "自定义" })[roundType] || roundType;
}

function renderRoundTabs() {
  $("round-tabs").innerHTML = state.rounds.map((round) =>
    `<button class="round-tab ${round.id === state.interviewId ? "active" : ""}" data-round="${round.id}">${roundLabel(round.round_type)}</button>`
  ).join("");
  document.querySelectorAll(".round-tab").forEach((button) => {
    button.addEventListener("click", async () => {
      state.interviewId = button.dataset.round;
      $("scorecard").classList.add("hidden");
      $("interviewer-review").classList.add("hidden");
      await loadInterview();
      await loadTodayInterviews();
    });
  });
}

function renderPlan(plan = {}) {
  const questions = plan.questions || [];
  const conversationMode = plan.interview_mode === "conversation";
  $("question-panel").classList.toggle("conversation-mode", conversationMode);
  if (conversationMode) {
    $("question-count").textContent = "自由";
    $("required-progress").textContent = "无固定必问题";
    $("question-mix").textContent = "AI 跟随面试官真实提问，只分析回答深度与事实风险";
    $("required-progress-bar").style.width = "0%";
    $("question-list").innerHTML = '<article class="question-card conversation-guide"><div class="question-card-head"><span class="target">自由对话分析</span><span class="question-kind optional">按完整对话评分</span></div><p>请按你的面试习惯提问。AI 会理解每个真实问题和候选人的回答，并在回答较浅时提示补问。</p><small>会后依据完整对话给出岗位证据参考分与是否进入下一轮建议，不要求使用固定题。</small></article>';
    return;
  }
  const progress = new Map((state.questionProgress?.items || []).map((item) => [item.question_id, item]));
  const answerState = new Map((state.questionCoverage || []).map((item) => [item.question_id, item]));
  const sourceLabel = (item) => item.required ? "本轮唯一统一题" : item.source === "resume_jd_match" ? (item.generation_mode === "llm_semantic" ? "AI 深度简历题" : "简历经历 × 岗位重点") : item.source === "resume_personalized" ? "简历核实" : item.source === "prior_round" ? "前轮待验证" : "通用可选";
  const answerLabel = { unanswered: "未回答", shallow: "回答较浅", evidenced: "已有证据" };
  $("question-count").textContent = questions.length;
  $("question-list").innerHTML = questions.map((item, index) => {
    const answer = answerState.get(item.id);
    return `
    <article class="question-card ${item.required ? "required" : ""} ${progress.get(item.id)?.asked ? "asked" : ""} answer-${answer?.status || "unanswered"}">
      <div class="question-card-head">
        <span class="target">${String(index + 1).padStart(2, "0")} · ${escapeHtml(item.competency_name)}</span>
        <span class="question-badges">
          <span class="question-kind ${item.required ? "" : "optional"}">${sourceLabel(item)}</span>
          <span class="answer-state ${answer?.status || "unanswered"}">${answerLabel[answer?.status || "unanswered"]}</span>
        </span>
      </div>
      <p>${escapeHtml(item.question)}</p>
      <small>可选追问：${escapeHtml(item.follow_up)}</small>
      ${answer?.status === "shallow" && answer.missing_dimensions?.length ? `<small class="answer-gap">待补：${escapeHtml(answer.missing_dimensions.join("、"))}</small>` : ""}
      ${item.rationale ? `<small class="question-rationale">建议依据：${escapeHtml(item.rationale)}${item.source_evidence ? ` · ${escapeHtml(item.source_evidence)}` : ""}</small>` : ""}
      ${item.required ? `<button class="mark-asked" data-question-id="${item.id}">${progress.get(item.id)?.asked ? "撤销已问" : "标记已问"}</button>` : ""}
    </article>
  `}).join("") || '<p class="muted">尚未生成计划</p>';
  document.querySelectorAll("[data-question-id]").forEach((button) => button.addEventListener("click", toggleQuestionAsked));
  const total = state.questionProgress?.required_total || 0;
  const asked = state.questionProgress?.required_asked || 0;
  $("required-progress").textContent = `${asked} / ${total} 已完成`;
  const mix = plan.question_mix || {
    required: questions.filter((item) => item.required).length,
    resume_jd_match: questions.filter((item) => item.source === "resume_jd_match").length,
    resume_personalized: questions.filter((item) => item.source === "resume_personalized").length,
    prior_round: questions.filter((item) => item.source === "prior_round").length,
  };
  const mixLabels = [`${mix.required || 0} 道统一必问`, `${mix.resume_jd_match || 0} 道简历经历题`];
  if (mix.company_standard) mixLabels.push(`${mix.company_standard} 道公司通用`);
  if (mix.resume_personalized) mixLabels.push(`${mix.resume_personalized} 道简历定制`);
  if (mix.prior_round) mixLabels.push(`${mix.prior_round} 道前轮待验证`);
  const semanticQuestions = plan.semantic_question_assistance || {};
  if (semanticQuestions.status === "fallback_preserved") mixLabels.push("深度模型未产出有效题，已保留简历核实题");
  if (semanticQuestions.status === "no_grounded_match") mixLabels.push("简历中暂未找到与本轮岗位重点直接相关的经历");
  if (semanticQuestions.status === "degraded") mixLabels.push("深度问题服务暂不可用");
  $("question-mix").textContent = mixLabels.join(" · ");
  $("required-progress-bar").style.width = `${total ? (asked / total) * 100 : 0}%`;
}

async function refreshQuestionProgress() {
  state.questionProgress = await api(`/api/v1/interviews/${state.interviewId}/questions/progress`);
  renderPlan(state.interview.plan_payload);
}

async function toggleQuestionAsked(event) {
  const questionId = event.currentTarget.dataset.questionId;
  const current = state.questionProgress.items.find((item) => item.question_id === questionId);
  state.questionProgress = await api(`/api/v1/interviews/${state.interviewId}/questions/progress`, {
    method: "PUT",
    body: JSON.stringify({ question_id: questionId, asked: !current?.asked, asked_by: state.currentUser?.display_name || "当前面试官" }),
  });
  renderPlan(state.interview.plan_payload);
}

function renderStatus() {
  const interview = state.interview;
  const labels = { planned: "待准备", in_progress: "面试进行中", completed: "已完成" };
  $("session-state").textContent = labels[interview.status] || interview.status;
  const notified = interview.notice_status === "acknowledged";
  $("notice-gate").classList.toggle("done", notified);
  $("notice-check").checked = notified;
  $("notice-check").disabled = notified;
  $("notice-btn").disabled = notified;
  $("notice-btn").textContent = notified ? "已完成告知" : "确认告知";
  $("start-btn").disabled = !notified || interview.status !== "planned";
  $("end-btn").disabled = interview.status !== "in_progress";
  const live = interview.status === "in_progress";
  $("transcript-input").disabled = !live;
  $("speaker-select").disabled = !live;
  $("sample-btn").disabled = !live;
  $("send-btn").disabled = !live;
  $("mic-start-btn").disabled = !live || Boolean(state.mediaStream);
  $("mic-stop-btn").disabled = !state.mediaStream;
  if (live) {
    state.startedAt = Date.now();
    openSocket();
  }
}

async function acknowledgeNotice() {
  if (!$("notice-check").checked) {
    toast("请先确认候选人已收到告知", true);
    return;
  }
  try {
    state.interview = await api(`/api/v1/interviews/${state.interviewId}/notice`, {
      method: "POST",
      body: JSON.stringify({ acknowledged_by: "演示面试官", candidate_was_notified: true }),
    });
    renderStatus();
    toast("告知确认已记录，可开始面试");
  } catch (error) { toast(error.message, true); }
}

async function startInterview() {
  try {
    state.interview = await api(`/api/v1/interviews/${state.interviewId}/start`, { method: "POST" });
    renderStatus();
    toast("面试已开始，实时通道已连接");
  } catch (error) { toast(error.message, true); }
}

async function endInterview() {
  $("end-btn").disabled = true;
  try {
    await stopMicrophone();
    state.interview = await api(`/api/v1/interviews/${state.interviewId}/end`, { method: "POST" });
    closeSocket();
    renderStatus();
    const scorecard = await api(`/api/v1/interviews/${state.interviewId}/scorecard`);
    renderScorecard(scorecard);
    await loadInterviewerReview();
    await showEvaluationView();
    const recommendation = scorecard.recommendation?.ai_recommendation || {};
    const score = recommendation.overall_score == null ? "暂不可评" : `${recommendation.overall_score} / 5`;
    toast(`面试已结束：AI 参考分 ${score} · ${recommendation.label || "等待人工复核"}`);
  } catch (error) {
    toast(error.message, true);
    $("end-btn").disabled = false;
  }
}

function openSocket() {
  if (state.ws && state.ws.readyState <= 1) return;
  const protocol = location.protocol === "https:" ? "wss" : "ws";
  state.ws = new WebSocket(`${protocol}://${location.host}/ws/interviews/${state.interviewId}/live`);
  state.ws.onopen = () => {
    $("ws-state").textContent = "实时通道已连接";
    $("ws-state").classList.add("live");
  };
  state.ws.onclose = () => {
    $("ws-state").textContent = "实时通道未连接";
    $("ws-state").classList.remove("live");
  };
  state.ws.onerror = () => toast("实时通道连接失败", true);
  state.ws.onmessage = (event) => {
    const message = JSON.parse(event.data);
    if (message.type === "error") return toast(message.message, true);
    if (message.type === "live.update") {
      appendTranscript(message.segment);
      renderAnalysis(message.analysis);
    }
  };
}

function closeSocket() {
  if (state.ws) state.ws.close();
  state.ws = null;
}

async function startMicrophone() {
  if (!state.interview || state.interview.status !== "in_progress") {
    toast("请先开始面试", true);
    return;
  }
  if (!navigator.mediaDevices?.getUserMedia) {
    toast("当前浏览器不支持麦克风采集，请使用最新版 Edge 或 Chrome", true);
    return;
  }
  $("mic-start-btn").disabled = true;
  try {
    state.mediaStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
      video: false,
    });
    await connectAudioSocket();
    await createAudioGraph();
    state.audioStartedAt = Date.now();
    state.audioDurationTimer = setInterval(renderAudioDuration, 250);
    $("mic-icon").classList.add("live");
    $("mic-state").textContent = "正在收音并录制";
    $("mic-stop-btn").disabled = false;
    const liveAsr = state.capabilities?.asr?.status === "ready";
    toast(liveAsr ? "麦克风已开启，正在生成实时字幕" : "麦克风已开启；当前保存录音，ASR 未配置");
  } catch (error) {
    await releaseMicrophoneResources();
    $("mic-start-btn").disabled = false;
    const denied = error?.name === "NotAllowedError";
    toast(denied ? "麦克风权限未允许，请在浏览器地址栏重新授权" : `麦克风启动失败：${error.message}`, true);
  }
}

async function connectAudioSocket() {
  const protocol = location.protocol === "https:" ? "wss" : "ws";
  state.audioWs = new WebSocket(`${protocol}://${location.host}/ws/interviews/${state.interviewId}/audio`);
  state.audioWs.binaryType = "arraybuffer";
  state.audioReady = false;
  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("音频通道连接超时")), 7000);
    state.audioWs.onopen = () => state.audioWs.send(JSON.stringify({
      type: "audio.start",
      audio: { format: "pcm_s16le", sample_rate: 16000, channels: 1 },
    }));
    state.audioWs.onerror = () => {
      clearTimeout(timer);
      reject(new Error("音频通道连接失败"));
    };
    state.audioWs.onmessage = (event) => {
      const message = JSON.parse(event.data);
      if (message.type === "audio.ready") {
        clearTimeout(timer);
        state.audioReady = true;
        $("audio-pipeline").textContent = message.pipeline.pipecat_installed ? "Pipecat 帧桥" : "原生帧桥";
        renderAsrStatus({ ...(state.capabilities?.asr || {}), status: message.pipeline.asr_status, provider: message.pipeline.asr_provider });
        resolve();
      } else if (message.type === "audio.metrics") {
        $("audio-duration").textContent = formatDuration(message.duration_ms);
      } else if (message.type === "transcript.interim") {
        $("interim-speaker").textContent = message.segment.provider_speaker_id === null || message.segment.provider_speaker_id === undefined
          ? "识别中"
          : `${speakerSourceLabel(message.segment.provider_speaker_id)} · 识别中`;
        $("interim-text").textContent = message.segment.text_raw;
        $("interim-transcript").classList.remove("hidden");
      } else if (message.type === "transcript.final") {
        $("interim-transcript").classList.add("hidden");
        $("interim-text").textContent = "";
        if (message.speaker_mappings) {
          state.speakerMappings = message.speaker_mappings;
          renderSpeakerMappings();
        }
        appendTranscript(message.segment);
        renderAnalysis(message.analysis);
      } else if (message.type === "analysis.update") {
        renderAnalysis(message.analysis);
      } else if (message.type === "asr.status") {
        renderAsrStatus(message);
        if (message.recovered) toast("实时字幕连接已自动恢复");
      } else if (message.type === "error") {
        toast(message.message, true);
      }
    };
    state.audioWs.onclose = () => {
      state.audioReady = false;
      if (state.mediaStream) toast("音频通道已断开，已停止上传音频", true);
    };
  });
}

async function createAudioGraph() {
  const AudioContextClass = window.AudioContext || window.webkitAudioContext;
  state.audioContext = new AudioContextClass({ latencyHint: "interactive" });
  await state.audioContext.resume();
  state.audioSource = state.audioContext.createMediaStreamSource(state.mediaStream);
  // MVP uses ScriptProcessor for broad compatibility. Production moves to AudioWorklet/WebRTC.
  state.audioProcessor = state.audioContext.createScriptProcessor(4096, 1, 1);
  const silentGain = state.audioContext.createGain();
  silentGain.gain.value = 0;
  state.audioProcessor.onaudioprocess = (event) => {
    if (!state.audioReady || !state.audioWs || state.audioWs.readyState !== WebSocket.OPEN) return;
    const input = event.inputBuffer.getChannelData(0);
    const pcm = downsampleToPcm16(input, state.audioContext.sampleRate, 16000);
    if (pcm.byteLength) state.audioWs.send(pcm.buffer);
    let sum = 0;
    for (let index = 0; index < input.length; index += 1) sum += input[index] * input[index];
    const rms = Math.sqrt(sum / input.length);
    $("audio-level").style.width = `${Math.min(100, Math.max(2, rms * 320))}%`;
  };
  state.audioSource.connect(state.audioProcessor);
  state.audioProcessor.connect(silentGain);
  silentGain.connect(state.audioContext.destination);
}

function downsampleToPcm16(input, inputSampleRate, outputSampleRate) {
  if (outputSampleRate > inputSampleRate) throw new Error("输出采样率不能高于输入采样率");
  const ratio = inputSampleRate / outputSampleRate;
  const outputLength = Math.floor(input.length / ratio);
  const output = new Int16Array(outputLength);
  let inputOffset = 0;
  for (let outputOffset = 0; outputOffset < outputLength; outputOffset += 1) {
    const nextInputOffset = Math.floor((outputOffset + 1) * ratio);
    let sum = 0;
    let count = 0;
    for (; inputOffset < nextInputOffset && inputOffset < input.length; inputOffset += 1) {
      sum += input[inputOffset];
      count += 1;
    }
    const sample = Math.max(-1, Math.min(1, count ? sum / count : 0));
    output[outputOffset] = sample < 0 ? sample * 32768 : sample * 32767;
  }
  return output;
}

async function stopMicrophone() {
  if (!state.mediaStream && !state.audioWs) return;
  if (state.audioWs?.readyState === WebSocket.OPEN) {
    state.audioWs.send(JSON.stringify({ type: "audio.stop" }));
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  await releaseMicrophoneResources();
  $("mic-state").textContent = "麦克风已停止";
  $("mic-icon").classList.remove("live");
  $("audio-level").style.width = "0%";
  $("mic-start-btn").disabled = state.interview?.status !== "in_progress";
  $("mic-stop-btn").disabled = true;
}

async function releaseMicrophoneResources() {
  if (state.audioDurationTimer) clearInterval(state.audioDurationTimer);
  state.audioDurationTimer = null;
  if (state.audioProcessor) {
    state.audioProcessor.disconnect();
    state.audioProcessor.onaudioprocess = null;
  }
  if (state.audioSource) state.audioSource.disconnect();
  if (state.mediaStream) state.mediaStream.getTracks().forEach((track) => track.stop());
  if (state.audioContext && state.audioContext.state !== "closed") await state.audioContext.close();
  if (state.audioWs && state.audioWs.readyState < WebSocket.CLOSING) state.audioWs.close();
  state.audioWs = null;
  state.mediaStream = null;
  state.audioContext = null;
  state.audioSource = null;
  state.audioProcessor = null;
  state.audioReady = false;
}

function renderAudioDuration() {
  $("audio-duration").textContent = formatDuration(Date.now() - state.audioStartedAt);
}

function formatDuration(milliseconds) {
  const totalSeconds = Math.floor(milliseconds / 1000);
  return `${String(Math.floor(totalSeconds / 60)).padStart(2, "0")}:${String(totalSeconds % 60).padStart(2, "0")}`;
}

function sendSegment() {
  const text = $("transcript-input").value.trim();
  if (!text) return;
  if (!state.ws || state.ws.readyState !== WebSocket.OPEN) {
    toast("实时通道尚未连接", true);
    return;
  }
  const startMs = Math.max(0, Date.now() - state.startedAt);
  state.ws.send(JSON.stringify({
    type: "transcript.final",
    payload: {
      speaker_role: $("speaker-select").value,
      speaker_confidence: 1,
      start_ms: startMs,
      end_ms: startMs + Math.max(800, text.length * 120),
      text,
      is_final: true,
    },
  }));
  $("transcript-input").value = "";
}

function fillSample() {
  $("speaker-select").value = "candidate";
  $("transcript-input").value = samples[state.sampleIndex % samples.length];
  state.sampleIndex += 1;
  $("transcript-input").focus();
}

async function refreshTranscript() {
  const [segments, mappings] = await Promise.all([
    api(`/api/v1/interviews/${state.interviewId}/segments`),
    api(`/api/v1/interviews/${state.interviewId}/speaker-mappings`),
  ]);
  state.speakerMappings = mappings;
  renderSpeakerMappings();
  $("transcript-list").innerHTML = "";
  segments.forEach(appendTranscript);
  $("transcript-empty").classList.toggle("hidden", segments.length > 0);
  $("interim-transcript").classList.add("hidden");
}

function appendTranscript(segment) {
  $("transcript-empty").classList.add("hidden");
  const row = document.createElement("div");
  row.className = `transcript-row ${segment.speaker_role}`;
  row.dataset.segmentId = segment.id;
  const speaker = segment.speaker_role === "candidate" ? "候选人" : segment.speaker_role === "interviewer" ? "面试官" : "待确认";
  const source = speakerSourceLabel(segment.provider_speaker_id);
  row.innerHTML = `<span class="speaker">${speaker}${source ? `<small>${source}</small>` : ""}</span><p class="utterance">${escapeHtml(segment.text_corrected || segment.text_raw)}${segment.text_corrected ? '<small class="transcript-corrected">AI 结合上下文修正</small>' : ""}</p>`;
  $("transcript-list").appendChild(row);
  $("transcript-list").scrollTop = $("transcript-list").scrollHeight;
}

function speakerSourceLabel(speakerId) {
  if (speakerId === null || speakerId === undefined) return "";
  return `声源 ${String.fromCharCode(65 + Number(speakerId))}`;
}

function renderSpeakerMappings() {
  const panel = $("speaker-mapping-panel");
  const mappings = state.speakerMappings || [];
  panel.classList.toggle("hidden", mappings.length === 0);
  $("speaker-mapping-list").innerHTML = mappings.map((item) => {
    const roleLabel = item.speaker_role === "candidate" ? "候选人" : item.speaker_role === "interviewer" ? "面试官" : "待确认";
    const status = item.source === "human" ? `人工已确认 · ${roleLabel}` : item.speaker_role === "unknown" ? "置信度不足，暂不用于分析" : `自动判断 ${Math.round(item.confidence * 100)}% · ${roleLabel}`;
    return `<article class="speaker-mapping-card">
      <div><div class="speaker-mapping-status"><strong>${escapeHtml(item.speaker_label)}</strong><span>${escapeHtml(status)}</span></div><p>${escapeHtml(item.sample_text || "等待稳定语句样本")}</p></div>
      <div class="speaker-mapping-actions">
        <button class="speaker-role-button interviewer ${item.speaker_role === "interviewer" ? "active" : ""}" data-speaker-id="${item.provider_speaker_id}" data-speaker-role="interviewer" ${state.speakerConfirmationInFlight ? "disabled" : ""}>面试官</button>
        <button class="speaker-role-button ${item.speaker_role === "candidate" ? "active" : ""}" data-speaker-id="${item.provider_speaker_id}" data-speaker-role="candidate" ${state.speakerConfirmationInFlight ? "disabled" : ""}>候选人</button>
      </div>
    </article>`;
  }).join("");
  document.querySelectorAll("[data-speaker-role]").forEach((button) => button.addEventListener("click", confirmSpeakerRole));
}

async function confirmSpeakerRole(event) {
  if (state.speakerConfirmationInFlight) return;
  const button = event.currentTarget;
  state.speakerConfirmationInFlight = true;
  document.querySelectorAll("[data-speaker-role]").forEach((item) => { item.disabled = true; });
  try {
    state.speakerMappings = await api(`/api/v1/interviews/${state.interviewId}/speaker-mappings/${button.dataset.speakerId}`, {
      method: "PUT",
      body: JSON.stringify({ speaker_role: button.dataset.speakerRole }),
    });
    await refreshTranscript();
    await refreshLiveState();
    toast("说话人身份已确认，本场历史字幕已自动回标");
  } catch (error) {
    toast(error.message, true);
  } finally {
    state.speakerConfirmationInFlight = false;
    renderSpeakerMappings();
  }
}

async function refreshLiveState() {
  const analysis = await api(`/api/v1/interviews/${state.interviewId}/live-state`);
  renderAnalysis(analysis);
}

function renderAnalysis(analysis) {
  if (analysis.model_assistance) renderLlmStatus(analysis.model_assistance);
  (analysis.transcript_corrections || []).forEach((item) => {
    const row = document.querySelector(`[data-segment-id="${CSS.escape(item.segment_id)}"]`);
    if (!row) return;
    const utterance = row.querySelector(".utterance");
    if (utterance) utterance.innerHTML = `${escapeHtml(item.corrected_text)}<small class="transcript-corrected">AI 结合上下文修正</small>`;
  });
  const liveFollowUpsAvailable = state.interview?.status === "in_progress" && analysis.availability !== "waiting_for_candidate_answer";
  const conversationMode = analysis.analysis_mode === "conversation" || state.interview?.interview_mode === "conversation";
  $("analysis-panel").classList.toggle("hidden", conversationMode);
  const coverage = analysis.coverage || [];
  const covered = coverage.filter((x) => x.status !== "uncovered").length;
  $("coverage-ratio").textContent = `${covered}/${coverage.length}`;
  const labels = { uncovered: "未覆盖", mentioned: "已提及", verified: "已核验" };
  const widths = { uncovered: 8, mentioned: 58, verified: 100 };
  $("coverage-list").innerHTML = coverage.map((item) => `
    <div class="coverage-item ${item.status}">
      <div class="coverage-label"><span>${escapeHtml(item.name)}</span><span>${labels[item.status]}</span></div>
      <div class="bar"><i style="width:${widths[item.status]}%"></i></div>
    </div>
  `).join("");

  state.questionCoverage = analysis.question_coverage || [];
  const answerLabels = { unanswered: "未回答", shallow: "回答较浅", evidenced: "已有证据" };
  const visibleQuestions = state.questionCoverage.filter((item) => conversationMode || item.required || ["resume_jd_match", "resume_personalized", "prior_round"].includes(item.source));
  const answeredQuestions = visibleQuestions.filter((item) => item.status !== "unanswered").length;
  $("question-coverage-ratio").textContent = `${answeredQuestions}/${visibleQuestions.length}`;
  $("question-coverage-list").innerHTML = visibleQuestions.map((item) => `
    <article class="question-coverage-item ${item.status}">
      <div><strong>${escapeHtml(item.competency_name)}</strong><span>${answerLabels[item.status]}</span></div>
      ${item.status === "shallow" && item.missing_dimensions?.length ? `<small>待补：${escapeHtml(item.missing_dimensions.join("、"))}</small>` : ""}
    </article>
  `).join("") || '<p class="muted">暂无需要跟踪的问题。</p>';
  if (state.interview?.plan_payload) renderPlan(state.interview.plan_payload);

  const currentSuggestions = liveFollowUpsAvailable ? (analysis.suggestions || []) : [];
  const currentIds = new Set(analysis.current_suggestion_ids || []);
  const history = liveFollowUpsAvailable && analysis.suggestion_history?.length
    ? analysis.suggestion_history
    : currentSuggestions.map((item, index) => ({ ...item, id: `current-${index}`, status: "active" }));
  const orderedHistory = history.slice().sort((left, right) =>
    String(left.created_at || "").localeCompare(String(right.created_at || ""))
  );
  const activeCount = history.filter((item) => item.status === "active").length;
  $("suggestion-history-count").textContent = `${activeCount} 条待处理 · 共 ${history.length} 条`;
  const urgent = history.find((item) =>
    item.status === "active" && currentIds.has(item.id) && item.priority === "high"
    && (
      item.source === "llm_semantic_evidence_gap"
      || item.answer_status === "shallow"
      || String(item.reason || "").includes("回答较浅")
    )
  );
  $("urgent-followup").classList.toggle("hidden", !urgent);
  $("urgent-followup-source").textContent = urgent?.source_question_text ? `对应原问题：${urgent.source_question_text}` : "";
  $("urgent-followup-question").textContent = urgent?.question || "";
  $("urgent-followup-reason").textContent = urgent?.reason || "";
  if (urgent?.id && state.lastUrgentSuggestionId !== urgent.id) {
    state.lastUrgentSuggestionId = urgent.id;
    toast("候选人回答较浅：右侧已生成立即追问建议");
  } else if (!urgent) {
    state.lastUrgentSuggestionId = null;
  }
  const suggestionStatusLabels = { active: "待处理", addressed: "已追问", skipped: "已略过", deferred: "已收起" };
  const suggestionCard = (item) => {
    const isCurrent = currentIds.has(item.id) && item.status === "active";
    return `<article class="suggestion ${escapeHtml(item.priority || "normal")} ${item.status !== "active" ? "resolved" : ""} ${isCurrent ? "current" : ""}">
      <div class="suggestion-meta"><span>${isCurrent ? "当前建议" : escapeHtml(suggestionStatusLabels[item.status] || "已记录")}</span>${item.source === "question_gap" ? "<span>回答深度</span>" : ""}</div>
      ${item.source_question_text ? `<small class="suggestion-source-question"><strong>对应原问题：</strong>${escapeHtml(item.source_question_text)}</small>` : ""}
      ${item.basis_quote ? `<small class="suggestion-basis">依据候选人原话：“${escapeHtml(item.basis_quote)}”</small>` : ""}
      <small>${escapeHtml(item.reason)}</small>
      <p>${escapeHtml(item.question)}</p>
      ${item.status === "active" && !String(item.id).startsWith("current-") ? `<div class="suggestion-actions"><button type="button" class="confirm" data-suggestion-id="${escapeHtml(item.id)}" data-suggestion-action="addressed">已追问</button><button type="button" class="secondary" data-suggestion-id="${escapeHtml(item.id)}" data-suggestion-action="skipped">暂不追问</button></div>` : ""}
    </article>`;
  };
  const activeSuggestions = orderedHistory.filter((item) => item.status === "active").slice(0, 3);
  const archivedSuggestions = orderedHistory.filter((item) => !activeSuggestions.includes(item));
  const emptySuggestionText = state.interview?.status !== "in_progress"
    ? "面试开始并出现候选人回答后，这里才会生成追问。"
    : analysis.availability === "waiting_for_candidate_answer"
      ? "等待候选人回答后生成追问。"
      : analysis.availability === "semantic_analysis_pending"
        ? "AI 正在理解这段回答；没有高价值追问时这里会保持为空。"
      : "当前暂无高价值追问。";
  const suggestionHtml = `${activeSuggestions.map(suggestionCard).join("") || `<p class="muted">${emptySuggestionText}</p>`}${archivedSuggestions.length ? `<details class="suggestion-archive"><summary>查看已处理或已收起的 ${archivedSuggestions.length} 条建议</summary>${archivedSuggestions.map(suggestionCard).join("")}</details>` : ""}`;
  const suggestionList = $("suggestion-list");
  if (suggestionList.innerHTML !== suggestionHtml) {
    suggestionList.innerHTML = suggestionHtml;
    suggestionList.querySelectorAll("[data-suggestion-action]").forEach((button) => button.addEventListener("click", updateSuggestionStatus));
  }

  const evidence = analysis.evidence || [];
  const digest = analysis.evidence_digest || {};
  const digestSummary = digest.summary || {};
  const keyEvidence = digest.key_evidence || evidence.filter((item) => item.direction === "support");
  const risks = digest.risks || evidence.filter((item) => item.direction === "negative");
  const unknowns = digest.unknowns || [];
  const visibleEvidenceCount = keyEvidence.length + risks.length;
  $("evidence-count").textContent = `${visibleEvidenceCount} 条事实`;
  const evidenceCard = (item) => `
    <article class="evidence-item ${item.direction === "negative" ? "risk" : "support"}">
      <div class="evidence-meta"><span>${escapeHtml(item.competency_name || item.competency_id)}</span><span>${item.direction === "support" ? "支持判断" : "风险信号"} · ${Math.round((item.strength || 0) * 100)}%</span></div>
      ${item.source_question_text ? `<small class="evidence-source"><strong>来自问题：</strong>${escapeHtml(item.source_question_text)}</small>` : '<small class="evidence-source">来自本轮自然对话，暂未定位到具体问题</small>'}
      <blockquote>${escapeHtml(item.quote)}</blockquote>
      <p class="evidence-impact"><strong>${escapeHtml(item.decision_impact || "需要结合岗位要求核对")}</strong><span>${escapeHtml(item.why_it_matters || "")}</span></p>
      <div class="evidence-review-state"><span>${escapeHtml(item.review_note || (item.human_status === "pending" ? "待面试官核对语境" : "已人工确认"))}</span>${(item.related_count || 1) > 1 ? `<em>已合并 ${item.related_count} 条相近原话</em>` : ""}</div>
      ${item.human_status === "pending" ? `
        <div class="evidence-actions">
          <button class="confirm" data-evidence="${escapeHtml(item.primary_evidence_id || item.id)}" data-decision="confirmed">确认事实</button>
          <button class="reject" data-evidence="${escapeHtml(item.primary_evidence_id || item.id)}" data-decision="rejected">排除本条</button>
        </div>` : `<span class="reviewed">已人工确认</span>`}
    </article>`;
  const unknownCard = (item) => `
    <article class="evidence-unknown">
      <div><strong>${escapeHtml(item.competency_name || "待验证事项")}</strong><span>${item.status === "shallow" ? "回答较浅" : "尚未回答"}</span></div>
      ${item.source_question_text ? `<p>${escapeHtml(item.source_question_text)}</p>` : ""}
      ${item.basis_quote ? `<small>已有回答：“${escapeHtml(item.basis_quote)}”</small>` : ""}
      <small>${escapeHtml(item.reason || "暂不能形成结论")}${item.missing_dimensions?.length ? ` 待补：${escapeHtml(item.missing_dimensions.join("、"))}` : ""}</small>
    </article>`;
  const evidenceSummaryHtml = `<div class="evidence-digest-summary"><span><strong>${keyEvidence.length}</strong>支持</span><span class="risk"><strong>${risks.length}</strong>风险</span><span class="unknown"><strong>${unknowns.length}</strong>未知</span></div>`;
  const supportHtml = keyEvidence.length ? `<section class="evidence-group"><h4>关键事实</h4>${keyEvidence.map(evidenceCard).join("")}</section>` : "";
  const riskHtml = risks.length ? `<section class="evidence-group risk"><h4>明确风险</h4>${risks.map(evidenceCard).join("")}</section>` : "";
  const unknownHtml = unknowns.length ? `<section class="evidence-group unknown"><h4>尚待验证 <small>不是反向证据</small></h4>${unknowns.map(unknownCard).join("")}</section>` : "";
  const hiddenHtml = digestSummary.hidden_cluster_count ? `<details class="evidence-audit-note"><summary>另有 ${digestSummary.hidden_cluster_count} 组次要事实未展开</summary><p>${escapeHtml(digest.policy || "原始证据仍完整保留。")}</p></details>` : "";
  $("evidence-list").innerHTML = visibleEvidenceCount || unknowns.length
    ? `${evidenceSummaryHtml}${riskHtml}${supportHtml}${unknownHtml}${hiddenHtml}`
    : '<p class="muted">候选人回答后，AI 才会整理关键事实；面试前不会生成。</p>';
  document.querySelectorAll("[data-evidence]").forEach((button) => button.addEventListener("click", reviewEvidence));
}

async function updateSuggestionStatus(event) {
  const button = event.currentTarget;
  try {
    await api(`/api/v1/interviews/${state.interviewId}/suggestions/${encodeURIComponent(button.dataset.suggestionId)}`, {
      method: "PATCH",
      body: JSON.stringify({ status: button.dataset.suggestionAction }),
    });
    await refreshLiveState();
    toast(button.dataset.suggestionAction === "addressed" ? "已记为完成追问" : "已略过该建议，后续对话不会被它卡住");
  } catch (error) { toast(error.message, true); }
}

async function reviewEvidence(event) {
  const button = event.currentTarget;
  try {
    await api(`/api/v1/evidence/${button.dataset.evidence}`, {
      method: "PATCH",
      body: JSON.stringify({ status: button.dataset.decision, reviewed_by: "演示面试官" }),
    });
    await refreshLiveState();
    if (state.interview?.status === "completed") {
      const scorecard = await api(`/api/v1/interviews/${state.interviewId}/scorecard/draft`, { method: "POST" });
      renderScorecard(scorecard);
    }
    toast(button.dataset.decision === "confirmed" ? "证据已确认" : "证据已排除");
  } catch (error) { toast(error.message, true); }
}

async function tryLoadScorecard() {
  try {
    const scorecard = await api(`/api/v1/interviews/${state.interviewId}/scorecard`);
    renderScorecard(scorecard);
  } catch (_) {}
}

async function regenerateScorecard() {
  if (!state.interviewId || state.interview?.status !== "completed") return;
  const button = $("scorecard-refresh-btn");
  const originalLabel = button.textContent;
  button.disabled = true;
  button.textContent = "AI 正在复盘全部对话…";
  try {
    const scorecard = await api(`/api/v1/interviews/${state.interviewId}/scorecard/draft`, { method: "POST" });
    renderScorecard(scorecard);
    const assistance = scorecard.recommendation?.model_assistance || {};
    toast(assistance.status === "degraded" ? "真实模型暂未完成评分，请查看顶部 AI 状态" : "AI 已按本轮全部真实问答重新生成评价");
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
    button.textContent = originalLabel;
  }
}

function renderAnswerLogicReview(review = {}) {
  const section = $("answer-logic-section");
  if (!review || !Object.keys(review).length) {
    section.classList.add("hidden");
    $("answer-logic-review").innerHTML = "";
    return;
  }
  section.classList.remove("hidden");
  const statusLabels = { coherent: "表述较一致", needs_verification: "需要核验", unknown: "信息不足" };
  const flagLabels = {
    factual_conflict: "事实口径冲突",
    timeline_conflict: "时间线冲突",
    ownership_shift: "本人贡献口径变化",
    causal_gap: "因果链待补充",
    claim_needs_verification: "成果主张待核实",
  };
  const dimensions = review.dimensions || [];
  const flags = review.consistency_flags || [];
  const score = review.logic_score == null ? "暂不可评" : `${review.logic_score} / 5`;
  const dimensionsHtml = dimensions.length ? `<div class="logic-dimensions">${dimensions.map((item) => `
    <article class="logic-dimension ${escapeHtml(item.status || "unknown")}">
      <div><strong>${escapeHtml(item.name || item.id)}</strong><span>${escapeHtml(statusLabels[item.status] || "待核验")}</span></div>
      <p>${escapeHtml(item.explanation || "尚无足够信息。")}</p>
      ${(item.quotes || []).length ? `<small>${item.quotes.map((quote) => `“${escapeHtml(quote.quote)}”`).join("；")}</small>` : ""}
    </article>`).join("")}</div>` : "";
  const flagsHtml = flags.length ? `<div class="logic-flags"><h4>需要面试官复核的表述</h4>${flags.map((item) => `
    <article class="logic-flag ${escapeHtml(item.severity || "medium")}">
      <div><strong>${escapeHtml(flagLabels[item.flag_type] || "一致性待核验")}</strong><span>${item.severity === "high" ? "优先核验" : "建议核验"}</span></div>
      <p>${escapeHtml(item.description || "")}</p>
      ${(item.quotes || []).length ? `<blockquote>${item.quotes.map((quote) => `“${escapeHtml(quote.quote)}”`).join("<br>")}</blockquote>` : ""}
      <small><strong>建议核实：</strong>${escapeHtml(item.verification_question || "请候选人澄清前后口径。")}</small>
    </article>`).join("")}</div>` : `<div class="logic-no-conflict">当前没有发现能够由候选人原话直接支持的明显矛盾。没有发现矛盾不代表所有经历已经完成外部验证。</div>`;
  const questions = review.verification_questions || [];
  const questionsHtml = questions.length ? `<div class="logic-verification"><strong>后续可核实</strong><ul>${questions.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul></div>` : "";
  $("answer-logic-review").innerHTML = `
    <div class="logic-overview">
      <div><small>逻辑可追溯参考</small><strong>${escapeHtml(score)}</strong></div>
      <div><small>${escapeHtml(review.label || "回答逻辑核验")}</small><p>${escapeHtml(review.summary || "")}</p></div>
      <span>分析置信度 ${Math.round((review.confidence || 0) * 100)}%</span>
    </div>
    ${dimensionsHtml}${flagsHtml}${questionsHtml}
    <p class="logic-boundary">${escapeHtml(review.boundary || "不能仅凭面试表现判断候选人是否撒谎；异常项只用于人工核实。")}</p>`;
}

function renderScorecard(scorecard) {
  state.scorecard = scorecard;
  renderSidebarScore(scorecard);
  if (scorecard.recommendation?.model_assistance) renderLlmStatus(scorecard.recommendation.model_assistance);
  $("scorecard").classList.remove("hidden");
  const humanDecision = scorecard.recommendation.human_decision;
  const decisionLabels = { advance: "建议进入下一轮", supplementary_interview: "补充面试后再判断", hold: "保留讨论", reject: "不建议进入下一轮" };
  $("scorecard-status").textContent = scorecard.status === "submitted" ? "人工评价已提交" : "需人工复核";
  $("scorecard-status").classList.toggle("warning", scorecard.status !== "submitted");
  const aiRecommendation = scorecard.recommendation.ai_recommendation || {};
  const responseQuality = scorecard.recommendation.response_quality || {};
  const evaluationScope = scorecard.recommendation.evaluation_scope || {};
  const conversationAssessment = scorecard.recommendation.conversation_assessment || {};
  renderAnswerLogicReview(scorecard.recommendation.answer_logic_review || {});
  const conversationMode = scorecard.recommendation.interview_mode === "conversation" || state.interview?.interview_mode === "conversation";
  $("jd-evaluation-section").classList.toggle("hidden", conversationMode);
  $("competency-score-heading").classList.toggle("hidden", conversationMode);
  $("score-grid").classList.toggle("hidden", conversationMode);
  if (conversationMode) {
    const dialogue = scorecard.recommendation.dialogue_analysis || {};
    const responseScore = responseQuality.score == null ? "暂不可评" : `${responseQuality.score} / 5`;
    const evidenceScore = aiRecommendation.overall_score == null ? "暂不可评" : `${aiRecommendation.overall_score} / 5`;
    const batchText = conversationAssessment.total_batches ? ` · 已分析 ${conversationAssessment.completed_batches}/${conversationAssessment.total_batches} 个完整对话批次` : "";
    $("recommendation").innerHTML = `<div class="ai-recommendation-head"><strong>AI 建议：${escapeHtml(aiRecommendation.label || "请结合完整对话人工判断")}</strong></div><div class="evaluation-scope"><strong>本轮方式：自由对话证据评分</strong><span>${escapeHtml((evaluationScope.interviewer_names || []).join("、") || "本轮面试官")} · 不拆能力维度 · ${escapeHtml(evaluationScope.transcript_scope || "本轮全部真实问答")}</span><em>不要求按预设题提问${escapeHtml(batchText)}</em></div><div class="ai-recommendation-metrics"><span><small>岗位证据参考分</small>${escapeHtml(evidenceScore)}</span><span><small>回答质量参考</small>${escapeHtml(responseScore)}</span><span><small>浅回答</small>${dialogue.shallow_answer_count || 0} 个</span></div><p>${escapeHtml(dialogue.summary || aiRecommendation.rationale || scorecard.recommendation.summary)}</p>${(dialogue.observations || []).length ? `<ul>${dialogue.observations.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>` : ""}${(dialogue.risks || []).length ? `<div class="process-warning">${escapeHtml(dialogue.risks.join("；"))}</div>` : ""}${responseQuality.rationale ? `<p class="response-quality-rationale"><strong>${escapeHtml(responseQuality.label || "回答质量观察")}：</strong>${escapeHtml(responseQuality.rationale)}</p>` : ""}<small>置信度 ${Math.round((aiRecommendation.confidence || 0) * 100)}% · ${escapeHtml(scorecard.recommendation.policy)}</small>${humanDecision ? `<hr><strong>面试官结论：${escapeHtml(decisionLabels[humanDecision.decision] || humanDecision.decision)}</strong>${humanDecision.summary_notes ? `<br>${escapeHtml(humanDecision.summary_notes)}` : ""}` : ""}`;
    const summary = scorecard.recommendation.question_evidence_summary || {};
    $("question-evidence-summary").innerHTML = `<article class="evidence-summary-item"><small>实际提问</small><strong>${dialogue.interviewer_question_count || summary.total || 0}</strong></article><article class="evidence-summary-item"><small>回答充分</small><strong>${dialogue.substantive_answer_count || summary.evidenced || 0}</strong></article><article class="evidence-summary-item shallow"><small>回答较浅</small><strong>${dialogue.shallow_answer_count || summary.shallow || 0}</strong></article>`;
    $("jd-assessment-list").innerHTML = "";
    $("score-grid").innerHTML = "";
    const questions = scorecard.next_round_questions || [];
    $("next-round").innerHTML = questions.length ? `<h3>本轮未及时核实，留给下一轮</h3><ul>${questions.map((item) => `<li>${item.source_question_text ? `<small>原问题：${escapeHtml(item.source_question_text)}</small>` : ""}${escapeHtml(item.question)} <small>${escapeHtml(item.reason)}</small></li>`).join("")}</ul>` : "<strong>当前没有必须带入下一轮的问题。</strong>";
    $("knowledge-learning").innerHTML = '<div><span class="kicker">KNOWLEDGE LOOP</span><strong>人工评价后再决定是否沉淀通用问题</strong><small>自由对话不会把候选人原话或个人结论写入知识库。</small></div>';
    $("knowledge-learning").classList.remove("ready");
    $("human-decision").value = humanDecision?.decision || "";
    $("scorecard-notes").value = humanDecision?.summary_notes || "";
    $("scorecard-submit-form").querySelector("button").textContent = scorecard.status === "submitted" ? "更新人工评价" : "提交人工评价";
    return;
  }
  const evidenceScore = aiRecommendation.overall_score == null ? "暂不可评" : `${aiRecommendation.overall_score} / 5`;
  const responseScore = responseQuality.score == null ? "暂不可评" : `${responseQuality.score} / 5`;
  const completenessScore = aiRecommendation.interview_completeness_score == null ? "待计算" : `${aiRecommendation.interview_completeness_score} / 5`;
  const scopeNames = (evaluationScope.dimensions || []).map((item) => item.competency_name).slice(0, 8);
  $("recommendation").innerHTML = `<div class="ai-recommendation-head"><strong>AI 建议：${escapeHtml(aiRecommendation.label || (scorecard.recommendation.decision === "insufficient_evidence" ? "补充证据后再判断" : "进入人工评审"))}</strong></div><div class="evaluation-scope"><strong>本轮评价范围：${escapeHtml(evaluationScope.round_label || roundLabel(state.currentRoundType))}</strong><span>${escapeHtml((evaluationScope.interviewer_names || []).join("、") || "本轮面试官")} · ${(evaluationScope.dimensions || []).length} 项维度 · ${escapeHtml(evaluationScope.transcript_scope || "本轮全部真实问答")}</span>${scopeNames.length ? `<small>${escapeHtml(scopeNames.join(" · "))}</small>` : ""}<em>${evaluationScope.planned_question_dependency === false ? "不要求按预设题提问" : ""}${conversationAssessment.total_batches ? ` · 已复盘 ${conversationAssessment.completed_batches}/${conversationAssessment.total_batches} 个对话批次` : ""}</em></div><div class="ai-recommendation-metrics"><span><small>岗位证据分</small>${escapeHtml(evidenceScore)}</span><span><small>回答质量分</small>${escapeHtml(responseScore)}</span><span><small>面试完整度</small>${escapeHtml(completenessScore)}</span></div><p>${escapeHtml(aiRecommendation.rationale || scorecard.recommendation.summary)}</p>${responseQuality.rationale ? `<p class="response-quality-rationale"><strong>${escapeHtml(responseQuality.label || "回答质量观察")}：</strong>${escapeHtml(responseQuality.rationale)}</p>` : ""}${aiRecommendation.process_warning ? `<div class="process-warning">${escapeHtml(aiRecommendation.process_warning)}</div>` : ""}<small>置信度 ${Math.round((aiRecommendation.confidence || 0) * 100)}% · ${escapeHtml(scorecard.recommendation.policy)}</small>${responseQuality.boundary ? `<small class="response-boundary">${escapeHtml(responseQuality.boundary)}</small>` : ""}${humanDecision ? `<hr><strong>面试官结论：${escapeHtml(decisionLabels[humanDecision.decision] || humanDecision.decision)}</strong>${humanDecision.summary_notes ? `<br>${escapeHtml(humanDecision.summary_notes)}` : ""}<br><small>候选人阶段尚未自动变更</small>` : ""}`;

  const evidenceSummary = scorecard.recommendation.question_evidence_summary || {};
  $("question-evidence-summary").innerHTML = `
    <article class="evidence-summary-item"><small>纳入跟踪的问题</small><strong>${evidenceSummary.tracked_total || 0}</strong></article>
    <article class="evidence-summary-item"><small>已有可复核回答</small><strong>${evidenceSummary.evidenced || 0}</strong></article>
    <article class="evidence-summary-item shallow"><small>回答较浅</small><strong>${evidenceSummary.shallow || 0}</strong></article>
    <article class="evidence-summary-item unanswered"><small>尚未验证</small><strong>${evidenceSummary.unanswered || 0}</strong></article>`;

  const jdLabels = { evidenced: "已有证据", shallow: "回答较浅", unanswered: "尚未验证" };
  const jdAssessments = scorecard.recommendation.jd_assessments || [];
  $("jd-assessment-list").innerHTML = jdAssessments.map((item) => `
    <article class="jd-assessment ${item.status}">
      <div><strong>${escapeHtml(item.competency_name)}</strong><span class="status">${jdLabels[item.status]}</span></div>
      <div><p>${escapeHtml(item.assessment)}</p>${item.answer_excerpt ? `<small>回答摘录：${escapeHtml(item.answer_excerpt)}</small>` : ""}</div>
      <small>${item.missing_dimensions?.length ? `仍缺：${escapeHtml(item.missing_dimensions.join("、"))}` : item.status === "evidenced" ? "需要面试官核对原文语境" : "应带入下一轮继续验证"}</small>
    </article>
  `).join("") || '<p class="muted">本轮没有从简历中找到与岗位重点直接相关的经历题。</p>';

  const humanScores = new Map((scorecard.human_scores || []).map((item) => [item.competency_id, item]));
  $("score-grid").innerHTML = scorecard.ai_scores.map((item) => `
    <article class="score-item">
      <strong>${escapeHtml(item.competency_name)}</strong>
      <span class="score-value ${item.score == null ? "na" : ""}">${item.score == null ? "AI 未评估" : `AI 建议 ${item.score} / 5`}</span>
      <small>${item.evidence_ids.length} 条引用证据 · 置信度 ${Math.round(item.confidence * 100)}%</small>
      <label>面试官评分
        <select data-human-score="${item.competency_id}" ${item.confirmed_evidence_ids?.length ? "" : "disabled"}>
          <option value="">${item.confirmed_evidence_ids?.length ? "请选择" : item.evidence_ids.length ? "请先确认引用证据" : "无证据，不能评分"}</option>
          ${[1, 2, 3, 4, 5].map((score) => `<option value="${score}" ${humanScores.get(item.competency_id)?.score === score ? "selected" : ""}>${score} 分</option>`).join("")}
        </select>
      </label>
    </article>
  `).join("");
  const questions = scorecard.next_round_questions || [];
  const sourceLabels = { jd_gap: "简历相关经历待验证", competency_gap: "能力证据缺口", required_gap: "统一问题缺口", ad_hoc_gap: "临场问题待补" };
  $("next-round").innerHTML = questions.length ? `<h3>本轮未及时核实，留给下一轮</h3><ul>${questions.map((item) => `<li><span class="next-round-tag">${escapeHtml(sourceLabels[item.source_type] || "待验证")}</span>${escapeHtml(item.question)} <small>${escapeHtml(item.reason)}</small></li>`).join("")}</ul>` : "<strong>本轮没有必须带入下一轮的问题，请继续人工核验现有证据。</strong>";
  const learning = scorecard.recommendation.knowledge_learning;
  if (learning) {
    const statusText = learning.pending_hr_review
      ? `已形成 ${learning.proposal_count} 条脱敏提案，其中 ${learning.pending_hr_review} 条等待 HR 审批`
      : learning.proposal_count
        ? "相关改进项已有审批记录，本次没有重复创建"
        : "本轮没有发现适合沉淀的通用 JD 问题缺口";
    $("knowledge-learning").innerHTML = `<div><span class="kicker">KNOWLEDGE LOOP</span><strong>${escapeHtml(statusText)}</strong><small>${escapeHtml(learning.policy)}</small></div>${state.currentUser?.role === "hr" ? '<button type="button" class="secondary compact" data-open-knowledge-center>前往 HR 知识审批</button>' : ""}`;
    $("knowledge-learning").classList.add("ready");
    document.querySelector("[data-open-knowledge-center]")?.addEventListener("click", openKnowledgePanel);
  } else {
    $("knowledge-learning").innerHTML = '<div><span class="kicker">KNOWLEDGE LOOP</span><strong>提交人工评价后生成知识提案</strong><small>系统只学习可复用的问题缺口，不会把简历、逐字稿或候选人评价写入知识库。</small></div>';
    $("knowledge-learning").classList.remove("ready");
  }
  $("human-decision").value = humanDecision?.decision || "";
  $("scorecard-notes").value = humanDecision?.summary_notes || "";
  $("scorecard-submit-form").querySelector("button").textContent = scorecard.status === "submitted" ? "更新人工评价" : "提交人工评价";
}

function renderSidebarScore(scorecard) {
  const recommendation = scorecard?.recommendation?.ai_recommendation || {};
  const score = recommendation.overall_score;
  $("sidebar-evaluation-btn").disabled = false;
  $("sidebar-evaluation-label").textContent = score == null ? "AI 暂不可评" : `AI ${score} / 5`;
  $("sidebar-score-summary").classList.remove("hidden");
  $("sidebar-score-value").textContent = score == null ? "暂不可评" : `${score} / 5`;
  $("sidebar-score-decision").textContent = recommendation.label || "等待人工复核";
}

async function submitScorecard(event) {
  event.preventDefault();
  if (!state.scorecard) return;
  const decision = $("human-decision").value;
  if (!decision) {
    toast("请先选择本轮建议", true);
    return;
  }
  const aiByCompetency = new Map(state.scorecard.ai_scores.map((item) => [item.competency_id, item]));
  const scores = [...document.querySelectorAll("[data-human-score]")]
    .filter((select) => select.value)
    .map((select) => ({
      competency_id: select.dataset.humanScore,
      score: Number(select.value),
      evidence_ids: aiByCompetency.get(select.dataset.humanScore)?.confirmed_evidence_ids || [],
      note: null,
    }));
  const conversationMode = state.scorecard.recommendation.interview_mode === "conversation" || state.interview?.interview_mode === "conversation";
  if (!conversationMode && ["advance", "reject"].includes(decision) && !scores.length) {
    toast("进入下一轮或不建议进入下一轮，至少需要一项已确认证据支持的人工评分", true);
    return;
  }
  if (conversationMode && ["advance", "reject"].includes(decision) && $("scorecard-notes").value.trim().length < 5) {
    toast("自由对话分析不使用能力分；进入下一轮或不建议进入下一轮时，请写明至少 5 个字的对话证据依据", true);
    return;
  }
  try {
    const saved = await api(`/api/v1/interviews/${state.interviewId}/scorecard/submit`, {
      method: "POST",
      body: JSON.stringify({
        submitted_by: state.currentUser?.display_name || "当前面试官",
        decision,
        summary_notes: $("scorecard-notes").value.trim() || null,
        scores,
      }),
    });
    renderScorecard(saved);
    loadPersonalActionCenter().catch(() => {});
    const pendingKnowledge = saved.recommendation.knowledge_learning?.pending_hr_review || 0;
    toast(pendingKnowledge ? `人工评价已保存，并生成 ${pendingKnowledge} 条待 HR 审批的脱敏知识提案` : "人工评价已保存，候选人阶段未自动变更");
  } catch (error) { toast(error.message, true); }
}

async function loadInterviewerReview() {
  const review = await api(`/api/v1/interviews/${state.interviewId}/interviewer-review`);
  $("interviewer-review").classList.remove("hidden");
  $("review-status").textContent = review.status === "reviewed" ? "已完成复盘" : "待招聘负责人复核";
  const metrics = review.automated_metrics;
  const talkShare = metrics.candidate_talk_share == null ? "待说话人确认" : `${Math.round(metrics.candidate_talk_share * 100)}%`;
  const aiRatings = metrics.ai_ratings || {};
  const ratingLabels = { preparation: "准备充分度", question_quality: "提问质量", listening: "倾听质量", fairness: "公平一致性" };
  $("interviewer-metrics").innerHTML = `
    <article class="quality-metric ai-overall"><small>AI 面试质量总分</small><strong>${metrics.ai_overall_score ?? "待积累"}${metrics.ai_overall_score == null ? "" : " / 5"}</strong></article>
    ${Object.entries(aiRatings).map(([name, value]) => `<article class="quality-metric"><small>${escapeHtml(ratingLabels[name] || name)}</small><strong>${value} / 5</strong><span>${escapeHtml(metrics.ai_rating_basis?.[name] || "基于本轮过程证据")}</span></article>`).join("")}
    <article class="quality-metric"><small>统一必问题覆盖</small><strong>${Math.round(metrics.required_question_coverage * 100)}%</strong></article>
    <article class="quality-metric"><small>候选人表达占比</small><strong>${talkShare}</strong></article>
    <article class="quality-metric"><small>候选人回答片段</small><strong>${metrics.candidate_segment_count}</strong></article>
    <article class="quality-metric"><small>证据产出</small><strong>${metrics.evidence_count}</strong></article>
    <div class="quality-flags">${metrics.flags.length ? metrics.flags.map((item) => escapeHtml(item.message)).join(" · ") : "本轮暂未发现明显流程异常。"}</div>`;
  $("review-notes").value = review.notes || "";
}

async function submitInterviewerReview(event) {
  event.preventDefault();
  await api(`/api/v1/interviews/${state.interviewId}/interviewer-review`, {
    method: "POST",
    body: JSON.stringify({ reviewed_by: state.currentUser?.display_name || "招聘负责人", ratings: {}, notes: $("review-notes").value.trim() || null }),
  });
  await loadInterviewerReview();
  toast("面试质量复盘已保存，可用于岗位招聘漏斗分析");
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[char]));
}

$("today-btn").addEventListener("click", enterTodayInterviews);
document.querySelectorAll("[data-app-nav]").forEach((button) => button.addEventListener("click", async () => {
  const target = button.dataset.appNav;
  if (target === "home") await showHomeView();
  else if (target === "interviews") await showInterviewView();
  else if (target === "evaluation") await showEvaluationView();
  else if (target === "admin") await openAdminPanel("admin");
  else if (target === "final-review") await openAdminPanel("final-review");
  else if (target === "jobs") await openJobCenter();
  else if (target === "talent-profile") await openTalentProfilePanel();
  else if (target === "company-profile") await openCompanyProfilePanel();
  else if (target === "quality") await openQualityDashboard();
  else if (target === "knowledge") await openKnowledgePanel();
  else if (target === "notifications") await openNotificationCenter();
  else if (target === "governance") await openGovernanceCenter();
  else if (target === "readiness") await openReadinessCenter();
}));
$("sidebar-score-open").addEventListener("click", showEvaluationView);
$("prep-report-btn").addEventListener("click", (event) => {
  const reportId = event.currentTarget.dataset.reportId;
  if (reportId) openInterviewerReport(reportId).catch((error) => toast(error.message, true));
});
$("font-toggle").addEventListener("click", () => setLargeText(!document.body.classList.contains("large-text")));
$("admin-open-btn").addEventListener("click", openAdminPanel);
$("admin-job-btn").addEventListener("click", () => openJobCenter());
$("admin-import-btn").addEventListener("click", () => openResumeImport());
$("admin-readiness-btn").addEventListener("click", openReadinessCenter);
$("admin-quality-btn").addEventListener("click", openQualityDashboard);
$("admin-company-profile-btn").addEventListener("click", openCompanyProfilePanel);
$("admin-profile-btn").addEventListener("click", () => openTalentProfilePanel());
$("admin-knowledge-btn").addEventListener("click", openKnowledgePanel);
$("admin-governance-btn").addEventListener("click", openGovernanceCenter);
$("admin-notifications-btn").addEventListener("click", openNotificationCenter);
$("readiness-close-btn").addEventListener("click", closeReadinessCenter);
$("readiness-refresh-btn").addEventListener("click", loadReadinessCenter);
$("notification-close-btn").addEventListener("click", closeNotificationCenter);
$("notification-sync-btn").addEventListener("click", syncNotificationQueue);
$("notification-refresh-btn").addEventListener("click", loadNotificationCenter);
$("notification-confirm").addEventListener("change", updateNotificationSendState);
$("notification-send-btn").addEventListener("click", sendSelectedNotifications);
$("governance-close-btn").addEventListener("click", closeGovernanceCenter);
$("governance-refresh-btn").addEventListener("click", loadGovernanceCenter);
$("governance-confirm").addEventListener("change", updateGovernanceCleanupState);
$("governance-cleanup-btn").addEventListener("click", executeGovernanceCleanup);
$("knowledge-close-btn").addEventListener("click", closeKnowledgePanel);
$("knowledge-refresh-btn").addEventListener("click", loadKnowledgeCenter);
$("system-docs-confirm").addEventListener("change", updateSystemDocsSyncState);
$("system-docs-sync-btn").addEventListener("click", syncSystemDocs);
$("quality-dashboard-close").addEventListener("click", closeQualityDashboard);
$("quality-dashboard-refresh").addEventListener("click", loadQualityDashboard);
$("quality-job-select").addEventListener("change", loadQualityDashboard);
$("company-profile-close-btn").addEventListener("click", closeCompanyProfilePanel);
$("company-profile-form").addEventListener("submit", saveCompanyProfileDraft);
$("company-competency-add").addEventListener("click", () => {
  const current = readCompanyCompetencies(false);
  if (current.length >= 8) return toast("公司通用能力最多设置 8 项", true);
  current.push(blankCompanyCompetency());
  renderCompanyCompetencyEditor(current);
});
$("profile-close-btn").addEventListener("click", closeTalentProfilePanel);
$("profile-refresh-btn").addEventListener("click", loadTalentProfileCenter);
$("profile-generate-btn").addEventListener("click", generateTalentProfileDraft);
$("profile-job-select").addEventListener("change", () => { clearHistoricalImport(); loadTalentProfileCenter(); });
$("profile-history-import-btn").addEventListener("click", () => $("profile-history-file").click());
$("profile-history-file").addEventListener("change", (event) => beginHistoricalImport(event.target.files?.[0]));
$("historical-import-cancel").addEventListener("click", clearHistoricalImport);
$("historical-import-commit").addEventListener("click", commitHistoricalImport);
$("historical-select-all").addEventListener("change", (event) => {
  document.querySelectorAll("[data-historical-select]").forEach((input) => { input.checked = event.target.checked; });
});
$("job-center-close-btn").addEventListener("click", closeJobCenter);
$("job-new-btn").addEventListener("click", startNewJob);
$("job-search-input").addEventListener("input", renderJobList);
$("job-editor-form").addEventListener("submit", saveJobDefinition);
$("job-jd-input").addEventListener("input", updateJobJdCount);
$("job-jd-file-btn").addEventListener("click", () => $("job-jd-file").click());
$("job-jd-file").addEventListener("change", importJobJdFile);
$("job-open-profile-btn").addEventListener("click", openSelectedJobProfile);
$("job-import-candidates-btn").addEventListener("click", openSelectedJobResumeImport);
$("resume-import-close-btn").addEventListener("click", closeResumeImport);
$("import-job-select").addEventListener("change", renderImportJobContext);
$("import-create-job-btn").addEventListener("click", () => openJobCenter());
$("resume-batch-files").addEventListener("change", (event) => beginResumeUpload(event.target.files));
$("import-select-all").addEventListener("change", (event) => {
  document.querySelectorAll("[data-import-select]:not(:disabled)").forEach((input) => { input.checked = event.target.checked; });
  updateImportSummary();
});
$("import-commit-btn").addEventListener("click", commitResumeImport);
const resumeDropzone = $("resume-dropzone");
["dragenter", "dragover"].forEach((name) => resumeDropzone.addEventListener(name, (event) => { event.preventDefault(); resumeDropzone.classList.add("dragover"); }));
["dragleave", "drop"].forEach((name) => resumeDropzone.addEventListener(name, (event) => { event.preventDefault(); resumeDropzone.classList.remove("dragover"); }));
resumeDropzone.addEventListener("drop", (event) => beginResumeUpload(event.dataTransfer.files));
$("task-close-btn").addEventListener("click", closeTaskCreator);
$("workspace-task-btn").addEventListener("click", openAdminPanel);
$("admin-close-btn").addEventListener("click", closeAdminPanel);
$("admin-create-btn").addEventListener("click", openTaskCreator);
$("task-job-select").addEventListener("change", syncTaskJobFields);
$("task-job-manage-btn").addEventListener("click", () => openJobCenter());
$("admin-refresh-btn").addEventListener("click", loadAdminTasks);
$("final-review-close-btn").addEventListener("click", closeFinalReview);
$("final-report-open-btn").addEventListener("click", () => openReportCenter());
$("report-close-btn").addEventListener("click", closeReportPanel);
$("report-version-select").addEventListener("change", (event) => loadReportVersion(event.target.value));
$("report-audience-select").addEventListener("change", (event) => { state.reportAudience = event.target.value; loadReportVersion(state.report.id); });
$("report-refresh-btn").addEventListener("click", refreshReportDraft);
$("report-lock-btn").addEventListener("click", lockCurrentReport);
$("report-copy-link-btn").addEventListener("click", copyReportLink);
$("final-decision-form").addEventListener("submit", submitFinalDecision);
$("scorecard-refresh-btn").addEventListener("click", regenerateScorecard);
$("logout-btn").addEventListener("click", logout);
$("task-delete-close").addEventListener("click", closeTaskDeleteDialog);
$("task-delete-cancel").addEventListener("click", closeTaskDeleteDialog);
$("task-delete-confirm").addEventListener("click", confirmTaskDeletion);
$("task-delete-dialog").addEventListener("cancel", () => { state.taskDeletion = null; });
document.querySelectorAll("[data-dev-login]").forEach((button) => button.addEventListener("click", () => devLogin(button.dataset.devLogin)));
$("task-form").addEventListener("submit", createInterviewTask);
document.querySelectorAll("#task-form [data-round-enabled]").forEach((checkbox) => checkbox.addEventListener("change", updateTaskRoundFlow));
document.querySelectorAll("#task-form [data-round-move]").forEach((button) => button.addEventListener("click", () => moveTaskRound(button.closest("[data-round-form]"), button.dataset.roundMove)));
$("resume-file").addEventListener("change", () => importDocument("resume-file", "resume_text"));
$("notice-btn").addEventListener("click", acknowledgeNotice);
$("start-btn").addEventListener("click", startInterview);
$("end-btn").addEventListener("click", endInterview);
$("mic-start-btn").addEventListener("click", startMicrophone);
$("mic-stop-btn").addEventListener("click", stopMicrophone);
$("sample-btn").addEventListener("click", fillSample);
$("send-btn").addEventListener("click", sendSegment);
$("interviewer-review-form").addEventListener("submit", submitInterviewerReview);
$("scorecard-submit-form").addEventListener("submit", submitScorecard);
$("transcript-input").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    sendSegment();
  }
});

window.addEventListener("beforeunload", () => {
  closeSocket();
  if (state.mediaStream) state.mediaStream.getTracks().forEach((track) => track.stop());
  if (state.audioWs) state.audioWs.close();
});
setLargeText(localStorage.getItem("interview-large-text") === "1");
checkHealth();
