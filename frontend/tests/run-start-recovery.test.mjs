import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {test} from 'node:test';
import {runInNewContext} from 'node:vm';
import {connectionNotice, runStartError} from '../src/connection.js';
import {CLOUD_POLL_MS, LOCAL_POLL_MS, pollDelayMs, retryAfterMs} from '../src/polling.js';

const source = (await readFile(new URL('../src/runtime.js', import.meta.url), 'utf8'))
  .replace(/^import .*\n/gm, '').replace(/^export /gm, '');
const localities = [{id:'a'}, {id:'b'}];
const receipt = {id:'run-22',locality_id:'a',status:'queued',steps:120,completed_steps:0};
const full = {...receipt,status:'completed',completed_steps:120,published_steps:0};
const flush = () => new Promise(resolve=>setImmediate(resolve));
const fail = (status,message='mock private diagnostic')=>Object.assign(new Error(message),{status});

function deferred() {
  let resolve, reject;
  const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});
  return {promise,resolve,reject};
}

function setup({post=()=>receipt, details, readRun=()=>full, initial, cloud=false}={}) {
  const calls=[], hooks=[], effects=[], storage=new Map();
  let hook=0, saved=false, polled, interval, now=Date.parse('2026-11-23T12:00:00Z');
  const times=[];
  class MockDate extends Date {static now(){return now;}}
  const useRuntime=runInNewContext(source+'\nuseRuntime;', {
    useState(initial) {
      const index=hook++;
      if (!hooks[index]) hooks[index]={value:initial};
      return [hooks[index].value, update=>{hooks[index].value=typeof update==='function'?update(hooks[index].value):update;}];
    },
    useRef(initial) {return hooks[hook++]??= {current:initial};},
    useEffect(effect) {const index=hook++;if(!hooks[index]){hooks[index]={};effects.push(effect);}},
    setInterval(callback, ms) {polled=callback;interval=ms;return 1;}, clearInterval() {},
    setTimeout() {return 1;}, clearTimeout() {},
    Date: MockDate, AbortController, CLOUD_POLL_MS, LOCAL_POLL_MS, pollDelayMs, cloudAuth:cloud,
    sessionStorage: {getItem:key=>storage.get(key),setItem:(key,value)=>storage.set(key,value)},
    completeSignIn: async()=>{},
    async api(path, options) {
      calls.push([path,options?.method??'GET']);
      times.push(now);
      if(path==='/bootstrap')return {localities,runs:saved?[full]:initial?[initial]:[]};
      if(path.startsWith('/participants'))return [];
      if(path==='/runs' && options?.method==='POST') {
        const run=await post();
        saved=true;
        return run;
      }
      if(path==='/runs/run-22')return readRun(options?.signal);
      if(details && path.startsWith('/runs/run-22/events'))return details(path);
      if (path==='/runs')return initial?[initial]:[];
      if (/\/(events|orders|trades)/.test(path)) return [];
      throw Error('Unexpected test endpoint '+path);
    },
    runStartError,
  });
  function render(){hook=0;return useRuntime();}
  render();
  for(const effect of effects) effect();
  return {render,calls,times,poll:(elapsed=interval)=>{now+=elapsed;return polled();}};
}
const noRepost = calls => assert.deepEqual(calls.filter(([,method])=>method==='POST'),[['/runs','POST']]);

