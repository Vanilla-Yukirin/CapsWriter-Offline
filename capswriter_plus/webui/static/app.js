"use strict";

const state = {
  overviewTimer: null,
  logTimer: null,
  logCursor: null,
  logLines: [],
  logsPaused: false,
  logLevel: "all",
  authenticated: false,
};

const $ = (id) => document.getElementById(id);

async function requestJSON(url, options = {}) {
  const response = await fetch(url, {
    credentials: "same-origin",
    headers: { Accept: "application/json", ...(options.headers || {}) },
    ...options,
  });
  let payload = {};
  try { payload = await response.json(); } catch (_) { payload = {}; }
  if (!response.ok) {
    const error = new Error(payload.error || `HTTP ${response.status}`);
    error.status = response.status;
    error.retryAfter = response.headers.get("Retry-After");
    throw error;
  }
  return payload;
}

function showLogin(message = "") {
  state.authenticated = false;
  stopPolling();
  $("dashboard-view").hidden = true;
  $("login-view").hidden = false;
  $("login-error").textContent = message;
  $("token").value = "";
  $("token").focus();
}

function showDashboard() {
  state.authenticated = true;
  $("login-view").hidden = true;
  $("dashboard-view").hidden = false;
  startPolling();
}

function stopPolling() {
  if (state.overviewTimer) clearInterval(state.overviewTimer);
  if (state.logTimer) clearInterval(state.logTimer);
  state.overviewTimer = null;
  state.logTimer = null;
}

function startPolling() {
  stopPolling();
  refreshAll();
  state.overviewTimer = setInterval(loadOverview, 5000);
  state.logTimer = setInterval(loadLogs, 1500);
}

function formatUptime(seconds) {
  if (seconds === null || seconds === undefined) return "不可用";
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (days) return `${days} 天 ${hours} 小时`;
  if (hours) return `${hours} 小时 ${minutes} 分`;
  return `${minutes} 分钟`;
}

function formatBytes(bytes) {
  if (!Number.isFinite(bytes)) return "不可用";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
}

function setPill(element, text, kind) {
  element.textContent = text;
  element.className = `pill ${kind}`;
}

function renderOverview(data) {
  const asr = data.asr || {};
  const device = data.device || {};
  const webui = data.webui || {};
  const healthy = asr.ready === true;
  const awaiting = asr.ready === null || asr.ready === undefined;

  $("instance-name").textContent = data.instance?.name || "CapsWriter Plus";
  $("hero-title").textContent = healthy ? "语音服务运行正常" : (awaiting ? "等待 ASR 运行时状态" : "语音服务需要关注");
  $("hero-subtitle").textContent = healthy
    ? "ASR 与 Web UI 均已就绪。"
    : (awaiting ? "Web UI 正常，尚未收到 ASR 自报告状态。" : "请检查 ASR 运行状态；Web UI 本身仍保持可用。");
  $("hero-badge").className = `status-badge ${healthy ? "healthy" : "warning"}`;
  $("hero-badge").lastChild.textContent = healthy ? "运行正常" : (awaiting ? "等待状态" : "需要关注");

  $("asr-state").textContent = asr.ready === true ? "已就绪" : (awaiting ? "未确认" : (asr.state || "不可用"));
  $("asr-detail").textContent = `${asr.port || 6016} / ${asr.listening === true ? "正在监听" : (asr.listening === false ? "未监听" : "等待状态")}`;
  $("asr-indicator").classList.toggle("offline", asr.ready === false);
  $("worker-state").textContent = asr.worker_pid ? "已加载" : "未确认";
  $("worker-detail").textContent = asr.worker_pid ? `PID ${asr.worker_pid}` : "等待 ASR 运行时报告";
  $("connection-count").textContent = asr.connections ?? "—";

  $("device-state").textContent = device.mode && device.mode !== "unknown" ? device.mode.toUpperCase() : "未确认";
  $("device-detail").textContent = device.label || "等待 ASR 运行时报告";

  $("asr-service-meta").textContent = `TCP ${asr.port || 6016}${asr.main_pid ? ` · PID ${asr.main_pid}` : ""}`;
  setPill($("asr-service-pill"), healthy ? "已就绪" : (awaiting ? "未确认" : "不可用"), healthy ? "healthy" : (awaiting ? "neutral" : "danger"));
  $("webui-service-meta").textContent = `${webui.listen || "127.0.0.1"}:${webui.port || 6017} · PID ${webui.pid || "—"}`;

  $("version-value").textContent = data.version?.capswriter || "—";
  $("build-value").textContent = `${data.version?.extension || "—"} · ${data.version?.build || "—"}`;
  $("uptime-value").textContent = formatUptime(asr.uptime_seconds);
  $("pid-value").textContent = asr.main_pid || "—";
  $("log-value").textContent = data.log?.available
    ? `${data.log.filename} · ${formatBytes(data.log.size_bytes)}` : "不可用";

  $("sidebar-status").textContent = healthy ? "系统正常" : (awaiting ? "等待 ASR" : "ASR 不可用");
  $("sidebar-status-dot").classList.toggle("offline", asr.ready === false);
  $("last-updated").textContent = `更新于 ${new Date(data.generated_at).toLocaleTimeString("zh-CN", { hour12: false })}`;
}

