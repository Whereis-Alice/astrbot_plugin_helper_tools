const PLUGIN_API_BASE = "/api/plug/astrbot_plugin_helper_tools";
const THEMES = new Set(["dark", "light"]);
const state = {
  activeTab: "overview",
  data: null,
  activity: null,
  moduleNodes: new Map(),
  theme: "dark",
  dirty: false,
  storageLoadedAt: 0,
  wallpaper: {
    libraries: [],
    selectedLibraryId: "",
    images: [],
    pagination: null,
    query: "",
    sort: "newest",
    previewImage: null,
    previewRequestId: 0,
    uploadMaxBytes: 20 * 1024 * 1024,
    uploadFileLimit: 24,
    libraryRequestId: 0,
    imageRequestId: 0,
    serverConfigRows: null,
    searchTimer: null,
    thumbnailCache: new Map(),
    thumbnailRequests: new Map(),
    thumbnailQueue: [],
    thumbnailLoading: 0,
  },
};

const tabTitles = {
  wallpaper: ["LOCAL / WALLPAPERS", "壁纸库", "管理图库、图片文件、随机抽图指令与本地目录。"],
  overview: ["HELPER / CONTROL", "概览", "查看模块、工具和本地运行状态。"],
  config: ["MODULE / CONFIG", "模块配置", "按模块展开配置；每项会按插件当前 Schema 校验后保存。"],
  activity: ["LOCAL / AUDIT", "运行记录", "查看已记录的重要动作和结果。"],
  storage: ["LOCAL / STORAGE", "存储诊断", "确认本地数据、凭据状态和运行环境。"],
  about: ["HELPER / INFO", "说明", "了解控制台的保存范围和生效方式。"],
};

const byId = (id) => document.getElementById(id);

function el(tag, className = "", text = "") {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== "") node.textContent = text;
  return node;
}

function text(value, fallback = "") {
  const result = String(value ?? "").trim();
  return result || fallback;
}

function displayTitle(value, fallback = "") {
  return text(value, fallback).replace(/^【|】$/g, "");
}

function pathKey(path) {
  return path.join(".");
}

function normalizeSearch(value) {
  return text(value).toLocaleLowerCase("zh-CN");
}

function getBridge() {
  return window.AstrBotPluginPage || null;
}

async function directRequest(endpoint, method, body, params = {}) {
  const query = new URLSearchParams(params || {}).toString();
  const response = await fetch(`${PLUGIN_API_BASE}/${endpoint}${query ? `?${query}` : ""}`, {
    method,
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
    credentials: "same-origin",
  });
  const payload = await response.json().catch(() => ({ success: false, message: "控制台接口返回了无法识别的数据。" }));
  if (!response.ok && payload?.success !== true) {
    throw new Error(payload?.message || `请求失败（${response.status}）`);
  }
  return payload;
}

async function apiGet(endpoint, params = {}) {
  const bridge = getBridge();
  if (bridge?.apiGet) return bridge.apiGet(endpoint, params);
  return directRequest(endpoint, "GET", undefined, params);
}

async function apiPost(endpoint, body) {
  const bridge = getBridge();
  if (bridge?.apiPost) return bridge.apiPost(endpoint, body);
  return directRequest(endpoint, "POST", body);
}

function apiUrl(endpoint, params = {}) {
  const query = new URLSearchParams(params || {}).toString();
  return `${PLUGIN_API_BASE}/${endpoint}${query ? `?${query}` : ""}`;
}

async function apiMultipartPost(endpoint, formData) {
  const response = await fetch(apiUrl(endpoint), {
    method: "POST",
    body: formData,
    credentials: "same-origin",
  });
  const payload = await response.json().catch(() => ({ success: false, message: "上传接口返回了无法识别的数据。" }));
  if (!response.ok && payload?.success !== true) {
    throw new Error(payload?.message || `上传失败（${response.status}）。`);
  }
  return payload;
}

async function waitForBridge() {
  const bridge = getBridge();
  if (bridge?.ready) {
    await bridge.ready();
  }
}

function setHeaderState(message, error = false) {
  const target = byId("header-state");
  target.textContent = message || "";
  target.style.color = error ? "var(--red)" : "";
  byId("sidebar-status").textContent = message || "已连接控制台。";
}

let toastTimer = null;
function showToast(message, error = false) {
  const target = byId("toast");
  target.textContent = message || (error ? "操作失败。" : "已完成。");
  target.classList.toggle("error", Boolean(error));
  target.classList.add("visible");
  window.clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => target.classList.remove("visible"), 3400);
}

let pendingConfirmation = null;

function settleConfirmation(confirmed) {
  const pending = pendingConfirmation;
  pendingConfirmation = null;
  closeWallpaperDialog("confirm-action-dialog");
  if (pending) pending(Boolean(confirmed));
}

function confirmAction(message, options = {}) {
  const dialog = byId("confirm-action-dialog");
  const title = byId("confirm-action-title");
  const body = byId("confirm-action-message");
  const submit = byId("confirm-action-submit");
  if (!dialog || !title || !body || !submit) {
    return Promise.resolve(window.confirm(message));
  }
  if (pendingConfirmation) settleConfirmation(false);
  title.textContent = options.title || "确认操作";
  body.textContent = message || "确认继续执行这个操作吗？";
  submit.textContent = options.confirmLabel || "确认";
  dialog.dataset.tone = options.tone || "danger";
  showWallpaperDialog("confirm-action-dialog");
  window.setTimeout(() => submit.focus(), 0);
  return new Promise((resolve) => {
    pendingConfirmation = resolve;
  });
}

function setDirty(value) {
  state.dirty = Boolean(value);
  const button = byId("save-button");
  button.disabled = !state.data;
  if (state.dirty) {
    button.querySelector("span").textContent = "保存配置 *";
    setHeaderState("有尚未保存的配置更改。", false);
  } else if (state.data) {
    button.querySelector("span").textContent = "保存配置";
  }
}

function applyTheme(theme, persist = false) {
  const next = THEMES.has(theme) ? theme : "dark";
  state.theme = next;
  document.body.dataset.theme = next;
  document.querySelectorAll("[data-theme]").forEach((button) => {
    if (!button.classList.contains("theme-option")) return;
    button.setAttribute("aria-pressed", String(button.dataset.theme === next));
  });
  if (persist) {
    void saveTheme(next);
  }
}

async function saveTheme(theme) {
  try {
    const response = await apiPost("save_theme", { theme });
    if (!response?.success) throw new Error(response?.message || "主题保存失败。");
    if (state.data?.config?.webui) state.data.config.webui.dashboard_theme = theme;
    showToast(response.message || "主题已保存。");
  } catch (error) {
    showToast(error.message || "主题保存失败。", true);
  }
}

function formatBytes(value) {
  const bytes = Number(value) || 0;
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let size = bytes / 1024;
  let index = 0;
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024;
    index += 1;
  }
  return `${size >= 100 || index === 0 ? size.toFixed(0) : size.toFixed(1)} ${units[index]}`;
}

function formatDate(value, withSeconds = false) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "-";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: withSeconds ? "2-digit" : undefined,
    hour12: false,
  }).format(date);
}

function metric(label, value, note = "") {
  const node = el("article", "metric");
  const labelNode = el("span", "", label);
  if (note) labelNode.title = note;
  node.append(labelNode, el("strong", "", String(value)));
  return node;
}

function emptyState(message) {
  return el("p", "empty-state", message);
}

function statusPill(stateName, label) {
  const pill = el("span", `status-pill ${text(stateName, "empty")}`, label);
  return pill;
}

function setTabBadge(id, value) {
  const node = byId(id);
  if (!node) return;
  const count = Number(value) || 0;
  if (count <= 0) {
    node.hidden = true;
    node.textContent = "";
    node.removeAttribute("title");
    return;
  }
  node.hidden = false;
  node.textContent = count > 99 ? "99+" : String(count);
  node.title = String(count);
}

// 顶部标签栏的数字角标：为 0 或缺数据时保持隐藏，避免出现一排「0」。
function renderTabBadges() {
  const metrics = state.data?.metrics || {};
  setTabBadge("tab-badge-config", metrics.modules_enabled);
  setTabBadge("tab-badge-activity", metrics.activity_today);
  const libraries = state.wallpaper.libraries || [];
  setTabBadge(
    "tab-badge-wallpaper",
    libraries.reduce((total, item) => total + (Number(item.image_count) || 0), 0),
  );
}

function normalizedVersion() {
  const version = text(state.data?.version).replace(/^v/i, "");
  return version ? `v${version}` : "";
}

// 顶栏品牌区与底部状态栏各显示一次版本号，数据来源同 get_state 的 version。
function renderVersionLabels() {
  const version = normalizedVersion();
  const brand = byId("brand-version");
  if (brand) brand.textContent = version || "v-";
  const footer = byId("footer-version");
  if (footer) footer.textContent = version ? `astrbot · ${version}` : "astrbot · 插件控制台";
}
function renderOverview() {
  const data = state.data;
  if (!data) return;
  const metrics = byId("overview-metrics");
  metrics.replaceChildren(
    metric("已启用模块", `${data.metrics.modules_enabled}/${data.metrics.modules_total}`),
    metric("当前 LLM 工具", data.metrics.llm_tools_enabled),
    metric("本地数据", formatBytes(data.metrics.storage_bytes)),
    metric("今日运行记录", data.metrics.activity_today),
  );
  renderRuntimeList(byId("runtime-list"), data.runtime || []);
  renderToolList(byId("tool-list"), data.llm_tools || []);
  renderModuleMatrix(data.modules || []);
  renderRecentActivity(data.recent_activities || []);
  renderTabBadges();
  renderVersionLabels();
}