test('an acknowledged run closes the create path before detail reads and recovers without another POST', async () => {
  const late=deferred();
  let unavailable=true;
  const app=setup({details:()=>unavailable?late.promise:[],readRun:()=>unavailable?receipt:full});
  await flush();
  const [,,action]=app.render();
  const launched=action('createRun',{locality_id:'a'});
  try {
    const [ack] = await Promise.race([launched.then(result=>[result]),flush().then(()=>[])]);
    assert.equal(ack,receipt,'the create action does not wait for a pending details request');
    const [state] = app.render();
    assert.equal(state.run.id,receipt.id);
    assert.equal(state.runs[0].id,receipt.id);
    assert.equal(state.run.status,'queued');
  } finally {
    late.reject(new TypeError('mock private endpoint URL'));
  }
  await launched;
  await flush();
  const [after, warning, retry] = app.render();
  assert.equal(after.run.id,receipt.id);
  assert.equal(warning.connected,false);
  assert.equal(warning.detailRun,receipt.id);
  const notice=connectionNotice(warning.error,true,warning.detailRun===after.run.id);
  assert.equal(notice.title,'Run created; updates unavailable');
  assert.equal(notice.action,'retry');
  assert.doesNotMatch(notice.detail,/mock private/u);
  unavailable=false;
  await retry('retry');
  const [recovered, connected]=app.render();
  assert.equal(recovered.run.status,'completed');
  assert.equal(connected.connected,true);
  noRepost(app.calls);
});

test('unauthorized and forbidden starts remain distinct; a lost response does not invite a duplicate', async () => {
  for(const [error, pattern, action] of [
    [fail(401),/Sign in before starting/,'sign-in'],
    [fail(403),/does not have permission to start/,undefined],
    [new TypeError('mock private endpoint URL'),/Check Run History before submitting again/,undefined],
    [fail(502),/Check Run History before submitting again/,undefined],
    [fail(409),/active run/,undefined],
  ]){
    const app=setup({post:()=>{throw error;}});
    await flush();
    await assert.rejects(app.render()[2]('createRun',{locality_id:'a'}), problem=>{
      assert.match(problem.message,pattern);
      assert.doesNotMatch(problem.message,/mock private/u);
      return true;
    });
    const [state,status]=app.render();
    assert.equal(state.run,null);
    if(action || error.status===403)assert.equal(connectionNotice(status.error,true).action,action);
    noRepost(app.calls);
  }
});

test('a delayed receipt goes into history without switching a newer locality selection', async () => {
  const late=deferred();
  const app=setup({post:()=>late.promise});
  await flush();
  const action=app.render()[2];
  const creating=action('createRun',{locality_id:'a'});
  await action('locality','b');
  late.resolve(receipt);
  await creating;
  const [state]=app.render();
  assert.equal(state.localityId,'b');
  assert.equal(state.run,null);
  assert.equal(state.runs[0].id,receipt.id);
  assert.equal(app.calls.some(([path])=>path.startsWith('/runs/run-22')),false);
});

test('connection recovery only offers Cognito for real authentication failures', () => {
  assert.equal(connectionNotice(new TypeError('hidden URL'),true).action,'retry');
  assert.equal(connectionNotice(fail(429),true).action,'retry');
  assert.equal(connectionNotice(fail(401),true).action,'sign-in');
  assert.equal(connectionNotice(fail(401),false).action,'api-key');
  assert.equal(connectionNotice(fail(403),true,true).title,'Access denied');
  assert.equal(connectionNotice({kind:'sign-in'},true).action,'sign-in');
  assert.equal(connectionNotice({kind:'demo-expired'},true).action,undefined);
  assert.equal(connectionNotice({kind:'cloud-config'},true).action,undefined);
});

