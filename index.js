/**
 * OpenCode adapter. One default export serves both plugin APIs: OpenCode 1
 * (1.4 and later) calls server() and OpenCode 2 calls setup(ctx). There is no
 * other export: an OpenCode 1 loader that walks every export would load a
 * second copy beside the default.
 */
import { guard, bounded, pluginRoot, runPython } from './scripts/harness_bridge.js';

const modelName = (model) => model?.providerID && (model?.modelID || model?.id)
  ? `${model.providerID}/${model.modelID || model.id}` : null;

// OpenCode triggers the hook with `{ args }` and then executes its own `args`
// binding, so replacing output.args changes nothing. Edit that object in place
// and report whether it now holds exactly the corrected input.
const applyInPlace = (target, updated) => {
  if (!target || typeof target !== 'object' || !updated || typeof updated !== 'object') return false;
  try {
    for (const key of Object.keys(target)) if (!Object.hasOwn(updated, key)) delete target[key];
    Object.assign(target, updated);
  } catch { return false; }
  const keys = Object.keys(updated);
  return Object.keys(target).length === keys.length && keys.every((key) => Object.is(target[key], updated[key]));
};

// OpenCode 1: native agents carry models; task has no model field.
const server = async (ctx) => {
  const root = pluginRoot(import.meta.url);
  const directory = ctx?.directory || ctx?.worktree || process.cwd();
  const parents = new Map();
  return {
    event: async ({ event }) => {
      const info = event?.properties?.info;
      if (event?.type === 'message.updated' && info?.sessionID) {
        const model = modelName(info.model) || modelName(info);
        if (model) {
          parents.delete(info.sessionID);
          parents.set(info.sessionID, model);
          if (parents.size > 1024) parents.delete(parents.keys().next().value);
        }
      }
      if (event?.type === 'session.deleted' && info?.id) parents.delete(info.id);
      if (event?.type === 'session.created' && process.env.LEOS_AGENT_PRICE_REFRESH !== 'off') {
        void runPython(root, 'opencode', 'pricing.py', ['refresh'], {}, 55000);
      }
    },
    'tool.execute.before': async (input, output) => {
      if (input?.tool !== 'task' || !output?.args) return;
      const response = await bounded(Promise.resolve().then(() => ctx?.client?.app?.agents?.()));
      const native_profiles = {};
      for (const agent of Array.isArray(response?.data) ? response.data : []) {
        native_profiles[agent.name] = { model: modelName(agent.model) };
      }
      const result = await guard(root, 'opencode', {
        tool_name: input.tool, tool_input: output.args, session_id: input.sessionID,
        call_id: input.callID, cwd: directory, parent_model: parents.get(input.sessionID), native_profiles,
      });
      if (result.action === 'block') throw new Error(`[leo routing] ${result.reason}. ${result.retry || ''}`);
      if (result.action === 'correct' && result.updated_input && !applyInPlace(output.args, result.updated_input)) {
        // An unapplied correction would run the uncorrected task: block instead.
        const agent = result.updated_input.subagent_type;
        throw new Error(`[leo routing] correction-not-applied. Retry with ${
          typeof agent === 'string' ? `subagent_type=${JSON.stringify(agent)}` : 'the corrected task arguments'}.`);
      }
    },
    // The task tool's return value is the child's final text. Its last 4 KiB
    // go to the observer for the Result/Verified tokens and are then dropped;
    // callID is the join key back to the dispatch row. Task metadata carries
    // the child session and the model the child ran on, but no agent: the
    // agent is the executed argument. A background task fires this hook at
    // launch, before the child has done anything, so it records nothing.
    // Never mutates output.
    'tool.execute.after': async (input, output) => {
      if (input?.tool !== 'task') return;
      const metadata = output?.metadata ?? {};
      if (metadata.background === true) return;
      const text = String(output?.output ?? '').slice(-4096);
      await bounded(runPython(root, 'opencode', 'observe_agent.py', [], {
        hook_event_name: 'SubagentStop', tool_name: 'task', session_id: input.sessionID, call_id: input.callID,
        agent: typeof input?.args?.subagent_type === 'string' ? input.args.subagent_type : null,
        agent_id: typeof metadata.sessionId === 'string' ? metadata.sessionId : null,
        child_model: modelName(metadata.model),
        result_text: text, outcome_source: 'tool-output', reason: 'opencode-tool-output',
      }), 3000);
    },
  };
};
// A bounded map that keeps the most recently set keys.
const remember = (map, key, value) => {
  map.delete(key);
  map.set(key, value);
  if (map.size > 1024) map.delete(map.keys().next().value);
};

