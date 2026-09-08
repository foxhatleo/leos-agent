/** OpenCode adapter: native agents carry models; task has no model field. */
import { fileURLToPath } from 'node:url';
import { guard, bounded, runPython } from './scripts/harness_bridge.js';

const modelName = (model) => model?.providerID && (model?.modelID || model?.id)
  ? `${model.providerID}/${model.modelID || model.id}` : null;

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
      if (result.action === 'correct' && result.updated_input) output.args = result.updated_input;
    },
  };
};
export default LeosAgent;
