/**
 * OpenCode plugin entry point for leos-agent.
 *
 * Deliberately does nothing at runtime. OpenCode's plugin API registers tools
 * and hooks, not skills or commands, and the payload is installed to disk on
 * demand rather than written at session start. This export exists so that
 * `opencode plugin leos-agent -g` resolves and loads cleanly; the real work
 * happens in scripts/leo-install.py, which the installed /leo-install command runs.
 */
export const LeosAgent = async () => ({});

export default LeosAgent;
