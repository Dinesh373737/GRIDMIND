import React, { useEffect, useRef, useState } from 'react';
import './agentDemo.css';

const STEP_DURATION_MS = 1450;
const PLAYBACK_SPEEDS = [0.5, 1, 1.5, 2];
const newStepClock = () => ({ remaining: STEP_DURATION_MS });

const agents = {
  grid: { name: 'Grid Reliability', short: 'ASSESS', description: 'Checks the upstream connection', icon: 'grid' },
  supply: { name: 'Renewable & Storage', short: 'SUPPLY', description: 'Measures local energy available', icon: 'supply' },
  demand: { name: 'Demand & Priority', short: 'RANK', description: 'Puts critical demand first', icon: 'demand' },
  market: { name: 'P2P Market', short: 'MATCH', description: 'Clears eligible local orders', icon: 'market' },
  coordinator: { name: 'Coordinator', short: 'VALIDATE', description: 'Reconciles the interval', icon: 'coordinator' },
};

const steps = [
  { agent: 'coordinator', label: 'Read interval', detail: 'Loaded a 15-minute interval for the fictional Eastbank locality.', time: '00.0' },
  { agent: 'grid', label: 'Detect outage', detail: 'The upstream feeder tripped. A grid-forming battery keeps Eastbank islanded, so the emergency market can open.', time: '00.4' },
  { agent: 'supply', label: 'Evaluate supply', detail: 'The solar cooperative offers 4.0 kWh and the community battery offers 2.0 kWh after local reserve checks.', time: '00.8' },
  { agent: 'demand', label: 'Rank requests', detail: 'The clinic requests 4.0 kWh of essential energy; the apartments request 3.0 kWh of standard energy.', time: '01.2' },
  { agent: 'market', label: 'Match orders', detail: 'Matched 4.0 kWh to the clinic first, then 2.0 kWh to the apartments. The remaining 1.0 kWh was not served.', time: '01.7' },
  { agent: 'coordinator', label: 'Validate balance', detail: 'Confirmed no upstream import, 6.0 kWh supplied and served, and 1.0 kWh unmet. Nothing was saved.', time: '02.1' },
];

const links = [
  ['grid', 'M 171 68 H 251 Q 273 68 273 90 V 111 Q 273 133 297 133 H 359'],
  ['supply', 'M 629 68 H 549 Q 527 68 527 90 V 111 Q 527 133 503 133 H 441'],
  ['demand', 'M 171 198 H 251 Q 273 198 273 176 V 155 Q 273 133 297 133 H 359'],
  ['market', 'M 629 198 H 549 Q 527 198 527 176 V 155 Q 527 133 503 133 H 441'],
];

function AgentIcon({ type }) {
  const shapes = {
    grid: <><path d="m3 21 9-18 9 18M7 13h10M5 17h14M12 3v18"/></>,
    supply: <><rect x="3" y="6" width="16" height="12" rx="2"/><path d="M21 10v4M10 9l-2 4h4l-2 3"/></>,
    demand: <><path d="M4 13h4l2-6 4 11 2-5h4"/><rect x="2" y="3" width="20" height="18" rx="4"/></>,
    market: <><path d="M3 8h17l-4-4M21 16H4l4 4"/></>,
    coordinator: <><circle cx="12" cy="12" r="4"/><path d="M12 2v6m0 8v6M2 12h6m8 0h6"/><circle cx="12" cy="12" r="1" fill="currentColor"/></>,
  };
  return <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="1.65" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{shapes[type]}</svg>;
}

