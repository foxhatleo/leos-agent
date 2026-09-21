/** Pi loads policy once per session; subagent tools depend on installed extensions. */
import { fileURLToPath } from 'node:url';
import { guard, runPython } from './scripts/harness_bridge.js';

export default function (pi) {
  const root = process.env.LEOS_AGENT_ROOT || process.env.PLUGIN_ROOT || fileURLToPath(new URL('.', import.meta.url));
  let cached = null;
  pi.on('session_start', async () => { cached = null; });
  pi.on('before_agent_start', async (event) => {
    if (cached === null) {
      const result = await runPython(root, 'pi', 'emit_payload.py', [], { hook_event_name: 'SessionStart' });
      if (result?.code === 0 && result.stdout.trim()) cached = result.stdout.trim();
    }
    if (cached) return { systemPrompt: `${event.systemPrompt}\n\n${cached}` };
  });
  pi.on('tool_call', async (event, ctx) => {
    if (event.toolName !== 'subagent') return;
    const result = await guard(root, 'pi', {
      tool_name: event.toolName, tool_input: event.input, toolCallId: event.toolCallId,
      parent_model: ctx?.model?.id, cwd: ctx?.cwd,
    });
    if (result.action === 'block') return { block: true, reason: result.retry || result.reason };
  });
  pi.on('tool_result', async (event) => {
    if (event.toolName !== 'subagent') return;
    // Text parts only, last 4 KiB, classified and dropped by the observer.
    // Returns nothing: this never alters Pi's tool result.
    const text = (Array.isArray(event.content) ? event.content : [])
      .filter((part) => part?.type === 'text' && typeof part.text === 'string')
      .map((part) => part.text).join('\n').slice(-4096);
    await runPython(root, 'pi', 'observe_agent.py', [], {
      hook_event_name: 'SubagentStop', tool_name: 'subagent', toolCallId: event.toolCallId,
      agent: event.input?.agent ?? event.input?.subagent_type ?? null,
      result_text: text, status: event.isError ? 'error' : null,
      usage: event.usage ?? null, reason: 'pi-tool-result',
    });
  });
  // package.json's pi.skills is the single skill discovery path.
}
