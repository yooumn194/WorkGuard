const state = { workspaceId: "", apiKey: "", workspaceKey: "", changes: [], selectedId: "", decisions: {}, expandedDiff: new Set() };
const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

const escapeHtml = (value = "") => String(value).replace(/[&<>'"]/g, (char) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;"
})[char]);

async function api(path, options = {}) {
  const apiKeyInput = $("#api-key");
  const workspaceKeyInput = $("#workspace-key");
  if (apiKeyInput) state.apiKey = apiKeyInput.value.trim();
  if (workspaceKeyInput) state.workspaceKey = workspaceKeyInput.value.trim();
  const headers = new Headers(options.headers || {});
  if (state.apiKey) headers.set("X-API-Key", state.apiKey);
  if (state.workspaceKey) headers.set("X-Workspace-Key", state.workspaceKey);
  const response = await fetch(`/api${path}`, { ...options, headers });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || `请求失败 (${response.status})`);
  return payload;
}

function toast(message, isError = false) {
  const node = $("#toast");
  node.textContent = message;
  node.className = isError ? "show error" : "show";
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { node.className = ""; }, 3200);
}

function statusLabel(status) {
  return ({ pending_approval: "待审批", executed: "已执行", rolled_back: "已回滚",
    partially_executed: "部分执行", rollback_failed: "回滚待重试",
    rejected: "已拒绝", verification_failed: "验证失败", reviewing: "需复核" })[status] || status;
}

async function loadWorkspace(workspaceId, preferredChange = "") {
  state.apiKey = $("#api-key").value.trim();
  state.workspaceKey = $("#workspace-key").value.trim();
  state.workspaceId = workspaceId.trim();
  if (!state.workspaceId) return;
  $("#workspace-id").value = state.workspaceId;
  $("#upload-workspace-id").textContent = state.workspaceId;
  state.changes = await api(`/workspaces/${state.workspaceId}/changes`);
  state.selectedId = preferredChange || state.selectedId || state.changes[0]?.change_id || "";
  if (!state.changes.some((c) => c.change_id === state.selectedId)) state.selectedId = state.changes[0]?.change_id || "";
  renderList();
  if (state.selectedId) await loadDetail(state.selectedId);
  else $("#change-detail").innerHTML = emptyDetail("尚未检测到事实变更", "上传带有明确改期决策的新文档后，变更会出现在这里。");
  $("#setup").classList.add("hidden");
  loadEntityCount().catch(() => {});
}

function renderList() {
  $("#change-count").textContent = state.changes.length;
  $("#metric-changes").textContent = state.changes.length;
  $("#metric-approval").textContent = state.changes.filter((c) => c.status === "pending_approval").length;
  const list = $("#change-list");
  if (!state.changes.length) { list.innerHTML = '<div class="empty">暂无变更</div>'; return; }
  list.innerHTML = state.changes.map((change) => `
    <button class="change-row ${change.change_id === state.selectedId ? "active" : ""}" data-change-id="${escapeHtml(change.change_id)}">
      <span class="change-row-top"><strong>${escapeHtml(change.entity)} · ${escapeHtml(change.predicate)}</strong><span class="status ${escapeHtml(change.status)}">${escapeHtml(statusLabel(change.status))}</span></span>
      <span class="date-shift">${escapeHtml(change.old_value)} → ${escapeHtml(change.new_value)}</span>
      <small>confidence ${(Number(change.confidence) * 100).toFixed(0)}%</small>
    </button>`).join("");
  $$(".change-row").forEach((row) => row.addEventListener("click", () => {
    loadDetail(row.dataset.changeId).catch((error) => toast(error.message, true));
  }));
}

function emptyDetail(title, copy) {
  return `<div class="empty large"><span class="empty-icon">◎</span><strong>${escapeHtml(title)}</strong><small>${escapeHtml(copy)}</small></div>`;
}