function renderRuntimeList(target, values) {
  target.replaceChildren();
  if (!values.length) {
    target.append(emptyState("暂时没有可显示的状态。"));
    return;
  }
  values.forEach((item) => {
    const row = el("div", "runtime-row");
    const copy = el("div");
    copy.append(el("strong", "", item.label || item.key || "状态"));
    copy.append(el("small", "", item.value || item.detail || "-"));
    if (item.detail && item.value) copy.lastChild.title = item.detail;
    row.append(copy, statusPill(item.state, runtimeStateLabel(item.state)));
    target.append(row);
  });
}

function runtimeStateLabel(value) {
  const labels = { ready: "就绪", active: "进行中", idle: "空闲", disabled: "未启用", empty: "未配置", success: "成功", failed: "失败", error: "错误", warning: "警告", message: "消息" };
  return labels[value] || "状态";
}

function activitySessionLabel(record) {
  if (record?.session) return text(record.session, "会话");
  const labels = {
    GroupMessage: "群聊",
    FriendMessage: "好友私聊",
    PrivateMessage: "私聊",
    Message: "消息会话",
  };
  return labels[text(record?.session_kind)] || "未提供";
}

function renderToolList(target, tools) {
  target.replaceChildren();
  if (!tools.length) {
    target.append(emptyState("当前没有启用的 LLM 工具。"));
    return;
  }
  tools.forEach((tool) => {
    const row = el("div", "tool-row");
    const code = el("code", "", tool.name || "tool");
    code.title = tool.name || "";
    row.append(code, el("span", "", moduleDisplayName(tool.module)));
    target.append(row);
  });
}

function moduleDisplayName(module) {
  const matched = state.data?.modules?.find((item) => item.key === module);
  return displayTitle(matched?.title, module || "模块");
}

function renderModuleMatrix(modules) {
  const target = byId("module-matrix");
  target.replaceChildren();
  modules.forEach((item) => {
    const cell = el("div", "module-cell");
    const copy = el("div");
    copy.append(el("strong", "", displayTitle(item.title, item.key)));
    const extra = item.llm_tools?.length ? `字段 ${item.field_count} · 工具 ${item.llm_tools.length}` : `字段 ${item.field_count}`;
    copy.append(el("small", "", extra));
    cell.append(copy, statusPill(item.enabled ? "ready" : "disabled", item.enabled ? "已启用" : "已停用"));
    target.append(cell);
  });
}

function renderRecentActivity(records) {
  const target = byId("recent-activity");
  target.replaceChildren();
  if (!records.length) {
    target.append(emptyState("还没有可显示的运行记录。"));
    return;
  }
  records.forEach((item) => {
    const row = el("div", "activity-item");
    row.append(el("time", "", formatDate(item.at)));
    const copy = el("div");
    copy.append(el("strong", "", `${moduleDisplayName(item.module)} · ${item.action || "操作"}`));
    const detail = item.detail || "无额外说明。";
    copy.append(el("p", "", `${activitySessionLabel(item)} · ${detail}`));
    row.append(copy, statusPill(item.status, runtimeStateLabel(item.status)));
    target.append(row);
  });
}

function renderConfig() {
  const target = byId("config-modules");
  target.replaceChildren();
  state.moduleNodes.clear();
  if (!state.data) return;
  const schema = state.data.schema || {};
  Object.entries(schema).forEach(([moduleKey, entry], index) => {
    const details = el("details", "config-module");
    details.open = index < 2 || moduleKey === "webui";
    const summary = el("summary");
    const copy = el("div", "module-summary-copy");
    copy.append(el("strong", "", displayTitle(entry.description, moduleKey)));
    copy.append(el("small", "", moduleSummaryText(entry)));
    summary.append(copy);
    const meta = el("span", "module-summary-meta", `${fieldCount(entry)} 项设置`);
    summary.append(meta);
    const body = el("div", "module-body");
    const rootNode = renderNode(entry, state.data.config?.[moduleKey], [moduleKey], { topLevel: true });
    const enabledNode = rootNode._children?.get("enabled");
    if (enabledNode?._control) {
      const toggle = el("label", "module-enable");
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.checked = Boolean(enabledNode._control.checked);
      checkbox.addEventListener("click", (event) => event.stopPropagation());
      checkbox.addEventListener("change", () => {
        enabledNode._control.checked = checkbox.checked;
        setDirty(true);
        filterConfigModules();
      });
      toggle.append(checkbox, el("span", "", "启用"));
      toggle.addEventListener("click", (event) => event.stopPropagation());
      summary.append(toggle);
    }
    body.append(rootNode);
    details.append(summary, body);
    details.dataset.search = normalizeSearch(`${moduleKey} ${entry.description || ""} ${collectSchemaText(entry)}`);
    details._moduleKey = moduleKey;
    details._rootNode = rootNode;
    target.append(details);
    state.moduleNodes.set(moduleKey, details);
  });
  filterConfigModules();
}

function moduleSummaryText(entry) {
  const hint = text(entry.hint);
  if (hint) return hint;
  const children = entry.items ? Object.values(entry.items) : [];
  const labels = children.slice(0, 3).map((item) => displayTitle(item.description)).filter(Boolean);
  return labels.length ? labels.join("、") : "配置项";
}

function collectSchemaText(entry) {
  const own = `${entry.description || ""} ${entry.hint || ""}`;
  const children = entry.items ? Object.values(entry.items).map(collectSchemaText).join(" ") : "";
  const templates = entry.templates ? Object.values(entry.templates).map(collectSchemaText).join(" ") : "";
  return `${own} ${children} ${templates}`;
}

function fieldCount(entry) {
  if (entry.type === "object") return Object.values(entry.items || {}).reduce((sum, item) => sum + fieldCount(item), 0);
  if (entry.type === "template_list") return Object.values(entry.templates || {}).reduce((sum, item) => sum + fieldCount(item), 0);
  return 1;
}

function renderNode(entry, value, path, options = {}) {
  const node = el("div", "config-node");
  node._entry = entry;
  node._path = path;
  node._kind = entry.type;
  const type = entry.type;
  if (["text", "object", "template_list", "list", "file"].includes(type)) node.classList.add("full");

  if (type === "object") {
    node._children = new Map();
    if (!options.topLevel) {
      const titleRow = el("div", "nested-title");
      titleRow.append(el("strong", "", displayTitle(entry.description, path.at(-1) || "配置")));
      const count = fieldCount(entry);
      titleRow.append(el("small", "", `${count} 项`));
      node.append(titleRow);
      if (entry.hint) node.append(hintNode(entry.hint));
      node.classList.add("nested-block");
    }
    const grid = el("div", "field-grid");
    Object.entries(entry.items || {}).forEach(([key, child]) => {
      const childNode = renderNode(child, value?.[key], [...path, key]);
      node._children.set(key, childNode);
      grid.append(childNode);
    });
    node.append(grid);
    return node;
  }

  if (type === "template_list") return renderTemplateList(node, entry, Array.isArray(value) ? value : [], path);

  const label = el("div", "field-label", displayTitle(entry.description, path.at(-1) || "设置"));
  node.append(label);
  const secret = isSecret(path);
  if (secret) label.append(el("span", "secret-flag", secretConfigured(path) ? "已配置" : "敏感项"));

  if (type === "bool") {
    const control = el("label", "bool-control");
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = Boolean(value);
    control.append(checkbox, el("span", "", checkbox.checked ? "已开启" : "已关闭"));
    checkbox.addEventListener("change", () => {
      control.querySelector("span").textContent = checkbox.checked ? "已开启" : "已关闭";
    });
    node._control = checkbox;
    node.append(control);
  } else if (type === "file") {
    renderFileControl(node, entry, path);
  } else if (secret) {
    renderSecretControl(node, entry, path);
  } else if (type === "list") {
    renderListControl(node, entry, value);
  } else {
    renderScalarControl(node, entry, value);
  }
  if (entry.hint) node.append(hintNode(entry.hint));
  return node;
}

function hintNode(value) {
  const hint = el("p", "field-hint", value);
  return hint;
}

function isSecret(path) {
  return Boolean(state.data?.secret_state?.[pathKey(path)]);
}

function secretConfigured(path) {
  return Boolean(state.data?.secret_state?.[pathKey(path)]?.configured);
}

function renderScalarControl(node, entry, value) {
  let control;
  if (Array.isArray(entry.options) && entry.options.length) {
    control = document.createElement("select");
    entry.options.forEach((optionValue) => {
      const option = document.createElement("option");
      option.value = String(optionValue);
      option.textContent = String(optionValue);
      option.selected = String(value ?? entry.default ?? "") === String(optionValue);
      control.append(option);
    });
  } else if (entry.type === "text") {
    control = document.createElement("textarea");
    control.value = String(value ?? entry.default ?? "");
  } else {
    control = document.createElement("input");
    control.type = entry.type === "int" || entry.type === "float" ? "number" : "text";
    if (entry.type === "float") control.step = "any";
    if (entry.min !== undefined) control.min = String(entry.min);
    if (entry.max !== undefined) control.max = String(entry.max);
    control.value = String(value ?? entry.default ?? "");
  }
  node._control = control;
  node.append(control);
}

function renderSecretControl(node, entry, path) {
  const holder = el("div", "secret-control");
  const control = document.createElement(entry.type === "text" ? "textarea" : "input");
  if (control instanceof HTMLInputElement) control.type = "password";
  control.placeholder = secretConfigured(path) ? "已保存；留空则保留原值" : "填写后保存";
  control.autocomplete = "new-password";
  const clear = el("label", "secret-clear");
  const checkbox = document.createElement("input");
  checkbox.type = "checkbox";
  clear.append(checkbox, el("span", "", "清除已保存"));
  holder.append(control, clear);
  node._control = control;
  node._clearSecret = checkbox;
  node._secret = true;
  node.append(holder);
}

