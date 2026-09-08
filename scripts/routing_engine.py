"""Pure dispatch routing shared by harness adapters.

Capability comes from a supported interface, never from the presence of config.
No network, prompts in storage, or guessed model IDs on this path.
"""
import copy

import pricing
import routing

CAPABILITIES = {
    "claude": {"tools": ("Agent", "Task"), "model_field": "model", "rewrite": True, "profiles": True},
    "codex": {"tools": ("spawn_agent",), "model_field": "model", "rewrite": False, "profiles": True},
    "cursor": {"tools": ("Task", "task"), "model_field": None, "rewrite": False, "profiles": True},
    "opencode": {"tools": ("task",), "model_field": None, "rewrite": True, "profiles": True},
    "hermes": {"tools": ("delegate_task",), "model_field": None, "rewrite": False, "profiles": False},
    "pi": {"tools": ("subagent",), "model_field": None, "rewrite": False, "profiles": False},
}
PROFILE_TIERS = {"leo-cheap": "cheap", "leo-standard": "standard", "leo-parent": "parent",
                 "leo-runner": "cheap", "leo-executor": "standard", "leo-reviewer": "standard"}


def tier_for(agent):
    if not isinstance(agent, str):
        return None
    # Only our namespace is trusted. another-plugin:leo-cheap is not ours.
    if agent.startswith("leos-agent:"):
        agent = agent[len("leos-agent:"):]
    return PROFILE_TIERS.get(agent)


def route(harness, tool, args, parent=None, config=None, catalog=None, effective_model=None, native_profiles=None):
    """Return an advisory decision; adapters implement only native controls.

effective_model is supplied by an adapter only when native profile precedence
or global settings determine the actual requested child model.
"""
    cap = CAPABILITIES.get(harness)
    result = {"action": "allow", "reason": "not-a-dispatch", "updated_input": None,
              "requested_model": None, "effective_model": None, "price": None}
    if cap is None or tool not in cap["tools"] or not isinstance(args, dict):
        return result
    if harness == "hermes" and args.get("action", "spawn") != "spawn":
        return result
    config = routing.load() if config is None else config
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
            return decision
    field = cap["model_field"]
    requested = args.get(field) if field else None
    requested = requested if isinstance(requested, str) and requested.strip() else None
    result["requested_model"] = requested
    selected = effective_model or requested
    if tier and not selected:
        selected = routing.tier_model(harness, tier, config, parent)
    if not selected:
        if field:
            # Standard is the default for unspecified nontrivial work. A hook
            # cannot infer complexity from the brief or safely choose cheap.
            selected = routing.tier_model(harness, "standard", config, parent)
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
    if effective_model and over and harness == "codex":
        result.update(action="block", reason="profile-over-ceiling",
                      retry="Use a model-routed spawn without the overriding native profile, at the current parent model.")
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
        if harness == "opencode" and tier and isinstance(parent_profile, dict) and not parent_profile.get("model"):
            updated = copy.deepcopy(args)
            updated["subagent_type"] = "leo-parent"
            result.update(action="correct", reason="native-profile-over-ceiling", updated_input=updated)
        else:
            result.update(action="block", reason="native-profile-over-ceiling",
                          retry="Use the parent-level native agent, or perform the work in the current parent.")
    else:
        result["reason"] = "price-unknown" if result["price"] is None or result["price"]["status"] == "unknown" else "within-ceiling"
    return result
