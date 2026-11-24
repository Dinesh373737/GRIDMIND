import {useEffect,useRef,useState} from 'react';
import {api,cloudAuth,completeSignIn} from './api';
import {runStartError} from './connection';
import {CLOUD_POLL_MS,LOCAL_POLL_MS,pollDelayMs} from './polling';
export const active = run => ['queued','starting','running','stopping','pending'].includes(run?.status);
const blank = {packs:[],facility_types:[],localities:[],participants:[],runs:[],localityId:'',run:null,events:[],orders:[],trades:[],preview:null,reports:null,comparison:null,benchmark:null};
const enc = encodeURIComponent;
const pathFor = id=>'/runs/'+enc(id);
const cloudLanes = ['run','events','run','orders','run','trades','run','history'];
const permissionError = error => [401,403].includes(error?.status) || ['sign-in','demo-expired','cloud-config'].includes(error?.kind);
async function settleReads(reads) {
  const results=await Promise.allSettled(reads);
  const failures=results.filter(result=>result.status==='rejected');
  if(failures.length) {
    const permission=failures.find(({reason})=>permissionError(reason));
    const slowed=failures.filter(({reason})=>reason?.retryAfterMs).sort((a,b)=>b.reason.retryAfterMs-a.reason.retryAfterMs)[0];
    throw (permission ?? slowed ?? failures[0]).reason;
  }
}
const best = (runs,id)=>runs.find(run=>run.locality_id===id && active(run))||runs.find(run=>run.locality_id===id)||null;

