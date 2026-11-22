import React, {useEffect, useRef, useState} from 'react';

const messages = {
  loading: 'Requesting an explanation…',
  pending: 'This explanation is still being generated. Check again in a moment.',
  unavailable: 'The explanation couldn’t be generated. The simulation report is still available.',
  disabled: 'Run explanations are turned off for this deployment.',
  incomplete: 'This run isn’t complete yet. Try again after it finishes.',
  unauthorized: 'Sign in or reconnect before requesting an explanation.',
  error: 'Couldn’t load the explanation. The simulation report is still available.',
};
const retryLabels = {
  pending: 'Check again', incomplete: 'Try again', unauthorized: 'Try again', error: 'Try again',
};

function normalize(result) {
  switch (result?.status) {
    case 'ready':
      return typeof result.explanation === 'string' && result.explanation.trim()
        ? {status: 'ready', text: result.explanation}
        : {status: 'error'};
    case 'pending':
    case 'unavailable':
    case 'disabled':
      return {status: result.status};
    default:
      return {status: 'error'};
  }
}

export function useRunExplanations(requestExplanation) {
  const [explanations, setExplanations] = useState(() => new Map());
  const requests = useRef(new Map());

  function dismiss(id) {
    const controller = requests.current.get(id);
    requests.current.delete(id);
    controller?.abort();
    setExplanations(previous => {
      const next = new Map(previous);
      next.delete(id);
      return next;
    });
  }

  async function request(id) {
    if (requests.current.has(id)) return;
    const controller = new AbortController();
    requests.current.set(id, controller);
    setExplanations(previous => new Map(previous).set(id, {status: 'loading'}));
    let result;
    try {
      result = normalize(await requestExplanation(id, controller.signal));
    } catch (error) {
      result = {status: error?.status === 409 ? 'incomplete' : [401, 403].includes(error?.status) ? 'unauthorized' : 'error'};
    }
    // A closed or unmounted disclosure must not display an older result.
    if (requests.current.get(id) !== controller) return;
    requests.current.delete(id);
    setExplanations(previous => new Map(previous).set(id, result));
  }

  useEffect(() => () => {
    for (const controller of requests.current.values()) controller.abort();
    requests.current.clear();
  }, []);

  return {explanations, request, dismiss};
}

export function RunExplanation({state, id, retry}) {
  const {status, text} = state;
  const retryLabel = retryLabels[status];
  const detail = useRef(null);
  return <section id={id} ref={detail} tabIndex={-1} className="gm-run-explanation" aria-labelledby={id+'-title'}>
    <h3 id={id+'-title'}>Run explanation</h3>
    <div role="status" aria-atomic="true">
      {status === 'ready' ? <>
        <p className="gm-run-explanation-note">AI-generated from saved run results. Use the simulation report for exact figures.</p>
        <p className="gm-run-explanation-text">{text}</p>
      </> : <p>{messages[status]}</p>}
    </div>
    {retryLabel && <button type="button" className="gm-record-button small" onClick={()=>{detail.current?.focus(); retry();}}>{retryLabel}</button>}
  </section>;
}
