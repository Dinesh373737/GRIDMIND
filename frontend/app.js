"use strict";

const $ = id => document.getElementById(id);
const activeStatuses = new Set(["queued", "starting", "running", "stopping", "pending"]);
const scenarioLabels = {
  historical: "Historical outage replay",
  scheduled: "Scheduled outage",
  stress: "Feeder stress",
  reference: "Reference run"
};
const state = {
  packs: [], localities: [], facilityTypes: [], participants: [], runs: [],
  localityId: "", runId: "", run: null, events: new Map(), orders: [], trades: [],
  preview: null, report: null, benchmark: null, comparison: null, referenceId: "", health: null, view: "simulation",
  epoch: 0, ready: false
};
let submitEditor;
let editorGeneration = 0;
let toastTimer;
let refreshBusy = false;
let nextRunListCheck = 0;

const same = (a, b) => String(a ?? "") === String(b ?? "");
const active = run => activeStatuses.has(run?.status);
const locality = () => state.localities.find(row => same(row.id, state.localityId));
const selectedPack = () => state.packs.find(pack => same(pack.id, locality()?.replay_pack));
const participant = id => state.participants.find(row => same(row.id, id));
const safeId = value => encodeURIComponent(value);
const label = value => String(value ?? "").replaceAll("_", " ").replace(/^[a-z]/, ch => ch.toUpperCase());
const number = new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 });
const date = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", timeZone: "UTC" });
const dateLong = new Intl.DateTimeFormat(undefined, { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", timeZone: "UTC" });
const numeric = value => value !== null && value !== undefined && value !== "" && Number.isFinite(Number(value));
const qty = (value, unit = "") => numeric(value) ? number.format(Number(value)) + (unit ? " " + unit : "") : "—";
const utc = (value, long = false) => {
  const parsed = value ? new Date(value) : null;
  return parsed && !Number.isNaN(parsed.getTime()) ? (long ? dateLong : date).format(parsed) + " UTC" : "—";
};

function el(tag, className, ...children) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  for (const child of children) {
    if (child !== null && child !== undefined) node.append(child instanceof Node ? child : String(child));
  }
  return node;
}

function btn(text, fn, style = "btn-secondary") {
  const button = el("button", "btn " + style, text);
  button.type = "button";
  button.addEventListener("click", async () => {
    button.disabled = true;
    try {
      await fn(button);
    } catch (error) {
      notify(error.message, true);
    } finally {
      if (button.isConnected) button.disabled = false;
    }
  });
  return button;
}

function badge(value, tone = "") {
  return el("span", "badge " + tone, value);
}

function empty(text, inset = false) {
  return el("div", "empty" + (inset ? " inset" : ""), text);
}

function table(headers, values, fallback) {
  if (!values.length) return empty(fallback);
  const header = el("tr", "", ...headers.map(text => {
    const cell = el("th", "", text);
    cell.scope = "col";
    return cell;
  }));
  const rows = values.map(row => el("tr", "", ...row.map(item => el("td", "", item))));
  const scroller = el("div", "table-scroll", el("table", "", el("thead", "", header), el("tbody", "", ...rows)));
  scroller.tabIndex = 0;
  scroller.setAttribute("aria-label", headers[0] + " table");
  return scroller;
}

function details(items) {
  return el("dl", "details", ...items.map(([term, content]) => el("div", "", el("dt", "", term), el("dd", "", content))));
}

function card(title, subtitle, body, control = null) {
  return el("article", "card", el("div", "card-head", el("div", "", el("h2", "", title), subtitle ? el("p", "", subtitle) : null), control), body);
}

function notify(text, warning = false) {
  clearTimeout(toastTimer);
  const toast = $("toast");
  toast.textContent = text;
  toast.classList.toggle("error", warning);
  toast.hidden = false;
  toastTimer = setTimeout(() => { toast.hidden = true; }, 4200);
}

async function api(path, options = {}) {
  const key = sessionStorage.getItem("gridmind-key");
  const headers = new Headers(options.headers);
  if (key) headers.set("X-API-Key", key);
  if (options.body !== undefined) headers.set("Content-Type", "application/json");
  const response = await fetch("/api" + path, {
    ...options, headers,
    body: options.body !== undefined ? JSON.stringify(options.body) : undefined
  });
  const body = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = body?.detail;
    const nested = Array.isArray(detail) ? detail.map(item => {
      const field = item.loc?.at(-1);
      return (field ? label(field) + " — " : "") + (item.msg || "Invalid value");
    }).join(" · ") : detail;
    const error = new Error(typeof nested === "string" ? nested : "Request failed (" + response.status + ")");
    error.status = response.status;
    throw error;
  }
  return body;
}

function selectOptions(element, entries, placeholder = "", target = element.value) {
  const choices = placeholder ? [["", placeholder], ...entries] : entries;
  const matches = choices.length === element.options.length && choices.every(([value, text], index) =>
    same(element.options[index].value, value) && element.options[index].text === text);
  if (!matches) element.replaceChildren(...choices.map(([value, text]) => new Option(text, String(value))));
  if (choices.some(([value]) => same(value, target)) && !same(element.value, target)) element.value = String(target);
}

function runUrl(suffix = "") {
  return "/runs/" + safeId(state.runId) + suffix;
}

function updateConnection(connected, message) {
  $("connection-dot").className = "dot " + (connected ? "ok" : "down");
  $("connection-text").textContent = message;
}

async function loadBootstrap(keepRun = true) {
  const initial = await api("/bootstrap");
  state.packs = initial.packs ?? [];
  state.localities = initial.localities ?? [];
  state.facilityTypes = initial.facility_types ?? [];
  state.runs = initial.runs ?? [];
  state.health = initial.health;
  state.ready = true;
  $("startup-error").hidden = true;
  updateConnection(true, "Local server connected");
  const remembered = state.localities.find(row => same(row.id, sessionStorage.getItem("gridmind-locality")));
  const preferred = locality() ?? remembered ?? state.localities[0];
  if (!keepRun || !same(preferred?.id, state.localityId)) {
    await setLocality(preferred?.id ?? "");
  } else {
    await loadParticipants();
    render();
  }
}