async function loadDetail(changeId) {
  state.selectedId = changeId;
  state.decisions = {};
  state.expandedDiff = new Set();
  renderList();
  const change = await api(`/changes/${changeId}`);
  $("#metric-conflicts").textContent = change.conflicts.filter((c) => c.verdict === "conflict").length;
  const actionable = change.status === "pending_approval";
  state.decisions = Object.fromEntries(
    (change.plan?.actions || [])
      .filter((action) => actionable && isModifiable(action))
      .map((action) => [action.action_id, "approve"])
  );
  const rollbackable = ["executed", "partially_executed", "verification_failed", "rollback_failed"].includes(change.status);
  const batchCount = Number(change.approval_scope?.change_count || 1);
  $("#change-detail").innerHTML = `
    <div class="detail-header">
      <div class="detail-header-row"><div><p class="eyebrow">CHANGE EVENT · ${escapeHtml(change.change_id)}</p><h2>${escapeHtml(change.entity)} / ${escapeHtml(change.predicate)}</h2><div class="shift">${escapeHtml(change.old_value)} &nbsp;→&nbsp; ${escapeHtml(change.new_value)}</div></div><span class="status ${escapeHtml(change.status)}">${escapeHtml(statusLabel(change.status))}</span></div>
      <div class="source"><strong>Evidence · ${escapeHtml(change.source.artifact)} @ ${escapeHtml(change.source.location)} · Authority ${escapeHtml(change.source.authority)}</strong><br>“${escapeHtml(change.source.evidence)}”</div>
    </div>
    <div class="detail-actions">
      ${(actionable || rollbackable) && batchCount > 1 ? `<div class="batch-notice">本次 LangGraph Run 包含 ${batchCount} 个变更；批准、拒绝或回滚会作用于整个批次。</div>` : ""}
      ${actionable ? '<button class="primary-button" data-action="approve-selected">提交所选动作</button><button class="secondary-button" data-action="approve">全部批准</button><button class="danger-button" data-action="reject">拒绝计划</button>' : ''}
      ${rollbackable ? '<button class="danger-button" data-action="rollback">回滚修改</button>' : ''}
      <button class="secondary-button" data-action="refresh">刷新状态</button>
    </div>
    <div class="detail-section">
      <div class="section-title"><h3>Direct conflicts</h3><span>${change.conflicts.filter((c) => c.verdict === "conflict").length} 项明确冲突</span></div>
      ${renderFindings(change.conflicts.filter((c) => c.verdict === "conflict"), "conflict")}
    </div>
    <div class="detail-section">
      <div class="section-title"><h3>Potential impacts</h3><span>仅提示，不自动修改</span></div>
      ${renderFindings(change.impacts, "impact")}
    </div>
    <div class="detail-section">
      <div class="section-title"><h3>Change plan</h3><span>${change.plan.actions.length} 个动作${actionable ? " · 未勾选 = 拒绝（fail-closed）" : ""}</span></div>
      ${renderPlan(change.plan.actions, actionable)}
    </div>`;
  $$('[data-action]').forEach((button) => button.addEventListener("click", () => handleAction(button.dataset.action, change)));
  $$('[data-toggle-diff]').forEach((button) => button.addEventListener("click", () => toggleDiff(button.dataset.toggleDiff, change.change_id)));
  $$('input[data-decision]').forEach((box) => box.addEventListener("change", () => {
    state.decisions[box.dataset.decision] = box.checked ? "approve" : "reject";
  }));
}

function renderFindings(items, type) {
  if (!items.length) return '<div class="empty">无相关项</div>';
  return items.map((item) => `<div class="finding ${type}"><span class="finding-icon">${type === "conflict" ? "!" : "?"}</span><div><strong>${escapeHtml(item.artifact)}${item.location ? ` · ${escapeHtml(item.location)}` : ""}</strong><small>${escapeHtml(item.reason)}</small></div><span class="confidence">${Math.round(Number(item.confidence) * 100)}%</span></div>`).join("");
}

function isModifiable(action) {
  return ["update_artifact", "suggest_patch"].includes(action.action_type);
}

