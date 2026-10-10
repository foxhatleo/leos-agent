"""Pure dispatch routing shared by harness adapters.

Capability comes from a supported interface, never from the presence of config.
No network, prompts in storage, or guessed model IDs on this path.
"""
import copy

import pricing
import routing

CAPABILITIES = {
    "claude": {"tools": ("Agent", "Task"), "model_field": "model", "rewrite": True, "profiles": True},
    # Multi-agent v2 namespaces its tools, and Codex hooks see namespace and
    # name joined with no separator: collaboration + spawn_agent.
    "codex": {"tools": ("spawn_agent", "collaborationspawn_agent"), "model_field": "model", "rewrite": False,
              "profiles": True},
    "cursor": {"tools": ("Task", "task"), "model_field": None, "rewrite": False, "profiles": True},
    "opencode": {"tools": ("task",), "model_field": None, "rewrite": True, "profiles": True},
    "hermes": {"tools": ("delegate_task",), "model_field": None, "rewrite": False, "profiles": False},
    "pi": {"tools": ("subagent",), "model_field": None, "rewrite": False, "profiles": False},
}
PROFILE_TIERS = {"leo-cheap": "cheap", "leo-standard": "standard", "leo-premium": "premium", "leo-parent": "parent",
                 "leo-runner": "cheap", "leo-executor": "standard", "leo-reviewer": "standard", "leo-lens": "standard"}
# review-pr's read-only lens. Every other profile can edit, so no guard path
# moves a lens to one: where its model cannot be held within the ceiling, the
# guard refuses it and the reviewer covers that area itself (procedure.md).
LENS = "leo-lens"
LENS_REFUSED = ("leo-lens's own model is over the parent's price, and this dispatch cannot name a cheaper one. "
                "Review that area yourself rather than retrying it on a writable profile.")

# Claude's Agent `model` enum. A family alias under a parent of that family
# runs on the parent's exact model, so a parent's own alias is its ceiling.
CLAUDE_ALIASES = routing.CLAUDE_AGENT_MODELS
# Built-in Claude agent types whose definition names no model or `inherit`,
# so an omitted `model` runs them on the parent. Any other agent may pin a
# model in a definition the guard cannot see, and the guard leaves it alone.
CLAUDE_INHERITING = frozenset(("general-purpose", "claude", "Explore", "Plan"))
# Those whose definition names no model at all (an omitted subagent_type is
# general-purpose). CLAUDE_CODE_SUBAGENT_MODEL is their default; Explore and
# Plan say `inherit`, and a definition's `inherit` outranks the setting.
CLAUDE_UNPINNED = frozenset(("general-purpose", "claude"))
# A fork always runs on the parent's model and shares its prompt cache;
# Claude ignores `model` for it.
CLAUDE_FORK = "fork"


CONFIG_INVALID = "routing-config-invalid"


def load_config():
    """(config, error). An invalid routing.json must not switch routing off.

    Each harness section that validates on its own is kept; the rest fall back
    to defaults. error is what made the file invalid, or None when it loaded whole.
    """
    try:
        return routing.load(), None
    except (ValueError, OSError) as exc:
        error = exc
    config = {}
    try:
        raw = routing.read_raw()
    except (ValueError, OSError):
        raw = None
    for harness, entry in (raw.items() if isinstance(raw, dict) else ()):
        try:
            config.update(routing.validate({harness: entry}))
        except (ValueError, OSError):
            continue
    return config, error


def hermes_spawn(args):
    """Hermes's own reading of delegate_task: (action or "").strip().lower(),
    where empty means spawn. A truthy non-string never spawns there."""
    action = args.get("action") or ""
    return isinstance(action, str) and action.strip().lower() in ("", "spawn")


def own_profile(agent):
    """Our profile's bare name, or None for any other agent."""
    if not isinstance(agent, str):
        return None
    # Only our namespace is trusted. another-plugin:leo-cheap is not ours.
    if agent.startswith("leos-agent:"):
        agent = agent[len("leos-agent:"):]
    return agent if agent in PROFILE_TIERS else None


def tier_for(agent):
    return PROFILE_TIERS.get(own_profile(agent))


def dispatch_tool(harness, tool):
    """Whether `tool` is this harness's dispatch tool. Adapters ask first, so an
    ordinary tool call never pays for a transcript read or the price catalog."""
    cap = CAPABILITIES.get(harness)
    return cap is not None and tool in cap["tools"]


def _claude_alias(parent):
    """The parent's own family alias, or None when its ID names no family.

    Claude runs a family alias on the parent's exact model when the parent is
    of that family, so this alias is the parent itself, not the newest release.
    """
    identity = pricing.identity(parent)
    alias = identity[1] if identity and identity[0] == "claude" else None
    return alias if alias in CLAUDE_ALIASES else None