function reportStartupError(error) {
  updateConnection(false, "Local server unavailable");
  const box = $("startup-error");
  box.hidden = false;
  const text = error.status === 401 || error.status === 403
    ? "Enter the local API key to connect."
    : error.message || "Could not reach the local server.";
  box.replaceChildren(el("p", "", text), btn(error.status === 401 || error.status === 403 ? "Enter API key" : "Retry", async () => {
    if (error.status === 401 || error.status === 403) {
      openEditor("Connect to GRIDMIND", [field("key", "Local API key", { type: "password", required: true, full: true })], async form => {
        sessionStorage.setItem("gridmind-key", String(new FormData(form).get("key")).trim());
        await loadBootstrap();
      }, "Connect");
    } else {
      await loadBootstrap();
    }
  }, "btn-secondary btn-small"));
}

async function loadParticipants() {
  const id = state.localityId;
  const rows = locality() ? await api("/participants?locality_id=" + safeId(id)) : [];
  if (same(id, state.localityId)) state.participants = Array.isArray(rows) ? rows : rows?.participants ?? [];
}

function bestRun(id) {
  const runs = state.runs.filter(run => same(run.locality_id, id));
  return runs.find(active) ?? runs[0];
}

async function setLocality(id) {
  state.localityId = id;
  state.participants = [];
  sessionStorage.setItem("gridmind-locality", String(id));
  await chooseRun(bestRun(id) ?? null, false);
  await loadParticipants();
  render();
}

async function chooseRun(run, promptView = true) {
  const localityChanged = run && !same(run.locality_id, state.localityId);
  if (localityChanged) {
    state.localityId = run.locality_id;
    state.participants = [];
    sessionStorage.setItem("gridmind-locality", String(run.locality_id));
  }
  state.epoch += 1;
  state.runId = run?.id ?? "";
  state.run = run;
  state.events = new Map();
  state.orders = [];
  state.trades = [];
  state.preview = null;
  state.report = null;
  state.comparison = null;
  state.referenceId = "";
  $("trace-step").value = "";
  if (promptView) location.hash = "simulation";
  render();
  if (localityChanged) await loadParticipants();
  if (state.runId !== "") await refreshRun();
  if (state.view === "database") await loadReports();
}

async function loadRuns() {
  const response = await api("/runs");
  state.runs = Array.isArray(response) ? response : response?.runs ?? [];
}

async function refreshRun() {
  if (state.runId === "") return;
  const version = state.epoch;
  const last = state.events.size ? Math.max(...state.events.keys()) : -1;
  const url = runUrl();
  const wantMarket = state.view === "simulation";
  const [run, events, orders, trades] = await Promise.all([
    api(url),
    api(url + "/events?after=" + last + "&limit=100"),
    wantMarket ? api(url + "/orders") : null,
    wantMarket ? api(url + "/trades") : null
  ]);
  if (version !== state.epoch) return;
  state.run = run;
  const index = state.runs.findIndex(row => same(row.id, run.id));
  if (index < 0) state.runs.unshift(run);
  else state.runs.splice(index, 1, run);
  for (const event of Array.isArray(events) ? events : events?.events ?? []) state.events.set(Number(event.step), event);
  if (orders) state.orders = Array.isArray(orders) ? orders : orders.orders ?? [];
  if (trades) state.trades = Array.isArray(trades) ? trades : trades.trades ?? [];
  updateConnection(true, "Local server connected");
  render();
}

function latest() {
  if (state.run?.latest) return state.run.latest;
  if (state.events.size) return state.events.get(Math.max(...state.events.keys()));
  return null;
}

async function poll() {
  if (!state.ready || refreshBusy) return;
  const currentActive = active(state.run);
  const needsFrames = state.run && state.events.size < (state.run.completed_steps ?? 0);
  const hasActiveLocalities = state.runs.some(active);
  if (!currentActive && !needsFrames && !hasActiveLocalities) return;
  refreshBusy = true;
  try {
    if (currentActive || needsFrames) await refreshRun();
    if (hasActiveLocalities && Date.now() >= nextRunListCheck) {
      nextRunListCheck = Date.now() + 3500;
      await loadRuns();
      render();
    }
  } catch (error) {
    updateConnection(false, "Replay connection interrupted");
  } finally {
    refreshBusy = false;
  }
}

