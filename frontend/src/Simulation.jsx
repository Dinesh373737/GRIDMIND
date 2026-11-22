import React, {useEffect, useRef, useState} from 'react';
import {Field, Records, RecordDialog, format} from './Registry';
import './registry.css';

const scenarios = {
  historical: ['Historical replay','Replay an archived upstream outage.'],
  scheduled: ['Scheduled outage','Choose when and how long the feeder disconnects.'],
  stress: ['Feeder stress','Estimate disconnection under sustained import shortfalls.'],
  reference: ['Reference run','Run a matching scenario without P2P allocation.'],
};
const active = run => ['pending','queued','starting','running','stopping'].includes(run?.status);
const nameStatus = s => String(s || 'ready').replaceAll('_',' ').replace(/^./,x=>x.toUpperCase());
const dates = new Intl.DateTimeFormat(undefined,{dateStyle:'medium',timeStyle:'short',timeZone:'UTC'});
const utc = value => value && Number.isFinite(new Date(value).valueOf()) ? dates.format(new Date(value))+' UTC' : '—';

function RunEditor({place,pack,participants,onClose,onAction}) {
  const [scenario,setScenario]=useState('historical'), [reference,setReference]=useState('historical');
  const gridScenario=scenario==='reference' ? reference : scenario;
  const cap=pack?.steps ?? 288;
  return <RecordDialog title="Configure simulation" subtitle={`${place.name} · ${pack?.name ?? 'Replay dataset'}${pack?.interval_minutes ? ' · '+pack.interval_minutes+' min intervals' : ''}`}
    submitLabel="Run simulation" onClose={onClose} onSubmit={async data=>{
      const body={locality_id:place.id,scenario,steps:Number(data.get('steps')),start_offset:Number(data.get('start_offset'))-1,
        speed:Number(data.get('speed')),seed:Number(data.get('seed'))};
      if (body.start_offset+body.steps>cap) throw new Error('The replay extends beyond the selected dataset.');
      if (scenario==='reference') body.reference_scenario=reference;
      if (gridScenario==='stress' && data.has('augment_weather')) body.augment_weather=true;
      if (gridScenario==='scheduled') {
        body.outage_duration=Number(data.get('outage_duration'));
        if(data.get('outage_step')!=='') {
          body.outage_step=Number(data.get('outage_step'))-1;
          if(body.outage_step>=body.steps) throw new Error('The scheduled outage must start inside the replay window.');
        }
      }
      const ids=data.getAll('join_participant_ids');
      if(ids.length>3) throw new Error('Choose at most three participants.');
      if(ids.length) {
        const step=Number(data.get('join_step'));
        if(step<2 || step>body.steps) throw new Error('Choose a join step between 2 and the end of the replay.');
        body.join_step=step-1;
        body.join_participant_ids=ids;
      }
      await onAction('createRun',body);
    }}>
    <Field label="Scenario" full><select value={scenario} onChange={e=>setScenario(e.target.value)} autoFocus>{Object.entries(scenarios).map(([id,[label]])=><option value={id} key={id}>{label}</option>)}</select></Field>
    <p className="gm-record-form-hint full">{scenarios[scenario][1]}</p>
    {scenario==='reference' && <Field label="Reference grid scenario" full><select value={reference} onChange={e=>setReference(e.target.value)}>{Object.entries(scenarios).filter(([id])=>id!=='reference').map(([id,[label]])=><option value={id} key={id}>{label}</option>)}</select></Field>}
    <Field label="Replay steps" name="steps" type="number" min="1" max={Math.min(cap,672)} defaultValue={Math.min(cap,672)} required/>
    <Field label="First dataset step" name="start_offset" type="number" min="1" max={cap} defaultValue="1" required/>
    <Field label="Steps per second" name="speed" type="number" min=".1" max="20" defaultValue="1" step="any" required/>
    <Field label="Simulation seed" name="seed" type="number" min="0" max="2147483647" defaultValue="42" required/>
    {gridScenario==='scheduled' && <>
      <Field label="Outage starts at step (optional)" name="outage_step" type="number" min="1" max="672"/>
      <Field label="Outage duration (steps)" name="outage_duration" type="number" min="1" max="96" defaultValue="4" required/>
    </>}
    {gridScenario==='stress' && <label className="gm-record-check full"><input name="augment_weather" type="checkbox"/> Include weather variability</label>}
    <details className="gm-run-join full">
      <summary>Schedule up to three participants to join</summary>
      <fieldset><legend>Participants</legend>{participants.map(p=><label key={p.id} className="gm-record-check"><input name="join_participant_ids" type="checkbox" value={p.id}/>{p.name}</label>)}</fieldset>
      <Field label="Join at replay step" name="join_step" type="number" min="2" max="672"/>
    </details>
  </RecordDialog>;
}

