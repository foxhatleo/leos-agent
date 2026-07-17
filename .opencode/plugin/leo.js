// Leo's OpenCode bridge: registers the leo:* skills dir, injects agents from
// this plugin's agents/*.md, bootstraps the using-leo policy into the first
// user message of a session, and guards Bash execution the same way the
// Claude Code harness does.
//
// Node builtins only, ESM. No external dependencies.

import { readFile, readdir } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawn } from 'node:child_process';

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');

const TIER_MODEL = {
  opus: process.env.LEO_MODEL_OPUS || 'openrouter/z-ai/glm-5.2',
  sonnet: process.env.LEO_MODEL_SONNET || 'openrouter/minimax/minimax-m3',
  haiku: process.env.LEO_MODEL_HAIKU || 'openrouter/deepseek/deepseek-v4-pro',
};

const LEO_POLICY_MARKER = '<leo-policy>';

// ---------------------------------------------------------------------------
// Tiny frontmatter parser — handles inline `key: value` lines and YAML folded
// (`>`) / literal (`|`) block scalars, which is all the agents/*.md files use.
// ---------------------------------------------------------------------------

function parseFrontmatter(raw) {
  const lines = raw.split('\n');
  if (!lines.length || lines[0].trim() !== '---') {
    return { attrs: {}, body: raw };
  }
  let end = -1;
  for (let i = 1; i < lines.length; i++) {
    if (lines[i].trim() === '---') {
      end = i;
      break;
    }
  }
  if (end === -1) {
    return { attrs: {}, body: raw };
  }
  const fmLines = lines.slice(1, end);
  const body = lines.slice(end + 1).join('\n').replace(/^\n+/, '');

  const attrs = {};
  let i = 0;
  while (i < fmLines.length) {
    const line = fmLines[i];
    const m = /^([A-Za-z0-9_-]+):\s*(.*)$/.exec(line);
    if (!m) {
      i++;
      continue;
    }
    const key = m[1];
    let val = m[2].trim();
    if (val === '>' || val === '|' || val === '>-' || val === '|-') {
      // Folded/literal block scalar: gather subsequent indented (or blank) lines.
      const parts = [];
      i++;
      while (i < fmLines.length && (fmLines[i].trim() === '' || /^\s/.test(fmLines[i]))) {
        if (fmLines[i].trim() !== '') parts.push(fmLines[i].trim());
        i++;
      }
      val = parts.join(' ');
    } else {
      i++;
    }
    attrs[key] = val;
  }
  return { attrs, body };
}

function tierFromModel(model) {
  if (typeof model !== 'string') return null;
  const m = model.trim();
  if (m === 'opus[1m]' || m === 'opus') return 'opus';
  if (m === 'sonnet[1m]' || m === 'sonnet') return 'sonnet';
  if (m === 'haiku') return 'haiku';
  return null;
}

function isReadOnly(toolsField) {
  const tools = typeof toolsField === 'string' ? toolsField : '';
  return !/\bWrite\b/.test(tools) && !/\bEdit\b/.test(tools);
}

// ---------------------------------------------------------------------------
// Bootstrap: using-leo policy body + OpenCode mapping, cached at module scope.
// ---------------------------------------------------------------------------

let bootstrapCache = null;

async function buildBootstrap() {
  const skillPath = path.join(REPO, 'skills', 'using-leo', 'SKILL.md');
  const rawSkill = await readFile(skillPath, 'utf8');
  const { body } = parseFrontmatter(rawSkill);
  const resolvedBody = body.replace(/\$\{CLAUDE_PLUGIN_ROOT\}/g, REPO).trim();

  const mappingPath = path.join(REPO, 'skills', 'using-leo', 'references', 'opencode-mapping.md');
  const mapping = (await readFile(mappingPath, 'utf8')).trim();

  const activeModels =
    'Active tier models: opus=' + TIER_MODEL.opus +
    ', sonnet=' + TIER_MODEL.sonnet +
    ', haiku=' + TIER_MODEL.haiku;

  return (
    LEO_POLICY_MARKER + '\n' +
    resolvedBody + '\n\n' +
    mapping + '\n' +
    activeModels + '\n' +
    '</leo-policy>'
  );
}