// OpenCode 2 does not load `instructions` entries, so the policy goes in
// through the context hook, and leo-install leaves the skills to this plugin
// there. The policy is rendered once per session so the prompt prefix
// stays byte-identical within it. Hooks get one mutable event; a throw in
// execute.before fails the call with its message, and a replaced `input` is
// what runs.
const setup = async (ctx) => {
  const root = pluginRoot(import.meta.url);
  const directory = ctx?.location?.directory || process.cwd();
  const models = new Map(); // sessionID -> the model its latest request went to
  const policies = new Map(); // sessionID -> rendered policy; '' after a failed render
  // The skills leo-install copies on OpenCode 1, rendered once here; the
  // transform must stay synchronous. A skill OpenCode already found under that
  // id (a copy the user edited and kept) stays in place of the plugin's.
  const listed = await runPython(root, 'opencode', 'opencode_skills.py');
  let skills = [];
  try { skills = listed?.code === 0 ? JSON.parse(listed.stdout) : []; } catch { skills = []; }
  if (Array.isArray(skills) && skills.length && typeof ctx.skill?.transform === 'function') {
    await ctx.skill.transform((editor) => {
      for (const skill of skills) if (!editor.get(skill.id)) editor.add({ ...skill });
    });
  }
  await ctx.session.hook('context', async (event) => {
    const session = event?.sessionID;
    if (typeof session !== 'string') return;
    const model = modelName(event.model);
    if (model) remember(models, session, model);
    let policy = policies.get(session);
    if (policy === undefined) {
      const result = await runPython(root, 'opencode', 'emit_payload.py', [], {
        hook_event_name: 'SessionStart', session_id: session, cwd: directory,
      });
      policy = result?.code === 0 ? result.stdout.trim() : '';
      remember(policies, session, policy);
    }
    if (!policy || !Array.isArray(event.system)) return;
    // Should a later OpenCode 2 load the rendered `instructions` copy, send it once.
    if (event.system.some((part) => typeof part?.text === 'string' && part.text.includes(policy))) return;
    event.system.push({ type: 'text', text: policy });
  });
  await ctx.tool.hook('execute.before', async (event) => {
    if (event?.tool !== 'subagent' || !event.input || typeof event.input !== 'object') return;
    const response = await bounded(Promise.resolve().then(() => ctx.agent.list()));
    const native_profiles = {};
    for (const agent of Array.isArray(response?.data) ? response.data : []) {
      if (typeof agent?.id === 'string') native_profiles[agent.id] = { model: modelName(agent.model) };
    }
    const result = await guard(root, 'opencode', {
      tool_name: event.tool, tool_input: event.input, session_id: event.sessionID, call_id: event.id,
      cwd: directory, parent_model: models.get(event.sessionID), native_profiles,
    });
    if (result.action === 'block') throw new Error(`[leo routing] ${result.reason}. ${result.retry || ''}`);
    if (result.action === 'correct' && result.updated_input && typeof result.updated_input === 'object') {
      event.input = result.updated_input;
    }
  });
  // A foreground subagent's result carries the child's final text and session;
  // its last 4 KiB go to the observer and are dropped. A background one
  // completes later, outside this hook, so it records nothing. The child's
  // model is the one its own requests went to. Never mutates the event.
  await ctx.tool.hook('execute.after', async (event) => {
    if (event?.tool !== 'subagent' || event.status !== 'completed') return;
    const output = event.result?.output;
    if (!output || typeof output !== 'object' || output.status !== 'completed') return;
    const child = typeof output.sessionID === 'string' ? output.sessionID : null;
    await bounded(runPython(root, 'opencode', 'observe_agent.py', [], {
      hook_event_name: 'SubagentStop', tool_name: event.tool, session_id: event.sessionID, call_id: event.id,
      agent: typeof event.input?.agent === 'string' ? event.input.agent : null, agent_id: child,
      child_model: (child && models.get(child)) || null,
      result_text: String(output.output ?? '').slice(-4096), outcome_source: 'tool-output', reason: 'opencode-tool-output',
    }), 3000);
  });
  if (process.env.LEOS_AGENT_PRICE_REFRESH === 'off') return undefined;
  const controller = new AbortController();
  void (async () => {
    try {
      for await (const event of ctx.event.subscribe({ signal: controller.signal })) {
        if (event?.type === 'session.created') void runPython(root, 'opencode', 'pricing.py', ['refresh'], {}, 55000);
      }
    } catch { /* the stream ends with the plugin */ }
  })();
  return () => controller.abort();
};

export default { id: 'leos-agent', setup, server };