function renderListControl(node, entry, value) {
  const control = document.createElement("textarea");
  const fallback = Array.isArray(entry.default) ? entry.default : [];
  const values = Array.isArray(value) ? value : fallback;
  const complex = values.some((item) => typeof item === "object" && item !== null) || fallback.some((item) => typeof item === "object" && item !== null);
  node._listMode = complex ? "json" : "lines";
  control.value = complex ? JSON.stringify(values, null, 2) : values.map((item) => String(item)).join("\n");
  control.placeholder = complex ? "请输入 JSON 数组" : "每行一项";
  node._control = control;
  node.append(el("div", "list-control"));
  node.lastChild.append(control);
}

function renderFileControl(node, entry, path) {
  const info = state.data?.file_state?.[pathKey(path)] || {};
  const holder = el("div", "file-control");
  const status = el("span", `file-state ${info.configured ? "ready" : ""}`, info.configured ? `已配置：${info.name || "已上传文件"}` : "未配置文件");
  const input = document.createElement("input");
  input.type = "file";
  input.accept = Array.isArray(entry.file_types) ? entry.file_types.join(",") : "";
  input.addEventListener("change", async () => {
    const [file] = input.files || [];
    if (!file) return;
    await uploadConfigFile(pathKey(path), file);
    input.value = "";
  });
  holder.append(status, input);
  if (info.configured) {
    const clear = el("button", "file-clear", "清除");
    clear.type = "button";
    clear.title = "清除已上传的配置文件";
    clear.addEventListener("click", async () => {
      if (!allowFileConfigMutation()) return;
      if (!await confirmAction("确认清除这个已配置文件吗？", {
        title: "清除配置文件",
        confirmLabel: "确认清除",
      })) return;
      await clearConfigFile(pathKey(path));
    });
    holder.append(clear);
  }
  node._file = true;
  node.append(holder);
}

function renderTemplateList(node, entry, rows, path) {
  node.classList.add("template-list");
  node._kind = "template_list";
  node._rows = [];
  node._templates = entry.templates || {};
  const label = el("div", "field-label", displayTitle(entry.description, path.at(-1) || "列表"));
  node.append(label);
  if (entry.hint) node.append(hintNode(entry.hint));
  const toolbar = el("div", "template-toolbar");
  const selector = document.createElement("select");
  Object.entries(node._templates).forEach(([templateKey, template]) => {
    const option = document.createElement("option");
    option.value = templateKey;
    option.textContent = displayTitle(template.name, templateKey);
    selector.append(option);
  });
  const add = el("button", "template-add", "添加一项");
  add.type = "button";
  add.addEventListener("click", () => {
    const templateKey = selector.value;
    addTemplateRow(node, templateKey, defaultTemplateRow(node._templates[templateKey]), path);
    setDirty(true);
  });
  toolbar.append(selector, add);
  const container = el("div", "template-rows");
  node._rowsContainer = container;
  node.append(toolbar, container);
  rows.forEach((row) => {
    const templateKey = text(row?.__template_key, Object.keys(node._templates)[0] || "");
    if (node._templates[templateKey]) addTemplateRow(node, templateKey, row, path);
  });
  return node;
}

function defaultTemplateRow(template) {
  const result = { __template_key: "" };
  Object.entries(template?.items || {}).forEach(([key, entry]) => {
    result[key] = defaultValue(entry);
  });
  return result;
}

function defaultValue(entry) {
  if (Object.prototype.hasOwnProperty.call(entry, "default")) return cloneValue(entry.default);
  if (entry.type === "object") {
    return Object.fromEntries(Object.entries(entry.items || {}).map(([key, value]) => [key, defaultValue(value)]));
  }
  if (["list", "template_list", "file"].includes(entry.type)) return [];
  if (entry.type === "bool") return false;
  if (entry.type === "int" || entry.type === "float") return 0;
  return "";
}

function cloneValue(value) {
  if (typeof structuredClone === "function") return structuredClone(value);
  return JSON.parse(JSON.stringify(value));
}

function addTemplateRow(listNode, templateKey, values, basePath, insertionIndex = null) {
  const template = listNode._templates?.[templateKey];
  if (!template) return;
  const row = el("article", "template-row");
  row._templateKey = templateKey;
  row._children = new Map();
  const header = el("div", "template-row-header");
  const title = el("strong", "", displayTitle(template.name, templateKey));
  header.append(title);
  if (Object.keys(listNode._templates).length > 1) {
    const selector = document.createElement("select");
    Object.entries(listNode._templates).forEach(([key, item]) => {
      const option = document.createElement("option");
      option.value = key;
      option.textContent = displayTitle(item.name, key);
      option.selected = key === templateKey;
      selector.append(option);
    });
    selector.addEventListener("change", () => {
      const position = listNode._rows.indexOf(row);
      if (position < 0) return;
      row.remove();
      listNode._rows.splice(position, 1);
      const next = defaultTemplateRow(listNode._templates[selector.value]);
      addTemplateRow(listNode, selector.value, next, basePath, position);
      setDirty(true);
    });
    header.append(selector);
  }
  const remove = el("button", "template-remove", "×");
  remove.type = "button";
  remove.title = "删除这一项";
  remove.setAttribute("aria-label", "删除这一项");
  remove.addEventListener("click", () => {
    row.remove();
    listNode._rows = listNode._rows.filter((item) => item !== row);
    setDirty(true);
  });
  header.append(remove);
  row.append(header);
  const grid = el("div", "field-grid");
  Object.entries(template.items || {}).forEach(([key, entry]) => {
    const child = renderNode(entry, values?.[key], [...basePath, String(listNode._rows.length), key]);
    row._children.set(key, child);
    grid.append(child);
  });
  row.append(grid);
  const position = Number.isInteger(insertionIndex) ? insertionIndex : listNode._rows.length;
  if (position >= listNode._rows.length) {
    listNode._rows.push(row);
    listNode._rowsContainer.append(row);
  } else {
    listNode._rows.splice(position, 0, row);
    listNode._rowsContainer.insertBefore(row, listNode._rowsContainer.children[position] || null);
  }
}

function readNode(node) {
  const entry = node._entry;
  if (node._kind === "object") {
    return Object.fromEntries([...node._children.entries()].map(([key, child]) => [key, readNode(child)]));
  }
  if (node._kind === "template_list") {
    return node._rows.filter((row) => row.isConnected).map((row) => {
      const result = { __template_key: row._templateKey };
      row._children.forEach((child, key) => { result[key] = readNode(child); });
      return result;
    });
  }
  if (node._file) return [];
  if (node._secret) {
    if (node._clearSecret.checked) return { __helper_tools_secret_action: "clear" };
    const replacement = text(node._control.value);
    if (replacement) return { __helper_tools_secret_action: "replace", value: replacement };
    return { __helper_tools_secret_action: "keep" };
  }
  if (entry.type === "bool") return Boolean(node._control.checked);
  if (entry.type === "list") {
    const raw = node._control.value;
    if (node._listMode === "json") {
      if (!text(raw)) return [];
      try {
        const parsed = JSON.parse(raw);
        if (!Array.isArray(parsed)) throw new Error("not array");
        return parsed;
      } catch {
        throw new Error(`${displayTitle(entry.description, "列表")} 的 JSON 必须是数组。`);
      }
    }
    return raw.split(/\r?\n/).map((item) => item.trim()).filter(Boolean);
  }
  if (entry.type === "int" || entry.type === "float") return node._control.value;
  return node._control.value;
}

function valuesEqual(left, right) {
  try {
    return JSON.stringify(left) === JSON.stringify(right);
  } catch {
    return false;
  }
}

function wallpaperConfigFormRows() {
  const details = state.moduleNodes.get("wallpaper");
  const node = details?._rootNode?._children?.get("libraries");
  if (!node || node._kind !== "template_list") return null;
  try {
    return readNode(node);
  } catch {
    // An invalid JSON field elsewhere should not prevent the wallpaper page
    // from refreshing its own server-side configuration snapshot.
    return null;
  }
}

function replaceWallpaperConfigFormRows(rows) {
  const nextRows = Array.isArray(rows) ? cloneValue(rows) : [];
  if (state.data?.config?.wallpaper) {
    state.data.config.wallpaper.libraries = nextRows;
  }
  const details = state.moduleNodes.get("wallpaper");
  const oldNode = details?._rootNode?._children?.get("libraries");
  const schemaEntry = state.data?.schema?.wallpaper?.items?.libraries;
  if (!details?._rootNode?._children || !oldNode || !schemaEntry) return;
  const replacement = renderNode(schemaEntry, nextRows, ["wallpaper", "libraries"]);
  oldNode.replaceWith(replacement);
  details._rootNode._children.set("libraries", replacement);
}

function reconcileWallpaperConfigRows(rows, previousServerRows = state.wallpaper.serverConfigRows) {
  if (!Array.isArray(rows)) return;
  const localRows = wallpaperConfigFormRows();
  const formMatchesServer = (
    localRows === null
    || (Array.isArray(previousServerRows) && valuesEqual(localRows, previousServerRows))
  );
  // Preserve an intentional edit in the generic module form. When the form
  // still matches the last server snapshot, it is safe to update it with a
  // library created by the dedicated manager or by a message handler.
  if (formMatchesServer || !state.dirty) replaceWallpaperConfigFormRows(rows);
  state.wallpaper.serverConfigRows = cloneValue(rows);
}

