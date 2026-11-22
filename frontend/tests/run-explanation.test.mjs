import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { fileURLToPath } from 'node:url';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer } from 'vite';

let server, Simulation, RunExplanation;
before(async () => {
  server = await createServer({
    root: fileURLToPath(new URL('../', import.meta.url)),
    configFile: false,
    appType: 'custom',
    optimizeDeps: { noDiscovery: true },
    server: { middlewareMode: true, hmr: false, ws: false, watch: null },
  });
  ({ default: Simulation } = await server.ssrLoadModule('/src/Simulation.jsx'));
  ({ RunExplanation } = await server.ssrLoadModule('/src/RunExplanation.jsx'));
});

after(async () => { await server?.close(); });

function row(id, status) {
  return { id, status, scenario: 'historical', locality_name: id, steps: 24, completed_steps: 24 };
}

test('only completed rows offer an explicit explanation; opening runs still works independently', () => {
  const runs = [row('Completed Run', 'completed'), row('Running Run', 'running'), row('Stopped Run', 'stopped'), row('Failed Run', 'failed')];
  const requests = [];
  const output = renderToStaticMarkup(React.createElement(Simulation, {
    runtime: { runs },
    requestExplanation: id => requests.push(id),
  }));
  const [completed, running, stopped, failed] = [...output.matchAll(/<tr><th scope="row"[\s\S]*?<\/tr>/g)].map(match => match[0]);
  assert.match(completed, /aria-expanded="false">Explain this run<\/button>/);
  for (const markup of [running, stopped, failed]) assert.doesNotMatch(markup, /Explain this run/);
  for (const markup of [completed, running, stopped, failed]) assert.match(markup, />Open<\/button>/);
  assert.doesNotMatch(output, /Requesting an explanation|Run explanation<\/h3>/);
  assert.deepEqual(requests, []);
});

function renderDetail(state) {
  return renderToStaticMarkup(React.createElement(RunExplanation, { state, id: 'explanation-A' }));
}

test('explanations are labeled as generated and rendered as plain text', () => {
  const text = '<img src=x onerror=alert(1)>\nSecond line';
  const markup = renderDetail({ status: 'ready', text });
  assert.match(markup, /aria-labelledby="explanation-A-title"/);
  assert.match(markup, /role="status"/);
  assert.match(markup, /AI-generated from saved run results/);
  assert.match(markup, /&lt;img src=x onerror=alert\(1\)&gt;\nSecond line/);
  assert.doesNotMatch(markup, /<img/);
});

test('loading, disabled, pending, unavailable, incomplete and error states retain contextual feedback', () => {
  for (const [status, copy, action] of [
    ['loading', /Requesting an explanation/, null],
    ['disabled', /turned off for this deployment/, null],
    ['pending', /still being generated/, /Check again<\/button>/],
    ['unavailable', /couldn’t be generated/, null],
    ['incomplete', /isn’t complete yet/, /Try again<\/button>/],
    ['unauthorized', /Sign in or reconnect/, /Try again<\/button>/],
    ['error', /Couldn’t load/, /Try again<\/button>/],
  ]) {
    const markup = renderDetail({ status });
    assert.match(markup, copy);
    if (action) assert.match(markup, action);
    else assert.doesNotMatch(markup, /<button/);
  }
});
