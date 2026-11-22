import React,{useEffect,useRef,useState} from 'react';
import {createRoot} from 'react-dom/client';
import {useRuntime} from './runtime';
import {cloudAuth,explainRun,signIn,signOut} from './api';
import Market from './Market';
import Registry from './Registry';
import Simulation from './Simulation';
import AgentDemo from './AgentDemo';
import {Activity,Renewables,Reports} from './Insights';
import './theme.css';

const tabs=[
  ['market','Live P2P Market','network'],['simulation','Simulations','pulse'],['registry','Registry','layers'],
  ['activity','Agent Activity','list'],['renewables','Renewable Outlook','sun'],['database','Database & Reports','database'],
  ['demo','Agent Demo','spark'],
];
const icons={
  network:'M5 6h14M6 7l5 10m7-10-5 10 M4 3h3v4H4zm13 0h3v4h-3zM10 17h4v4h-4z',
  pulse:'M2 12h5l3-8 4 16 3-8h5',layers:'m12 3 9 5-9 5-9-5 9-5zM3 12l9 5 9-5M3 16l9 5 9-5',
  list:'M8 6h12M8 12h12M8 18h12M4 6h.1M4 12h.1M4 18h.1',
  sun:'M12 1v3m0 16v3M1 12h3m16 0h3M4 4l2 2m12 12 2 2M4 20l2-2m12-12 2-2M17 12a5 5 0 1 0-10 0 5 5 0 0 0 10 0Z',
  database:'M20 5c0 2-3.6 3.5-8 3.5S4 7 4 5s3.6-3.5 8-3.5S20 3 20 5v14c0 2-3.6 3.5-8 3.5S4 21 4 19V5m0 7c0 2 3.6 3.5 8 3.5s8-1.5 8-3.5',
  spark:'m13 2-9 12h7l-1 8 10-12h-7z',
  menu:'M3 6h18M3 12h18M3 18h18'
};
function Icon({name,size=18}){return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d={icons[name]}/></svg>;}
function pageFromLocation(){
  const aliases={participants:'registry',trace:'activity',agents:'activity',history:'simulation',reports:'database'};
  const raw=location.hash.slice(1);const route=aliases[raw]||raw;
  return tabs.some(tab=>tab[0]===route)?route:'market';
}
function OrderDialog({run,participants,onSubmit,onClose}){
  const ref=useRef(null),[side,setSide]=useState('buy'),[power,setPower]=useState(''),[price,setPrice]=useState('12'),[busy,setBusy]=useState(false),[error,setError]=useState('');
  const roster=run?.participants??participants;
  useEffect(()=>{ref.current?.showModal();},[]);
  async function submit(event){
    event.preventDefault();setBusy(true);setError('');
    const values=new FormData(event.currentTarget);
    const participant=roster.find(row=>row.id===values.get('participant_id'));
    const body={participant_id:participant.id,side,power_kw:Number(power),limit_price:Number(price),urgency:values.get('urgency')||'routine'};
    if(values.get('step'))body.step=Number(values.get('step'))-1;
    else if(participant.active_from_step>run.completed_steps)body.step=participant.active_from_step;
    try{await onSubmit(body);onClose();}catch(err){setError(err.message);}finally{setBusy(false);}
  }
  return <dialog ref={ref} className="gm-dialog" onCancel={onClose} aria-labelledby="order-title"><form onSubmit={submit}>
    <div className="gm-dialog-header"><div><p className="eyebrow">Emergency exchange</p><h2 id="order-title">New request or offer</h2></div><button type="button" className="gm-icon-button" onClick={onClose} aria-label="Close">×</button></div>
    <label>Participant<select name="participant_id" required>{roster.map(p=><option key={p.id} value={p.id}>{p.name}</option>)}</select></label>
    <div className="gm-form-grid"><label>Side<select value={side} onChange={event=>{const next=event.target.value;if(price===(side==='buy'?'12':'6'))setPrice(next==='buy'?'12':'6');setSide(next);}}><option value="buy">Request energy</option><option value="sell">Offer energy</option></select></label>
      <label>Urgency<select name="urgency" disabled={side==='sell'}><option value="routine">Routine</option><option value="urgent">Urgent</option></select></label>
      <label>Power (kW)<input required type="number" min=".004" max="100000" step="any" value={power} onChange={e=>setPower(e.target.value)}/></label>
      <label>Limit (credits/kWh)<input required type="number" min="0" max="100000" step="any" value={price} onChange={e=>setPrice(e.target.value)}/></label>
      <label>Replay step (optional)<input name="step" type="number" min={Math.min(run.steps,run.completed_steps+1)} max={run.steps}/></label>
    </div>
    <p className="gm-muted">{power?Number((power*(run.interval_minutes??15)/60).toFixed(3))+' kWh for this interval. ':'Each interval covers '+(run.interval_minutes??15)+' minutes. '}Leave the step blank to use the next eligible interval.</p>
    {error&&<p role="alert" className="gm-error">{error}</p>}
    <div className="gm-form-actions"><button type="button" className="gm-button" onClick={onClose}>Cancel</button><button className="gm-button primary" disabled={busy}>{busy?'Saving…':'Submit order'}</button></div>
  </form></dialog>;
}
function App(){
  const [page,setPage]=useState(pageFromLocation);
  const [runtime,status,perform]=useRuntime(page!=='demo');
  const [menu,setMenu]=useState(false),[order,setOrder]=useState(false),[startSignal,setStartSignal]=useState(0),[flash,setFlash]=useState(null),[key,setKey]=useState('');
  const ref=useRef(null);
  function navigate(next){location.hash=next;setPage(pageFromLocation());setMenu(false);}
  useEffect(()=>{const route=()=>{setPage(pageFromLocation());setMenu(false);};addEventListener('hashchange',route);return()=>removeEventListener('hashchange',route);},[]);
  useEffect(()=>{if(page==='database'&&status.connected)perform('reports').catch(error=>setFlash(error.message));},[page,status.connected,runtime.run?.id,runtime.localityId]);
  useEffect(()=>{if(flash){const id=setTimeout(()=>setFlash(null),5500);return()=>clearTimeout(id);}},[flash]);
  async function onAction(name,value){
    if(name==='start'){setStartSignal(1);navigate('simulation');return;}
    if(name==='order'){if(runtime.run)setOrder(true);return;}
    if(name==='run'&&page==='simulation')setOrder(false);
    try{return await perform(name,value);}catch(error){setFlash(error.message);throw error;}
  }
  const props={runtime,status,onAction,navigate};
  const authError=[401,403].includes(status.error?.status);
  const view={market:<Market {...props}/>,simulation:<Simulation {...props} requestExplanation={explainRun} startSignal={startSignal} onStartSignalConsumed={()=>setStartSignal(0)}/>,registry:<Registry {...props}/>,activity:<Activity {...props}/>,renewables:<Renewables {...props}/>,database:<Reports {...props}/>,demo:<AgentDemo/>};
  return <div className="gm-shell">
    <a className="gm-skip" href="#primary-content" onClick={event=>{event.preventDefault();ref.current?.focus();}}>Skip to content</a>
    <aside className={'gm-rail'+(menu?' opened':'')}>
      <a className="gm-brand" href="#market" aria-label="GRIDMIND home"><span className="gm-brand-mark"><Icon name="spark" size={22}/></span><span><strong>GRIDMIND</strong><small>GRID OPERATIONS</small></span></a>
      <button className="gm-menu gm-icon-button" type="button" aria-label="Toggle navigation" aria-expanded={menu} onClick={()=>setMenu(!menu)}><Icon name="menu"/></button>
      <nav aria-label="Workspace">
        <p>OPERATIONS</p>
        {tabs.slice(0,3).map(([id,title,icon])=><a key={id} href={'#'+id} aria-current={page===id?'page':undefined}><Icon name={icon}/>{title}</a>)}
        <p>INTELLIGENCE</p>
        {tabs.slice(3,6).map(([id,title,icon])=><a key={id} href={'#'+id} aria-current={page===id?'page':undefined}><Icon name={icon}/>{title}</a>)}
        <p>SANDBOX</p>
        <a href="#demo" aria-current={page==='demo'?'page':undefined}><Icon name="spark"/>Agent Demo <span className="gm-nav-tag">DEMO</span></a>
      </nav>
      <div className="gm-rail-footer"><span className="gm-rail-led"/><div><strong>Simulation workspace</strong><small>No real grid control</small></div></div>
    </aside>
    <div className="gm-workspace">
      <header className="gm-top"><div aria-label="Breadcrumb"><span>Workspace</span><span className="gm-slash">/</span><strong>{tabs.find(x=>x[0]===page)?.[1]}</strong></div><div className="gm-top-right"><span className={'gm-connection'+(status.connected?' online':'')}><i/>{page==='demo'?'Scripted demo':status.loading?'Connecting':status.connected?(cloudAuth?'AWS connected':'Local connected'):'Disconnected'}</span>{page!=='demo'&&cloudAuth&&status.connected&&<button className="gm-button small" type="button" onClick={signOut}>Sign out</button>}</div></header>
      <main className="gm-main" id="primary-content" tabIndex="-1" ref={ref}>
        {status.error&&page!=='demo'&&<section className="gm-connect" aria-label="Connection status"><div><strong>{authError?'Connect to GRIDMIND':'Connection unavailable'}</strong><p>{status.error.message}</p></div>
          {cloudAuth?<button className="gm-button primary" onClick={()=>signIn().catch(e=>setFlash(e.message))}>Sign in with Cognito</button>:authError?<form onSubmit={event=>{event.preventDefault();sessionStorage.setItem('gridmind-key',key.trim());setKey('');perform('retry').catch(()=>{});}}><label className="gm-sr" htmlFor="local-key">API key</label><input id="local-key" placeholder="Local API key" type="password" value={key} onChange={e=>setKey(e.target.value)} required/><button className="gm-button primary">Connect</button></form>:<button className="gm-button" onClick={()=>perform('retry').catch(()=>{})}>Retry connection</button>}
        </section>}
        {view[page]}
      </main>
    </div>
    {order&&runtime.run&&<OrderDialog run={runtime.run} participants={runtime.participants} onSubmit={body=>onAction('submitOrder',body)} onClose={()=>setOrder(false)}/>}
    {flash&&<div className="gm-toast" role="alert">{flash}<button className="gm-icon-button" aria-label="Dismiss message" onClick={()=>setFlash(null)}>×</button></div>}
  </div>;
}
createRoot(document.getElementById('root')).render(<App/>);