def _claude(result, args, agent, tier, requested, parent, config, catalog, effective_model, default_model=None):
    """Claude's native order: the call's `model`, then the agent definition,
    then the default subagent model, then the parent. The guard fills `model`
    only where the child would run on the parent or is ours, and caps it at the parent."""
    if agent == CLAUDE_FORK:
        result.update(reason="fork-inherits-parent", effective_model=parent)
        return result
    if effective_model:
        # A forced setting decides the child; only its price is reported.
        selected = parent if effective_model == "inherit" else effective_model
        result["effective_model"] = selected
        result["price"] = pricing.compare(selected, parent, catalog) if selected and parent else None
        status = (result["price"] or {}).get("status")
        result["reason"] = ("parent-model-unavailable" if not parent else "over-ceiling" if status == "over-ceiling"
                            else "within-ceiling" if status == "allowed" else "price-unknown")
        return result
    # The default subagent model, not the parent, runs an agent whose
    # definition names no model, so omitting `model` there inherits nothing.
    setting = default_model if agent is None or (isinstance(agent, str) and agent in CLAUDE_UNPINNED) else None
    # leo-lens is defined as `inherit` and gets its tier from the call, so an
    # over-ceiling lens model with no alias to cap it at is dropped, not refused.
    inherits = not setting and (agent is None or (isinstance(agent, str) and agent in CLAUDE_INHERITING)
                                or tier == "parent" or own_profile(agent) == LENS)
    if requested:
        selected = requested
    elif tier and tier != "parent":
        selected = routing.tier_model("claude", tier, config, parent)
    elif setting:
        # The user's own default decides this child; only its price is checked.
        selected = setting
    elif inherits and tier != "parent" and parent:
        # Standard is the default for unspecified nontrivial work. A hook
        # cannot infer complexity from the brief or safely choose cheap.
        selected = routing.tier_model("claude", "standard", config, parent)
    else:
        selected = None
    if selected == "inherit":
        selected, inherits = None, True
    if not selected:
        # Parent-level, an unknown parent, or an agent that picks its own model:
        # the definition decides, and for these it is the parent or unknowable.
        reason = ("inherits-parent" if inherits and parent else
                  "parent-model-unavailable" if inherits else "agent-defined-model")
        result.update(reason=reason, effective_model=parent if inherits else None)
        return result
    result["effective_model"] = selected
    result["price"] = pricing.compare(selected, parent, catalog) if parent else None
    status = (result["price"] or {}).get("status")
    if status == "over-ceiling":
        alias = _claude_alias(parent)
        updated = copy.deepcopy(args)
        if alias:
            updated["model"] = alias
        elif inherits:
            # Omitting the model inherits the parent exactly, whatever its ID.
            updated.pop("model", None)
        else:
            # Omitting the model here would fall back to the agent's own
            # definition or the default subagent model, not the parent, so
            # the ceiling cannot be applied.
            chosen = "The selected model" if requested or not setting else "CLAUDE_CODE_SUBAGENT_MODEL"
            result.update(action="block", reason="unsupported-native-model",
                          retry=chosen + " is over the parent, and the parent's model has no Agent alias to cap it at. "
                                "Use leos-agent:leo-parent, which inherits the parent, or do the work locally.")
            return result
        result["effective_model"] = parent
        if updated == args:
            result["reason"] = "inherits-parent"
        else:
            result.update(action="correct", reason="over-ceiling", updated_input=updated)
        return result
    if requested:
        result["reason"] = ("parent-model-unavailable" if not parent else
                            "within-ceiling" if status == "allowed" else "price-unknown")
        return result
    if setting:
        result["reason"] = "subagent-model-setting"
        return result
    if selected not in CLAUDE_ALIASES:
        # routing.py refuses these; never send Agent a value outside its enum.
        result.update(reason="unsupported-native-model", effective_model=None)
        return result
    updated = copy.deepcopy(args)
    updated["model"] = selected
    result.update(action="correct", reason="explicit-tier-default", updated_input=updated)
    return result


