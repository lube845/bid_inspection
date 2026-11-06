/* 投标文件智能比对与风险分析系统 —— 轻量管理台（无构建步骤） */
const API = "/api/v1";

const state = {
  token: localStorage.getItem("bid_token") || "",
  user: JSON.parse(localStorage.getItem("bid_user") || "null"),
  lastProjectId: localStorage.getItem("bid_last_project") || "",
};

const PROJECT_STATUS = { pending: "待整理", ready: "待分析", analyzing: "分析中", analyzed: "分析完成", archived: "已归档" };
const COMPANY_STATUS = { incomplete: "待完善", pending_parse: "待解析", parsing: "解析中", pending_confirm: "待确认", analyzable: "可分析", abnormal: "资料异常" };
const FILE_STATUS = { uploading: "上传中", pending: "待解析", parsing: "解析中", success: "解析成功", partial: "部分成功", failed: "解析失败", superseded: "已失效" };
const TASK_STATUS = { queued: "排队中", running: "执行中", partial: "部分完成", completed: "已完成", failed: "执行失败", cancelled: "已取消" };
const RISK = { high: "高", medium: "中", low: "低", none: "无" };
const REVIEW = { pending: "待审核", confirmed: "确认异常线索", excluded: "正常/排除", undecided: "暂不判断" };
const TYPE = { text: "文本相似", personnel: "人员重复", price: "价格异常" };
const DIM = { text: "文本相似分析", personnel: "人员重复分析", price: "价格异常分析" };
const SUBTYPE = {
  text_similarity: "文本相似",
  exact_name: "完全同名",
  similar_name: "疑似相似姓名",
  high_concentration: "报价高度集中",
  close_deviation: "偏差过小",
  arithmetic_sequence: "等差分布",
  special_tail: "特殊尾数",
  tail_concentration: "尾数集中",
};
const subtypeLabel = (value) => SUBTYPE[value] || value;
const ROLE = { a: "企业A", b: "企业B" };

/* 系统级模块用到的标签 */
const USER_ROLE = { admin: "系统管理员", analyst: "项目分析人员", reviewer: "风险审核人员" };
const RULE_DIM = { text: "文本相似", personnel: "人员重复", price: "价格异常" };

let pollTimer = null;
let eventSource = null;
let projectListState = { q: "", status: "", include_archived: false };
let riskListState = {
  project_id: "", company_id: "", evidence_type: "", severity: "",
  review_status: "", reviewer_id: "", date_from: "", date_to: "", page: 1, page_size: 20,
};
let systemTab = "rules";
let logFilterState = { action: "", result: "", source: "", actor_id: "", date_from: "", date_to: "" };
let selectedCompanyByProject = {};

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const num = (value, fallback = 0) => (value === null || value === undefined ? fallback : value);
const icon = (name, cls = "") => `<svg class="icon ${cls}" aria-hidden="true" focusable="false"><use href="#i-${esc(name)}"></use></svg>`;

/* 加载占位：骨架屏比空白 spinner 更能保持布局稳定 */
function skeletonPage(cards = 4) {
  return `<div class="skeleton-stack" aria-busy="true" aria-label="加载中">
    <div class="skeleton skeleton-card"></div>
    ${Array.from({ length: cards }, () => `<div class="skeleton skeleton-card" style="height:56px"></div>`).join("")}
  </div>`;
}
function skeletonBlock(rows = 4) {
  return `<div class="skeleton-stack" aria-busy="true" aria-label="加载中">
    ${Array.from({ length: rows }, (_, index) => `<div class="skeleton skeleton-line ${index === 0 ? "short" : index % 2 ? "medium" : ""}"></div>`).join("")}
  </div>`;
}
/* 异步按钮：禁用 + aria-busy + 旋转指示，结束时恢复原文案 */
async function withBusy(button, task, label = "处理中…") {
  if (!button) return task();
  const original = button.innerHTML;
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  button.innerHTML = `<span class="spinner" aria-hidden="true"></span><span>${esc(label)}</span>`;
  try {
    return await task();
  } finally {
    button.disabled = false;
    button.removeAttribute("aria-busy");
    button.innerHTML = original;
  }
}
/* 统计卡片：图标 + 标签 + 数值，保持数据看板的一致节奏 */
function statCard(label, value, iconName = "activity", tone = "", sub = "") {
  return `<div class="stat">
    <div class="stat-head"><span class="stat-icon ${tone}">${icon(iconName)}</span><span class="label">${esc(label)}</span></div>
    <div class="value">${value}</div>
    ${sub ? `<div class="stat-sub">${sub}</div>` : ""}
  </div>`;
}

