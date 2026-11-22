const env = import.meta.env;
const localOnly = env.VITE_GRIDMIND_LOCAL_ONLY === '1';
const domain = (localOnly ? '' : env.VITE_COGNITO_DOMAIN || '').replace(/\/$/,'');
const clientId = localOnly ? '' : env.VITE_COGNITO_CLIENT_ID;
const base = (localOnly ? '' : env.VITE_API_BASE_URL || '').replace(/\/$/,'');
export const cloudAuth = Boolean(domain || clientId || base);
const httpsOrigin = /^https:\/\/[a-z0-9.-]+(?::[0-9]+)?$/i;
function requireValidCloud() {
  if (cloudAuth && (!httpsOrigin.test(domain) || !httpsOrigin.test(base) || !/^[a-z0-9]+$/i.test(clientId || ''))) {
    throw new Error('Configure all three GRIDMIND cloud settings with HTTPS origins and a Cognito client ID.');
  }
}
const redirectUri = window.location.origin + '/';
const tokenKey = 'gridmind-access';
const pkceKey = 'gridmind-pkce';
const encode = bytes => btoa(String.fromCharCode(...new Uint8Array(bytes))).replaceAll('+','-').replaceAll('/','_').replaceAll('=','');
const random = () => encode(crypto.getRandomValues(new Uint8Array(32)));

export async function signIn() {
  requireValidCloud();
  const verifier = random(), state = random();
  const challenge = encode(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(verifier)));
  sessionStorage.setItem(pkceKey, JSON.stringify({verifier,state}));
  const params = new URLSearchParams({client_id:clientId,response_type:'code',scope:'openid',redirect_uri:redirectUri,code_challenge_method:'S256',code_challenge:challenge,state});
  window.location.assign(domain+'/oauth2/authorize?'+params);
}
let callback;
export function completeSignIn() {
  return callback ??= (async () => {
    if (!cloudAuth) return;
    requireValidCloud();
    const url = new URL(window.location.href);
    if (!url.searchParams.has('code') && !url.searchParams.has('error')) return;
    window.history.replaceState(null,'',url.pathname+url.hash);
    const pending = JSON.parse(sessionStorage.getItem(pkceKey)||'null');
    sessionStorage.removeItem(pkceKey);
    const code = url.searchParams.get('code');
    if (!code || !pending || pending.state !== url.searchParams.get('state')) throw new Error('Sign-in could not be verified. Please try again.');
    const response = await fetch(domain+'/oauth2/token',{method:'POST',body:new URLSearchParams({grant_type:'authorization_code',client_id:clientId,code,redirect_uri:redirectUri,code_verifier:pending.verifier}),credentials:'omit',referrerPolicy:'no-referrer'});
    if (!response.ok) throw new Error('Sign-in could not be completed. Please try again.');
    const token = await response.json();
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
  const response=await fetch(base+'/api'+path,{...options,headers,body:body===undefined?undefined:JSON.stringify(body),cache:'no-store'});
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
