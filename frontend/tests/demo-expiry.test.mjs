import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {test} from 'node:test';
import {runInNewContext} from 'node:vm';

const source = await readFile(new URL('../src/api.js', import.meta.url), 'utf8');
const cloud = {
  VITE_COGNITO_DOMAIN: 'https://auth.example.test',
  VITE_COGNITO_CLIENT_ID: 'test1234',
  VITE_API_BASE_URL: 'https://192.0.2.5:8443',
};
const origin = 'https://gridmind.example.test';
const key = 'gridmind-access';
const pendingKey = 'gridmind-pkce';
const expiryEnv = 'VITE_GRIDMIND_DEMO_EXPIRES_AT';
const expires = '2030-01-15T12:00:00Z';
const expiryTime = Date.parse(expires);
const invalid = /invalid expiration setting/;
const expired = /temporary GRIDMIND demo has expired/;
const response = data => new Response(JSON.stringify(data ?? {ok: true}));

function harness(env = {}, {href = origin + '/', now = expiryTime - 1, digest, responder} = {}) {
  const calls = [], redirects = [], rewrites = [];
  const state = new Map([
    [key, JSON.stringify({value: 'mock-bearer', expires: expiryTime + 3600_000})],
    [pendingKey, JSON.stringify({verifier: 'mock-verifier', state: 'mock-state'})],
    ['gridmind-key', 'mock-local-key'],
  ]);
  const clock = {now};
  class MockDate extends Date { static now() {return clock.now;} }
  const location = {origin, href, assign: to => redirects.push(to)};
  const api = runInNewContext(source
    .replace('import.meta.env', 'testEnv')
    .replace(/^export /gm, '') + '\n({api, cloudAuth, signIn, completeSignIn, explainRun});', {
    testEnv: {...cloud, ...env},
    Date: MockDate, Headers, URL, URLSearchParams, TextEncoder, Uint8Array, btoa,
    window: {
      location,
      history: {replaceState: (_, __, path) => {rewrites.push(path); location.href = new URL(path, origin).href;}},
    },
    sessionStorage: {
      getItem: name => state.get(name) ?? null,
      setItem: (name, data) => state.set(name, data),
      removeItem: name => state.delete(name),
    },
    crypto: {getRandomValues: array => array.fill(1), subtle: {digest: digest ?? (() => new ArrayBuffer(32))}},
    fetch: async (url, options) => {calls.push({url, options}); return responder ? responder(url) : response();},
  });
  return {api, calls, redirects, rewrites, state, clock};
}

test('a cloud deployment with no demo expiry works as usual, even after the test cutoff', async () => {
  const demo = harness({}, {now: expiryTime + 1});
  assert.equal(demo.api.cloudAuth, true);
  await demo.api.completeSignIn();
  await demo.api.api('/bootstrap');
  assert.equal(demo.calls[0].url, cloud.VITE_API_BASE_URL + '/api/bootstrap');
  assert.equal(demo.calls[0].options.headers.get('Authorization'), 'Bearer mock-bearer');
});

test('valid UTC expiry preserves authenticated API, Cognito sign-in and callback behavior', async () => {
  const demo = harness({[expiryEnv]: expires});
  await demo.api.explainRun('run/22');
  assert.equal(demo.calls[0].url, cloud.VITE_API_BASE_URL + '/api/runs/run%2F22/explanation');
  assert.equal(demo.calls[0].options.headers.get('Authorization'), 'Bearer mock-bearer');
  assert.equal(demo.calls[0].options.method, 'POST');
  await demo.api.signIn();
  assert.equal(demo.redirects.length, 1);
  assert.equal(new URL(demo.redirects[0]).origin, cloud.VITE_COGNITO_DOMAIN);
  assert.ok(demo.state.has(pendingKey));

  const callback = harness({[expiryEnv]: '2030-01-15T12:00:00.125Z'}, {
    href: origin + '/?code=mock-code&state=mock-state#simulation',
    responder: () => response({access_token:'new-mock-bearer',expires_in:3600}),
  });
  await callback.api.completeSignIn();
  assert.equal(callback.calls[0].url, cloud.VITE_COGNITO_DOMAIN + '/oauth2/token');
  assert.equal(callback.calls[0].options.body.get('code_verifier'), 'mock-verifier');
  assert.equal(callback.calls[0].options.credentials, 'omit');
  assert.equal(JSON.parse(callback.state.get(key)).value, 'new-mock-bearer');
  assert.equal(callback.state.has(pendingKey), false);
  assert.deepEqual(callback.rewrites, ['/#simulation']);
});

