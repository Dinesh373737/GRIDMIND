import React, {useEffect, useRef, useState} from 'react';
import './registry.css';

const ids = (a,b) => String(a) === String(b);
export const format = (value, unit = '') => value == null || value === '' || !Number.isFinite(Number(value)) ? '—' :
  `${Number(value).toLocaleString(undefined,{maximumFractionDigits:2})}${unit?' '+unit:''}`;
const facilityIcon = id => /clinic|hospital|health/i.test(id) ? '✚' : /solar|wind|producer/i.test(id) ? '☀' :
  /house|home|residential/i.test(id) ? '⌂' : /school|college/i.test(id) ? '⚑' : /industry/i.test(id) ? '⚙' : '▥';
const recordName = id => String(id ?? '').replaceAll('_',' ');

export function RecordDialog({title, subtitle, submitLabel='Save changes', danger=false, onSubmit, onClose, children}) {
  const dialog = useRef(null), busyRef = useRef(false);
  const [busy,setBusy] = useState(false), [error,setError] = useState('');
  useEffect(() => {
    const el = dialog.current;
    const previous = document.activeElement;
    el.showModal();
    return () => {
      el.close();
      if (previous instanceof HTMLElement && previous.isConnected) previous.focus();
    };
  },[]);
  async function submit(event) {
    event.preventDefault();
    if (busyRef.current) return;
    const form = new FormData(event.currentTarget);
    busyRef.current=true; setBusy(true); setError('');
    try {await onSubmit(form); onClose();}
    catch (e) {setError(e?.message || 'The operation could not be completed.');}
    finally {busyRef.current=false; setBusy(false);}
  }
  return <dialog className="gm-record-dialog" ref={dialog} aria-labelledby="gm-dialog-title" onCancel={e => {
    e.preventDefault(); if (!busyRef.current) onClose();
  }}>
    <form onSubmit={submit}>
      <header><div><h2 id="gm-dialog-title">{title}</h2>{subtitle && <p>{subtitle}</p>}</div>
        <button className="gm-record-icon" type="button" aria-label="Close dialog" onClick={onClose} disabled={busy}>×</button>
      </header>
      <div className="gm-record-fields">{children}</div>
      {error && <p className="gm-record-error" role="alert">{error}</p>}
      <footer><button type="button" className="gm-record-button" onClick={onClose} disabled={busy}>Cancel</button>
        <button className={`gm-record-button ${danger?'danger':'primary'}`} type="submit" disabled={busy}>{busy?'Working…':submitLabel}</button></footer>
    </form>
  </dialog>;
}

export function Field({label, full=false, children, ...props}) {
  return <label className={`gm-record-field ${full?'full':''}`}><span>{label}</span>{children || <input {...props}/>}</label>;
}

export function Records({headers, rows, empty}) {
  return rows.length ? <div className="gm-record-scroll"><table className="gm-record-table">
    <thead><tr>{headers.map(x=><th key={x} scope="col">{x}</th>)}</tr></thead>
    <tbody>{rows.map(({id,cells})=><tr key={id}>{cells.map((cell,i)=> i===0 ? <th scope="row" key={i}>{cell}</th>:<td key={i}>{cell}</td>)}</tr>)}</tbody>
  </table></div> : <p className="gm-record-empty">{empty}</p>;
}

const peopleFields = [
  ['base_load_kw','Base load (kW)',10,100000], ['essential_kw','Essential load (kW)',0,100000],
  ['solar_kw','Solar capacity (kW)',0,100000], ['wind_kw','Wind capacity (kW)',0,100000],
  ['battery_kw','Battery power (kW)',0,100000], ['battery_kwh','Battery energy (kWh)',0,1000000],
];

