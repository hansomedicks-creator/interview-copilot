/* Draft edits are isolated per job. Network responses cannot replace another job's form. */
function balancedAssessmentWeights(criteria) {
  if (!criteria.length || criteria.length > 100) return [];
  const values = criteria.map(c => Math.max(1, Number(c.weight) || 1));
  const total = values.reduce((a, b) => a + b, 0);
  if (total === 100 && values.every(Number.isInteger)) return [...values];
  const remainder = 100 - values.length;
  const exact = values.map(v => v / total * remainder);
  const result = exact.map(v => 1 + Math.floor(v));
  const order = exact.map((v, i) => ({i, fraction: v - Math.floor(v)})).sort((a, b) => b.fraction - a.fraction || a.i - b.i);
  order.slice(0, 100 - result.reduce((a, b) => a + b, 0)).forEach(v => result[v.i]++);
  return result;
}

const jobAssessmentEditor = (() => {
  const sessions = new Map();
  let currentId = null;
  const clone = value => JSON.parse(JSON.stringify(value));
  const root = () => document.getElementById("job-assessment-editor");
  const rounds = {business: "业务面", hr: "HR 面", ceo: "CEO 面", custom: "补充面试"};
  const button = (action, label, type = "secondary") => `<button type="button" class="${type}" data-assessment-action="${action}">${label}</button>`;
  function entry(data) {
    const version = data?.draft || data?.active;
    return {data: data || {}, version, criteria: clone(version?.criteria || []), dirty: false, busy: false, preview: null};
  }
  function acceptResponse(jobId, data) {
    sessions.set(jobId, entry(data));
    const job = state.jobCenterJobs.find(item => item.id === jobId);
    if (job) job.assessment = data;
  }
  function mount(jobId, data) {
    currentId = jobId;
    if (!jobId) { root().classList.add("hidden"); return; }
    if (!sessions.get(jobId)?.dirty && !sessions.get(jobId)?.busy) sessions.set(jobId, entry(data));
    root().classList.remove("hidden");
    render();
  }
  function render() {
    const s = sessions.get(currentId);
    if (!s) return;
    const v = s.version;
    const active = s.data.active;
    root().innerHTML = `<h3>岗位考核项与权重</h3>
      <p>推荐 → 调整 → 确认生效。只评岗位相关行为，不按背景或个人偏好打分。</p>
      <p class="helper">${active ? `当前生效 v${active.version}` : "尚未确认标准"} · ${v ? `正在编辑 v${v.version}${v.status === "draft" ? " 草稿" : "（保存会另建草稿）"}` : "点击下方生成推荐"}。新标准仅用于新建面试流程；已有流程及其后续轮次保留原版本。</p>
      ${active?.source_changed ? '<p class="process-warning">JD 已更新，新面试仍使用旧版已确认标准。请复核新草稿并生效后，再安排新流程。</p>' : ""}
      ${v?.recommendation_status !== "ai_recommended" ? `<p class="process-warning">${v?.status === "active" ? "此版本已由 HR 确认；初始条目来自本地初稿，不是模型语义推荐。" : "暂无有效 AI 推荐。初始条目仅是本地初稿，等权不代表岗位重要性，请人工调整或重新推荐。"}</p>` : ""}
      <fieldset class="assessment-fields" ${s.busy ? "disabled" : ""}>
      <div class="assessment-toolbar">${button("recommend", "根据已保存 JD 重新推荐")}${button("add", "＋ 增加考核项")}${button("balance", "预览平衡至 100%")}</div>
      <div class="assessment-criteria">${s.criteria.map((c, i) => `<article class="assessment-criterion" data-assessment-index="${i}">
        <div class="assessment-heading"><label>考核项<input data-field="name" aria-label="第 ${i+1} 项名称" maxlength="80" value="${escapeHtml(c.name)}" /></label>
        <label>权重 %<input data-field="weight" type="number" min="1" max="100" step="1" value="${Number(c.weight)}" /></label>${button(`delete:${i}`, "移除", "text-button")}</div>
        <label>要验证的工作能力<textarea data-field="definition" rows="2" maxlength="600">${escapeHtml(c.definition)}</textarea></label>
        <div class="assessment-rounds"><label><input data-field="critical" type="checkbox" ${c.critical ? "checked" : ""} />核心项（本轮证据不足时不直接建议通过）</label>
        <span>适用环节：</span>${Object.entries(rounds).map(([key,label]) => `<label><input type="checkbox" data-round="${key}" ${c.round_types.includes(key) ? "checked" : ""} />${label}</label>`).join("")}</div>
        <small>推荐依据：${escapeHtml(c.reason || "人工新增")}</small>
        <details><summary>评分标准与 JD 依据</summary>${c.source_excerpt ? `<blockquote>${escapeHtml(c.source_excerpt)}</blockquote>` : ""}
          ${[1,3,5].map(n => `<label>${n} 分${n===3 ? " · 独立胜任常规工作" : n===1 ? " · 已有明确不符事实" : " · 复杂情境表现"}<textarea rows="2" data-anchor="${n}" maxlength="600">${escapeHtml(c.score_anchors[String(n)] || "")}</textarea></label>`).join("")}
          <small>未涉及该能力 = 未知，不是 1 分。</small></details>
        </article>`).join("") || '<p class="empty-state">尚无考核项。生成推荐或手动添加后，再确认标准。</p>'}</div>
      ${v?.recommendations?.length ? `<details><summary>查看最新推荐方案（不会自动覆盖人工调整）</summary><ul>${v.recommendations.map(c => `<li><strong>${escapeHtml(c.name)} · ${c.weight}%</strong>：${escapeHtml(c.reason)}</li>`).join("")}</ul>${button("apply-recommendations", "将此推荐方案替换到编辑区（仍需保存）")}</details>` : ""}
      ${s.preview ? `<div class="assessment-preview"><strong>平衡预览</strong><ul>${s.criteria.map((c,i)=>`<li>${escapeHtml(c.name)}：${c.weight}% → ${s.preview[i]}%</li>`).join("")}</ul>${button("apply-balance", "应用预览权重")}${button("cancel-preview", "取消")}</div>` : ""}
      <div class="assessment-footer"><strong data-total aria-live="polite"></strong><span data-save-state></span>${button("save", "保存草稿")}${button("activate", "确认并生效", "primary")}</div>
      <p class="helper">生效需合计 100%，每项至少指定一个环节并写明评分标准。同一项可由多轮验证，不重复累加岗位权重；总分不是自动录用或淘汰指令。</p></fieldset>`;
    root().oninput = event => {
      const card = event.target.closest("[data-assessment-index]");
      if (!card) return;
      const c = s.criteria[Number(card.dataset.assessmentIndex)], target = event.target;
      if (target.dataset.field) c[target.dataset.field] = target.type === "checkbox" ? target.checked : target.type === "number" ? Number(target.value) : target.value;
      if (target.dataset.anchor) c.score_anchors[target.dataset.anchor] = target.value;
      if (target.dataset.round) c.round_types = [...card.querySelectorAll("[data-round]:checked")].map(el=>el.dataset.round);
      s.dirty = true; s.preview = null; updateFooter(s);
      root().querySelector(".assessment-preview")?.remove();
    };
    root().onclick = event => { const target = event.target.closest("[data-assessment-action]"); if (target) act(target.dataset.assessmentAction); };
    updateFooter(s);
  }
  function updateFooter(s) {
    const total = s.criteria.reduce((sum,c)=>sum+Number(c.weight || 0),0);
    root().querySelector("[data-total]").textContent = `合计 ${total}%${total === 100 ? "" : " · 生效需 100%"}`;
    root().querySelector("[data-save-state]").textContent = s.busy ? "处理中…" : s.dirty ? "有未保存修改" : "已保存";
    root().querySelector('[data-assessment-action="activate"]').disabled = !s.criteria.length || total !== 100 || (s.version?.status === "active" && !s.dirty) || s.version?.source_changed;
  }
  function validate(s) {
    if (!s.criteria.length || s.criteria.length > 20) throw new Error("请保留 1–20 项考核标准");
    for (const c of s.criteria) {
      if (c.name.trim().length < 2 || c.definition.trim().length < 5 || !c.round_types.length || !Number.isInteger(c.weight) || c.weight < 1 || c.weight > 100 || [1,3,5].some(n=>!c.score_anchors[String(n)] || c.score_anchors[String(n)].trim().length < 5)) throw new Error(`请补全“${c.name || "新考核项"}”的名称、能力说明、整数权重、适用环节和评分标准`);
    }
  }
  async function save(jobId) {
    const s = sessions.get(jobId);
    if (!s) return;
    if (s.busy) throw new Error("考核标准正在保存，请稍候");
    validate(s);
    if (!s.version) throw new Error("请先生成一份推荐草稿，再保存编辑内容");
    const body = {version_id: s.version.id, revision: s.version.revision, criteria: clone(s.criteria)};
    s.busy = true; if (currentId === jobId) render();
    try {
      const data = await api(`/api/v1/admin/jobs/${jobId}/assessment`, {method:"PUT", body:JSON.stringify(body)});
      acceptResponse(jobId, data);
    } finally { s.busy = false; if (currentId === jobId) render(); }
  }
  async function act(action) {
    const id = currentId, s = sessions.get(id);
    if (!s || s.busy) return;
    try {
      if (action.startsWith("delete:")) { s.criteria.splice(Number(action.split(":")[1]),1); s.dirty=true; s.preview=null; }
      else if (action === "add") {
        if (!s.version) return toast("请先生成推荐草稿，再增删考核项", true);
        if (s.criteria.length >= 20) return toast("最多 20 项，建议聚焦关键工作能力", true);
        s.criteria.push({id:`manual-${crypto.randomUUID()}`, name:"", definition:"", weight:10, critical:false, round_types:["business"], reason:"人工新增", source_excerpt:"", score_anchors:{"1":"", "3":"", "5":""}}); s.dirty=true; s.preview=null;
      } else if (action === "balance") s.preview = balancedAssessmentWeights(s.criteria);
      else if (action === "cancel-preview") s.preview=null;
      else if (action === "apply-balance") { if (s.preview) s.criteria.forEach((c,i)=>c.weight=s.preview[i]); s.preview=null; s.dirty=true; }
      else if (action === "apply-recommendations") { s.criteria=clone(s.version.recommendations); s.dirty=true; s.preview=null; }
      else if (action === "save") { await save(id); toast("考核项草稿已保存，尚未改变生效标准"); }
      else if (action === "activate") {
        if (s.dirty || s.version?.status !== "draft") await save(id);
        const fresh = sessions.get(id); fresh.busy=true; if(currentId===id)render();
        const data = await api(`/api/v1/admin/jobs/${id}/assessment/activate`, {method:"POST",body:JSON.stringify({version_id:fresh.version.id,revision:fresh.version.revision,confirmed:true})});
        acceptResponse(id,data); toast(`考核标准 v${data.active.version} 已生效，仅应用于新面试流程`);
      } else if (action === "recommend") {
        if (s.dirty) await save(id);
        const fresh = sessions.get(id); fresh.busy=true; if(currentId===id)render();
        const data = await api(`/api/v1/admin/jobs/${id}/assessment/recommend`, {method:"POST"});
        acceptResponse(id,data); toast("推荐已更新。已保存的人工调整保持不变，请核对后决定是否替换");
      }
    } catch (error) { toast(error.message, true); }
    finally { const fresh=sessions.get(id); if(fresh)fresh.busy=false; if(currentId===id)render(); }
  }
  window.addEventListener("beforeunload", event=>{ if([...sessions.values()].some(s=>s.dirty || s.busy)){event.preventDefault();event.returnValue="";} });
  return {mount, saveIfDirty: async id=>{if(sessions.get(id)?.dirty)await save(id);}, isBusy:id=>Boolean(sessions.get(id)?.busy)};
})();