async function getBootstrap() {
  if (!bootstrapCache) {
    bootstrapCache = await buildBootstrap();
  }
  return bootstrapCache;
}


// ---------------------------------------------------------------------------
// Plugin
// ---------------------------------------------------------------------------

let guardWarnedOnce = false;

export default async function leoPlugin(_ctx) {
  return {
    async config(config) {
      config.skills ||= {};
      config.skills.paths ||= [];
      const skillsDir = path.resolve(REPO, 'skills');
      if (!config.skills.paths.includes(skillsDir)) {
        config.skills.paths.push(skillsDir);
      }

      config.agent ||= {};
      const agentsDir = path.join(REPO, 'agents');
      let files = [];
      try {
        files = (await readdir(agentsDir)).filter((f) => f.endsWith('.md'));
      } catch {
        files = [];
      }

      for (const file of files) {
        let raw;
        try {
          raw = await readFile(path.join(agentsDir, file), 'utf8');
        } catch {
          continue;
        }
        const { attrs, body } = parseFrontmatter(raw);
        const name = attrs.name;
        const model = attrs.model;
        if (!name || !model) continue;
        if (model.trim() === 'fable') continue; // expert (and any fable-tier agent) is dropped
        const tier = tierFromModel(model);
        if (!tier) continue;

        const readOnly = isReadOnly(attrs.tools);
        config.agent[name.toLowerCase()] = {
          description: attrs.description || '',
          mode: 'subagent',
          model: TIER_MODEL[tier],
          prompt: body,
          ...(readOnly
            ? { permission: { edit: 'deny' } }
            : {
                // Stopgap for opencode#5894 (tool.execute.before may not
                // intercept subagent tool calls): coarse denies on the
                // catastrophic rm class for write-capable agents. The
                // precise tripwire stays hooks/bash-guard.py; OpenCode's
                // own external_directory ask remains the outer layer.
                // No wildcard allow entry: unmatched commands keep OpenCode's
                // default behavior, so these denies hold regardless of
                // whether the resolver is most-specific-match or last-match.
                permission: {
                  bash: {
                    'rm -rf ~': 'deny',
                    'rm -rf ~/*': 'deny',
                    'rm -rf /': 'deny',
                    'rm -rf /*': 'deny',
                  },
                },
              }),
        };
      }

      return config;
    },

    // The system array is the reliable injection point: it takes plain
    // strings (no Part[] schema to satisfy) and this hook fires on every
    // request, so the policy also survives context compaction — same
    // property the Claude SessionStart matcher provides via `compact`.
    'experimental.chat.system.transform': async (input, output) => {
      const system = output && output.system;
      if (!Array.isArray(system)) return;
      // Dedupe: skip if the policy marker is already present in the array.
      if (system.some((s) => typeof s === 'string' && s.includes(LEO_POLICY_MARKER))) return;

      const bootstrap = await getBootstrap();
      if (bootstrap) system.push(bootstrap);
    },

    'tool.execute.before': async (input, output) => {
      if (!input || input.tool !== 'bash') return;
      const command = output && output.args && output.args.command;
      if (typeof command !== 'string' || !command) return;

      const cwd = (input && (input.directory || input.worktree || input.cwd)) || process.cwd();
      const payload = JSON.stringify({ tool_name: 'Bash', tool_input: { command }, cwd });
      const guardPath = path.join(REPO, 'hooks', 'bash-guard.py');

      let exitCode;
      let stderr = '';
      try {
        exitCode = await new Promise((resolve, reject) => {
          const proc = spawn('python3', [guardPath], { stdio: ['pipe', 'ignore', 'pipe'] });
          proc.stderr.on('data', (d) => {
            stderr += d.toString();
          });
          proc.on('error', reject);
          proc.on('close', (code) => resolve(code));
          proc.stdin.write(payload);
          proc.stdin.end();
        });
      } catch (err) {
        if (!guardWarnedOnce) {
          guardWarnedOnce = true;
          console.error('[leo guard] infra failure spawning bash-guard.py, allowing command:', err && err.message ? err.message : err);
        }
        return; // infra fail-open
      }

      if (exitCode === 2) {
        throw new Error(stderr.trim() || '[leo guard] blocked');
      }
    },
  };
}
