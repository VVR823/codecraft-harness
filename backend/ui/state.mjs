export function renderText(node, value) { node.textContent = String(value ?? ''); }
export function createSelection(fetchRun, render) {
  let generation = 0;
  return {async load(id) {
    const version = ++generation;
    const run = await fetchRun(id);
    if (version === generation) render(run);
  }};
}
export function canResume(run) {
  return !run.legacy && !run.active && !run.unknown_calls && ['paused', 'budget_paused','running'].includes(run.status);
}
export function resumePayload(run, value) {
  if (run.status !== 'budget_paused') return {};
  const new_budget = Number(value);
  if (!Number.isInteger(new_budget) || new_budget <= run.known_tokens || new_budget > 1000000) throw Error('新预算须大于已用量，且不超过 1,000,000');
  return {new_budget};
}
