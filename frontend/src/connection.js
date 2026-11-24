export function connectionNotice(error, cloud, createdRun = false) {
  if (error?.kind === 'demo-expired') return {
    title: 'Temporary demo expired', detail: 'Open an updated GRIDMIND deployment to continue.',
  };
  if (error?.kind === 'cloud-config') return {
    title: 'Deployment configuration error', detail: 'Contact the GRIDMIND deployment owner.',
  };
  if (error?.status === 403) return {
    title: 'Access denied', detail: 'This account does not have permission to load GRIDMIND data.',
  };
  if (error?.status === 401 || error?.kind === 'sign-in') return cloud ? {
    title: 'Connect to GRIDMIND', detail: 'Sign in with an approved account to continue.', action: 'sign-in',
  } : {
    title: 'Connect to GRIDMIND', detail: 'Enter the local API key to continue.', action: 'api-key',
  };
  if(error?.status===429) return {
    title: createdRun ? 'Run created; updates unavailable' : 'GRIDMIND is busy',
    detail: createdRun ? 'GRIDMIND confirmed the run. Wait a moment for its latest progress.' : 'Wait a moment before retrying the connection.',
    action: 'retry',
  };
  return createdRun ? {
    title: 'Run created; updates unavailable',
    detail: 'GRIDMIND confirmed the new run. Reconnect to see its latest progress.',
    action: 'retry',
  } : {
    title: 'Connection unavailable', detail: 'Could not load current GRIDMIND data. Retry the connection.', action: 'retry',
  };
}

export function runStartError(error) {
  if (error?.kind === 'demo-expired') return 'This temporary demo expired before the run could be requested.';
  if (error?.kind === 'cloud-config') return 'The deployment configuration does not allow starting a run.';
  if (error?.status === 401) return 'Sign in before starting a simulation.';
  if (error?.status === 403) return 'This account does not have permission to start a simulation.';
  if (error?.status === 409) return 'A run could not be started. Check Run History for an active run in this locality.';
  if ([400, 422].includes(error?.status)) return 'GRIDMIND rejected the simulation settings. Check the configuration and try again.';
  // A lost response or proxy/server failure does not prove the write was rejected.
  return 'Could not confirm whether the run started. Check Run History before submitting again.';
}