async function loadOverview() {
  try {
    renderOverview(await requestJSON("/api/overview"));
  } catch (error) {
    if (error.status === 401) showLogin("会话已失效，请重新登录。");
    else {
      $("hero-title").textContent = "状态读取失败";
      $("hero-subtitle").textContent = "Web UI 无法读取运行信息，请稍后重试。";
    }
  }
}

function humanKey(key) {
  const names = {
    instance: "实例", server: "服务端", model: "模型", webui: "Web UI",
    features: "功能开关", security: "安全状态", listen: "监听地址", port: "端口",
    model_type: "模型类型", log_level: "日志级别", aligner_idle_timeout_seconds: "对齐器空闲释放",
    format_numbers: "数字格式化", format_spacing: "中英文空格", name: "名称", context_size: "上下文",
    chunk_seconds: "分段秒数", memory_segments: "记忆段数", gpu_enabled: "GPU 加速", read_only: "只读模式",
    http_transcription_api: "HTTP 转录 API", hotword_editor: "热词编辑",
    model_reload: "模型重载", tts: "TTS", feedback_agent: "反馈 Agent", token_configured: "Token 已配置",
    minimum_token_length: "Token 最短长度", token_visible: "Token 可见",
  };
  return names[key] || key.replaceAll("_", " ");
}

function humanValue(value) {
  if (value === true) return "是";
  if (value === false) return "否";
  if (value === null || value === undefined || value === "") return "—";
  return String(value);
}

function renderConfig(config) {
  const grid = $("config-grid");
  grid.replaceChildren();
  Object.entries(config).forEach(([section, values]) => {
    const card = document.createElement("article");
    card.className = "config-card";
    const heading = document.createElement("h3");
    heading.textContent = humanKey(section);
    card.appendChild(heading);
    Object.entries(values || {}).forEach(([key, value]) => {
      const row = document.createElement("div");
      row.className = "config-row";
      const label = document.createElement("span");
      label.textContent = humanKey(key);
      const content = document.createElement("span");
      content.textContent = humanValue(value);
      row.append(label, content);
      card.appendChild(row);
    });
    grid.appendChild(card);
  });
}

async function loadConfig() {
  try { renderConfig(await requestJSON("/api/config")); }
  catch (error) { if (error.status === 401) showLogin("会话已失效，请重新登录。"); }
}

function classifyLog(line) {
  if (/\b(ERROR|CRITICAL)\b|Traceback/i.test(line)) return "error";
  if (/\bWARN(?:ING)?\b/i.test(line)) return "warning";
  return "info";
}

