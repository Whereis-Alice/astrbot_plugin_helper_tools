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
};

const tabTitles = {
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
      if (!window.confirm("确认清除这个已配置文件吗？")) return;
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
  } catch (error) {
    showToast(error.message || "请先修正配置格式。", true);
    return false;
  }
  const button = byId("save-button");
  button.disabled = true;
  setHeaderState("正在保存配置...");
  try {
    const response = await apiPost("save_config", { config });
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
  if (!window.confirm("确认清空本插件控制台保存的全部运行记录吗？此操作不能恢复。")) return;
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

function switchTab(tab) {
  if (!tabTitles[tab]) return;
  state.activeTab = tab;
  document.querySelectorAll(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.tab === tab));
  document.querySelectorAll(".tab-pane").forEach((item) => item.classList.toggle("active", item.id === `tab-${tab}`));
  const [kicker, title, subtitle] = tabTitles[tab];
  byId("page-kicker").textContent = kicker;
  byId("page-title").textContent = title;
  byId("page-subtitle").textContent = subtitle;
  if (tab === "activity") void loadActivities();
  if (tab === "storage") void loadStorage();
}

async function loadState() {
  setHeaderState("正在读取插件配置与状态...");
  try {
    const response = await apiGet("state");
    if (!response?.success) throw new Error(response?.message || "控制台状态读取失败。");
    state.data = response;
    state.storageLoadedAt = Date.now();
    applyTheme(response.theme, false);
    renderOverview();
    renderConfig();
    renderStorage(response.storage, response.runtime);
    renderAbout();
    setDirty(false);
    setHeaderState(`配置已加载 · ${response.config_updated_at ? `最近保存 ${formatDate(response.config_updated_at)}` : "未提供保存时间"}`);
  } catch (error) {
    console.error(error);
    setHeaderState("控制台加载失败。", true);
    showToast(error.message || "控制台加载失败。", true);
  }
}

document.addEventListener("click", (event) => {
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
    if (state.dirty && !window.confirm("存在尚未保存的更改，确认重新读取并放弃这些更改吗？")) return;
    setDirty(false);
    void loadState();
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
  }
});

document.addEventListener("input", (event) => {
  if (event.target.closest("#config-modules")) setDirty(true);
  if (event.target.id === "config-search") filterConfigModules();
});

document.addEventListener("change", (event) => {
  if (event.target.closest("#config-modules")) setDirty(true);
  if (event.target.id === "enabled-only") filterConfigModules();
  if (event.target.id === "activity-module-filter" || event.target.id === "activity-status-filter") void loadActivities();
});

applyTheme("dark", false);

try {
  await waitForBridge();
  await loadState();
} catch (error) {
  console.error(error);
  setHeaderState("控制台初始化失败。", true);
  showToast(error.message || "控制台初始化失败。", true);
}
