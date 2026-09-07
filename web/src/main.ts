import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { open as openDialog, save as saveDialog } from "@tauri-apps/plugin-dialog";
import { openUrl } from "@tauri-apps/plugin-opener";
import "./styles.css";

type Page = "overview" | "checks" | "activity" | "settings" | "about";
type CheckState = "healthy" | "failed" | "unconnectable";

interface Rule {
  id: string; name: string; resource_id: string; portal_url: string; tenant_id: string;
  expected_values: string[]; enabled: boolean; kind: string; query: string; scope: string;
  workspace_id: string; lookback_minutes: number; metric_name: string; metric_namespace: string;
  metric_aggregation: string; metric_reducer: string; metric_operator: string;
  metric_threshold: number; metric_filter: string; property_path: string; property_operator: string;
}

interface Config {
  onboarding_completed: boolean; azure_subscription_id: string; azure_subscription_name: string;
  azure_tenant_id: string; interval_minutes: number; timeout_seconds: number; retry_count: number;
  update_mode: string; start_with_windows: boolean; start_minimized: boolean; theme_mode: "dark" | "light";
  checks: Rule[];
}

interface Result { check_id: string; name: string; state: CheckState; summary: string; observed_value: string; checked_at: string; portal_url: string; }
interface Snapshot { version: string; connected: boolean; connectionExpiresUtc: string; config: Config; results: Result[]; beaconState: string; }
interface Subscription { id: string; name: string; tenantId: string; }

const sources = [
  ["azure_resource_provisioning", "Provisioning state", "Confirm that a resource finished provisioning successfully.", "icon-checks"],
  ["azure_vm_power_state", "VM power state", "Confirm that a virtual machine is running, stopped, or deallocated.", "icon-server"],
  ["azure_resource_property", "Resource property", "Read and compare any supported property in an ARM resource document.", "icon-braces"],
  ["azure_resource_graph", "Resource Graph", "Write KQL across every accessible Azure subscription.", "icon-hierarchy"],
  ["azure_log_analytics", "Logs / Application Insights", "Write KQL where every returned row becomes a finding.", "icon-file"],
  ["azure_monitor_metric", "Azure Monitor metric", "Compare any exposed Azure Monitor metric with a threshold.", "icon-activity"],
] as const;

const app = document.querySelector<HTMLDivElement>("#app")!;
let snapshot: Snapshot;
let page: Page = "overview";
let editing: Rule | null = null;
let choosingSource = false;
let subscriptions: Subscription[] = [];
let busy = false;

