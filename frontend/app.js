/* 投标文件智能比对与风险分析系统 —— 轻量管理台（无构建步骤） */
const API = "/api/v1";

const state = {
  token: localStorage.getItem("bid_token") || "",
  user: JSON.parse(localStorage.getItem("bid_user") || "null"),
  lastProjectId: localStorage.getItem("bid_last_project") || "",
};

/* 项目状态文案：优先使用服务端下发的 status_label，这里仅作兜底 */
const PROJECT_STATUS = { pending: "准备中", ready: "待分析", analyzing: "分析中", analyzed: "分析完成", archived: "已归档" };
const LOGICAL_STATUS = { not_generated: "未生成", generated: "已生成", outdated: "待更新" };
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
let logFilterState = { action: "", result: "", source: "", actor_id: "", date_from: "", date_to: "", page: 1, page_size: 50 };
let selectedCompanyByProject = {};

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const num = (value, fallback = 0) => (value === null || value === undefined ? fallback : value);
const icon = (name, cls = "") => `<svg class="icon ${cls}" aria-hidden="true" focusable="false"><use href="#i-${esc(name)}"></use></svg>`;
const projectStatusLabel = (project) => project?.status_label || PROJECT_STATUS[project?.status] || project?.status || "—";

/* 错误页：给出明确出口，避免用户停在死胡同 */
function errorPageHtml(message, { back = "#/projects", backLabel = "返回项目列表" } = {}) {
  return `<div class="error-box" role="alert">
    <div style="margin-bottom:10px">${esc(message)}</div>
    <div class="row">
      <a class="btn" href="#/workbench">回到工作台</a>
      <a class="btn primary" href="${esc(back)}">${esc(backLabel)}</a>
    </div>
  </div>`;
}

/* 产品内确认弹窗：替代浏览器原生 confirm，可写清影响面 */
function confirmDialog({ title = "确认操作", message = "", confirmLabel = "确认", cancelLabel = "取消", danger = false, extraHtml = "" } = {}) {
  return new Promise((resolve) => {
    let settled = false;
    const modal = openModal({
      title,
      body: `<p style="margin:0 0 12px">${esc(message)}</p>${extraHtml}`,
      footer: `<button type="button" class="btn" data-close>${esc(cancelLabel)}</button>
        <button type="button" class="btn ${danger ? "danger" : "primary"}" id="confirm-ok">${esc(confirmLabel)}</button>`,
      /* 按 Esc / 点遮罩关闭时也要结束等待，否则调用方会一直挂着 */
      onClose: () => {
        if (!settled) { settled = true; resolve(false); }
      },
    });
    const finish = (value) => {
      if (settled) return;
      settled = true;
      resolve(value);
      modal.close();
    };
    $("#confirm-ok", modal.root)?.addEventListener("click", () => finish(true));
    $$("[data-close]", modal.root).forEach((button) => button.addEventListener("click", () => finish(false)));
    modal.root.querySelector(".modal-mask")?.addEventListener("click", (event) => {
      if (event.target.classList.contains("modal-mask")) finish(false);
    });
  });
}

/* 通过短期票据打开原始文件，避免把长期登录令牌放进 URL */
async function openFileTab(fileId, page) {
  if (!fileId) return;
  try {
    const { ticket } = await api(`/files/${fileId}/ticket`, { method: "POST" });
    const anchor = page ? `#page=${encodeURIComponent(page)}` : "";
    window.open(`${API}/files/${fileId}/content?ticket=${encodeURIComponent(ticket)}${anchor}`, "_blank", "noopener");
  } catch (error) {
    toast(error.message, "error");
  }
}

/* 带上传进度的请求：fetch 不支持上传进度，这里用 XHR */
function uploadWithProgress(path, files, onProgress) {
  return new Promise((resolve, reject) => {
    const form = new FormData();
    files.forEach((file) => form.append("files", file));
    const xhr = new XMLHttpRequest();
    xhr.open("POST", API + path);
    if (state.token) xhr.setRequestHeader("Authorization", `Bearer ${state.token}`);
    xhr.upload.addEventListener("progress", (event) => {
      if (event.lengthComputable && onProgress) onProgress(event.loaded / event.total);
    });
    xhr.addEventListener("load", () => {
      let data = null;
      try { data = JSON.parse(xhr.responseText || "null"); } catch (_) { /* ignore */ }
      if (xhr.status >= 200 && xhr.status < 300) {
        onProgress?.(1);
        resolve(data);
      } else if (xhr.status === 401) {
        logout();
        reject(new Error("登录已过期，请重新登录"));
      } else {
        const detail = data && (typeof data.detail === "string" ? data.detail : data.detail ? JSON.stringify(data.detail) : "");
        reject(new Error(detail || `上传失败 (${xhr.status})`));
      }
    });
    xhr.addEventListener("error", () => reject(new Error("网络异常，上传失败，请重试")));
    xhr.addEventListener("abort", () => reject(new Error("上传已取消")));
    xhr.send(form);
  });
}

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
  const coverage = entry?.coverage || {};
  if (coverage.status === "no_quote") return "未执行 · 未识别到投标总报价";
  if (coverage.expected > 0) {
    const missing = Math.max(0, num(coverage.expected) - num(coverage.recognized));
    if (missing > 0) return `部分覆盖 · ${missing} 家未识别到报价`;
  }
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

/* GET 去重：同一数据在短时间内被多个页面/组件重复请求时复用结果 */
const apiGetCache = new Map();
const API_GET_TTL = 1200;

async function api(path, options = {}) {
  const method = options.method || "GET";
  const headers = { ...(options.headers || {}) };
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  let body = options.body;
  if (body && !(body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(body);
  }
  const cacheable = method === "GET" && !options.blob;
  if (cacheable) {
    const cached = apiGetCache.get(path);
    if (cached && Date.now() - cached.at < API_GET_TTL) return cached.value;
  } else {
    /* 写操作后清空 GET 缓存，保证页面看到最新数据 */
    apiGetCache.clear();
  }
  const response = await fetch(API + path, { method, headers, body });
  if (response.status === 401) {
    logout();
    const expired = new Error("登录已过期，请重新登录");
    expired.status = 401;
    throw expired;
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
    const error = new Error(message);
    error.status = response.status;
    throw error;
  }
  let result;
  if (options.blob) result = await response.blob();
  else if (response.status === 204) result = null;
  else result = await response.json().catch(() => null);
  if (cacheable) apiGetCache.set(path, { value: result, at: Date.now() });
  return result;
}

/* ---------- 实时事件流（SSE） ---------- */
function closeEventStream() {
  if (eventSource) {
    eventSource.close();
    eventSource = null;
  }
}

/* 打开项目事件流：EventSource 无法设置请求头，改用限定到项目的短期票据 */
async function openEventStream(projectId, onSnapshot, onError) {
  closeEventStream();
  if (!state.token) return null;
  let ticket;
  try {
    const data = await api(`/projects/${projectId}/events/ticket`, { method: "POST" });
    ticket = data.ticket;
  } catch (error) {
    if (onError) onError(error);
    return null;
  }
  const url = `${API}/projects/${projectId}/events?ticket=${encodeURIComponent(ticket)}`;
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
  if (box) {
    /* 姓名与角色上下两行，便于和右侧退出按钮排在同一行 */
    box.innerHTML = state.user
      ? `<span class="user-meta"><span class="user-name">${esc(name)}</span><span class="user-sub">${esc(subline)}</span></span>`
      : "";
  }
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
  /* 清空主内容，避免退出后旧页面 DOM（含用户列表等）仍留在页面中 */
  const main = $("#main");
  if (main) main.innerHTML = "";
  clearTimeout(pollTimer);
  pollTimer = null;
  apiGetCache.clear();
  state.token = "";
  state.user = null;
  selectedCompanyByProject = {};
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
  if (!main) return;
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
      main.innerHTML = errorPageHtml("页面不存在。");
    }
  } catch (error) {
    console.error(error);
    main.innerHTML = errorPageHtml(error.message || "页面加载失败");
  }
  /* 路由切换后把焦点移到主内容，便于屏幕阅读器感知页面已更新 */
  requestAnimationFrame(() => main.focus({ preventScroll: true }));
}

