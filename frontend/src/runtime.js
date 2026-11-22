import {useEffect,useRef,useState} from 'react';
import {api,completeSignIn} from './api';
export const active = run => ['queued','starting','running','stopping','pending'].includes(run?.status);
const blank = {packs:[],facility_types:[],localities:[],participants:[],runs:[],localityId:'',run:null,events:[],orders:[],trades:[],preview:null,reports:null,comparison:null,benchmark:null};
const enc = encodeURIComponent;
const pathFor = id=>'/runs/'+enc(id);
const best = (runs,id)=>runs.find(run=>run.locality_id===id && active(run))||runs.find(run=>run.locality_id===id)||null;

export function useRuntime(enabled=true) {
  const [runtime,setRuntime]=useState(blank);
  const [status,setStatus]=useState({connected:false,loading:true});
  const snapshot=useRef(blank), version=useRef(0), polling=useRef(false);
  const update = patch => {
    snapshot.current={...snapshot.current,...patch};
    setRuntime(snapshot.current);
  };
  async function refresh(id=snapshot.current.run?.id, revision=version.current) {
    if(!id) return;
    const original=snapshot.current;
    const previous=original.events.at(-1)?.step??-1;
    const url=pathFor(id);
    const [run,events,orders,trades]=await Promise.all([
      api(url),api(url+'/events?after='+previous+'&limit=100'),api(url+'/orders'),api(url+'/trades')
    ]);
    if(revision!==version.current || snapshot.current.run?.id!==id) return;
    const merged = new Map(snapshot.current.events.map(event=>[event.step,event]));
    for(const event of events) merged.set(event.step,event);
    const runs=snapshot.current.runs.some(item=>item.id===id)?snapshot.current.runs.map(item=>item.id===id?run:item):[run,...snapshot.current.runs];
    update({run,runs,events:[...merged.values()].sort((a,b)=>a.step-b.step),orders,trades});
  }
  async function select(id,run) {
    const revision=++version.current;
    if(id) sessionStorage.setItem('gridmind-locality',id);
    update({localityId:id,run,participants:[],orders:[],trades:[],events:[],preview:null,reports:null,comparison:null});
    await Promise.all([
      id?api('/participants?locality_id='+enc(id)).then(participants=>{if(revision===version.current)update({participants});}):null,
      refresh(run?.id,revision)
    ]);
  }
  async function load(preferred) {
    try {
      await completeSignIn();
      const data=await api('/bootstrap');
      update(data);
      const desired=preferred??(snapshot.current.localityId||sessionStorage.getItem('gridmind-locality'));
      const id=data.localities.find(place=>place.id===desired)?.id??data.localities[0]?.id??'';
      await select(id,best(data.runs,id));
      setStatus({connected:true,loading:false});
    } catch(error) {
      setStatus({connected:false,loading:false,error});
      throw error;
    }
  }
  async function reports() {
    const revision=version.current;
    const run=snapshot.current.run;
    const [reports,benchmark,runs]=await Promise.all([api('/reports'+(run?'?run_id='+enc(run.id):'')),api('/benchmarks/ugim').catch(()=>({available:false})),api('/runs')]);
    if(revision===version.current)update({reports,benchmark,runs});
  }
  async function action(name,value) {
    const state=snapshot.current, id=state.run?.id, url=id?pathFor(id):null;
    const mutation=(path,body,method='POST')=>api(path,{method,...(body===undefined?{}:{body})});
    switch(name){
      case 'retry': return load();
      case 'locality': return select(value,best(state.runs,value));
      case 'run': {if(value==='')return select(state.localityId,null);const run=state.runs.find(row=>row.id===(value?.id??value));if(run)return select(run.locality_id,run);return;}
      case 'createRun': {const run=await mutation('/runs',value);await select(run.locality_id,run);return run;}
      case 'stop': if(url){await mutation(url+'/stop');await refresh(id);}return;
      case 'submitOrder': if(url){await mutation(url+'/orders',value);await refresh(id);}return;
      case 'cancelOrder': if(url){await mutation(url+'/orders/'+enc(value)+'/cancel');await refresh(id);}return;
      case 'saveLocality': {const item=await mutation('/localities'+(value.id?'/'+enc(value.id):''),value.body,value.id?'PUT':'POST');await load(item.id);return item;}
      case 'saveParticipant': {const item=await mutation('/participants'+(value.id?'/'+enc(value.id):''),value.body,value.id?'PUT':'POST');if(snapshot.current.localityId===value.body.locality_id){const participants=await api('/participants?locality_id='+enc(value.body.locality_id));if(snapshot.current.localityId===value.body.locality_id)update({participants});}return item;}
      case 'archiveLocality': await mutation('/localities/'+enc(value),undefined,'DELETE');return load();
      case 'archiveParticipant': {await mutation('/participants/'+enc(value),undefined,'DELETE');const participants=await api('/participants?locality_id='+enc(state.localityId));if(snapshot.current.localityId===state.localityId)update({participants});return;}
      case 'refreshRuns': {const runs=await api('/runs');update({runs});return;}
      case 'reports': return reports();
      case 'compare': {const current=version.current, comparison=await api('/comparison?'+new URLSearchParams(value));if(current===version.current)update({comparison});return comparison;}
      case 'preview': {if(!url)return;const current=version.current,preview=await api(url+'/preview');if(current===version.current)update({preview:preview?.result??preview});return preview;}
      case 'clearPreview': return update({preview:null});
      default: throw new Error('Unknown control: '+name);
    }
  }
  useEffect(()=>{
    if(!enabled)return;
    load().catch(()=>{});
    let alive=true,ticks=0;
    const timer=setInterval(async()=>{
      if(polling.current) return;
      const state=snapshot.current, shouldRefresh=active(state.run) || state.events.length<(state.run?.published_steps??0);
      if(!shouldRefresh && !state.runs.some(active)) return;
      polling.current=true;
      try{
        if(shouldRefresh)await refresh();
        if(++ticks%4===0){
          const runs=await api('/runs');
          if(alive)update({runs});
        }
        if(alive)setStatus(previous=>previous.connected?previous:{connected:true,loading:false});
      }catch(error){
        if(alive)setStatus({connected:false,loading:false,error});
      }finally{polling.current=false;}
    },1100);
    return()=>{alive=false;clearInterval(timer);};
  },[enabled]);
  return [{...runtime,step:runtime.run?.latest??runtime.events.at(-1)??null},status,action];
}