export function useRuntime(enabled=true) {
  const [runtime,setRuntime]=useState(blank);
  const [status,setStatus]=useState({connected:false,loading:true});
  const snapshot=useRef(blank), version=useRef(0), polling=useRef(false), foreground=useRef(0);
  const hold=useRef({attempt:0,resumeAt:0,retryAfterAt:0,blocked:false});
  const update = patch => {
    snapshot.current={...snapshot.current,...patch};
    setRuntime(snapshot.current);
  };
  function connected() {
    hold.current={attempt:0,resumeAt:0,retryAfterAt:0,blocked:false};
    setStatus(previous=>previous.connected?previous:{connected:true,loading:false});
  }
  function disconnected(error, detailRun) {
    const now=Date.now(), attempt=hold.current.attempt+1;
    const retryAfterAt=Math.max(hold.current.retryAfterAt,now+(error?.retryAfterMs||0));
    const blocked=permissionError(error);
    hold.current={attempt,resumeAt:Math.max(retryAfterAt,now+pollDelayMs(attempt)),retryAfterAt,blocked};
    setStatus(previous=>({connected:false,loading:false,error,
      detailRun:detailRun ?? (previous.detailRun===snapshot.current.run?.id ? previous.detailRun : undefined),
    }));
  }
  async function foregroundRead(read) {
    foreground.current++;
    try {return await read();}
    finally {foreground.current--;}
  }
  async function readRunPart(part, id=snapshot.current.run?.id, revision=version.current, signal) {
    if(!id || revision!==version.current) return;
    const url=pathFor(id);
    const previous=snapshot.current.events.at(-1)?.step??-1;
    const suffix={run:'',events:'/events?after='+previous+'&limit=100',orders:'/orders',trades:'/trades'}[part];
    const result=await api(url+suffix,{signal});
    if(signal?.aborted || revision!==version.current || snapshot.current.run?.id!==id) return;
    if(part==='run') {
      const runs=snapshot.current.runs.some(item=>item.id===id)?snapshot.current.runs.map(item=>item.id===id?result:item):[result,...snapshot.current.runs];
      update({run:result,runs});
    } else if(part==='events') {
      const merged=new Map(snapshot.current.events.map(event=>[event.step,event]));
      for(const event of result) merged.set(event.step,event);
      update({events:[...merged.values()].sort((a,b)=>a.step-b.step)});
    } else update({[part]:result});
  }
  async function refresh(id=snapshot.current.run?.id, revision=version.current, signal) {
    if(!id) return;
    const parts=['run','events','orders','trades'];
    return foregroundRead(()=>settleReads(parts.map(part=>readRunPart(part,id,revision,signal))));
  }
  async function select(id,run) {
    const revision=++version.current;
    const current=snapshot.current, sameLocality=id===current.localityId, sameRun=sameLocality && run?.id===current.run?.id;
    if(id) sessionStorage.setItem('gridmind-locality',id);
    update({localityId:id,run,...(sameLocality?{}:{participants:[]}),
      ...(sameRun?{}:{orders:[],trades:[],events:[],preview:null,reports:null,comparison:null}),
    });
    return foregroundRead(()=>settleReads([
      id?api('/participants?locality_id='+enc(id)).then(participants=>{if(revision===version.current)update({participants});}):null,
      refresh(run?.id,revision),
    ]));
  }
  async function load(preferred) {
    return foregroundRead(async()=>{
      try {
        await completeSignIn();
        const previous=snapshot.current;
        const data=await api('/bootstrap');
        update(data);
        const desired=preferred??(previous.localityId||sessionStorage.getItem('gridmind-locality'));
        const id=data.localities.find(place=>place.id===desired)?.id??data.localities[0]?.id??'';
        const current=data.runs.find(item=>item.locality_id===id && item.id===previous.run?.id);
        await select(id,current??best(data.runs,id));
        connected();
      } catch(error) {
        disconnected(error);
        throw error;
      }
    });
  }
  async function reports() {
    return foregroundRead(async()=>{
      const revision=version.current;
      const run=snapshot.current.run;
      const [reports,benchmark,runs]=await Promise.all([api('/reports'+(run?'?run_id='+enc(run.id):'')),api('/benchmarks/ugim').catch(()=>({available:false})),api('/runs')]);
      if(revision===version.current)update({reports,benchmark,runs});
    });
  }
  async function action(name,value) {
    const state=snapshot.current, id=state.run?.id, url=id?pathFor(id):null;
    const mutation=(path,body,method='POST')=>api(path,{method,...(body===undefined?{}:{body})});
    switch(name){
      case 'retry': if(Date.now()>=hold.current.retryAfterAt)return load();return;
      case 'locality': return select(value,best(state.runs,value));
      case 'run': {if(value==='')return select(state.localityId,null);const run=state.runs.find(row=>row.id===(value?.id??value));if(run)return select(run.locality_id,run);return;}
      case 'createRun': {
        const previousRevision=version.current;
        let run;
        try {
          run=await mutation('/runs',value);
          if(!run?.id || !run?.locality_id) throw new Error('Missing run receipt.');
        } catch(error) {
          if([401,403].includes(error?.status) || ['cloud-config','demo-expired'].includes(error?.kind)) {
            disconnected(error);
          }
          throw new Error(runStartError(error));
        }
        update({runs:[run,...snapshot.current.runs.filter(item=>item.id!==run.id)]});
        if(previousRevision===version.current) {
          // The POST is acknowledged. A failed detail read must not turn it into a failed create.
          const details=select(run.locality_id,run);
          const revision=version.current;
          details.catch(error=>{
            if(revision===version.current) disconnected(error,run.id);
          });
        }
        return run;
      }
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
    let alive=true,ticks=0,lane=0,watchedId,terminal=[],lastHistory=0,lastDetails={},controller;
    const timer=setInterval(async()=>{
      if(polling.current || foreground.current || hold.current.blocked || Date.now()<hold.current.resumeAt) return;
      const revision=version.current, state=snapshot.current;
      const id=state.run?.id, running=active(state.run);
      const backlog=state.events.length<(state.run?.published_steps??0);
      const otherActive=state.runs.some(run=>run.id!==id && active(run));
      if(watchedId!==id){watchedId=id;lane=0;terminal=[];lastDetails={};}
      if(!running && !backlog && !otherActive && !terminal.length)return;
      let part;
      const progress=state.run?.completed_steps+':'+state.run?.published_steps;
      if(cloudAuth) {
        if(running) {
          part=cloudLanes[lane++ % cloudLanes.length];
          if((part==='events' && !backlog) || (part==='history' && !otherActive))return;
          if((part==='orders' || part==='trades') && lastDetails[part]?.progress===progress &&
             Date.now()-lastDetails[part].at<15_000)return;
        } else if(terminal.length) part=terminal[0];
        else if(backlog) part='events';
        else {
          if(Date.now()-lastHistory<5000)return;
          part='history';
        }
      }
      polling.current=true;
      controller=new AbortController();
      const request=controller, {signal}=request;
      const timeout=setTimeout(()=>request.abort(),10_000);
      const stillCurrent=()=>alive && revision===version.current;
      try{
        if(cloudAuth) {
          if(part==='history') {
            const runs=await api('/runs',{signal});
            if(stillCurrent()&&!signal.aborted)update({runs});
            lastHistory=Date.now();
          } else {
            await readRunPart(part,id,revision,signal);
            if(stillCurrent()&&!signal.aborted) {
              if(part==='run' && running && !active(snapshot.current.run)) terminal=['orders','trades'];
              else if(!running && terminal[0]===part) terminal.shift();
              if(part==='orders' || part==='trades')lastDetails[part]={progress,at:Date.now()};
            }
          }
        } else {
          if(running || backlog) await refresh(id,revision,signal);
          if(++ticks%4===0){
            const runs=await api('/runs',{signal});
            if(stillCurrent()&&!signal.aborted)update({runs});
          }
        }
        if(stillCurrent()&&!signal.aborted)connected();
      }catch(error){
        if(stillCurrent()) {
          if(cloudAuth && running)lane=0;
          disconnected(error);
        }
      }finally{
        clearTimeout(timeout);
        controller=undefined;
        polling.current=false;
      }
    },cloudAuth?CLOUD_POLL_MS:LOCAL_POLL_MS);
    return()=>{alive=false;clearInterval(timer);controller?.abort();};
  },[enabled]);
  return [{...runtime,step:runtime.run?.latest??runtime.events.at(-1)??null},status,action];
}