/* ---------- modal ---------- */
function openModal({ title, body, footer = "", wide = false, onClose = null }) {
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
    if (typeof onClose === "function") onClose();
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
  const hasFilters = Boolean(projectListState.q || projectListState.status || projectListState.include_archived);
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
      <td>${pill(project.status, projectStatusLabel(project))}</td>
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
      <div class="filter-bar" style="margin-bottom:14px">
        <div class="filter-field filter-field--search">
          <label for="filter-q">搜索项目</label>
          <input type="search" id="filter-q" aria-label="搜索项目名称或编号" placeholder="输入项目名称或编号" value="${esc(projectListState.q)}" />
        </div>
        <div class="filter-field filter-field--select">
          <label for="filter-status">项目状态</label>
          <select id="filter-status" aria-label="按项目状态筛选">
            <option value="">全部状态</option>
            ${Object.entries(PROJECT_STATUS).map(([value, label]) => `<option value="${value}" ${projectListState.status === value ? "selected" : ""}>${label}</option>`).join("")}
          </select>
        </div>
        <div class="filter-field">
          <span class="filter-field-label">归档范围</span>
          <label class="switch filter-switch">
            <input type="checkbox" id="filter-archived" ${projectListState.include_archived ? "checked" : ""} />
            <span class="switch-track" aria-hidden="true"></span>
            <span class="switch-text">显示已归档</span>
          </label>
        </div>
        ${hasFilters ? `<div class="filter-actions"><button type="button" class="btn small" id="filter-reset">${icon("refresh")}重置筛选</button></div>` : ""}
      </div>
      ${rows ? `<table><thead><tr><th>项目名称</th><th>企业数</th><th>文件数</th><th>项目状态</th><th>最近分析</th><th>操作</th></tr></thead><tbody>${rows}</tbody></table>`
        : `<div class="empty">暂无项目，创建第一个项目开始整理标书资料。<button class="btn primary" id="empty-create-project">${icon("plus")}创建项目</button></div>`}
    </div>`;

  let searchTimer = null;
  $("#filter-q").addEventListener("input", (event) => {
    projectListState.q = event.target.value;
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => renderProjects(), 300);
  });
  $("#filter-q").addEventListener("keydown", (event) => {
    if (event.key === "Enter") { clearTimeout(searchTimer); renderProjects(); }
  });
  $("#filter-status").addEventListener("change", (event) => {
    projectListState.status = event.target.value;
    /* 选择「已归档」时自动勾选「显示已归档」，避免两个条件互相冲突得到空列表 */
    if (projectListState.status === "archived") projectListState.include_archived = true;
    renderProjects();
  });
  $("#filter-archived").addEventListener("change", (event) => {
    projectListState.include_archived = event.target.checked;
    /* 取消「显示已归档」时同步清除「已归档」状态筛选 */
    if (!projectListState.include_archived && projectListState.status === "archived") {
      projectListState.status = "";
    }
    renderProjects();
  });
  $("#filter-reset")?.addEventListener("click", () => {
    projectListState = { q: "", status: "", include_archived: false };
    renderProjects();
  });
  $("#create-project").addEventListener("click", openCreateProject);
  $("#empty-create-project")?.addEventListener("click", openCreateProject);

  $$("[data-archive]").forEach((btn) => btn.addEventListener("click", async () => {
    const ok = await confirmDialog({
      title: "归档项目",
      message: "归档后该项目默认不在项目列表中显示，历史分析记录仍会保留。",
      confirmLabel: "归档",
    });
    if (!ok) return;
    await withBusy(btn, async () => {
      try { await api(`/projects/${btn.dataset.archive}/archive`, { method: "POST" }); toast("项目已归档", "success"); renderProjects(); }
      catch (error) { toast(error.message, "error"); }
    }, "归档中…");
  }));
  $$("[data-delete]").forEach((btn) => btn.addEventListener("click", async () => {
    const ok = await confirmDialog({
      title: "删除项目",
      message: `确认删除项目“${btn.dataset.name}”？原始资料与历史分析记录会保留在数据库中，但项目将从列表和统计中移除。`,
      confirmLabel: "删除",
      danger: true,
    });
    if (!ok) return;
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
    const nameInput = $("#project-name", modal.root);
    if (!nameInput.value.trim()) {
      toast("请填写项目名称", "error");
      nameInput.focus();
      return;
    }
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
        <div class="sub">${esc(project.project_code || "无项目编号")} · 创建于 ${fmtDate(project.created_at)} · ${pill(project.status, projectStatusLabel(project))}</div>
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
    const ok = await confirmDialog({ title: "归档项目", message: "归档后该项目默认不在项目列表中显示，可随时取消归档。", confirmLabel: "归档" });
    if (!ok) return;
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
    const nameInput = $("#edit-name", modal.root);
    if (!nameInput.value.trim()) {
      toast("请填写项目名称", "error");
      nameInput.focus();
      return;
    }
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
  const priceCoverage = stages.analysis?.price_coverage || {};
  const priceMissing = priceCoverage.expected > 0 ? Math.max(0, num(priceCoverage.expected) - num(priceCoverage.recognized)) : 0;
  const priceMissingNames = (priceCoverage.missing_companies || []).slice(0, 5).join("、");
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
      <td>#${task.task_no}</td><td>${taskPill(task.status)}</td><td class="num">${task.company_count} 家</td>
      <td>${fmtDate(task.created_at)}</td><td>${fmtDate(task.finished_at)}</td>
      <td><a class="btn small" href="#/projects/${project.id}?tab=task">查看任务</a></td>
    </tr>`).join("");

  container.innerHTML = `
    <div class="grid cols-5">
      ${statCard("投标企业", `${num(stats.company_count)} <small>/ ${num(stats.company_limit, 20)}</small>`, "users")}
      ${statCard("原始文件（不含招标）", num(stats.file_count), "file")}
      ${statCard("已解析文件", `${num(stats.parsed_file_count)} <small>/ ${num(stats.file_count)}</small>`, "scan")}
      ${statCard("可参与分析企业", num(stats.analyzable_company_count), "shield", "ok")}
      ${statCard("待处理异常", num(stats.pending_issue_count), "alert", num(stats.pending_issue_count) ? "high" : "")}
    </div>
    <div class="next-step">
      <div class="next-step-body">
        <span class="next-step-label">下一步</span>
        <strong>${esc(todo.label || "继续完善项目资料")}</strong>
        <span class="small muted">系统根据当前资料准备情况动态推荐下一步操作</span>
      </div>
      <a class="btn primary" href="${todoTarget[todo.code] || `#/projects/${project.id}`}">${esc(todo.label || "进入项目")}</a>
    </div>
    ${priceMissing ? `<div class="warn-box" style="margin-top:16px" role="alert">
      <strong>价格维度覆盖不足：</strong>${num(priceCoverage.expected)} 家企业中 ${priceMissing} 家未识别到投标总报价
      ${priceMissingNames ? `（${esc(priceMissingNames)}）` : ""}。这些企业不参与价格异常判断，请核对报价写法或在「分析任务」查看执行日志。
    </div>` : ""}
    <div class="grid cols-2">
      <div class="card">
        <div class="card-head"><h2>项目准备情况</h2></div>
        <dl class="kv">
          <dt>招标文件</dt><dd>${stages.tender?.uploaded ? `已上传 ${stages.tender.file_count} 份` : "未上传（跳过公共内容排除）"}</dd>
          <dt>企业资料</dt><dd>${num(stages.companies?.analyzable)} 家可分析，${num(stages.companies?.abnormal)} 家异常，${num(stages.companies?.pending_confirm)} 家待确认</dd>
          <dt>文件解析</dt><dd>${num(stages.files?.parsed)} 份已解析，${num(stages.files?.partial)} 份部分成功，${num(stages.files?.failed)} 份失败</dd>
          <dt>比对分析</dt><dd>${stages.analysis?.started ? `${num(stages.analysis.task_count)} 次任务，最近状态：${TASK_STATUS[stages.analysis.last_status] || stages.analysis.last_status || "—"}` : "尚未启动"}</dd>
          <dt>总页数</dt><dd>${num(stages.files?.pages)} 页 <span class="small muted">（不含招标文件；解析中心统计含招标文件）</span></dd>
        </dl>
      </div>
      <div class="card">
        <div class="card-head"><h2>最近分析任务</h2></div>
        ${recent ? `<div class="table-scroll"><table><thead><tr><th>任务</th><th>状态</th><th class="num">企业</th><th>创建时间</th><th>完成时间</th><th></th></tr></thead><tbody>${recent}</tbody></table></div>` : `<div class="empty">还没有分析任务。</div>`}
      </div>
    </div>`;
}

/* ---------- 上传弹窗 ---------- */
const UPLOAD_ACCEPT = ".pdf,.docx,.doc,.txt,.md,.wps";
const UPLOAD_EXTENSIONS = UPLOAD_ACCEPT.split(",");
const UPLOAD_MAX_BYTES = 500 * 1024 * 1024;

/* 统一的文件上传弹窗：点击打开本地文件选择，或拖入文件批量上传 */
function openUploadModal({ title, hint = "", endpoint, onDone }) {
  const modal = openModal({
    title,
    body: `
      <div class="upload-dropzone" id="upload-dropzone" role="button" tabindex="0" autofocus
        aria-label="点击选择文件，或将文件拖放到此处批量上传">
        <div class="upload-dropzone-icon">${icon("upload")}</div>
        <div class="upload-dropzone-title">点击选择文件，或将文件拖放到此处</div>
        <div class="small muted">支持 PDF / DOCX / DOC / TXT / MD / WPS，可批量上传，单个文件不超过 500MB</div>
        ${hint ? `<div class="small muted" style="margin-top:6px">${esc(hint)}</div>` : ""}
      </div>
      <input type="file" id="upload-modal-input" multiple accept="${UPLOAD_ACCEPT}" hidden />
      <div class="upload-selected" id="upload-selected"></div>
      <div id="upload-progress" class="upload-progress" hidden style="margin-top:12px">
        <div class="progress-track" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0">
          <div class="progress-fill" id="upload-progress-fill" style="width:0%"></div>
        </div>
        <div class="small muted" id="upload-progress-text" style="margin-top:6px"></div>
      </div>`,
    footer: `<button type="button" class="btn" data-close>取消</button>
      <button type="button" class="btn primary" id="upload-submit" disabled>${icon("upload")}开始上传</button>`,
  });

  const dropzone = $("#upload-dropzone", modal.root);
  const input = $("#upload-modal-input", modal.root);
  const selectedBox = $("#upload-selected", modal.root);
  const submitBtn = $("#upload-submit", modal.root);
  let selected = [];

  const renderSelected = () => {
    selectedBox.innerHTML = selected.length
      ? `<div class="small muted">已选择 ${selected.length} 个文件</div>
        ${selected.map((file, index) => `
          <div class="upload-file">
            ${icon("file")}
            <div class="upload-file-name">${esc(file.name)}
              <div class="small muted">${fmtSize(file.size)}</div>
            </div>
            <button type="button" class="btn small ghost" data-remove="${index}" aria-label="移除文件：${esc(file.name)}">${icon("close")}</button>
          </div>`).join("")}`
      : "";
    $$("[data-remove]", selectedBox).forEach((btn) => btn.addEventListener("click", () => {
      selected.splice(Number(btn.dataset.remove), 1);
      renderSelected();
    }));
    submitBtn.disabled = !selected.length;
  };

  const addFiles = (fileList) => {
    const files = Array.from(fileList || []);
    let unsupported = 0;
    let oversized = 0;
    files.forEach((file) => {
      const ext = `.${file.name.split(".").pop() || ""}`.toLowerCase();
      if (!UPLOAD_EXTENSIONS.includes(ext)) { unsupported += 1; return; }
      if (file.size > UPLOAD_MAX_BYTES) { oversized += 1; return; }
      const key = `${file.name}:${file.size}:${file.lastModified}`;
      if (selected.some((item) => `${item.name}:${item.size}:${item.lastModified}` === key)) return;
      selected.push(file);
    });
    renderSelected();
    if (unsupported) toast(`已跳过 ${unsupported} 个不支持的文件格式`, "error");
    if (oversized) toast(`已跳过 ${oversized} 个超过 500MB 的文件`, "error");
  };

  dropzone.addEventListener("click", () => input.click());
  dropzone.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" && event.key !== " ") return;
    event.preventDefault();
    input.click();
  });
  input.addEventListener("change", () => { addFiles(input.files); input.value = ""; });
  ["dragenter", "dragover"].forEach((type) => dropzone.addEventListener(type, (event) => {
    event.preventDefault();
    event.stopPropagation();
    dropzone.classList.add("dragover");
  }));
  ["dragleave", "drop"].forEach((type) => dropzone.addEventListener(type, (event) => {
    event.preventDefault();
    event.stopPropagation();
    dropzone.classList.remove("dragover");
  }));
  dropzone.addEventListener("drop", (event) => addFiles(event.dataTransfer?.files));
  /* 防止文件落在弹窗其他位置时被浏览器直接打开 */
  modal.root.addEventListener("dragover", (event) => event.preventDefault());
  modal.root.addEventListener("drop", (event) => event.preventDefault());

  submitBtn.addEventListener("click", () => withBusy(submitBtn, async () => {
    if (!selected.length) return toast("请选择文件", "error");
    const progressBox = $("#upload-progress", modal.root);
    const progressFill = $("#upload-progress-fill", modal.root);
    const progressText = $("#upload-progress-text", modal.root);
    const rows = $$(".upload-file", selectedBox);
    const setStatus = (text) => rows.forEach((row) => {
      let status = row.querySelector(".upload-file-status");
      if (!status) {
        status = document.createElement("span");
        status.className = "upload-file-status small muted";
        row.appendChild(status);
      }
      status.textContent = text;
    });
    progressBox.hidden = false;
    setStatus("上传中…");
    try {
      const result = await uploadWithProgress(endpoint, selected, (ratio) => {
        const percent = Math.round(ratio * 100);
        progressFill.style.width = `${percent}%`;
        progressFill.parentElement?.setAttribute("aria-valuenow", String(percent));
        progressText.textContent = `正在上传 ${selected.length} 个文件 · ${percent}%`;
      });
      setStatus("已完成");
      progressText.textContent = `已完成：${result?.message || `已上传 ${selected.length} 份文件`}`;
      toast(result.message, "success");
      modal.close();
      if (onDone) onDone();
    } catch (error) {
      setStatus("失败");
      progressText.textContent = `上传失败：${error.message}`;
      toast(error.message, "error");
    }
  }, "上传中…"));
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
      <button type="button" class="btn small" data-open-file="${esc(file.id)}">查看</button>
      <button class="btn small" data-parse-file="${file.id}">${file.parse_status === "success" ? "重新解析" : "解析"}</button>
    </div>`).join("");

  container.innerHTML = `
    <div class="card">
      <div class="row" style="justify-content:space-between;align-items:flex-start">
        <div>
          <h2 style="margin:0">招标文件</h2>
          <div class="small muted" style="margin-top:4px">可选 · 用于识别公共内容，避免模板条款产生误报</div>
        </div>
        <button class="btn" id="tender-upload">${icon("upload")}上传文件</button>
      </div>
      <div style="margin-top:12px">${tenderRows || `<div class="empty" style="padding:18px">尚未上传招标文件。未上传时分析不执行公共内容排除。</div>`}</div>
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

  $("#tender-upload").addEventListener("click", () => openUploadModal({
    title: "上传招标文件",
    hint: "招标文件为可选项；未上传时分析不执行公共内容排除。",
    endpoint: `/projects/${project.id}/tender-files`,
    onDone: () => renderFiles(container, project),
  }));
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
      <button type="button" class="btn small" data-open-file="${esc(file.id)}">${icon("external")}查看</button>
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
          <button class="btn small" id="company-file-upload">${icon("upload")}上传文件</button>
          ${!company.confirmed && company.file_count > 0 ? `<button class="btn primary small" id="confirm-company">${icon("check")}确认企业资料</button>` : ""}
          <button class="btn small" id="preview-logical">${icon("layers")}预览逻辑标书</button>
        </div>
      </div>
      ${rows || `<div class="empty" style="padding:20px">该企业还没有文件。</div>`}
    </div>`;
}

