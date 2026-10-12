"""Guard engine - evaluate whether a tool call is allowed in the current step."""

import json
import os
import re
import sys
from pathlib import Path

from . import event_store, state_machine

# Standard keys that carry executable commands or scripts in MCP and CLI tool inputs
COMMAND_KEYS = ("command", "cmd", "script", "query", "eval", "code")


def _approved_commands(project_dir: str | None) -> set[str]:
    """Per-run pre-accept allowlist: exact commands an operator vetted for THIS run.
    Written to <project>/.claude/weft/approved.json (a JSON list of strings) by the
    dashboard when a human approves a handed_back finding's command. Empty/absent =
    nothing pre-approved (default-deny stays fully in force)."""
    base = project_dir or os.environ.get("CLAUDE_PROJECT_DIR", ".")
    try:
        data = json.loads((Path(base) / ".claude" / "weft" / "approved.json").read_text())
        return {str(c).strip() for c in data if str(c).strip()}
    except (OSError, json.JSONDecodeError, TypeError):
        return set()


def _extract_command_and_target(tool_name: str, tool_input: dict) -> tuple[str, str | None]:
    """Extract command text or target file from hook input.

    Inspects Bash, Edit/Write, and MCP tool invocations (mcp__*, ctx_execute, etc.).
    """
    if not isinstance(tool_input, dict):
        return "", None

    # Standard tool types
    if tool_name == "Bash":
        return tool_input.get("command", ""), None
    if tool_name in ("Edit", "Write"):
        return tool_input.get("file_path", ""), tool_input.get("file_path")

    # MCP tool invocations (e.g. mcp__* or custom execute tools)
    if tool_name.startswith("mcp__") or "execute" in tool_name.lower():
        # Look for direct command strings
        for k in COMMAND_KEYS:
            val = tool_input.get(k)
            if isinstance(val, str) and val.strip():
                return val, None

        # Look for file path edits through MCP tools
        if "file_path" in tool_input and isinstance(tool_input["file_path"], str):
            return tool_input["file_path"], tool_input["file_path"]

    return "", None


def _log_refusal(tool_name: str, command: str, reason: str, rule: str, project_dir: str | None = None) -> None:
    """Record refusal telemetry to agentry events stream if configured."""
    # 1. Weft event store (local project events.jsonl)
    # Recorded in evaluate() via event_store.append("wf.guard_blocked")

    # 2. Agentry events stream (~/.local/state/agentry/events.jsonl)
    # Reads build_id / task from AGENT_P_TAGS environment variable if present
    agentry_dir = Path.home() / ".local" / "state" / "agentry"
    agent_p_tags = os.environ.get("AGENT_P_TAGS", "")
    
    # Extract build_id and task from AGENT_P_TAGS (e.g., 'build_id=xyz,task=abc')
    tags = {}
    if agent_p_tags:
        for part in agent_p_tags.split(","):
            if "=" in part:
                k, v = part.split("=", 1)
                tags[k.strip()] = v.strip()

    payload = {
        "event": "guard",
        "tool": tool_name,
        "refused_input": command,
        "rule": rule,
        "reason": reason,
        "build_id": tags.get("build_id"),
        "task": tags.get("task"),
    }

    try:
        agentry_dir.mkdir(parents=True, exist_ok=True)
        log_file = agentry_dir / "events.jsonl"
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload) + "\n")
    except OSError:
        pass


def evaluate(hook_input: dict, project_dir: str | None = None) -> dict | None:
    """Check if the tool call is blocked by a guard from a non-current step.

    Returns None if allowed, or a dict with {blocked: True, reason: str} if blocked.
    """
    state = state_machine.load_state(project_dir)
    if not state or state.get("status") != "running":
        return None

    tool_name = hook_input.get("tool_name", "")
    tool_input = hook_input.get("tool_input", {})
    command, _ = _extract_command_and_target(tool_name, tool_input)

    if not command:
        return None

    # Pre-accept escape hatch: an operator-vetted exact command bypasses every guard
    # for this one run. Exact-match only - if the agent deviates from the approved
    # string at all, it stays blocked. This is the sole path a destructive/publish
    # command reaches the system, and it requires a human's explicit approval.
    if (tool_name == "Bash" or tool_name.startswith("mcp__")) and command.strip() in _approved_commands(project_dir):
        return None

    current_id = state["current_step"]

    for step in state["steps"]:
        if step["id"] == current_id:
            continue
        for guard in step.get("guards", []):
            pattern = guard if isinstance(guard, str) else guard.get("command_pattern", guard.get("pattern", ""))
            if not pattern:
                continue
            try:
                if re.search(pattern, command):
                    step_name = step["name"]
                    current_name = state["steps"][current_id]["name"]
                    msg = f"Blocked: command matches guard for step '{step_name}' " \
                          f"(current step: '{current_name}')"
                    if isinstance(guard, dict) and guard.get("message"):
                        msg = guard["message"]
                    event_store.append(
                        "wf.guard_blocked",
                        {"workflow_id": state["workflow_id"], "step_id": step["id"],
                         "step_name": step_name, "pattern": pattern,
                         "command": command, "tool": tool_name},
                        session_id=state.get("session_id", "unknown"),
                        workflow_id=state["workflow_id"],
                        project_dir=project_dir,
                    )
                    _log_refusal(tool_name, command, msg, pattern, project_dir)
                    return {"blocked": True, "reason": msg}
            except re.error as e:
                print(f"[weft] Warning: invalid guard pattern '{pattern}': {e}", file=sys.stderr)
                continue

    return None


def main():
    """Entry point for hook script. Reads stdin, evaluates guards."""
    raw = sys.stdin.read()
    try:
        hook_input = json.loads(raw)
    except json.JSONDecodeError:
        sys.exit(0)

    result = evaluate(hook_input)

    if result and result.get("blocked"):
        print(result["reason"], file=sys.stderr)
        sys.exit(2)

    sys.exit(0)


if __name__ == "__main__":
    main()