function renderPlan(actions, actionable) {
  if (!actions.length) return '<div class="empty">无需执行动作</div>';
  const rows = actions.map((action) => {
    const modifiable = isModifiable(action);
    const checkbox = actionable && modifiable
      ? `<input type="checkbox" data-decision="${escapeHtml(action.action_id)}" checked aria-label="批准该动作">`
      : '<span class="muted">—</span>';
    const diffButton = action.before_text && action.before_text !== action.after_text
      ? `<button class="link-button" data-toggle-diff="${escapeHtml(action.action_id)}">Diff</button>`
      : "";
    return `<tr>
      <td>${checkbox}</td>
      <td>${escapeHtml(action.artifact)}<small class="row-sub">${escapeHtml(action.action_type)} · ${escapeHtml(action.method)} · ${escapeHtml((action.locations || []).join(", "))}</small></td>
      <td class="risk-${escapeHtml(action.risk)}">${escapeHtml(action.risk)}</td>
      <td>${escapeHtml(action.old_value)} → ${escapeHtml(action.new_value)}</td>
      <td>${escapeHtml(action.status)}</td>
      <td>${diffButton}</td>
    </tr>
    <tr class="diff-row ${state.expandedDiff.has(action.action_id) ? "" : "hidden"}" id="diff-${escapeHtml(action.action_id)}">
      <td colspan="6">${renderDiff(action)}</td>
    </tr>`;
  }).join("");
  return `<table class="plan-table"><thead><tr><th></th><th>ARTIFACT / ACTION</th><th>RISK</th><th>CHANGE</th><th>STATUS</th><th></th></tr></thead><tbody>${rows}</tbody></table>`;
}

function renderDiff(action) {
  const before = action.before_text || "";
  const after = action.after_text || "";
  let start = 0;
  while (start < before.length && start < after.length && before[start] === after[start]) start += 1;
  let endBefore = before.length, endAfter = after.length;
  while (endBefore > start && endAfter > start && before[endBefore - 1] === after[endAfter - 1]) { endBefore -= 1; endAfter -= 1; }
  const mark = (text, from, to, cls) =>
    escapeHtml(text.slice(0, from)) + `<mark class="${cls}">${escapeHtml(text.slice(from, to))}</mark>` + escapeHtml(text.slice(to));
  const patchNote = action.method === "suggestion"
    ? `<div class="diff-note">建议补丁模式：原文件不会被修改，确认后请按补丁手工落地（${escapeHtml(action.patch_path || "补丁路径见动作详情")}）。</div>`
    : "";
  return `${patchNote}
    <div class="diff-block">
      <div class="diff-line del">- ${mark(before, start, endBefore, "mark-del")}</div>
      <div class="diff-line ins">+ ${mark(after, start, endAfter, "mark-ins")}</div>
    </div>`;
}

function toggleDiff(actionId, changeId) {
  const row = $(`#diff-${CSS.escape(actionId)}`);
  if (!row) return;
  row.classList.toggle("hidden");
  if (!row.classList.contains("hidden")) state.expandedDiff.add(actionId);
  else state.expandedDiff.delete(actionId);
}

async function handleAction(action, change) {
  try {
    if (action === "refresh") return loadWorkspace(state.workspaceId, change.change_id);
    const batchCount = Number(change.approval_scope?.change_count || 1);
    // Treat the rendered controls as the source of truth at submit time. This
    // also preserves the default-checked selection if a cached page or a
    // programmatic render did not emit a change event.
    const decisionInputs = $$('input[data-decision]');
    const decisions = action === "approve-selected"
      ? Object.fromEntries(decisionInputs.map((box) => [box.dataset.decision, box.checked ? "approve" : "reject"]))
      : { all: "approve" };
    if (action === "approve-selected") state.decisions = { ...decisions };
    const selectedCount = Object.values(decisions).filter((d) => d === "approve").length;
    if (action === "approve-selected" && selectedCount === 0) {
      toast("未勾选任何动作（未勾选 = 拒绝）。请勾选要批准的动作。", true);
      return;
    }
    if ((action === "approve" || action === "approve-selected") && batchCount > 1) {
      const note = action === "approve-selected"
        ? `将批准当前勾选的 ${selectedCount} 个动作，其余动作按拒绝处理，并作用于同批 ${batchCount} 个变更。`
        : `将批准同批 ${batchCount} 个变更的全部动作，是否继续？`;
      if (!window.confirm(note)) return;
    }
    if (action === "approve") await api(`/changes/${change.change_id}/approve`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ decisions: { all: "approve" } }) });
    if (action === "approve-selected") await api(`/changes/${change.change_id}/approve`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ decisions }) });
    if (action === "reject") await api(`/changes/${change.change_id}/reject`, { method: "POST" });
    if (action === "rollback") await api(`/changes/${change.change_id}/rollback`, { method: "POST" });
    toast(({ approve: "全部安全修改已执行并完成验证", "approve-selected": "所选动作已执行，其余按拒绝处理", reject: "修改计划已拒绝", rollback: "文件与事实指针已回滚" })[action]);
    await loadWorkspace(state.workspaceId, change.change_id);
  } catch (error) { toast(error.message, true); }
}

