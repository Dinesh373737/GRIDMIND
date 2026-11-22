import React, { useEffect, useState } from 'react';
import './insights.css';

const number = new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 });
const clock = new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', timeZone: 'UTC' });
const qty = (n, unit = '') => n == null || !Number.isFinite(Number(n)) ? '—' : `${number.format(Number(n))}${unit ? ' ' + unit : ''}`;
const date = value => value && Number.isFinite(new Date(value).getTime()) ? `${clock.format(new Date(value))} UTC` : '—';
const label = value => String(value ?? '').replaceAll('_', ' ').replace(/^./, c => c.toUpperCase());
const val = value => typeof value === 'object' && value !== null ? JSON.stringify(value) : typeof value === 'boolean' ? (value ? 'Yes' : 'No') : value ?? '—';
const runTitle = run => `${run.locality_name ?? 'Locality'} · ${label(run.scenario)}`;
const latest = runtime => typeof runtime.step === 'object' && runtime.step ? runtime.step : runtime.run?.latest;
const hasOutputRange = ({ range_low_kw: low, range_high_kw: high }) =>
  Number.isFinite(low) && Number.isFinite(high) && low >= 0 && high >= low;
const formatOutputRange = row => hasOutputRange(row) ? `${qty(row.range_low_kw)}–${qty(row.range_high_kw, 'kW')}` : 'Not available';
const hasScenarioRange = row => row.range_kind === 'scenario';
const hasModelErrorMargin = ({ model_error_margin_kw: margin }) => Number.isFinite(margin) && margin >= 0;
const metrics = [
  ['served_kwh', 'Energy served'], ['unserved_kwh', 'Energy unserved'],
  ['essential_served_kwh', 'Essential energy served'], ['essential_unserved_kwh', 'Essential energy unserved'],
  ['traded_kwh', 'Energy traded'],
];

function Header({ section, title, children, detail }) {
  return <header className="ins-head"><div><p className="ins-kicker">{section}</p><h1>{title}</h1><p className="ins-desc">{detail}</p></div>{children}</header>;
}
function Panel({ title, detail, children, controls }) {
  return <section className="ins-panel"><header><div><h2>{title}</h2>{detail && <p>{detail}</p>}</div>{controls}</header>{children}</section>;
}
function Empty({ children }) {
  return <p className="ins-empty">{children}</p>;
}
function Tag({ children, tone }) {
  return <span className={`ins-tag${tone ? ' ins-tag-' + tone : ''}`}>{children}</span>;
}
function Facts({ rows, className = '' }) {
  return <dl className={`ins-facts${className ? ' ' + className : ''}`}>{rows.map(([k, v]) => <div key={k}><dt>{k}</dt><dd>{v}</dd></div>)}</dl>;
}
function Table({ headers, rows, empty }) {
  return rows.length ? <div className="ins-table-scroll" tabIndex={0} role="region" aria-label={headers.join(', ')}>
    <table className="ins-table"><thead><tr>{headers.map(x => <th key={x} scope="col">{x}</th>)}</tr></thead>
      <tbody>{rows.map((row, index) => <tr key={index}>{row.map((col, i) => <td key={i}>{col}</td>)}</tr>)}</tbody></table>
  </div> : <Empty>{empty}</Empty>;
}

function RenewableEstimates({ row }) {
  const target = typeof row.model_error_target === 'string' && row.model_error_target.trim();
  return <Facts className="ins-estimates" rows={[
    [hasScenarioRange(row) ? 'Scenario range' : 'Output range', formatOutputRange(row)],
    ['Model error margin', hasModelErrorMargin(row) ? <>
      ±{qty(row.model_error_margin_kw, 'kW')}
      <span className="ins-fact-note">{target ? `Compared with ${target}.` : 'Validation reference not available.'}</span>
    </> : 'Not available'],
  ]} />;
}