test('an expired or malformed demo does not send any network requests or retain authorization', async () => {
  for (const [value, error] of [
    [expires, expired],
    ['', invalid],
    ['2030-01-15T12:00:00+00:00', invalid],
    ['2030-01-15T12:00:00', invalid],
    ['2030-02-30T12:00:00Z', invalid],
    ['2030-01-15T24:00:00Z', invalid],
    ['2030-01-15', invalid],
  ]) {
    const demo = harness({[expiryEnv]: value}, {
      href: origin + '/?code=mock-code&state=mock-state#simulation',
      now: expiryTime,
    });
    // Check all independent entry points, including calls made without the App's initial guard.
    for (const request of [
      () => demo.api.api('/bootstrap',{headers:{Authorization:'Bearer forced-header'}}),
      () => demo.api.signIn(),
      () => demo.api.completeSignIn(),
    ]) {
      demo.state.set(key, JSON.stringify({value:'mock-bearer',expires:expiryTime+3600_000}));
      demo.state.set(pendingKey, JSON.stringify({verifier:'mock-verifier',state:'mock-state'}));
      await assert.rejects(request, error);
      assert.equal(demo.state.has(key), false);
      assert.equal(demo.state.has(pendingKey), false);
    }
    assert.deepEqual(demo.calls, []);
    assert.deepEqual(demo.redirects, []);
    assert.deepEqual(demo.rewrites, ['/#simulation']);
  }
});

test('local-only mode ignores cloud settings and demo expiration', async () => {
  const demo = harness({VITE_GRIDMIND_LOCAL_ONLY:'1', [expiryEnv]:'bad'}, {now:expiryTime+1});
  assert.equal(demo.api.cloudAuth, false);
  await demo.api.completeSignIn();
  await demo.api.api('/bootstrap');
  assert.equal(demo.calls[0].url, '/api/bootstrap');
  assert.equal(demo.calls[0].options.headers.get('X-API-Key'), 'mock-local-key');
  assert.equal(demo.calls[0].options.headers.get('Authorization'), null);
});

test('a previously successful API or callback check does not let the next action cross expiry', async () => {
  const demo = harness({[expiryEnv]: expires});
  await demo.api.completeSignIn();
  await demo.api.api('/bootstrap');
  demo.clock.now = expiryTime;
  await assert.rejects(demo.api.completeSignIn(), expired);
  await assert.rejects(demo.api.explainRun('run-22'), expired);
  assert.equal(demo.calls.length, 1);
  assert.equal(demo.state.has(key), false);
});

test('crossing expiry during PKCE computation, API serialization, or a token exchange fails closed', async () => {
  let finishDigest;
  const pkce = harness({[expiryEnv]: expires}, {digest: () => new Promise(resolve => {finishDigest=resolve;})});
  const authorizing = pkce.api.signIn();
  pkce.clock.now = expiryTime;
  finishDigest(new ArrayBuffer(32));
  await assert.rejects(authorizing, expired);
  assert.deepEqual(pkce.redirects, []);
  assert.equal(pkce.state.has(pendingKey), false);

  const forced = harness({[expiryEnv]: expires});
  await assert.rejects(forced.api.api('/bootstrap',{body:{toJSON() {forced.clock.now=expiryTime; return {};}}}), expired);
  assert.deepEqual(forced.calls, []);
  assert.equal(forced.state.has(key), false);

  let returnToken;
  const callback = harness({[expiryEnv]: expires}, {
    href: origin + '/?code=mock-code&state=mock-state',
    responder: () => new Promise(resolve => {returnToken=resolve;}),
  });
  const exchanging = callback.api.completeSignIn();
  callback.clock.now = expiryTime;
  returnToken(response({access_token:'late-bearer',expires_in:3600}));
  await assert.rejects(exchanging, expired);
  assert.equal(callback.calls.length, 1);
  assert.equal(callback.state.has(key), false);
  assert.equal(callback.state.has(pendingKey), false);
});