async function refreshLatestWallpaperConfig(config) {
  const localRows = config?.wallpaper?.libraries;
  const previousServerRows = state.wallpaper.serverConfigRows;
  if (!Array.isArray(localRows) || !Array.isArray(previousServerRows)) return;
  if (!valuesEqual(localRows, previousServerRows)) return;

  const response = await apiGet("wallpaper_libraries");
  if (!response?.success || !Array.isArray(response.config_libraries)) {
    throw new Error("无法确认最新图库配置，请刷新控制台后再保存。");
  }
  const latestRows = response.config_libraries;
  if (!valuesEqual(localRows, latestRows)) {
    config.wallpaper.libraries = cloneValue(latestRows);
    replaceWallpaperConfigFormRows(latestRows);
  }
  state.wallpaper.serverConfigRows = cloneValue(latestRows);
}

function buildConfigPayload() {
  return Object.fromEntries([...state.moduleNodes.entries()].map(([key, details]) => [key, readNode(details._rootNode)]));
}

function filterConfigModules() {
  const query = normalizeSearch(byId("config-search").value);
  const enabledOnly = byId("enabled-only").checked;
  let visible = 0;
  state.moduleNodes.forEach((details) => {
    const matches = !query || details.dataset.search.includes(query);
    const enabledNode = details._rootNode?._children?.get("enabled");
    const enabled = enabledNode?._control ? Boolean(enabledNode._control.checked) : true;
    const show = matches && (!enabledOnly || enabled);
    details.hidden = !show;
    if (show && query) details.open = true;
    if (show) visible += 1;
  });
  byId("config-count").textContent = `显示 ${visible}/${state.moduleNodes.size} 个模块`;
}

function setConfigModulesOpen(open) {
  state.moduleNodes.forEach((details) => {
    if (!details.hidden) details.open = open;
  });
}

async function saveConfig() {
  if (!state.data) return false;
  let config;
  try {
    config = buildConfigPayload();
    await refreshLatestWallpaperConfig(config);
  } catch (error) {
    showToast(error.message || "请先修正配置格式。", true);
    return false;
  }
  const button = byId("save-button");
  button.disabled = true;
  setHeaderState("正在保存配置...");
  try {
    const payload = { config };
    if (Array.isArray(state.wallpaper.serverConfigRows)) {
      payload.wallpaper_config_snapshot = cloneValue(state.wallpaper.serverConfigRows);
    }
    const response = await apiPost("save_config", payload);
    if (!response?.success) throw new Error(response?.message || "配置保存失败。");
    showToast(response.message || "配置已保存。");
    setDirty(false);
    await loadState();
    if (response.reload_recommended) {
      showToast("配置已保存。涉及调度或运行资源的改动，建议到 AstrBot 插件页重载本插件。", false);
    }
    return true;
  } catch (error) {
    setHeaderState("保存失败。", true);
    showToast(error.message || "配置保存失败。", true);
    return false;
  } finally {
    button.disabled = false;
  }
}

function readFileAsDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(new Error("无法读取选择的文件。"));
    reader.readAsDataURL(file);
  });
}

function allowFileConfigMutation() {
  if (!state.dirty) return true;
  showToast("还有未保存的配置修改。请先保存配置，再上传或清除文件。", true);
  return false;
}

async function uploadConfigFile(path, file) {
  try {
    if (!allowFileConfigMutation()) return;
    if (file.size > 24 * 1024 * 1024) throw new Error("上传文件不能超过 24 MB。");
    setHeaderState("正在上传配置文件...");
    const dataUrl = await readFileAsDataUrl(file);
    const response = await apiPost("upload_file", { path, filename: file.name, data_url: dataUrl });
    if (!response?.success) throw new Error(response?.message || "文件上传失败。");
    showToast(response.message || "文件已保存。");
    setDirty(false);
    await loadState();
  } catch (error) {
    showToast(error.message || "文件上传失败。", true);
  }
}

async function clearConfigFile(path) {
  try {
    if (!allowFileConfigMutation()) return;
    const response = await apiPost("clear_file", { path });
    if (!response?.success) throw new Error(response?.message || "清除文件失败。");
    showToast(response.message || "文件配置已清除。");
    await loadState();
  } catch (error) {
    showToast(error.message || "清除文件失败。", true);
  }
}

function renderActivity(response) {
  state.activity = response;
  const body = byId("activity-table-body");
  body.replaceChildren();
  const records = response?.records || [];
  if (!records.length) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 6;
    cell.className = "empty-state";
    cell.textContent = "没有符合条件的运行记录。";
    row.append(cell);
    body.append(row);
  } else {
    records.forEach((record) => {
      const row = document.createElement("tr");
      [
        formatDate(record.at, true),
        activitySessionLabel(record),
        moduleDisplayName(record.module),
        record.action || "操作",
        runtimeStateLabel(record.status),
        record.detail || "-",
      ].forEach((value) => row.append(el("td", "", value)));
      body.append(row);
    });
  }
  renderActivitySummary(response?.summary || {});
  populateActivityModuleFilter(records);
}

function renderActivitySummary(summary) {
  const target = byId("activity-summary");
  target.replaceChildren(
    summaryMetric("本地记录", summary.total || 0),
    summaryMetric("近 24 小时", summary.today || 0),
    summaryMetric("近 7 天", summary.recent_week || 0),
    summaryMetric("异常与警告", summary.failure_count || 0),
  );
}

function summaryMetric(label, value) {
  const node = el("span");
  node.append(document.createTextNode(label), el("strong", "", String(value)));
  return node;
}

function populateActivityModuleFilter(records) {
  const select = byId("activity-module-filter");
  const previous = select.value;
  const modules = new Map((state.data?.modules || []).map((item) => [item.key, displayTitle(item.title, item.key)]));
  records.forEach((record) => { if (record.module) modules.set(record.module, moduleDisplayName(record.module)); });
  select.replaceChildren();
  const all = document.createElement("option");
  all.value = "";
  all.textContent = "全部模块";
  select.append(all);
  [...modules.entries()].sort(([left], [right]) => left.localeCompare(right)).forEach(([key, label]) => {
    const option = document.createElement("option");
    option.value = key;
    option.textContent = label;
    option.selected = key === previous;
    select.append(option);
  });
}

async function loadActivities() {
  const body = byId("activity-table-body");
  body.replaceChildren();
  const loading = document.createElement("tr");
  const cell = el("td", "loading-state", "正在读取运行记录...");
  cell.colSpan = 6;
  loading.append(cell);
  body.append(loading);
  try {
    const response = await apiGet("activities", {
      limit: 160,
      module: byId("activity-module-filter").value,
      status: byId("activity-status-filter").value,
    });
    if (!response?.success) throw new Error(response?.message || "无法读取运行记录。");
    renderActivity(response);
  } catch (error) {
    showToast(error.message || "无法读取运行记录。", true);
    renderActivity({ records: [], summary: {} });
  }
}

async function clearActivities() {
  if (!await confirmAction("确认清空本插件控制台保存的全部运行记录吗？此操作不能恢复。", {
    title: "清空运行记录",
    confirmLabel: "确认清空",
  })) return;
  try {
    const response = await apiPost("clear_activities", {});
    if (!response?.success) throw new Error(response?.message || "清空失败。");
    showToast(response.message || "运行记录已清空。");
    await loadActivities();
    await loadState();
  } catch (error) {
    showToast(error.message || "清空运行记录失败。", true);
  }
}

function renderStorage(storage, runtime) {
  const source = storage || {};
  const metrics = byId("storage-metrics");
  metrics.replaceChildren(
    metric("文件数量", source.total_files || 0),
    metric("数据占用", formatBytes(source.total_bytes || 0)),
    metric("最新写入", source.latest_modified_at ? formatDate(source.latest_modified_at) : "-"),
    metric("扫描状态", source.truncated ? "已截断" : "完整"),
  );
  renderRuntimeList(byId("storage-runtime-list"), runtime || []);
  const buckets = byId("storage-buckets");
  buckets.replaceChildren();
  if (!source.buckets?.length) {
    buckets.append(emptyState("插件数据目录当前没有可统计的文件。"));
  } else {
    source.buckets.forEach((bucket) => {
      const row = el("div", "storage-row");
      const copy = el("div");
      copy.append(el("strong", "", bucket.name || "根目录"));
      copy.append(el("small", "", bucket.latest_modified_at ? `最近写入 ${formatDate(bucket.latest_modified_at, true)}` : "没有可用的修改时间"));
      const stat = el("div", "storage-stat", formatBytes(bucket.bytes || 0));
      stat.append(el("small", "", `${bucket.files || 0} 个文件`));
      row.append(copy, stat);
      buckets.append(row);
    });
  }
  byId("storage-note").textContent = source.truncated
    ? "文件数量较多，控制台为避免影响 Bot 消息处理，只统计了前 30,000 个本地文件。"
    : "这里只统计插件自己的本地数据目录，不会读取或传出聊天正文、缓存图片或凭据内容。";
}

async function loadStorage(force = false) {
  if (!force && Date.now() - state.storageLoadedAt < 4_000 && state.data) {
    renderStorage(state.data.storage, state.data.runtime);
    return;
  }
  try {
    const response = await apiGet("storage");
    if (!response?.success) throw new Error(response?.message || "无法读取存储状态。");
    state.storageLoadedAt = Date.now();
    renderStorage(response.storage, response.runtime);
  } catch (error) {
    showToast(error.message || "无法读取存储状态。", true);
  }
}

function selectedWallpaperLibrary() {
  const selected = String(state.wallpaper.selectedLibraryId || "");
  return state.wallpaper.libraries.find((item) => String(item.id) === selected) || null;
}

function wallpaperLibraryStateLabel(value) {
  const labels = {
    ready: "可用",
    missing: "待创建",
    not_directory: "路径无效",
    unsafe: "受限",
  };
  return labels[value] || "未知";
}