export function Renewables({ runtime }) {
  const [owner, setOwner] = useState('');
  const { run, participants = [] } = runtime;
  const sample = latest(runtime);
  const owners = (run?.participants ?? participants).filter(row => Number(row.solar_kw) > 0 || Number(row.wind_kw) > 0);
  const chosen = owners.some(x => x.id === owner) ? owner : '';
  const outlook = (sample?.renewables ?? []).filter(row => !chosen || row.participant_id === chosen);
  return <div className="ins-view">
    <Header section="Forecasting" title="Renewable outlook" detail="Archived weather estimates for the selected simulation interval.">
      <label className="ins-select">Participant<select value={chosen} onChange={e => setOwner(e.target.value)}>
        <option value="">All renewable owners</option>{owners.map(row => <option key={row.id} value={row.id}>{row.name}</option>)}
      </select></label>
    </Header>
    {sample && <div className="ins-context"><span>INTERVAL {Number(sample.step) + 1}</span><span>{date(sample.timestamp)}</span><span>{run?.locality_name}</span></div>}
    {outlook.some(row => !hasScenarioRange(row) && !hasModelErrorMargin(row) && !hasOutputRange(row)) && <p className="ins-desc">
      A range is unavailable when this replay doesn’t provide uncertainty bands for all of a participant’s renewable sources. The solar and wind forecasts shown are still model estimates.
    </p>}
    {!outlook.length ? <Panel title="Energy outlook"><Empty>{!sample ? 'Start or select a simulation to see renewable estimates.' : !owners.length ? 'Register a solar or wind asset to see a participant outlook.' : 'No forecast is available for this participant in the selected interval.'}</Empty></Panel> :
      <div className="ins-cards">{outlook.map(row => {
        const name = row.participant_name ?? owners.find(x => x.id === row.participant_id)?.name ?? 'Participant';
        const short = Number(row.shortfall_kw) > 0;
        const showEstimates = hasScenarioRange(row) || hasModelErrorMargin(row);
        return <Panel key={row.participant_id} title={name} detail={`Valid ${date(row.valid_at ?? sample.timestamp)}`} controls={<Tag tone={short ? 'amber' : 'green'}>{label(row.condition) || 'Archived forecast'}</Tag>}>
          <div className="ins-power">
            <div><span>Solar forecast</span><strong>{qty(row.forecast_solar_kw, 'kW')}</strong></div>
            <div><span>Wind forecast</span><strong>{qty(row.forecast_wind_kw, 'kW')}</strong></div>
          </div>
          <Facts rows={[
            ['Expected load', qty(row.expected_load_kw, 'kW')],
            ...(!showEstimates ? [['Output range', formatOutputRange(row)]] : []),
            ['Possible shortfall', qty(row.shortfall_kw, 'kW')],
          ]} />
          {showEstimates && <RenewableEstimates row={row} />}
          {row.advisory && <p className="ins-advice">{row.advisory}</p>}
        </Panel>;
      })}</div>}
  </div>;
}

function eventRows(events) {
  const rows = events instanceof Map ? Array.from(events.values()) : Array.isArray(events) ? events : Object.values(events ?? {});
  return rows.filter(row => row && Number.isFinite(Number(row.step))).sort((a, b) => Number(a.step) - Number(b.step));
}
export function Activity({ runtime, status, onAction, navigate }) {
  const { run, preview, events } = runtime;
  const [selection, setSelection] = useState('');
  const rows = eventRows(events);
  const selected = rows.find(row => String(row.step) === selection);
  const sample = preview ?? selected ?? latest(runtime);
  const trace = sample?.trace ?? [];
  useEffect(() => { setSelection(''); }, [run?.id]);
  return <div className="ins-view">
    <Header section="Operations" title="Agent activity" detail="Read the decision trace from a saved simulation, or preview the next interval.">
      <button className="ins-btn" onClick={() => navigate('demo')}>Open agent demo</button>
    </Header>
    <Panel title={preview ? 'Next interval preview' : sample ? `Interval ${Number(sample.step) + 1}` : 'Decision trace'} detail={sample ? date(sample.timestamp) : undefined}
      controls={<div className="ins-controls">
        <label className="ins-select">Interval<select value={selected ? selection : ''} onChange={e => { onAction('clearPreview'); setSelection(e.target.value); }}>
          <option value="">Latest</option>{rows.map(row => <option key={row.step} value={row.step}>{Number(row.step) + 1}</option>)}
        </select></label>
        <button className="ins-btn" disabled={!preview && (!run || Number(run.completed_steps) >= Number(run.steps) || status?.busy === 'preview')} onClick={() => onAction(preview ? 'clearPreview' : 'preview')}>{preview ? 'Close preview' : 'Preview next'}</button>
      </div>}>
      {preview && <p className="ins-advice">Preview only. It does not settle trades or save a new interval.</p>}
      {!trace.length ? <Empty>The coordination trace appears after the first completed interval.</Empty> : <ol className="ins-trace">
        {trace.map((item, i) => <li key={i}>
          <span className="ins-seq" aria-hidden="true">{String(i + 1).padStart(2, '0')}</span><div>
            <div className="ins-trace-title"><h3>{item.agent ?? 'Agent'}</h3>{item.status && <Tag tone={/error|failed|blocked/i.test(item.status) ? 'amber' : 'green'}>{label(item.status)}</Tag>}</div>
            {(item.action || item.reason) && <p>{[label(item.action), item.reason].filter(Boolean).join(' · ')}</p>}
            {typeof item.summary === 'string' ? (item.summary && <p className="ins-trace-value">{item.summary}</p>) :
              item.summary && Object.keys(item.summary).length > 0 && <Facts rows={Object.entries(item.summary).map(([key, value]) => [label(key), val(value)])} />}
          </div>
        </li>)}
      </ol>}
    </Panel>
  </div>;
}

