// Runs the JavaScript of ONE n8n Code node (runOnceForAllItems) offline, with the n8n helpers it uses stubbed from a JSON spec on stdin:
//   { code, input: [{json, binary?}], nodes: { "Node name": { items: [...], executed: true } }, runIndex, executionId }
// Prints {ok:true, result:[...]} or {ok:false, error:"..."}. No network, no n8n. Used only by tests/test_n8n_report_workflows.py.
const chunks = [];
process.stdin.on('data', c => chunks.push(c));
process.stdin.on('end', async () => {
  const spec = JSON.parse(Buffer.concat(chunks).toString('utf8'));
  const input = spec.input || [];
  const nodes = spec.nodes || {};
  const ref = name => {
    const n = nodes[name] || { items: [], executed: false };
    return { first: () => n.items[0], last: () => n.items[n.items.length - 1], all: () => n.items, isExecuted: !!n.executed, item: n.items[0] };
  };
  const $input = { first: () => input[0], last: () => input[input.length - 1], all: () => input };
  const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
  try {
    const fn = new AsyncFunction('$input', '$', '$runIndex', '$execution', '$json', spec.code);
    const out = await fn($input, ref, spec.runIndex || 0, { id: spec.executionId || '123' }, input[0] ? input[0].json : {});
    process.stdout.write(JSON.stringify({ ok: true, result: out }));
  } catch (e) {
    process.stdout.write(JSON.stringify({ ok: false, error: String(e && e.message || e) }));
  }
});