function wallpaperImageParams(image) {
  return {
    library_id: String(image.library_id ?? state.wallpaper.selectedLibraryId),
    path: image.relative_path,
  };
}

function wallpaperImageUrl(endpoint, image) {
  return apiUrl(endpoint, wallpaperImageParams(image));
}

function wallpaperThumbnailKey(image) {
  const params = wallpaperImageParams(image);
  return `${params.library_id}:${params.path}`;
}

function cacheWallpaperThumbnail(key, dataUrl) {
  state.wallpaper.thumbnailCache.set(key, dataUrl);
  while (state.wallpaper.thumbnailCache.size > 72) {
    const oldest = state.wallpaper.thumbnailCache.keys().next().value;
    state.wallpaper.thumbnailCache.delete(oldest);
  }
}

function clearWallpaperThumbnailCache() {
  state.wallpaper.thumbnailCache.clear();
  state.wallpaper.thumbnailRequests.clear();
}

async function getWallpaperThumbnailData(image) {
  const key = wallpaperThumbnailKey(image);
  const cached = state.wallpaper.thumbnailCache.get(key);
  if (cached) return cached;
  const pending = state.wallpaper.thumbnailRequests.get(key);
  if (pending) return pending;
  const request = apiGet("wallpaper_thumbnail_data", wallpaperImageParams(image))
    .then((response) => {
      const dataUrl = text(response?.data_url);
      if (!response?.success || !dataUrl.startsWith("data:image/jpeg;base64,")) {
        throw new Error(response?.message || "无法读取缩略图。");
      }
      cacheWallpaperThumbnail(key, dataUrl);
      return dataUrl;
    })
    .finally(() => state.wallpaper.thumbnailRequests.delete(key));
  state.wallpaper.thumbnailRequests.set(key, request);
  return request;
}

function pumpWallpaperThumbnailQueue() {
  while (state.wallpaper.thumbnailLoading < 4 && state.wallpaper.thumbnailQueue.length) {
    const task = state.wallpaper.thumbnailQueue.shift();
    state.wallpaper.thumbnailLoading += 1;
    void task().finally(() => {
      state.wallpaper.thumbnailLoading -= 1;
      pumpWallpaperThumbnailQueue();
    });
  }
}

function renderWallpaperThumbnail(media, image) {
  const preview = document.createElement("img");
  const fallback = el("span", "wallpaper-image-fallback", "正在生成缩略图...");
  preview.alt = image.name || "壁纸缩略图";
  preview.loading = "lazy";
  preview.hidden = true;
  preview.addEventListener("error", () => {
    preview.hidden = true;
    fallback.hidden = false;
    fallback.textContent = "缩略图不可用";
  }, { once: true });
  media.append(preview, fallback);
  state.wallpaper.thumbnailQueue.push(async () => {
    try {
      const dataUrl = await getWallpaperThumbnailData(image);
      if (!media.isConnected) return;
      preview.src = dataUrl;
      preview.hidden = false;
      fallback.hidden = true;
    } catch (error) {
      if (!media.isConnected) return;
      fallback.textContent = error.message || "缩略图不可用";
    }
  });
  pumpWallpaperThumbnailQueue();
}

function wallpaperSelectedSummary(library, response = null) {
  if (!library) return "先从上方选择一个图库。";
  const current = response?.library || library;
  const count = Number(current.image_count ?? library.image_count ?? 0);
  return `${library.name} · ${count} 张图片`;
}

function renderWallpaperMetrics() {
  const target = byId("wallpaper-metrics");
  const libraries = state.wallpaper.libraries || [];
  const images = libraries.reduce((total, item) => total + (Number(item.image_count) || 0), 0);
  const bytes = libraries.reduce((total, item) => total + (Number(item.total_bytes) || 0), 0);
  const attention = libraries.filter((item) => item.state !== "ready").length;
  target.replaceChildren(
    metric("已配置图库", libraries.length),
    metric("已索引图片", images),
    metric("图片占用", formatBytes(bytes)),
    metric("待处理目录", attention),
  );
  renderTabBadges();
}

function wallpaperActionButton(symbol, label, action, image = null, extraClass = "") {
  const button = el("button", `wallpaper-icon-button ${extraClass}`.trim(), symbol);
  button.type = "button";
  button.title = label;
  button.setAttribute("aria-label", label);
  button.dataset.wallpaperAction = action;
  if (image) button.dataset.wallpaperPath = image.relative_path;
  return button;
}

function renderWallpaperLibraries() {
  const target = byId("wallpaper-library-list");
  target.replaceChildren();
  const libraries = state.wallpaper.libraries || [];
  if (!libraries.length) {
    target.append(emptyState("还没有可管理的图库。"));
    return;
  }
  libraries.forEach((library) => {
    const row = el("div", "wallpaper-library-row");
    if (String(library.id) === String(state.wallpaper.selectedLibraryId)) row.classList.add("selected");
    const select = el("button", "wallpaper-library-select");
    select.type = "button";
    select.dataset.wallpaperAction = "select-library";
    select.dataset.wallpaperLibraryId = String(library.id);
    select.append(el("strong", "", library.name || "未命名图库"));
    select.append(el("small", "", `${library.image_count || 0} 张 · ${formatBytes(library.total_bytes || 0)}`));
    const status = el("div", "wallpaper-library-status");
    const stateName = library.state === "ready" ? "ready" : "warning";
    status.append(statusPill(stateName, wallpaperLibraryStateLabel(library.state)));
    if (library.scan_truncated) status.append(el("small", "", "索引已截断"));
    select.append(status);

    const actions = el("div", "wallpaper-library-actions");
    const edit = wallpaperActionButton("✎", "编辑图库", "edit-library");
    edit.dataset.wallpaperLibraryId = String(library.id);
    const remove = wallpaperActionButton("-", "仅删除图库配置", "delete-library");
    remove.dataset.wallpaperLibraryId = String(library.id);
    const purge = wallpaperActionButton("x", "删除图库配置，并删除目录内的图片文件", "delete-library-files", null, "danger");
    purge.dataset.wallpaperLibraryId = String(library.id);
    actions.append(edit, remove, purge);
    row.append(select, actions);
    target.append(row);
  });
}

function renderWallpaperLibraryPicker() {
  const picker = byId("wallpaper-library-picker");
  const status = byId("wallpaper-library-status");
  const library = selectedWallpaperLibrary();
  picker.replaceChildren();
  const libraries = state.wallpaper.libraries || [];
  if (!libraries.length) {
    const option = el("option", "", "没有可管理的图库");
    option.value = "";
    picker.append(option);
  } else {
    libraries.forEach((item) => {
      const option = el(
        "option",
        "",
        `${item.name || "未命名图库"} · ${Number(item.image_count) || 0} 张 · ${wallpaperLibraryStateLabel(item.state)}`,
      );
      option.value = String(item.id);
      picker.append(option);
    });
    picker.value = String(state.wallpaper.selectedLibraryId || libraries[0].id);
  }
  picker.disabled = !library;
  status.replaceChildren();
  if (library) {
    status.append(statusPill(library.state === "ready" ? "ready" : "warning", wallpaperLibraryStateLabel(library.state)));
    if (library.scan_truncated) status.append(el("small", "", "索引已截断"));
    if (!library.writable && library.state === "ready") status.append(el("small", "", "目录不可写"));
  }
  ["wallpaper-library-edit", "wallpaper-library-remove-config", "wallpaper-library-delete-files"].forEach((id) => {
    const button = byId(id);
    if (button) button.disabled = !library;
  });
}

function renderWallpaperBrowserState(response = null) {
  const library = selectedWallpaperLibrary();
  const summary = byId("wallpaper-selection-summary");
  summary.textContent = wallpaperSelectedSummary(library, response);
  summary.title = text(response?.library?.resolved_path || library?.resolved_path);
  const note = byId("wallpaper-browser-note");
  const meta = byId("wallpaper-browser-meta");
  const detail = text(response?.library?.detail || library?.detail);
  const scanTruncated = Boolean(response?.pagination?.scan_truncated || library?.scan_truncated);
  note.textContent = [detail, scanTruncated ? "图片索引较大，当前只显示已扫描范围。" : ""].filter(Boolean).join(" ");
  note.hidden = !note.textContent;
  summary.hidden = true;
  meta.hidden = !note.textContent;
  const selected = Boolean(library);
  byId("wallpaper-upload-input").disabled = !selected;
  byId("wallpaper-image-search").disabled = !selected;
  byId("wallpaper-image-sort").disabled = !selected;
  byId("wallpaper-images-refresh").disabled = !selected;
  updateWallpaperUploadSelection();
}

function imageMetaText(image) {
  const dimensions = image.width && image.height ? `${image.width} × ${image.height}` : "尺寸未知";
  const frameText = Number(image.frames) > 1 ? ` · ${image.frames} 帧` : "";
  return `${dimensions} · ${image.format || "文件"} · ${formatBytes(image.bytes || 0)}${frameText}`;
}

