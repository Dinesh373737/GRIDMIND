import React, { useEffect, useState } from "react";
import "./market.css";

const ACTIVE = new Set(["queued", "starting", "running", "stopping", "pending"]);
const showNumber = new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 });
const amount = (value, unit = "") => value != null && value !== "" && Number.isFinite(Number(value))
  ? showNumber.format(value) + (unit ? ` ${unit}` : "") : "—";
const same = (x, y) => String(x ?? "") === String(y ?? "");
const title = (s) => typeof s === "string" ? s.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase()) : "—";

const points = [
  { x: 75, name: "North district", kind: "solar" },
  { x: 305, name: "West district", kind: "home" },
  { x: 535, name: "Central district", kind: "clinic" },
  { x: 765, name: "East district", kind: "battery" },
];

function Symbol({ name, size = 22 }) {
  const icons = {
    solar: <><circle cx="17" cy="6" r="2"/><path d="M17 2v1m0 6v1m-4-4h1m6 0h1M4 12h13l2 7H2l2-7Zm2 7 1-7m4 0 1 7m-9-3h15M10 19v3"/></>,
    clinic: <><path d="M3 22V8h4V3h10v5h4v14M1 22h22M10 22v-4h4v4m-2-16v6m-3-3h6"/></>,
    battery: <><rect x="3" y="6" width="16" height="12" rx="2"/><path d="M22 10v4M10 9l-2 4h4l-2 3"/></>,
    home: <><path d="m2 11 10-8 10 8m-3-2v12H5V9m5 12v-6h4v6"/></>,
    utility: <><path d="m12 2-6 20m6-20 6 20M3 22h18M5 9h14M7 15h10M5 9v3m14-3v3"/></>,
    zone: <><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></>,
    clock: <><circle cx="12" cy="12" r="9"/><path d="M12 7v5l4 2"/></>,
  };
  return <svg aria-hidden="true" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">{icons[name] ?? icons.zone}</svg>;
}

function Diagram({ grid, step, isReference }) {
  const [frame, setFrame] = useState(null);
  useEffect(() => {
    if (frame === null || frame > 1) return;
    const timeout = setTimeout(() => setFrame(frame + 1), frame === 0 ? 1550 : 1500);
    return () => clearTimeout(timeout);
  }, [frame]);
  const live = frame === null && Boolean(grid.status);
  const islanded = live ? grid.status === "islanded" : frame >= 1;
  const trading = live ? islanded && !isReference && Boolean(step.market?.open) : frame === 2;
  const statusLabel = islanded ? (trading ? "P2P market open" : "Feeder disconnected") : "Grid connected";
  const detail = islanded ? (trading ? "Local supply is matched to requests" : "Upstream power is unavailable")
    : "The utility feeder carries power to each district";
  const drawClass = islanded ? (trading ? "is-trading" : "is-islanded") : "is-powered";
  return <section className="gm-network" aria-labelledby="gm-network-title">
    <div className="gm-network-heading">
      <div><span className="gm-market-kicker">Energy network</span><h2 id="gm-network-title">From grid power to local trade</h2></div>
      <button className="gm-market-button gm-network-action" type="button" disabled={frame === 0 || frame === 1}
        onClick={() => setFrame(frame === 2 ? null : 0)}>
        {frame === 2 ? "Exit preview" : frame !== null ? "Playing preview…" : "Preview grid failure"}
      </button>
    </div>
    <div className="gm-network-illustration" tabIndex={0} aria-label="Scrollable energy network diagram">
      <svg className={`gm-network-svg ${drawClass}`} viewBox="0 0 840 275" role="img" aria-label={`Illustrative four-district network: ${statusLabel}. ${detail}.`}>
        <defs>
          <pattern id="gm-market-dots" width="17" height="17" patternUnits="userSpaceOnUse"><circle cx="2" cy="2" r="1" fill="#ffffff18"/></pattern>
          <marker id="gm-market-arrow" markerWidth="8" markerHeight="8" refX="6" refY="4" orient="auto"><path d="M1 1l5 3-5 3" fill="none" stroke="#fcca73" strokeWidth="1.5"/></marker>
        </defs>
        <rect width="840" height="275" rx="12" fill="url(#gm-market-dots)"/>
        <g className="gm-utility-box"><rect x="340" y="12" width="160" height="58" rx="8"/><g transform="translate(355 28)"><Symbol name="utility" size={24}/></g><text x="389" y="46">Utility feeder</text></g>
        <path className="gm-utility-trunk" d="M420 70v16m0 14v17"/>
        {islanded && <path className="gm-fault" d="m427 81-14 14m0-14 14 14"/>}
        {points.map(({ x, name, kind }) => {
          const feeder = `M420 116H${x}v50`;
          return <g key={name}>
            <path className="gm-feeder" d={feeder}/>
            <path className="gm-feeder-pulse" d={feeder}/>
            <circle className="gm-district-connector" cx={x} cy="166" r="3"/>
            <g className="gm-district-box" transform={`translate(${x - 69} 174)`}>
              <rect width="138" height="78" rx="8"/>
              <g transform="translate(57 13)"><Symbol name={kind} size={24}/></g>
              <text x="69" y="57" textAnchor="middle">{name}</text>
            </g>
          </g>;
        })}
        {trading && <g className="gm-p2p-connections">
          <path className="gm-p2p-line" d="M146 211h84" markerEnd="url(#gm-market-arrow)"/>
          <path className="gm-p2p-line" d="M694 211h-84" markerEnd="url(#gm-market-arrow)"/>
          <text className="gm-trade-caption" x="188" y="198" textAnchor="middle">P2P</text>
          <text className="gm-trade-caption" x="652" y="198" textAnchor="middle">P2P</text>
        </g>}
      </svg>
    </div>
    <p className="gm-network-swipe">Scroll the diagram to see all four districts.</p>
    <div className="gm-network-footer">
      <div className="gm-network-state" aria-live="polite"><span className={`gm-market-dot ${islanded ? "alert" : ""}`}/><strong>{statusLabel}</strong><span>{detail}</span></div>
      <small>{live ? "Illustrative topology · linked to selected run status" : "Illustrative topology · not a live reading"}</small>
    </div>
  </section>;
}