def route(harness, tool, args, parent=None, config=None, catalog=None, effective_model=None, native_profiles=None,
          default_model=None):
    """Return an advisory decision; adapters implement only native controls.

effective_model is supplied by an adapter only when native profile precedence
or global settings determine the actual requested child model. default_model is
Claude's unforced CLAUDE_CODE_SUBAGENT_MODEL, or None when it is unset: the
model an agent whose definition names none runs on when `model` is omitted.
"""
    cap = CAPABILITIES.get(harness)
    result = {"action": "allow", "reason": "not-a-dispatch", "updated_input": None,
              "requested_model": None, "effective_model": None, "price": None}
    if cap is None or tool not in cap["tools"] or not isinstance(args, dict):
        return result
    if harness == "hermes" and not hermes_spawn(args):
        return result
    if config is None:
        config, error = load_config()
        if error is not None:
            result["diagnostic"] = CONFIG_INVALID
    catalog = pricing.load() if catalog is None else catalog
    agent = args.get("subagent_type") or args.get("agent_type") or args.get("agent") or args.get("profile")
    tier = tier_for(agent)
    native_profiles = native_profiles or {}
    if harness == "opencode":
        native = native_profiles.get(agent)
        if isinstance(native, dict):
            effective_model = native.get("model") or parent
        if agent == "general" and "leo-standard" in native_profiles:
            # Select only an agent the running harness confirms exists.
            updated = copy.deepcopy(args)
            updated["subagent_type"] = "leo-standard"
            decision = route(harness, tool, updated, parent, config, catalog,
                             native_profiles=native_profiles)
            if decision["action"] != "block":
                decision.update(action="correct", updated_input=decision["updated_input"] or updated)
            if "diagnostic" in result:
                decision["diagnostic"] = result["diagnostic"]
            return decision
    field = cap["model_field"]
    requested = args.get(field) if field else None
    requested = requested if isinstance(requested, str) and requested.strip() else None
    result["requested_model"] = requested
    if harness == "claude":
        return _claude(result, args, agent, tier, requested, parent, config, catalog, effective_model, default_model)
    if harness == "codex" and tier and not effective_model:
        expected = routing.tier_model(harness, tier, config, parent)
        comparison = pricing.compare(expected, parent, catalog) if expected and parent else None
        if comparison and comparison["status"] == "over-ceiling":
            expected = parent
        entry = routing.profile(config, harness, tier) or {}
        effort = entry.get("effort") if expected != parent else None
        result.update(effective_model=expected, price=comparison)
        if expected and (requested != expected or (effort and args.get("reasoning_effort") != effort)):
            retry = f"Retry this profile with model={expected!r}"
            if effort:
                retry += f" and reasoning_effort={effort!r}"
            result.update(action="block", reason="tier-selection-required",
                          retry=retry + ". Select a different tier explicitly if the work requires it.")
            return result
    selected = effective_model or requested
    if tier and not selected and harness not in ("cursor", "opencode", "hermes", "pi"):
        selected = routing.tier_model(harness, tier, config, parent)
    if not selected:
        if field and parent:
            # Standard is the default for unspecified nontrivial work. A hook
            # cannot infer complexity from the brief or safely choose cheap.
            selected = routing.tier_model(harness, "standard", config, parent)
        elif field:
            # The child inherits a parent this hook cannot see; naming a tier
            # here could only upgrade it.
            result["reason"] = "parent-model-unavailable"
            return result
        elif harness in ("hermes", "pi", "cursor"):
            result["reason"] = "per-dispatch-routing-unavailable"
            return result
        else:
            result["reason"] = "unconfigured-native-profile"
            return result
    if selected == "inherit":
        selected = parent
    result["effective_model"] = selected
    if selected and parent:
        result["price"] = pricing.compare(selected, parent, catalog)
    over = result["price"] is not None and result["price"]["status"] == "over-ceiling"
    if over:
        # Use the parent directly: it is already competent for the task and
        # avoids replacing a standard task with an unqualified cheap worker.
        selected = parent
        result["effective_model"] = selected
    needs_model = selected and field and (not requested or over)
    lens = own_profile(agent) == LENS
    if effective_model and over and harness == "codex":
        result.update(action="block", reason="profile-over-ceiling", retry=LENS_REFUSED if lens else
                      "Use a model-routed spawn without the overriding native profile, at the current parent model.")
        return result
    if needs_model:
        if cap["rewrite"]:
            updated = copy.deepcopy(args)
            updated[field] = selected
            result.update(action="correct", reason="over-ceiling" if over else "explicit-tier-default",
                          updated_input=updated)
        else:
            result.update(action="block", reason="over-ceiling" if over else "missing-explicit-model",
                          retry=f"Retry with {field}={selected!r} using this harness's supported spawn fields.")
    elif over:
        parent_profile = native_profiles.get("leo-parent")
        if lens:
            # leo-parent can edit, so swapping a lens to it would drop `edit: deny`.
            result.update(action="block", reason="native-profile-over-ceiling", retry=LENS_REFUSED)
        elif harness == "opencode" and tier and isinstance(parent_profile, dict) and not parent_profile.get("model"):
            updated = copy.deepcopy(args)
            updated["subagent_type"] = "leo-parent"
            result.update(action="correct", reason="native-profile-over-ceiling", updated_input=updated)
        elif harness == "hermes":
            # Hermes has no native agents and delegate_task takes no model:
            # every delegate runs on delegation.model, or inherits the parent
            # when that is unset.
            result.update(action="block", reason="native-profile-over-ceiling", retry=(
                "Hermes runs every delegate on delegation.model in config.yaml, which is over the parent, and "
                "delegate_task cannot name another model. Do this work in the current session, or ask the user to "
                "set delegation.model within the parent's price, or unset it so delegates inherit the parent."))
        else:
            result.update(action="block", reason="native-profile-over-ceiling",
                          retry="Use the parent-level native agent, or perform the work in the current parent.")
    else:
        result["reason"] = "parent-model-unavailable" if not parent else "price-unknown" if result["price"] is None or result["price"]["status"] == "unknown" else "within-ceiling"
    return result