function renderWallpaperImages(response) {
  const target = byId("wallpaper-image-grid");
  target.replaceChildren();
  const library = selectedWallpaperLibrary();
  if (!library) {
    target.append(emptyState("选择图库后即可查看图片。"));
    renderWallpaperPagination(null);
    return;
  }
  const images = response?.images || [];
  if (!images.length) {
    target.append(emptyState(response?.query ? "没有匹配的图片。" : "这个图库还没有图片。"));
    renderWallpaperPagination(response?.pagination || null);
    return;
  }
  images.forEach((image) => {
    const card = el("article", "wallpaper-image-card");
    const media = el("div", "wallpaper-image-media");
    if (image.preview_supported) {
      renderWallpaperThumbnail(media, image);
    } else {
      media.append(el("span", "wallpaper-image-fallback", image.error || "仅支持下载原文件"));
    }
    const body = el("div", "wallpaper-image-body");
    const name = el("strong", "wallpaper-image-name", image.name || "未命名图片");
    name.title = image.name || "";
    const relative = el("small", "wallpaper-image-path", image.relative_path || "");
    relative.title = image.relative_path || "";
    body.append(name, relative, el("p", "wallpaper-image-meta", imageMetaText(image)));
    const actions = el("div", "wallpaper-image-actions");
    if (image.preview_supported) actions.append(wallpaperActionButton("⌕", "预览图片", "preview-image", image));
    actions.append(wallpaperActionButton("↓", "下载原图", "download-image", image));
    actions.append(wallpaperActionButton("✎", "重命名图片", "rename-image", image));
    actions.append(wallpaperActionButton("×", "删除图片", "delete-image", image, "danger"));
    body.append(actions);
    card.append(media, body);
    target.append(card);
  });
  renderWallpaperPagination(response?.pagination || null);
}

function renderWallpaperPagination(pagination) {
  const target = byId("wallpaper-pagination");
  target.replaceChildren();
  if (!pagination || Number(pagination.total) <= 0) return;
  const previous = wallpaperActionButton("‹", "上一页", "wallpaper-page");
  previous.dataset.wallpaperPage = String(Math.max(1, Number(pagination.page) - 1));
  previous.disabled = Number(pagination.page) <= 1;
  const next = wallpaperActionButton("›", "下一页", "wallpaper-page");
  next.dataset.wallpaperPage = String(Math.min(Number(pagination.page_count), Number(pagination.page) + 1));
  next.disabled = Number(pagination.page) >= Number(pagination.page_count);
  target.append(previous, el("span", "", `${pagination.page} / ${pagination.page_count} · ${pagination.total} 张`), next);
}

function renderWallpaperLoading(message = "正在读取图片…") {
  byId("wallpaper-image-grid").replaceChildren(el("p", "loading-state", message));
  byId("wallpaper-pagination").replaceChildren();
}

async function loadWallpaperLibraries(options = {}) {
  const requestId = ++state.wallpaper.libraryRequestId;
  if (!options.silent) byId("wallpaper-library-list").replaceChildren(el("p", "loading-state", "正在读取图库…"));
  try {
    const response = await apiGet("wallpaper_libraries");
    if (!response?.success) throw new Error(response?.message || "无法读取壁纸库。");
    if (requestId !== state.wallpaper.libraryRequestId) return;
    const previousServerRows = state.wallpaper.serverConfigRows;
    reconcileWallpaperConfigRows(response.config_libraries, previousServerRows);
    state.wallpaper.libraries = Array.isArray(response.libraries) ? response.libraries : [];
    state.wallpaper.uploadMaxBytes = Number(response.upload_max_bytes) || state.wallpaper.uploadMaxBytes;
    state.wallpaper.uploadFileLimit = Number(response.upload_file_limit) || state.wallpaper.uploadFileLimit;
    const selectedStillExists = state.wallpaper.libraries.some((item) => String(item.id) === String(state.wallpaper.selectedLibraryId));
    if (!selectedStillExists) state.wallpaper.selectedLibraryId = state.wallpaper.libraries[0] ? String(state.wallpaper.libraries[0].id) : "";
    renderWallpaperMetrics();
    renderWallpaperLibraries();
    renderWallpaperLibraryPicker();
    renderWallpaperBrowserState();
    if (options.loadImages !== false && state.wallpaper.selectedLibraryId) {
      await loadWallpaperImages({ page: options.page || 1 });
    } else if (!state.wallpaper.selectedLibraryId) {
      renderWallpaperImages(null);
    }
  } catch (error) {
    if (requestId !== state.wallpaper.libraryRequestId) return;
    byId("wallpaper-library-list").replaceChildren(emptyState("图库读取失败。"));
    renderWallpaperBrowserState();
    showToast(error.message || "图库读取失败。", true);
  }
}

async function loadWallpaperImages(options = {}) {
  const library = selectedWallpaperLibrary();
  if (!library) {
    renderWallpaperImages(null);
    return;
  }
  const requestId = ++state.wallpaper.imageRequestId;
  const page = Number(options.page || state.wallpaper.pagination?.page || 1);
  renderWallpaperLoading();
  try {
    const response = await apiGet("wallpaper_images", {
      library_id: String(library.id),
      page,
      page_size: 36,
      query: state.wallpaper.query,
      sort: state.wallpaper.sort,
    });
    if (!response?.success) throw new Error(response?.message || "无法读取图库图片。");
    if (requestId !== state.wallpaper.imageRequestId) return;
    state.wallpaper.images = Array.isArray(response.images) ? response.images : [];
    state.wallpaper.pagination = response.pagination || null;
    renderWallpaperBrowserState(response);
    renderWallpaperImages(response);
  } catch (error) {
    if (requestId !== state.wallpaper.imageRequestId) return;
    byId("wallpaper-image-grid").replaceChildren(emptyState("图片读取失败。"));
    byId("wallpaper-pagination").replaceChildren();
    showToast(error.message || "图片读取失败。", true);
  }
}

function updateWallpaperUploadSelection() {
  const input = byId("wallpaper-upload-input");
  const selection = byId("wallpaper-upload-selection");
  const upload = byId("wallpaper-upload-button");
  const files = Array.from(input.files || []);
  const hasLibrary = Boolean(selectedWallpaperLibrary());
  const total = files.reduce((sum, file) => sum + file.size, 0);
  const tooMany = files.length > state.wallpaper.uploadFileLimit;
  const tooLarge = files.some((file) => file.size > state.wallpaper.uploadMaxBytes);
  if (!files.length) {
    selection.textContent = "未选择文件";
  } else if (tooMany) {
    selection.textContent = `一次最多 ${state.wallpaper.uploadFileLimit} 张`;
  } else if (tooLarge) {
    selection.textContent = `单张不超过 ${formatBytes(state.wallpaper.uploadMaxBytes)}`;
  } else {
    selection.textContent = `${files.length} 张 · ${formatBytes(total)}`;
  }
  upload.disabled = !hasLibrary || !files.length || tooMany || tooLarge;
}

async function uploadWallpaperImages() {
  const library = selectedWallpaperLibrary();
  const input = byId("wallpaper-upload-input");
  const files = Array.from(input.files || []);
  if (!library || !files.length) return;
  const upload = byId("wallpaper-upload-button");
  const previousText = upload.textContent;
  upload.disabled = true;
  upload.textContent = "上传中…";
  try {
    let response;
    const bridge = getBridge();
    if (bridge?.upload) {
      const saved = [];
      const skipped = [];
      const errors = [];
      for (const file of files) {
        try {
          const item = await bridge.upload(
            `wallpaper_upload_file/${encodeURIComponent(String(library.id))}`,
            file,
          );
          if (!item?.success) throw new Error(item?.message || "上传失败。");
          saved.push(...(Array.isArray(item.saved) ? item.saved : []));
          skipped.push(...(Array.isArray(item.skipped) ? item.skipped : []));
          errors.push(...(Array.isArray(item.errors) ? item.errors : []));
        } catch (error) {
          errors.push({ filename: file.name, message: error.message || "上传失败。" });
        }
      }
      const parts = [];
      if (saved.length) parts.push(`已添加 ${saved.length} 张图片`);
      if (skipped.length) parts.push(`跳过 ${skipped.length} 张重复图片`);
      if (errors.length) parts.push(`${errors.length} 张图片未能添加`);
      response = {
        success: true,
        saved,
        skipped,
        errors,
        message: parts.length ? `${parts.join("，")}。` : "没有可添加的图片。",
      };
    } else {
      const formData = new FormData();
      formData.append("library_id", String(library.id));
      files.forEach((file) => formData.append("files", file, file.name));
      response = await apiMultipartPost("wallpaper_upload", formData);
    }
    if (!response?.success) throw new Error(response?.message || "上传壁纸失败。");
    input.value = "";
    updateWallpaperUploadSelection();
    await loadWallpaperLibraries({ loadImages: false, silent: true });
    await loadWallpaperImages({ page: 1 });
    showToast(response.message || "壁纸已上传。", !response.saved?.length);
  } catch (error) {
    showToast(error.message || "上传壁纸失败。", true);
  } finally {
    upload.textContent = previousText;
    updateWallpaperUploadSelection();
  }
}

function showWallpaperDialog(id) {
  const dialog = byId(id);
  if (!dialog) return;
  if (typeof dialog.showModal === "function") {
    if (!dialog.open) dialog.showModal();
  } else {
    dialog.setAttribute("open", "");
  }
}

function closeWallpaperDialog(id) {
  const dialog = byId(id);
  if (!dialog) return;
  if (typeof dialog.close === "function" && dialog.open) dialog.close();
  else dialog.removeAttribute("open");
}

function normalizeWallpaperSendMode(value) {
  const source = text(value);
  if (source === "caption_first") return "先发文案再发图";
  if (source === "image_only") return "只发图片";
  if (source === "together") return "同一条消息";
  return ["同一条消息", "先发文案再发图", "只发图片"].includes(source) ? source : "同一条消息";
}

function openWallpaperLibraryDialog(library = null) {
  closeWallpaperDialog("wallpaper-library-management-dialog");
  byId("wallpaper-library-dialog-title").textContent = library ? "编辑图库" : "新增图库";
  byId("wallpaper-library-id").value = library ? String(library.id) : "";
  byId("wallpaper-library-name").value = library?.name || "";
  byId("wallpaper-library-path").value = library?.configured_path || "";
  byId("wallpaper-library-commands").value = Array.isArray(library?.commands) ? library.commands.join("\n") : "";
  byId("wallpaper-library-caption").value = library?.caption || "随机给你抽一张 {library}。";
  byId("wallpaper-library-send-mode").value = normalizeWallpaperSendMode(library?.send_mode);
  byId("wallpaper-library-recursive").checked = Boolean(library?.recursive);
  byId("wallpaper-library-create-directory").checked = true;
  showWallpaperDialog("wallpaper-library-dialog");
  byId("wallpaper-library-name").focus();
}

