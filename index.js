/** OpenCode adapter: native agents carry models; task has no model field. */
import { fileURLToPath } from 'node:url';
import { guard, bounded, runPython } from './scripts/harness_bridge.js';

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

export const LeosAgent = async (ctx) => {
  const root = process.env.LEOS_AGENT_ROOT || process.env.PLUGIN_ROOT || fileURLToPath(new URL('.', import.meta.url));
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
export default LeosAgent;