// ------------------------------------------------------------------ upload
function displayUploadResult(name, result, error) {
  const list = $("#upload-results");
  const item = error
    ? `<div class="upload-result error"><strong>${escapeHtml(name)}</strong><span>${escapeHtml(error)}</span></div>`
    : (() => {
        const summary = result.summary || {};
        const events = summary.change_events || [];
        const pending = result.run_state?.waiting_approval;
        const factsNote = events.length
          ? `检测到变更：${escapeHtml(events[0].entity_name || "")} ${escapeHtml(events[0].old_value)} → ${escapeHtml(events[0].new_value)}`
          : "未检测到事实变更（新事实已入库）";
        return `<div class="upload-result ok"><strong>${escapeHtml(name)}</strong><span>${factsNote}${pending ? " · 等待人工批准" : ""}</span><button class="link-button" data-open-change="${escapeHtml(events[0]?.change_event_id || "")}">查看</button></div>`;
      })();
  list.insertAdjacentHTML("beforeend", item);
  $$(".link-button[data-open-change]").forEach((button) => button.addEventListener("click", () => {
    if (button.dataset.openChange) {
      switchView("changes");
      loadDetail(button.dataset.openChange).catch((e) => toast(e.message, true));
    }
  }));
}

async function uploadFiles(files) {
  if (!state.workspaceId) { toast("请先载入或创建工作区", true); return; }
  for (const file of files) {
    const form = new FormData();
    form.append("file", file);
    try {
      const result = await api(`/workspaces/${state.workspaceId}/artifacts?sync=true`, { method: "POST", body: form });
      displayUploadResult(file.name, result);
    } catch (error) {
      displayUploadResult(file.name, null, error.message);
    }
  }
  state.changes = await api(`/workspaces/${state.workspaceId}/changes`);
  renderList();
}

