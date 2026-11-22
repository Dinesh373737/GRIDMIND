const env = import.meta.env;
const localOnly = env.VITE_GRIDMIND_LOCAL_ONLY === '1';
const domain = (localOnly ? '' : env.VITE_COGNITO_DOMAIN || '').replace(/\/$/,'');
const clientId = localOnly ? '' : env.VITE_COGNITO_CLIENT_ID;
const base = (localOnly ? '' : env.VITE_API_BASE_URL || '').replace(/\/$/,'');
export const cloudAuth = Boolean(domain || clientId || base);
const httpsOrigin = /^https:\/\/[a-z0-9.-]+(?::[0-9]+)?$/i;
const redirectUri = window.location.origin + '/';
const tokenKey = 'gridmind-access';
const pkceKey = 'gridmind-pkce';
const demoExpiry = cloudAuth ? env.VITE_GRIDMIND_DEMO_EXPIRES_AT : undefined;
const demoExpiryTime = parseUtc(demoExpiry);

function parseUtc(value) {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?Z$/.test(value)) return NaN;
  const time = Date.parse(value);
  // Date.parse can normalize an invalid calendar date instead of rejecting it.
  return Number.isFinite(time) && new Date(time).toISOString().slice(0,19) === value.slice(0,19) ? time : NaN;
}

function requireValidCloud() {
  if (!cloudAuth) return;
  if (demoExpiry !== undefined && (!Number.isFinite(demoExpiryTime) || Date.now() >= demoExpiryTime)) {
    sessionStorage.removeItem(tokenKey);
    sessionStorage.removeItem(pkceKey);
    throw new Error(Number.isFinite(demoExpiryTime)
      ? 'This temporary GRIDMIND demo has expired. Open an updated deployment to continue.'
      : 'This GRIDMIND demo has an invalid expiration setting. Contact the deployment owner.');
  }
  if (!httpsOrigin.test(domain) || !httpsOrigin.test(base) || !/^[a-z0-9]+$/i.test(clientId || '')) {
    throw new Error('Configure all three GRIDMIND cloud settings with HTTPS origins and a Cognito client ID.');
  }
}
const encode = bytes => btoa(String.fromCharCode(...new Uint8Array(bytes))).replaceAll('+','-').replaceAll('/','_').replaceAll('=','');
const random = () => encode(crypto.getRandomValues(new Uint8Array(32)));

export async function signIn() {
  requireValidCloud();
  const verifier = random(), state = random();
  const challenge = encode(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(verifier)));
  requireValidCloud();
  sessionStorage.setItem(pkceKey, JSON.stringify({verifier,state}));
  const params = new URLSearchParams({client_id:clientId,response_type:'code',scope:'openid',redirect_uri:redirectUri,code_challenge_method:'S256',code_challenge:challenge,state});
  window.location.assign(domain+'/oauth2/authorize?'+params);
}
let callback;
export async function completeSignIn() {
  if (!cloudAuth) return;
  const url = new URL(window.location.href);
  const hasCallback = url.searchParams.has('code') || url.searchParams.has('error');
  if (hasCallback) window.history.replaceState(null,'',url.pathname+url.hash);
  requireValidCloud();
  return callback ??= (async () => {
    if (!hasCallback) return;
    const pending = JSON.parse(sessionStorage.getItem(pkceKey)||'null');
    sessionStorage.removeItem(pkceKey);
    const code = url.searchParams.get('code');
    if (!code || !pending || pending.state !== url.searchParams.get('state')) throw new Error('Sign-in could not be verified. Please try again.');
    requireValidCloud();
    const response = await fetch(domain+'/oauth2/token',{method:'POST',body:new URLSearchParams({grant_type:'authorization_code',client_id:clientId,code,redirect_uri:redirectUri,code_verifier:pending.verifier}),credentials:'omit',referrerPolicy:'no-referrer'});
    requireValidCloud();
    if (!response.ok) throw new Error('Sign-in could not be completed. Please try again.');
    const token = await response.json();
    requireValidCloud();
    if (!token.access_token) throw new Error('Sign-in did not return an access token.');
    sessionStorage.setItem(tokenKey, JSON.stringify({value:token.access_token,expires:Date.now()+token.expires_in*1000}));
  })();
}
export function signOut() {
  sessionStorage.removeItem(tokenKey);
  window.location.assign(domain+'/logout?'+new URLSearchParams({client_id:clientId,logout_uri:redirectUri}));
}
export async function api(path,{body,headers:initial,...options}={}) {
  requireValidCloud();
  const headers = new Headers(initial);
  if (cloudAuth) {
    const token = JSON.parse(sessionStorage.getItem(tokenKey)||'null');
    if (token?.expires > Date.now()) headers.set('Authorization','Bearer '+token.value);
  } else {
    const key = sessionStorage.getItem('gridmind-key');
    if(key) headers.set('X-API-Key',key);
  }
  if(body !== undefined) headers.set('Content-Type','application/json');
  const serializedBody = body===undefined ? undefined : JSON.stringify(body);
  requireValidCloud();
  const response=await fetch(base+'/api'+path,{...options,headers,body:serializedBody,cache:'no-store'});
  const payload=await response.json().catch(()=>null);
  if(!response.ok) {
    const detail = payload?.detail;
    const error = new Error(typeof detail==='string'? detail : Array.isArray(detail)? detail.map(x=>[x.loc?.at(-1),x.msg].filter(Boolean).join(': ')).join(' • '):'Request failed ('+response.status+').');
    error.status=response.status;
    throw error;
  }
  return payload;
}

export function explainRun(id, signal) {
  return api('/runs/'+encodeURIComponent(id)+'/explanation',{method:'POST',signal});
}