async function saveWallpaperLibrary() {
  const libraryId = text(byId("wallpaper-library-id").value);
  const name = text(byId("wallpaper-library-name").value);
  const commands = byId("wallpaper-library-commands").value
    .split(/\r?\n/)
    .map((item) => text(item))
    .filter(Boolean);
  const payload = {
    library_id: libraryId || null,
    create_directory: byId("wallpaper-library-create-directory").checked,
    library: {
      name,
      path: byId("wallpaper-library-path").value,
      commands,
      caption: byId("wallpaper-library-caption").value,
      send_mode: byId("wallpaper-library-send-mode").value,
      recursive: byId("wallpaper-library-recursive").checked,
    },
  };
  try {
    const response = await apiPost("wallpaper_save_library", payload);
    if (!response?.success) throw new Error(response?.message || "保存图库失败。");
    state.wallpaper.selectedLibraryId = String(response.library?.id ?? libraryId);
    closeWallpaperDialog("wallpaper-library-dialog");
    await loadWallpaperLibraries({ page: 1 });
    showToast(response.message || "图库已保存。");
  } catch (error) {
    showToast(error.message || "保存图库失败。", true);
  }
}

async function deleteWallpaperLibrary(libraryId, options = {}) {
  const library = state.wallpaper.libraries.find((item) => String(item.id) === String(libraryId));
  if (!library) return false;
  const deleteFiles = Boolean(options.deleteFiles);
  if (
    !deleteFiles
    && !await confirmAction(`删除图库“${library.name}”的配置？磁盘中的图片不会删除。`, {
      title: "删除图库配置",
      confirmLabel: "删除配置",
    })
  ) return false;
  try {
    const response = await apiPost("wallpaper_delete_library", {
      library_id: String(library.id),
      confirm: true,
      delete_files: deleteFiles,
      confirmation_name: options.confirmationName || "",
    });
    if (!response?.success) throw new Error(response?.message || "删除图库失败。");
    if (String(state.wallpaper.selectedLibraryId) === String(library.id)) state.wallpaper.selectedLibraryId = "";
    clearWallpaperThumbnailCache();
    await loadWallpaperLibraries({ page: 1 });
    showToast(response.message || "图库配置已删除。");
    return true;
  } catch (error) {
    showToast(error.message || "删除图库失败。", true);
    return false;
  }
}

function openWallpaperLibraryPurge(libraryId) {
  const library = state.wallpaper.libraries.find((item) => String(item.id) === String(libraryId));
  if (!library) return;
  closeWallpaperDialog("wallpaper-library-management-dialog");
  byId("wallpaper-library-purge-id").value = String(library.id);
  byId("wallpaper-library-purge-name").value = "";
  byId("wallpaper-library-purge-note").textContent = `将删除“${library.name}”的配置，以及 ${library.resolved_path || "对应目录"} 内被索引到的图片文件（仅限允许的图片扩展名）。目录内的其它文件会保留，只有变空的目录会被回收。此操作无法撤销。`;
  showWallpaperDialog("wallpaper-library-purge-dialog");
  byId("wallpaper-library-purge-name").focus();
}

async function purgeWallpaperLibrary() {
  const libraryId = text(byId("wallpaper-library-purge-id").value);
  const confirmationName = text(byId("wallpaper-library-purge-name").value);
  const library = state.wallpaper.libraries.find((item) => String(item.id) === libraryId);
  if (!library) return;
  if (confirmationName !== library.name) {
    showToast("输入的图库名称不一致。", true);
    return;
  }
  const deleted = await deleteWallpaperLibrary(libraryId, { deleteFiles: true, confirmationName });
  if (deleted) closeWallpaperDialog("wallpaper-library-purge-dialog");
}

function findWallpaperImage(path) {
  return state.wallpaper.images.find((item) => item.relative_path === path) || null;
}

async function downloadWallpaperImage(image) {
  if (!image) return;
  try {
    const bridge = getBridge();
    if (bridge?.download) {
      await bridge.download("wallpaper_download", wallpaperImageParams(image), image.name || "wallpaper");
      return;
    }
    const link = document.createElement("a");
    link.href = wallpaperImageUrl("wallpaper_download", image);
    link.download = image.name || "wallpaper";
    link.hidden = true;
    document.body.append(link);
    link.click();
    link.remove();
  } catch (error) {
    showToast(error.message || "下载原图失败。", true);
  }
}

async function openWallpaperPreview(image) {
  if (!image?.preview_supported) {
    showToast("该格式不能直接预览，可下载原文件。", true);
    return;
  }
  state.wallpaper.previewImage = image;
  const requestId = ++state.wallpaper.previewRequestId;
  const preview = byId("wallpaper-preview-image");
  const status = byId("wallpaper-preview-status");
  byId("wallpaper-preview-title").textContent = image.name || "图片预览";
  preview.removeAttribute("src");
  preview.hidden = true;
  preview.onerror = () => {
    preview.hidden = true;
    status.hidden = false;
    status.textContent = "预览图片无法显示。";
  };
  status.hidden = false;
  status.textContent = "正在加载预览...";
  byId("wallpaper-preview-meta").textContent = `${image.relative_path} · ${imageMetaText(image)} · 修改于 ${formatDate(image.modified_at, true)}`;
  showWallpaperDialog("wallpaper-preview-dialog");
  try {
    const response = await apiGet("wallpaper_preview_data", wallpaperImageParams(image));
    const dataUrl = text(response?.data_url);
    if (!response?.success || !dataUrl.startsWith("data:image/jpeg;base64,")) {
      throw new Error(response?.message || "无法读取图片预览。");
    }
    if (requestId !== state.wallpaper.previewRequestId || state.wallpaper.previewImage !== image) return;
    preview.src = dataUrl;
    preview.hidden = false;
    status.hidden = true;
  } catch (error) {
    if (requestId !== state.wallpaper.previewRequestId) return;
    status.hidden = false;
    status.textContent = error.message || "无法加载图片预览。";
  }
}

function openWallpaperRename(image) {
  if (!image) return;
  byId("wallpaper-rename-path").value = image.relative_path;
  byId("wallpaper-rename-name").value = image.name || "";
  closeWallpaperDialog("wallpaper-preview-dialog");
  showWallpaperDialog("wallpaper-rename-dialog");
  byId("wallpaper-rename-name").focus();
  byId("wallpaper-rename-name").select();
}

async function renameWallpaperImage() {
  const path = text(byId("wallpaper-rename-path").value);
  const newName = text(byId("wallpaper-rename-name").value);
  const library = selectedWallpaperLibrary();
  if (!library || !path || !newName) return;
  try {
    const response = await apiPost("wallpaper_rename_image", {
      library_id: String(library.id),
      path,
      new_name: newName,
    });
    if (!response?.success) throw new Error(response?.message || "重命名图片失败。");
    closeWallpaperDialog("wallpaper-rename-dialog");
    state.wallpaper.previewImage = null;
    clearWallpaperThumbnailCache();
    await loadWallpaperLibraries({ loadImages: false, silent: true });
    await loadWallpaperImages();
    showToast(response.message || "图片已重命名。");
  } catch (error) {
    showToast(error.message || "重命名图片失败。", true);
  }
}

async function deleteWallpaperImage(image) {
  const library = selectedWallpaperLibrary();
  if (!library || !image) return;
  const confirmed = await confirmAction(`删除图片“${image.name}”？图片文件会从磁盘中删除，此操作无法撤销。`, {
    title: "删除图片",
    confirmLabel: "删除图片",
  });
  if (!confirmed) return;
  try {
    const response = await apiPost("wallpaper_delete_image", {
      library_id: String(library.id),
      path: image.relative_path,
      confirm: true,
    });
    if (!response?.success) throw new Error(response?.message || "删除图片失败。");
    closeWallpaperDialog("wallpaper-preview-dialog");
    closeWallpaperDialog("wallpaper-rename-dialog");
    state.wallpaper.previewImage = null;
    clearWallpaperThumbnailCache();
    await loadWallpaperLibraries({ loadImages: false, silent: true });
    await loadWallpaperImages();
    showToast(response.message || "图片已删除。");
  } catch (error) {
    showToast(error.message || "删除图片失败。", true);
  }
}

async function selectWallpaperLibrary(libraryId) {
  if (!libraryId || String(state.wallpaper.selectedLibraryId) === String(libraryId)) return;
  state.wallpaper.selectedLibraryId = String(libraryId);
  state.wallpaper.pagination = null;
  state.wallpaper.query = "";
  byId("wallpaper-image-search").value = "";
  renderWallpaperLibraries();
  renderWallpaperLibraryPicker();
  renderWallpaperBrowserState();
  closeWallpaperDialog("wallpaper-library-management-dialog");
  await loadWallpaperImages({ page: 1 });
}