async function createWorkspace() {
  const name = $("#new-ws-name").value.trim();
  if (!name) { toast("请填写工作区名称", true); return; }
  const body = { name, preset_entities: [] };
  const canonical = $("#new-ws-entity").value.trim();
  if (canonical) {
    const aliases = $("#new-ws-aliases").value.split(/[,，]/).map((a) => a.trim()).filter(Boolean);
    body.preset_entities.push({ canonical_name: canonical, aliases });
  }
  const workspace = await api("/workspaces", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  state.workspaceKey = workspace.workspace_key || "";
  $("#workspace-key").value = state.workspaceKey;
  toast(`工作区已创建：${workspace.workspace_id}`);
  await loadWorkspace(workspace.workspace_id);
}

// ------------------------------------------------------------- entities
async function loadEntityCount() {
  if (!state.workspaceId) return;
  const pending = await api(`/workspaces/${state.workspaceId}/entities/pending`);
  $("#entity-count").textContent = pending.length;
}

async function loadEntities() {
  if (!state.workspaceId) { $("#entity-list").innerHTML = '<div class="empty">请先载入工作区</div>'; return; }
  const pending = await api(`/workspaces/${state.workspaceId}/entities/pending`);
  $("#entity-count").textContent = pending.length;
  if (!pending.length) { $("#entity-list").innerHTML = '<div class="empty">没有待消歧的实体 ✓</div>'; return; }
  $("#entity-list").innerHTML = pending.map((item) => `
    <div class="entity-card" data-entity="${escapeHtml(item.entity_id)}">
      <div class="entity-head">
        <strong>“${escapeHtml(item.mention)}”</strong>
        <small>是否就是以下已知实体之一？裁定后将写入别名词典。</small>
      </div>
      <div class="entity-actions">
        <select data-role="target">
          <option value="">— 选择已知实体 —</option>
          ${(item.known_entities || []).map((k) => `<option value="${escapeHtml(k.entity_id)}">${escapeHtml(k.canonical_name)}</option>`).join("")}
        </select>
        <button class="secondary-button" data-resolve="merge_to">是同一个，合并并学习别名</button>
        <button class="secondary-button" data-resolve="keep_as_new">是新实体，单独建卡</button>
      </div>
    </div>`).join("");
  $$(".entity-card [data-resolve]").forEach((button) => button.addEventListener("click", () => resolveEntity(button.closest(".entity-card"), button.dataset.resolve)));
}

async function resolveEntity(card, mode) {
  const entityId = card.dataset.entity;
  const body = { mode };
  if (mode === "merge_to") {
    body.target_entity_id = card.querySelector('[data-role="target"]').value;
    if (!body.target_entity_id) { toast("请先选择要合并到的已知实体", true); return; }
  }
  try {
    await api(`/entities/${entityId}/resolve`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    toast(mode === "merge_to" ? "已合并并学习别名" : "已作为新实体保留");
    await loadEntities();
  } catch (error) { toast(error.message, true); }
}

// ------------------------------------------------------------- facts / runs
async function loadFacts() {
  if (!state.workspaceId) { $("#fact-list").innerHTML = '<div class="empty">请先载入工作区</div>'; return; }
  const facts = await api(`/workspaces/${state.workspaceId}/facts`);
  if (!facts.length) { $("#fact-list").innerHTML = '<div class="empty">暂无事实</div>'; return; }
  $("#fact-list").innerHTML = facts.map((fact) => `
    <div class="data-row ${fact.status === "unverified" ? "needs-review" : ""}">
      <div><strong>${escapeHtml(fact.entity || "未解析实体")} · ${escapeHtml(fact.predicate)}</strong><small>${escapeHtml(fact.artifact)} @ ${escapeHtml(fact.location)} · Authority ${escapeHtml(fact.source_authority)}</small></div>
      <code>${escapeHtml(fact.value)}</code>
      <div><span class="status ${fact.status === "verified" ? "executed" : "verification_failed"}">${escapeHtml(fact.status)}</span><small>${Math.round(Number(fact.confidence) * 100)}%</small></div>
      <p>“${escapeHtml(fact.evidence)}”${fact.review_reason ? `<br><em>${escapeHtml(fact.review_reason)}</em>` : ""}</p>
    </div>`).join("");
}

async function loadRuns() {
  if (!state.workspaceId) { $("#run-list").innerHTML = '<div class="empty">请先载入工作区</div>'; return; }
  const runs = await api(`/workspaces/${state.workspaceId}/runs`);
  if (!runs.length) { $("#run-list").innerHTML = '<div class="empty">暂无运行记录</div>'; return; }
  $("#run-list").innerHTML = runs.map((run) => `
    <div class="data-row run-row">
      <div><strong>${escapeHtml(run.thread_id)}</strong><small>artifact ${escapeHtml(run.artifact_id)} · ${escapeHtml(run.started_at || "")}</small></div>
      <span class="status ${run.status === "failed" ? "verification_failed" : run.status === "completed" ? "executed" : "pending_approval"}">${escapeHtml(run.status)}</span>
      <p>${escapeHtml((run.errors || []).map((item) => item.reason || JSON.stringify(item)).join("；") || "无错误")}</p>
      ${run.status === "failed" ? `<button class="secondary-button" data-retry-run="${escapeHtml(run.thread_id)}">重试</button>` : ""}
    </div>`).join("");
  $$('[data-retry-run]').forEach((button) => button.addEventListener("click", async () => {
    try {
      const result = await api(`/runs/${button.dataset.retryRun}/retry`, { method: "POST" });
      toast(`已创建重试 Run：${result.thread_id}`);
      await loadRuns();
    } catch (error) { toast(error.message, true); }
  }));
}

// ------------------------------------------------------------- Feishu
async function loadFeishu() {
  const status = await api("/integrations/feishu/status");
  $("#feishu-status").textContent = status.configured
    ? `Open API 已配置；事件校验 ${status.verification_configured ? "已启用" : "未配置"}；审批通知 ${status.app_notification_configured || status.webhook_configured ? "已配置" : "未配置"}；审批链接 ${status.approval_link_configured ? "已配置" : "未配置"}。`
    : "尚未配置 FEISHU_APP_ID / FEISHU_APP_SECRET；可先创建绑定，但同步会安全拒绝。";
  if (!state.workspaceId) { $("#feishu-bindings").innerHTML = '<div class="empty">请先载入工作区</div>'; return; }
  const bindings = await api(`/workspaces/${state.workspaceId}/integrations/feishu`);
  $("#feishu-bindings").innerHTML = bindings.length ? bindings.map((item) => `
    <div class="data-row"><div><strong>${escapeHtml(item.folder_token)}</strong><small>${item.last_synced_at ? `上次同步 ${escapeHtml(item.last_synced_at)}` : "尚未同步"}</small></div><span class="status ${item.enabled ? "executed" : "rejected"}">${item.enabled ? "enabled" : "disabled"}</span></div>`).join("") : '<div class="empty">暂无绑定</div>';
}

async function bindFeishu() {
  if (!state.workspaceId) { toast("请先载入工作区", true); return; }
  const folderToken = $("#feishu-folder-token").value.trim();
  if (!folderToken) { toast("请填写 folder_token", true); return; }
  await api("/integrations/feishu/bindings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ workspace_id: state.workspaceId, folder_token: folderToken }) });
  toast("飞书文件夹已绑定");
  await loadFeishu();
}

async function syncFeishu() {
  if (!state.workspaceId) { toast("请先载入工作区", true); return; }
  const folderToken = $("#feishu-folder-token").value.trim();
  if (!folderToken) { toast("请填写 folder_token", true); return; }
  const result = await api("/integrations/feishu/sync", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ workspace_id: state.workspaceId, folder_token: folderToken }) });
  toast(`同步完成：更新 ${result.synced.length}，未变化 ${result.unchanged.length}`);
  await loadFeishu();
  await loadWorkspace(state.workspaceId);
}