function e(value: unknown): string {
  return String(value ?? "").replace(/[&<>'"]/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[char]!);
}

function newRule(kind: string): Rule {
  return {
    id: crypto.randomUUID(), name: "", resource_id: "", portal_url: "", tenant_id: snapshot?.config.azure_tenant_id ?? "",
    expected_values: kind === "azure_vm_power_state" ? ["PowerState/running"] : ["azure_resource_graph", "azure_log_analytics", "azure_monitor_metric"].includes(kind) ? [] : ["Succeeded"], enabled: true,
    kind, query: "", scope: kind === "azure_resource_graph" ? "all_accessible" : kind === "azure_log_analytics" ? "workspace" : "resource",
    workspace_id: "", lookback_minutes: 5, metric_name: "", metric_namespace: "", metric_aggregation: "Average",
    metric_reducer: "latest", metric_operator: "gt", metric_threshold: 0, metric_filter: "", property_path: "properties.provisioningState", property_operator: "equals_any",
  };
}

async function load(): Promise<void> {
  try {
    snapshot = await invoke<Snapshot>("snapshot");
    applyTheme();
    render();
  } catch (error) {
    if (!("__TAURI_INTERNALS__" in window) && (import.meta.env.DEV || location.hostname === "127.0.0.1")) {
      snapshot = mockSnapshot();
      applyTheme();
      render();
      return;
    }
    app.innerHTML = `<div class="page"><div class="panel"><h1>Azure Health Beacon could not start</h1><p>${e(error)}</p></div></div>`;
  }
}

function mockSnapshot(): Snapshot {
  const setupPreview = new URLSearchParams(location.search).has("setup");
  const demo = newRule("azure_resource_graph");
  demo.name = "Expired Key Vault certificates";
  demo.query = "Resources\n| where type =~ 'microsoft.keyvault/vaults'\n| where properties.expiryDate < now()";
  return {
    version: "0.8.0-preview", connected: !setupPreview, connectionExpiresUtc: setupPreview ? "" : new Date(Date.now() + 12 * 86400000).toISOString(), beaconState: setupPreview ? "unconnectable" : "healthy",
    config: { onboarding_completed: !setupPreview, azure_subscription_id: setupPreview ? "" : "00000000-0000-0000-0000-000000000000", azure_subscription_name: setupPreview ? "" : "Preview subscription", azure_tenant_id: setupPreview ? "" : "11111111-1111-1111-1111-111111111111", interval_minutes: 5, timeout_seconds: 30, retry_count: 2, update_mode: "manual", start_with_windows: false, start_minimized: false, theme_mode: "dark", checks: setupPreview ? [] : [demo] },
    results: setupPreview ? [] : [{ check_id: demo.id, name: demo.name, state: "healthy", summary: "Resource Graph returned no findings.", observed_value: "0", checked_at: new Date().toISOString(), portal_url: "" }],
  };
}

function applyTheme(): void { document.documentElement.dataset.theme = snapshot?.config.theme_mode ?? "dark"; }

function layout(title: string, subtitle: string, body: string): string {
  const connected = snapshot.connected;
  return `<div class="shell"><aside class="sidebar">
    <div class="brand"><img src="/AzureHealthBeacon-Brand-Square.png" alt=""><div><strong>Azure Health</strong><span>Beacon</span></div></div>
    <nav class="nav">${nav("overview", "icon-activity", "Overview")}${nav("checks", "icon-checks", "Checks")}${nav("activity", "icon-file", "Activity")}${nav("settings", "icon-settings", "Settings")}${nav("about", "icon-info", "About")}</nav>
    <div class="connection-card"><strong>${connected ? "Connected" : "Not connected"}</strong><span>${connected ? e(snapshot.config.azure_subscription_name) : "Azure connection required for checks"}</span></div>
  </aside><main class="content"><header class="topbar"><div><h1>${e(title)}</h1><p>${e(subtitle)}</p></div><button class="button" id="theme-toggle">${snapshot.config.theme_mode === "dark" ? "☀ Light" : "◐ Dark"}</button></header><section class="page">${body}</section></main></div>`;
}

function nav(target: Page, icon: string, label: string): string { return `<button data-page="${target}" class="${page === target ? "active" : ""}"><span class="icon ${icon}"></span>${label}</button>`; }

function render(): void {
  if (page === "overview") renderOverview();
  if (page === "checks") renderChecks();
  if (page === "activity") renderActivity();
  if (page === "settings") renderSettings();
  if (page === "about") renderAbout();
  bindCommon();
}

function renderOverview(): void {
  if (!snapshot.connected) {
    app.innerHTML = layout("Welcome to Azure Health Beacon", "Secure setup before checks", `<div class="setup panel"><h2>Connect Azure securely</h2><p class="muted">The Beacon uses Microsoft’s system-browser sign-in. Passwords, passkeys and MFA responses are handled by Microsoft and never enter this app.</p>
      <div class="setup-step"><div class="step-number">1</div><div><h3>Sign in with Microsoft</h3><p class="muted">A random loopback address and PKCE protect the sign-in. The renewable authorization is stored only as Windows DPAPI CurrentUser ciphertext, then hard-deleted after 14 days.</p><button class="button primary" id="sign-in" ${busy ? "disabled" : ""}>${busy ? "Waiting for Microsoft…" : "Sign in with Microsoft"}</button></div></div>
      ${subscriptions.length ? subscriptionPicker() : ""}
      <div class="callout">Settings, About, version information and update recovery remain available without signing in.</div></div>`);
    document.querySelector("#sign-in")?.addEventListener("click", signIn);
    document.querySelector("#finish-setup")?.addEventListener("click", finishSetup);
    return;
  }
  const failed = snapshot.results.filter(item => item.state === "failed").length;
  const healthy = snapshot.results.filter(item => item.state === "healthy").length;
  const unknown = snapshot.results.filter(item => item.state === "unconnectable").length;
  const results = snapshot.results.length ? snapshot.results.map(resultRow).join("") : `<div class="empty">No checks have run yet. Add a rule or select Check now.</div>`;
  app.innerHTML = layout("Overview", "Azure signals at a glance", `<div class="toolbar"><div><span class="status-dot ${e(snapshot.beaconState)}"></span><strong>${stateLabel(snapshot.beaconState)}</strong></div><button id="check-now" class="button primary" ${busy ? "disabled" : ""}>${busy ? "Checking…" : "Check now"}</button></div>
    <div class="grid three"><div class="metric"><span class="muted">Healthy</span><strong>${healthy}</strong></div><div class="metric"><span class="muted">Needs attention</span><strong>${failed}</strong></div><div class="metric"><span class="muted">Unknown</span><strong>${unknown}</strong></div></div>
    <div class="panel"><h2>Current results</h2>${results}</div>`);
  document.querySelector("#check-now")?.addEventListener("click", checkNow);
  bindPortalLinks();
}

function resultRow(result: Result): string {
  return `<div class="result"><span class="status-dot ${e(result.state)}"></span><div><h3>${e(result.name)}</h3><p>${e(new Date(result.checked_at).toLocaleString())}</p></div><div><strong>${e(result.state === "failed" ? "NEEDS ATTENTION" : result.state === "healthy" ? "HEALTHY" : "UNKNOWN")}</strong><p>${e(result.summary)}</p></div>${result.portal_url ? `<button class="button portal-link" data-url="${e(result.portal_url)}">Open in Azure</button>` : ""}</div>`;
}

function renderChecks(): void {
  if (choosingSource) {
    const cards = sources.map(source => `<button class="source-card" data-source="${source[0]}"><span class="icon source-icon ${source[3]}"></span><strong>${source[1]}</strong><span>${source[2]}</span></button>`).join("");
    app.innerHTML = layout("New check", "Choose the signal first — nothing is preselected", `<div class="toolbar"><button class="button" id="back-checks">← Back to checks</button></div><div class="source-grid">${cards}</div>`);
    document.querySelector("#back-checks")?.addEventListener("click", () => { choosingSource = false; render(); });
    document.querySelectorAll<HTMLElement>("[data-source]").forEach(card => card.addEventListener("click", () => { editing = newRule(card.dataset.source!); choosingSource = false; render(); }));
    return;
  }
  if (editing) { renderEditor(); return; }
  const rows = snapshot.config.checks.map(rule => `<button class="rule-row" data-rule="${e(rule.id)}"><strong>${e(rule.name)}</strong><span>${e(sourceName(rule.kind))} · ${rule.enabled ? "Enabled" : "Disabled"}</span></button>`).join("");
  app.innerHTML = layout("Checks", "Create, edit, test and share Azure signals", `<div class="toolbar"><p class="muted">${snapshot.config.checks.length} configured rule(s)</p><div class="actions"><button class="button" id="import-rules">Import rule pack</button><button class="button" id="export-rules" ${snapshot.config.checks.length ? "" : "disabled"}>Export all rules</button><button class="button primary" id="new-rule">＋ New check</button></div></div><div class="panel">${rows || `<div class="empty">No rules yet. Start by choosing a signal source.</div>`}</div>`);
  document.querySelector("#new-rule")?.addEventListener("click", () => { choosingSource = true; render(); });
  document.querySelector("#import-rules")?.addEventListener("click", importRules);
  document.querySelector("#export-rules")?.addEventListener("click", exportRules);
  document.querySelectorAll<HTMLElement>("[data-rule]").forEach(row => row.addEventListener("click", () => { editing = structuredClone(snapshot.config.checks.find(rule => rule.id === row.dataset.rule)!); render(); }));
}

function renderEditor(): void {
  const rule = editing!;
  app.innerHTML = layout(rule.name ? `Edit ${rule.name}` : `New ${sourceName(rule.kind)} check`, "Test live before applying changes", `<div class="toolbar"><button class="button" id="cancel-edit">← Back</button><div class="actions"><button class="button danger" id="delete-rule" ${snapshot.config.checks.some(item => item.id === rule.id) ? "" : "disabled"}>Delete</button><button class="button" id="test-rule" ${busy || !snapshot.connected ? "disabled" : ""}>Test without saving</button><button class="button primary" id="save-rule" disabled>Apply tested rule</button></div></div><div class="panel"><div class="form-grid">${ruleFields(rule)}</div><div id="test-output" style="margin-top:14px"></div></div>`);
  hydrateEditor(rule);
  document.querySelector("#cancel-edit")?.addEventListener("click", () => { editing = null; render(); });
  document.querySelector("#test-rule")?.addEventListener("click", testRule);
  document.querySelector("#save-rule")?.addEventListener("click", saveRule);
  document.querySelector("#delete-rule")?.addEventListener("click", deleteRule);
  document.querySelectorAll<HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement>("[data-field]").forEach(field => field.addEventListener("input", () => { readEditor(); (document.querySelector("#save-rule") as HTMLButtonElement).disabled = true; }));
}

function ruleFields(rule: Rule): string {
  let specific = "";
  if (["azure_resource_provisioning", "azure_vm_power_state", "azure_resource_property", "azure_monitor_metric"].includes(rule.kind)) specific += field("resource_id", "Azure resource ID", "Paste the full /subscriptions/... resource ID");
  if (rule.kind === "azure_resource_provisioning") specific += field("expected_values", "Healthy provisioning states", "Comma-separated, normally Succeeded");
  if (rule.kind === "azure_vm_power_state") specific += field("expected_values", "Healthy VM power states", "Example: PowerState/running");
  if (rule.kind === "azure_resource_property") specific += field("property_path", "Property path", "Example: properties.provisioningState") + selectField("property_operator", "Comparison", [["equals_any", "Equals any healthy value"], ["not_equals_any", "Does not equal any"], ["contains", "Contains"], ["not_contains", "Does not contain"], ["greater_than", "Greater than"], ["less_than", "Less than"], ["exists", "Exists"], ["missing", "Missing"]]) + field("expected_values", "Comparison values", "Comma-separated");
  if (rule.kind === "azure_resource_graph") specific += textareaField("query", "Resource Graph KQL", "Any returned row is a confirmed finding. Zero rows means healthy.");
  if (rule.kind === "azure_log_analytics") specific += `<div class="form-grid compact">${field("workspace_id", "Workspace customer ID", "GUID")}${numberField("lookback_minutes", "Lookback in minutes")}</div>` + textareaField("query", "Azure Monitor KQL", "Any returned row is a confirmed finding. Zero rows means healthy.");
  if (rule.kind === "azure_monitor_metric") specific += `<div class="form-grid compact">${field("metric_name", "Metric name", "Azure metric")}${field("metric_namespace", "Metric namespace", "Optional")}${selectField("metric_aggregation", "Aggregation", [["Average", "Average"], ["Count", "Count"], ["Maximum", "Maximum"], ["Minimum", "Minimum"], ["Total", "Total"]])}${selectField("metric_reducer", "Reduce samples", [["latest", "Latest"], ["maximum", "Maximum"], ["minimum", "Minimum"], ["average", "Average"], ["total", "Total"]])}${selectField("metric_operator", "Alert when", [["gt", "> Greater than"], ["gte", "≥ At least"], ["lt", "< Less than"], ["lte", "≤ At most"], ["eq", "= Equal"], ["ne", "≠ Not equal"]])}${numberField("metric_threshold", "Threshold")}${numberField("lookback_minutes", "Lookback in minutes")}${field("metric_filter", "Dimension filter", "Optional Azure Monitor filter")}</div>`;
  return `<div class="form-grid compact">${field("name", "Rule name", "Editable friendly name")}${field("tenant_id", "Tenant ID", "Optional safety binding")}${field("portal_url", "Azure Portal URL", "Optional direct link")}</div><div class="toggle-row"><input id="f-enabled" data-field="enabled" type="checkbox"><label for="f-enabled"><strong>Enabled</strong><br><small class="muted">Included in scheduled checks after it has been tested and applied.</small></label></div>${specific}`;
}

function field(key: keyof Rule, label: string, hint: string): string { return `<div class="field"><label for="f-${key}">${label}</label><input id="f-${key}" data-field="${key}"><small>${hint}</small></div>`; }
function numberField(key: keyof Rule, label: string): string { return `<div class="field"><label for="f-${key}">${label}</label><input id="f-${key}" data-field="${key}" type="number"></div>`; }
function textareaField(key: keyof Rule, label: string, hint: string): string { return `<div class="field"><label for="f-${key}">${label}</label><textarea id="f-${key}" data-field="${key}"></textarea><small>${hint}</small></div>`; }
function selectField(key: keyof Rule, label: string, options: [string, string][]): string { return `<div class="field"><label for="f-${key}">${label}</label><select id="f-${key}" data-field="${key}">${options.map(option => `<option value="${e(option[0])}">${e(option[1])}</option>`).join("")}</select></div>`; }

function hydrateEditor(rule: Rule): void {
  document.querySelectorAll<HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement>("[data-field]").forEach(input => {
    const key = input.dataset.field as keyof Rule;
    const value = rule[key];
    if (input.type === "checkbox") (input as HTMLInputElement).checked = Boolean(value);
    else input.value = Array.isArray(value) ? value.join(", ") : String(value ?? "");
  });
}

function readEditor(): Rule {
  const rule = editing!;
  document.querySelectorAll<HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement>("[data-field]").forEach(input => {
    const key = input.dataset.field as keyof Rule;
    let value: unknown = input.type === "checkbox" ? (input as HTMLInputElement).checked : input.value;
    if (key === "expected_values") value = input.value.split(",").map(item => item.trim()).filter(Boolean);
    if (["lookback_minutes", "metric_threshold"].includes(key)) value = Number(input.value);
    (rule as unknown as Record<string, unknown>)[key] = value;
  });
  return rule;
}

function renderActivity(): void {
  const content = snapshot.results.length ? snapshot.results.slice().reverse().map(resultRow).join("") : `<div class="empty">Activity appears after checks run.</div>`;
  app.innerHTML = layout("Activity", "Recent local check results", `<div class="panel">${content}</div>`);
  bindPortalLinks();
}

function renderSettings(): void {
  const c = snapshot.config;
  app.innerHTML = layout("Settings", "Windows, monitoring, credentials and updates", `<div class="settings-grid">
    <div class="panel"><h2>Windows</h2><div class="form-grid"><label class="toggle-row"><input id="start-windows" type="checkbox" ${c.start_with_windows ? "checked" : ""}><span><strong>Start with Windows</strong><br><small class="muted">Explicitly opt in.</small></span></label><label class="toggle-row"><input id="start-minimized" type="checkbox" ${c.start_minimized ? "checked" : ""}><span><strong>Start minimized in notification area</strong><br><small class="muted">Explicitly opt in.</small></span></label></div></div>
    <div class="panel"><h2>Monitoring</h2><div class="form-grid compact">${settingNumber("interval", "Check interval (minutes)", c.interval_minutes)}${settingNumber("timeout", "Attempt timeout (seconds)", c.timeout_seconds)}${settingNumber("retry", "Retry count", c.retry_count)}</div></div>
    <div class="panel"><h2>Azure connection</h2><p class="muted">${snapshot.connected ? `Connected to ${e(c.azure_subscription_name)}. Authorization expires ${e(new Date(snapshot.connectionExpiresUtc).toLocaleString())}.` : "No Azure authorization is stored."}</p><button class="button danger" id="delete-connection" ${snapshot.connected ? "" : "disabled"}>Delete Azure connection</button></div>
    <div class="panel"><h2>Updates</h2><div class="form-grid"><label><input name="updates" type="radio" value="manual" ${c.update_mode === "manual" ? "checked" : ""}> Manual only</label><label><input name="updates" type="radio" value="notify" ${c.update_mode === "notify" ? "checked" : ""}> Notify me</label><label><input name="updates" type="radio" value="automatic" ${c.update_mode === "automatic" ? "checked" : ""}> Install verified updates automatically</label><small class="muted">Notification and automatic installation are explicitly opt-in. The Rust preview will only install Tauri-signed updater artifacts.</small><button class="button" id="check-updates">Check for updates</button><div id="update-status"></div></div></div>
  </div><button class="button primary" style="width:100%; margin-top:18px" id="save-settings">Save settings</button>`);
  document.querySelector("#save-settings")?.addEventListener("click", saveSettings);
  document.querySelector("#delete-connection")?.addEventListener("click", deleteConnection);
  document.querySelector("#check-updates")?.addEventListener("click", () => { document.querySelector("#update-status")!.innerHTML = `<div class="callout">v0.8 preview updater metadata is not published yet. Settings and recovery remain accessible here without Azure sign-in.</div>`; });
}

function settingNumber(id: string, label: string, value: number): string { return `<div class="field"><label for="${id}">${label}</label><input id="${id}" type="number" value="${value}"></div>`; }

function renderAbout(): void {
  app.innerHTML = layout("About", "Version, security boundary and recovery", `<div class="grid two"><div class="panel"><img src="/AzureHealthBeacon-Brand-Square.png" width="76" height="76" alt="Azure Health Beacon"><h2>Azure Health Beacon</h2><p>Rust/Tauri preview v${e(snapshot.version)}</p><p class="muted">Windows 11 local-first Azure signal monitor.</p><button class="button primary" id="github">Open GitHub repository</button></div><div class="panel"><h2>Credential security</h2><p class="muted">Microsoft handles passwords, passkeys and MFA in the system browser. The app stores only a renewable OAuth authorization as Windows DPAPI CurrentUser ciphertext and hard-deletes it after 14 days.</p><p class="muted">The web interface never receives OAuth tokens. Rules cannot contain credentials or execute code.</p></div></div>`);
  document.querySelector("#github")?.addEventListener("click", () => openUrl("https://github.com/Anders0lesen/azure-extensible-systray-monitor"));
}

function subscriptionPicker(): string { return `<div class="setup-step"><div class="step-number">2</div><div><h3>Choose the initial subscription</h3><p class="muted">This confirms the authorization works. Resource Graph rules can still query every accessible subscription.</p><select id="subscription">${subscriptions.map(item => `<option value="${e(item.id)}">${e(item.name)} — ${e(item.id)}</option>`).join("")}</select><button class="button primary" style="margin-top:10px" id="finish-setup">Test credentials and finish setup</button></div></div>`; }

async function signIn(): Promise<void> { busy = true; render(); try { subscriptions = await invoke<Subscription[]>("sign_in"); toast("Microsoft sign-in completed. Choose a subscription to finish."); } catch (error) { toast(String(error), true); } finally { busy = false; render(); } }
async function finishSetup(): Promise<void> { const id = (document.querySelector("#subscription") as HTMLSelectElement).value; try { snapshot = await invoke<Snapshot>("complete_setup", { subscriptionId: id }); subscriptions = []; toast("Azure connection tested and stored securely."); render(); } catch (error) { toast(String(error), true); } }
async function checkNow(): Promise<void> { busy = true; render(); try { snapshot = await invoke<Snapshot>("check_now"); } catch (error) { toast(String(error), true); } finally { busy = false; render(); } }
async function testRule(): Promise<void> { const rule = readEditor(); busy = true; try { const result = await invoke<Result>("test_rule", { rule }); const output = document.querySelector("#test-output")!; output.innerHTML = `<div class="callout ${result.state === "unconnectable" ? "error" : "success"}"><strong>${e(result.state.toUpperCase())}</strong><br>${e(result.summary)}</div>`; (document.querySelector("#save-rule") as HTMLButtonElement).disabled = result.state === "unconnectable"; } catch (error) { document.querySelector("#test-output")!.innerHTML = `<div class="callout error">${e(error)}</div>`; } finally { busy = false; } }
async function saveRule(): Promise<void> { try { snapshot = await invoke<Snapshot>("save_rule", { rule: readEditor() }); editing = null; toast("Tested rule applied."); render(); } catch (error) { toast(String(error), true); } }
async function importRules(): Promise<void> { try { const selected = await openDialog({ multiple: false, filters: [{ name: "Azure Health Beacon rule pack", extensions: ["json"] }] }); if (!selected) return; snapshot = await invoke<Snapshot>("import_rules", { path: selected }); toast("Rules imported disabled. Review and test each before enabling."); render(); } catch (error) { toast(String(error), true); } }
async function exportRules(): Promise<void> { try { const selected = await saveDialog({ defaultPath: "azure-health-beacon-rules.json", filters: [{ name: "Azure Health Beacon rule pack", extensions: ["json"] }] }); if (!selected) return; await invoke("export_rules", { path: selected }); toast("Credential-free rule pack exported."); } catch (error) { toast(String(error), true); } }
async function deleteRule(): Promise<void> { if (!editing || !confirm(`Delete ${editing.name}?`)) return; try { snapshot = await invoke<Snapshot>("delete_rule", { ruleId: editing.id }); editing = null; toast("Rule deleted."); render(); } catch (error) { toast(String(error), true); } }
async function deleteConnection(): Promise<void> { if (!confirm("Delete the complete encrypted Azure connection? Rules will be retained.")) return; try { snapshot = await invoke<Snapshot>("delete_connection"); page = "overview"; toast("Azure connection deleted."); render(); } catch (error) { toast(String(error), true); } }
async function saveSettings(): Promise<void> { try { const selected = document.querySelector<HTMLInputElement>('input[name="updates"]:checked')!; snapshot = await invoke<Snapshot>("save_settings", { patch: { intervalMinutes: Number((document.querySelector("#interval") as HTMLInputElement).value), timeoutSeconds: Number((document.querySelector("#timeout") as HTMLInputElement).value), retryCount: Number((document.querySelector("#retry") as HTMLInputElement).value), updateMode: selected.value, startWithWindows: (document.querySelector("#start-windows") as HTMLInputElement).checked, startMinimized: (document.querySelector("#start-minimized") as HTMLInputElement).checked, themeMode: snapshot.config.theme_mode } }); toast("Settings saved."); render(); } catch (error) { toast(String(error), true); } }

function bindCommon(): void {
  document.querySelectorAll<HTMLElement>("[data-page]").forEach(item => item.addEventListener("click", () => { page = item.dataset.page as Page; editing = null; choosingSource = false; render(); }));
  document.querySelector("#theme-toggle")?.addEventListener("click", async () => { const themeMode = snapshot.config.theme_mode === "dark" ? "light" : "dark"; snapshot.config.theme_mode = themeMode; applyTheme(); render(); try { snapshot = await invoke<Snapshot>("set_theme", { themeMode }); } catch (error) { toast(String(error), true); } });
}
function bindPortalLinks(): void { document.querySelectorAll<HTMLElement>(".portal-link").forEach(item => item.addEventListener("click", () => openUrl(item.dataset.url!))); }
function sourceName(kind: string): string { return sources.find(source => source[0] === kind)?.[1] ?? kind; }
function stateLabel(state: string): string { return state === "failed" ? "Azure needs attention" : state === "healthy" ? "All checked signals are healthy" : state === "checking" ? "Checking Azure" : "Azure status is not confirmed"; }
function toast(message: string, error = false): void { document.querySelector(".toast")?.remove(); const node = document.createElement("div"); node.className = `toast${error ? " error" : ""}`; node.textContent = message; document.body.append(node); setTimeout(() => node.remove(), 5500); }

void load();
if ("__TAURI_INTERNALS__" in window) {
  void listen("tray-check-now", () => void checkNow());
  void listen("snapshot-updated", () => void load());
}