export default function Registry({runtime={},onAction}) {
  const {localities=[],localityId,participants=[],packs=[],facility_types:types=[]}=runtime;
  const locality=localities.find(place=>ids(place.id,localityId));
  const [editor,setEditor]=useState(null);
  const close=()=>setEditor(null);
  const open=(kind,row=null)=>setEditor({kind,row,place:locality?.id,placeName:locality?.name});
  const facility = person => person.facility_type ?? types.find(t=>ids(t.id,person.facility_type_id))?.name ?? recordName(person.facility_type_id);
  return <div className="gm-record-page">
    <section className="gm-record-intro">
      <div><span className="gm-record-kicker">Operations / Registry</span><h1>Network registry</h1><p>Manage the localities, participants and energy assets that form each simulation.</p></div>
      <button type="button" className="gm-record-button primary" disabled={!packs.length} onClick={()=>open('locality')}>+ Add locality</button>
    </section>
    <section className="gm-record-card" aria-labelledby="gm-localities-title">
      <div className="gm-record-card-head"><div><h2 id="gm-localities-title">Localities <span className="gm-record-count">{localities.length}</span></h2><p>Select a locality to view its network roster.</p></div></div>
      <Records headers={['Locality','Import limit','Grid mode','Replay dataset','Actions']} empty="No localities yet. Add one to get started." rows={localities.map(place=>({id:place.id,cells:[
        <span className="gm-record-name"><span className="gm-record-glyph" aria-hidden="true">⌘</span>{place.name}</span>,
        format(place.import_limit_kw,'kW'), <span className={`gm-record-chip ${place.island_capable?'good':''}`}>{place.island_capable?'Island capable':'Grid only'}</span>,
        packs.find(p=>ids(p.id,place.replay_pack))?.name || recordName(place.replay_pack),
        <span className="gm-record-actions">
          <button className="gm-record-button small" type="button" aria-pressed={ids(place.id,localityId)} onClick={()=>Promise.resolve(onAction('locality',place.id)).catch(()=>{})}>{ids(place.id,localityId)?'Selected':'Select'}</button>
          <button className="gm-record-button small" type="button" aria-label={`Edit ${place.name}`} onClick={()=>open('locality',place)}>Edit</button>
          <button className="gm-record-button small quiet" type="button" aria-label={`Archive ${place.name}`} onClick={()=>open('archiveLocality',place)}>Archive</button>
        </span>
      ]}))}/>
    </section>
    <section className="gm-record-card" aria-labelledby="gm-participants-title">
      <div className="gm-record-card-head"><div><h2 id="gm-participants-title">Participants <span className="gm-record-count">{participants.length}</span></h2><p>{locality?.name || 'Select a locality to view participants.'}</p></div>
        <button type="button" className="gm-record-button primary" disabled={!locality || !types.length} onClick={()=>open('participant')}>+ Add participant</button></div>
      <Records headers={['Participant','Facility','Base load','Essential load','Actions']} empty="No participants registered for this locality." rows={participants.map(person=>({id:person.id,cells:[
        <span className="gm-record-name"><span className="gm-record-glyph" aria-hidden="true">{facilityIcon(person.facility_type_id)}</span>{person.name}</span>,
        facility(person), format(person.base_load_kw,'kW'), format(person.essential_kw,'kW'),
        <span className="gm-record-actions"><button className="gm-record-button small" type="button" aria-label={`Edit ${person.name}`} onClick={()=>open('participant',person)}>Edit</button>
          <button className="gm-record-button small quiet" type="button" aria-label={`Archive ${person.name}`} onClick={()=>open('archiveParticipant',person)}>Archive</button></span>
      ]}))}/>
    </section>
    <section className="gm-record-card" aria-labelledby="gm-assets-title">
      <div className="gm-record-card-head"><div><h2 id="gm-assets-title">Energy assets</h2><p>Installed generation and available battery ratings.</p></div></div>
      <Records headers={['Participant','Solar','Wind','Battery power','Battery energy','Actions']} empty="Add a participant to configure energy assets." rows={participants.map(person=>({id:person.id,cells:[
        person.name,format(person.solar_kw,'kW'),format(person.wind_kw,'kW'),format(person.battery_kw,'kW'),format(person.battery_kwh,'kWh'),
        <button type="button" className="gm-record-button small" onClick={()=>open('participant',person)}>Edit assets</button>
      ]}))}/>
    </section>
    {editor?.kind==='locality' && <RecordDialog title={editor.row?'Edit locality':'Add locality'} subtitle="Set the feeder limit and replay source." onClose={close} onSubmit={async data=>{
      const body={name:String(data.get('name')).trim(),import_limit_kw:Number(data.get('import_limit_kw')),replay_pack:data.get('replay_pack'),island_capable:data.has('island_capable')};
      if (!body.name) throw new Error('Enter a locality name.');
      await onAction('saveLocality',{id:editor.row?.id,body});
    }}>
      <Field label="Locality name" name="name" defaultValue={editor.row?.name} required maxLength={100} full autoFocus/>
      <Field label="Import limit (kW)" name="import_limit_kw" type="number" min=".001" max="100000" step="any" defaultValue={editor.row?.import_limit_kw ?? 50} required/>
      <Field label="Replay dataset"><select name="replay_pack" defaultValue={editor.row?.replay_pack ?? packs[0]?.id} required>{packs.map(pack=><option key={pack.id} value={pack.id}>{pack.name}</option>)}</select></Field>
      <label className="gm-record-check full"><input type="checkbox" name="island_capable" defaultChecked={editor.row?.island_capable ?? true}/> Local supply can operate while disconnected</label>
    </RecordDialog>}
    {editor?.kind==='participant' && <RecordDialog title={editor.row?'Edit participant':'Add participant'} subtitle={editor.placeName} onClose={close} onSubmit={async data=>{
      const body={locality_id:editor.place,name:String(data.get('name')).trim(),facility_type_id:data.get('facility_type_id')};
      peopleFields.forEach(([name])=>{body[name]=Number(data.get(name));});
      if (!body.name) throw new Error('Enter a participant name.');
      if (body.essential_kw>body.base_load_kw) throw new Error('Essential load must fit within base load.');
      if (Boolean(body.battery_kwh)!==Boolean(body.battery_kw)) throw new Error('Set both battery ratings, or leave both at zero.');
      await onAction('saveParticipant',{id:editor.row?.id,body});
    }}>
      <Field label="Participant name" name="name" defaultValue={editor.row?.name} required maxLength={100} full autoFocus/>
      <Field label="Facility type" full><select name="facility_type_id" defaultValue={editor.row?.facility_type_id ?? types[0]?.id} required>{types.map(type=><option key={type.id} value={type.id}>{type.name}</option>)}</select></Field>
      {peopleFields.map(([name,label,fallback,max])=><Field key={name} label={label} name={name} type="number" min="0" max={max} step="any" defaultValue={editor.row?.[name] ?? fallback} required/>)}
    </RecordDialog>}
    {editor?.kind?.startsWith('archive') && <RecordDialog danger title={`Archive ${editor.row.name}?`} submitLabel="Archive record" onClose={close} onSubmit={()=>onAction(editor.kind,editor.row.id)}>
      <p className="full">This removes the entry from the active registry. Existing simulation records are retained.</p>
    </RecordDialog>}
  </div>;
}