function fmtDate(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleString("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
}
function fmtSize(bytes) {
  if (!bytes) return "0 B";
  if (bytes >= 1024 * 1024) return (bytes / 1024 / 1024).toFixed(1) + " MB";
  if (bytes >= 1024) return (bytes / 1024).toFixed(1) + " KB";
  return bytes + " B";
}
function pill(value, label) {
  const cls = value || "none";
  return `<span class="pill ${esc(cls)}">${esc(label || value || "—")}</span>`;
}
function riskPill(value) {
  return `<span class="pill ${esc(value || "none")}">${RISK[value] || "无"}</span>`;
}
function filePill(value) {
  return `<span class="pill ${esc(value)}">${FILE_STATUS[value] || value}</span>`;
}
function companyPill(value) {
  return `<span class="pill ${esc(value)}">${COMPANY_STATUS[value] || value}</span>`;
}
function taskPill(value) {
  return `<span class="pill ${esc(value)}">${TASK_STATUS[value] || value}</span>`;
}
/* 可实时更新的进度条：节点带 data-dim-fill / data-dim-count，供 SSE 快照就地刷新 */
function liveProgressBar(done, total, dimension) {
  const percent = total ? Math.min(100, Math.round((done / total) * 100)) : 0;
  const label = DIM[dimension] || dimension;
  return `<div class="progress-track" role="progressbar" aria-valuemin="0" aria-valuemax="${total}" aria-valuenow="${done}" aria-label="${esc(label)}进度"><div class="progress-fill" style="width:${percent}%" data-dim-fill="${esc(dimension)}"></div></div>
    <div class="small muted" style="margin-top:4px" data-dim-count="${esc(dimension)}">${done} / ${total}</div>`;
}
function dimStatusLabel(entry) {
  const status = entry.status === "completed" ? "完成" : entry.status === "failed" ? "失败" : entry.status === "running" ? "执行中" : "等待中";
  return `${status}${entry.failed ? ` · ${entry.failed} 组失败` : ""}`;
}

function toast(message, type = "") {
  const root = $("#toast-root");
  if (!root) return;
  const item = document.createElement("div");
  item.className = `toast ${type}`;
  const iconName = type === "error" ? "alert" : type === "success" ? "check" : "info";
  item.innerHTML = `${icon(iconName)}<span>${esc(message)}</span><button type="button" class="toast-close" aria-label="关闭提示">${icon("close")}</button>`;
  const remove = () => item.remove();
  item.querySelector(".toast-close").addEventListener("click", remove);
  root.appendChild(item);
  setTimeout(remove, type === "error" ? 6500 : 4200);
}

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  let body = options.body;
  if (body && !(body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(body);
  }
  const response = await fetch(API + path, { method: options.method || "GET", headers, body });
  if (response.status === 401) {
    logout();
    throw new Error("登录已过期，请重新登录");
  }
  if (!response.ok) {
    let message = `请求失败 (${response.status})`;
    try {
      const data = await response.json();
      if (typeof data.detail === "string") message = data.detail;
      else if (data.detail) message = JSON.stringify(data.detail);
      if (Array.isArray(data.errors) && data.errors.length) {
        message += "：" + data.errors.map((e) => e.message).join("；");
      }
    } catch (_) { /* ignore */ }
    throw new Error(message);
  }
  if (options.blob) return response.blob();
  if (response.status === 204) return null;
  return response.json().catch(() => null);
}

/* ---------- 实时事件流（SSE） ---------- */
function closeEventStream() {
  if (eventSource) {
    eventSource.close();
    eventSource = null;
  }
}

/* 打开项目事件流：后端在解析进度、任务进度、证据数量变化时推送快照 */
function openEventStream(projectId, onSnapshot, onError) {
  closeEventStream();
  if (!state.token) return null;
  const url = `${API}/projects/${projectId}/events?token=${encodeURIComponent(state.token)}`;
  const source = new EventSource(url);
  source.addEventListener("snapshot", (event) => {
    try { onSnapshot(JSON.parse(event.data)); } catch (_) { /* 忽略非法负载 */ }
  });
  source.addEventListener("error", () => { if (onError) onError(); });
  eventSource = source;
  return source;
}

/* ---------- routing / 移动端抽屉 ---------- */
let drawerPreviousFocus = null;
function setDrawer(open) {
  const sidebar = $("#sidebar");
  const scrim = $("#drawer-scrim");
  const button = $("#menu-btn");
  if (!sidebar || !scrim || !button) return;
  const wasOpen = sidebar.classList.contains("open");
  sidebar.classList.toggle("open", open);
  scrim.classList.toggle("hidden", !open);
  button.setAttribute("aria-expanded", String(open));
  button.setAttribute("aria-label", open ? "关闭导航菜单" : "打开导航菜单");
  document.body.classList.toggle("drawer-open", open);
  if (open && !wasOpen) {
    drawerPreviousFocus = document.activeElement;
    requestAnimationFrame(() => {
      const first = $("#sidebar .side-nav a");
      if (first) first.focus({ preventScroll: true });
    });
  } else if (!open && wasOpen) {
    const target = drawerPreviousFocus || button;
    drawerPreviousFocus = null;
    requestAnimationFrame(() => {
      if (target && typeof target.focus === "function") target.focus({ preventScroll: true });
    });
  }
}
function showLogin() {
  setDrawer(false);
  $("#login-view").classList.remove("hidden");
  $("#app-shell").classList.add("hidden");
}
function showApp() {
  $("#login-view").classList.add("hidden");
  $("#app-shell").classList.remove("hidden");
  const name = state.user ? state.user.display_name || state.user.username : "";
  const role = state.user ? USER_ROLE[state.user.role] || state.user.role : "";
  const subline = name === role ? state.user?.username || "" : role;
  const box = $("#user-box");
  if (box) box.innerHTML = state.user ? `${esc(name)}<div class="small muted">${esc(subline)}</div>` : "";
  const mobile = $("#mobile-user");
  if (mobile) mobile.textContent = name;
}
function logout() {
  /* 先记录退出登录的审计日志，再清除令牌 */
  if (state.token) api("/auth/logout", { method: "POST" }).catch(() => {});
  closeEventStream();
  const modalRoot = $("#modal-root");
  if (modalRoot) modalRoot.innerHTML = "";
  document.body.classList.remove("modal-open");
  state.token = "";
  state.user = null;
  localStorage.removeItem("bid_token");
  localStorage.removeItem("bid_user");
  showLogin();
}
function navigate(hash) {
  if (location.hash === hash) route();
  else location.hash = hash;
}
function setNav(name) {
  $$(".side-nav a[data-nav]").forEach((link) => {
    const active = link.dataset.nav === name;
    link.classList.toggle("active", active);
    if (active) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });
}

async function route() {
  clearTimeout(pollTimer);
  pollTimer = null;
  closeEventStream();
  setDrawer(false);
  if (!state.token) { showLogin(); return; }
  showApp();
  const raw = (location.hash || "#/workbench").slice(1);
  const [path, queryString] = raw.split("?");
  const params = new URLSearchParams(queryString || "");
  const main = $("#main");
  try {
    let match;
    if (path === "/workbench" || path === "") {
      setNav("workbench");
      await renderWorkbench();
    } else if (path === "/risks") {
      setNav("risks");
      await renderRisks(params);
    } else if (path === "/system") {
      setNav("system");
      await renderSystem(params);
    } else if (path === "/projects") {
      setNav("projects");
      await renderProjects();
    } else if ((match = path.match(/^\/projects\/([^/]+)\/pairs\/([^/]+)$/))) {
      setNav("projects");
      await renderPair(match[1], match[2], params.get("evidence"));
    } else if ((match = path.match(/^\/projects\/([^/]+)$/))) {
      const tab = params.get("tab") || "overview";
      setNav("projects");
      /* 人工审核已并入比对分析，旧的 ?tab=review 链接自动转到比对分析 */
      let target = tab === "review" ? "analysis" : tab;
      /* 分析任务与启动已独立成「分析任务」标签页，旧的 ?tab=analysis&task= 链接自动转到分析任务 */
      if (target === "analysis" && params.get("task")) target = "task";
      await renderProject(match[1], target, { pairId: params.get("pair"), evidenceId: params.get("evidence") });
    } else {
      main.innerHTML = `<div class="empty">页面不存在。<a href="#/workbench">返回工作台</a></div>`;
    }
  } catch (error) {
    console.error(error);
    main.innerHTML = `<div class="error-box">${esc(error.message)}</div>`;
  }
  /* 路由切换后把焦点移到主内容，便于屏幕阅读器感知页面已更新 */
  requestAnimationFrame(() => main.focus({ preventScroll: true }));
}

/* ---------- modal ---------- */
function openModal({ title, body, footer = "", wide = false }) {
  const root = $("#modal-root");
  const previousFocus = document.activeElement;
  const titleId = `modal-title-${Date.now()}`;
  root.innerHTML = `
    <div class="modal-mask">
      <div class="modal ${wide ? "wide" : ""}" role="dialog" aria-modal="true" aria-labelledby="${titleId}" tabindex="-1">
        <div class="modal-head"><h2 id="${titleId}">${esc(title)}</h2>
          <button type="button" class="icon-btn" data-close aria-label="关闭对话框">${icon("close")}</button>
        </div>
        <div class="modal-body">${body}</div>
        ${footer ? `<div class="modal-foot">${footer}</div>` : ""}
      </div>
    </div>`;
  const mask = $(".modal-mask", root);
  const dialog = $(".modal", root);
  document.body.classList.add("modal-open");

  const focusableSelector = 'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';
  const focusables = () => $$(focusableSelector, dialog).filter((el) => el.offsetWidth || el.offsetHeight || el.getClientRects().length);

  let closed = false;
  const close = () => {
    if (closed) return;
    closed = true;
    document.removeEventListener("keydown", onKey, true);
    root.innerHTML = "";
    document.body.classList.remove("modal-open");
    if (previousFocus && typeof previousFocus.focus === "function") previousFocus.focus({ preventScroll: true });
  };
  const onKey = (event) => {
    if (event.key === "Escape") { event.preventDefault(); close(); return; }
    if (event.key !== "Tab") return;
    const items = focusables();
    if (!items.length) { event.preventDefault(); dialog.focus(); return; }
    const first = items[0];
    const last = items[items.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  };

  document.addEventListener("keydown", onKey, true);
  $$("[data-close]", root).forEach((btn) => btn.addEventListener("click", close));
  mask.addEventListener("click", (event) => { if (event.target === mask) close(); });

  requestAnimationFrame(() => {
    const initial = $("[autofocus]", dialog)
      || $(".modal-body input, .modal-body select, .modal-body textarea", dialog)
      || $(".modal-body button, .modal-foot .btn.primary", dialog)
      || dialog;
    initial.focus({ preventScroll: true });
  });
  return { root, close };
}

/* ---------- project list ---------- */
async function renderProjects() {
  const main = $("#main");
  const params = new URLSearchParams();
  if (projectListState.q) params.set("q", projectListState.q);
  if (projectListState.status) params.set("status", projectListState.status);
  if (projectListState.include_archived) params.set("include_archived", "true");
  params.set("page_size", "100");
  const data = await api(`/projects?${params}`);
  const rows = data.items.map((project) => `
    <tr>
      <td><a href="#/projects/${project.id}"><strong>${esc(project.project_name)}</strong></a>
        <div class="small muted">${esc(project.project_code || "无项目编号")}</div></td>
      <td>${num(project.company_count)}</td>
      <td>${num(project.file_count)}</td>
      <td>${pill(project.status, PROJECT_STATUS[project.status] || project.status)}</td>
      <td>${fmtDate(project.last_analysis_at)}</td>
      <td>
        <a class="btn small" href="#/projects/${project.id}">进入</a>
        ${project.status !== "archived" ? `<button class="btn small" data-archive="${project.id}">归档</button>` : ""}
        <button class="btn small danger" data-delete="${project.id}" data-name="${esc(project.project_name)}">删除</button>
      </td>
    </tr>`).join("");

  main.innerHTML = `
    <div class="page-head">
      <div><h1>项目管理</h1><div class="sub">共 ${data.total} 个项目 · 单个项目最多 20 家投标企业</div></div>
      <button class="btn primary" id="create-project">${icon("plus")}创建项目</button>
    </div>
    <div class="card">
      <div class="row" style="margin-bottom:14px">
        <input type="search" id="filter-q" aria-label="搜索项目名称或编号" placeholder="搜索项目名称 / 编号" value="${esc(projectListState.q)}" />
        <select id="filter-status" aria-label="按项目状态筛选">
          <option value="">全部状态</option>
          ${Object.entries(PROJECT_STATUS).map(([value, label]) => `<option value="${value}" ${projectListState.status === value ? "selected" : ""}>${label}</option>`).join("")}
        </select>
        <label class="small muted"><input type="checkbox" id="filter-archived" ${projectListState.include_archived ? "checked" : ""} /> 显示已归档</label>
      </div>
      ${rows ? `<table><thead><tr><th>项目名称</th><th>企业数</th><th>文件数</th><th>项目状态</th><th>最近分析</th><th>操作</th></tr></thead><tbody>${rows}</tbody></table>`
        : `<div class="empty">暂无项目，创建第一个项目开始整理标书资料。<button class="btn primary" id="empty-create-project">${icon("plus")}创建项目</button></div>`}
    </div>`;

  $("#filter-q").addEventListener("input", (event) => { projectListState.q = event.target.value; });
  $("#filter-q").addEventListener("keydown", (event) => { if (event.key === "Enter") renderProjects(); });
  $("#filter-status").addEventListener("change", (event) => { projectListState.status = event.target.value; renderProjects(); });
  $("#filter-archived").addEventListener("change", (event) => { projectListState.include_archived = event.target.checked; renderProjects(); });
  $("#create-project").addEventListener("click", openCreateProject);
  $("#empty-create-project")?.addEventListener("click", openCreateProject);

  $$("[data-archive]").forEach((btn) => btn.addEventListener("click", async () => {
    if (!confirm("确认归档该项目？归档后默认不在项目列表中显示。")) return;
    await withBusy(btn, async () => {
      try { await api(`/projects/${btn.dataset.archive}/archive`, { method: "POST" }); toast("项目已归档", "success"); renderProjects(); }
      catch (error) { toast(error.message, "error"); }
    }, "归档中…");
  }));
  $$("[data-delete]").forEach((btn) => btn.addEventListener("click", async () => {
    if (!confirm(`确认删除项目“${btn.dataset.name}”？历史分析记录将保留在数据库中。`)) return;
    await withBusy(btn, async () => {
      try { await api(`/projects/${btn.dataset.delete}`, { method: "DELETE" }); toast("项目已删除", "success"); renderProjects(); }
      catch (error) { toast(error.message, "error"); }
    }, "删除中…");
  }));
}

function openCreateProject() {
  const modal = openModal({
    title: "创建项目",
    body: `<form id="project-form">
      <div class="form-row"><label for="project-name">项目名称 *</label><input id="project-name" name="project_name" required maxlength="255" /></div>
      <div class="form-row"><label for="project-code">项目编号</label><input id="project-code" name="project_code" maxlength="128" /></div>
      <div class="form-row"><label for="project-desc">项目说明</label><textarea id="project-desc" name="description"></textarea></div>
    </form>`,
    footer: `<button class="btn" data-close>取消</button><button class="btn primary" id="project-submit">创建并进入</button>`,
  });
  const submit = $("#project-submit", modal.root);
  submit.addEventListener("click", () => withBusy(submit, async () => {
    const form = $("#project-form", modal.root);
    if (!form.reportValidity()) return;
    const data = Object.fromEntries(new FormData(form).entries());
    if (!data.project_code) delete data.project_code;
    if (!data.description) delete data.description;
    try {
      const project = await api("/projects", { method: "POST", body: data });
      modal.close();
      navigate(`#/projects/${project.id}`);
    } catch (error) { toast(error.message, "error"); }
  }, "创建中…"));
}

/* ---------- project shell ---------- */
async function renderProject(projectId, tab, options = {}) {
  state.lastProjectId = projectId;
  localStorage.setItem("bid_last_project", projectId);
  const project = await api(`/projects/${projectId}`);
  const tabs = [
    ["overview", "项目概览"], ["files", "文件管理"], ["parse", "解析中心"],
    ["task", "分析任务"], ["analysis", "比对分析"], ["results", "比对结果"], ["report", "分析报告"],
  ];
  $("#main").innerHTML = `
    <div class="page-head">
      <div>
        <div class="small muted"><a href="#/projects">项目列表</a> / ${esc(project.project_name)}</div>
        <h1>${esc(project.project_name)}</h1>
        <div class="sub">${esc(project.project_code || "无项目编号")} · 创建于 ${fmtDate(project.created_at)} · ${pill(project.status, PROJECT_STATUS[project.status] || project.status)}</div>
      </div>
      <div class="row">
        <button class="btn" id="edit-project">${icon("edit")}编辑项目</button>
        ${project.status !== "archived" ? `<button class="btn" id="archive-project">归档</button>` : `<button class="btn" id="unarchive-project">取消归档</button>`}
      </div>
    </div>
    <div class="tabs" role="tablist" aria-label="项目页面">
      ${tabs.map(([key, label]) => `<a role="tab" href="#/projects/${projectId}?tab=${key}" class="${key === tab ? "active" : ""}" ${key === tab ? 'aria-current="page" aria-selected="true"' : 'aria-selected="false"'}>${label}</a>`).join("")}
    </div>
    <div id="tab-content">${skeletonPage(3)}</div>`;

  $("#edit-project").addEventListener("click", () => openEditProject(project));
  const archiveBtn = $("#archive-project");
  if (archiveBtn) archiveBtn.addEventListener("click", async () => {
    if (!confirm("确认归档该项目？")) return;
    try { await api(`/projects/${projectId}/archive`, { method: "POST" }); toast("已归档", "success"); renderProject(projectId, tab); }
    catch (error) { toast(error.message, "error"); }
  });
  const unarchiveBtn = $("#unarchive-project");
  if (unarchiveBtn) unarchiveBtn.addEventListener("click", async () => {
    try { await api(`/projects/${projectId}/unarchive`, { method: "POST" }); toast("已取消归档", "success"); renderProject(projectId, tab); }
    catch (error) { toast(error.message, "error"); }
  });

  const content = $("#tab-content");
  if (tab === "overview") await renderOverview(content, project);
  else if (tab === "files") await renderFiles(content, project);
  else if (tab === "parse") await renderParseCenter(content, project);
  else if (tab === "task") await renderTaskPage(content, project);
  else if (tab === "analysis") await renderCompare(content, project, { pairId: options.pairId, evidenceId: options.evidenceId });
  else if (tab === "results") await renderResults(content, project);
  else if (tab === "report") await renderReport(content, project);
  else content.innerHTML = `<div class="empty">未知页面</div>`;
}

function openEditProject(project) {
  const modal = openModal({
    title: "编辑项目",
    body: `<form id="edit-project-form">
      <div class="form-row"><label for="edit-name">项目名称 *</label><input id="edit-name" name="project_name" required maxlength="255" value="${esc(project.project_name)}" /></div>
      <div class="form-row"><label for="edit-code">项目编号</label><input id="edit-code" name="project_code" maxlength="128" value="${esc(project.project_code || "")}" /></div>
      <div class="form-row"><label for="edit-desc">项目说明</label><textarea id="edit-desc" name="description">${esc(project.description || "")}</textarea></div>
    </form>`,
    footer: `<button class="btn" data-close>取消</button><button class="btn primary" id="edit-submit">保存</button>`,
  });
  const submit = $("#edit-submit", modal.root);
  submit.addEventListener("click", () => withBusy(submit, async () => {
    const form = $("#edit-project-form", modal.root);
    if (!form.reportValidity()) return;
    const data = Object.fromEntries(new FormData(form).entries());
    data.project_code = data.project_code || null;
    data.description = data.description || null;
    try {
      await api(`/projects/${project.id}`, { method: "PATCH", body: data });
      modal.close(); toast("项目已更新", "success"); renderProject(project.id, "overview");
    } catch (error) { toast(error.message, "error"); }
  }, "保存中…"));
}

/* ---------- overview ---------- */
async function renderOverview(container, project) {
  const stats = project.stats || {};
  const stages = stats.stages || {};
  const todo = stats.todo || {};
  const todoTarget = {
    add_company: `#/projects/${project.id}?tab=files`,
    upload_files: `#/projects/${project.id}?tab=files`,
    parse_files: `#/projects/${project.id}?tab=parse`,
    check_parse: `#/projects/${project.id}?tab=parse`,
    view_progress: `#/projects/${project.id}?tab=task`,
    view_results: `#/projects/${project.id}?tab=results`,
    confirm_companies: `#/projects/${project.id}?tab=files`,
    auto_compare: `#/projects/${project.id}?tab=task`,
  };
  const recent = (project.recent_tasks || []).map((task) => `
    <tr>
      <td>#${task.task_no}</td><td>${taskPill(task.status)}</td><td>${task.company_count} 家</td>
      <td>${fmtDate(task.created_at)}</td><td>${fmtDate(task.finished_at)}</td>
      <td><a class="btn small" href="#/projects/${project.id}?tab=task">查看任务</a></td>
    </tr>`).join("");

  container.innerHTML = `
    <div class="grid cols-5">
      ${statCard("投标企业", `${num(stats.company_count)} <small>/ ${num(stats.company_limit, 20)}</small>`, "users")}
      ${statCard("原始文件", num(stats.file_count), "file")}
      ${statCard("已解析文件", `${num(stats.parsed_file_count)} <small>/ ${num(stats.file_count)}</small>`, "scan")}
      ${statCard("可参与分析企业", num(stats.analyzable_company_count), "shield", "ok")}
      ${statCard("待处理异常", num(stats.pending_issue_count), "alert", num(stats.pending_issue_count) ? "high" : "")}
    </div>
    <div class="card" style="margin-top:16px">
      <div class="row">
        <div>
          <h2 style="margin:0 0 4px">下一步：${esc(todo.label || "继续完善项目资料")}</h2>
          <div class="muted small">系统根据当前资料准备情况动态推荐下一步操作。</div>
        </div>
        <div class="spacer"></div>
        <a class="btn primary" href="${todoTarget[todo.code] || `#/projects/${project.id}`}">${esc(todo.label || "进入项目")}</a>
      </div>
    </div>
    <div class="grid cols-2">
      <div class="card">
        <h2>项目准备情况</h2>
        <dl class="kv">
          <dt>招标文件</dt><dd>${stages.tender?.uploaded ? `已上传 ${stages.tender.file_count} 份` : "未上传（跳过公共内容排除）"}</dd>
          <dt>企业资料</dt><dd>${num(stages.companies?.analyzable)} 家可分析，${num(stages.companies?.abnormal)} 家异常，${num(stages.companies?.pending_confirm)} 家待确认</dd>
          <dt>文件解析</dt><dd>${num(stages.files?.parsed)} 份已解析，${num(stages.files?.partial)} 份部分成功，${num(stages.files?.failed)} 份失败</dd>
          <dt>比对分析</dt><dd>${stages.analysis?.started ? `${num(stages.analysis.task_count)} 次任务，最近状态：${TASK_STATUS[stages.analysis.last_status] || stages.analysis.last_status || "—"}` : "尚未启动"}</dd>
          <dt>总页数</dt><dd>${num(stages.files?.pages)} 页</dd>
        </dl>
      </div>
      <div class="card">
        <h2>最近分析任务</h2>
        ${recent ? `<table><thead><tr><th>任务</th><th>状态</th><th>企业</th><th>创建时间</th><th>完成时间</th><th></th></tr></thead><tbody>${recent}</tbody></table>` : `<div class="empty">还没有分析任务。</div>`}
      </div>
    </div>`;
}

/* ---------- files ---------- */
async function renderFiles(container, project) {
  const [companyData, tenderData] = await Promise.all([
    api(`/projects/${project.id}/companies`),
    api(`/projects/${project.id}/files?kind=tender`),
  ]);
  const selectedId = selectedCompanyByProject[project.id];
  let detail = null;
  if (selectedId) {
    try { detail = await api(`/companies/${selectedId}`); }
    catch (_) { selectedCompanyByProject[project.id] = ""; }
  }

  const companyRows = companyData.items.map((company) => `
    <div class="company-row ${detail && detail.id === company.id ? "selected" : ""}">
      <div class="name">${esc(company.company_name)}</div>
      <div class="small muted">${company.file_count} 份文件 · ${company.total_pages} 页</div>
      <div>${companyPill(company.status)}</div>
      <div class="small muted">逻辑标书：${company.logical_document_status === "generated" ? "已生成" : company.logical_document_status === "outdated" ? "待更新" : "未生成"}</div>
      <div class="spacer"></div>
      <div class="small muted">${company.participate_analysis ? "参与分析" : "不参与"}</div>
      <button class="btn small" data-company="${company.id}">${detail && detail.id === company.id ? "收起" : "管理文件"}</button>
    </div>`).join("");

  const tenderRows = tenderData.items.map((file) => `
    <div class="file-row">
      <div class="name">${esc(file.file_name)}</div>
      <div class="small muted">${file.file_type.toUpperCase()} · ${fmtSize(file.file_size)} · ${file.page_count} 页</div>
      <div>${filePill(file.parse_status)}</div>
      <a class="btn small" href="${API}/files/${file.id}/content?token=${encodeURIComponent(state.token)}" target="_blank">查看</a>
      <button class="btn small" data-parse-file="${file.id}">${file.parse_status === "success" ? "重新解析" : "解析"}</button>
    </div>`).join("");

  container.innerHTML = `
    <div class="card">
      <div class="row" style="justify-content:space-between">
        <h2 style="margin:0">招标文件</h2>
        <div class="small muted">可选 · 用于识别公共内容，避免模板条款产生误报</div>
      </div>
      <div class="upload-zone" style="margin:12px 0">
        <input type="file" id="tender-input" aria-label="选择招标文件" multiple accept=".pdf,.docx,.doc,.txt,.md,.wps" />
        <button class="btn" id="tender-upload">${icon("upload")}上传招标文件</button>
        <span class="small muted">支持 PDF / DOCX / TXT，单个文件不超过 500MB</span>
      </div>
      ${tenderRows || `<div class="empty" style="padding:18px">尚未上传招标文件。未上传时分析不执行公共内容排除。</div>`}
    </div>
    <div class="card">
      <div class="row" style="justify-content:space-between">
        <h2 style="margin:0">投标企业 <span class="muted" style="font-weight:400">${companyData.total} / ${companyData.limit}</span></h2>
        <button class="btn primary" id="add-company" ${companyData.total >= companyData.limit ? "disabled" : ""}>${icon("plus")}添加企业</button>
      </div>
      <div style="margin-top:12px">${companyRows || `<div class="empty">尚未创建投标企业。</div>`}</div>
      ${companyData.total >= companyData.limit ? `<div class="warn-box" style="margin-top:8px">当前项目最多支持 20 家投标企业。</div>` : ""}
    </div>
    <div id="company-detail">${detail ? companyDetailHtml(detail, project) : ""}</div>`;

  const tenderBtn = $("#tender-upload");
  tenderBtn.addEventListener("click", () => withBusy(tenderBtn, async () => {
    const input = $("#tender-input");
    if (!input.files.length) return toast("请选择文件", "error");
    const form = new FormData();
    Array.from(input.files).forEach((file) => form.append("files", file));
    try {
      const result = await api(`/projects/${project.id}/tender-files`, { method: "POST", body: form });
      toast(result.message, "success"); renderFiles(container, project);
    } catch (error) { toast(error.message, "error"); }
  }, "上传中…"));
  $("#add-company").addEventListener("click", () => openAddCompany(project, container));
  $$("[data-company]").forEach((btn) => btn.addEventListener("click", () => {
    selectedCompanyByProject[project.id] = selectedCompanyByProject[project.id] === btn.dataset.company ? "" : btn.dataset.company;
    renderFiles(container, project);
  }));
  $$("[data-parse-file]").forEach((btn) => btn.addEventListener("click", () => parseFile(btn.dataset.parseFile, () => renderFiles(container, project))));
  if (detail) bindCompanyDetail(container, detail, project);
}

function companyDetailHtml(company, project) {
  const files = company.files || [];
  const rows = files.map((file, index) => `
    <div class="file-row" draggable="true" data-drag-file="${file.id}">
      <div class="order-handle" title="拖动调整顺序">${icon("grip")}${String(index + 1).padStart(2, "0")}</div>
      <div class="name">${esc(file.file_name)}
        <div class="small muted">${file.file_type.toUpperCase()} · ${fmtSize(file.file_size)} · ${file.page_count} 页 · v${file.version}</div>
        ${file.parse_message ? `<div class="small muted">${esc(file.parse_message)}</div>` : ""}
      </div>
      <div>${filePill(file.parse_status)}</div>
      <button class="btn small" data-move-up="${file.id}" aria-label="上移文件：${esc(file.file_name)}" ${index === 0 ? "disabled" : ""}>${icon("arrow-up")}</button>
      <button class="btn small" data-move-down="${file.id}" aria-label="下移文件：${esc(file.file_name)}" ${index === files.length - 1 ? "disabled" : ""}>${icon("arrow-down")}</button>
      <a class="btn small" href="${API}/files/${file.id}/content?token=${encodeURIComponent(state.token)}" target="_blank" rel="noreferrer">${icon("external")}查看</a>
      <button class="btn small" data-parse="${file.id}">${file.parse_status === "success" ? "重新解析" : "解析"}</button>
      <button class="btn small danger" data-delete-file="${file.id}" data-file-name="${esc(file.file_name)}" aria-label="删除文件：${esc(file.file_name)}">${icon("trash")}删除</button>
    </div>`).join("");

  return `
    <div class="card">
      <div class="row" style="justify-content:space-between">
        <div>
          <h2 style="margin:0">${esc(company.company_name)}</h2>
          <div class="small muted">${company.file_count} 份文件 · ${company.total_pages} 页 · 逻辑标书 ${company.logical_document_status === "generated" ? `v${company.logical_document?.version_no || 1} 已生成` : "待生成"}</div>
        </div>
        <div class="row">
          ${!company.confirmed && company.file_count > 0 ? `<button class="btn primary small" id="confirm-company">${icon("check")}确认企业资料</button>` : ""}
          <button class="btn small" id="preview-logical">${icon("layers")}预览逻辑标书</button>
        </div>
      </div>
      <div class="upload-zone" style="margin:14px 0">
        <input type="file" id="company-file-input" aria-label="选择企业文件" multiple accept=".pdf,.docx,.doc,.txt,.md,.wps" />
        <button class="btn" id="company-file-upload">${icon("upload")}上传企业文件</button>
        <span class="small muted">一家企业可上传多份文件；拖动顺序即逻辑标书顺序</span>
      </div>
      ${rows || `<div class="empty" style="padding:20px">该企业还没有文件。</div>`}
    </div>`;
}

function bindCompanyDetail(container, company, project) {
  const refresh = () => renderFiles(container, project);
  const input = $("#company-file-input", container);
  const uploadBtn = $("#company-file-upload", container);
  uploadBtn?.addEventListener("click", () => withBusy(uploadBtn, async () => {
    if (!input.files.length) return toast("请选择文件", "error");
    const form = new FormData();
    Array.from(input.files).forEach((file) => form.append("files", file));
    try {
      const result = await api(`/companies/${company.id}/files`, { method: "POST", body: form });
      toast(result.message, "success"); refresh();
    } catch (error) { toast(error.message, "error"); }
  }, "上传中…"));
  const confirmBtn = $("#confirm-company", container);
  confirmBtn?.addEventListener("click", () => withBusy(confirmBtn, async () => {
    try { await api(`/companies/${company.id}/confirm`, { method: "POST" }); toast("企业资料已确认", "success"); refresh(); }
    catch (error) { toast(error.message, "error"); }
  }, "确认中…"));
  $("#preview-logical", container)?.addEventListener("click", () => previewLogical(company));
  $$("[data-parse]", container).forEach((btn) => btn.addEventListener("click", () => parseFile(btn.dataset.parse, refresh)));
  $$("[data-delete-file]", container).forEach((btn) => btn.addEventListener("click", async () => {
    if (!confirm(`确认删除文件“${btn.dataset.fileName}”？原始文件将标记为失效，不会覆盖历史记录。`)) return;
    try { await api(`/files/${btn.dataset.deleteFile}`, { method: "DELETE" }); toast("文件已删除", "success"); refresh(); }
    catch (error) { toast(error.message, "error"); }
  }));
  const ids = (company.files || []).map((file) => file.id);
  const saveOrder = async (nextIds) => {
    try {
      await api(`/companies/${company.id}/file-order`, { method: "PUT", body: { file_ids: nextIds } });
      toast("文件顺序已保存，逻辑标书已生成新版本", "success");
      refresh();
    } catch (error) { toast(error.message, "error"); }
  };
  const reorder = (fileId, direction) => {
    const index = ids.indexOf(fileId);
    const next = index + direction;
    if (next < 0 || next >= ids.length) return;
    [ids[index], ids[next]] = [ids[next], ids[index]];
    saveOrder([...ids]);
  };
  $$("[data-move-up]", container).forEach((btn) => btn.addEventListener("click", () => reorder(btn.dataset.moveUp, -1)));
  $$("[data-move-down]", container).forEach((btn) => btn.addEventListener("click", () => reorder(btn.dataset.moveDown, 1)));

  let draggedId = null;
  $$("[data-drag-file]", container).forEach((row) => {
    row.addEventListener("dragstart", (event) => {
      draggedId = row.dataset.dragFile;
      row.classList.add("dragging");
      event.dataTransfer.effectAllowed = "move";
    });
    row.addEventListener("dragend", () => {
      draggedId = null;
      row.classList.remove("dragging");
    });
    row.addEventListener("dragover", (event) => event.preventDefault());
    row.addEventListener("drop", (event) => {
      event.preventDefault();
      const targetId = row.dataset.dragFile;
      if (!draggedId || draggedId === targetId) return;
      const nextIds = ids.filter((id) => id !== draggedId);
      nextIds.splice(nextIds.indexOf(targetId), 0, draggedId);
      saveOrder(nextIds);
    });
  });
}

async function parseFile(fileId, refresh) {
  try {
    const result = await api(`/files/${fileId}/parse`, { method: "POST" });
    toast(result.message || "已提交解析任务", "success");
    setTimeout(refresh, 600);
  } catch (error) { toast(error.message, "error"); }
}

function openAddCompany(project, container) {
  const modal = openModal({
    title: "添加投标企业",
    body: `<form id="company-form">
      <div class="form-row"><label for="company-name">企业名称 *</label><input id="company-name" name="company_name" required maxlength="255" /></div>
      <div class="form-row"><label for="company-credit">统一社会信用代码</label><input id="company-credit" name="credit_code" maxlength="64" /></div>
      <div class="form-row"><label for="company-remark">备注</label><textarea id="company-remark" name="remark"></textarea></div>
    </form>`,
    footer: `<button class="btn" data-close>取消</button><button class="btn primary" id="company-submit">添加并进入文件管理</button>`,
  });
  const submit = $("#company-submit", modal.root);
  submit.addEventListener("click", () => withBusy(submit, async () => {
    const form = $("#company-form", modal.root);
    if (!form.reportValidity()) return;
    const data = Object.fromEntries(new FormData(form).entries());
    if (!data.credit_code) delete data.credit_code;
    if (!data.remark) delete data.remark;
    try {
      const company = await api(`/projects/${project.id}/companies`, { method: "POST", body: data });
      selectedCompanyByProject[project.id] = company.id;
      modal.close(); toast("企业已添加", "success"); renderFiles(container, project);
    } catch (error) { toast(error.message, "error"); }
  }, "添加中…"));
}

async function previewLogical(company) {
  try {
    const tree = await api(`/companies/${company.id}/logical-document?include_pages=true`);
    const items = (tree.items || []).map((item) => `
      <details style="margin-bottom:8px" open>
        <summary><strong>${String(item.sort_order + 1).padStart(2, "0")} ${esc(item.file_name)}</strong>
          <span class="small muted">（${item.page_count} 页，${FILE_STATUS[item.parse_status] || item.parse_status}）</span></summary>
        <div class="small" style="margin:8px 0 0 18px; max-height:160px; overflow:auto">
          ${(item.pages || []).map((page) => `<a href="${API}/files/${item.file_id}/content?token=${encodeURIComponent(state.token)}#page=${page.page_index}" target="_blank" style="margin-right:10px">第${page.page_index}页</a>`).join("") || `<span class="muted">该文件尚未解析</span>`}
        </div>
      </details>`).join("");
    openModal({
      title: `逻辑标书预览 · ${company.company_name}`,
      wide: true,
      body: `<div class="info-box">逻辑标书只组织文件顺序，不合并、不修改原始文件；点击页码打开对应原始文件页。</div>
        <div class="small muted" style="margin-bottom:10px">版本 v${tree.version_no || "—"} · 逻辑总页数 ${tree.total_pages} · 状态 ${tree.status}</div>
        ${items || `<div class="empty">尚未生成逻辑标书</div>`}`,
    });
  } catch (error) { toast(error.message, "error"); }
}

/* ---------- parse center ---------- */
async function renderParseCenter(container, project) {
  const data = await api(`/projects/${project.id}/parse-center`);
  const counts = data.counts || {};
  const rows = data.rows.map((row) => `
    <tr>
      <td>${esc(row.file_name)}<div class="small muted">${row.file_type.toUpperCase()} · v${row.version}</div></td>
      <td>${esc(row.company_name)}</td>
      <td>${row.page_count}</td>
      <td>${row.success_pages}</td>
      <td>${row.ocr_pages}</td>
      <td>${row.empty_pages}</td>
      <td>${filePill(row.parse_status)}${row.parse_message ? `<div class="small muted">${esc(row.parse_message)}</div>` : ""}</td>
      <td>
        <button class="btn small" data-pages="${row.id}" data-name="${esc(row.file_name)}">查看解析</button>
        ${["pending", "failed", "partial"].includes(row.parse_status) ? `<button class="btn small" data-retry="${row.id}">${row.parse_status === "failed" ? "重试" : "解析"}</button>` : ""}
      </td>
    </tr>`).join("");

  container.innerHTML = `
    <div class="grid cols-auto" style="margin-bottom:14px">
      ${statCard("总文件数", data.total, "database")}
      ${statCard("待解析", num(counts.pending), "clock")}
      ${statCard("解析中", num(counts.parsing), "activity")}
      ${statCard("成功", num(counts.success), "check", "ok")}
      ${statCard("部分成功", num(counts.partial), "alert", "medium")}
      ${statCard("失败", num(counts.failed), "alert", "high")}
    </div>
    <div class="card">
      <div class="row" style="margin-bottom:12px">
        <h2 style="margin:0">文件解析</h2>
        <span class="spacer"></span>
        ${["queued", "running"].includes(data.analysis?.status)
          ? `<span class="small muted">比对已自动开始（${taskPill(data.analysis.status)}）</span><a class="btn small" href="#/projects/${project.id}?tab=task">查看比对进展</a>`
          : `<span class="small muted">全部文件解析完成后，系统会自动开始项目内企业两两比对</span>`}
        <button class="btn primary small" id="parse-all">${icon("scan")}全部解析</button>
      </div>
      <table><thead><tr><th>文件名称</th><th>所属企业</th><th>总页数</th><th>成功页数</th><th>OCR页数</th><th>空白页</th><th>状态</th><th>操作</th></tr></thead>
        <tbody>${rows || `<tr><td colspan="8"><div class="empty">暂无文件</div></td></tr>`}</tbody></table>
    </div>`;

  const parseAllBtn = $("#parse-all");
  parseAllBtn.addEventListener("click", () => withBusy(parseAllBtn, async () => {
    try { const result = await api(`/projects/${project.id}/parse`, { method: "POST" }); toast(result.message, "success"); setTimeout(() => renderParseCenter(container, project), 1000); }
    catch (error) { toast(error.message, "error"); }
  }, "提交中…"));
  $$("[data-retry]").forEach((btn) => btn.addEventListener("click", () => parseFile(btn.dataset.retry, () => renderParseCenter(container, project))));
  $$("[data-pages]").forEach((btn) => btn.addEventListener("click", () => openPagesModal(btn.dataset.pages, btn.dataset.name)));
  if (Object.keys(counts).some((key) => ["pending", "parsing", "uploading"].includes(key) && counts[key] > 0)) {
    pollTimer = setTimeout(() => renderParseCenter(container, project), 2500);
  }
}

async function openPagesModal(fileId, fileName) {
  const modal = openModal({
    title: `解析结果 · ${fileName}`,
    wide: true,
    body: `<div class="row" style="margin-bottom:10px">
        <a class="btn small" href="${API}/files/${fileId}/content?token=${encodeURIComponent(state.token)}" target="_blank">打开原始文件</a>
        <span class="small muted">左侧选择页面，右侧查看提取文本；原始 PDF 可在新窗口查看。</span>
      </div>
    <div class="viewer-grid" style="grid-template-columns:200px 1fr">
      <div><div id="page-list" class="check-list"></div></div>
      <div id="page-content" class="viewer-text muted">选择左侧页面查看提取文本</div>
    </div>`,
  });
  const data = await api(`/files/${fileId}/pages`);
  const list = $("#page-list", modal.root);
  list.innerHTML = data.items.map((page) => `
    <button class="btn small" style="justify-content:space-between" data-page="${page.page_index}">
      第 ${page.page_index} 页 ${page.parse_status === "success" ? "✓" : page.parse_status === "empty" ? "空白" : "失败"}
    </button>`).join("") || `<div class="empty">无页面</div>`;
  list.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-page]");
    if (!button) return;
    const content = $("#page-content", modal.root);
    content.innerHTML = "加载中…";
    try {
      const detail = await api(`/files/${fileId}/pages/${button.dataset.page}`);
      const page = detail.page;
      content.innerHTML = `
        <div class="small muted" style="margin-bottom:8px">第 ${page.page_index} 页 · ${page.extraction_method === "ocr" ? "OCR" : "原生文本"} · ${page.parse_status} ${page.note ? `· ${esc(page.note)}` : ""}</div>
        ${(page.blocks || []).map((block) => `<div style="margin-bottom:12px">${esc(block.content)}</div>`).join("") || `<div class="muted">该页没有可提取文本</div>`}`;
    } catch (error) { content.innerHTML = `<div class="error-box">${esc(error.message)}</div>`; }
  });
  const first = $("[data-page]", list);
  if (first) first.click();
}

/* ---------- 比对分析：两两比对工作区 ---------- */
async function renderCompare(container, project, options = {}) {
  const overview = await api(`/projects/${project.id}/results/overview`);
  const pairs = overview.pairs || [];

  const companyMap = new Map();
  pairs.forEach((pair) => {
    if (!companyMap.has(pair.company_a_id)) companyMap.set(pair.company_a_id, pair.company_a);
    if (!companyMap.has(pair.company_b_id)) companyMap.set(pair.company_b_id, pair.company_b);
  });
  const companies = [...companyMap.entries()].map(([id, name]) => ({ id, name }));

  container.innerHTML = `
    <div class="card">
      <div class="row">
        <h2 style="margin:0">两两比对</h2>
        <span class="spacer"></span>
        <span class="small muted">共 ${pairs.length} 个企业组合 · 选择任意两家企业查看三栏证据对比</span>
      </div>
      <div class="row" style="margin-top:12px">
        <select id="cmp-company-a" aria-label="选择企业A" ${companies.length ? "" : "disabled"}></select>
        <span class="muted" aria-hidden="true">${icon("arrow-left-right")}</span>
        <select id="cmp-company-b" aria-label="选择企业B" ${companies.length ? "" : "disabled"}></select>
        <span id="cmp-risk"></span>
        <span class="spacer"></span>
        <button class="btn small" id="cmp-export" ${pairs.length ? "" : "disabled"}>${icon("download")}导出本组合 Excel</button>
      </div>
      <div class="small muted" style="margin-top:8px" id="cmp-hint"></div>
    </div>
    <div id="cmp-viewer"></div>`;

  const selectA = $("#cmp-company-a", container);
  const selectB = $("#cmp-company-b", container);
  const optionHtml = companies.map((company) => `<option value="${company.id}">${esc(company.name)}</option>`).join("");
  selectA.innerHTML = optionHtml;
  selectB.innerHTML = optionHtml;

  const viewerEl = $("#cmp-viewer", container);
  let currentPairId = null;

  const findPair = (aId, bId) => pairs.find((pair) =>
    (pair.company_a_id === aId && pair.company_b_id === bId)
    || (pair.company_a_id === bId && pair.company_b_id === aId));

  const loadPair = async (pairId, evidenceId) => {
    currentPairId = pairId;
    const pair = pairs.find((item) => item.pair_id === pairId);
    $("#cmp-risk", container).innerHTML = pair ? `综合风险 ${riskPill(pair.overall_risk)}` : "";
    $("#cmp-hint", container).textContent = pair
      ? `文本证据 ${pair.text_count} 条 · 人员重复 ${pair.personnel_count} 条 · 价格异常 ${pair.price_count} 条`
      : "";
    if (!pairId) {
      viewerEl.innerHTML = `<div class="card"><div class="empty">请选择两家企业。</div></div>`;
      return;
    }
    viewerEl.innerHTML = `<div class="card"><div class="loading">加载三栏对比…</div></div>`;
    await mountPairViewer(viewerEl, pairId, evidenceId);
  };

  const onSelectChange = () => {
    const aId = selectA.value;
    const bId = selectB.value;
    if (!aId || !bId) return;
    if (aId === bId) {
      $("#cmp-hint", container).textContent = "请选择两家不同的企业。";
      return;
    }
    const pair = findPair(aId, bId);
    if (!pair) {
      currentPairId = null;
      $("#cmp-risk", container).innerHTML = "";
      $("#cmp-hint", container).textContent = "该企业组合尚未产生分析结果。";
      viewerEl.innerHTML = `<div class="card"><div class="empty">该企业组合尚未产生分析结果。</div></div>`;
      return;
    }
    selectA.value = pair.company_a_id;
    selectB.value = pair.company_b_id;
    loadPair(pair.pair_id);
  };
  selectA.addEventListener("change", onSelectChange);
  selectB.addEventListener("change", onSelectChange);

  const exportBtn = $("#cmp-export", container);
  exportBtn.addEventListener("click", () => withBusy(exportBtn, async () => {
    if (!currentPairId) return toast("请先选择企业组合", "error");
    const pair = pairs.find((item) => item.pair_id === currentPairId);
    try {
      const blob = await api(`/pairs/${currentPairId}/reports/excel`, { blob: true });
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `${pair ? `${pair.company_a} vs ${pair.company_b}` : "比对证据"}-比对证据.xlsx`;
      document.body.appendChild(link); link.click(); link.remove();
      URL.revokeObjectURL(url);
      toast("本组合证据已导出", "success");
    } catch (error) { toast(error.message, "error"); }
  }, "导出中…"));

  const riskRank = { high: 3, medium: 2, low: 1, none: 0 };
  const ranked = pairs.slice().sort((a, b) => (riskRank[b.overall_risk] || 0) - (riskRank[a.overall_risk] || 0));
  const initial = (options.pairId && pairs.find((pair) => pair.pair_id === options.pairId)) || ranked[0];
  if (initial) {
    selectA.value = initial.company_a_id;
    selectB.value = initial.company_b_id;
    await loadPair(initial.pair_id, options.evidenceId);
  } else {
    viewerEl.innerHTML = `<div class="card"><div class="empty">还没有分析结果。投标文件解析完成后系统会自动开始两两比对，可到「分析任务」查看进展。<a class="btn primary" href="#/projects/${project.id}?tab=task">前往分析任务</a></div></div>`;
  }
}

/* ---------- 分析任务：两两比对任务与进展 ---------- */
async function renderTaskPage(container, project) {
  const tasks = await api(`/projects/${project.id}/tasks`);

  const taskCards = (tasks.items || []).map((task) => `
    <div class="check-item">
      <div class="grow">
        <div><strong>任务 #${task.task_no}</strong> ${taskPill(task.status)} <span class="small muted">${task.company_count} 家企业 · ${esc(task.algorithm_version || "")}</span></div>
        <div class="small muted">创建 ${fmtDate(task.created_at)} · 完成 ${fmtDate(task.finished_at)}</div>
      </div>
      <a class="btn small" href="#/projects/${project.id}?tab=task&task=${task.id}">查看进度</a>
      ${["completed", "partial"].includes(task.status) ? `<a class="btn small" href="#/projects/${project.id}?tab=analysis">查看比对</a>` : ""}
      ${["completed", "partial"].includes(task.status) ? `<a class="btn small" href="#/projects/${project.id}?tab=results">查看结果</a>` : ""}
    </div>`).join("") || `<div class="empty">还没有分析任务。投标文件解析完成后，系统会自动开始两两比对。</div>`;

  const params = new URLSearchParams(location.hash.split("?")[1] || "");
  const requestedTask = params.get("task");
  const activeTask = requestedTask || tasks.items?.[0]?.id;

  container.innerHTML = `
    <div class="card">
      <div class="row">
        <h2 style="margin:0">分析任务</h2>
        <span class="spacer"></span>
        <span class="small muted">项目内两家以上企业的两两比对进展，完成后进入「比对分析」查看证据</span>
      </div>
      <div class="info-box" style="margin-top:12px">
        比对范围自动确定：所有解析完成并参与分析的企业全部进入比对，文本相似、人员重复、价格异常三个维度同时执行；
        招标文件公共内容命中情况在「比对分析」的筛选中区分。
      </div>
      <div style="margin-top:10px">${taskCards}</div>
    </div>
    <div id="task-detail"></div>`;

  if (activeTask) await renderTaskDetail($("#task-detail", container), project, activeTask);
}

function ruleVersionLabel(analysisConfig) {
  const versions = (analysisConfig || {}).rule_versions || {};
  const parts = Object.entries(versions).map(([dimension, entry]) => `${RULE_DIM[dimension] || dimension} v${num(entry.version_no)}`);
  return parts.length ? parts.join(" · ") : "—";
}

async function renderTaskDetail(container, project, taskId) {
  const task = await api(`/tasks/${taskId}`);
  const progressRows = Object.entries(task.progress || {}).map(([dimension, entry]) => `
    <div style="margin-bottom:12px">
      <div class="row"><strong>${DIM[dimension] || dimension}</strong><span class="spacer"></span>
        <span class="small muted" data-dim-label="${esc(dimension)}">${dimStatusLabel(entry)}</span></div>
      ${liveProgressBar(entry.done || 0, entry.total || 0, dimension)}
    </div>`).join("");
  const logs = (task.logs || []).map((log) => `<div class="line ${esc(log.level)}">[${fmtDate(log.created_at)}] ${esc(log.message)}</div>`).join("");
  const companies = (task.companies || []).map((company) => `<div class="small">${esc(company.company_name)} · ${company.file_count} 份文件 · ${company.total_pages} 页 ${company.complete ? "" : `<span class="pill warn">部分解析</span>`}</div>`).join("");
  const failedPairs = (task.pairs || []).filter((pair) => pair.status === "failed");
  const failedHtml = failedPairs.length ? `
    <div class="warn-box" style="margin-top:14px">
      <strong>失败的企业组合</strong>
      ${failedPairs.map((pair) => `<div class="row small" style="margin-top:6px">
        <span>${esc(pair.company_a)} ↔ ${esc(pair.company_b)}</span>
        <span class="muted">${esc(pair.error_message || "")}</span>
        <span class="spacer"></span>
        <button class="btn small" data-retry-pair="${pair.id}">重新执行该组合</button>
      </div>`).join("")}
    </div>` : "";

  container.innerHTML = `
    <div class="card">
      <div class="row">
        <h2 style="margin:0">任务 #${task.task_no} 进度</h2><span id="live-task-status" aria-live="polite" aria-atomic="true">${taskPill(task.status)}</span>
        <span class="spacer"></span>
        ${["queued", "running"].includes(task.status) ? `<button class="btn small" id="cancel-task">取消任务</button>` : ""}
      </div>
      <div class="small muted" id="live-task-counts" aria-live="polite" style="margin:8px 0 14px">算法版本 ${esc(task.algorithm_version)} · 企业组合 ${task.pair_count} 组 · 完成 ${task.completed_pair_count} 组 · 失败 ${task.failed_pair_count} 组</div>
      <div class="small muted" style="margin:-8px 0 14px">规则版本：${esc(ruleVersionLabel(task.analysis_config))}</div>
      ${task.error_message ? `<div class="warn-box">${esc(task.error_message)}</div>` : ""}
      ${progressRows || `<div class="empty">暂无进度</div>`}
      ${failedHtml}
      <div class="grid cols-2" style="margin-top:16px">
        <div><h3>参与企业及文件版本</h3>${companies}</div>
        <div><h3>执行日志</h3><div class="log-list">${logs || "暂无日志"}</div></div>
      </div>
    </div>`;
  const cancelBtn = $("#cancel-task", container);
  cancelBtn?.addEventListener("click", () => {
    if (!confirm("确认取消该分析任务？")) return;
    withBusy(cancelBtn, async () => {
      try { await api(`/tasks/${task.id}/cancel`, { method: "POST" }); toast("任务已取消", "success"); renderTaskDetail(container, project, task.id); }
      catch (error) { toast(error.message, "error"); }
    }, "取消中…");
  });
  $$("[data-retry-pair]", container).forEach((button) => button.addEventListener("click", async () => {
    button.disabled = true;
    try {
      await api(`/tasks/${task.id}/pairs/${button.dataset.retryPair}/retry`, { method: "POST" });
      toast("已提交重新执行", "success");
      setTimeout(() => renderTaskDetail(container, project, task.id), 1500);
    } catch (error) { toast(error.message, "error"); button.disabled = false; }
  }));
  /* 实时进度：优先使用 SSE 事件流推送，连接不可用时退回定时轮询 */
  let lastStatus = task.status;
  let sseBroken = false;
  const applySnapshot = (snapshot) => {
    const live = snapshot && snapshot.task;
    if (!live) return;
    /* 事件流推送的是项目最新任务；正在查看旧任务时不据此刷新 */
    if (live.id !== task.id) return;
    const statusNode = $("#live-task-status", container);
    if (statusNode) statusNode.innerHTML = taskPill(live.status);
    const counts = $("#live-task-counts", container);
    if (counts) {
      counts.textContent = `算法版本 ${task.algorithm_version} · 企业组合 ${live.pair_count} 组 · 完成 ${live.completed_pair_count} 组 · 失败 ${live.failed_pair_count} 组 · 证据 ${live.evidence_count} 条 · 待审核 ${live.pending_review_count} 条`;
    }
    Object.entries(live.progress || {}).forEach(([dimension, entry]) => {
      const done = entry.done || 0;
      const total = entry.total || 0;
      const fill = $(`[data-dim-fill="${dimension}"]`, container);
      const count = $(`[data-dim-count="${dimension}"]`, container);
      const label = $(`[data-dim-label="${dimension}"]`, container);
      if (fill) fill.style.width = `${total ? Math.min(100, Math.round((done / total) * 100)) : 0}%`;
      if (fill && fill.parentElement) {
        fill.parentElement.setAttribute("aria-valuenow", String(done));
        fill.parentElement.setAttribute("aria-valuemax", String(total));
      }
      if (count) count.textContent = `${done} / ${total}`;
      if (label) label.textContent = dimStatusLabel(entry);
    });
    if (live.status !== lastStatus) {
      lastStatus = live.status;
      if (["completed", "partial", "failed", "cancelled"].includes(live.status)) {
        renderTaskDetail(container, project, task.id);
      }
    }
  };
  const fallbackPoll = () => {
    if (sseBroken) return;
    sseBroken = true;
    clearTimeout(pollTimer);
    pollTimer = setTimeout(() => {
      if (location.hash.includes(`/projects/${project.id}`)) renderTaskDetail(container, project, task.id);
    }, 3000);
  };
  if (location.hash.includes(`/projects/${project.id}`)) {
    openEventStream(project.id, applySnapshot, fallbackPoll);
  }
}

/* ---------- results ---------- */
async function renderResults(container, project) {
  const data = await api(`/projects/${project.id}/results/overview`);
  if (!data.task) {
    container.innerHTML = `<div class="card empty">还没有完成的分析结果。<a class="btn primary" href="#/projects/${project.id}?tab=task">前往分析任务</a></div>`;
    return;
  }
  const stats = data.stats;
  const pairRows = data.pairs.map((pair) => `
    <tr>
      <td><a href="#/projects/${project.id}?tab=analysis&pair=${pair.pair_id}">${esc(pair.company_a)} ↔ ${esc(pair.company_b)}</a></td>
      <td>${pair.text_count}</td><td>${pair.personnel_count}</td><td>${pair.price_count}</td>
      <td class="opt">${riskPill(pair.text_risk)}</td><td class="opt">${riskPill(pair.personnel_risk)}</td><td class="opt">${riskPill(pair.price_risk)}</td>
      <td>${riskPill(pair.overall_risk)}</td>
      <td><a class="btn small" href="#/projects/${project.id}?tab=analysis&pair=${pair.pair_id}">三栏查看</a></td>
    </tr>`).join("");
  const top = data.top_evidences.map((evidence) => `
    <div class="evidence-item">
      <div class="row"><span class="pill ${esc(evidence.severity)}">${RISK[evidence.severity] || "无"}</span>
        <span class="small muted">${TYPE[evidence.evidence_type] || evidence.evidence_type} · ${esc(evidence.companies.join("、"))}</span>
        <span class="spacer"></span><span class="pill">${REVIEW[evidence.review_status] || evidence.review_status}</span></div>
      <div class="summary">${esc(evidence.summary)}</div>
      <div class="meta">${evidence.pair_id ? `<a href="#/projects/${project.id}?tab=analysis&pair=${evidence.pair_id}&evidence=${evidence.id}">进入三栏复核 →</a>` : `项目级价格异常证据`}</div>
    </div>`).join("") || `<div class="empty">暂无证据</div>`;

  container.innerHTML = `
    <div class="grid cols-5">
      ${statCard("参与企业", stats.participant_count, "users")}
      ${statCard("比对组合", stats.pair_count, "layers")}
      ${statCard("文本相似证据", stats.text_evidence_count, "file", "medium")}
      ${statCard("重复人员", stats.personnel_duplicate_count, "user", "medium")}
      ${statCard("价格异常企业", stats.price_abnormal_company_count, "coins", "high")}
    </div>
    <div class="card" style="margin-top:16px">
      <div class="row" style="justify-content:space-between">
        <h2 style="margin:0">企业组合矩阵</h2>
        <div class="small muted">风险分布：高 ${num(stats.risk_distribution.high)} · 中 ${num(stats.risk_distribution.medium)} · 低 ${num(stats.risk_distribution.low)} · 无 ${num(stats.risk_distribution.none)}</div>
      </div>
      <div class="matrix" style="margin-top:12px">
        ${data.matrix.map((pair) => `
          <a class="matrix-cell" href="#/projects/${project.id}?tab=analysis&pair=${pair.pair_id}" aria-label="查看 ${esc(pair.company_a)} 与 ${esc(pair.company_b)} 的比对证据">
            <div class="pair">${esc(pair.company_a)} ↔ ${esc(pair.company_b)}</div>
            <div class="meta">文本 ${pair.text_count} · 人员 ${pair.personnel_count} · 价格 ${pair.price_count}</div>
            <div style="margin-top:6px">${riskPill(pair.overall_risk)}</div>
          </a>`).join("") || `<div class="empty">暂无组合</div>`}
      </div>
    </div>
    <div class="card">
      <h2>企业组合列表</h2>
      <table><thead><tr><th>企业组合</th><th>文本证据</th><th>人员重复</th><th>价格异常</th><th class="opt">文本风险</th><th class="opt">人员风险</th><th class="opt">价格风险</th><th>综合风险</th><th>操作</th></tr></thead>
        <tbody>${pairRows || `<tr><td colspan="9"><div class="empty">暂无结果</div></td></tr>`}</tbody></table>
    </div>
    <div class="card">
      <div class="row" style="justify-content:space-between"><h2 style="margin:0">重点异常证据</h2><span class="small muted">待审核 ${stats.pending_review_count} 条</span></div>
      <div class="evidence-list" style="margin-top:10px">${top}</div>
    </div>`;
}

/* ---------- report ---------- */
async function renderReport(container, project) {
  const data = await api(`/projects/${project.id}/results/overview`);
  const stats = data.stats || {};
  const task = data.task;
  container.innerHTML = `
    <div class="card">
      <h2>分析报告</h2>
      ${task ? `<div class="info-box">当前报告基于任务 #${task.task_no}（${TASK_STATUS[task.status] || task.status}）生成，算法版本 ${esc(task.algorithm_version)}。报告包含 4 个工作表。</div>` : `<div class="warn-box">项目还没有可导出的分析结果。</div>`}
      <div class="grid cols-4">
        ${statCard("参与企业", num(stats.participant_count), "users")}
        ${statCard("比对组合", num(stats.pair_count), "layers")}
        ${statCard("证据总数", num(stats.text_evidence_count) + num(stats.personnel_duplicate_count) + num(stats.price_abnormal_company_count), "shield", "medium")}
        ${statCard("待审核", num(stats.pending_review_count), "clock", num(stats.pending_review_count) ? "high" : "")}
      </div>
      <div class="grid cols-2" style="margin-top:16px">
        <div>
          <h3>Excel 工作表</h3>
          <ul class="small muted">
            <li>投标人比对概览：企业组合、各维度统计和风险提示</li>
            <li>文本雷同汇总表：匹配文本、双方文件页码、相似字数、排除状态</li>
            <li>人员姓名重复表：重复姓名、企业、岗位、文件、页码、出现次数</li>
            <li>价格异常分析表：总报价、大写金额、偏差率、异常类型、风险提示</li>
          </ul>
        </div>
        <div>
          <h3>导出</h3>
          <button class="btn primary" id="download-report" ${task ? "" : "disabled"}>${icon("download")}下载 Excel 报告</button>
          <p class="small muted" style="margin-top:10px">报告内容为疑似异常线索，不构成围标串标认定结论；所有证据均保留原始文件、页码和文本位置。</p>
        </div>
      </div>
    </div>
    <div class="card">
      <h2>历史分析任务</h2>
      <table><thead><tr><th>任务</th><th>状态</th><th>企业数</th><th>创建时间</th><th>完成时间</th><th class="opt">算法版本</th></tr></thead>
        <tbody>${(data.tasks || []).map((t) => `<tr><td>#${t.task_no}</td><td>${taskPill(t.status)}</td><td>${t.company_count}</td><td>${fmtDate(t.created_at)}</td><td>${fmtDate(t.finished_at)}</td><td class="mono opt">${esc(t.algorithm_version)}</td></tr>`).join("") || `<tr><td colspan="6"><div class="empty">暂无任务</div></td></tr>`}</tbody></table>
    </div>`;
  const downloadBtn = $("#download-report", container);
  downloadBtn?.addEventListener("click", () => withBusy(downloadBtn, async () => {
    try {
      const blob = await api(`/projects/${project.id}/reports/excel`, { blob: true });
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url; link.download = `${project.project_name}-分析报告.xlsx`;
      document.body.appendChild(link); link.click(); link.remove();
      URL.revokeObjectURL(url);
      toast("报告已导出", "success");
    } catch (error) { toast(error.message, "error"); }
  }, "生成中…"));
}

/* ---------- 三栏证据查看器（可嵌入比对分析页，也可独立路由打开） ---------- */
async function mountPairViewer(container, pairId, evidenceId) {
  const data = await api(`/pairs/${pairId}`);
  const pair = data.pair;
  let currentType = "text";
  let currentEvidenceId = evidenceId || null;
  let filterReview = "";
  let filterSeverity = "";
  let filterTender = "";

  const byType = (type) => data.evidences.filter((item) => item.evidence_type === type);
  const visible = (type) => byType(type).filter((item) => {
    if (filterReview && item.review_status !== filterReview) return false;
    if (filterSeverity && item.severity !== filterSeverity) return false;
    if (filterTender === "hit" && !item.excluded_by_tender) return false;
    if (filterTender === "effective" && item.excluded_by_tender) return false;
    return true;
  });
  if (currentEvidenceId) {
    const found = data.evidences.find((item) => item.id === currentEvidenceId);
    if (found) currentType = found.evidence_type;
  } else {
    const first = data.evidences.find((item) => item.evidence_type === "text") || data.evidences[0];
    currentEvidenceId = first ? first.id : null;
    if (first) currentType = first.evidence_type;
  }

  container.innerHTML = `
    <div class="card viewer-toolbar">
      <div class="row">
        <strong>${esc(pair.company_a)} ↔ ${esc(pair.company_b)}</strong>
        ${riskPill(pair.overall_risk)}
        <span class="spacer"></span>
        <select id="ev-filter-review" aria-label="按审核状态筛选证据"><option value="">全部审核状态</option>${Object.entries(REVIEW).map(([v, l]) => `<option value="${v}">${l}</option>`).join("")}</select>
        <select id="ev-filter-severity" aria-label="按风险等级筛选证据"><option value="">全部风险</option>${Object.entries(RISK).map(([v, l]) => `<option value="${v}">${l}</option>`).join("")}</select>
        <select id="ev-filter-tender" aria-label="按招标文件命中情况筛选">
          <option value="">全部内容</option>
          <option value="hit">命中招标说明</option>
          <option value="effective">未命中招标说明</option>
        </select>
      </div>
      <div class="small muted" style="margin-top:8px">
        证据清单通过颜色和标识区分：<span class="flag-inline tender">命中招标说明</span> 表示内容命中招标文件公共内容（已排除，但仍保留识别记录）；
        左侧色条表示人工审核状态。筛选条件只影响清单展示，不影响原始识别结果。
      </div>
    </div>
    <div class="tabs" id="evidence-tabs" role="tablist" aria-label="证据类型">
      ${Object.entries(TYPE).map(([type, label]) => `<button type="button" class="tab ${type === currentType ? "active" : ""}" role="tab" aria-selected="${type === currentType}" data-type="${type}">${label}（${visible(type).length}）</button>`).join("")}
    </div>
    <div class="viewer-grid">
      <div class="viewer-col" id="viewer-a"><div class="loading">选择证据后定位原文</div></div>
      <div class="viewer-col" id="viewer-b"><div class="loading">选择证据后定位原文</div></div>
      <div class="viewer-col evidence-col">
        <div class="col-head"><div class="title">证据列表与详情</div><div class="file" id="evidence-count"></div></div>
        <div class="viewer-body">
          <div class="evidence-list" id="evidence-list"></div>
          <div id="evidence-detail" class="detail-block"><div class="muted small">点击上方证据查看详情并联动左右原文。</div></div>
        </div>
      </div>
    </div>`;

  const listEl = $("#evidence-list", container);
  const detailEl = $("#evidence-detail", container);
  const viewerA = $("#viewer-a", container);
  const viewerB = $("#viewer-b", container);

  const renderTabs = () => {
    $$("#evidence-tabs [data-type]", container).forEach((tab) => {
      const active = tab.dataset.type === currentType;
      tab.classList.toggle("active", active);
      tab.setAttribute("aria-selected", String(active));
      tab.textContent = `${TYPE[tab.dataset.type] || tab.dataset.type}（${visible(tab.dataset.type).length}）`;
    });
  };

  const renderList = () => {
    const items = visible(currentType);
    const total = byType(currentType).length;
    $("#evidence-count", container).textContent = items.length === total
      ? `${total} 条证据`
      : `${items.length} / ${total} 条（已筛选）`;
    listEl.innerHTML = items.map((item) => `
      <div class="evidence-item review-${esc(item.review_status)} ${item.excluded_by_tender ? "tender-hit" : ""} ${item.id === currentEvidenceId ? "active" : ""}"
        data-evidence-id="${item.id}" role="button" tabindex="0" aria-pressed="${item.id === currentEvidenceId}" aria-label="查看证据：${esc(item.summary)}">
        <div class="row"><span class="pill ${esc(item.severity)}">${RISK[item.severity] || "无"}</span>
          <span class="small muted">${esc(subtypeLabel(item.subtype))}</span><span class="spacer"></span>
          <span class="pill">${REVIEW[item.review_status] || item.review_status}</span></div>
        ${item.excluded_by_tender ? `<div class="flag tender">命中招标说明 · 公共内容已排除</div>` : ""}
        <div class="summary">${esc(item.summary)}</div>
      </div>`).join("") || `<div class="empty" style="padding:18px">没有符合筛选条件的证据</div>`;
    $$("[data-evidence-id]", listEl).forEach((el) => {
      el.addEventListener("click", () => selectEvidence(el.dataset.evidenceId));
      el.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          selectEvidence(el.dataset.evidenceId);
        }
      });
    });
  };

  const selectEvidence = async (id) => {
    currentEvidenceId = id;
    renderList();
    detailEl.innerHTML = skeletonBlock(5);
    try {
      const evidence = await api(`/evidences/${id}`);
      renderDetail(evidence);
      renderSides(evidence);
      currentType = evidence.evidence_type;
    } catch (error) {
      detailEl.innerHTML = `<div class="error-box">${esc(error.message)}</div>`;
    }
  };

  const renderDetail = (evidence) => {
    detailEl.innerHTML = evidenceDetailHtml(evidence) + reviewFormHtml(evidence);
    bindReviewForm(detailEl, evidence, async () => {
      const refreshed = await api(`/evidences/${evidence.id}`);
      const index = data.evidences.findIndex((item) => item.id === refreshed.id);
      if (index >= 0) data.evidences[index] = { ...data.evidences[index], review_status: refreshed.review_status };
      renderList();
      renderTabs();
      renderDetail(refreshed);
    });
  };

  const renderSides = (evidence) => {
    const sourceA = sideSource(evidence, pair, "a");
    const sourceB = sideSource(evidence, pair, "b");
    renderSource(viewerA, sourceA, pair.company_a, "企业A 原文");
    renderSource(viewerB, sourceB, pair.company_b, "企业B 原文");
  };

  const applyFilters = () => {
    filterReview = $("#ev-filter-review", container).value;
    filterSeverity = $("#ev-filter-severity", container).value;
    filterTender = $("#ev-filter-tender", container).value;
    renderList();
    renderTabs();
    if (currentEvidenceId && !visible(currentType).some((item) => item.id === currentEvidenceId)) {
      currentEvidenceId = null;
      detailEl.innerHTML = `<div class="muted small">当前证据已被筛选条件排除，请重新选择证据。</div>`;
    }
  };

  $("#ev-filter-review", container).addEventListener("change", applyFilters);
  $("#ev-filter-severity", container).addEventListener("change", applyFilters);
  $("#ev-filter-tender", container).addEventListener("change", applyFilters);

  $$("#evidence-tabs [data-type]", container).forEach((tab) => tab.addEventListener("click", () => {
    currentType = tab.dataset.type;
    $$("#evidence-tabs [data-type]", container).forEach((el) => el.classList.toggle("active", el === tab));
    const items = visible(currentType);
    const first = items[0];
    currentEvidenceId = first ? first.id : null;
    renderList();
    if (first) selectEvidence(first.id);
    else {
      detailEl.innerHTML = `<div class="muted small">该类型暂无符合条件的证据</div>`;
      viewerA.innerHTML = `<div class="loading">暂无证据</div>`;
      viewerB.innerHTML = `<div class="loading">暂无证据</div>`;
    }
  }));

  renderList();
  if (currentEvidenceId) await selectEvidence(currentEvidenceId);
  else detailEl.innerHTML = `<div class="muted small">该类型暂无证据</div>`;
}

/* ---------- 独立三栏详情路由 ---------- */
async function renderPair(projectId, pairId, evidenceId) {
  const data = await api(`/pairs/${pairId}`);
  const pair = data.pair;
  $("#main").innerHTML = `
    <div class="page-head">
      <div>
        <div class="small muted"><a href="#/projects/${projectId}?tab=analysis">比对分析</a> / 三栏详情</div>
        <h1>${esc(pair.company_a)} <span class="muted" style="font-weight:400">↔</span> ${esc(pair.company_b)}</h1>
        <div class="sub">综合风险 ${riskPill(pair.overall_risk)} ${(pair.risk_reasons || []).map((reason) => `<span class="small muted" style="margin-left:8px">${esc(reason)}</span>`).join("")}</div>
      </div>
      <a class="btn" href="#/projects/${projectId}?tab=analysis&pair=${pairId}">返回比对分析</a>
    </div>
    <div id="pair-viewer"></div>`;
  await mountPairViewer($("#pair-viewer"), pairId, evidenceId);
}

function sideSource(evidence, pair, side) {
  const companyId = side === "a" ? pair.company_a_id : pair.company_b_id;
  const participant = (evidence.participants || []).find((item) => item.company_id === companyId);
  if (participant?.source) return participant.source;
  return side === "a" ? evidence.source_a : evidence.source_b;
}

async function renderSource(container, source, companyName, label) {
  if (!source) {
    container.innerHTML = `<div class="col-head"><div class="title">${esc(companyName || label)}</div></div><div class="viewer-body muted">该证据没有提供此侧原文位置。</div>`;
    return;
  }
  const pageLabel = source.page_start && source.page_end && source.page_end !== source.page_start
    ? `第 ${source.page_start}-${source.page_end} 页`
    : `第 ${source.page_index ?? "—"} 页`;
  const head = `<div class="col-head">
      <div class="title">${esc(source.company_name || companyName || label)}</div>
      <div class="file">${esc(source.file_name || "")} · ${pageLabel} ${source.printed_page_number ? `（印刷页码 ${esc(source.printed_page_number)}）` : ""}</div>
      <div class="small muted">定位精度：${source.locator_precision === "block" ? "文本块级（无坐标）" : source.locator_precision || "页级"}</div>
    </div>`;
  if (source.file_type === "pdf") {
    const url = `${API}/files/${source.file_id}/content?token=${encodeURIComponent(state.token)}#page=${source.page_index || 1}`;
    container.innerHTML = `${head}<div class="viewer-body" style="padding:0"><iframe src="${url}" title="${esc(source.file_name || "")}"></iframe></div>`;
    return;
  }
  container.innerHTML = `${head}<div class="viewer-body"><div class="loading">加载原文…</div></div>`;
  const bodyEl = $(".viewer-body", container);
  try {
    const rangeList = (source.ranges && source.ranges.length)
      ? source.ranges
      : [{ block_id: source.block_id, start_offset: source.start_offset, end_offset: source.end_offset }];
    const firstPage = source.page_start || source.page_index || 1;
    const lastPage = Math.max(firstPage, source.page_end || source.page_index || firstPage);
    const pageNumbers = [];
    for (let pageNumber = firstPage; pageNumber <= lastPage; pageNumber += 1) pageNumbers.push(pageNumber);
    const details = await Promise.all(pageNumbers.map((pageNumber) => api(`/files/${source.file_id}/pages/${pageNumber}`)));
    const html = details.map((detail) => {
      const page = detail.page;
      const blocks = page.blocks || [];
      const blocksHtml = blocks.map((block) => {
        const ranges = rangeList
          .filter((range) => range.block_id === block.id)
          // range.start_offset / end_offset 是「块内」偏移（后端按 block.content 切片给出），
          // 而 block.start_offset 是「页内」偏移，两者不能相减，否则除每页第一个块外
          // 算出的区间都是负数，高亮会整段消失。
          .map((range) => ({
            start: Math.max(0, Math.min(range.start_offset || 0, block.content.length)),
            end: Math.max(0, Math.min(range.end_offset || block.content.length, block.content.length)),
          }))
          .filter((range) => range.end > range.start)
          .sort((a, b) => a.start - b.start);
        if (!ranges.length) return `<div style="margin-bottom:12px">${esc(block.content)}</div>`;
        let cursor = 0;
        let marked = "";
        for (const range of ranges) {
          const start = Math.max(cursor, range.start);
          marked += esc(block.content.slice(cursor, start));
          marked += `<mark>${esc(block.content.slice(start, range.end))}</mark>`;
          cursor = Math.max(cursor, range.end);
        }
        marked += esc(block.content.slice(cursor));
        return `<div style="margin-bottom:12px; background:#fffbeb; outline:1px solid #fde68a; padding:8px; border-radius:4px">${marked}</div>`;
      }).join("");
      return `<div class="small muted" style="margin:10px 0 6px">第 ${page.page_index} 页 · ${page.extraction_method === "ocr" ? "OCR 识别文本" : "原生文本"} · 共 ${blocks.length} 个文本块</div>${blocksHtml || `<div class="muted">该页没有文本</div>`}`;
    }).join("");
    bodyEl.innerHTML = html;
  } catch (error) {
    bodyEl.innerHTML = `<div class="error-box">${esc(error.message)}</div><div class="small muted">${esc(source.snippet || "")}</div>`;
  }
}

function evidenceDetailHtml(evidence) {
  const data = evidence.evidence_data || {};
  let fields = "";
  if (evidence.evidence_type === "text") {
    const effective = data.effective_similarity != null ? `（有效 ${num(data.effective_similarity)}）` : "";
    const chapterA = [evidence.source_a?.chapter, evidence.source_a?.section].filter(Boolean).join(" / ");
    const chapterB = [evidence.source_b?.chapter, evidence.source_b?.section].filter(Boolean).join(" / ");
    fields = `
      <dt>相似指标</dt><dd><strong>${num(data.similarity_percent)}%</strong>${effective} · 雷同 ${num(data.matched_chars)} 字</dd>
      <dt>相似度</dt><dd>块级 ${num(data.similarity)}（局部 ${num(data.local_ratio)}）</dd>
      <dt>章节定位</dt><dd class="small">A：${esc(chapterA || "—")}<br>B：${esc(chapterB || "—")}</dd>
      <dt>公共内容排除</dt><dd>${data.excluded_by_tender ? `<span class="pill warn">命中招标文件公共内容</span> 排除比例 ${num(data.exclusion_ratio)}` : "否"}</dd>
      <dt>匹配文本</dt><dd><div class="marker" style="max-height:160px; overflow:auto">${esc(data.matched_text || "")}</div></dd>`;
  } else if (evidence.evidence_type === "personnel") {
    const occurrences = [...(data.occurrences_a || []), ...(data.occurrences_b || [])];
    fields = `
      <dt>重复姓名</dt><dd><strong>${esc(data.name || `${data.name_a} / ${data.name_b}`)}</strong></dd>
      <dt>匹配类型</dt><dd>${data.match_type === "exact" ? "完全同名" : `疑似相似姓名（${num(data.similarity)}）`}</dd>
      <dt>涉及岗位</dt><dd>${esc((data.roles || []).join("、") || "未标注")}</dd>
      <dt>出现位置</dt><dd>${occurrences.map((occ) => `<div class="small">${esc(occ.company_name || "")} · ${esc(occ.role || "")} · ${esc(occ.file_name || "")} 第${occ.page_index}页</div>`).join("") || "—"}</dd>
      <dt>说明</dt><dd class="small muted">${esc(data.note || "同名不代表同一人，需要结合证件号码、资格证书等进一步核实。")}</dd>`;
  } else if (evidence.evidence_type === "price") {
    const quotes = data.quotes || [];
    fields = `
      <dt>异常规则</dt><dd>${esc(data.rule_label || evidence.evidence_subtype)}</dd>
      <dt>平均报价</dt><dd>${Number(data.mean || 0).toLocaleString("zh-CN", { minimumFractionDigits: 2 })} 元</dd>
      <dt>参与企业</dt><dd>${quotes.length} 家</dd>
      <dt>报价明细</dt><dd>
        <table class="small"><thead><tr><th>企业</th><th>总报价</th><th>偏差率</th><th>原文</th></tr></thead><tbody>
        ${quotes.map((quote) => `<tr><td>${esc(quote.company_name)}</td><td>${Number(quote.amount || 0).toLocaleString("zh-CN", { minimumFractionDigits: 2 })}</td><td>${(num(quote.deviation_rate) * 100).toFixed(2)}%</td>
          <td>${quote.source?.file_id ? `<a href="${API}/files/${quote.source.file_id}/content?token=${encodeURIComponent(state.token)}#page=${quote.source.page_index}" target="_blank">${esc(quote.source.file_name)} 第${quote.source.page_index}页</a>` : "—"}</td></tr>`).join("")}
        </tbody></table></dd>
      <dt>说明</dt><dd class="small muted">${esc(data.note || "")}</dd>`;
  }

  return `
    <div class="small muted" style="font-weight:600; margin-bottom:6px">证据详情</div>
    <div class="row"><span class="pill ${esc(evidence.severity)}">${RISK[evidence.severity] || "无"}</span>
      <span class="small muted">${TYPE[evidence.evidence_type] || evidence.evidence_type}</span>
      <span class="spacer"></span><span class="pill">${REVIEW[evidence.review_status] || evidence.review_status}</span></div>
    <p style="margin:8px 0">${esc(evidence.summary)}</p>
    <dl class="kv">${fields}</dl>
    ${renderHistory(evidence.review_records || [])}`;
}

function renderHistory(records) {
  if (!records.length) return "";
  return `<div class="review-history"><div class="small" style="font-weight:600; margin-bottom:6px">审核记录</div>
    ${records.map((record) => `<div class="item"><strong>${esc(record.reviewer_name)}</strong> · ${fmtDate(record.created_at)} · ${REVIEW[record.old_status] || record.old_status} → ${REVIEW[record.new_status] || record.new_status}
      ${record.comment ? `<div class="muted">${esc(record.comment)}</div>` : ""}</div>`).join("")}</div>`;
}

function reviewFormHtml(evidence) {
  return `<div class="detail-block">
    <div class="small" style="font-weight:600">人工审核</div>
    <textarea id="review-comment" aria-label="审核说明" placeholder="审核说明（可选）"></textarea>
    <div class="review-actions">
      <button class="btn small primary" data-review-status="confirmed">确认异常线索</button>
      <button class="btn small" data-review-status="excluded">正常 / 排除</button>
      <button class="btn small" data-review-status="undecided">暂不判断</button>
    </div>
    <div class="small muted" style="margin-top:6px">审核记录只追加保存，不覆盖历史；原始识别结果始终保留。</div>
  </div>`;
}

function bindReviewForm(root, evidence, refresh) {
  $$("[data-review-status]", root).forEach((button) => button.addEventListener("click", () => {
    const comment = $("#review-comment", root)?.value || "";
    withBusy(button, async () => {
      try {
        await api(`/evidences/${evidence.id}/review`, { method: "POST", body: { status: button.dataset.reviewStatus, comment: comment || null } });
        toast("审核结论已保存", "success");
        await refresh();
      } catch (error) { toast(error.message, "error"); }
    }, "保存中…");
  }));
}

/* ---------- 工作台（PRD 4.1） ---------- */
const WORKBENCH_STAT_LINKS = {
  project_total: "#/projects",
  project_active: "#/projects",
  task_completed: "#recent-tasks",
  evidence_total: "#/risks",
  evidence_pending_review: "#/risks?review_status=pending",
};

async function renderWorkbench() {
  const main = $("#main");
  main.innerHTML = skeletonPage(5);
  const data = await api("/workbench");
  const stats = data.stats || {};
  const risk = data.risk || {};

  const statMeta = {
    project_total: ["projects", ""],
    project_active: ["activity", ""],
    task_completed: ["check", "ok"],
    evidence_total: ["shield", "medium"],
    evidence_pending_review: ["clock", "high"],
  };
  const statCards = [
    ["project_total", "项目总数", stats.project_total, "#/projects"],
    ["project_active", "进行中项目", stats.project_active, "#/projects"],
    ["task_completed", "已完成分析任务", stats.task_completed, "#recent-tasks"],
    ["evidence_total", "异常证据总数", stats.evidence_total, "#/risks"],
    ["evidence_pending_review", "待审核证据", stats.evidence_pending_review, "#/risks?review_status=pending"],
  ].map(([key, label, value]) => {
    const [iconName, tone] = statMeta[key];
    return `<button type="button" class="stat clickable" data-stat="${key}" title="点击查看">
      <span class="stat-head"><span class="stat-icon ${tone}">${icon(iconName)}</span><span class="label">${esc(label)}</span></span>
      <span class="value">${num(value)}</span>
    </button>`;
  }).join("");

  const todoRows = (data.todos || []).map((todo) => `
    <tr>
      <td>${pill(todo.severity === "high" ? "high" : todo.severity === "medium" ? "medium" : "low", todo.type_label)}</td>
      <td><a href="#/projects/${esc(todo.project_id)}">${esc(todo.project_name)}</a></td>
      <td>${esc(todo.title)}<div class="small muted">${esc(todo.detail || "")}</div></td>
      <td class="opt">${fmtDate(todo.created_at)}</td>
      <td>${pill(todo.status, todo.status === "failed" ? "需处理" : "待处理")}</td>
      <td><a class="btn small" href="${esc(todo.link)}">去处理</a></td>
    </tr>`).join("");

  const projectRows = (data.recent_projects || []).map((project) => `
    <tr>
      <td><a href="#/projects/${esc(project.id)}"><strong>${esc(project.project_name)}</strong></a>
        <div class="small muted">${esc(project.project_code || "无项目编号")}</div></td>
      <td>${pill(project.status, project.status_label || PROJECT_STATUS[project.status] || project.status)}</td>
      <td class="opt">${num(project.company_count)}</td>
      <td class="opt">${fmtDate(project.last_analysis_at)}</td>
      <td>${num(project.pending_review_count)}</td>
      <td><a class="btn small" href="#/projects/${esc(project.id)}">进入</a></td>
    </tr>`).join("");

  const taskRows = (data.recent_tasks || []).map((task) => `
    <tr>
      <td>#${num(task.task_no)}</td>
      <td><a href="#/projects/${esc(task.project_id)}">${esc(task.project_name)}</a></td>
      <td>${num(task.company_count)}</td>
      <td>${fmtDate(task.created_at)}</td>
      <td class="opt">${fmtDate(task.finished_at)}</td>
      <td>${taskPill(task.status)}</td>
      <td class="mono small opt">${esc(task.algorithm_version || "—")}</td>
      <td class="small muted opt">${esc(task.rule_version_label || "—")}</td>
      <td><a class="btn small" href="${esc(task.link)}">查看</a></td>
    </tr>`).join("");

  const typeIcons = { text: "file", personnel: "user", price: "coins" };
  const typeCards = Object.entries(risk.by_type || {}).map(([key, value]) =>
    statCard(TYPE[key] || key, num(value), typeIcons[key] || "layers", "medium")).join("");
  const severityTone = { high: "high", medium: "medium", low: "", none: "" };
  const severityCards = Object.entries(risk.by_severity || {}).map(([key, value]) =>
    statCard(`${RISK[key] || key}风险`, num(value), key === "none" ? "shield" : "alert", severityTone[key] || "")).join("");
  const reviewIcons = { pending: "clock", confirmed: "alert", excluded: "check", undecided: "info" };
  const reviewTones = { pending: "medium", confirmed: "high", excluded: "ok", undecided: "medium" };
  const reviewCards = Object.entries(risk.by_review_status || {}).map(([key, value]) =>
    statCard(REVIEW[key] || key, num(value), reviewIcons[key] || "clock", reviewTones[key] || "")).join("");

  main.innerHTML = `
    <div class="page-head">
      <div>
        <h1>工作台</h1>
        <div class="sub">全局项目统计、待办事项与风险概况</div>
      </div>
      <button class="btn" id="refresh-workbench">${icon("refresh")}刷新</button>
    </div>

    <div class="grid cols-5">${statCards}</div>

    <div class="card">
      <h2>待办事项</h2>
      ${todoRows ? `<table><thead><tr><th>类型</th><th>所属项目</th><th>事项</th><th class="opt">创建时间</th><th>状态</th><th>操作</th></tr></thead><tbody>${todoRows}</tbody></table>`
        : `<div class="empty">暂无待办事项。</div>`}
    </div>

    <div class="card">
      <h2>最近项目</h2>
      ${projectRows ? `<table><thead><tr><th>项目</th><th>状态</th><th class="opt">企业数</th><th class="opt">最近分析</th><th>待审核</th><th>操作</th></tr></thead><tbody>${projectRows}</tbody></table>`
        : `<div class="empty">暂无项目。</div>`}
    </div>

    <div class="card" id="recent-tasks">
      <h2>最近分析任务</h2>
      ${taskRows ? `<table><thead><tr><th>任务</th><th>项目</th><th>企业数</th><th>创建</th><th class="opt">完成</th><th>状态</th><th class="opt">算法版本</th><th class="opt">规则版本</th><th>操作</th></tr></thead><tbody>${taskRows}</tbody></table>`
        : `<div class="empty">暂无分析任务。</div>`}
    </div>

    <div class="card">
      <h2>风险统计</h2>
      <div class="grid cols-3" style="margin-bottom:14px">${typeCards}</div>
      <div class="grid cols-4" style="margin-bottom:14px">${severityCards}</div>
      <div class="grid cols-4">${reviewCards}</div>
    </div>`;

  $$("[data-stat]").forEach((card) => card.addEventListener("click", () => {
    const link = WORKBENCH_STAT_LINKS[card.dataset.stat];
    if (!link) return;
    if (link.startsWith("#/")) { location.hash = link; return; }
    document.querySelector(link)?.scrollIntoView({ behavior: "smooth" });
  }));
  const refreshBtn = $("#refresh-workbench");
  refreshBtn.addEventListener("click", () => withBusy(refreshBtn, renderWorkbench, "刷新中…"));
}

/* ---------- 风险管理（PRD 4.11） ---------- */
function riskQueryString() {
  const params = new URLSearchParams();
  Object.entries(riskListState).forEach(([key, value]) => {
    if (value !== "" && value !== null && value !== undefined && key !== "page" && key !== "page_size") {
      params.set(key, value);
    }
  });
  params.set("page", riskListState.page || 1);
  params.set("page_size", riskListState.page_size || 20);
  return params.toString();
}

async function renderRisks(params) {
  const main = $("#main");
  /* 工作台统计卡片跳进来时带上筛选条件 */
  if (params) {
    ["project_id", "company_id", "evidence_type", "severity", "review_status", "reviewer_id", "date_from", "date_to"]
      .forEach((key) => { if (params.get(key)) riskListState[key] = params.get(key); });
  }
  main.innerHTML = skeletonPage(4);
  const [filters, data] = await Promise.all([
    api("/risks/filters"),
    api(`/risks?${riskQueryString()}`),
  ]);

  const companyOptions = filters.companies
    .filter((company) => !riskListState.project_id || company.project_id === riskListState.project_id);
  const rows = (data.items || []).map((item) => `
    <tr>
      <td><input type="checkbox" data-evidence-check="${esc(item.id)}" aria-label="选择证据 ${esc(item.evidence_code)}"></td>
      <td class="mono small nowrap">${esc(item.evidence_code)}</td>
      <td class="risk-project"><a href="#/projects/${esc(item.project_id)}">${esc(item.project_name)}</a></td>
      <td class="opt">#${num(item.task_no)}</td>
      <td>${pill(item.evidence_type, item.evidence_type_label)}</td>
      <td class="risk-companies">${esc((item.companies || []).join("、"))}</td>
      <td>${riskPill(item.severity)}</td>
      <td class="opt">${fmtDate(item.created_at)}</td>
      <td>${pill(item.review_status, item.review_status_label)}</td>
      <td class="opt">${esc(item.reviewer_name || "—")}</td>
      <td class="nowrap">
        <button class="btn small" data-risk-detail="${esc(item.id)}">详情</button>
        <a class="btn small" href="${esc(item.viewer_link)}">三栏比对</a>
      </td>
    </tr>`).join("");

  const totalPages = Math.max(1, Math.ceil((num(data.total)) / (riskListState.page_size || 20)));

  main.innerHTML = `
    <div class="page-head">
      <div>
        <h1>风险管理</h1>
        <div class="sub">汇总全部项目的异常证据，共 ${num(data.total)} 条 · 系统输出的是疑似异常线索，不构成违规认定</div>
      </div>
    </div>

    <div class="card">
      <div class="row">
        <select id="risk-project" aria-label="按项目筛选">
          <option value="">全部项目</option>
          ${filters.projects.map((project) => `<option value="${esc(project.id)}" ${riskListState.project_id === project.id ? "selected" : ""}>${esc(project.project_name)}</option>`).join("")}
        </select>
        <select id="risk-company" aria-label="按企业筛选">
          <option value="">全部企业</option>
          ${companyOptions.map((company) => `<option value="${esc(company.company_id)}" ${riskListState.company_id === company.company_id ? "selected" : ""}>${esc(company.company_name)}</option>`).join("")}
        </select>
        <select id="risk-type" aria-label="按证据类型筛选">
          <option value="">全部类型</option>
          ${Object.entries(TYPE).map(([value, label]) => `<option value="${value}" ${riskListState.evidence_type === value ? "selected" : ""}>${label}</option>`).join("")}
        </select>
        <select id="risk-severity" aria-label="按风险等级筛选">
          <option value="">全部等级</option>
          ${Object.entries(RISK).map(([value, label]) => `<option value="${value}" ${riskListState.severity === value ? "selected" : ""}>${label}</option>`).join("")}
        </select>
        <select id="risk-review" aria-label="按审核状态筛选">
          <option value="">全部审核状态</option>
          ${Object.entries(REVIEW).map(([value, label]) => `<option value="${value}" ${riskListState.review_status === value ? "selected" : ""}>${label}</option>`).join("")}
        </select>
        <select id="risk-reviewer" aria-label="按审核人筛选">
          <option value="">全部审核人</option>
          ${(filters.reviewers || []).map((reviewer) => `<option value="${esc(reviewer.id)}" ${riskListState.reviewer_id === reviewer.id ? "selected" : ""}>${esc(reviewer.name)}</option>`).join("")}
        </select>
        <input type="date" id="risk-from" aria-label="发现时间起" value="${esc(riskListState.date_from)}" title="发现时间起">
        <input type="date" id="risk-to" aria-label="发现时间止" value="${esc(riskListState.date_to)}" title="发现时间止">
        <button class="btn primary" id="risk-query">${icon("filter")}查询</button>
        <button class="btn" id="risk-reset">${icon("refresh")}重置</button>
      </div>
    </div>

    <div class="card">
      <div class="row" style="margin-bottom:12px">
        <label class="small muted"><input type="checkbox" id="risk-select-all"> 全选本页</label>
        <span class="small muted" id="risk-selected">已选 0 条</span>
        <span class="spacer"></span>
        <button class="btn small primary" id="risk-batch" disabled>批量标记为待进一步核查</button>
        <button class="btn small" disabled title="V1.0 不支持批量确认或批量排除">批量确认（不支持）</button>
        <button class="btn small" disabled title="V1.0 不支持批量确认或批量排除">批量排除（不支持）</button>
      </div>
      ${rows ? `<div class="table-scroll"><table><thead><tr>
          <th style="width:32px"></th><th>证据编号</th><th>所属项目</th><th class="opt">任务</th><th>证据类型</th>
          <th>涉及企业</th><th>风险等级</th><th class="opt">发现时间</th><th>审核状态</th><th class="opt">审核人</th><th>操作</th>
        </tr></thead><tbody>${rows}</tbody></table></div>`
        : `<div class="empty">没有符合条件的证据。</div>`}
      <div class="row" style="margin-top:12px">
        <span class="small muted">第 ${num(data.page)} / ${totalPages} 页，共 ${num(data.total)} 条</span>
        <span class="spacer"></span>
        <button class="btn small" id="risk-prev" ${num(data.page) <= 1 ? "disabled" : ""}>上一页</button>
        <button class="btn small" id="risk-next" ${num(data.page) >= totalPages ? "disabled" : ""}>下一页</button>
      </div>
    </div>`;

  const rerender = () => renderRisks(null);
  const bind = (selector, key) => {
    const element = $(selector);
    if (element) element.addEventListener("change", () => {
      riskListState[key] = element.value;
      /* 切换项目后，原先选中的企业可能不属于新项目，一并清空 */
      if (key === "project_id") riskListState.company_id = "";
      riskListState.page = 1;
      rerender();
    });
  };
  bind("#risk-project", "project_id");
  bind("#risk-company", "company_id");
  bind("#risk-type", "evidence_type");
  bind("#risk-severity", "severity");
  bind("#risk-review", "review_status");
  bind("#risk-reviewer", "reviewer_id");
  $("#risk-from")?.addEventListener("change", (event) => { riskListState.date_from = event.target.value; riskListState.page = 1; rerender(); });
  $("#risk-to")?.addEventListener("change", (event) => { riskListState.date_to = event.target.value; riskListState.page = 1; rerender(); });
  $("#risk-query")?.addEventListener("click", rerender);
  $("#risk-reset")?.addEventListener("click", () => {
    riskListState = { project_id: "", company_id: "", evidence_type: "", severity: "", review_status: "", reviewer_id: "", date_from: "", date_to: "", page: 1, page_size: 20 };
    rerender();
  });

  $("#risk-prev")?.addEventListener("click", () => { riskListState.page = Math.max(1, riskListState.page - 1); rerender(); });
  $("#risk-next")?.addEventListener("click", () => { riskListState.page = riskListState.page + 1; rerender(); });

  const updateSelection = () => {
    const checked = $$("[data-evidence-check]").filter((box) => box.checked);
    $("#risk-selected").textContent = `已选 ${checked.length} 条`;
    $("#risk-batch").disabled = checked.length === 0;
  };
  $$("[data-evidence-check]").forEach((box) => box.addEventListener("change", updateSelection));
  $("#risk-select-all")?.addEventListener("change", (event) => {
    $$("[data-evidence-check]").forEach((box) => { box.checked = event.target.checked; });
    updateSelection();
  });

  const batchBtn = $("#risk-batch");
  batchBtn?.addEventListener("click", () => {
    const ids = $$("[data-evidence-check]").filter((box) => box.checked).map((box) => box.dataset.evidenceCheck);
    if (!ids.length) return;
    if (!confirm(`确认把选中的 ${ids.length} 条证据标记为「待进一步核查」？`)) return;
    withBusy(batchBtn, async () => {
      try {
        const result = await api("/risks/batch-review", { method: "POST", body: { evidence_ids: ids, status: "undecided" } });
        toast(`已处理 ${result.updated} 条证据`, "success");
        rerender();
      } catch (error) { toast(error.message, "error"); }
    }, "处理中…");
  });

  $$("[data-risk-detail]").forEach((button) => button.addEventListener("click", () => openRiskDetail(button.dataset.riskDetail)));
}

async function openRiskDetail(evidenceId) {
  let detail;
  try {
    detail = await api(`/risks/${evidenceId}`);
  } catch (error) { toast(error.message, "error"); return; }
  const files = (detail.files || []).map((file) => `
    <div class="small">${esc(file.company_name || "")} · ${esc(file.file_name || "")} 第 ${num(file.page_index)} 页</div>`).join("") || "—";
  const modal = openModal({
    title: `风险详情 · ${detail.evidence_code}`,
    wide: true,
    body: `
      <dl class="kv">
        <dt>证据编号</dt><dd class="mono">${esc(detail.evidence_code)}</dd>
        <dt>所属项目</dt><dd><a href="#/projects/${esc(detail.project_id)}">${esc(detail.project_name)}</a></dd>
        <dt>分析任务</dt><dd>#${num(detail.task_no)} · ${esc(TASK_STATUS[detail.task_status] || detail.task_status || "—")}</dd>
        <dt>证据类型</dt><dd>${esc(detail.evidence_type_label || detail.evidence_type)} · ${esc(detail.evidence_subtype || "")}</dd>
        <dt>涉及企业</dt><dd>${esc((detail.companies || []).join("、") || "—")}</dd>
        <dt>风险等级</dt><dd>${riskPill(detail.severity)}</dd>
        <dt>发现时间</dt><dd>${fmtDate(detail.created_at)}</dd>
        <dt>审核状态</dt><dd>${pill(detail.review_status, REVIEW[detail.review_status])}</dd>
        <dt>审核人</dt><dd>${esc(detail.reviewer_name || "—")}</dd>
        <dt>相关文件</dt><dd>${files}</dd>
        <dt>原始证据</dt><dd>${esc(detail.summary || "")}</dd>
      </dl>
      ${detail.source_a ? `<div class="detail-block"><div class="small" style="font-weight:600">原始文本</div>
        <div class="viewer-text">${esc(detail.source_a.snippet || "")}</div></div>` : ""}
      ${renderHistory(detail.review_records || [])}
      <div class="review-actions">
        <a class="btn small primary" href="${esc(detail.viewer_link)}">进入三栏比对</a>
      </div>`,
  });
  return modal;
}

/* ---------- 系统管理（PRD 4.13） ---------- */
const SYSTEM_TABS = [["rules", "检测规则"], ["logs", "操作日志"], ["users", "用户管理"]];

async function renderSystem(params) {
  const main = $("#main");
  const isAdmin = state.user && state.user.role === "admin";

  main.innerHTML = `
    <div class="page-head">
      <div>
        <h1>系统管理</h1>
        <div class="sub">检测规则版本、操作日志与用户账号</div>
      </div>
    </div>
    <div class="tabs" role="tablist" aria-label="系统管理页面">
      ${SYSTEM_TABS.map(([key, label]) => `<button type="button" class="tab ${key === systemTab ? "active" : ""}" role="tab" aria-selected="${key === systemTab}" data-system-tab="${key}">${label}</button>`).join("")}
    </div>
    <div id="system-content">${skeletonBlock(6)}</div>`;

  $$("[data-system-tab]").forEach((link) => link.addEventListener("click", () => {
    systemTab = link.dataset.systemTab;
    renderSystem(null);
  }));

  const container = $("#system-content");
  if (systemTab === "rules") await renderSystemRules(container);
  else if (systemTab === "logs") await renderSystemLogs(container, isAdmin);
  else await renderSystemUsers(container, isAdmin);
}

function ruleParamLabel(key) {
  return {
    text_similarity: "文本相似度阈值",
    text_min_match_chars: "最小匹配字数",
    text_min_block_chars: "最小文本块长度",
    text_chunk_target_min: "文本块目标长度下限",
    text_chunk_target_max: "文本块目标长度上限",
    text_chunk_max_chars: "文本块最大长度",
    text_chunk_min_chars: "文本块最小有效长度",
    text_shingle_size: "Shingle 长度",
    text_minhash_num_perm: "MinHash 签名数",
    text_lsh_threshold: "LSH 召回阈值",
    max_candidate_pairs: "候选召回上限",
    max_matches_per_pair: "每组合最大匹配数",
    tender_exclusion_ratio: "招标公共内容排除阈值",
    risk_text_high_count: "文本高风险条数阈值",
    risk_text_high_similarity: "文本高风险相似度阈值",
    risk_text_medium_count: "文本中风险条数阈值",
    risk_text_medium_similarity: "文本中风险相似度阈值",
    similar_name_ratio: "近名相似度阈值",
    name_match_modes: "姓名匹配方式",
    key_positions: "关键岗位范围",
    risk_personnel_high_count: "人员高风险人数阈值",
    risk_personnel_medium_count: "人员中风险人数阈值",
    price_cv: "变异系数阈值",
    price_close_deviation: "报价偏差率阈值",
    price_sequence_tolerance: "等差数列误差阈值",
    special_tail: "特殊尾数取值",
    tail_concentration_min_count: "尾数集中最少家数",
    amount_case_consistency: "大小写金额一致性规则",
    id_card_match: "身份证号匹配规则",
    certificate_match: "证书编号匹配规则",
    title_filter: "常见称谓过滤规则",
  }[key] || key;
}

function ruleParamValue(value) {
  if (typeof value === "boolean") return value ? "启用" : "关闭";
  if (Array.isArray(value)) return value.join("、");
  if (typeof value === "number") return String(value);
  return String(value ?? "—");
}

async function renderSystemRules(container) {
  const data = await api("/system/rules");
  const versionsByDimension = {};
  (data.versions || []).forEach((version) => {
    (versionsByDimension[version.dimension] = versionsByDimension[version.dimension] || []).push(version);
  });

  const cards = (data.dimensions || []).map((dimension) => {
    const active = dimension.active;
    const history = versionsByDimension[dimension.dimension] || [];
    const paramRows = active
      ? Object.entries(active.params || {}).map(([key, value]) => `
          <dt>${esc(ruleParamLabel(key))}</dt><dd>${esc(ruleParamValue(value))}</dd>`).join("")
      : "";
    const historyRows = history.map((version) => {
      const changed = (version.changed || []).map((item) => `
        <tr><td>${esc(ruleParamLabel(item.key))}</td>
          <td class="mono small">${esc(ruleParamValue(item.before))}</td>
          <td class="mono small">${esc(ruleParamValue(item.after))}</td></tr>`).join("");
      return `<tr>
        <td>#${num(version.version_no)}</td>
        <td>${pill(version.status, version.status_label)}</td>
        <td>${esc(version.created_by_name || "—")}</td>
        <td>${fmtDate(version.created_at)}</td>
        <td>${esc(version.change_reason || "—")}</td>
        <td><details><summary class="small">查看差异</summary>
          ${changed ? `<table><thead><tr><th>参数</th><th>修改前</th><th>修改后</th></tr></thead><tbody>${changed}</tbody></table>` : `<div class="small muted">无参数变更</div>`}
        </details></td>
      </tr>`;
    }).join("");
    return `<div class="card">
      <div class="row">
        <h2 style="margin:0">${esc(dimension.label)}</h2>
        <span class="spacer"></span>
        ${active ? `<span class="small muted">当前生效 v${num(active.version_no)} · ${fmtDate(active.activated_at)} · ${esc(active.created_by_name || "系统")}</span>` : `<span class="small muted">尚未创建规则版本</span>`}
        <button class="btn small primary" data-new-rule="${esc(dimension.dimension)}">${icon("plus")}新建版本</button>
      </div>
      ${active ? `<dl class="kv wide" style="margin-top:12px">${paramRows}</dl>` : ""}
      ${(active && (active.not_enforced_params || []).length) ? `<div class="warn-box small">以下参数在 V1.0 仅记录与展示，尚未接入检测引擎：${esc((active.not_enforced_params || []).map(ruleParamLabel).join("、"))}</div>` : ""}
      <details style="margin-top:12px"><summary class="small">查看历史版本（${history.length}）</summary>
        ${historyRows ? `<table><thead><tr><th>版本</th><th>状态</th><th>修改人</th><th>修改时间</th><th>修改原因</th><th>变更前后</th></tr></thead><tbody>${historyRows}</tbody></table>` : `<div class="small muted">暂无历史版本</div>`}
      </details>
    </div>`;
  }).join("");

  container.innerHTML = cards || `<div class="empty">暂无检测规则。</div>`;

  $$("[data-new-rule]").forEach((button) => button.addEventListener("click", () => {
    const dimension = (data.dimensions || []).find((item) => item.dimension === button.dataset.newRule);
    openRuleVersionModal(dimension);
  }));
}

function openRuleVersionModal(dimension) {
  if (!dimension) return;
  const active = dimension.active;
  const params = active ? active.params : dimension.default_params;
  const fields = Object.entries(params || {}).map(([key, value]) => {
    const label = ruleParamLabel(key);
    if (typeof value === "boolean") {
      return `<label class="small">${esc(label)}<input type="checkbox" name="param" value="${esc(key)}" ${value ? "checked" : ""}></label>`;
    }
    if (Array.isArray(value)) {
      return `<label class="small">${esc(label)}（逗号分隔）<input type="text" name="param" value="${esc(value.join(","))}" data-kind="list"></label>`;
    }
    if (typeof value === "number") {
      return `<label class="small">${esc(label)}<input type="number" step="any" name="param" value="${esc(value)}" data-kind="number"></label>`;
    }
    return `<label class="small">${esc(label)}<input type="text" name="param" value="${esc(value)}"></label>`;
  }).join("");

  const modal = openModal({
    title: `新建规则版本 · ${dimension.label}`,
    wide: true,
    body: `
      <form id="rule-version-form">
        <div class="form-row">${fields}</div>
        <label class="small">修改原因（必填）<textarea id="rule-reason" placeholder="说明本次调整的依据"></textarea></label>
        <label class="small"><input type="checkbox" id="rule-confirm"> 我已确认本次规则变更内容</label>
      </form>`,
    footer: `
      <button class="btn" data-close>取消</button>
      <button class="btn" id="rule-save-draft">保存为待生效</button>
      <button class="btn primary" id="rule-save-active">保存并生效</button>`,
  });

  const collect = () => {
    const result = {};
    $$("[name=param]", modal.root).forEach((input) => {
      const key = input.value;
      if (input.type === "checkbox") { result[key] = input.checked; return; }
      if (input.dataset.kind === "number") { result[key] = Number(input.value); return; }
      if (input.dataset.kind === "list") { result[key] = input.value.split(",").map((item) => item.trim()).filter(Boolean); return; }
      result[key] = input.value;
    });
    return result;
  };
  const save = async (activate) => {
    const reason = $("#rule-reason", modal.root)?.value || "";
    if (!reason.trim()) { toast("请填写修改原因", "error"); return; }
    if (activate && !$("#rule-confirm", modal.root)?.checked) { toast("生效需要勾选生效确认", "error"); return; }
    try {
      await api("/system/rules/versions", {
        method: "POST",
        body: { dimension: dimension.dimension, params: collect(), change_reason: reason, activate, confirmed: activate },
      });
      toast(activate ? "规则版本已生效" : "规则版本已保存为待生效", "success");
      modal.close();
      renderSystem(null);
    } catch (error) { toast(error.message, "error"); }
  };
  const draftBtn = $("#rule-save-draft", modal.root);
  const activeBtn = $("#rule-save-active", modal.root);
  draftBtn?.addEventListener("click", () => withBusy(draftBtn, () => save(false), "保存中…"));
  activeBtn?.addEventListener("click", () => withBusy(activeBtn, () => save(true), "保存中…"));
}

async function renderSystemLogs(container, isAdmin) {
  if (!isAdmin) {
    container.innerHTML = `<div class="info-box">操作日志需要系统管理员权限。</div>`;
    return;
  }
  container.innerHTML = skeletonBlock(6);
  const params = new URLSearchParams();
  Object.entries(logFilterState).forEach(([key, value]) => { if (value) params.set(key, value); });
  params.set("page_size", "50");
  const [options, data] = await Promise.all([
    api("/system/log-options"),
    api(`/system/logs?${params.toString()}`),
  ]);
  const rows = (data.items || []).map((item) => `
    <tr>
      <td>${esc(item.actor_name || "—")}</td>
      <td>${fmtDate(item.created_at)}</td>
      <td>${esc(item.action_label || item.action)}</td>
      <td>${esc(item.entity_label || item.entity_type)}${item.entity_name ? ` · ${esc(item.entity_name)}` : ""}</td>
      <td>${pill(item.result, item.result_label)}</td>
      <td>${esc(item.source_label || item.source)}</td>
      <td><details><summary class="small">变更前后</summary>
        <div class="rule-diff mono small">修改前：${esc(JSON.stringify(item.before || {}))}<br>修改后：${esc(JSON.stringify(item.after || {}))}</div>
      </details></td>
    </tr>`).join("");
  container.innerHTML = `
    <div class="card">
      <div class="row" style="margin-bottom:12px">
        <select id="log-action" aria-label="按操作类型筛选">
          <option value="">全部操作</option>
          ${(options.actions || []).map((action) => `<option value="${esc(action.value)}" ${logFilterState.action === action.value ? "selected" : ""}>${esc(action.label)}</option>`).join("")}
        </select>
        <select id="log-result" aria-label="按操作结果筛选">
          <option value="">全部结果</option>
          ${(options.results || []).map((item) => `<option value="${esc(item.value)}" ${logFilterState.result === item.value ? "selected" : ""}>${esc(item.label)}</option>`).join("")}
        </select>
        <select id="log-source" aria-label="按操作来源筛选">
          <option value="">全部来源</option>
          ${(options.sources || []).map((item) => `<option value="${esc(item.value)}" ${logFilterState.source === item.value ? "selected" : ""}>${esc(item.label)}</option>`).join("")}
        </select>
        <select id="log-actor" aria-label="按操作人筛选">
          <option value="">全部操作人</option>
          ${(options.actors || []).map((actor) => `<option value="${esc(actor.id)}" ${logFilterState.actor_id === actor.id ? "selected" : ""}>${esc(actor.name)}</option>`).join("")}
        </select>
        <input type="date" id="log-from" aria-label="开始时间" value="${esc(logFilterState.date_from)}" title="开始时间">
        <input type="date" id="log-to" aria-label="结束时间" value="${esc(logFilterState.date_to)}" title="结束时间">
        <button class="btn primary" id="log-query">${icon("search")}查询</button>
        <button class="btn" id="log-reset">${icon("refresh")}重置</button>
      </div>
      ${rows ? `<div class="table-scroll"><table><thead><tr>
        <th>操作人</th><th>操作时间</th><th>操作类型</th><th>操作对象</th><th>操作结果</th><th>操作来源</th><th>变更前后</th>
      </tr></thead><tbody>${rows}</tbody></table></div>` : `<div class="empty">暂无操作日志。</div>`}
      <div class="row" style="margin-top:12px"><span class="small muted">共 ${num(data.total)} 条</span></div>
    </div>`;

  const bind = (selector, key) => {
    const element = $(selector);
    if (element) element.addEventListener("change", () => { logFilterState[key] = element.value; });
  };
  bind("#log-action", "action");
  bind("#log-result", "result");
  bind("#log-source", "source");
  bind("#log-actor", "actor_id");
  bind("#log-from", "date_from");
  bind("#log-to", "date_to");
  const logQueryBtn = $("#log-query");
  logQueryBtn.addEventListener("click", () => withBusy(logQueryBtn, () => renderSystemLogs(container, isAdmin), "查询中…"));
  $("#log-reset").addEventListener("click", () => {
    logFilterState = { action: "", result: "", source: "", actor_id: "", date_from: "", date_to: "" };
    renderSystemLogs(container, isAdmin);
  });
}

async function renderSystemUsers(container, isAdmin) {
  if (!isAdmin) {
    container.innerHTML = `<div class="info-box">用户管理需要系统管理员权限。</div>`;
    return;
  }
  container.innerHTML = skeletonBlock(5);
  const data = await api("/system/users");
  const rows = (data.items || []).map((user) => `
    <tr>
      <td>${esc(user.username)}</td>
      <td>${esc(user.display_name || "—")}</td>
      <td>${pill(user.role, user.role_label)}</td>
      <td>${pill(user.is_active ? "ok" : "bad", user.is_active ? "启用" : "禁用")}</td>
      <td>${fmtDate(user.created_at)}</td>
      <td>
        <button class="btn small" data-toggle-user="${esc(user.id)}" data-active="${user.is_active}">${user.is_active ? "禁用" : "启用"}</button>
        <button class="btn small" data-reset-user="${esc(user.id)}" data-name="${esc(user.username)}">重置密码</button>
      </td>
    </tr>`).join("");
  container.innerHTML = `
    <div class="card">
      <div class="row" style="margin-bottom:12px">
        <h2 style="margin:0">用户账号</h2>
        <span class="spacer"></span>
        <button class="btn primary" id="create-user">${icon("plus")}新增用户</button>
      </div>
      ${rows ? `<table><thead><tr><th>用户名</th><th>显示名</th><th>角色</th><th>状态</th><th>创建时间</th><th>操作</th></tr></thead><tbody>${rows}</tbody></table>`
        : `<div class="empty">暂无用户。</div>`}
    </div>`;

  $("#create-user").addEventListener("click", () => {
    const modal = openModal({
      title: "新增用户",
      body: `<form id="create-user-form">
        <label class="small">用户名<input type="text" id="new-username" required></label>
        <label class="small">显示名<input type="text" id="new-display"></label>
        <label class="small">初始密码<input type="password" id="new-password" required></label>
        <label class="small">角色<select id="new-role">
          ${Object.entries(USER_ROLE).map(([value, label]) => `<option value="${value}">${label}</option>`).join("")}
        </select></label>
      </form>`,
      footer: `<button class="btn" data-close>取消</button><button class="btn primary" id="save-user">创建</button>`,
    });
    const saveBtn = $("#save-user", modal.root);
    saveBtn.addEventListener("click", () => withBusy(saveBtn, async () => {
      try {
        await api("/system/users", {
          method: "POST",
          body: {
            username: $("#new-username", modal.root).value,
            display_name: $("#new-display", modal.root).value,
            password: $("#new-password", modal.root).value,
            role: $("#new-role", modal.root).value,
          },
        });
        toast("用户已创建", "success");
        modal.close();
        renderSystemUsers(container, isAdmin);
      } catch (error) { toast(error.message, "error"); }
    }, "创建中…"));
  });

  $$("[data-toggle-user]").forEach((button) => button.addEventListener("click", () => {
    const isActive = button.dataset.active === "true";
    withBusy(button, async () => {
      try {
        await api(`/system/users/${button.dataset.toggleUser}`, { method: "PATCH", body: { is_active: !isActive } });
        toast(isActive ? "用户已禁用" : "用户已启用", "success");
        renderSystemUsers(container, isAdmin);
      } catch (error) { toast(error.message, "error"); }
    }, "处理中…");
  }));

  $$("[data-reset-user]").forEach((button) => button.addEventListener("click", () => {
    const modal = openModal({
      title: `重置密码 · ${button.dataset.name}`,
      body: `<label class="small">新密码<input type="password" id="reset-password" autocomplete="new-password" required></label>`,
      footer: `<button class="btn" data-close>取消</button><button class="btn primary" id="save-password">重置</button>`,
    });
    const saveBtn = $("#save-password", modal.root);
    saveBtn.addEventListener("click", () => withBusy(saveBtn, async () => {
      try {
        await api(`/system/users/${button.dataset.resetUser}/password`, {
          method: "POST",
          body: { password: $("#reset-password", modal.root).value },
        });
        toast("密码已重置", "success");
        modal.close();
      } catch (error) { toast(error.message, "error"); }
    }, "重置中…"));
  }));
}

/* ---------- 渲染后增强：表头作用域 + 移动端表格横向滚动 ---------- */
function enhanceRendered() {
  $$("#main th:not([scope])").forEach((th) => th.setAttribute("scope", "col"));
  $$("#main .card > table").forEach((table) => {
    if (table.parentElement && table.parentElement.classList.contains("table-scroll")) return;
    const wrap = document.createElement("div");
    wrap.className = "table-scroll";
    table.parentNode.insertBefore(wrap, table);
    wrap.appendChild(table);
  });
  /* 小屏标签栏横向滚动时，让当前标签保持在可视范围内 */
  $$("#main .tabs").forEach((tabs) => {
    const active = tabs.querySelector(".active");
    if (!active) return;
    if (active.offsetLeft < tabs.scrollLeft || active.offsetLeft + active.clientWidth > tabs.scrollLeft + tabs.clientWidth) {
      tabs.scrollLeft = Math.max(0, active.offsetLeft - 16);
    }
  });
}

/* ---------- boot ---------- */
$("#login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.target;
  const button = $("button[type=submit]", form);
  const errorBox = $("#login-error");
  if (errorBox) { errorBox.hidden = true; errorBox.textContent = ""; }
  if (!form.reportValidity()) return;
  await withBusy(button, async () => {
    try {
      const data = Object.fromEntries(new FormData(form).entries());
      const result = await api("/auth/login", { method: "POST", body: data });
      state.token = result.access_token;
      state.user = result.user;
      localStorage.setItem("bid_token", state.token);
      localStorage.setItem("bid_user", JSON.stringify(state.user));
      showApp();
      if (!location.hash) location.hash = "#/workbench";
      await route();
    } catch (error) {
      if (errorBox) {
        errorBox.hidden = false;
        errorBox.textContent = error.message;
        errorBox.setAttribute("tabindex", "-1");
        errorBox.focus();
      }
      toast(error.message, "error");
    }
  }, "登录中…");
});

