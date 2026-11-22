import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { fileURLToPath } from 'node:url';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer } from 'vite';

let server;
let Renewables;
before(async () => {
  server = await createServer({
    root: fileURLToPath(new URL('../', import.meta.url)),
    configFile: false,
    appType: 'custom',
    optimizeDeps: { noDiscovery: true },
    server: { middlewareMode: true, hmr: false, watch: null },
  });
  ({ Renewables } = await server.ssrLoadModule('/src/Insights.jsx'));
});
after(async () => { await server?.close(); });

const participants = [
  { id: 'solar', name: 'Solar and storage cooperative', solar_kw: 46 },
  { id: 'homes', name: 'Riverside homes', solar_kw: 6 },
  { id: 'wind', name: 'Community wind cooperative', wind_kw: 18 },
];
const austinOutlook = [
  { participant_id: 'solar', forecast_solar_kw: 31.22296, forecast_wind_kw: 0 },
  { participant_id: 'homes', forecast_solar_kw: 4.07256, forecast_wind_kw: 0 },
  { participant_id: 'wind', forecast_solar_kw: 0, forecast_wind_kw: 0.68742 },
].map(row => ({
  condition: 'Sunny', valid_at: '2018-08-24T21:00:00Z',
  range_low_kw: null, range_high_kw: null, ...row,
}));

function render(runtime) {
  return renderToStaticMarkup(React.createElement(Renewables, { runtime }));
}
function renderOutlook(renewables, roster = participants) {
  return render({ run: {
    participants: roster,
    latest: { step: 8, timestamp: '2018-08-24T20:00:00Z', renewables },
  } });
}
function ranges(markup) {
  return Array.from(markup.matchAll(/<dt>Output range<\/dt><dd>(.*?)<\/dd>/g), result => result[1]);
}
const explanation = /this replay doesn’t provide uncertainty bands for all/;

test('bundled Austin forecasts remain visible when the replay has no uncertainty bands', () => {
  const markup = renderOutlook(austinOutlook);
  assert.deepEqual(ranges(markup), ['Not available', 'Not available', 'Not available']);
  assert.match(markup, explanation);
  const number = new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 });
  for (const estimate of [31.22, 4.07, 0.69]) {
    assert.ok(markup.includes(`<strong>${number.format(estimate)} kW</strong>`));
  }
});

test('complete bounds, including zero, keep their units when other ranges are missing', () => {
  const markup = renderOutlook([
    { ...austinOutlook[0], range_low_kw: 20, range_high_kw: 40 },
    { ...austinOutlook[1], range_low_kw: 0, range_high_kw: 0 },
    { ...austinOutlook[2], range_low_kw: 0 },
  ]);
  assert.deepEqual(ranges(markup), ['20–40 kW', '0–0 kW', 'Not available']);
  assert.match(markup, explanation);

  const allAvailable = renderOutlook([{ ...austinOutlook[0], range_low_kw: 0, range_high_kw: 1 }]);
  assert.deepEqual(ranges(allAvailable), ['0–1 kW']);
  assert.doesNotMatch(allAvailable, explanation);
});

test('idle and assetless runs do not present a missing range', () => {
  const idle = render({ participants });
  assert.match(idle, /Start or select a simulation/);
  assert.deepEqual(ranges(idle), []);
  const assetless = renderOutlook([], []);
  assert.match(assetless, /Register a solar or wind asset/);
  assert.deepEqual(ranges(assetless), []);
});