function Comparison({ run, runs, comparison, onAction }) {
  const [reference, setReference] = useState('');
  useEffect(() => { setReference(''); }, [run?.id]);
  const references = run?.status === 'completed' && run.scenario !== 'reference' ? runs.filter(item =>
    item.status === 'completed' && item.scenario === 'reference' && item.locality_id === run.locality_id
  ) : [];
  const result = reference && comparison?.runs?.some(x => x.id === reference) && comparison.runs.some(x => x.id === run?.id) ? comparison : null;
  return <Panel title="Paired comparison" detail="Compare against an equivalent run without P2P trading."
    controls={references.length > 0 && <label className="ins-select">Reference run<select value={reference} onChange={e => {
      setReference(e.target.value);
      if (e.target.value) onAction('compare', { run_id: run.id, reference_id: e.target.value });
    }}><option value="">Choose a reference</option>{references.map(x => <option key={x.id} value={x.id}>{date(x.created_at)} · {x.id.slice(0, 8)}</option>)}</select></label>}>
    {result ? <><div className="ins-comparison-status"><Tag tone={result.comparable ? 'green' : 'amber'}>{result.comparable ? 'Matched replay settings' : 'Replay settings differ'}</Tag></div>
      <Table headers={['Measure', ...result.runs.map(x => x.id === run.id ? 'Selected run' : 'Reference run')]} rows={metrics.map(([key, title]) =>
        [title, ...result.runs.map(x => qty(x.totals?.[key], 'kWh'))])} empty="No comparison totals available." /></> :
      <Empty>{references.length ? (reference ? 'Waiting for paired totals.' : 'Choose a reference run above.') : 'Open a completed run and finish a reference run in the same locality to compare.'}</Empty>}
  </Panel>;
}

export function Reports({ runtime, status, onAction }) {
  const { run, runs = [], health = {}, comparison, benchmark } = runtime;
  const reports = runtime.reports ?? runtime.report;
  const data = reports && (!('run_id' in reports) || (reports.run_id ?? null) === (run?.id ?? null)) ? reports : null;
  const totals = data?.totals;
  return <div className="ins-view">
    <Header section="Data & settlement" title="Database & reports" detail="Inspect persisted runs, settlement totals, and the database audit trail.">
      <button className="ins-btn" onClick={() => onAction('reports')}>Refresh reports</button>
    </Header>
    <div className="ins-context">
      <span>REPORT SCOPE</span><label className="ins-select ins-scope">Simulation<select value={run?.id ?? ''} onChange={e => onAction('run', e.target.value)}>
        <option value="">All simulations</option>{run && !runs.some(x => x.id === run.id) && <option value={run.id}>{runTitle(run)}</option>}
        {runs.map(x => <option key={x.id} value={x.id}>{runTitle(x)} · {date(x.created_at)}</option>)}
      </select></label>
    </div>
    <div className="ins-summary">
      <Panel title="Storage status" detail={run ? runTitle(run) : 'All simulations'}>
        <Facts rows={[
          ['SQL backend', data?.health?.sql ?? health.sql ?? '—'],
          ['Event store', data?.health?.event_store ?? health.event_store ?? '—'],
          ['Published steps', data?.published_steps ?? run?.published_steps ?? '—'],
        ]} />
      </Panel>
      <Panel title="Energy & settlement" detail="Totals for the current scope.">
        {totals ? <Facts rows={[
          ['Steps', qty(totals.step_count)], ...metrics.map(([key, title]) => [title, qty(totals[key], 'kWh')]),
          ['Settled trades', qty(data.trade_count)], ['Settled energy', qty(data.settled_kwh, 'kWh')], ['Settlement value', qty(data.settlement_value)],
        ]} /> : <Empty>{status?.busy === 'reports' ? 'Loading reports…' : 'No results loaded. Refresh to request the latest database totals.'}</Empty>}
      </Panel>
    </div>
    <Comparison run={run} runs={runs} comparison={comparison} onAction={onAction} />
    {data?.counts && <Panel title="Relational records" detail="Record counts across the database (all simulations)."><Facts rows={Object.entries(data.counts).map(([key, value]) => [label(key), qty(value)])} /></Panel>}
    {benchmark?.recorded_trades && <Panel title="µGIM market benchmark" detail={`${qty(benchmark.hourly_participant_records?.length)} hourly records · ${qty(benchmark.recorded_trades.length)} recorded trades`}>
      <Table headers={['Day', 'Buyer', 'Seller', 'Energy', 'EUR / kWh']} rows={benchmark.recorded_trades.slice(0, 8).map(row =>
        [row.day, row.buyer, row.seller, qty(row.quantity_kwh, 'kWh'), qty(row.price_eur_per_kwh)])} empty="No recorded benchmark trades." />
    </Panel>}
    {data?.datasets && <Panel title="Imported datasets">
      <Table headers={['Dataset', 'Identifier', 'Checksum']} rows={data.datasets.map(row => [row.name, row.id, <code>{row.digest}</code>])} empty="No replay dataset imported." />
    </Panel>}
    {data?.audit && <Panel title="Audit trail" detail="Latest database actions.">
      <Table headers={['Action', 'Record', 'Timestamp']} rows={data.audit.map(row => [label(row.action), <code>{row.subject_id}</code>, date(row.timestamp)])} empty="No audit events recorded." />
    </Panel>}
  </div>;
}