test('cloud polling sends one GET per turn and stages only missing event frames', async () => {
  let latest={...receipt,status:'running',completed_steps:2,published_steps:2};
  const app=setup({cloud:true,initial:latest,readRun:()=>latest,details:path=>{
    const cursor=Number(new URL(path,'https://mock.test').searchParams.get('after'));
    return Array.from({length:latest.published_steps},(_,step)=>({step})).filter(row=>row.step>cursor);
  }});
  await flush();
  const start=app.calls.length;
  for(let i=0;i<8;i++)await app.poll();
  const background=app.calls.slice(start);
  assert.ok(background.length>=5 && background.length<=8);
  assert.equal(background.filter(([url])=>url==='/runs/run-22').length,4);
  assert.equal(background.some(([url])=>url.includes('/events')),false);
  const times=app.times.slice(start);
  assert.ok(times.slice(1).every((time,index)=>time-times[index]>=1250));
  assert.ok(background.every(([,method])=>method==='GET'));

  latest={...latest,completed_steps:3,published_steps:3};
  await app.poll(); // Status discovers more published frames.
  await app.poll(); // Fetch only after the saved cursor.
  assert.ok(app.calls.at(-1)[0].includes('/events?after=1'));
  assert.equal(app.render()[0].events.length,3);

  latest={...latest,status:'completed'};
  await app.poll();
  const final=app.calls.length;
  await app.poll(); await app.poll(); await app.poll();
  assert.deepEqual(app.calls.slice(final),[['/runs/run-22/orders','GET'],['/runs/run-22/trades','GET']]);
});

test('429 and transient network recovery auto-retry only GETs, retain display data, and never overlap', async () => {
  const latest={...receipt,status:'running',completed_steps:1,published_steps:1};
  let readCount=0, stalled=deferred();
  const app=setup({cloud:true,initial:latest,details:()=>[{step:0}],readRun:()=>{
    readCount++;
    if(readCount===2)throw Object.assign(fail(429),{retryAfterMs:7000});
    if(readCount===3)return stalled.promise;
    return latest;
  }});
  await flush();
  await app.poll();
  const [old,status,retry]=app.render();
  assert.equal(old.events.length,1);
  assert.equal(old.run.completed_steps,1);
  assert.equal(status.connected,false);
  assert.equal(connectionNotice(status.error,true).action,'retry');
  const before=app.calls.length;
  await retry('retry'); // Cannot bypass an explicit server cooldown.
  await app.poll(6999);
  assert.equal(app.calls.length,before);
  const inFlight=app.poll(1);
  await app.poll(); await app.poll();
  assert.equal(app.calls.length,before+1,'pending background request must not overlap');
  stalled.reject(new TypeError('CORS-blocked 429 may look like a network failure'));
  await inFlight;
  assert.equal(app.render()[0].events.length,1);
  assert.equal(connectionNotice(app.render()[1].error,true).action,'retry');
  await app.poll(4999);
  assert.equal(app.calls.length,before+1);
  await app.poll(1);
  assert.equal(app.calls.length,before+2,'next attempt waits for exponential backoff');
  assert.equal(app.render()[1].connected,true);
  assert.ok(app.calls.every(([,method])=>method==='GET'));
});

test('permission failures stop the poll without wiping data or treating forbidden as sign-in', async () => {
  const latest={...receipt,status:'running',completed_steps:1,published_steps:1};
  let loaded=false;
  const app=setup({cloud:true,initial:latest,details:()=>[{step:0}],readRun:()=>{if(loaded)throw fail(403);return latest;}});
  await flush();
  loaded=true;
  await app.poll();
  const count=app.calls.length;
  assert.equal(app.render()[0].events.length,1);
  assert.equal(connectionNotice(app.render()[1].error,true).title,'Access denied');
  await app.poll(60_000);
  assert.equal(app.calls.length,count);
});

test('Retry-After handles delta-seconds, RFC HTTP dates, invalids, and server delays longer than capped backoff', () => {
  const now=Date.parse('2026-11-23T12:00:00Z');
  assert.equal(retryAfterMs('7',now),7000);
  assert.equal(retryAfterMs('Mon, 23 Nov 2026 12:00:11 GMT',now),11000);
  assert.equal(retryAfterMs('Mon, 23 Nov 2026 11:59:00 GMT',now),0);
  for(const bad of [null,'','-1','1.5','garbage','999999999999999999999999'])assert.equal(retryAfterMs(bad,now),0);
  assert.equal(pollDelayMs(1),2500);
  assert.equal(pollDelayMs(2),5000);
  assert.equal(pollDelayMs(100),30000);
  assert.equal(pollDelayMs(100,65000),65000);
});