function bindCompanyDetail(container, company, project) {
  const refresh = () => renderFiles(container, project);
  $("#company-file-upload", container)?.addEventListener("click", () => openUploadModal({
    title: `上传企业文件 · ${company.company_name}`,
    hint: "一家企业可上传多份文件；上传后拖动文件顺序即逻辑标书顺序。",
    endpoint: `/companies/${company.id}/files`,
    onDone: refresh,
  }));
  const confirmBtn = $("#confirm-company", container);
  confirmBtn?.addEventListener("click", () => withBusy(confirmBtn, async () => {
    try { await api(`/companies/${company.id}/confirm`, { method: "POST" }); toast("企业资料已确认", "success"); refresh(); }
    catch (error) { toast(error.message, "error"); }
  }, "确认中…"));
  $("#preview-logical", container)?.addEventListener("click", () => previewLogical(company));
  $$("[data-parse]", container).forEach((btn) => btn.addEventListener("click", () => parseFile(btn.dataset.parse, refresh)));
  $$("[data-delete-file]", container).forEach((btn) => btn.addEventListener("click", async () => {
    const ok = await confirmDialog({
      title: "删除文件",
      message: `确认删除文件“${btn.dataset.fileName}”？原始文件将标记为失效，历史分析记录不会被覆盖。`,
      confirmLabel: "删除",
      danger: true,
    });
    if (!ok) return;
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
    const nameInput = $("#company-name", modal.root);
    if (!nameInput.value.trim()) {
      toast("请填写企业名称", "error");
      nameInput.focus();
      return;
    }
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
          ${(item.pages || []).map((page) => `<button type="button" class="link-btn" data-open-file="${esc(item.file_id)}" data-page="${num(page.page_index)}" style="margin-right:10px">第${page.page_index}页</button>`).join("") || `<span class="muted">该文件尚未解析</span>`}
        </div>
      </details>`).join("");
    openModal({
      title: `逻辑标书预览 · ${company.company_name}`,
      wide: true,
      body: `<div class="info-box">逻辑标书只组织文件顺序，不合并、不修改原始文件；点击页码打开对应原始文件页。</div>
        <div class="small muted" style="margin-bottom:10px">版本 v${tree.version_no || "—"} · 逻辑总页数 ${tree.total_pages} · 状态 ${LOGICAL_STATUS[tree.status] || tree.status || "—"}</div>
        ${items || `<div class="empty">尚未生成逻辑标书</div>`}`,
    });
  } catch (error) { toast(error.message, "error"); }
}

/* ---------- parse center ---------- */
async function renderParseCenter(container, project) {
  const data = await api(`/projects/${project.id}/parse-center`);
  const counts = data.counts || {};
  const emptyPages = (data.rows || []).reduce((sum, row) => sum + num(row.empty_pages), 0);
  const ocrPages = (data.rows || []).reduce((sum, row) => sum + num(row.ocr_pages), 0);
  const ocrHint = emptyPages > 0 && ocrPages === 0
    ? `<div class="warn-box" style="margin-bottom:14px" role="alert">
        <strong>有 ${emptyPages} 页没有提取到文本（空白页或扫描页）。</strong>
        当前没有页面通过 OCR 识别。若这些是扫描件，请在「系统管理 → 检测规则 → OCR 识别」中启用 OCR 并配置凭据，否则这部分内容不会参与比对。
      </div>` : "";
  const rows = data.rows.map((row) => `
    <tr>
      <td>${esc(row.file_name)}<div class="small muted">${row.file_type.toUpperCase()} · v${row.version}</div></td>
      <td>${esc(row.company_name)}</td>
      <td>${row.page_count}</td>
      <td>${row.success_pages > row.page_count ? `<span class="pill warn" title="成功页数大于总页数，可能是历史重复解析数据">${row.success_pages} · 数据异常</span>` : row.success_pages}</td>
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
    ${ocrHint}
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
        <button type="button" class="btn small" data-open-file="${esc(fileId)}">打开原始文件</button>
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
  const priceCoverage = task.progress?.price?.coverage || {};
  const priceMissing = priceCoverage.expected > 0 ? Math.max(0, num(priceCoverage.expected) - num(priceCoverage.recognized)) : 0;
  const priceMissingNames = (priceCoverage.missing_companies || []).slice(0, 5).join("、");
  const priceCoverageHtml = priceMissing ? `
    <div class="warn-box" style="margin-top:14px" role="alert">
      <strong>价格维度未完全覆盖：</strong>${num(priceCoverage.expected)} 家企业中 ${priceMissing} 家未识别到投标总报价
      ${priceMissingNames ? `（${esc(priceMissingNames)}）` : ""}，这些企业不参与价格异常判断。
    </div>` : "";
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
      ${priceCoverageHtml}
      ${failedHtml}
      <div class="grid cols-2" style="margin-top:16px">
        <div><h3>参与企业及文件版本</h3>${companies}</div>
        <div><h3>执行日志</h3><div class="log-list">${logs || "暂无日志"}</div></div>
      </div>
    </div>`;
  const cancelBtn = $("#cancel-task", container);
  cancelBtn?.addEventListener("click", async () => {
    const ok = await confirmDialog({
      title: "取消分析任务",
      message: "取消后正在执行的比对会停止；已生成的证据会保留，可重新发起分析。",
      confirmLabel: "取消任务",
      danger: true,
    });
    if (!ok) return;
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
  const priceCoverage = stats.price_coverage || {};
  const priceMissing = priceCoverage.expected > 0 ? Math.max(0, num(priceCoverage.expected) - num(priceCoverage.recognized)) : 0;
  const missingNames = (priceCoverage.missing_companies || []).slice(0, 5).join("、");
  const excludedText = Math.max(0, num(stats.text_evidence_count) - num(stats.effective_text_count));
  const pairRows = data.pairs.map((pair) => `
    <tr>
      <td class="cell-main"><a href="#/projects/${project.id}?tab=analysis&pair=${pair.pair_id}">${esc(pair.company_a)} ↔ ${esc(pair.company_b)}</a></td>
      <td class="num">${pair.text_count}</td><td class="num">${pair.personnel_count}</td><td class="num">${pair.price_count}</td>
      <td class="col-secondary">${riskPill(pair.text_risk)}</td><td class="col-secondary">${riskPill(pair.personnel_risk)}</td><td class="col-secondary">${riskPill(pair.price_risk)}</td>
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

  const priceWarning = priceMissing > 0 ? `
    <div class="warn-box" style="margin-top:16px" role="alert">
      <strong>价格维度未完全覆盖：</strong>${num(priceCoverage.expected)} 家企业中 ${priceMissing} 家未识别到投标总报价
      ${missingNames ? `（${esc(missingNames)}${priceMissing > 5 ? " 等" : ""}）` : ""}。
      这些企业不参与价格异常判断，结果页的「价格异常 0 家」不代表其报价没有问题。
    </div>` : "";

  container.innerHTML = `
    <div class="grid cols-5">
      ${statCard("参与企业", stats.participant_count, "users")}
      ${statCard("比对组合", stats.pair_count, "layers")}
      ${statCard("文本相似证据", stats.text_evidence_count, "file", "medium", `有效 ${num(stats.effective_text_count)} · 命中招标公共内容已排除 ${excludedText}`)}
      ${statCard("重复人员", stats.personnel_duplicate_count, "user", "medium")}
      ${statCard("价格异常企业", stats.price_abnormal_company_count, "coins", "high", priceCoverage.expected ? `已识别报价 ${num(priceCoverage.recognized)} / ${num(priceCoverage.expected)} 家` : "")}
    </div>
    ${priceWarning}
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
      <table><thead><tr><th>企业组合</th><th class="num">文本证据</th><th class="num">人员重复</th><th class="num">价格异常</th><th class="col-secondary">文本风险</th><th class="col-secondary">人员风险</th><th class="col-secondary">价格风险</th><th>综合风险</th><th>操作</th></tr></thead>
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
  const priceCoverage = stats.price_coverage || {};
  const priceMissing = priceCoverage.expected > 0 ? Math.max(0, num(priceCoverage.expected) - num(priceCoverage.recognized)) : 0;
  container.innerHTML = `
    <div class="card">
      <h2>分析报告</h2>
      ${task ? `<div class="info-box">当前报告基于任务 #${task.task_no}（${TASK_STATUS[task.status] || task.status}）生成，算法版本 ${esc(task.algorithm_version)}。报告包含 4 个工作表。</div>` : `<div class="warn-box">项目还没有可导出的分析结果。</div>`}
      ${priceMissing ? `<div class="warn-box" style="margin-top:12px" role="alert"><strong>价格维度未完全覆盖：</strong>${priceMissing} 家企业未识别到投标总报价，报告中价格异常表不包含这些企业。</div>` : ""}
      <div class="grid cols-4">
        ${statCard("参与企业", num(stats.participant_count), "users")}
        ${statCard("比对组合", num(stats.pair_count), "layers")}
        ${statCard("文本相似证据", num(stats.text_evidence_count), "file", "medium", `有效 ${num(stats.effective_text_count)} 条`)}
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
      <table><thead><tr><th>任务</th><th>状态</th><th>企业数</th><th>创建时间</th><th>完成时间</th><th class="col-secondary">算法版本</th></tr></thead>
        <tbody>${(data.tasks || []).map((t) => `<tr><td>#${t.task_no}</td><td>${taskPill(t.status)}</td><td>${t.company_count}</td><td>${fmtDate(t.created_at)}</td><td>${fmtDate(t.finished_at)}</td><td class="mono col-secondary">${esc(t.algorithm_version)}</td></tr>`).join("") || `<tr><td colspan="6"><div class="empty">暂无任务</div></td></tr>`}</tbody></table>
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

/* ---------- 三栏证据查看器：左中两列展示证据片段，右列展示分析信息 ---------- */
/* 证据筛选条件提到模块级：切换企业组合后仍保留，避免审核员每组重设一次 */
let evidenceFilterState = { review: "", severity: "", tender: "" };

/* 上下文展示参数：与后端 app/services/evidence_context.py 保持一致 */
const CONTEXT_DEFAULT = { before: 100, after: 100 };
const CONTEXT_STEP = 200;

/* 某一侧用于展示的来源信息：优先用证据对照里的定位，缺失时回退到来源记录 */
function sidePayloadOf(evidence, side) {
  const payload = evidence.sides?.[side];
  if (payload && (payload.segments || []).length) return payload;
  const fallback = side === "a" ? evidence.source_a : evidence.source_b;
  return { ...(fallback || {}), segments: [] };
}

/* 命中片段 + 前后文的分段渲染：相似绿色、差异橙色、上下文灰色 */
function sideSegmentsHtml(payload) {
  const segments = payload?.segments || [];
  if (!segments.length) {
    const snippet = payload?.snippet || "";
    return `<div class="muted small">${snippet ? esc(snippet) : "该证据没有提供此侧原文位置。"}</div>`;
  }
  const parts = [];
  let lastPage = null;
  segments.forEach((segment) => {
    if (segment.page_index && segment.page_index !== lastPage) {
      parts.push(`<span class="ev-page">第 ${segment.page_index} 页</span>`);
      lastPage = segment.page_index;
    }
    const text = esc(segment.text);
    if (segment.kind === "match") parts.push(`<mark class="ev-match">${text}</mark>`);
    else if (segment.kind === "diff") parts.push(`<span class="ev-diff">${text}</span>`);
    else parts.push(`<span class="ev-ctx">${text}</span>`);
  });
  return `<div class="ev-text">${parts.join("")}</div>`;
}

/* 来源信息行：投标人、文件、页码、章节 */
function sideSourceLine(payload, fallbackName) {
  const name = payload?.company_name || fallbackName || "—";
  const file = payload?.file_name || "—";
  const pageStart = payload?.page_start || payload?.page_index;
  const pageEnd = payload?.page_end || payload?.page_index;
  const pageLabel = pageStart && pageEnd && pageEnd !== pageStart
    ? `第 ${pageStart}-${pageEnd} 页`
    : `第 ${pageStart ?? "—"} 页`;
  const printed = payload?.printed_page_number ? `（印刷页码 ${esc(payload.printed_page_number)}）` : "";
  const version = payload?.file_version > 1 ? ` · 第 ${payload.file_version} 版` : "";
  const chapter = [payload?.chapter, payload?.section].filter(Boolean).join(" / ");
  return `${esc(name)} · ${esc(file)} · ${pageLabel}${printed}${version}${chapter ? ` · ${esc(chapter)}` : ""}`;
}

/* 一条证据在某一侧的对照卡片 */
function evidenceCardHtml(item, side, index, fallbackName, activeId) {
  const payload = item.sides?.[side];
  const group = item.group_size > 1
    ? `<span class="ev-group">第 ${item.group_index}/${item.group_size} 处相似片段</span>`
    : "";
  const contextState = item.context_state?.[side] || {};
  const expanded = (contextState.before || CONTEXT_DEFAULT.before) > CONTEXT_DEFAULT.before
    || (contextState.after || CONTEXT_DEFAULT.after) > CONTEXT_DEFAULT.after;
  return `
    <article class="ev-card${item.id === activeId ? " active" : ""}" data-evidence-id="${esc(item.id)}" data-side="${side}">
      <header class="ev-card-head">
        <span class="ev-seq">证据 ${index + 1}</span>
        ${group}
        <span class="spacer"></span>
        <span class="pill ${esc(item.severity)}">${RISK[item.severity] || "无"}</span>
      </header>
      <div class="ev-source small muted">${sideSourceLine(payload, fallbackName)}</div>
      ${sideSegmentsHtml(payload)}
      <div class="ev-actions">
        <button type="button" class="btn tiny" data-expand="before"${payload?.has_more_before ? "" : " disabled"}>展开更多前文</button>
        <button type="button" class="btn tiny" data-expand="after"${payload?.has_more_after ? "" : " disabled"}>展开更多后文</button>
        <button type="button" class="btn tiny ghost" data-expand="collapse"${expanded ? "" : " disabled"}>收起扩展</button>
        <button type="button" class="btn tiny" data-original>${icon("file")}查看完整原文</button>
      </div>
    </article>`;
}

async function mountPairViewer(container, pairId, evidenceId) {
  const meta = await api(`/pairs/${pairId}`);
  const pair = meta.pair;
  const view = {
    type: "text",
    review: evidenceFilterState.review,
    severity: evidenceFilterState.severity,
    tender: evidenceFilterState.tender,
    page: 1,
    pageSize: 20,
    total: 0,
    items: [],
    counts: meta.counts || {},
    breakdown: meta.count_breakdown || {},
    currentId: evidenceId || null,
  };
  /* 深链进入时按证据类型切换标签页 */
  if (view.currentId) {
    try {
      const linked = await api(`/evidences/${view.currentId}`);
      if (linked.evidence_type && TYPE[linked.evidence_type]) view.type = linked.evidence_type;
    } catch (error) {
      view.currentId = null;
    }
  }

  container.innerHTML = `
    <div class="card viewer-toolbar">
      <div class="row">
        <strong>${esc(pair.company_a)} ↔ ${esc(pair.company_b)}</strong>
        ${riskPill(pair.overall_risk)}
        <span class="spacer"></span>
        <select id="ev-filter-review" aria-label="按审核状态筛选证据"><option value="">全部审核状态</option>${Object.entries(REVIEW).map(([v, l]) => `<option value="${v}" ${view.review === v ? "selected" : ""}>${l}</option>`).join("")}</select>
        <select id="ev-filter-severity" aria-label="按风险等级筛选证据"><option value="">全部风险</option>${Object.entries(RISK).map(([v, l]) => `<option value="${v}" ${view.severity === v ? "selected" : ""}>${l}</option>`).join("")}</select>
        <select id="ev-filter-tender" aria-label="按招标文件命中情况筛选">
          <option value="">全部内容</option>
          <option value="hit" ${view.tender === "hit" ? "selected" : ""}>命中招标说明</option>
          <option value="effective" ${view.tender === "effective" ? "selected" : ""}>未命中招标说明</option>
        </select>
        <button type="button" class="btn small" id="viewer-focus" aria-pressed="false" title="隐藏右栏证据清单，让两侧原文更宽">专注阅读</button>
      </div>
      <div class="small muted" id="count-note" style="margin-top:8px" aria-live="polite"></div>
      <div class="small muted" style="margin-top:8px">
        左列与中列只展示命中片段及其前后必要上下文，完整原文按需打开；<span class="flag-inline tender">命中招标说明</span>
        表示内容命中招标文件公共内容（已排除，但仍保留识别记录）。筛选条件只影响清单展示，不影响原始识别结果。
      </div>
    </div>
    <div class="tabs" id="evidence-tabs" role="tablist" aria-label="证据类型">
      ${Object.entries(TYPE).map(([type, label]) => `<button type="button" class="tab ${type === view.type ? "active" : ""}" role="tab" aria-selected="${type === view.type}" data-type="${type}">${label}（${view.counts[type] || 0}）</button>`).join("")}
    </div>
    <div class="viewer-grid">
      <div class="viewer-col" id="viewer-a">
        <div class="col-head"><div class="title">${esc(pair.company_a)} · 证据片段</div><div class="file" id="side-a-count"></div></div>
        <div class="viewer-body" id="side-a-body"></div>
      </div>
      <div class="viewer-col" id="viewer-b">
        <div class="col-head"><div class="title">${esc(pair.company_b)} · 证据片段</div><div class="file" id="side-b-count"></div></div>
        <div class="viewer-body" id="side-b-body"></div>
      </div>
      <div class="viewer-col evidence-col">
        <div class="col-head"><div class="title">证据清单与分析</div><div class="file" id="evidence-count"></div></div>
        <div class="viewer-body">
          <div class="evidence-list" id="evidence-list"></div>
          <div id="ev-more" class="ev-more"></div>
          <div id="evidence-detail" class="detail-block"><div class="muted small">选择一条证据查看相似度、差异说明并完成审核。</div></div>
        </div>
      </div>
    </div>`;

  const listEl = $("#evidence-list", container);
  const detailEl = $("#evidence-detail", container);
  const bodyA = $("#side-a-body", container);
  const bodyB = $("#side-b-body", container);

  const renderTabs = () => {
    $$("#evidence-tabs [data-type]", container).forEach((tab) => {
      const active = tab.dataset.type === view.type;
      tab.classList.toggle("active", active);
      tab.setAttribute("aria-selected", String(active));
      tab.textContent = `${TYPE[tab.dataset.type] || tab.dataset.type}（${view.counts[tab.dataset.type] || 0}）`;
    });
  };

  const renderList = () => {
    $("#evidence-count", container).textContent = view.items.length === view.total
      ? `${view.total} 条证据`
      : `${view.items.length} / ${view.total} 条（已筛选）`;
    listEl.innerHTML = view.items.map((item) => `
      <div class="evidence-item review-${esc(item.review_status)} ${item.excluded_by_tender ? "tender-hit" : ""} ${item.id === view.currentId ? "active" : ""}"
        data-evidence-id="${esc(item.id)}" role="button" tabindex="0" aria-pressed="${item.id === view.currentId}" aria-label="查看证据：${esc(item.summary)}">
        <div class="row"><span class="pill ${esc(item.severity)}">${RISK[item.severity] || "无"}</span>
          <span class="small muted">${esc(subtypeLabel(item.subtype))}</span><span class="spacer"></span>
          <span class="pill">${REVIEW[item.review_status] || item.review_status}</span></div>
        ${item.excluded_by_tender ? `<div class="flag tender">命中招标说明 · 公共内容已排除</div>` : ""}
        <div class="summary">${esc(item.summary)}</div>
        ${item.group_size > 1 ? `<div class="meta">第 ${item.group_index}/${item.group_size} 处相似片段</div>` : ""}
      </div>`).join("") || `<div class="empty" style="padding:18px">没有符合筛选条件的证据</div>`;
    if (view.items.length < view.total) {
      const more = document.createElement("button");
      more.type = "button";
      more.className = "btn small block";
      more.id = "ev-load-more";
      more.textContent = `加载更多证据（还有 ${view.total - view.items.length} 条）`;
      more.addEventListener("click", () => loadItems(false));
      $("#ev-more", container).innerHTML = "";
      $("#ev-more", container).appendChild(more);
    } else {
      $("#ev-more", container).innerHTML = "";
    }
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

  /* 下一个待审证据：审核是条队列，审完一条要能直接进下一条 */
  const nextPendingId = (fromId = view.currentId) => {
    const queue = view.items;
    const start = queue.findIndex((item) => item.id === fromId);
    for (let step = 1; step <= queue.length; step += 1) {
      const item = queue[(start + step) % queue.length];
      if (item && item.id !== fromId && item.review_status === "pending") return item.id;
    }
    return null;
  };

  /* 审核后把焦点交还清单，键盘用户不会掉回 body */
  const focusEvidenceItem = (id) => {
    const node = listEl.querySelector(`[data-evidence-id="${id}"]`);
    if (node) node.focus({ preventScroll: false });
  };

  const renderColumn = (side) => {
    const body = side === "a" ? bodyA : bodyB;
    const fallbackName = side === "a" ? pair.company_a : pair.company_b;
    const keepScroll = body.scrollTop;
    body.innerHTML = view.items.map((item, index) =>
      evidenceCardHtml(item, side, index, fallbackName, view.currentId)).join("")
      || `<div class="empty">该类型暂无符合条件的证据</div>`;
    $$(`[data-evidence-id]`, body).forEach((card) => {
      card.addEventListener("click", (event) => {
        if (event.target.closest("button")) return;
        selectEvidence(card.dataset.evidenceId);
      });
    });
    $$("[data-expand]", body).forEach((button) => {
      button.addEventListener("click", (event) => {
        event.stopPropagation();
        expandContext(card_owner(button), side, button.dataset.expand);
      });
    });
    $$("[data-original]", body).forEach((button) => {
      button.addEventListener("click", (event) => {
        event.stopPropagation();
        openOriginal(card_owner(button), side);
      });
    });
    // 重绘后保持原来的阅读位置
    body.scrollTop = keepScroll;
  };

  /* 找到按钮所属的证据卡片 */
  const card_owner = (button) => button.closest("[data-evidence-id]")?.dataset.evidenceId;

  const renderColumns = () => {
    renderColumn("a");
    renderColumn("b");
    $("#side-a-count", container).textContent = `${view.items.length} 条证据片段`;
    $("#side-b-count", container).textContent = `${view.items.length} 条证据片段`;
  };

  const highlightActive = () => {
    $$(".ev-card", container).forEach((card) => {
      card.classList.toggle("active", card.dataset.evidenceId === view.currentId);
    });
    $$(".evidence-item", listEl).forEach((item) => {
      const active = item.dataset.evidenceId === view.currentId;
      item.classList.toggle("active", active);
      item.setAttribute("aria-pressed", String(active));
    });
  };

  const scrollToEvidence = (evidenceId) => {
    $$(".ev-card", container).forEach((card) => {
      if (card.dataset.evidenceId !== evidenceId) return;
      const body = card.parentElement;
      const top = card.getBoundingClientRect().top - body.getBoundingClientRect().top;
      body.scrollTo({ top: body.scrollTop + top - 8, behavior: "smooth" });
    });
  };

  const selectEvidence = async (id) => {
    view.currentId = id;
    highlightActive();
    scrollToEvidence(id);
    detailEl.innerHTML = skeletonBlock(4);
    try {
      const evidence = await api(`/evidences/${id}`);
      renderDetail(evidence);
    } catch (error) {
      detailEl.innerHTML = `<div class="error-box">${esc(error.message)}</div>
        <button class="btn small" id="evidence-retry">重试</button>`;
      $("#evidence-retry", detailEl)?.addEventListener("click", () => selectEvidence(id));
    }
  };

  const renderDetail = (evidence) => {
    detailEl.innerHTML = evidenceDetailHtml(evidence) + reviewFormHtml(evidence);
    bindReviewForm(detailEl, evidence, async () => {
      const refreshed = await api(`/evidences/${evidence.id}`);
      const index = view.items.findIndex((item) => item.id === refreshed.id);
      if (index >= 0) {
        view.items[index] = { ...view.items[index], review_status: refreshed.review_status };
      }
      renderList();
      renderTabs();
      renderDetail(refreshed);
      focusEvidenceItem(evidence.id);
    });
    const jump = nextPendingId(evidence.id);
    const jumpBtn = $("#review-next", detailEl);
    if (jumpBtn) {
      jumpBtn.disabled = !jump;
      jumpBtn.textContent = jump ? "下一条待审核 →" : "队列已审完";
      jumpBtn.addEventListener("click", () => { if (jump) selectEvidence(jump); });
    }
  };

  /* 展开 / 收起某一侧的上下文：只请求当前证据所需的文本范围 */
  const expandContext = async (evidenceId, side, direction) => {
    const item = view.items.find((entry) => entry.id === evidenceId);
    if (!item) return;
    const state = item.context_state?.[side] || { ...CONTEXT_DEFAULT };
    const next = { ...state };
    if (direction === "before") next.before += CONTEXT_STEP;
    else if (direction === "after") next.after += CONTEXT_STEP;
    else next.before = CONTEXT_DEFAULT.before, next.after = CONTEXT_DEFAULT.after;
    item.context_state = { ...(item.context_state || {}), [side]: next };
    const body = side === "a" ? bodyA : bodyB;
    const card = $(`[data-evidence-id="${evidenceId}"]`, body);
    const button = $("[data-expand]", card);
    if (button) {
      button.disabled = true;
      button.textContent = "加载中…";
    }
    try {
      const data = await api(`/evidences/${evidenceId}/context?side=${side}&before=${next.before}&after=${next.after}`);
      item.sides = { ...item.sides, [side]: data.side_payload };
      renderColumn(side);
      highlightActive();
    } catch (error) {
      toast(error.message, "error");
      renderColumn(side);
    }
  };

  /* 查看完整原文：按需加载对应文件并定位到证据所在页 */
  const openOriginal = async (evidenceId, side) => {
    const item = view.items.find((entry) => entry.id === evidenceId);
    const payload = item?.sides?.[side];
    const fileId = payload?.file_id;
    if (!fileId) {
      toast("该证据没有关联的原始文件", "error");
      return;
    }
    const page = payload.page_start || payload.page_index || 1;
    const title = `${payload.file_name || "原始文件"} · 第 ${page} 页`;
    let rawUrl;
    try {
      const { ticket } = await api(`/files/${fileId}/ticket`, { method: "POST" });
      rawUrl = `${API}/files/${fileId}/content?ticket=${encodeURIComponent(ticket)}`;
    } catch (error) {
      toast(error.message, "error");
      return;
    }
    const toolbar = `<div class="original-bar">
        <span class="small muted">${esc(payload.company_name || "")} · ${esc(payload.file_name || "")} · 第 ${page} 页</span>
        <span class="spacer"></span>
        <a class="btn tiny" href="${rawUrl}#page=${page}" target="_blank" rel="noopener">${icon("external")}在新标签页打开</a>
      </div>`;
    if (payload.file_type === "pdf") {
      const url = `${rawUrl}#page=${page}`;
      openModal({
        title,
        wide: true,
        body: `${toolbar}<div class="original-frame"><iframe src="${url}" title="${esc(payload.file_name || "原始文件")}"></iframe></div>`,
      });
      return;
    }
    const modal = openModal({
      title,
      wide: true,
      body: `${toolbar}<div class="loading">加载原文…</div>`,
    });
    const bodyEl = $(".modal-body", modal.root);
    try {
      const ranges = payload.ranges || [];
      const pages = [];
      const firstPage = payload.page_start || page;
      const lastPage = Math.max(firstPage, payload.page_end || page);
      for (let index = firstPage; index <= lastPage; index += 1) pages.push(index);
      const details = await Promise.all(pages.map((index) => api(`/files/${fileId}/pages/${index}`)));
      bodyEl.innerHTML = details.map((detail) => {
        const blocks = (detail.page.blocks || []).map((block) => {
          const hits = ranges
            .filter((range) => range.block_id === block.id)
            .map((range) => ({
              start: Math.max(0, Math.min(range.start || 0, block.content.length)),
              end: Math.max(0, Math.min(range.end || block.content.length, block.content.length)),
            }))
            .filter((range) => range.end > range.start)
            .sort((a, b) => a.start - b.start);
          if (!hits.length) return `<div class="original-block">${esc(block.content)}</div>`;
          let cursor = 0;
          let marked = "";
          for (const hit of hits) {
            const start = Math.max(cursor, hit.start);
            marked += esc(block.content.slice(cursor, start));
            marked += `<mark>${esc(block.content.slice(start, hit.end))}</mark>`;
            cursor = Math.max(cursor, hit.end);
          }
          marked += esc(block.content.slice(cursor));
          return `<div class="original-block">${marked}</div>`;
        }).join("");
        return `<div class="small muted" style="margin:10px 0 6px">第 ${detail.page.page_index} 页</div>${blocks || `<div class="muted">该页没有文本</div>`}`;
      }).join("");
    } catch (error) {
      bodyEl.innerHTML = `<div class="error-box">${esc(error.message)}</div>`;
    }
  };

  const loadItems = async (reset = true) => {
    const params = new URLSearchParams();
    params.set("page", String(view.page));
    params.set("page_size", String(view.pageSize));
    if (view.type) params.set("evidence_type", view.type);
    if (view.review) params.set("review_status", view.review);
    if (view.severity) params.set("severity", view.severity);
    if (view.tender) params.set("tender", view.tender);
    const button = $("#ev-load-more", listEl);
    if (button) {
      button.disabled = true;
      button.textContent = "加载中…";
    }
    try {
      const data = await api(`/pairs/${pairId}/evidences?${params.toString()}`);
      view.counts = data.counts || view.counts;
      view.total = data.total || 0;
      view.items = reset ? (data.items || []) : [...view.items, ...(data.items || [])];
      const note = $("#count-note", container);
      if (note) {
        const breakdown = view.breakdown?.[view.type];
        note.textContent = breakdown
          ? `本组合${TYPE[view.type] || view.type}：识别 ${breakdown.identified} 条，命中招标公共内容已排除 ${breakdown.excluded_by_tender} 条，有效 ${breakdown.effective} 条；当前筛选显示 ${view.total} 条。`
          : `本组合${TYPE[view.type] || view.type}：共 ${view.total} 条。`;
      }
      renderTabs();
      renderList();
      renderColumns();
      if (view.currentId && view.items.some((item) => item.id === view.currentId)) {
        highlightActive();
        scrollToEvidence(view.currentId);
      }
    } catch (error) {
      toast(error.message, "error");
    }
  };

  const applyFilters = async () => {
    view.review = $("#ev-filter-review", container).value;
    view.severity = $("#ev-filter-severity", container).value;
    view.tender = $("#ev-filter-tender", container).value;
    evidenceFilterState = { review: view.review, severity: view.severity, tender: view.tender };
    view.page = 1;
    await loadItems(true);
    if (view.currentId && !view.items.some((item) => item.id === view.currentId)) {
      view.currentId = null;
      detailEl.innerHTML = `<div class="muted small">当前证据已被筛选条件排除，请重新选择证据。</div>`;
    }
  };

  $("#ev-filter-review", container).addEventListener("change", applyFilters);
  $("#ev-filter-severity", container).addEventListener("change", applyFilters);
  $("#ev-filter-tender", container).addEventListener("change", applyFilters);

  /* 专注阅读：隐藏右栏，把宽度让给两侧原文 */
  const focusBtn = $("#viewer-focus", container);
  focusBtn?.addEventListener("click", () => {
    const grid = $(".viewer-grid", container);
    const active = grid.classList.toggle("focus");
    focusBtn.setAttribute("aria-pressed", String(active));
    focusBtn.textContent = active ? "退出专注" : "专注阅读";
  });

  $$("#evidence-tabs [data-type]", container).forEach((tab) => tab.addEventListener("click", async () => {
    view.type = tab.dataset.type;
    view.page = 1;
    view.currentId = null;
    detailEl.innerHTML = `<div class="muted small">选择一条证据查看相似度、差异说明并完成审核。</div>`;
    renderTabs();
    await loadItems(true);
  }));

  /* 双侧滚动同步：两侧证据顺序一致，滚动一侧时另一侧跟随 */
  let suppressScroll = false;
  const syncScroll = (from, to) => {
    if (suppressScroll) {
      suppressScroll = false;
      return;
    }
    const cards = $$(".ev-card", from);
    if (!cards.length) return;
    const fromRect = from.getBoundingClientRect();
    // 找到当前视口顶部所在的那条证据
    let index = 0;
    let found = false;
    let current = null;
    cards.forEach((card, position) => {
      if (!found && card.getBoundingClientRect().bottom > fromRect.top + 4) {
        index = position;
        current = card;
        found = true;
      }
    });
    if (!current) current = cards[cards.length - 1];
    const target = $$(".ev-card", to)[index];
    if (!target) return;
    // 目标列里同一条证据保持相同的相对位置
    const offset = current.getBoundingClientRect().top - fromRect.top;
    const delta = target.getBoundingClientRect().top - to.getBoundingClientRect().top - offset;
    if (Math.abs(delta) < 1) return;
    suppressScroll = true;
    to.scrollTop += delta;
    window.setTimeout(() => { suppressScroll = false; }, 150);
  };
  bodyA.addEventListener("scroll", () => syncScroll(bodyA, bodyB), { passive: true });
  bodyB.addEventListener("scroll", () => syncScroll(bodyB, bodyA), { passive: true });

  await loadItems(true);
  if (view.currentId) {
    highlightActive();
    scrollToEvidence(view.currentId);
    await selectEvidence(view.currentId);
  }
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

function evidenceDetailHtml(evidence) {
  const data = evidence.evidence_data || {};
  const sides = evidence.sides || {};
  const sideA = sidePayloadOf(evidence, "a");
  const sideB = sidePayloadOf(evidence, "b");
  let fields = "";
  if (evidence.evidence_type === "text") {
    const effective = data.effective_similarity != null ? `（有效 ${num(data.effective_similarity)}）` : "";
    const chapterA = [sideA.chapter, sideA.section].filter(Boolean).join(" / ");
    const chapterB = [sideB.chapter, sideB.section].filter(Boolean).join(" / ");
    fields = `
      <dt>相似指标</dt><dd><strong>${num(data.similarity_percent)}%</strong>${effective} · 雷同 ${num(data.matched_chars)} 字</dd>
      <dt>相似度</dt><dd>块级 ${num(data.similarity)}（局部 ${num(data.local_ratio)}）</dd>
      <dt>命中区间</dt><dd>${num(data.match_interval_count, 1)} 处相似片段${data.match_interval_count > 1 ? "（已分别高亮）" : ""}</dd>
      <dt>差异说明</dt><dd class="small">${esc(sides.diff?.summary || "—")}</dd>
      <dt>来源定位</dt><dd class="small">A：${sideSourceLine(sideA, evidence.company_a) || "—"}<br>B：${sideSourceLine(sideB, evidence.company_b) || "—"}</dd>
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
          <td>${quote.source?.file_id ? `<button type="button" class="link-btn" data-open-file="${esc(quote.source.file_id)}" data-page="${num(quote.source.page_index)}">${esc(quote.source.file_name)} 第${quote.source.page_index}页</button>` : "—"}</td></tr>`).join("")}
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

/* 审核表单：确认异常线索是本系统里后果最重的动作，必须带理由并二次确认；
   排除与暂不判断保留轻量路径。撤回走追加记录，不覆盖历史。 */
function reviewFormHtml(evidence) {
  const status = evidence.review_status || "pending";
  const records = evidence.review_records || [];
  const last = records[records.length - 1];
  const revoke = last
    ? `<button class="btn small" data-review-revoke="1" title="以一条新的审核记录把状态改回待审核，历史记录保留">撤回本次审核</button>`
    : "";
  const label = status === "pending" ? "尚未审核" : `当前结论：${REVIEW[status] || status}`;
  return `<div class="detail-block">
    <div class="small" style="font-weight:600">人工审核</div>
    <div class="small muted" style="margin:4px 0 8px">${esc(label)}${last ? ` · 最近一位审核人 ${esc(last.reviewer_name)} · ${fmtDate(last.created_at)}` : ""}</div>
    <label class="small">审核说明<span class="muted">（确认异常线索时必填，将写入导出报告）</span>
      <textarea id="review-comment" placeholder="写清判断依据，例如：已核对纸质原件，两份技术标第 4.2 节为同一底稿"></textarea>
    </label>
    <p id="review-error" class="form-error" role="alert" hidden></p>
    <div class="review-actions">
      <button class="btn small primary" data-review-status="confirmed">确认异常线索</button>
      <button class="btn small" data-review-status="excluded">正常 / 排除</button>
      <button class="btn small" data-review-status="undecided">暂不判断</button>
      ${revoke}
    </div>
    <div class="review-actions">
      <button class="btn small" id="review-next" type="button" disabled>下一条待审核 →</button>
    </div>
    <div class="small muted" style="margin-top:6px">审核记录只追加保存，不覆盖历史；原始识别结果始终保留。</div>
  </div>`;
}

function bindReviewForm(root, evidence, refresh) {
  const commentValue = () => $("#review-comment", root)?.value || "";
  const fail = (message) => {
    const box = $("#review-error", root);
    if (box) { box.hidden = false; box.textContent = message; box.setAttribute("tabindex", "-1"); box.focus(); }
    else toast(message, "error");
  };
  const submit = async (button, status, comment) => {
    await withBusy(button, async () => {
      try {
        await api(`/evidences/${evidence.id}/review`, { method: "POST", body: { status, comment: comment || null } });
        toast(status === "pending" ? "审核结论已撤回" : "审核结论已保存", "success");
        await refresh();
      } catch (error) { toast(error.message, "error"); }
    }, "保存中…");
  };

  $$("[data-review-status]", root).forEach((button) => button.addEventListener("click", () => {
    const status = button.dataset.reviewStatus;
    const comment = commentValue().trim();
    const box = $("#review-error", root);
    if (box) { box.hidden = true; box.textContent = ""; }
    if (status === "confirmed") {
      if (!comment) { fail("确认异常线索前必须填写审核说明：这条结论会进入导出报告，事后需要能复核。"); return; }
      const ok = confirm(`将把这条证据标记为「确认异常线索」，并写入导出报告。\n\n待核对：${evidence.summary}\n\n确认继续？`);
      if (!ok) return;
      submit(button, status, comment);
      return;
    }
    submit(button, status, comment);
  }));

  $$("[data-review-revoke]", root).forEach((button) => button.addEventListener("click", () => {
    if (!confirm("撤回本次审核？\n\n会追加一条「改回待审核」的记录，原审核记录与结论历史都保留。")) return;
    submit(button, "pending", "撤回本次审核");
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
  main.innerHTML = skeletonPage(3);
  const data = await api("/workbench");
  const stats = data.stats || {};
  const risk = data.risk || {};

  const todoRows = (data.todos || []).map((todo) => `
    <tr>
      <td>${pill(todo.severity === "high" ? "high" : todo.severity === "medium" ? "medium" : "low", todo.type_label)}</td>
      <td><a href="#/projects/${esc(todo.project_id)}">${esc(todo.project_name)}</a></td>
      <td>${esc(todo.title)}<div class="small muted">${esc(todo.detail || "")}</div></td>
      <td class="col-secondary">${fmtDate(todo.created_at)}</td>
      <td>${pill(todo.status, todo.status === "failed" ? "需处理" : "待处理")}</td>
      <td><a class="btn small" href="${esc(todo.link)}">去处理</a></td>
    </tr>`).join("");

  const projectRows = (data.recent_projects || []).map((project) => `
    <tr>
      <td><a href="#/projects/${esc(project.id)}"><strong>${esc(project.project_name)}</strong></a>
        <div class="small muted">${esc(project.project_code || "无项目编号")}</div></td>
      <td>${pill(project.status, projectStatusLabel(project))}</td>
      <td class="col-secondary">${num(project.company_count)}</td>
      <td class="col-secondary">${fmtDate(project.last_analysis_at)}</td>
      <td>${num(project.pending_review_count)}</td>
      <td><a class="btn small" href="#/projects/${esc(project.id)}">进入</a></td>
    </tr>`).join("");

  const taskRows = (data.recent_tasks || []).map((task) => `
    <tr>
      <td>#${num(task.task_no)}</td>
      <td><a href="#/projects/${esc(task.project_id)}">${esc(task.project_name)}</a></td>
      <td>${num(task.company_count)}</td>
      <td>${fmtDate(task.created_at)}</td>
      <td class="col-secondary">${fmtDate(task.finished_at)}</td>
      <td>${taskPill(task.status)}</td>
      <td class="mono small col-secondary">${esc(task.algorithm_version || "—")}</td>
      <td class="small muted col-secondary">${esc(task.rule_version_label || "—")}</td>
      <td><a class="btn small" href="${esc(task.link)}">查看</a></td>
    </tr>`).join("");

  const typeIcons = { text: "file", personnel: "user", price: "coins" };

  /* 风险分布：不再用 11 张等大卡片平铺，改为一条可读的分布条 + 明细。
     分布条让「风险集中在哪」一眼可见，卡片墙只能让人逐个相加。 */
  const bySeverity = risk.by_severity || {};
  const severityOrder = ["high", "medium", "low", "none"];
  const severityTotal = severityOrder.reduce((sum, key) => sum + num(bySeverity[key]), 0);
  const severityBar = severityTotal
    ? `<div class="dist-bar" role="img" aria-label="风险等级分布：${severityOrder.map((k) => `${RISK[k]}风险 ${num(bySeverity[k])} 条`).join("，")}">
        ${severityOrder.map((key) => {
          const value = num(bySeverity[key]);
          if (!value) return "";
          return `<span class="dist-seg seg-${key}" style="flex:${value}" title="${RISK[key]}风险 ${value} 条"></span>`;
        }).join("")}
      </div>`
    : `<div class="muted small">暂无风险数据</div>`;
  const severityLegend = severityOrder.map((key) => `
    <span class="dist-legend"><i class="dot seg-${key}"></i>${RISK[key]}风险 <b>${num(bySeverity[key])}</b></span>`).join("");

  const byReview = risk.by_review_status || {};
  const reviewOrder = ["pending", "confirmed", "excluded", "undecided"];
  const reviewTotal = reviewOrder.reduce((sum, key) => sum + num(byReview[key]), 0);
  const reviewedCount = num(byReview.confirmed) + num(byReview.excluded) + num(byReview.undecided);
  const reviewedPercent = reviewTotal ? Math.round((reviewedCount / reviewTotal) * 100) : 0;

  const typeRows = Object.entries(risk.by_type || {}).map(([key, value]) => `
    <div class="dist-row">
      <span class="dist-row-label">${icon(typeIcons[key] || "layers")}${TYPE[key] || key}</span>
      <span class="dist-row-value">${num(value)}</span>
    </div>`).join("");

  main.innerHTML = `
    <div class="page-head">
      <div>
        <h1>工作台</h1>
        <div class="sub">待办优先：先处理需要你决策的事项，再看全局统计</div>
      </div>
      <button class="btn" id="refresh-workbench">${icon("refresh")}刷新</button>
    </div>

    <div class="kpi-strip">
      <button type="button" class="kpi" data-stat="evidence_pending_review">
        <span class="kpi-label">待审核证据</span>
        <span class="kpi-value tone-high">${num(stats.evidence_pending_review)}</span>
        <span class="kpi-foot">需要人工给出结论</span>
      </button>
      <button type="button" class="kpi" data-stat="project_active">
        <span class="kpi-label">进行中项目</span>
        <span class="kpi-value">${num(stats.project_active)}</span>
        <span class="kpi-foot">共 ${num(stats.project_total)} 个项目</span>
      </button>
      <button type="button" class="kpi" data-stat="evidence_total">
        <span class="kpi-label">异常证据总数</span>
        <span class="kpi-value">${num(stats.evidence_total)}</span>
        <span class="kpi-foot">已审 ${reviewedPercent}%</span>
      </button>
      <button type="button" class="kpi" data-stat="task_completed">
        <span class="kpi-label">已完成分析任务</span>
        <span class="kpi-value">${num(stats.task_completed)}</span>
        <span class="kpi-foot">累计比对次数</span>
      </button>
    </div>

    <div class="card">
      <div class="card-head">
        <h2>待办事项</h2>
        <span class="small muted">${(data.todos || []).length} 项待处理</span>
      </div>
      ${todoRows ? `<div class="table-scroll"><table><thead><tr><th>类型</th><th>所属项目</th><th>事项</th><th class="col-secondary">创建时间</th><th>状态</th><th>操作</th></tr></thead><tbody>${todoRows}</tbody></table></div>`
        : `<div class="empty">暂无待办事项。项目资料齐备后，系统会自动开始比对。</div>`}
    </div>

    <div class="grid cols-2">
      <div class="card">
        <div class="card-head"><h2>风险等级分布</h2><span class="small muted">共 ${severityTotal} 条</span></div>
        ${severityBar}
        <div class="dist-legend-row">${severityLegend}</div>
        <div class="card-head" style="margin-top:18px"><h2>按维度</h2></div>
        ${typeRows || `<div class="muted small">暂无数据</div>`}
      </div>
      <div class="card">
        <div class="card-head"><h2>审核进度</h2><span class="small muted">已审 ${reviewedCount} / ${reviewTotal}</span></div>
        <div class="progress-track" role="progressbar" aria-valuemin="0" aria-valuemax="${reviewTotal}" aria-valuenow="${reviewedCount}" aria-label="证据审核进度">
          <div class="progress-fill" style="width:${reviewedPercent}%"></div>
        </div>
        <div class="dist-legend-row">
          ${reviewOrder.map((key) => `<span class="dist-legend"><i class="dot seg-${key}"></i>${REVIEW[key]} <b>${num(byReview[key])}</b></span>`).join("")}
        </div>
        <div class="small muted" style="margin-top:12px">
          ${num(byReview.pending) ? `还有 ${num(byReview.pending)} 条证据没有结论，<a href="#/risks?review_status=pending">去逐条处理 →</a>` : "所有证据都已给出结论。"}
        </div>
      </div>
    </div>

    <div class="card">
      <div class="card-head"><h2>最近项目</h2><a class="small" href="#/projects">全部项目 →</a></div>
      ${projectRows ? `<div class="table-scroll"><table><thead><tr><th>项目</th><th>状态</th><th class="col-secondary">企业数</th><th class="col-secondary">最近分析</th><th>待审核</th><th>操作</th></tr></thead><tbody>${projectRows}</tbody></table></div>`
        : `<div class="empty">暂无项目。</div>`}
    </div>

    <div class="card" id="recent-tasks">
      <div class="card-head"><h2>最近分析任务</h2></div>
      ${taskRows ? `<div class="table-scroll"><table><thead><tr><th>任务</th><th>项目</th><th>企业数</th><th>创建</th><th class="col-secondary">完成</th><th>状态</th><th class="col-secondary">算法版本</th><th class="col-secondary">规则版本</th><th>操作</th></tr></thead><tbody>${taskRows}</tbody></table></div>`
        : `<div class="empty">暂无分析任务。</div>`}
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
      <td><input type="checkbox" data-evidence-check="${esc(item.id)}" data-review-status="${esc(item.review_status)}" aria-label="选择证据 ${esc(item.evidence_code)}"></td>
      <td class="mono small nowrap">${esc(item.evidence_code)}</td>
      <td class="risk-project"><a href="#/projects/${esc(item.project_id)}">${esc(item.project_name)}</a></td>
      <td class="col-secondary">#${num(item.task_no)}</td>
      <td>${pill(item.evidence_type, item.evidence_type_label)}</td>
      <td class="risk-companies">${esc((item.companies || []).join("、"))}</td>
      <td>${riskPill(item.severity)}</td>
      <td class="col-secondary">${fmtDate(item.created_at)}</td>
      <td>${pill(item.review_status, item.review_status_label)}</td>
      <td class="col-secondary">${esc(item.reviewer_name || "—")}</td>
      <td class="nowrap">
        <button class="btn small" data-risk-detail="${esc(item.id)}">详情</button>
        <a class="btn small" href="${esc(item.viewer_link)}">三栏比对</a>
      </td>
    </tr>`).join("");

  const totalPages = Math.max(1, Math.ceil((num(data.total)) / (riskListState.page_size || 20)));
  const activeFilterCount = ["project_id", "company_id", "evidence_type", "severity", "review_status", "reviewer_id", "date_from", "date_to"]
    .filter((key) => riskListState[key]).length;

  main.innerHTML = `
    <div class="page-head">
      <div>
        <h1>风险管理</h1>
        <div class="sub">汇总全部项目的异常证据，共 ${num(data.total)} 条${activeFilterCount ? ` · 已应用 ${activeFilterCount} 个筛选条件` : ""} · 系统输出的是疑似异常线索，不构成违规认定</div>
      </div>
    </div>

    <div class="card">
      <div class="filter-bar filter-bar--risk">
        <div class="filter-field filter-field--select filter-field--wide">
          <label for="risk-project">所属项目</label>
          <select id="risk-project" aria-label="按项目筛选">
            <option value="">全部项目</option>
            ${filters.projects.map((project) => `<option value="${esc(project.id)}" ${riskListState.project_id === project.id ? "selected" : ""}>${esc(project.project_name)}</option>`).join("")}
          </select>
        </div>
        <div class="filter-field filter-field--select filter-field--wide">
          <label for="risk-company">涉及企业</label>
          <select id="risk-company" aria-label="按企业筛选">
            <option value="">全部企业</option>
            ${companyOptions.map((company) => `<option value="${esc(company.company_id)}" ${riskListState.company_id === company.company_id ? "selected" : ""}>${esc(company.company_name)}</option>`).join("")}
          </select>
        </div>
        <div class="filter-field filter-field--select">
          <label for="risk-type">证据类型</label>
          <select id="risk-type" aria-label="按证据类型筛选">
            <option value="">全部类型</option>
            ${Object.entries(TYPE).map(([value, label]) => `<option value="${value}" ${riskListState.evidence_type === value ? "selected" : ""}>${label}</option>`).join("")}
          </select>
        </div>
        <div class="filter-field filter-field--select">
          <label for="risk-severity">风险等级</label>
          <select id="risk-severity" aria-label="按风险等级筛选">
            <option value="">全部等级</option>
            ${Object.entries(RISK).map(([value, label]) => `<option value="${value}" ${riskListState.severity === value ? "selected" : ""}>${label}</option>`).join("")}
          </select>
        </div>
        <div class="filter-field filter-field--select">
          <label for="risk-review">审核状态</label>
          <select id="risk-review" aria-label="按审核状态筛选">
            <option value="">全部审核状态</option>
            ${Object.entries(REVIEW).map(([value, label]) => `<option value="${value}" ${riskListState.review_status === value ? "selected" : ""}>${label}</option>`).join("")}
          </select>
        </div>
        <div class="filter-field filter-field--select">
          <label for="risk-reviewer">审核人</label>
          <select id="risk-reviewer" aria-label="按审核人筛选">
            <option value="">全部审核人</option>
            ${(filters.reviewers || []).map((reviewer) => `<option value="${esc(reviewer.id)}" ${riskListState.reviewer_id === reviewer.id ? "selected" : ""}>${esc(reviewer.name)}</option>`).join("")}
          </select>
        </div>
        <div class="filter-field filter-field--date">
          <label for="risk-from">发现时间</label>
          <div class="filter-date-range">
            <input type="date" id="risk-from" aria-label="发现时间起" value="${esc(riskListState.date_from)}" title="发现时间起">
            <span class="sep" aria-hidden="true">至</span>
            <input type="date" id="risk-to" aria-label="发现时间止" value="${esc(riskListState.date_to)}" title="发现时间止">
          </div>
        </div>
        <div class="filter-actions">
          <button class="btn primary" id="risk-query">${icon("filter")}查询</button>
          <button class="btn" id="risk-reset">${icon("refresh")}重置</button>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="row" style="margin-bottom:12px">
        <label class="small muted"><input type="checkbox" id="risk-select-all"> 全选本页</label>
        <span class="small muted" id="risk-selected">已选 0 条</span>
        <span class="spacer"></span>
        <span class="small muted">V1.0 仅支持批量标记为「待进一步核查」；确认异常/排除请逐条审核</span>
        <button class="btn small primary" id="risk-batch" disabled>批量标记为待进一步核查</button>
        <button class="btn small" id="risk-export">${icon("download")}导出当前筛选结果</button>
      </div>
      <div class="small muted" style="margin:-4px 0 12px">
        批量确认 / 批量排除按审计要求不提供：每条证据的结论必须逐条给出并写明依据。
      </div>
      ${rows ? `<div class="table-scroll"><table><thead><tr>
          <th style="width:32px"></th><th>证据编号</th><th>所属项目</th><th class="col-secondary">任务</th><th>证据类型</th>
          <th>涉及企业</th><th>风险等级</th><th class="col-secondary">发现时间</th><th>审核状态</th><th class="col-secondary">审核人</th><th>操作</th>
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
    const reviewed = checked.filter((box) => (box.dataset.reviewStatus || "pending") !== "pending");
    $("#risk-selected").textContent = reviewed.length
      ? `已选 ${checked.length} 条（其中 ${reviewed.length} 条已有审核结论）`
      : `已选 ${checked.length} 条`;
    $("#risk-batch").disabled = checked.length === 0;
  };
  $$("[data-evidence-check]").forEach((box) => box.addEventListener("change", updateSelection));
  $("#risk-select-all")?.addEventListener("change", (event) => {
    $$("[data-evidence-check]").forEach((box) => { box.checked = event.target.checked; });
    updateSelection();
  });

  const batchBtn = $("#risk-batch");
  batchBtn?.addEventListener("click", () => {
    const selected = $$("[data-evidence-check]").filter((box) => box.checked);
    const ids = selected.map((box) => box.dataset.evidenceCheck);
    if (!ids.length) return;
    const reviewedCount = selected.filter((box) => (box.dataset.reviewStatus || "pending") !== "pending").length;
    const warning = reviewedCount
      ? `<div class="warn-box" style="margin-top:10px">其中 <strong>${reviewedCount}</strong> 条已有审核结论（确认异常 / 正常排除 / 暂不判断）。批量标记会覆盖这些结论，历史审核记录仍会保留。</div>`
      : "";
    const modal = openModal({
      title: "批量标记证据",
      body: `<p style="margin:0">确认把选中的 <strong>${ids.length}</strong> 条证据标记为「待进一步核查」？</p>
        ${warning}
        ${reviewedCount ? `<label class="small" style="display:flex;align-items:center;gap:6px;margin-top:10px">
          <input type="checkbox" id="batch-skip-reviewed" checked> 跳过已有审核结论（推荐）
        </label>` : ""}`,
      footer: `<button type="button" class="btn" data-close>取消</button>
        <button type="button" class="btn primary" id="batch-confirm">确认标记</button>`,
    });
    $("#batch-confirm", modal.root)?.addEventListener("click", () => {
      const skip = reviewedCount ? ($("#batch-skip-reviewed", modal.root)?.checked ?? true) : false;
      modal.close();
      withBusy(batchBtn, async () => {
        try {
          const result = await api("/risks/batch-review", {
            method: "POST",
            body: { evidence_ids: ids, status: "undecided", skip_reviewed: skip },
          });
          const skipped = (result.skipped_reviewed || []).length;
          toast(
            skipped ? `已标记 ${result.updated} 条，跳过 ${skipped} 条已有审核结论` : `已处理 ${result.updated} 条证据`,
            "success",
          );
          rerender();
        } catch (error) { toast(error.message, "error"); }
      }, "处理中…");
    });
  });

  $$("[data-risk-detail]").forEach((button) => button.addEventListener("click", () => openRiskDetail(button.dataset.riskDetail)));

  /* 导出：后端报告是项目级的，先让用户确认导出哪个项目，
     避免导出一个「看不出筛选口径」的文件。 */
  const exportBtn = $("#risk-export");
  exportBtn?.addEventListener("click", () => {
    const projectId = riskListState.project_id;
    if (!projectId) {
      toast("请先在上方选择「所属项目」再导出：报告按项目生成。", "error");
      return;
    }
    const projectName = (filters.projects || []).find((project) => project.id === projectId)?.project_name || "项目";
    const scope = [
      riskListState.evidence_type && `类型：${TYPE[riskListState.evidence_type] || riskListState.evidence_type}`,
      riskListState.severity && `等级：${RISK[riskListState.severity] || riskListState.severity}`,
      riskListState.review_status && `审核：${REVIEW[riskListState.review_status] || riskListState.review_status}`,
      riskListState.company_id && "已限定企业",
      (riskListState.date_from || riskListState.date_to) && `时间：${riskListState.date_from || "不限"} 至 ${riskListState.date_to || "不限"}`,
    ].filter(Boolean).join("；") || "全部证据";
    if (!confirm(`将导出「${projectName}」的项目分析报告（含当前任务的全部证据）。\n\n当前筛选：${scope}\n\n报告为项目级汇总，不会只包含被筛出的条目。确认导出？`)) return;
    withBusy(exportBtn, async () => {
      try {
        const blob = await api(`/projects/${projectId}/reports/excel`, { blob: true });
        const url = URL.createObjectURL(blob);
        const link = document.createElement("a");
        link.href = url;
        link.download = `${projectName}-分析报告.xlsx`;
        document.body.appendChild(link); link.click(); link.remove();
        URL.revokeObjectURL(url);
        toast("报告已导出", "success");
      } catch (error) { toast(error.message, "error"); }
    }, "生成中…");
  });
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

/* ---------- 检测规则：维度切换 + 参数分组 ---------- */

/* 系统管理里选中的检测维度 */
let systemRuleDim = "text";

/* 脱敏占位符：与后端 rule_service.SECRET_MASK 保持一致 */
const RULE_SECRET_MASK = "********";

/* 各维度的参数分组：把一长串阈值拆成可读的板块 */
const RULE_PARAM_GROUPS = {
  text: [
    { title: "相似度判定", keys: ["text_similarity", "text_min_match_chars", "text_min_block_chars", "tender_exclusion_ratio"] },
    { title: "文本分块", keys: ["text_chunk_target_min", "text_chunk_target_max", "text_chunk_max_chars", "text_chunk_min_chars"] },
    { title: "算法参数", keys: ["text_shingle_size", "text_minhash_num_perm", "text_lsh_threshold", "max_candidate_pairs", "max_matches_per_pair"] },
    { title: "风险分级", keys: ["risk_text_high_count", "risk_text_high_similarity", "risk_text_medium_count", "risk_text_medium_similarity"] },
  ],
  personnel: [
    { title: "姓名匹配", keys: ["similar_name_ratio", "name_match_modes", "key_positions"] },
    { title: "风险分级", keys: ["risk_personnel_high_count", "risk_personnel_medium_count"] },
  ],
  price: [
    { title: "报价偏差", keys: ["price_cv", "price_close_deviation", "price_sequence_tolerance"] },
    { title: "尾数特征", keys: ["special_tail", "tail_concentration_min_count"] },
  ],
  ocr: [
    { title: "百度 OCR 凭据", keys: ["baidu_api_key", "baidu_secret_key"] },
    { title: "识别参数", keys: ["baidu_language_type", "baidu_detect_direction", "baidu_probability", "ocr_timeout_seconds", "ocr_max_file_mb"] },
  ],
};

/* 参数说明：规则页只解释「这个参数管什么」，具体口径见各维度文档 */
const RULE_PARAM_HINTS = {
  text_similarity: "块级相似度达到该值即视为雷同",
  text_min_match_chars: "相似片段的最短字数",
  text_min_block_chars: "参与比较的文本块最短长度",
  tender_exclusion_ratio: "命中招标文件公共内容的比例达到该值即排除",
  text_chunk_target_min: "文本块目标长度下限",
  text_chunk_target_max: "文本块目标长度上限",
  text_chunk_max_chars: "单个文本块的长度上限",
  text_chunk_min_chars: "低于该长度的段落会与相邻段落合并",
  text_shingle_size: "字符级 Shingle 长度",
  text_minhash_num_perm: "MinHash 签名数，越大越精确",
  text_lsh_threshold: "LSH 召回阈值，越低召回越多",
  max_candidate_pairs: "单个企业组合的候选文本块对上限",
  max_matches_per_pair: "单个企业组合保留的最大匹配数",
  risk_text_high_count: "达到该条数判为高风险",
  risk_text_high_similarity: "同时满足该相似度才判为高风险",
  risk_text_medium_count: "达到该条数判为中风险",
  risk_text_medium_similarity: "同时满足该相似度才判为中风险",
  similar_name_ratio: "姓名相似度达到该值视为疑似同名",
  name_match_modes: "姓名匹配方式",
  key_positions: "纳入比对的关键岗位",
  risk_personnel_high_count: "重复人数达到该值判为高风险",
  risk_personnel_medium_count: "重复人数达到该值判为中风险",
  price_cv: "报价变异系数阈值",
  price_close_deviation: "报价偏差率小于该值视为异常接近",
  price_sequence_tolerance: "等差数列的允许误差",
  special_tail: "纳入特殊尾数识别的取值",
  tail_concentration_min_count: "同一尾数集中出现的最少家数",
  ocr_enabled: "只对没有文本层的扫描页生效",
  ocr_provider: "OCR 服务的调用方式",
  baidu_api_key: "百度智能云应用的 API Key",
  baidu_secret_key: "百度智能云应用的 Secret Key",
  baidu_language_type: "识别语言类型",
  baidu_detect_direction: "检测图片朝向",
  baidu_probability: "返回每行识别结果的置信度",
  ocr_timeout_seconds: "单页识别超时时间",
  ocr_max_file_mb: "超过该大小的 PDF 会自动拆分成多个文件逐份上传；百度接口 base64 后上限 8MB，原始文件最大 6MB",
};

/* 参数在编辑表单里的呈现方式 */
const RULE_PARAM_KINDS = {
  ocr_enabled: "switch",
  ocr_provider: "provider",
  baidu_api_key: "secret",
  baidu_secret_key: "secret",
  baidu_language_type: "language",
  baidu_detect_direction: "switch",
  baidu_probability: "switch",
  ocr_timeout_seconds: "number",
  ocr_max_file_mb: "size",
};

const OCR_LANGUAGE_OPTIONS = [
  ["CHN_ENG", "中英文混合"],
  ["ENG", "英文"],
  ["JAP", "日语"],
  ["KOR", "韩语"],
  ["FRE", "法语"],
  ["SPA", "西班牙语"],
  ["POR", "葡萄牙语"],
  ["GER", "德语"],
  ["ITA", "意大利语"],
  ["RUS", "俄语"],
];

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
    ocr_enabled: "启用 OCR 识别",
    ocr_provider: "OCR 调用方式",
    baidu_api_key: "百度 API Key",
    baidu_secret_key: "百度 Secret Key",
    baidu_language_type: "识别语言",
    baidu_detect_direction: "检测图片朝向",
    baidu_probability: "返回识别置信度",
    ocr_timeout_seconds: "单页识别超时（秒）",
    ocr_max_file_mb: "单次 OCR 文件大小上限（MB）",
  }[key] || key;
}

function ruleParamValue(value) {
  if (typeof value === "boolean") return value ? "启用" : "关闭";
  if (Array.isArray(value)) return value.join("、");
  if (typeof value === "number") return String(value);
  return String(value ?? "—");
}

/* 敏感参数在清单里只显示「已配置 / 未配置」，不显示真实值 */
function ruleParamDisplay(key, value) {
  if (key === "baidu_api_key" || key === "baidu_secret_key") {
    return String(value || "").trim() ? "已配置" : "未配置";
  }
  return ruleParamValue(value);
}

/* 数值参数的安全区间。缺省时按当前值推断，保证合法输入不受影响。 */
const RULE_PARAM_BOUND = {
  text_similarity: { min: 0, max: 1, step: 0.01 },
  text_lsh_threshold: { min: 0, max: 1, step: 0.01 },
  tender_exclusion_ratio: { min: 0, max: 1, step: 0.01 },
  price_cv: { min: 0, max: 1, step: 0.001 },
  price_close_deviation: { min: 0, max: 1, step: 0.001 },
  price_sequence_tolerance: { min: 0, max: 1, step: 0.001 },
  similar_name_ratio: { min: 0, max: 1, step: 0.01 },
  risk_text_high_similarity: { min: 0, max: 1, step: 0.01 },
  risk_text_medium_similarity: { min: 0, max: 1, step: 0.01 },
  text_min_match_chars: { min: 1, max: 5000, step: 1 },
  text_min_block_chars: { min: 1, max: 5000, step: 1 },
  text_chunk_target_min: { min: 1, max: 10000, step: 1 },
  text_chunk_target_max: { min: 1, max: 10000, step: 1 },
  text_chunk_max_chars: { min: 1, max: 20000, step: 1 },
  text_chunk_min_chars: { min: 1, max: 5000, step: 1 },
  text_shingle_size: { min: 1, max: 10, step: 1 },
  text_minhash_num_perm: { min: 16, max: 1024, step: 16 },
  max_candidate_pairs: { min: 1, max: 1000000, step: 100 },
  max_matches_per_pair: { min: 1, max: 10000, step: 1 },
  risk_text_high_count: { min: 0, max: 1000, step: 1 },
  risk_text_medium_count: { min: 0, max: 1000, step: 1 },
  risk_personnel_high_count: { min: 0, max: 1000, step: 1 },
  risk_personnel_medium_count: { min: 0, max: 1000, step: 1 },
  tail_concentration_min_count: { min: 0, max: 100, step: 1 },
};

function ruleParamBound(key, value) {
  const bound = RULE_PARAM_BOUND[key];
  if (bound) return bound;
  const magnitude = Math.max(Math.abs(Number(value) || 0), 1);
  return { min: 0, max: magnitude * 100, step: Number.isInteger(value) ? 1 : 0.01 };
}

async function renderSystemRules(container) {
  const data = await api("/system/rules");
  const isAdmin = state.user && state.user.role === "admin";
  const dimensions = data.dimensions || [];
  const versionsByDimension = {};
  (data.versions || []).forEach((version) => {
    (versionsByDimension[version.dimension] = versionsByDimension[version.dimension] || []).push(version);
  });
  const dimension = dimensions.find((item) => item.dimension === systemRuleDim) || dimensions[0];
  if (!dimension) {
    container.innerHTML = `<div class="empty">暂无检测规则。</div>`;
    return;
  }
  systemRuleDim = dimension.dimension;
  const active = dimension.active;
  const params = (active && active.params) || dimension.default_params || {};
  const history = versionsByDimension[dimension.dimension] || [];

  const dimNav = dimensions.map((item) => {
    const isCurrent = item.dimension === systemRuleDim;
    const version = item.active;
    return `<button type="button" class="rule-dim${isCurrent ? " active" : ""}" data-dim="${esc(item.dimension)}" aria-pressed="${isCurrent}">
      <span class="name">${esc(item.label)}</span>
      <span class="meta">${version ? `v${num(version.version_no)} · ${esc(version.status_label)}` : "未配置"}</span>
    </button>`;
  }).join("");

  const groups = (RULE_PARAM_GROUPS[systemRuleDim] || []).map((group) => {
    const rows = group.keys
      .filter((key) => key in params)
      .map((key) => `
        <dt>${esc(ruleParamLabel(key))}<span class="hint">${esc(RULE_PARAM_HINTS[key] || "")}</span></dt>
        <dd>${esc(ruleParamDisplay(key, params[key]))}</dd>`).join("");
    return rows ? `<section class="rule-group"><h3>${esc(group.title)}</h3><dl class="kv">${rows}</dl></section>` : "";
  }).join("");

  const ocrPanel = systemRuleDim === "ocr" ? `
    <div class="rule-switch-row">
      <label class="switch">
        <input type="checkbox" id="ocr-enabled" ${params.ocr_enabled ? "checked" : ""} ${isAdmin ? "" : "disabled"}>
        <span class="switch-track" aria-hidden="true"></span>
        <span class="switch-text">启用 OCR 识别</span>
      </label>
      <div class="small muted">启用后，PDF 中没有文本层的扫描页会交给 OCR 服务识别，识别结果与原生文本一样参与雷同比对。</div>
    </div>
    <div class="rule-ocr-bar">
      <span class="small">调用方式 <strong>百度智能云 OCR</strong></span>
      <a class="small" href="https://cloud.baidu.com/doc/OCR/s/vk3h7y58v" target="_blank" rel="noopener">接口文档</a>
      <span class="spacer"></span>
      <span class="small muted">凭据 ${params.baidu_api_key ? "已配置" : "未配置"}</span>
      <span class="small muted">单次上传 ≤ ${num(params.ocr_max_file_mb, 6)}MB，超出自动拆分</span>
      ${isAdmin ? `<button type="button" class="btn small" id="ocr-test">${icon("refresh")}测试连接</button>` : ""}
    </div>` : "";

  const historyRows = history.map((version) => {
    const changed = (version.changed || []).map((item) => `
      <tr><td>${esc(ruleParamLabel(item.key))}</td>
        <td class="mono small">${esc(ruleParamDisplay(item.key, item.before))}</td>
        <td class="mono small">${esc(ruleParamDisplay(item.key, item.after))}</td></tr>`).join("");
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

  container.innerHTML = `
    <div class="rule-layout">
      <nav class="rule-dims" role="tablist" aria-label="检测规则维度">
        ${dimNav}
      </nav>
      <div class="rule-main">
        <div class="card">
          <div class="row">
            <h2 style="margin:0">${esc(dimension.label)}</h2>
            <span class="spacer"></span>
            ${active ? `<span class="small muted">当前生效 v${num(active.version_no)} · ${fmtDate(active.activated_at)} · ${esc(active.created_by_name || "系统")}</span>` : `<span class="small muted">尚未创建规则版本</span>`}
            ${isAdmin ? `<button class="btn small primary" data-new-rule="${esc(dimension.dimension)}">${icon("plus")}新建版本</button>` : ""}
          </div>
          ${ocrPanel}
          ${groups ? `<div class="rule-groups">${groups}</div>` : `<div class="muted small" style="margin-top:12px">该维度暂无参数。</div>`}
          ${(active && (active.not_enforced_params || []).length) ? `<div class="warn-box small">以下参数在 V1.0 仅记录与展示，尚未接入检测引擎：${esc((active.not_enforced_params || []).map(ruleParamLabel).join("、"))}</div>` : ""}
        </div>
        <div class="card">
          <div class="row" style="margin-bottom:10px">
            <h2 style="margin:0">版本历史</h2>
            <span class="spacer"></span>
            <span class="small muted">共 ${history.length} 个版本 · 修改需填写原因并确认生效</span>
          </div>
          ${historyRows ? `<table><thead><tr><th>版本</th><th>状态</th><th>修改人</th><th>修改时间</th><th>修改原因</th><th>变更前后</th></tr></thead><tbody>${historyRows}</tbody></table>` : `<div class="small muted">暂无历史版本</div>`}
        </div>
      </div>
    </div>`;

  $$("[data-dim]", container).forEach((button) => button.addEventListener("click", () => {
    systemRuleDim = button.dataset.dim;
    renderSystemRules(container);
  }));

  $$("[data-new-rule]", container).forEach((button) => button.addEventListener("click", () => {
    openRuleVersionModal(dimension, data, container);
  }));

  const ocrToggle = $("#ocr-enabled", container);
  ocrToggle?.addEventListener("change", async () => {
    const enabled = ocrToggle.checked;
    ocrToggle.disabled = true;
    try {
      await api("/system/rules/versions", {
        method: "POST",
        body: {
          dimension: "ocr",
          params: { ...params, ocr_enabled: enabled },
          change_reason: enabled ? "启用 OCR 识别" : "停用 OCR 识别",
          activate: true,
          confirmed: true,
        },
      });
      toast(enabled ? "已启用 OCR 识别" : "已停用 OCR 识别", "success");
    } catch (error) {
      toast(error.message, "error");
      ocrToggle.checked = !enabled;
    }
    await renderSystemRules(container);
  });

  const testBtn = $("#ocr-test", container);
  testBtn?.addEventListener("click", () => withBusy(testBtn, async () => {
    try {
      const result = await api("/system/ocr/test", { method: "POST", body: {} });
      toast(result.message, "success");
    } catch (error) { toast(error.message, "error"); }
  }, "测试中…"));
}

/* 编辑表单里的一个参数控件 */
function ruleParamField(key, value, providers) {
  const label = ruleParamLabel(key);
  const hint = RULE_PARAM_HINTS[key] || "";
  const kind = RULE_PARAM_KINDS[key]
    || (typeof value === "boolean" ? "switch"
      : (typeof value === "number" ? "number" : (Array.isArray(value) ? "list" : "text")));
  if (kind === "switch") {
    return `<label class="switch-row">
      <input type="checkbox" name="param" data-param="${esc(key)}" ${value ? "checked" : ""}>
      <span class="switch-track" aria-hidden="true"></span>
      <span class="switch-text">${esc(label)}</span>
    </label>`;
  }
  if (kind === "secret") {
    return `<label class="small">${esc(label)}
      <input type="password" name="param" data-param="${esc(key)}" data-kind="secret" value="" autocomplete="new-password"
        placeholder="${value ? "已配置，留空表示不修改" : "请输入百度智能云凭据"}">
      <span class="field-hint">${esc(hint)}${value ? " · 已配置，留空表示不修改" : ""}
        <button type="button" class="link-btn" data-clear-secret>清除</button>
      </span>
    </label>`;
  }
  if (kind === "provider") {
    const options = (providers || []).map((item) =>
      `<option value="${esc(item.value)}" ${item.value === value ? "selected" : ""}>${esc(item.label)}</option>`).join("");
    return `<label class="small">${esc(label)}
      <select name="param" data-param="${esc(key)}" data-kind="provider">${options}</select>
      <span class="field-hint">${esc(hint)}</span>
    </label>`;
  }
  if (kind === "language") {
    const options = OCR_LANGUAGE_OPTIONS.map(([code, text]) =>
      `<option value="${code}" ${code === value ? "selected" : ""}>${text}（${code}）</option>`).join("");
    return `<label class="small">${esc(label)}
      <select name="param" data-param="${esc(key)}" data-kind="language">${options}</select>
      <span class="field-hint">${esc(hint)}</span>
    </label>`;
  }
  if (kind === "size") {
    return `<label class="small">${esc(label)}<input type="number" min="1" max="6" step="1" name="param" data-param="${esc(key)}" value="${esc(value)}" data-kind="number"><span class="field-hint">${esc(hint)}</span></label>`;
  }
  if (kind === "number") {
    /* 给数值参数一个安全区间，越界由提交前的 validate() 拦下 */
    const bound = ruleParamBound(key, value);
    return `<label class="small">${esc(label)}<input type="number" min="${bound.min}" max="${bound.max}" step="${bound.step}" name="param" data-param="${esc(key)}" value="${esc(value)}" data-kind="number"><span class="field-hint">${esc(hint)}</span></label>`;
  }
  if (kind === "list") {
    return `<label class="small">${esc(label)}（逗号分隔）<input type="text" name="param" data-param="${esc(key)}" value="${esc((value || []).join(","))}" data-kind="list"><span class="field-hint">${esc(hint)}</span></label>`;
  }
  return `<label class="small">${esc(label)}<input type="text" name="param" data-param="${esc(key)}" value="${esc(value)}"><span class="field-hint">${esc(hint)}</span></label>`;
}

function openRuleVersionModal(dimension, data, container) {
  if (!dimension) return;
  const active = dimension.active;
  const params = active ? active.params : dimension.default_params;
  /* 参数键一律走 data-param（值在 value 里）：用 value 当键会把 {text_similarity: 0.6}
     提交成 {"0.6": 0.6}，阈值被静默写坏。 */
  const fields = Object.entries(params || {}).map(([key, value]) =>
    ruleParamField(key, value, data?.ocr_providers)).join("");

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
      ${dimension.dimension === "ocr" ? `<button type="button" class="btn" id="rule-ocr-test">${icon("refresh")}测试连接</button>` : ""}
      <button type="button" class="btn" data-close>取消</button>
      <button type="button" class="btn" id="rule-save-draft">保存为待生效</button>
      <button type="button" class="btn primary" id="rule-save-active">保存并生效</button>`,
  });

  const collect = () => {
    const result = {};
    $$("[name=param]", modal.root).forEach((input) => {
      const key = input.dataset.param;
      if (!key) return;
      if (input.type === "checkbox") { result[key] = input.checked; return; }
      if (input.dataset.kind === "number") {
        /* 空输入不再静默变成 0，交给 validate() 拦截 */
        const raw = String(input.value ?? "").trim();
        result[key] = raw === "" ? null : Number(raw);
        return;
      }
      if (input.dataset.kind === "list") { result[key] = input.value.split(/[,，]/).map((item) => item.trim()).filter(Boolean); return; }
      if (input.dataset.kind === "secret") {
        // 留空 = 保留原值（回传脱敏占位符）；点「清除」才真正清空
        result[key] = input.dataset.cleared === "1" ? "" : (input.value.trim() || RULE_SECRET_MASK);
        return;
      }
      result[key] = input.value;
    });
    return result;
  };

  $$("[data-clear-secret]", modal.root).forEach((button) => button.addEventListener("click", () => {
    const input = button.closest("label").querySelector("input");
    if (!input) return;
    input.dataset.cleared = "1";
    input.value = "";
    input.placeholder = "保存后将清空该凭据";
    button.disabled = true;
  }));

  /* 提交前逐项校验：空值、非数值、越界都当场指出，不让坏阈值落库 */
  const validate = (payload) => {
    const problems = [];
    $$("[name=param]", modal.root).forEach((input) => {
      const key = input.dataset.param;
      if (!key) return;
      const label = ruleParamLabel(key);
      if (input.dataset.kind === "number") {
        const value = payload[key];
        if (value === null || value === undefined || value === "" || Number.isNaN(value)) {
          problems.push(`「${label}」不能为空`);
          return;
        }
        const bound = ruleParamBound(key, value);
        if (value < bound.min || value > bound.max) {
          problems.push(`「${label}」应在 ${bound.min}–${bound.max} 之间`);
        }
      } else if (input.dataset.kind === "list") {
        if (!Array.isArray(payload[key]) || payload[key].length === 0) {
          problems.push(`「${label}」至少填写一项`);
        }
      }
    });
    return problems;
  };

  const save = async (activate) => {
    const reason = $("#rule-reason", modal.root)?.value || "";
    if (!reason.trim()) { toast("请填写修改原因", "error"); return; }
    if (activate && !$("#rule-confirm", modal.root)?.checked) { toast("生效需要勾选生效确认", "error"); return; }
    const payload = collect();
    const problems = validate(payload);
    if (problems.length) { toast(problems.slice(0, 2).join("；"), "error"); return; }
    try {
      await api("/system/rules/versions", {
        method: "POST",
        body: { dimension: dimension.dimension, params: payload, change_reason: reason, activate, confirmed: activate },
      });
      toast(activate ? "规则版本已生效" : "规则版本已保存为待生效", "success");
      modal.close();
      if (container) renderSystemRules(container);
      else renderSystem(null);
    } catch (error) { toast(error.message, "error"); }
  };
  const draftBtn = $("#rule-save-draft", modal.root);
  const activeBtn = $("#rule-save-active", modal.root);
  draftBtn?.addEventListener("click", () => withBusy(draftBtn, () => save(false), "保存中…"));
  activeBtn?.addEventListener("click", () => withBusy(activeBtn, () => save(true), "保存中…"));

  const testBtn = $("#rule-ocr-test", modal.root);
  testBtn?.addEventListener("click", () => withBusy(testBtn, async () => {
    const values = collect();
    try {
      const result = await api("/system/ocr/test", {
        method: "POST",
        body: { api_key: values.baidu_api_key || "", secret_key: values.baidu_secret_key || "" },
      });
      toast(result.message, "success");
    } catch (error) { toast(error.message, "error"); }
  }, "测试中…"));
}

async function renderSystemLogs(container, isAdmin) {
  if (!isAdmin) {
    container.innerHTML = `<div class="info-box">操作日志需要系统管理员权限。</div>`;
    return;
  }
  container.innerHTML = skeletonBlock(6);
  const params = new URLSearchParams();
  Object.entries(logFilterState).forEach(([key, value]) => {
    if (key === "page" || key === "page_size") return;
    if (value) params.set(key, value);
  });
  const logPageSize = 50;
  params.set("page", String(logFilterState.page || 1));
  params.set("page_size", String(logPageSize));
  const [options, data] = await Promise.all([
    api("/system/log-options"),
    api(`/system/logs?${params.toString()}`),
  ]);
  const logTotalPages = Math.max(1, Math.ceil(num(data.total) / logPageSize));
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
      <div class="filter-bar filter-bar--logs" style="margin-bottom:12px">
        <div class="filter-field filter-field--select">
          <label for="log-action">操作类型</label>
          <select id="log-action" aria-label="按操作类型筛选">
            <option value="">全部操作</option>
            ${(options.actions || []).map((action) => `<option value="${esc(action.value)}" ${logFilterState.action === action.value ? "selected" : ""}>${esc(action.label)}</option>`).join("")}
          </select>
        </div>
        <div class="filter-field filter-field--select">
          <label for="log-result">操作结果</label>
          <select id="log-result" aria-label="按操作结果筛选">
            <option value="">全部结果</option>
            ${(options.results || []).map((item) => `<option value="${esc(item.value)}" ${logFilterState.result === item.value ? "selected" : ""}>${esc(item.label)}</option>`).join("")}
          </select>
        </div>
        <div class="filter-field filter-field--select">
          <label for="log-source">操作来源</label>
          <select id="log-source" aria-label="按操作来源筛选">
            <option value="">全部来源</option>
            ${(options.sources || []).map((item) => `<option value="${esc(item.value)}" ${logFilterState.source === item.value ? "selected" : ""}>${esc(item.label)}</option>`).join("")}
          </select>
        </div>
        <div class="filter-field filter-field--select">
          <label for="log-actor">操作人</label>
          <select id="log-actor" aria-label="按操作人筛选">
            <option value="">全部操作人</option>
            ${(options.actors || []).map((actor) => `<option value="${esc(actor.id)}" ${logFilterState.actor_id === actor.id ? "selected" : ""}>${esc(actor.name)}</option>`).join("")}
          </select>
        </div>
        <div class="filter-field filter-field--date">
          <label for="log-from">操作时间</label>
          <div class="filter-date-range">
            <input type="date" id="log-from" aria-label="开始时间" value="${esc(logFilterState.date_from)}" title="开始时间">
            <span class="sep" aria-hidden="true">至</span>
            <input type="date" id="log-to" aria-label="结束时间" value="${esc(logFilterState.date_to)}" title="结束时间">
          </div>
        </div>
        <div class="filter-actions">
          <button class="btn primary" id="log-query">${icon("search")}查询</button>
          <button class="btn" id="log-reset">${icon("refresh")}重置</button>
        </div>
      </div>
      ${rows ? `<div class="table-scroll"><table><thead><tr>
        <th>操作人</th><th>操作时间</th><th>操作类型</th><th>操作对象</th><th>操作结果</th><th>操作来源</th><th>变更前后</th>
      </tr></thead><tbody>${rows}</tbody></table></div>` : `<div class="empty">暂无操作日志。</div>`}
      <div class="row" style="margin-top:12px">
        <span class="small muted">第 ${num(data.page) || 1} / ${logTotalPages} 页，共 ${num(data.total)} 条</span>
        <span class="spacer"></span>
        <button class="btn small" id="log-prev" ${(num(data.page) || 1) <= 1 ? "disabled" : ""}>上一页</button>
        <button class="btn small" id="log-next" ${(num(data.page) || 1) >= logTotalPages ? "disabled" : ""}>下一页</button>
      </div>
    </div>`;

  const bind = (selector, key) => {
    const element = $(selector);
    if (element) element.addEventListener("change", () => {
      logFilterState[key] = element.value;
      logFilterState.page = 1;
      renderSystemLogs(container, isAdmin);
    });
  };
  bind("#log-action", "action");
  bind("#log-result", "result");
  bind("#log-source", "source");
  bind("#log-actor", "actor_id");
  bind("#log-from", "date_from");
  bind("#log-to", "date_to");
  const logQueryBtn = $("#log-query");
  logQueryBtn.addEventListener("click", () => withBusy(logQueryBtn, () => renderSystemLogs(container, isAdmin), "查询中…"));
  $("#log-prev")?.addEventListener("click", () => {
    logFilterState.page = Math.max(1, (num(data.page) || 1) - 1);
    renderSystemLogs(container, isAdmin);
  });
  $("#log-next")?.addEventListener("click", () => {
    logFilterState.page = Math.min(logTotalPages, (num(data.page) || 1) + 1);
    renderSystemLogs(container, isAdmin);
  });
  $("#log-reset").addEventListener("click", () => {
    logFilterState = { action: "", result: "", source: "", actor_id: "", date_from: "", date_to: "", page: 1, page_size: 50 };
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
  const username = $("#login-username")?.value.trim() || "";
  const password = $("#login-password")?.value || "";
  if (!username || !password) {
    const message = "请输入用户名和密码";
    if (errorBox) { errorBox.hidden = false; errorBox.textContent = message; errorBox.focus(); }
    return;
  }
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

/* 原始文件统一走短期票据打开（委托绑定，适配动态渲染的按钮） */
document.addEventListener("click", (event) => {
  const trigger = event.target.closest("[data-open-file]");
  if (!trigger) return;
  event.preventDefault();
  openFileTab(trigger.dataset.openFile, trigger.dataset.page);
});

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
  } catch (error) {
    /* 只有明确的 401 才清除登录态；网络抖动/网关异常时保留令牌并允许重试 */
    if (error.status === 401) {
      logout();
      return;
    }
    showApp();
    const main = $("#main");
    if (main) {
      main.innerHTML = `<div class="error-box" role="alert">
        <div style="margin-bottom:10px">${esc(error.message || "网络异常，暂时无法连接服务")}</div>
        <div class="row">
          <button class="btn primary" id="boot-retry">重试</button>
          <button class="btn" id="boot-logout">退出登录</button>
        </div>
      </div>`;
      $("#boot-retry")?.addEventListener("click", () => boot());
      $("#boot-logout")?.addEventListener("click", logout);
    }
  }
})();