// ------------------------------------------------------------------ demo
async function seedDemo() {
  const button = $("#start-demo");
  button.disabled = true;
  button.textContent = "正在解析与分析…";
  try {
    const workspace = await api("/workspaces", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: "Alpha Workspace", preset_entities: [{ canonical_name: "Alpha V2.0", aliases: ["Alpha", "V2.0"] }] }) });
    state.workspaceKey = workspace.workspace_key || "";
    $("#workspace-key").value = state.workspaceKey;
    const files = ["PRD.docx", "release_plan.xlsx", "test_plan.docx", "launch_plan.md", "weekly_0829.md", "weekly_0905.md"];
    let changeId = "";
    for (const name of files) {
      const asset = await fetch(`/demo-assets/${name}`);
      if (!asset.ok) throw new Error(`无法载入演示文件 ${name}`);
      const form = new FormData();
      form.append("file", new File([await asset.blob()], name));
      const result = await api(`/workspaces/${workspace.workspace_id}/artifacts?sync=true`, { method: "POST", body: form });
      changeId = result.summary?.change_events?.[0]?.change_event_id || changeId;
    }
    await loadWorkspace(workspace.workspace_id, changeId);
    toast("Alpha 演示工作区已准备完成");
  } catch (error) {
    toast(error.message, true);
    button.disabled = false;
    button.innerHTML = '重新载入演示 <span>→</span>';
  }
}