function showView() {
  const target = location.hash.slice(1);
  const allowed = ["simulation", "participants", "renewables", "agents", "history", "database"];
  state.view = allowed.includes(target) ? target : "simulation";
  for (const link of document.querySelectorAll("[data-view]")) {
    if (link.dataset.view === state.view) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  for (const view of document.querySelectorAll(".view")) view.hidden = view.id !== "view-" + state.view;
  document.querySelector(".rail").classList.remove("opened");
  document.querySelector(".menu-toggle").setAttribute("aria-expanded", "false");
  render();
  if (state.ready) {
    if (state.view === "database") loadReports().catch(error => notify(error.message, true));
    if (state.view === "history") loadRuns().then(render).catch(error => notify(error.message, true));
    if (state.view === "simulation" && state.run) refreshRun().catch(error => notify(error.message, true));
  }
}

function render() {
  const locations = state.localities.map(item => [item.id, item.name]);
  const selector = $("locality-select");
  const changed = locations.length !== selector.options.length ||
    locations.some(([id, text], index) => !same(selector.options[index]?.value, id) || selector.options[index]?.text !== text);
  if (changed) selectOptions(selector, locations, locations.length ? "" : "Choose a locality", state.localityId);
  selector.value = locations.length ? String(state.localityId) : "";
  selector.disabled = !locations.length;
  const views = {
    simulation: renderSimulation, participants: renderParticipants, renewables: renderRenewables,
    agents: renderTrace, history: renderHistory, database: renderReports
  };
  views[state.view]();
}

function gridReason(value) {
  const known = {
    nominal: "Import connection available",
    upstream_outage: "Upstream outage recorded in the replay",
    historical_outage: "Upstream outage recorded in the replay",
    scheduled_outage: "Scheduled disconnection",
    overload: "Sustained import shortfall",
    feeder_stress: "Sustained import shortfall",
    restored: "Upstream supply restored"
  };
  return known[value] ?? label(value).replaceAll("-", " ");
}

function renderSimulation() {
  const run = state.run;
  const step = latest();
  const grid = step?.grid ?? {};
  const totals = step?.totals ?? {};
  const market = step?.market ?? {};
  const islanded = grid.status === "islanded";
  const running = active(run);
  const anotherActive = state.runs.find(row => same(row.locality_id, state.localityId) && active(row));
  $("grid-hero").classList.toggle("islanded", islanded);
  $("grid-status").textContent = grid.status ? (islanded ? "Islanded" : "Grid connected") : run ? label(run.status) : "Ready";
  $("grid-heading").textContent = islanded ? "Local energy only" : step ? "Grid available" :
    !run ? "Set up your first run" : running ? "Preparing the replay" : "No completed intervals";
  $("grid-detail").textContent = run?.error || gridReason(grid.reason) || (locality() ? locality().name : "Add a locality to begin.");
  const time = $("replay-clock");
  time.textContent = utc(step?.timestamp);
  if (step?.timestamp) time.dateTime = step.timestamp;
  else time.removeAttribute("datetime");
  $("run-progress").textContent = run ? label(run.status) + " · " + (run.completed_steps ?? 0) + " / " + run.steps +
    (run.interval_minutes ? " · " + run.interval_minutes + " min per interval" : " intervals") : "No run selected";
  $("open-setup").hidden = running;
  $("open-setup").textContent = !locality() ? "Set up a locality →" : anotherActive ? "View active simulation →" : "Start simulation →";
  $("stop-run").hidden = !running;
  $("stop-run").disabled = run?.status === "stopping";
  $("open-order").disabled = !runAcceptsOrders() || !(run.participants ?? state.participants).length;
  $("market-status").textContent = !run ? "Awaiting a simulation" :
    run.scenario === "reference" ? (running ? "Reference run · market closed · requests can be staged" : "Reference run · saved results") :
    !running ? "Market closed · saved results" : market.open ? "Open · allocating local energy" : "Closed · requests can be staged";
  $("run-metrics").replaceChildren(
    metric("Next-hour risk", numeric(grid.risk_next_hour) ? qty(100 * grid.risk_next_hour, "%") : "—", "Import shortfall"),
    metric("Import headroom", qty(grid.headroom_kw, "kW"), "Current interval"),
    metric("Essential load served", qty(totals.essential_served_kwh, "kWh"), "Current interval"),
    metric("Energy traded", qty(totals.traded_kwh, "kWh"), "Current interval")
  );
  $("dispatch-summary").replaceChildren(step ? details([
    ["Local generation", qty(totals.generation_kwh, "kWh")],
    ["Battery discharge", qty(totals.battery_discharge_kwh, "kWh")],
    ["Grid imports", qty(totals.grid_import_kwh, "kWh")],
    ["Unmet demand", qty(totals.unserved_kwh, "kWh")]
  ]) : empty("Supply is calculated after the first interval."));
  renderActiveLocalities();
  renderMarket(market, step, running);
  requestAnimationFrame(drawRisk);
}

function metric(title, value, detail) {
  return el("div", "metric", el("span", "", title), el("strong", "", value), el("small", "", detail));
}

function renderActiveLocalities() {
  const pane = $("active-localities");
  const running = state.runs.filter(active);
  pane.hidden = running.length === 0 || running.length === 1 && same(running[0].id, state.runId);
  if (pane.hidden) return;
  pane.replaceChildren(el("h2", "", "Active localities"), el("div", "action-group",
    ...running.map(run => {
      const current = same(run.id, state.runId);
      const place = run.locality_name ?? state.localities.find(row => same(row.id, run.locality_id))?.name ?? "Locality";
      const control = btn(place + " · " + qty(run.completed_steps ?? 0) + " / " + run.steps, () => chooseRun(run),
        current ? "btn-primary btn-small" : "btn-secondary btn-small");
      if (current) control.setAttribute("aria-current", "true");
      return control;
    })
  ));
}

function drawRisk() {
  const canvas = $("risk-chart");
  if (state.view !== "simulation" || !canvas) return;
  const frames = new Map(state.events);
  const step = latest();
  if (step) frames.set(Number(step.step), step);
  const values = [...frames].sort((a, b) => a[0] - b[0]).slice(-96).filter(([, item]) => numeric(item.grid?.risk_next_hour));
  canvas.hidden = !values.length;
  $("risk-empty").hidden = Boolean(values.length);
  if (!values.length || canvas.clientWidth === 0) return;
  const w = canvas.clientWidth;
  const h = canvas.clientHeight;
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.round(w * dpr);
  canvas.height = Math.round(h * dpr);
  const ctx = canvas.getContext("2d");
  if (!ctx) return;
  ctx.scale(dpr, dpr);
  const p = { left: 39, right: 15, top: 12, bottom: 27 };
  const width = w - p.left - p.right;
  const height = h - p.top - p.bottom;
  const bottom = p.top + height;
  const first = values[0][0];
  const last = values.at(-1)[0];
  const points = values.map(([tick, item]) => [
    p.left + (last === first ? width / 2 : width * (tick - first) / (last - first)),
    bottom - height * Math.min(1, Math.max(0, Number(item.grid.risk_next_hour)))
  ]);
  ctx.font = '11px "Segoe UI", sans-serif';
  ctx.fillStyle = "#566456";
  ctx.strokeStyle = "#e3e8dd";
  for (const fraction of [0, .25, .5, .75, 1]) {
    const y = bottom - height * fraction;
    ctx.beginPath();
    ctx.moveTo(p.left, y);
    ctx.lineTo(w - p.right, y);
    ctx.stroke();
    if (fraction === 0 || fraction === 0.5 || fraction === 1) ctx.fillText(Math.round(100 * fraction) + "%", 4, y + 4);
  }
  ctx.fillText("Step " + (first + 1), p.left, bottom + 20);
  if (last !== first && width > 128) {
    const end = "Step " + (last + 1);
    ctx.fillText(end, w - p.right - ctx.measureText(end).width, bottom + 20);
  }
  ctx.beginPath();
  points.forEach(([x, y], index) => index ? ctx.lineTo(x, y) : ctx.moveTo(x, y));
  ctx.strokeStyle = "#397957";
  ctx.lineWidth = 2;
  ctx.stroke();
  ctx.lineTo(points.at(-1)[0], bottom);
  ctx.lineTo(points[0][0], bottom);
  ctx.closePath();
  ctx.fillStyle = "#67966a29";
  ctx.fill();
  const [x, y] = points.at(-1);
  ctx.beginPath();
  ctx.arc(x, y, 4, 0, 2 * Math.PI);
  ctx.fillStyle = "#27563c";
  ctx.fill();
  canvas.setAttribute("aria-label", "Next-hour risk by replay step. Latest " + qty(values.at(-1)[1].grid.risk_next_hour * 100, "%"));
}

function orderName(row) {
  return row.participant_name ?? participant(row.participant_id)?.name ?? row.participant_id ?? "—";
}

function orderStatus(status) {
  const good = new Set(["filled", "open", "completed", "settled"]);
  const bad = new Set(["failed", "rejected", "cancelled", "canceled"]);
  return badge(label(status) || "—", good.has(status) ? "good" : bad.has(status) ? "bad" : "warning");
}

function renderMarket(market, step, running) {
  const liveOrders = market.orders ?? [];
  const latestIds = new Set(liveOrders.map(row => String(row.id)));
  const history = state.orders.filter(row => !latestIds.has(String(row.id)));
  const manual = history.filter(row => row.manual);
  const orders = [...liveOrders, ...manual.slice(0, 25)];
  const sells = market.sellers ?? [];
  const fills = state.trades.length ? state.trades : market.fills ?? [];
  const orderRows = orders.map(row => {
    const submitted = state.orders.find(item => same(item.id, row.id) && item.manual);
    const canCancel = running && runAcceptsOrders() && submitted && ["queued", "pending"].includes(submitted.status);
    const interval = numeric(row.step ?? step?.step) ? Number(row.step ?? step.step) + 1 : "Next";
    return [
      orderName(row), badge(row.side === "buy" ? "Request" : "Offer"), interval,
      row.priority !== null && row.priority !== undefined ? label(row.priority) : "—",
      row.side === "buy" ? label(row.urgency) || "—" : "—",
      numeric(row.quantity_kwh) ? qty(row.quantity_kwh, "kWh") : qty(row.power_kw, "kW"),
      qty(row.price),
      qty(row.filled_kwh, "kWh"),
      orderStatus(row.status),
      canCancel ? btn("Cancel", () => cancelOrder(row.id), "btn-secondary btn-small") : "—"
    ];
  });
  const sellers = sells.map(row => [orderName(row), qty(row.available_kwh,"kWh"), qty(row.offered_kwh,"kWh"), qty(row.remaining_kwh,"kWh")]);
  const recent = fills.slice().sort((a, b) => (b.step ?? 0) - (a.step ?? 0)).slice(0, 25);
  const content = $("market-content");
  const scrolls = Array.from(content.querySelectorAll(".table-scroll"), element => element.scrollLeft);
  content.replaceChildren(
    el("section", "", el("h3", "subhead", "Requests & offers"),
      table(["Participant", "Side", "Step", "Priority", "Urgency", "Requested", "Limit credits / kWh", "Filled", "Status", "Manage"], orderRows, "The order book is empty.")),
    el("section", "", el("h3", "subhead", "Available sellers"),
      table(["Seller", "Available", "Offered", "Remaining"], sellers, "No local seller supply in this interval.")),
    el("section", "", el("h3", "subhead", "Latest trades"),
      table(["Buyer", "Seller", "Energy", "Credits / kWh"], recent.map(row => [
        row.buyer_name ?? participant(row.buyer_id)?.name ?? "—",
        row.seller_name ?? participant(row.seller_id)?.name ?? "—",
        qty(row.quantity_kwh, "kWh"), qty(row.price ?? row.price_per_kwh)
      ]), "Settled trades will appear here."))
  );
  content.querySelectorAll(".table-scroll").forEach((element, index) => { element.scrollLeft = scrolls[index] ?? 0; });
}

function runAcceptsOrders() {
  return ["queued", "running"].includes(state.run?.status);
}

async function cancelOrder(id) {
  await api(runUrl("/orders/" + safeId(id) + "/cancel"), { method: "POST" });
  await refreshRun();
  notify("Order cancelled.");
}

function renderParticipants() {
  const rows = state.localities.map(row => [
    row.name, qty(row.import_limit_kw, "kW"), row.island_capable ? badge("Island capable", "good") : badge("Grid connected"),
    state.packs.find(pack => same(pack.id, row.replay_pack))?.name ?? label(row.replay_pack),
    el("div", "action-group",
      btn("Select", () => setLocality(row.id), "btn-quiet btn-small"),
      btn("Edit", () => editLocality(row), "btn-secondary btn-small"),
      btn("Delete", () => deleteEntry("localities", row), "btn-quiet btn-small text-danger"))
  ]);
  $("locality-list").replaceChildren(table(["Locality", "Import capacity", "Mode", "Replay dataset", "Manage"], rows, "Add a locality to configure your first run."));
  $("participants-context").textContent = locality()?.name ?? "Select a locality to view its participants.";
  $("open-participant").disabled = !locality();
  $("participant-list").replaceChildren(table(["Participant", "Facility", "Base load", "Essential load", "Manage"],
    state.participants.map(row => [
      row.name, row.facility_type ?? state.facilityTypes.find(type => same(type.id, row.facility_type_id))?.name ?? label(row.facility_type_id),
      qty(row.base_load_kw,"kW"), qty(row.essential_kw,"kW"),
      el("div", "action-group",
        btn("Edit", () => editParticipant(row), "btn-secondary btn-small"),
        btn("Delete", () => deleteEntry("participants", row), "btn-quiet btn-small text-danger"))
    ]), "No participants registered for this locality."));
  $("asset-list").replaceChildren(table(["Participant", "Solar", "Wind", "Battery capacity", "Battery power", "Manage"],
    state.participants.map(row => [
      row.name, qty(row.solar_kw,"kW"), qty(row.wind_kw,"kW"), qty(row.battery_kwh,"kWh"), qty(row.battery_kw,"kW"),
      btn("Edit assets", () => editParticipant(row), "btn-secondary btn-small")
    ]), "Add a participant to register energy assets."));
}

function renderRenewables() {
  const picker = $("renewable-participant");
  const previous = picker.value;
  const roster = state.run?.participants ?? state.participants;
  const owners = roster.filter(row => Number(row.solar_kw) > 0 || Number(row.wind_kw) > 0);
  selectOptions(picker, owners.map(row => [row.id, row.name]), "All renewable owners", previous);
  const sample = latest();
  const outlook = (sample?.renewables ?? []).filter(row => !picker.value || same(row.participant_id, picker.value));
  if (!outlook.length) {
    const prompt = !owners.length ? "Register a solar or wind asset to see a participant outlook." :
      sample ? "No forecast for this participant in the selected interval." : "Start or select a run to see renewable estimates.";
    $("renewables-content").replaceChildren(card("Energy outlook", "", empty(prompt)));
    return;
  }
  const views = outlook.map(row => card(
    row.participant_name ?? participant(row.participant_id)?.name ?? "Participant",
    row.valid_at ? "Valid " + utc(row.valid_at) : utc(sample.timestamp),
    el("div", "", details([
      ["Forecast solar", qty(row.forecast_solar_kw,"kW")],
      ["Forecast wind", qty(row.forecast_wind_kw,"kW")],
      ["Expected load", qty(row.expected_load_kw,"kW")],
      ["Output range", numeric(row.range_low_kw) && numeric(row.range_high_kw) ? qty(row.range_low_kw) + "–" + qty(row.range_high_kw,"kW") : "—"],
      ["Possible shortfall", qty(row.shortfall_kw,"kW")]
    ]), row.advisory ? el("p", "advice", row.advisory) : null),
    badge(label(row.condition) || "Weather", Number(row.shortfall_kw) > 0 ? "warning" : "good")
  ));
  $("renewables-content").replaceChildren(el("div", "outlook-cards", ...views));
}

function renderTrace() {
  const picker = $("trace-step");
  const previous = picker.value;
  const eventSteps = [...state.events.keys()].sort((a, b) => a - b);
  selectOptions(picker, eventSteps.map(step => [step, "Step " + (step + 1)]), "Latest", previous);
  const sample = state.preview ?? (picker.value !== "" ? state.events.get(Number(picker.value)) : latest());
  const trace = sample?.trace ?? [];
  $("preview-step").disabled = !state.run;
  $("trace-heading").textContent = state.preview ? "Preview · step " + (Number(sample.step) + 1) :
    sample ? "Step " + (Number(sample.step) + 1) + " · " + utc(sample.timestamp) : "Latest completed interval";
  if (!trace.length) {
    $("trace-content").replaceChildren(empty("The coordination trace appears after the first interval."));
    return;
  }
  const rows = trace.map(row => el("li", "", el("span", "trace-dot"),
    el("div", "", el("h3", "", row.agent ?? "Agent", orderStatus(row.status)),
      el("p", "", [label(row.action), row.reason].filter(Boolean).join(" · ")),
      typeof row.summary === "string" && row.summary ? el("p", "", row.summary) :
        row.summary && Object.keys(row.summary).length ? el("p", "", Object.entries(row.summary).slice(0, 6).map(([key, value]) => label(key) + " " + (typeof value === "object" ? JSON.stringify(value) : value)).join(" · ")) : null
    )));
  $("trace-content").replaceChildren(el("ol", "trace-list", ...rows));
}

function runTitle(run) {
  const place = run.locality_name ?? state.localities.find(row => same(row.id, run.locality_id))?.name ?? "Locality";
  return [place, scenarioLabels[run.scenario] ?? label(run.scenario)].filter(Boolean).join(" · ");
}

function renderHistory() {
  $("run-list").replaceChildren(table(["Simulation", "Status", "Progress", "Worker PID", "Started", "Manage"], state.runs.map(run => [
    runTitle(run), orderStatus(run.status), (run.completed_steps ?? 0) + " / " + run.steps,
    run.worker_pid ?? "—", utc(run.started_at ?? run.created_at, true), btn("Open", () => chooseRun(run), "btn-secondary btn-small")
  ]), "No saved simulations. Start a run to create the first record."));
}

async function loadReports() {
  const version = state.epoch;
  const suffix = state.runId !== "" ? "?run_id=" + safeId(state.runId) : "";
  const [data, benchmark] = await Promise.all([api("/reports" + suffix),
    state.benchmark ?? api("/benchmarks/ugim").catch(() => ({ available: false })), loadRuns()]);
  if (version !== state.epoch) return;
  state.report = data;
  state.benchmark = benchmark;
  renderReports();
}

function formatReportField(value) {
  if (value === null || value === undefined) return "—";
  if (typeof value === "object") return Object.entries(value).map(([key, item]) => label(key) + " " + (typeof item === "object" ? JSON.stringify(item) : item)).join(" · ");
  if (typeof value === "boolean") return value ? "Yes" : "No";
  return typeof value === "number" ? qty(value) : String(value);
}

function comparisonCard() {
  const current = state.run;
  const available = current?.status === "completed" ? state.runs.filter(run =>
    run.status === "completed" && run.scenario === "reference" && !same(run.id, current.id) &&
    same(run.locality_id, current.locality_id)) : [];
  if (!available.length) {
    return card("Paired comparison", "", empty("Open a completed run and finish a reference run in the same locality to compare."));
  }
  const picker = field("reference", "Reference run", {
    choices: available.map(run => [run.id, utc(run.created_at, true)]),
    required: true, value: state.referenceId
  });
  const select = picker.querySelector("select");
  select.addEventListener("change", async () => {
    const version = state.epoch;
    const referenceId = state.referenceId = select.value;
    state.comparison = null;
    if (!referenceId) {
      renderReports();
      return;
    }
    try {
      const comparison = await api("/comparison?run_id=" + safeId(current.id) + "&reference_id=" + safeId(referenceId));
      if (version === state.epoch && same(referenceId, state.referenceId)) {
        state.comparison = comparison;
        renderReports();
      }
    } catch (error) {
      notify(error.message, true);
    }
  });
  const result = state.comparison;
  let report = empty("Choose a reference run.");
  if (result) {
    const keys = [
      ["served_kwh", "Energy served"], ["unserved_kwh", "Energy unserved"],
      ["essential_served_kwh", "Essential energy served"],
      ["essential_unserved_kwh", "Essential energy unserved"], ["traded_kwh", "Energy traded"]
    ];
    const variant = run => label(run.scenario) + (same(run.id, current.id) ? " · selected" : " · comparison");
    report = el("div", "",
      el("p", "comparison-note", result.comparable ? "Matched replay settings" : "Replay settings differ"),
      table(["Measure", ...result.runs.map(variant)], keys.map(([name, title]) => [
        title, ...result.runs.map(run => qty(run.totals?.[name], "kWh"))
      ]), "No comparison totals available.")
    );
  }
  return card("Paired comparison", "", report, picker);
}

function renderReports() {
  const data = state.report;
  const sql = data?.health?.sql ?? data?.sql_backend ?? data?.sql ?? state.health?.sql ?? "—";
  const events = data?.health?.event_store ?? data?.event_store ?? state.health?.event_store ?? "—";
  const ready = card("Storage status", state.run ? runTitle(state.run) : "", details([
    ["SQL", formatReportField(sql)], ["Event store", formatReportField(events)],
    ["Published steps", data?.published_steps ?? "—"]
  ]));
  const reports = !data ? [card("Saved records", "", empty("Loading database reports…"))] :
    [
      data.totals ? card("Energy and settlement", "Totals across the selected records", details([
        ["Steps", qty(data.totals.step_count)], ["Served energy", qty(data.totals.served_kwh,"kWh")],
        ["Unserved energy", qty(data.totals.unserved_kwh,"kWh")], ["Essential load served", qty(data.totals.essential_served_kwh,"kWh")],
        ["Essential load unserved", qty(data.totals.essential_unserved_kwh,"kWh")], ["Settled trades", qty(data.trade_count)],
        ["Settled energy", qty(data.settled_kwh,"kWh")], ["Settlement value", qty(data.settlement_value)]
      ])) : null,
      comparisonCard(),
      data.counts ? card("Relational records", "", details(Object.entries(data.counts).map(([key, value]) => [label(key), qty(value)]))) : null,
      benchmarkCard(),
      data.datasets ? card("Imported datasets", "", table(["Dataset", "Identifier", "Checksum"],
        data.datasets.map(item => [item.name, item.id, item.digest]), "No replay dataset imported.")) : null,
      data.audit ? card("Audit trail", "Latest database actions", table(["Action", "Record", "Timestamp"],
        data.audit.map(row => [label(row.action), row.subject_id, utc(row.timestamp, true)]), "No audit events recorded.")) : null
    ];
  $("database-content").replaceChildren(el("div", "report-stack", ready, ...reports));
}

function benchmarkCard() {
  const data = state.benchmark;
  if (!data || data.available === false || !Array.isArray(data.recorded_trades)) return null;
  const trades = data.recorded_trades;
  const sample = trades.slice(0, 8).map(row => [
    row.day, row.buyer, row.seller, qty(row.quantity_kwh, "kWh"), qty(row.price_eur_per_kwh)
  ]);
  return card("µGIM market benchmark", qty(data.hourly_participant_records?.length) + " hourly records · " + qty(trades.length) + " recorded trades",
    table(["Day", "Buyer", "Seller", "Energy", "EUR / kWh"], sample, "No recorded trades."));
}

function field(name, title, opts = {}) {
  const { type = "text", choices, value = "", min, max, step, required = false, full = false } = opts;
  const control = choices ? el("select") : el("input");
  control.name = name;
  if (choices) {
    selectOptions(control, choices, required ? "Choose " + title.toLowerCase() : "", value);
  } else {
    control.type = type;
    if (type === "checkbox") control.checked = Boolean(value);
    else control.value = value;
    if (min !== undefined) control.min = min;
    if (max !== undefined) control.max = max;
    if (step !== undefined) control.step = step;
    if (type === "text") control.maxLength = 100;
  }
  control.required = required;
  const className = "field" + (full ? " full" : "") + (type === "checkbox" ? " check" : "");
  return type === "checkbox" ? el("label", className, control, title) :
    el("label", className, el("span", "", title), control);
}

function openEditor(title, fields, onSubmit, submitLabel = "Save") {
  const dialog = $("editor-dialog");
  editorGeneration += 1;
  submitEditor = onSubmit;
  $("editor-title").textContent = title;
  $("editor-fields").replaceChildren(...fields.filter(Boolean));
  $("editor-error").hidden = true;
  $("save-editor").textContent = submitLabel;
  $("save-editor").disabled = false;
  if (!dialog.open) dialog.showModal();
}

function editLocality(row = null) {
  const packs = state.packs.map(item => [item.id, item.name]);
  const inputs = [
    field("name", "Locality name", { value: row?.name ?? "", required: true, full: true }),
    field("import_limit_kw", "Import limit (kW)", { type: "number", value: row?.import_limit_kw ?? 50, min: 0.01, step: "any", required: true }),
    field("replay_pack", "Replay dataset", { choices: packs, value: row?.replay_pack ?? packs[0]?.[0], required: true }),
    field("island_capable", "Local supply can operate while disconnected", { type: "checkbox", value: row?.island_capable ?? true, full: true })
  ];
  openEditor(row ? "Edit locality" : "Add locality", inputs, async form => {
    const data = new FormData(form);
    const value = await api("/localities" + (row ? "/" + safeId(row.id) : ""), {
      method: row ? "PUT" : "POST",
      body: {
        name: String(data.get("name")).trim(), import_limit_kw: Number(data.get("import_limit_kw")),
        island_capable: data.has("island_capable"), replay_pack: data.get("replay_pack")
      }
    });
    await loadBootstrap();
    if (!row && value?.id !== undefined) await setLocality(value.id);
    notify("Locality saved.");
  });
}

function editParticipant(row = null) {
  if (!locality()) throw new Error("Choose a locality first.");
  const place = state.localityId;
  const types = state.facilityTypes.map(type => [type.id, type.name]);
  const numberFields = [
    ["base_load_kw","Base load (kW)",10], ["essential_kw","Essential load (kW)",0],
    ["solar_kw","Solar capacity (kW)",0], ["wind_kw","Wind capacity (kW)",0],
    ["battery_kwh","Battery capacity (kWh)",0], ["battery_kw","Battery power (kW)",0]
  ];
  const inputs = [
    field("name", "Participant name", { value: row?.name ?? "", required: true, full: true }),
    field("facility_type_id", "Facility type", { choices: types, value: row?.facility_type_id ?? types[0]?.[0], required: true, full: true }),
    ...numberFields.map(([name, title, fallback]) => field(name, title, { type: "number", value: row?.[name] ?? fallback, min: 0, step: "any", required: true }))
  ];
  openEditor(row ? "Edit participant" : "Add participant", inputs, async form => {
    const data = new FormData(form);
    const body = {
      locality_id: place, name: String(data.get("name")).trim(), facility_type_id: data.get("facility_type_id")
    };
    for (const [name] of numberFields) body[name] = Number(data.get(name));
    if (body.essential_kw > body.base_load_kw) throw new Error("Essential load must be within the base load.");
    await api("/participants" + (row ? "/" + safeId(row.id) : ""), { method: row ? "PUT" : "POST", body });
    await loadParticipants();
    render();
    notify("Participant saved.");
  });
}

function deleteEntry(resource, row) {
  openEditor("Delete " + row.name + "?", [el("p", "form-note", "This removes the entry from the locality registry.")], async () => {
    await api("/" + resource + "/" + safeId(row.id), { method: "DELETE" });
    if (resource === "localities") await loadBootstrap();
    else {
      await loadParticipants();
      render();
    }
    notify("Entry deleted.");
  }, "Delete");
}

function startEditor() {
  if (!locality()) {
    location.hash = "participants";
    editLocality();
    return;
  }
  const ongoing = state.runs.find(row => same(row.locality_id, state.localityId) && active(row));
  if (ongoing) return chooseRun(ongoing);
  if (!state.participants.length) {
    location.hash = "participants";
    editParticipant();
    return;
  }
  const place = state.localityId;
  const pack = selectedPack();
  const cap = pack?.steps ?? 288;
  const choices = Object.entries(scenarioLabels);
  const scenario = field("scenario", "Scenario", { choices, value: "historical", required: true, full: true });
  const reference = field("reference_scenario", "Reference grid scenario", { choices: choices.filter(([id]) => id !== "reference"), value: "historical", required: true, full: true });
  const outageStep = field("outage_step", "Outage starts at step", { type: "number", min: 1, max: 672, step: 1 });
  const outageDuration = field("outage_duration", "Outage duration (steps)", { type: "number", value: 4, min: 1, max: 96, step: 1, required: true });
  const weather = field("augment_weather", "Include weather variability", { type: "checkbox", full: true });
  const updateSchedule = () => {
    const type = scenario.querySelector("select").value;
    reference.hidden = type !== "reference";
    const gridScenario = type === "reference" ? reference.querySelector("select").value : type;
    outageStep.hidden = outageDuration.hidden = gridScenario !== "scheduled";
    weather.hidden = gridScenario !== "stress";
  };
  scenario.addEventListener("change", updateSchedule);
  reference.addEventListener("change", updateSchedule);
  updateSchedule();
  const roster = el("fieldset", "schedule-roster", el("legend", "", "Schedule participants to join"));
  for (const row of state.participants) {
    const option = field("join_participant_ids", row.name, { type: "checkbox" });
    option.querySelector("input").value = row.id;
    roster.append(option);
  }
  const join = field("join_step", "Join at replay step", { type: "number", min: 2, max: 672, step: 1 });
  const schedule = el("details", "schedule", el("summary", "", "Schedule up to three participants to join"),
    roster, join);
  openEditor("Start simulation", [
    pack?.interval_minutes ? el("p", "form-note", pack.name + " · " + qty(pack.interval_minutes, "min per step")) : null,
    scenario,
    reference,
    field("steps", "Replay steps", { type: "number", value: Math.min(cap,672), min: 1, max: Math.min(cap,672), step: 1, required: true }),
    field("start_offset", "First dataset step", { type: "number", value: 1, min: 1, max: cap, step: 1, required: true }),
    field("speed", "Steps per second", { type: "number", value: 1, min: 0.1, max: 20, step: "any", required: true }),
    field("seed", "Simulation seed", { type: "number", value: 42, min: 0, max: 2147483647, step: 1, required: true }),
    outageStep, outageDuration, weather, schedule
  ], async form => {
    const data = new FormData(form);
    const body = {
      locality_id: place, scenario: data.get("scenario"), steps: Number(data.get("steps")),
      start_offset: Number(data.get("start_offset")) - 1, speed: Number(data.get("speed")), seed: Number(data.get("seed"))
    };
    if (body.start_offset + body.steps > cap) throw new Error("The replay extends beyond the selected dataset.");
    if (body.scenario === "reference") body.reference_scenario = data.get("reference_scenario");
    if (!weather.hidden && data.has("augment_weather")) body.augment_weather = true;
    if (body.scenario === "scheduled" || body.reference_scenario === "scheduled") {
      body.outage_duration = Number(data.get("outage_duration"));
      if (data.get("outage_step") !== "") {
        body.outage_step = Number(data.get("outage_step")) - 1;
        if (body.outage_step >= body.steps) throw new Error("The scheduled outage must be within the replay window.");
      }
    }
    const joins = data.getAll("join_participant_ids");
    if (joins.length) {
      if (joins.length > 3) throw new Error("Choose at most three participants.");
      const step = Number(data.get("join_step"));
      if (step < 2 || step > body.steps) throw new Error("Choose a join step between 2 and the end of the replay.");
      body.join_step = step - 1;
      body.join_participant_ids = joins;
    }
    const run = await api("/runs", { method: "POST", body });
    await chooseRun(run);
    notify("Simulation started.");
  }, "Run simulation");
}

function orderEditor() {
  const run = state.run;
  if (!run || !runAcceptsOrders()) return;
  const url = runUrl("/orders");
  const roster = run.participants ?? state.participants;
  const people = roster.map(row => [row.id, row.name +
    (row.active_from_step > (run.completed_steps ?? 0) ? " · joins step " + (row.active_from_step + 1) : "")]);
  const marketStep = Math.min(run.steps, (run.completed_steps ?? 0) + 1);
  const side = field("side", "Side", { choices: [["buy","Request energy"],["sell","Offer energy"]], value: "buy", required: true });
  const urgency = field("urgency", "Urgency", { choices: [["routine","Routine"],["urgent","Urgent"]], value: "routine", required: true });
  const power = field("power_kw", "Power (kW)", { type: "number", min: 0.004, max: 100000, step: "any", required: true });
  const price = field("limit_price", "Price limit (credits per kWh)", { type: "number", min: 0, max: 100000, step: "any", value: 12, required: true });
  const terms = el("p", "form-note");
  const priceInput = price.querySelector("input");
  let priorDefault = 12;
  side.addEventListener("change", () => {
    const sell = side.querySelector("select").value === "sell";
    const nextDefault = sell ? 6 : 12;
    if (Number(priceInput.value) === priorDefault) priceInput.value = nextDefault;
    priorDefault = nextDefault;
    urgency.querySelector("select").disabled = sell;
  });
  const updateTerms = () => {
    const ratedPower = power.querySelector("input").value;
    const minutes = run.interval_minutes ?? 15;
    terms.textContent = numeric(ratedPower) ? qty(Number(ratedPower) * minutes / 60, "kWh") + " for a " + minutes + "-minute interval." :
      "Each interval lasts " + minutes + " minutes. Leave the step blank to use the next available interval.";
  };
  power.addEventListener("input", updateTerms);
  updateTerms();
  openEditor("New request or offer", [
    field("participant_id", "Participant", { choices: people, required: true, full: true }),
    side, urgency, power, price,
    field("step", "Replay step (optional)", { type: "number", min: marketStep, max: run.steps, step: 1 }),
    terms
  ], async form => {
    const data = new FormData(form);
    const row = roster.find(item => same(item.id, data.get("participant_id")));
    if (!row) throw new Error("Choose a participant.");
    const body = {
      participant_id: row.id, side: data.get("side"), power_kw: Number(data.get("power_kw")),
      limit_price: Number(data.get("limit_price")), urgency: data.get("urgency") ?? "routine"
    };
    if (data.get("step") !== "") body.step = Number(data.get("step")) - 1;
    else if (row.active_from_step > (state.run?.completed_steps ?? 0)) body.step = row.active_from_step;
    await api(url, { method: "POST", body });
    if (same(run.id, state.runId)) await refreshRun();
    notify("Order saved.");
  }, "Submit order");
}

function guarded(button, handler) {
  button.addEventListener("click", async () => {
    try {
      await handler();
    } catch (error) {
      notify(error.message, true);
    }
  });
}

$("editor-form").addEventListener("submit", async event => {
  event.preventDefault();
  const generation = editorGeneration;
  $("editor-error").hidden = true;
  $("save-editor").disabled = true;
  try {
    await submitEditor(event.currentTarget);
    if (generation === editorGeneration) $("editor-dialog").close();
  } catch (error) {
    if (generation === editorGeneration) {
      $("editor-error").textContent = error.message;
      $("editor-error").hidden = false;
    }
  } finally {
    if (generation === editorGeneration) $("save-editor").disabled = false;
  }
});
for (const id of ["close-editor", "cancel-editor"]) $(id).addEventListener("click", () => $("editor-dialog").close());
document.querySelector(".menu-toggle").addEventListener("click", event => {
  const opened = document.querySelector(".rail").classList.toggle("opened");
  event.currentTarget.setAttribute("aria-expanded", String(opened));
});
$("locality-select").addEventListener("change", event => {
  const row = state.localities.find(item => same(item.id, event.target.value));
  setLocality(row?.id ?? "").catch(error => notify(error.message, true));
});
$("renewable-participant").addEventListener("change", renderRenewables);
$("trace-step").addEventListener("change", () => { state.preview = null; renderTrace(); });
guarded($("open-setup"), startEditor);
guarded($("stop-run"), async () => {
  $("stop-run").disabled = true;
  try {
    await api(runUrl("/stop"), { method: "POST" });
    await refreshRun();
    notify("Stop requested.");
  } finally {
    $("stop-run").disabled = state.run?.status === "stopping";
  }
});
guarded($("open-order"), orderEditor);
guarded($("open-locality"), () => editLocality());
guarded($("open-participant"), () => editParticipant());
guarded($("preview-step"), async () => {
  const version = state.epoch;
  const preview = await api(runUrl("/preview"));
  if (version !== state.epoch) return;
  state.preview = preview?.result ?? preview;
  renderTrace();
});
guarded($("refresh-runs"), async () => { await loadRuns(); renderHistory(); });
guarded($("refresh-reports"), loadReports);
window.addEventListener("hashchange", showView);
window.addEventListener("resize", () => requestAnimationFrame(drawRisk));
showView();
loadBootstrap().catch(reportStartupError);
setInterval(poll, 1000);