async function handleWallpaperAction(button) {
  const action = button.dataset.wallpaperAction;
  const libraryId = button.dataset.wallpaperLibraryId;
  const image = findWallpaperImage(button.dataset.wallpaperPath || "");
  if (action === "select-library") {
    await selectWallpaperLibrary(libraryId);
    return;
  }
  if (action === "edit-library") {
    const library = state.wallpaper.libraries.find((item) => String(item.id) === String(libraryId));
    if (library) openWallpaperLibraryDialog(library);
    return;
  }
  if (action === "delete-library") {
    await deleteWallpaperLibrary(libraryId);
    return;
  }
  if (action === "delete-library-files") {
    openWallpaperLibraryPurge(libraryId);
    return;
  }
  if (action === "preview-image") {
    await openWallpaperPreview(image);
    return;
  }
  if (action === "download-image") {
    if (image) await downloadWallpaperImage(image);
    return;
  }
  if (action === "rename-image") {
    openWallpaperRename(image);
    return;
  }
  if (action === "delete-image") {
    await deleteWallpaperImage(image);
    return;
  }
  if (action === "wallpaper-page") {
    await loadWallpaperImages({ page: Number(button.dataset.wallpaperPage) || 1 });
  }
}

function renderAbout() {
  const target = byId("about-content");
  target.replaceChildren();
  const version = state.data?.version || "-";
  target.append(
    el("p", "", `当前版本：${version}。这是 AstrBot 内嵌插件页，使用已登录后台的鉴权和 API 通道，不会额外开放端口。`),
  );
  const list = el("ul", "about-list");
  [
    ["配置保存", "页面按现有 _conf_schema.json 自动生成。新增模块或字段后会自动进入这里，避免维护两份配置表。"],
    ["敏感信息", "Cookie、Token、API Key 与上传文件只显示“已配置/未配置”；页面不会读回明文。"],
    ["运行记录", "默认仅保留模块、动作、结果和时间。聊天正文、图片、语音和外部资料不会写入控制台记录。"],
    ["生效方式", "普通开关、阈值和文本设置大多立即生效。浏览器、定时任务、下载器或已初始化的缓存服务改动后，建议在 AstrBot 插件页重载一次。"],
  ].forEach(([name, content]) => {
    const item = el("li");
    item.append(el("strong", "", `${name}：`), document.createTextNode(content));
    list.append(item);
  });
  target.append(list);
}

function syncWallpaperToolsForViewport() {
  const tools = document.querySelector(".wallpaper-mobile-tools");
  if (!tools) return;
  if (window.matchMedia("(max-width: 620px)").matches) {
    if (!tools.dataset.mobileInitialized) {
      tools.open = false;
      tools.dataset.mobileInitialized = "true";
    }
    return;
  }
  tools.open = true;
  delete tools.dataset.mobileInitialized;
}

function switchTab(tab) {
  if (!tabTitles[tab]) return;
  state.activeTab = tab;
  document.body.classList.toggle("wallpaper-focused", tab === "wallpaper");
  syncWallpaperToolsForViewport();
  document.querySelectorAll(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.tab === tab));
  document.querySelectorAll(".tab-pane").forEach((item) => item.classList.toggle("active", item.id === `tab-${tab}`));
  const [kicker, title, subtitle] = tabTitles[tab];
  byId("page-kicker").textContent = kicker;
  byId("page-title").textContent = title;
  byId("page-subtitle").textContent = subtitle;
  if (tab === "activity") void loadActivities();
  if (tab === "storage") void loadStorage();
  if (tab === "wallpaper") void loadWallpaperLibraries();
}

async function loadState() {
  setHeaderState("正在读取插件配置与状态...");
  try {
    const response = await apiGet("state");
    if (!response?.success) throw new Error(response?.message || "控制台状态读取失败。");
    state.data = response;
    state.wallpaper.serverConfigRows = cloneValue(response.config?.wallpaper?.libraries || []);
    state.storageLoadedAt = Date.now();
    applyTheme(response.theme, false);
    renderOverview();
    renderConfig();
    renderStorage(response.storage, response.runtime);
    renderAbout();
    if (state.activeTab === "wallpaper") void loadWallpaperLibraries({ silent: true });
    setDirty(false);
    setHeaderState(`配置已加载 · ${response.config_updated_at ? `最近保存 ${formatDate(response.config_updated_at)}` : "未提供保存时间"}`);
  } catch (error) {
    console.error(error);
    setHeaderState("控制台加载失败。", true);
    showToast(error.message || "控制台加载失败。", true);
  }
}

document.addEventListener("click", (event) => {
  const confirmationAction = event.target.closest("[data-confirm-action]");
  if (confirmationAction) {
    settleConfirmation(confirmationAction.dataset.confirmAction === "confirm");
    return;
  }
  const closeDialog = event.target.closest("[data-close-dialog]");
  if (closeDialog) {
    closeWallpaperDialog(closeDialog.dataset.closeDialog);
    return;
  }
  const wallpaperAction = event.target.closest("[data-wallpaper-action]");
  if (wallpaperAction) {
    void handleWallpaperAction(wallpaperAction);
    return;
  }
  const nav = event.target.closest("[data-tab]");
  if (nav) {
    switchTab(nav.dataset.tab);
    return;
  }
  const openTab = event.target.closest("[data-open-tab]");
  if (openTab) {
    switchTab(openTab.dataset.openTab);
    return;
  }
  const theme = event.target.closest(".theme-option");
  if (theme) {
    applyTheme(theme.dataset.theme, true);
    return;
  }
  if (event.target.closest("#save-button")) {
    void saveConfig();
    return;
  }
  if (event.target.closest("#reload-button")) {
    void (async () => {
      if (
        state.dirty
        && !await confirmAction("存在尚未保存的更改，确认重新读取并放弃这些更改吗？", {
          title: "放弃未保存更改",
          confirmLabel: "放弃并重新读取",
        })
      ) return;
      setDirty(false);
      await loadState();
    })();
    return;
  }
  if (event.target.closest("#expand-all-config")) {
    setConfigModulesOpen(true);
    return;
  }
  if (event.target.closest("#collapse-all-config")) {
    setConfigModulesOpen(false);
    return;
  }
  if (event.target.closest("#activity-refresh")) {
    void loadActivities();
    return;
  }
  if (event.target.closest("#clear-activities")) {
    void clearActivities();
    return;
  }
  if (event.target.closest("#storage-refresh")) {
    void loadStorage(true);
    return;
  }
  if (event.target.closest("#wallpaper-library-create")) {
    closeWallpaperDialog("wallpaper-library-management-dialog");
    openWallpaperLibraryDialog();
    return;
  }
  if (event.target.closest("#wallpaper-library-edit")) {
    const library = selectedWallpaperLibrary();
    if (library) openWallpaperLibraryDialog(library);
    return;
  }
  if (event.target.closest("#wallpaper-library-remove-config")) {
    const library = selectedWallpaperLibrary();
    if (library) void deleteWallpaperLibrary(library.id);
    return;
  }
  if (event.target.closest("#wallpaper-library-delete-files")) {
    const library = selectedWallpaperLibrary();
    if (library) openWallpaperLibraryPurge(library.id);
    return;
  }
  if (event.target.closest("#wallpaper-library-manage")) {
    showWallpaperDialog("wallpaper-library-management-dialog");
    return;
  }
  if (event.target.closest("#wallpaper-upload-button")) {
    void uploadWallpaperImages();
    return;
  }
  if (event.target.closest("#wallpaper-images-refresh")) {
    void loadWallpaperLibraries({ page: state.wallpaper.pagination?.page || 1 });
    return;
  }
  if (event.target.closest("#wallpaper-preview-download")) {
    if (state.wallpaper.previewImage) void downloadWallpaperImage(state.wallpaper.previewImage);
    return;
  }
  if (event.target.closest("#wallpaper-preview-rename")) {
    openWallpaperRename(state.wallpaper.previewImage);
    return;
  }
  if (event.target.closest("#wallpaper-preview-delete")) {
    void deleteWallpaperImage(state.wallpaper.previewImage);
    return;
  }
});

document.addEventListener("input", (event) => {
  if (event.target.closest("#config-modules")) setDirty(true);
  if (event.target.id === "config-search") filterConfigModules();
  if (event.target.id === "wallpaper-image-search") {
    state.wallpaper.query = event.target.value;
    window.clearTimeout(state.wallpaper.searchTimer);
    state.wallpaper.searchTimer = window.setTimeout(() => void loadWallpaperImages({ page: 1 }), 260);
  }
});

document.addEventListener("change", (event) => {
  if (event.target.closest("#config-modules")) setDirty(true);
  if (event.target.id === "enabled-only") filterConfigModules();
  if (event.target.id === "activity-module-filter" || event.target.id === "activity-status-filter") void loadActivities();
  if (event.target.id === "wallpaper-upload-input") updateWallpaperUploadSelection();
  if (event.target.id === "wallpaper-image-sort") {
    state.wallpaper.sort = event.target.value;
    void loadWallpaperImages({ page: 1 });
  }
  if (event.target.id === "wallpaper-library-picker") {
    void selectWallpaperLibrary(event.target.value);
  }
});

document.addEventListener("submit", (event) => {
  if (event.target.id === "confirm-action-form") {
    event.preventDefault();
    settleConfirmation(true);
    return;
  }
  if (event.target.id === "wallpaper-library-form") {
    event.preventDefault();
    void saveWallpaperLibrary();
  }
  if (event.target.id === "wallpaper-rename-form") {
    event.preventDefault();
    void renameWallpaperImage();
  }
  if (event.target.id === "wallpaper-library-purge-form") {
    event.preventDefault();
    void purgeWallpaperLibrary();
  }
});

byId("confirm-action-dialog")?.addEventListener("cancel", (event) => {
  event.preventDefault();
  settleConfirmation(false);
});

byId("confirm-action-dialog")?.addEventListener("close", () => {
  if (pendingConfirmation) settleConfirmation(false);
});

applyTheme("dark", false);
syncWallpaperToolsForViewport();
window.addEventListener("resize", syncWallpaperToolsForViewport);

try {
  await waitForBridge();
  await loadState();
} catch (error) {
  console.error(error);
  setHeaderState("控制台初始化失败。", true);
  showToast(error.message || "控制台初始化失败。", true);
}