async function loadAudit() {
  if (!state.workspaceId) { $("#audit-list").innerHTML = '<div class="empty">请先载入工作区</div>'; return; }
  const rows = await api(`/workspaces/${state.workspaceId}/audit`);
  $("#audit-list").innerHTML = rows.length ? rows.slice().reverse().map((row) => `<div class="audit-row"><time>${escapeHtml(new Date(row.executed_at).toLocaleString("zh-CN", { hour12: false }))}</time><strong>${escapeHtml(row.tool)}</strong><code>${escapeHtml(JSON.stringify(row.input))}</code><span class="status ${row.status === "success" ? "executed" : "verification_failed"}">${escapeHtml(row.actor)} · ${escapeHtml(row.status)}</span></div>`).join("") : '<div class="empty">暂无审计记录</div>';
}

function switchView(view) {
  $$(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.view === view));
  ["changes", "upload", "facts", "entities", "runs", "feishu", "audit"].forEach((v) => $(`#${v}-view`)?.classList.toggle("hidden", v !== view));
  $("#view-title").textContent = ({ changes: "Change Center", upload: "Upload & Ingest", facts: "Facts", entities: "实体消歧", runs: "Agent Runs", feishu: "飞书接入", audit: "Audit Log" })[view];
  if (view === "audit") loadAudit().catch((error) => toast(error.message, true));
  if (view === "entities") loadEntities().catch((error) => toast(error.message, true));
  if (view === "facts") loadFacts().catch((error) => toast(error.message, true));
  if (view === "runs") loadRuns().catch((error) => toast(error.message, true));
  if (view === "feishu") loadFeishu().catch((error) => toast(error.message, true));
}

$("#start-demo").addEventListener("click", seedDemo);
$("#load-workspace").addEventListener("click", () => loadWorkspace($("#workspace-id").value, state.selectedId).catch((error) => toast(error.message, true)));
$("#workspace-id").addEventListener("keydown", (event) => { if (event.key === "Enter") $("#load-workspace").click(); });
$("#refresh-audit").addEventListener("click", () => loadAudit().catch((error) => toast(error.message, true)));
$("#refresh-entities").addEventListener("click", () => loadEntities().catch((error) => toast(error.message, true)));
$("#refresh-facts").addEventListener("click", () => loadFacts().catch((error) => toast(error.message, true)));
$("#refresh-runs").addEventListener("click", () => loadRuns().catch((error) => toast(error.message, true)));
$("#refresh-feishu").addEventListener("click", () => loadFeishu().catch((error) => toast(error.message, true)));
$("#bind-feishu").addEventListener("click", () => bindFeishu().catch((error) => toast(error.message, true)));
$("#sync-feishu").addEventListener("click", () => syncFeishu().catch((error) => toast(error.message, true)));
$("#create-workspace").addEventListener("click", () => createWorkspace().catch((error) => toast(error.message, true)));
$("#upload-zone").addEventListener("click", () => $("#file-input").click());
$("#file-input").addEventListener("change", (event) => uploadFiles([...event.target.files]).catch((error) => toast(error.message, true)));
$("#upload-zone").addEventListener("dragover", (event) => { event.preventDefault(); $("#upload-zone").classList.add("drag"); });
$("#upload-zone").addEventListener("dragleave", () => $("#upload-zone").classList.remove("drag"));
$("#upload-zone").addEventListener("drop", (event) => {
  event.preventDefault();
  $("#upload-zone").classList.remove("drag");
  uploadFiles([...event.dataTransfer.files]).catch((error) => toast(error.message, true));
});
$$(".nav-item").forEach((item) => item.addEventListener("click", () => switchView(item.dataset.view)));

const deepLink = new URLSearchParams(window.location.search);
const linkedWorkspace = deepLink.get("workspace") || "";
const linkedChange = deepLink.get("change") || "";
if (linkedWorkspace) {
  $("#workspace-id").value = linkedWorkspace;
  state.selectedId = linkedChange;
  loadWorkspace(linkedWorkspace, linkedChange).catch((error) => toast(error.message, true));
}