function Metric({title,value,detail}) {
  return <div className="gm-run-metric"><span>{title}</span><strong>{value}</strong><small>{detail}</small></div>;
}

function RiskChart({events,step}) {
  const frames=events instanceof Map ? [...events.values()] : Array.isArray(events) ? events : [];
  const byStep=new Map(frames.map(frame=>[Number(frame?.step),frame]));
  if(step) byStep.set(Number(step.step),step);
  const values=[...byStep].filter(([id,f])=>Number.isFinite(id) && f?.grid?.risk_next_hour!=null && Number.isFinite(Number(f.grid.risk_next_hour))).sort(([a],[b])=>a-b).slice(-96);
  if(!values.length) return <p className="gm-record-empty">Risk history appears when the simulation calculates an interval.</p>;
  const first=values[0][0], last=values.at(-1)[0], span=last-first;
  const points=values.map(([i,f])=>[40+(span ? (i-first)*550/span:275), 125-106*Math.max(0,Math.min(1,Number(f.grid.risk_next_hour)))]);
  const [x,y]=points.at(-1);
  return <svg className="gm-run-chart" viewBox="0 0 610 154" role="img" aria-label={`Next-hour risk by simulation interval. Latest: ${format(Number(values.at(-1)[1].grid.risk_next_hour)*100,'%')}`}>
    {[19,72,125].map((pos,i)=><g key={pos}><line x1="40" x2="590" y1={pos} y2={pos}/><text x="33" y={pos+4} textAnchor="end">{100-50*i}%</text></g>)}
    {points.length>1 && <><polygon fill="var(--gm-r-accent-soft)" points={`${points[0][0]},125 ${points.map(p=>p.join(',')).join(' ')} ${x},125`}/><polyline points={points.map(p=>p.join(',')).join(' ')}/></>}
    <circle cx={x} cy={y} r="4"/><text x="40" y="145">Step {first+1}</text>{span>0 && <text x="590" y="145" textAnchor="end">Step {last+1}</text>}
  </svg>;
}