function Panel({ title: heading, summary, children }) {
  return <section className="gm-market-panel"><div className="gm-market-panel-header"><div><h3>{heading}</h3>{summary && <p>{summary}</p>}</div></div>{children}</section>;
}

function MarketTable({ headers, rows, empty }) {
  if (!rows.length) return <p className="gm-market-empty">{empty}</p>;
  return <div className="gm-market-scroll" tabIndex={0} aria-label={`${headers[0]} data table`}><table><thead><tr>{headers.map(head => <th key={head} scope="col">{head}</th>)}</tr></thead>
    <tbody>{rows.map(([key, cells]) => <tr key={key}>{cells.map((cell, index) => <td key={index}>{cell}</td>)}</tr>)}</tbody></table></div>;
}

function Pill({ tone, children }) {
  return <span className={`gm-market-pill ${tone ?? ""}`}>{children}</span>;
}

export default function Market({ runtime = {}, status, onAction, navigate }) {
  const { localities = [], localityId, participants = [], runs = [], run, orders = [], trades = [] } = runtime;
  const selectedLocality = localities.find((row) => same(row.id, localityId));
  const step = runtime.step ?? run?.latest;
  const grid = step?.grid ?? {};
  const market = step?.market ?? {};
  const totals = step?.totals ?? {};
  const running = ACTIVE.has(run?.status);
  const acceptsOrder = ["queued", "running"].includes(run?.status);
  const stale = status?.connected === false && !status?.loading;
  const act = (...args) => Promise.resolve().then(() => onAction?.(...args)).catch(() => { /* App reports API errors. */ });
  const currentOrders = market.orders ?? [];
  const currentIds = new Set(currentOrders.map((row) => String(row.id)));
  const marketOrders = [...currentOrders, ...orders.filter((row) => row.manual && !currentIds.has(String(row.id))).slice(0, 25)];
  const settled = (trades.length ? trades : market.fills ?? []).slice().sort((left, right) => (right.step ?? 0) - (left.step ?? 0)).slice(0, 25);
  const names = new Map([...(run?.participants ?? []), ...participants].map((person) => [String(person.id), person.name]));
  const rowName = (row) => row.participant_name ?? names.get(String(row.participant_id)) ?? row.participant_id ?? "—";
  const registry = localities.map((item, index) => {
    const activeRun = runs.find((entry) => same(entry.locality_id, item.id) && ACTIVE.has(entry.status));
    return <button type="button" className="gm-market-locality" key={item.id} aria-pressed={same(item.id, localityId)}
      onClick={() => act("locality", item.id)}>
      <span className="gm-locality-symbol"><Symbol name={points[index % points.length].kind}/></span>
      <span><strong>{item.name}</strong><small>{activeRun ? title(activeRun.status) : item.island_capable ? "Island-capable" : "Grid-dependent"} · {amount(item.import_limit_kw, "kW")}</small></span>
    </button>;
  });

  return <div className="gm-market-view">
    <header className="gm-market-page-header">
      <div><span className="gm-market-kicker">OPERATIONS / MARKET</span><h1>Live P2P Market</h1><p>Monitor local energy supply, requests and settled trades.</p></div>
    </header>
    <Diagram grid={grid} step={step} isReference={run?.scenario === "reference"}/>
    <section className="gm-market-live" aria-labelledby="gm-live-title">
      <header className="gm-live-heading"><div><span className="gm-market-kicker">Market data</span><h2 id="gm-live-title">Trading desk</h2><p>{selectedLocality?.name ?? "No locality selected"}{run ? ` · ${title(run.scenario)}` : ""}</p></div>
        <div className="gm-market-actions">
          {running ? <button className="gm-market-button" type="button" onClick={() => act("stop")} disabled={stale || run.status === "stopping"}>{run.status === "stopping" ? "Stopping…" : "Stop run"}</button>
            : <button className="gm-market-button primary" type="button" onClick={() => selectedLocality ? act("start") : navigate?.("registry")}>{selectedLocality ? "Start simulation" : "Set up locality"}</button>}
          <button className={`gm-market-button ${running ? "primary" : ""}`} type="button" onClick={() => act("order")} disabled={stale || !acceptsOrder || !(run?.participants ?? participants).length}>Place order</button>
        </div>
      </header>
      <div className="gm-market-tools">
        <label>Locality<select value={localityId ?? ""} onChange={(event) => act("locality", event.target.value)} disabled={!localities.length}>
          {!localities.length && <option value="">No localities</option>}
          {localities.map((place) => <option value={place.id} key={place.id}>{place.name}</option>)}
        </select></label>
        <label>Run<select value={run?.id ?? ""} onChange={(event) => act("run", event.target.value)}>
          <option value="" disabled>No run selected</option>
          {runs.filter((entry) => same(entry.locality_id, localityId)).map((entry) => <option value={entry.id} key={entry.id}>
            {title(entry.scenario)} · {title(entry.status)} · {entry.id.slice(0, 8)}
          </option>)}
        </select></label>
        <div className="gm-market-runstate"><Pill tone={running && market.open ? "good" : ""}>{!run ? "No run selected" : running ? (market.open ? "Trading open" : "Trading closed") : `Saved · ${title(run.status)}`}</Pill>
          {run && <small>{run.completed_steps ?? 0} / {run.steps} intervals</small>}</div>
      </div>
      {stale && run && <p role="status" className="gm-market-empty">Server unavailable. Any results shown are from the last successful refresh.</p>}
      {run?.error && <p role="alert" className="gm-market-empty">{run.error}</p>}
      <dl className="gm-market-metrics">
        {[["Import headroom", amount(grid.headroom_kw, "kW")], ["Essential load served", amount(totals.essential_served_kwh, "kWh")],
          ["Energy traded", amount(totals.traded_kwh, "kWh")], ["Unmet demand", amount(totals.unserved_kwh, "kWh")]].map(([key, value]) =>
          <div key={key}><dt>{key}</dt><dd>{value}</dd></div>)}
      </dl>
      <Panel title="Order book" summary="Latest requests, offers and staged manual orders">
        <MarketTable headers={["Participant", "Side", "Interval", "Priority", "Urgency", "Quantity", "Limit credits / kWh", "Filled", "Status", "Manage"]}
          rows={marketOrders.map((order, index) => {
            const submitted = orders.find((entry) => same(entry.id, order.id) && entry.manual);
            const canCancel = !stale && acceptsOrder && submitted?.status === "queued";
            return [order.id ?? index, [rowName(order), <Pill tone={order.side === "buy" ? "request" : "good"}>{order.side === "buy" ? "Request" : "Offer"}</Pill>,
              order.step != null || step?.step != null ? Number(order.step ?? step.step) + 1 : "—", title(order.priority), order.side === "buy" ? title(order.urgency) : "—",
              order.quantity_kwh != null ? amount(order.quantity_kwh, "kWh") : amount(order.power_kw, "kW"), amount(order.price),
              amount(order.filled_kwh, "kWh"), title(order.status),
              canCancel ? <button className="gm-market-button small" type="button" onClick={() => act("cancelOrder", order.id)}>Cancel</button> : "—"]];
          })} empty="Orders appear here after a simulation interval or when a manual order is placed."/>
      </Panel>
      <div className="gm-market-half">
        <Panel title="Available sellers" summary="Supply in the selected interval">
          <MarketTable headers={["Seller", "Available", "Offered", "Remaining"]}
            rows={(market.sellers ?? []).map((seller, index) => [seller.id ?? seller.participant_id ?? index, [rowName(seller), amount(seller.available_kwh, "kWh"), amount(seller.offered_kwh, "kWh"), amount(seller.remaining_kwh, "kWh")]])}
            empty="No local seller supply in this interval."/>
        </Panel>
        <Panel title="Latest trades" summary="Most recently settled energy transfers">
          <MarketTable headers={["Buyer", "Seller", "Energy", "Credits / kWh"]}
            rows={settled.map((trade, index) => [trade.id ?? index, [
              trade.buyer_name ?? names.get(String(trade.buyer_id)) ?? "—",
              trade.seller_name ?? names.get(String(trade.seller_id)) ?? "—", amount(trade.quantity_kwh, "kWh"), amount(trade.price ?? trade.price_per_kwh)]])}
            empty="Settled trades will appear here."/>
        </Panel>
      </div>
    </section>
    <section className="gm-market-panel gm-market-registry" aria-labelledby="gm-registry-title">
      <div className="gm-market-panel-header"><div><span className="gm-market-kicker">Registry</span><h3 id="gm-registry-title">Market localities</h3><p>Select a locality to switch the trading desk.</p></div>
        <button className="gm-market-button" type="button" onClick={() => navigate?.("registry")}>Open registry</button></div>
      {registry.length ? <div className="gm-market-localities">{registry}</div> : <p className="gm-market-empty">Add a locality and register participants to run a simulation.</p>}
    </section>
  </div>;
}