export default function AgentDemo() {
  const [index, setIndex] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [hasStarted, setHasStarted] = useState(false);
  const [speed, setSpeed] = useState(1);
  const stepClock = useRef(newStepClock());
  const current = steps[index];
  const ended = index === steps.length - 1;
  const status = ended ? 'Interval validated' : index > 0 ? 'Islanded · market enabled' : 'Waiting for feeder assessment';

  useEffect(() => {
    if (!playing || ended) return undefined;
    const clock = stepClock.current;
    const startedAt = window.performance.now();
    const handle = window.setTimeout(() => {
      if (stepClock.current !== clock) return;
      stepClock.current = newStepClock();
      setIndex(index + 1);
      if (index + 1 === steps.length - 1) setPlaying(false);
    }, clock.remaining / speed);
    return () => {
      window.clearTimeout(handle);
      // Keep time in 1× units so pausing or changing speed only affects the time left.
      if (stepClock.current === clock) {
        clock.remaining = Math.max(0, clock.remaining - (window.performance.now() - startedAt) * speed);
      }
    };
  }, [index, playing, ended, speed]);

  function play() {
    if (playing) {
      setPlaying(false);
      return;
    }
    if (ended) {
      stepClock.current = newStepClock();
      setIndex(0);
    }
    setHasStarted(true);
    setPlaying(true);
  }
  function next() {
    stepClock.current = newStepClock();
    setPlaying(false);
    if (!hasStarted) {
      setHasStarted(true);
    } else if (!ended) {
      setIndex(index + 1);
    }
  }
  function reset() {
    stepClock.current = newStepClock();
    setPlaying(false);
    setHasStarted(false);
    setIndex(0);
  }

  return (
    <section className="gm-demo" aria-labelledby="gm-demo-title">
      <header className="gm-demo-heading">
        <div>
          <p className="gm-demo-eyebrow">GRIDMIND / SIMULATION LAB</p>
          <h1 id="gm-demo-title">Agent Demo</h1>
          <p className="gm-demo-intro">Follow one simulated outage interval from feeder failure to local energy settlement.</p>
        </div>
        <span className="gm-demo-pill">DEMO ONLY</span>
      </header>

      <div className="gm-demo-console" style={{ '--demo-flow-duration': `${750 / speed}ms` }}>
        <div className="gm-demo-console-head">
          <div>
            <p className="gm-demo-kicker">SCENARIO 01 <span>·</span> FICTIONAL LOCALITY</p>
            <h2>Eastbank feeder outage</h2>
          </div>
          <div className="gm-demo-interval"><span aria-hidden="true" />{status}</div>
        </div>

        <figure className="gm-demo-map" aria-label="Coordinator centered between Grid Reliability, Renewable and Storage, Demand and Priority, and P2P Market">
          <svg viewBox="0 0 800 266" preserveAspectRatio="none" className="gm-demo-links" aria-hidden="true">
            {links.map(([id, path]) => {
              const flowing = hasStarted && (current.agent === id || (ended && id === 'market'));
              return <g key={id}>
                <path d={path} className="gm-demo-link-base" />
                {flowing && <path d={path} className={`gm-demo-link-live ${playing ? 'is-playing' : ''}`}/>}
              </g>;
            })}
          </svg>
          {Object.entries(agents).map(([id, agent]) => {
            const active = hasStarted && current.agent === id;
            return <article key={id} className={`gm-demo-node gm-demo-node-${id} ${active ? 'is-active' : ''}`}>
              <span className="gm-demo-node-icon"><AgentIcon type={agent.icon}/></span>
              <div className="gm-demo-node-copy">
                <span>{agent.short}</span>
                <h3>{agent.name}</h3>
                <p>{agent.description}</p>
              </div>
            </article>;
          })}
        </figure>

        <p className="gm-demo-flow-caption"><span aria-hidden="true" /> Highlighted links show decisions flowing back to the Coordinator. This demo never starts or changes a real run.</p>
        <div className="gm-demo-controls">
          <div className="gm-demo-controls-buttons">
            <button className="gm-demo-play" type="button" onClick={play}>
              <span aria-hidden="true">{playing ? 'Ⅱ' : '▶'}</span> {playing ? 'Pause' : ended ? 'Replay demo' : hasStarted ? 'Resume' : 'Play demo'}
            </button>
            <button type="button" onClick={next} disabled={ended}>Next step</button>
            <button type="button" onClick={reset} disabled={!hasStarted}>Reset</button>
            <label className="gm-demo-speed">
              Playback speed
              <select value={speed} onChange={(event) => setSpeed(Number(event.target.value))}>
                {PLAYBACK_SPEEDS.map((rate) => <option key={rate} value={rate}>{rate}×</option>)}
              </select>
            </label>
          </div>
          <div className="gm-demo-progress" aria-label={`Step ${hasStarted ? index + 1 : 0} of ${steps.length}`}>
            {steps.map((s, i) => <i className={hasStarted && i <= index ? 'is-done' : ''} key={s.time}/>)}
            <span>{hasStarted ? index + 1 : 0} / {steps.length}</span>
          </div>
        </div>
      </div>

      <section className="gm-demo-trace" aria-labelledby="gm-demo-trace-title">
        <header>
          <div>
            <p className="gm-demo-eyebrow">OBSERVABILITY</p>
            <h2 id="gm-demo-trace-title">Decision log</h2>
          </div>
          <span>Simulated timestamps · no writes</span>
        </header>
        <div role="log" aria-live="polite" aria-label="Demo decision log">
          {!hasStarted ? <p className="gm-demo-empty">Play the demo or choose Next step to inspect the decisions one at a time.</p> :
            <ol>{steps.slice(0, index + 1).map((step) =>
              <li key={step.time}>
                <span className="gm-demo-time">+{step.time}s</span>
                <div><strong>{step.label}</strong><p>{step.detail}</p></div>
                <span className="gm-demo-agent-name">{agents[step.agent].name}</span>
              </li>
            )}</ol>}
        </div>
      </section>
    </section>
  );
}
