"""Tests for MCP tool guard detection and refusal telemetry."""

import json
from pathlib import Path
import pytest

from core import guard_engine, state_machine


class TestMcpGuards:
    def test_mcp_tool_command_is_guarded(self, project_dir, guarded_template):
        """MCP tool call carrying a shell command is caught by the guard."""
        pdir = str(project_dir)
        state_machine.start_workflow(guarded_template, "s1", pdir)

        result = guard_engine.evaluate(
            {
                "tool_name": "mcp__context_mode__ctx_execute",
                "tool_input": {"command": "git push origin main"}
            },
            pdir,
        )
        assert result is not None
        assert result["blocked"] is True

    def test_mcp_tool_cmd_key_is_guarded(self, project_dir, guarded_template):
        """MCP tool call using 'cmd' or 'script' is caught by the guard."""
        pdir = str(project_dir)
        state_machine.start_workflow(guarded_template, "s1", pdir)

        result = guard_engine.evaluate(
            {
                "tool_name": "mcp__runner__run",
                "tool_input": {"cmd": "kubectl apply -f pod.yaml"}
            },
            pdir,
        )
        assert result is not None
        assert result["blocked"] is True

    def test_logs_refusal_event_with_agent_p_tags(self, project_dir, guarded_template, monkeypatch, tmp_path):
        """Refusal telemetry records to ~/.local/state/agentry/events.jsonl with AGENT_P_TAGS."""
        pdir = str(project_dir)
        state_machine.start_workflow(guarded_template, "s1", pdir)

        monkeypatch.setenv("AGENT_P_TAGS", "build_id=bld_4910,task=refactor_core")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        result = guard_engine.evaluate(
            {"tool_name": "Bash", "tool_input": {"command": "git push origin main"}},
            pdir,
        )
        assert result is not None and result["blocked"] is True

        agentry_log = tmp_path / ".local" / "state" / "agentry" / "events.jsonl"
        assert agentry_log.exists()
        lines = [json.loads(line) for line in agentry_log.read_text(encoding="utf-8").splitlines()]
        assert len(lines) == 1
        refusal = lines[0]
        assert refusal["event"] == "guard"
        assert refusal["tool"] == "Bash"
        assert refusal["refused_input"] == "git push origin main"
        assert refusal["build_id"] == "bld_4910"
        assert refusal["task"] == "refactor_core"
