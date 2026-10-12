/* Idea2App 多项目工作台前端。
 *
 * 架构:单一 state 持「项目注册表」+「每个 scope 一个会话」,DOM 只有一套视图,按当前
 * scope 渲染。事件按 ev.project_id 路由到对应会话;非当前 scope 的事件只更新结构化
 * 状态(messages/docs/phase/tokens),切回时重放,不写 DOM。
 *
 * scope 概念:
 *   'global'   -> 左侧 Chat(全局随便聊),右侧同一套消息视图
 *   <project_id> -> 项目视图:header(名称/目录/阶段/启停/完成) + chat/prd/spec 三标签
 */
"use strict";

/* ---------------- 常量 / 工具 ---------------- */
const WS_URL = (location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws";

const PHASE_TEXT = {
  idea: "想法", prd_draft: "PRD 草稿", prd_approved: "PRD 已批",
  spec_draft: "SPEC 草稿", spec_approved: "SPEC 冻结", executing: "执行中", done: "完成",
};

const STEPS = [
  { key: "idea", label: "想法" },
  { key: "prd", label: "PRD" },
  { key: "spec", label: "SPEC" },
  { key: "executing", label: "执行" },
  { key: "done", label: "完成" },
];

function stepOf(stage) {
  const s = String(stage || "idea");
  if (s === "idea") return 0;
  if (s.startsWith("prd")) return 1;
  if (s.startsWith("spec")) return 2;
  if (s === "executing") return 3;
  if (s === "done") return 4;
  return 0;
}

const $ = (id) => document.getElementById(id);
const esc = (s) => (s == null ? "" : String(s))
  .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
  .replace(/"/g, "&quot;");

function now() {
  return new Date().toLocaleTimeString("zh-CN", { hour12: false });
}

/* 轻量 markdown 渲染(PRD/SPEC 文档足够) */
function md(src) {
  if (!src) return "";
  const codes = [];
  let s = String(src).replace(/```[\s\S]*?```/g, (m) => {
    codes.push(m);
    return "\u0000" + (codes.length - 1) + "\u0000";
  });
  s = esc(s);
  s = s.replace(/\u0000(\d+)\u0000/g, (_, i) => "<pre>" + esc(codes[+i].slice(3, -3)) + "</pre>");
  s = s.replace(/^### (.*)$/gm, "<h3>$1</h3>");
  s = s.replace(/^## (.*)$/gm, "<h2>$1</h2>");
  s = s.replace(/^# (.*)$/gm, "<h1>$1</h1>");
  s = s.replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>");
  s = s.replace(/`([^`]+)`/g, "<code>$1</code>");
  s = s.replace(/^[-*] (.*)$/gm, "<li>$1</li>");
  s = s.replace(/\n/g, "<br>");
  return s;
}

/* ---------------- 状态 ---------------- */
function makeSession() {
  return {
    phase: "idea",
    totalIn: 0, totalOut: 0,
    turns: [],                    // [{in, out}]
    docs: {
      prd: { content: "", status: "", version: 0 },
      spec: { content: "", status: "", version: 0 },
    },
    messages: [],                 // 结构化消息(可重放)
    view: "chat",                 // chat | prd | spec
  };
}

const state = {
  scope: "global",                // 当前视图 scope
  config: null,                   // 全局 API 配置(public: 无 key)
  projects: {},                   // pid -> {id,name,dir,status,last_stage,created}
  global: makeSession(),          // 全局 chat 会话
  sessions: {},                   // pid -> 项目会话
};

const cur = () =>
  state.scope === "global"
    ? state.global
    : (state.sessions[state.scope] || (state.sessions[state.scope] = makeSession()));

const pidOf = () => (state.scope === "global" ? null : state.scope);

/* ---------------- DOM 渲染 ---------------- */
function el(tag, cls, html) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (html != null) e.innerHTML = html;
  return e;
}

function renderHeader() {
  const h = $("header");
  if (state.scope === "global") {
    const c = state.config || {};
    h.innerHTML =
      '<div class="brand"><b>Idea2App</b><span>多项目工作台</span></div>' +
      '<div class="grow"></div>' +
      '<div class="model-chip"><i></i>' + esc(c.model || "未配置模型") + "</div>" +
      '<button class="btn" onclick="openApi()">⚙ API 配置</button>';
    $("tabs").hidden = true;
    $("msgInput").placeholder = "和 Idea2App 随便聊聊…";
    $("hint").textContent = "全局 Chat · 只聊天,不写文件 · Enter 发送";
    return;
  }
  const p = state.projects[state.scope] || { name: "?", dir: "?", id: state.scope, status: "stopped" };
  const s = cur();
  h.innerHTML =
    '<button class="btn ghost" onclick="switchScope(\'global\')" title="返回">←</button>' +
    '<div class="p-title"><b>' + esc(p.name) + '</b><span class="p-dir" title="' + esc(p.dir) + '">' + esc(p.dir) + "</span></div>" +
    '<div class="grow"></div>' +
    '<div class="stepper" id="stepper"></div>' +
    '<div class="grow"></div>' +
    '<div class="p-actions" id="pActions"></div>';
  renderStepper();
  renderActions(p, s);
  $("tabs").hidden = false;
  renderTabs(s.view);
  $("msgInput").placeholder = "和这个项目对话(首次输入想法会生成 PRD)…";
  $("hint").textContent = "对话 Tag 与 PRD / SPEC 独立 · Enter 发送";
}

function renderTabs(view) {
  const t = $("tabs");
  t.querySelectorAll("button").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
}

function renderActions(p, s) {
  const a = $("pActions");
  let html = "";
  const st = p.status || "stopped";
  if (st === "running") html += '<button class="btn" onclick="stopProject()">■ 停止</button>';
  else if (st === "completed") html += '<span class="pill ok">已完成</span>';
  else html += '<button class="btn primary" onclick="startProject()">▶ 启动</button>';
  if (st !== "completed")
    html += '<button class="btn ghost" onclick="markComplete()" title="移入已完成">✓ 完成</button>';
  html += '<button class="btn ghost danger" onclick="deleteProject()" title="从列表移除">删</button>';
  a.innerHTML = html;
}

function renderStepper() {
  const st = $("stepper");
  if (!st) return;
  const idx = stepOf(cur().phase);
  st.innerHTML = STEPS.map((s, i) => {
    const done = i < idx || (i === idx && idx === STEPS.length - 1 && cur().phase === "done");
    const active = i === idx;
    return '<span class="step' + (active ? " active" : "") + (done ? " done" : "") + '">' +
      '<span class="node">' + (done ? "✓" : (i + 1)) + "</span>" +
      '<span class="lbl">' + s.label + "</span>" +
      (i < STEPS.length - 1 ? '<span class="conn' + (done ? " done" : "") + '"></span>' : "") +
      "</span>";
  }).join("");
}

function renderSidebar() {
  $("chatEntry").classList.toggle("active", state.scope === "global");
  const active = [], done = [];
  for (const pid in state.projects) {
    const p = state.projects[pid];
    (p.status === "completed" ? done : active).push(pid);
  }
  $("projActive").innerHTML = active.length
    ? active.map(projEntry).join("") : '<div class="empty">暂无进行中项目</div>';
  $("projDone").innerHTML = done.map(projEntry).join("");
  $("projDone").style.display = done.length ? "" : "none";
}

function projStage(pid) {
  const s = state.sessions[pid];
  if (s && s.phase) return s.phase;
  return state.projects[pid]?.last_stage || null;
}

function projEntry(pid) {
  const p = state.projects[pid];
  const sel = state.scope === pid ? "active" : "";
  const dot = { running: "●", starting: "◐", error: "✕", stopped: "○", completed: "✓" }[p.status] || "○";
  const cls = { running: "st-run", starting: "st-run", error: "st-err", completed: "st-done" }[p.status] || "";
  const stg = PHASE_TEXT[projStage(pid)] || "";
  return '<div class="p-item ' + sel + " " + cls + '" data-pid="' + pid + '">' +
    '<span class="p-dot">' + dot + "</span>" +
    '<span class="p-name">' + esc(p.name) + "</span>" +
    '<span class="p-stage">' + esc(stg) + "</span></div>";
}

/* ---------------- 消息流 ---------------- */
function renderMsg(rec) {
  const box = $("messages");
  if (rec.kind === "user") {
    box.appendChild(el("div", "msg user", md(rec.text)));
  } else if (rec.kind === "assistant") {
    box.appendChild(el("div", "msg assistant", md(rec.text)));
  } else if (rec.kind === "tool") {
    box.appendChild(el("div", "tool-chip",
      '<span class="t">' + esc(rec.name) + '</span><span class="f">' + esc(rec.arg) + "</span>"));
  } else if (rec.kind === "review") {
    box.appendChild(renderReviewEl(rec, cur().messages.indexOf(rec)));
  }
  scrollDown();
}

function renderReviewEl(rec, idx) {
  const processed = rec.processed || (cur().docs[rec.dockind] && cur().docs[rec.dockind].status !== "draft");
  const d = el("div", "review");
  d.dataset.ridx = idx;
  const top = el("div", "r-top");
  top.innerHTML = '<span class="tag">' + esc(rec.artifact || rec.dockind) + "</span>" +
    "<b>" + (processed ? "已处理" : "待审") + "</b>";
  const body = el("div", "r-body");
  body.textContent = rec.content || "(空文档)";
  d.appendChild(top);
  d.appendChild(body);
  if (!processed) {
    const bar = el("div", "r-actions");
    bar.innerHTML =
      '<button class="btn ghost" onclick="sendVerdict(' + idx + ',false)">驳回</button>' +
      '<button class="btn primary" onclick="sendVerdict(' + idx + ',true)">通过</button>';
    d.appendChild(bar);
  }
  return d;
}

function pushMsg(rec) {
  cur().messages.push(rec);
  renderMsg(rec);
}

function refreshReviewAt(idx) {
  const s = cur();
  const rec = s.messages[idx];
  if (!rec || rec.kind !== "review") return;
  const old = document.querySelector('.review[data-ridx="' + idx + '"]');
  if (old) old.replaceWith(renderReviewEl(rec, idx));
}

function scrollDown() {
  const m = $("messages");
  if (m) m.scrollTop = m.scrollHeight;
}

/* ---------------- 右侧实时动态(feed) + token ---------------- */
function feed(kind, text) {
  const f = $("feed");
  const icon = { phase: "◈", tool: "⚒", sys: "●", ok: "✓", err: "✕", run: "▶", stop: "■" }[kind] || "·";
  const cls = { phase: "phase", tool: "tool", ok: "ok", err: "err", install: "install" }[kind] || "sys";
  const row = el("div", "ev " + cls);
  row.innerHTML = '<span class="ico">' + icon + '</span><div class="t">' + esc(text) +
    '</div><span class="ts">' + now() + "</span>";
  f.appendChild(row);
  while (f.children.length > 60) f.removeChild(f.firstChild);
  f.scrollTop = f.scrollHeight;
}

function renderTokens() {
  const s = cur();
  const last = s.turns[s.turns.length - 1];
  $("turnTokens").textContent = last ? (last.in + last.out).toLocaleString() : "0";
  $("turnSplit").textContent = "in " + (last ? last.in : 0).toLocaleString() + " · out " + (last ? last.out : 0).toLocaleString();
  $("totalTokens").textContent = (s.totalIn + s.totalOut).toLocaleString();
  $("totalSplit").textContent = "in " + s.totalIn.toLocaleString() + " · out " + s.totalOut.toLocaleString();
}

function renderChart() {
  const s = cur();
  const c = $("chart");
  c.innerHTML = "";
  const max = Math.max(1, ...s.turns.map((t) => t.in + t.out));
  s.turns.forEach((t, i) => {
    const bar = el("div", "bar");
    const h = Math.max(4, Math.round(((t.in + t.out) / max) * 100));
    bar.style.height = h + "%";
    bar.title = "第 " + (i + 1) + " 轮:in " + t.in + " · out " + t.out;
    if (i === s.turns.length - 1) bar.classList.add("latest");
    c.appendChild(bar);
  });
}

function renderDocs() {
  const s = cur();
  renderDoc("prd", s);
  renderDoc("spec", s);
}

function renderDoc(kind, s) {
  s = s || cur();
  const view = $("view-" + kind);
  const d = s.docs[kind];
  if (!d.content) {
    view.innerHTML = '<div class="empty-state"><div class="big">📄</div><div>还没有 ' + kind.toUpperCase() +
      " 文档</div><div>" +
      (kind === "prd" ? "在对话里描述想法,PRD 会在这里生成" : "PRD 通过后会在这里生成 SPEC") +
      "</div></div>";
    return;
  }
  const pill = d.status
    ? '<span class="pill ' + (d.status === "draft" ? "draft" : (d.status === "frozen" ? "frozen" : "ok")) + '">' + esc(d.status) + "</span>"
    : "";
  view.innerHTML = '<div class="doc"><div class="doc-inner">' +
    '<div class="doc-head"><div class="doc-kicker">' + kind.toUpperCase() + "</div>" + pill +
    '<span class="doc-meta">v' + (d.version || 1) + "</span></div>" +
    '<div class="doc-body">' + md(d.content) + "</div>" +
    "</div></div>";
}

function updateBuildbar() {
  const s = cur();
  const show = state.scope !== "global" && (s.phase === "spec_approved" || s.phase === "executing");
  $("buildbar").hidden = !show;
}

function updateInputState() {
  const s = cur();
  const isProj = state.scope !== "global";
  const states = { idea: 0, prd_draft: 1, prd_approved: 1, spec_draft: 2, spec_approved: 2, executing: 3, done: 4 };
  // 在 PRD_APPROVED 之后输入 = 推进到下一步(后端决定),这里只提示
  $("msgInput").disabled = false;
}

/* ---------------- 视图切换 ---------------- */
function switchView(v) {
  cur().view = v;
  renderTabs(v);
  $("view-chat").classList.toggle("active", v === "chat");
  $("view-prd").classList.toggle("active", v === "prd");
  $("view-spec").classList.toggle("active", v === "spec");
}

function switchScope(target) {
  if (state.scope === target) return;
  state.scope = target;
  rehydrate();
}

function rehydrate() {
  const s = cur();
  const m = $("messages");
  m.innerHTML = "";
  s.messages.forEach(renderMsg);
  $("feed").innerHTML = "";
  renderTokens();
  renderChart();
  renderDocs();
  updateBuildbar();
  if (state.scope === "global") {
    $("view-chat").classList.add("active");
    $("view-prd").classList.remove("active");
    $("view-spec").classList.remove("active");
  } else {
    switchView(s.view);
  }
  renderHeader();
  renderSidebar();
  scrollDown();
}

/* ---------------- 输入 / 交互 ---------------- */
let ws = null;

function post(obj) {
  if (ws && ws.readyState === 1) ws.send(JSON.stringify(obj));
  else feed("err", "未连接,正在重连…");
}

function send() {
  const input = $("msgInput");
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  if (state.scope === "global") {
    pushMsg({ kind: "user", text });
    post({ type: "chat", text });
  } else {
    pushMsg({ kind: "user", text });
    post({ type: "user", text, project_id: state.scope });
  }
}

function sendVerdict(idx, accept) {
  const rec = cur().messages[idx];
  if (!rec || rec.kind !== "review") return;
  rec.processed = true;
  refreshReviewAt(idx);
  post({ type: "verdict", accept, project_id: pidOf() });
}

function startBuild() {
  if (state.scope === "global") return;
  pushMsg({ kind: "assistant", text: "_进入自驱执行阶段…_" });
  post({ type: "build", project_id: state.scope });
}

function startProject() {
  post({ type: "start_project", project_id: state.scope });
}
function stopProject() {
  post({ type: "stop_project", project_id: state.scope });
}
function markComplete() {
  if (state.scope === "global") return;
  if (!confirm("将该项目标记为「已完成」?")) return;
  post({ type: "mark_complete", project_id: state.scope });
}
function deleteProject() {
  if (state.scope === "global") return;
  if (!confirm("从列表移除该项目?(磁盘文件不会被删除)")) return;
  post({ type: "delete_project", project_id: state.scope });
}

/* ---------------- 项目 / 配置弹层 ---------------- */
function openNew() {
  $("overlay").classList.add("open");
  $("newModal").classList.add("open");
  setTimeout(() => $("projDir").focus(), 50);
}
function openApi() {
  const c = state.config || {};
  $("apiProvider").value = c.provider || "anthropic";
  $("apiKey").value = "";
  $("apiUrl").value = c.base_url || "";
  $("apiModel").value = c.model || "";
  $("apiMax").value = c.max_tokens || 8000;
  $("apiWindow").value = c.window || 200000;
  $("apiNote").textContent = c.has_key ? "已保存 API key;留空则保持不变" : "尚未配置 API key";
  $("overlay").classList.add("open");
  $("apiModal").classList.add("open");
}
function closeModal() {
  $("overlay").classList.remove("open");
  $("newModal").classList.remove("open");
  $("apiModal").classList.remove("open");
}

function pickDir() {
  post({ type: "pick_dir" });
}

function createProject() {
  const dir = $("projDir").value.trim();
  const name = $("projName").value.trim();
  if (!dir) { feed("err", "请先选择或输入项目目录"); return; }
  post({ type: "create_project", name, dir });
  $("projName").value = "";
  $("projDir").value = "";
  closeModal();
}

function saveConfig() {
  const cfg = {
    provider: $("apiProvider").value,
    api_key: $("apiKey").value.trim(),      // 空 = 后端保留旧 key
    base_url: $("apiUrl").value.trim(),
    model: $("apiModel").value.trim(),
    max_tokens: parseInt($("apiMax").value, 10) || 8000,
    window: parseInt($("apiWindow").value, 10) || 200000,
  };
  post({ type: "set_config", config: cfg });
  closeModal();
}

function toggleTheme() {
  const cur2 = document.documentElement.getAttribute("data-theme") || "dark";
  const next = cur2 === "dark" ? "light" : "dark";
  document.documentElement.setAttribute("data-theme", next);
  localStorage.setItem("i2a-theme", next);
}

/* ---------------- 事件 ---------------- */
function applySessionEvent(s, ev, isCur) {
  const t = ev.type;
  if (t === "phase") {
    s.phase = ev.stage;
    if (isCur) { renderStepper(); updateBuildbar(); feed("phase", PHASE_TEXT[ev.stage] || ev.stage); }
  } else if (t === "tool_call") {
    s.messages.push({ kind: "tool", name: ev.name, arg: ev.arg });
    if (isCur) { renderMsg(s.messages[s.messages.length - 1]); feed("tool", ev.name + " " + (ev.arg || "")); }
  } else if (t === "tool_result") {
    if (isCur) feed(ev.ok ? "ok" : "err", (ev.preview || "").slice(0, 60));
  } else if (t === "gate_request") {
    s.messages.push({ kind: "review", dockind: ev.kind, artifact: ev.artifact || ev.kind, content: ev.content });
    if (isCur) renderMsg(s.messages[s.messages.length - 1]);
  } else if (t === "doc") {
    s.docs[ev.kind] = { content: ev.content, status: ev.status, version: ev.version };
    if (isCur) { renderDoc(ev.kind, s); updateBuildbar(); }
  } else if (t === "assistant") {
    s.messages.push({ kind: "assistant", text: ev.text });
    if (isCur) { renderMsg(s.messages[s.messages.length - 1]); }
  } else if (t === "usage") {
    s.totalIn += ev.input; s.totalOut += ev.output;
    s.turns.push({ in: ev.input, out: ev.output });
    if (isCur) { renderTokens(); renderChart(); }
  } else if (t === "permission") {
    if (isCur) feed(ev.policy === "deny" ? "err" : "sys", "权限(" + ev.policy + "):" + ev.shown);
  } else if (t === "cleared") {
    s.messages = []; s.docs = { prd: { content: "", status: "", version: 0 }, spec: { content: "", status: "", version: 0 } };
    s.phase = "idea"; s.totalIn = 0; s.totalOut = 0; s.turns = [];
    if (isCur) rehydrate();
  }
  // turn_start / turn_end 已在 onEvent 用 typing 处理
}

function typing(on) {
  let t = $("typing");
  if (on) {
    if (!t) {
      t = el("div", "typing");
      t.id = "typing";
      t.innerHTML = "<i></i><i></i><i></i>";
      $("messages").appendChild(t);
      scrollDown();
    }
  } else if (t) {
    t.remove();
  }
}

function onEvent(ev) {
  const t = ev.type;

  // 元事件
  if (t === "hello") {
    state.config = ev.config || null;
    state.projects = {};
    (ev.projects || []).forEach((p) => (state.projects[p.id] = p));
    renderSidebar();
    renderHeader();
    return;
  }
  if (t === "projects") {
    const keep = new Set(Object.keys(state.sessions).filter((pid) => state.projects[pid]));
    state.projects = {};
    (ev.projects || []).forEach((p) => (state.projects[p.id] = p));
    // 若当前项目被删除,退回全局
    if (state.scope !== "global" && !state.projects[state.scope]) state.scope = "global";
    renderSidebar();
    renderHeader();
    return;
  }
  if (t === "project_status") {
    const pid = ev.project_id;
    if (pid) {
      const p = state.projects[pid];
      if (p) p.status = ev.status;
      renderSidebar();
      if (state.scope === pid) renderHeader();
      const label = { starting: "启动中", running: "已运行", stopped: "已停止", error: "出错", completed: "已完成" }[ev.status] || ev.status;
      feed(ev.status === "error" ? "err" : (ev.status === "running" ? "run" : "sys"),
        (p ? p.name : pid) + " → " + label + (ev.detail ? " (" + ev.detail + ")" : ""));
    } else {
      feed("sys", ev.detail || ev.status);
    }
    return;
  }
  if (t === "config_updated") {
    state.config = ev.config || null;
    renderHeader();
    feed("ok", "API 配置已保存");
    return;
  }
  if (t === "dir_picked") {
    if (ev.path) { $("projDir").value = ev.path; if (!$("projName").value.trim()) $("projName").value = ev.path.split(/[\\/]/).pop(); }
    if (ev.cancelled) feed("sys", "已取消目录选择");
    return;
  }

  // 会话事件:路由
  const pid = ev.project_id === undefined ? null : ev.project_id;
  const s = pid ? (state.sessions[pid] || (state.sessions[pid] = makeSession())) : state.global;
  const isCur = pid === null ? state.scope === "global" : state.scope === pid;

  if (t === "turn_start") { if (isCur) typing(true); return; }
  if (t === "turn_end") { if (isCur) typing(false); return; }
  if (t === "error") { if (isCur) feed("err", ev.message || "发生错误"); return; }

  applySessionEvent(s, ev, isCur);
}

/* ---------------- WebSocket ---------------- */
function connect() {
  ws = new WebSocket(WS_URL);
  ws.onopen = () => { feed("ok", "已连接"); };
  ws.onmessage = (e) => {
    try { onEvent(JSON.parse(e.data)); } catch (_) { /* 忽略坏包 */ }
  };
  ws.onclose = () => { feed("err", "连接断开,2s 后重连"); setTimeout(connect, 2000); };
  ws.onerror = () => { try { ws.close(); } catch (_) {} };
}

/* ---------------- 初始化 ---------------- */
function bindUI() {
  $("sendBtn").addEventListener("click", send);
  $("msgInput").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } });
  $("chatEntry").addEventListener("click", () => switchScope("global"));
  $("newProjectBtn").addEventListener("click", openNew);
  $("apiBtn").addEventListener("click", openApi);
  $("toggleTheme").addEventListener("click", toggleTheme);
  $("pickBtn").addEventListener("click", pickDir);
  $("overlay").addEventListener("click", closeModal);
  $("tabs").querySelectorAll("button").forEach((b) => b.addEventListener("click", () => switchView(b.dataset.view)));
  // 项目列表点击(事件委托)
  $("projActive").addEventListener("click", onProjClick);
  $("projDone").addEventListener("click", onProjClick);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeModal(); });
}

function onProjClick(e) {
  const item = e.target.closest(".p-item");
  if (item && item.dataset.pid) switchScope(item.dataset.pid);
}

(function init() {
  const saved = localStorage.getItem("i2a-theme") || "dark";
  document.documentElement.setAttribute("data-theme", saved);
  bindUI();
  renderSidebar();
  renderHeader();
  renderTokens();
  renderChart();
  connect();
})();