export default function Simulation({runtime={},onAction,navigate,startSignal,onStartSignalConsumed}) {
  const {localities=[],localityId,participants=[],packs=[],runs=[],run,step,events}=runtime;
  const locality=localities.find(p=>String(p.id)===String(localityId));
  const ongoing=runs.find(item=>String(item.locality_id)===String(localityId) && active(item));
  const pack=packs.find(p=>p.id===locality?.replay_pack);
  const [editor,setEditor]=useState(null);
  const seenSignal=useRef(null);
  const canLaunch=Boolean(locality && participants.length && !ongoing && !active(run));
  useEffect(()=>{
    if (!startSignal) { seenSignal.current=null; return; }
    if (seenSignal.current===startSignal) return;
    seenSignal.current=startSignal;
    if (locality && participants.length && !ongoing && !active(run)) setEditor('start');
    onStartSignalConsumed?.();
  },[startSignal,locality,participants.length,ongoing,run,onStartSignalConsumed]);
  const grid=step?.grid ?? {}, totals=step?.totals ?? {};
  const island=grid.status==='islanded';
  const connected=Boolean(grid.status) && !island;
  const percent=run?.steps ? Math.min(100,100*(Number(run?.completed_steps)||0)/run.steps) : 0;
  const openRun = id => Promise.resolve(onAction('run',id)).catch(()=>{});
  return <div className="gm-record-page gm-sim-page">
    <section className="gm-record-intro">
      <div><span className="gm-record-kicker">Operations / Replay control</span><h1>Simulations</h1><p>Run historical energy replays, test disconnection scenarios, and inspect dispatch results.</p></div>
      <div className="gm-record-actions">
        {active(run) ? <button type="button" className="gm-record-button danger" disabled={run.status==='stopping'} onClick={()=>setEditor('stop')}>{run.status==='stopping'?'Stopping…':'Stop simulation'}</button> : ongoing ? <button type="button" className="gm-record-button primary" onClick={()=>openRun(ongoing.id)}>Open active run</button> :
          <button type="button" className="gm-record-button primary" onClick={()=>canLaunch?setEditor('start'):navigate?.('registry')}>{canLaunch?'▶ Start simulation':'Open network registry'}</button>}
      </div>
    </section>

    <section className="gm-record-card gm-run-overview" aria-labelledby="gm-run-overview-title">
      <div className="gm-record-card-head"><div><span className="gm-record-kicker">Selected replay</span><h2 id="gm-run-overview-title">{run?.locality_name || locality?.name || 'No locality selected'}</h2><p>{run ? scenarios[run.scenario]?.[0] ?? nameStatus(run.scenario) : 'Choose a locality and start a simulation.'}</p></div>
      <span className={`gm-record-chip ${island?'warn':connected?'good':''}`}>{island?'Islanded':connected?'Grid connected':nameStatus(run?.status)}</span></div>
      {run && <div className="gm-run-progress"><div><span>{nameStatus(run.status)} · {run.completed_steps ?? 0}/{run.steps} intervals</span><span>{utc(step?.timestamp)}</span></div><progress max="100" value={percent} aria-label="Replay progress"/></div>}
      {run?.error && <p className="gm-record-error" role="status">{run.error}</p>}
      <div className="gm-run-metrics">
        <Metric title="Next-hour risk" value={grid.risk_next_hour==null?'—':format(Number(grid.risk_next_hour)*100,'%')} detail="Modeled import shortfall"/>
        <Metric title="Import headroom" value={format(grid.headroom_kw,'kW')} detail="Current interval"/>
        <Metric title="Essential load served" value={format(totals.essential_served_kwh,'kWh')} detail="Current interval"/>
        <Metric title="Energy traded" value={format(totals.traded_kwh,'kWh')} detail="Current interval"/>
      </div>
    </section>

    <div className="gm-run-split">
      <section className="gm-record-card">
        <div className="gm-record-card-head"><div><h2>Feeder risk</h2><p>Probability of exceeding the modeled import limit.</p></div></div>
        <RiskChart events={events} step={step}/>
      </section>
      <section className="gm-record-card">
        <div className="gm-record-card-head"><div><h2>Dispatch summary</h2><p>Supply and demand for the latest interval.</p></div></div>
        {step ? <dl className="gm-run-dispatch">{[['Local generation','generation_kwh'],['Battery discharge','battery_discharge_kwh'],['Grid imports','grid_import_kwh'],['Unmet demand','unserved_kwh']].map(([title,key])=><div key={key}><dt>{title}</dt><dd>{format(totals[key],'kWh')}</dd></div>)}</dl> :
        <p className="gm-record-empty">Dispatch data appears after the first interval.</p>}
      </section>
    </div>
    <section className="gm-record-card" aria-labelledby="gm-history-title">
      <div className="gm-record-card-head"><div><h2 id="gm-history-title">Run history <span className="gm-record-count">{runs.length}</span></h2><p>Open current and past simulations across localities.</p></div></div>
      <Records headers={['Simulation','Status','Progress','Worker PID','Started','Run']} empty="No saved simulations." rows={runs.map(record=>({id:record.id,cells:[
        <span className="gm-run-title"><strong>{record.locality_name || localities.find(l=>l.id===record.locality_id)?.name || 'Locality'}</strong><small>{scenarios[record.scenario]?.[0] ?? nameStatus(record.scenario)}</small></span>,
        <span className={`gm-record-chip ${record.status==='failed'?'warn':active(record)?'good':''}`}>{nameStatus(record.status)}</span>,
        `${record.completed_steps ?? 0} / ${record.steps}`,record.worker_pid ?? '—',utc(record.started_at ?? record.created_at),
        <button type="button" className="gm-record-button small" aria-pressed={record.id===run?.id} onClick={()=>openRun(record.id)}>{record.id===run?.id?'Selected':'Open'}</button>,
      ]}))}/>
    </section>
    {editor==='start' && locality && <RunEditor place={locality} pack={pack} participants={participants} onAction={onAction} onClose={()=>setEditor(null)}/>}
    {editor==='stop' && run && <RecordDialog title="Stop this simulation?" submitLabel="Stop simulation" danger onSubmit={()=>onAction('stop')} onClose={()=>setEditor(null)}>
      <p className="full">Completed intervals will remain available in run history. The remaining intervals won’t be replayed.</p>
    </RecordDialog>}
  </div>;
}