function renderLogs() {
  const output = $("log-output");
  const query = $("log-search").value.trim().toLowerCase();
  const filtered = state.logLines.filter((line) => {
    const levelMatches = state.logLevel === "all" || classifyLog(line) === state.logLevel;
    return levelMatches && (!query || line.toLowerCase().includes(query));
  });
  output.replaceChildren();
  if (!filtered.length) {
    const empty = document.createElement("div");
    empty.className = "log-empty";
    empty.textContent = state.logLines.length ? "没有符合筛选条件的日志" : "暂无日志";
    output.appendChild(empty);
  } else {
    filtered.forEach((line) => {
      const row = document.createElement("div");
      row.className = `log-line ${classifyLog(line)}`;
      const text = document.createElement("span");
      text.textContent = line;
      row.appendChild(text);
      output.appendChild(row);
    });
    if (!state.logsPaused) output.scrollTop = output.scrollHeight;
  }
  $("log-count").textContent = `${filtered.length} / ${state.logLines.length} 行`;
}

async function loadLogs() {
  if (state.logsPaused || !state.authenticated) return;
  const suffix = state.logCursor === null ? "" : `?cursor=${encodeURIComponent(state.logCursor)}`;
  try {
    const data = await requestJSON(`/api/logs${suffix}`);
    if (data.reset) state.logLines = [];
    if (Array.isArray(data.lines)) state.logLines.push(...data.lines);
    if (state.logLines.length > 2000) state.logLines = state.logLines.slice(-2000);
    state.logCursor = data.cursor;
    renderLogs();
  } catch (error) {
    if (error.status === 401) showLogin("会话已失效，请重新登录。");
  }
}

async function refreshAll() {
  $("refresh-button").classList.add("loading");
  await Promise.allSettled([loadOverview(), loadConfig(), loadLogs()]);
  $("refresh-button").classList.remove("loading");
}

function switchTab(tab) {
  const titles = { overview: "运行概览", logs: "服务端日志", config: "只读配置" };
  document.querySelectorAll(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.tab === tab));
  document.querySelectorAll(".tab-panel").forEach((panel) => {
    const active = panel.id === `tab-${tab}`;
    panel.classList.toggle("active", active);
    panel.hidden = !active;
  });
  $("page-title").textContent = titles[tab];
  if (tab === "logs") loadLogs();
}

$("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = $("login-button");
  const token = $("token").value;
  if (token.length < 16) {
    $("login-error").textContent = "访问密钥至少需要 16 个字符。";
    return;
  }
  button.disabled = true;
  $("login-error").textContent = "";
  try {
    await requestJSON("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token }),
    });
    showDashboard();
  } catch (error) {
    $("login-error").textContent = error.status === 429
      ? `尝试过于频繁，请在 ${error.retryAfter || 300} 秒后重试。`
      : "访问密钥无效。";
  } finally {
    button.disabled = false;
  }
});

$("toggle-token").addEventListener("click", () => {
  const input = $("token");
  const showing = input.type === "text";
  input.type = showing ? "password" : "text";
  $("toggle-token").textContent = showing ? "显示" : "隐藏";
  $("toggle-token").setAttribute("aria-label", showing ? "显示密钥" : "隐藏密钥");
});

$("logout-button").addEventListener("click", async () => {
  try { await requestJSON("/api/logout", { method: "POST" }); } catch (_) { /* local logout still applies */ }
  showLogin();
});
$("refresh-button").addEventListener("click", refreshAll);
$("pause-logs").addEventListener("click", () => {
  state.logsPaused = !state.logsPaused;
  $("pause-logs").textContent = state.logsPaused ? "继续" : "暂停";
  if (!state.logsPaused) loadLogs();
});
$("log-search").addEventListener("input", renderLogs);
document.querySelectorAll(".nav-item").forEach((item) => item.addEventListener("click", () => switchTab(item.dataset.tab)));
document.querySelectorAll(".filter-chip").forEach((item) => item.addEventListener("click", () => {
  state.logLevel = item.dataset.level;
  document.querySelectorAll(".filter-chip").forEach((chip) => chip.classList.toggle("active", chip === item));
  renderLogs();
}));

(async () => {
  try {
    await requestJSON("/api/session");
    showDashboard();
  } catch (_) {
    showLogin();
  }
})();