/* 密码显示 / 隐藏 */
const passwordToggle = $("#password-toggle");
passwordToggle?.addEventListener("click", () => {
  const input = $("#login-password");
  if (!input) return;
  const show = input.type === "password";
  input.type = show ? "text" : "password";
  passwordToggle.setAttribute("aria-pressed", String(show));
  passwordToggle.setAttribute("aria-label", show ? "隐藏密码" : "显示密码");
  passwordToggle.innerHTML = icon(show ? "eye-off" : "eye");
});

/* 移动端导航抽屉 */
$("#menu-btn")?.addEventListener("click", () => {
  const sidebar = $("#sidebar");
  setDrawer(!(sidebar && sidebar.classList.contains("open")));
});
$("#drawer-scrim")?.addEventListener("click", () => setDrawer(false));
$("#drawer-close")?.addEventListener("click", () => setDrawer(false));
$$(".side-nav a[data-nav]").forEach((link) => link.addEventListener("click", () => setDrawer(false)));
document.addEventListener("keydown", (event) => {
  const sidebar = $("#sidebar");
  if (!sidebar || !sidebar.classList.contains("open")) return;
  if (event.key === "Escape") { setDrawer(false); return; }
  if (event.key !== "Tab") return;
  /* 抽屉展开时把焦点限制在导航内部，避免 Tab 逃逸到被遮挡的页面内容 */
  const items = $$("a[href], button:not([disabled])", sidebar).filter((el) => el.offsetWidth || el.offsetHeight);
  if (!items.length) return;
  const first = items[0];
  const last = items[items.length - 1];
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
});
window.addEventListener("resize", () => {
  if (window.innerWidth > 1024) setDrawer(false);
});

$("#logout-btn")?.addEventListener("click", logout);
window.addEventListener("hashchange", route);

const mainEl = $("#main");
if (mainEl && typeof MutationObserver !== "undefined") {
  new MutationObserver(enhanceRendered).observe(mainEl, { childList: true, subtree: true });
  enhanceRendered();
}

(async function boot() {
  if (!state.token) { showLogin(); return; }
  try {
    const me = await api("/auth/me");
    state.user = me;
    localStorage.setItem("bid_user", JSON.stringify(me));
    showApp();
    await route();
  } catch (_) {
    logout();
  }
})();
