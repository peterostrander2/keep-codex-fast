#!/usr/bin/env python3
"""Smoke tests for keep-codex-fast using a fake Codex home."""

from __future__ import annotations

import argparse
import contextlib
import io
import importlib.util
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "keep_codex_fast.py"


def load_module():
    spec = importlib.util.spec_from_file_location("keep_codex_fast", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["keep_codex_fast"] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    module.codex_processes_running = lambda: []
    module.top_node_processes = lambda details=False: module.report("top_node_processes skipped_in_smoke")
    return module


def make_fake_home(root: Path) -> dict[str, Path]:
    codex_home = root / ".codex"
    sessions = codex_home / "sessions" / "2026" / "01" / "01"
    sessions.mkdir(parents=True)
    rollout = sessions / "rollout-2026-01-01T00-00-00-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa.jsonl"
    rollout.write_text('{"type":"test"}\n', encoding="utf-8")
    old_time = time.time() - 30 * 86400
    os.utime(rollout, (old_time, old_time))

    (codex_home / ".codex-global-state.json").write_text('{"pinned-thread-ids":[]}', encoding="utf-8")
    (codex_home / "config.toml").write_text(
        'model = "gpt-test"\n'
        'model_reasoning_effort = "low"\n'
        '[projects."C:\\\\DefinitelyMissingKeepCodexFast"]\ntrust_level = "trusted"\n'
        '[mcp_servers.example]\ncommand = "example"\n'
        '[mcp_servers.example.env]\nPRIVATE_TOKEN = "must-not-print"\n',
        encoding="utf-8",
    )
    (codex_home / "AGENTS.md").write_text("one\ntwo\n", encoding="utf-8")
    (codex_home / "skills" / "example").mkdir(parents=True)
    (codex_home / "skills" / "example" / "SKILL.md").write_text("---\nname: example\n---\n", encoding="utf-8")
    (codex_home / "agents").mkdir()
    (codex_home / "agents" / "summariser.toml").write_text('name = "summariser"\n', encoding="utf-8")
    (codex_home / "hooks.json").write_text(
        '{"hooks":{"PreToolUse":[{"hooks":[{"type":"command","command":"safe-hook"}]}]}}',
        encoding="utf-8",
    )

    worktree = codex_home / "worktrees" / "oldtree"
    worktree.mkdir(parents=True)
    (worktree / "file.txt").write_text("x", encoding="utf-8")
    os.utime(worktree, (old_time, old_time))

    log_file = codex_home / "logs_2.sqlite"
    log_file.write_text("log", encoding="utf-8")

    state_db = codex_home / "state_5.sqlite"
    conn = sqlite3.connect(state_db)
    conn.execute(
        "create table threads (id text primary key, title text, rollout_path text, cwd text, updated_at integer, archived_at integer, archived integer)"
    )
    conn.execute(
        "insert into threads values (?,?,?,?,?,?,?)",
        (
            "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "Old test thread",
            str(rollout),
            r"\\?\C:\DefinitelyMissingKeepCodexFast",
            int(old_time),
            None,
            0,
        ),
    )
    conn.commit()
    conn.close()

    return {
        "codex_home": codex_home,
        "rollout": rollout,
        "worktree": worktree,
        "log_file": log_file,
        "state_db": state_db,
    }


def assert_report_mode(module) -> None:
    with tempfile.TemporaryDirectory() as td:
        paths = make_fake_home(Path(td))
        backup = Path(td) / "backup-report"
        args = argparse.Namespace(
            apply=False,
            backup_only=False,
            details=False,
            wait_for_codex_exit=False,
            codex_home=str(paths["codex_home"]),
            backup_root=str(backup),
            archive_older_than_days=10,
            worktree_older_than_days=7,
            rotate_logs_above_mb=0,
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            assert module.run(args) == 0
        text = output.getvalue()
        assert paths["rollout"].exists(), "report mode must not move sessions"
        assert paths["worktree"].exists(), "report mode must not move worktrees"
        assert paths["log_file"].exists(), "report mode must not rotate logs"
        assert not backup.exists(), "report mode must not create backup artifacts"
        assert "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa" not in text
        assert "Old test thread" not in text
        assert str(paths["codex_home"]) not in text
        assert "local_configuration_inventory" in text
        assert "global_agents_md_bytes 8" in text
        assert "global_agents_md_lines 2" in text
        assert "codex_home_skill_count 1" in text
        assert "codex_home_helper_agent_count 1" in text
        assert "configured_mcp_server_count 1" in text
        assert "configured_hook_command_count 1" in text
        assert "config_toml_model gpt-test" in text
        assert "config_toml_model_reasoning_effort low" in text
        assert "must-not-print" not in text


def inventory_output(module, home: Path) -> dict[str, str]:
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        module.context_footprint(home)
    return dict(line.split(" ", 1) for line in output.getvalue().splitlines() if " " in line)


def assert_inventory_failure_regression(module) -> None:
    """Baseline must fail on false healthy output, independently of metric names."""
    with tempfile.TemporaryDirectory() as td:
        home = make_fake_home(Path(td))["codex_home"]
        original = Path.read_bytes

        def denied(path, *args, **kwargs):
            if path == home / "AGENTS.md":
                raise PermissionError("fixture denied")
            return original(path, *args, **kwargs)

        with patch.object(Path, "read_bytes", denied):
            values = inventory_output(module, home)
        pressure = values.get("global_agents_md_pressure", values.get("agents_md_pressure"))
        assert pressure == "UNAVAILABLE", f"unreadable AGENTS.md reported pressure={pressure}"
        assert values["global_agents_md_state"] == "UNREADABLE"
        assert values["global_agents_md_bytes"] == "UNAVAILABLE"


def assert_inventory_states(module) -> None:
    inputs = (
        ("AGENTS.md", "global_agents_md_state", "global_agents_md_bytes"),
        ("config.toml", "config_toml_state", "configured_mcp_server_count"),
        ("hooks.json", "hooks_json_state", "configured_hook_command_count"),
        ("skills", "skills_directory_state", "codex_home_skill_count"),
        ("agents", "agents_directory_state", "codex_home_helper_agent_count"),
    )
    for name, state_key, metric in inputs:
        for state in ("OK", "MISSING", "UNREADABLE", "MALFORMED"):
            with tempfile.TemporaryDirectory() as td:
                home = make_fake_home(Path(td))["codex_home"]
                target = home / name
                with contextlib.ExitStack() as stack:
                    if state == "MISSING":
                        target.rename(home / (name + ".fixture-away"))
                    elif state == "MALFORMED":
                        if name in ("skills", "agents"):
                            target.rename(home / (name + ".fixture-away"))
                            target.write_text("not a directory")
                        else:
                            target.write_bytes(b"\xff")
                    elif state == "UNREADABLE":
                        if name in ("skills", "agents"):
                            original_scan = module.os.scandir

                            def denied_scan(path):
                                if Path(path) == target:
                                    raise PermissionError("fixture denied")
                                return original_scan(path)

                            stack.enter_context(patch.object(module.os, "scandir", denied_scan))
                        else:
                            method = "read_bytes" if name == "AGENTS.md" else "read_text"
                            original_read = getattr(Path, method)

                            def denied_read(path, *args, **kwargs):
                                if path == target:
                                    raise PermissionError("fixture denied")
                                return original_read(path, *args, **kwargs)

                            stack.enter_context(patch.object(Path, method, denied_read))
                    values = inventory_output(module, home)
                assert values[state_key] == state, (name, state, values)
                assert (values[metric] == "UNAVAILABLE") == (state != "OK"), (name, state, values)
                if name == "config.toml" and state != "OK":
                    for key in ("global_agents_md_limit_bytes", "global_agents_md_limit_source",
                                "global_agents_md_budget_pct", "global_agents_md_pressure",
                                "config_toml_model", "config_toml_model_reasoning_effort"):
                        assert values[key] == "UNAVAILABLE", (state, key, values)


def assert_inventory_semantics(module) -> None:
    with tempfile.TemporaryDirectory() as td:
        home = make_fake_home(Path(td))["codex_home"]
        values = inventory_output(module, home)
        assert values["global_agents_md_limit_source"] == "DEFAULT"
        assert values["global_agents_md_limit_bytes"] == "32768"
        config = home / "config.toml"
        original = config.read_text()
        config.write_text('project_doc_max_bytes = 16\n' + original)
        values = inventory_output(module, home)
        assert values["global_agents_md_limit_source"] == "CONFIGURED_OVERRIDE"
        assert values["global_agents_md_budget_pct"] == "50.0"
        for malformed in ('model = [', 'project_doc_max_bytes = true',
                          'project_doc_max_bytes = -1', 'mcp_servers = []', 'model = 42'):
            config.write_text(malformed)
            values = inventory_output(module, home)
            assert values["config_toml_state"] == "MALFORMED"
            assert values["configured_mcp_server_count"] == "UNAVAILABLE"
        config.write_text("")
        values = inventory_output(module, home)
        assert values["config_toml_state"] == "OK"
        assert values["configured_mcp_server_count"] == "0"
        assert values["config_toml_model"] == "UNSET"
        hooks = home / "hooks.json"
        for malformed in ('{', '[]', '{}', '{"hooks":[]}',
                          '{"hooks":{"PreToolUse":[{}]}}',
                          '{"hooks":{"PreToolUse":[{"hooks":[{"type":"command"}]}]}}'):
            hooks.write_text(malformed)
            values = inventory_output(module, home)
            assert values["hooks_json_state"] == "MALFORMED"
            assert values["configured_hook_command_count"] == "UNAVAILABLE"
        hooks.write_text('{"hooks":{"PreToolUse":[{"hooks":[{"type":"prompt","prompt":"fixture"}]}]}}')
        assert inventory_output(module, home)["configured_hook_command_count"] == "0"
        (home / "AGENTS.md").write_bytes(b"")
        values = inventory_output(module, home)
        assert values["global_agents_md_state"] == "OK"
        assert values["global_agents_md_bytes"] == "0"
        (home / "AGENTS.md").write_bytes(b"\xef\xbb\xbfone\r\ntwo")
        values = inventory_output(module, home)
        assert values["global_agents_md_bytes"] == "11"
        assert values["global_agents_md_lines"] == "2"
        nested = home / "skills" / "group" / "nested"
        nested.mkdir(parents=True)
        (nested / "SKILL.md").write_text("fixture")
        assert inventory_output(module, home)["codex_home_skill_count"] == "1"
        nested_agent = home / "agents" / "nested"
        nested_agent.mkdir()
        (nested_agent / "helper.toml").write_text("fixture")
        assert inventory_output(module, home)["codex_home_helper_agent_count"] == "2"
        original_scan = module.os.scandir

        def denied_nested(path):
            if Path(path) == nested_agent:
                raise PermissionError("fixture denied")
            return original_scan(path)

        with patch.object(module.os, "scandir", denied_nested):
            values = inventory_output(module, home)
        assert values["agents_directory_state"] == "UNREADABLE"
        assert values["codex_home_helper_agent_count"] == "UNAVAILABLE"


def assert_backup_only_mode(module) -> None:
    with tempfile.TemporaryDirectory() as td:
        paths = make_fake_home(Path(td))
        backup = Path(td) / "backup-only"
        args = argparse.Namespace(
            apply=False,
            backup_only=True,
            details=False,
            wait_for_codex_exit=False,
            codex_home=str(paths["codex_home"]),
            backup_root=str(backup),
            archive_older_than_days=10,
            worktree_older_than_days=7,
            rotate_logs_above_mb=0,
        )
        assert module.run(args) == 0
        assert paths["rollout"].exists(), "backup-only mode must not move sessions"
        assert paths["worktree"].exists(), "backup-only mode must not move worktrees"
        assert paths["log_file"].exists(), "backup-only mode must not rotate logs"
        assert (backup / "state_5.sqlite").exists()
        assert (backup / "config.toml").exists()
        assert not (backup / "moved-sessions.jsonl").exists()


def assert_session_alias_detection(module) -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        real_root = root / "real"
        alias_root = root / "alias"
        real_root.mkdir()
        try:
            alias_root.symlink_to(real_root, target_is_directory=True)
        except OSError:
            return

        paths = make_fake_home(real_root)
        alias_home = alias_root / ".codex"
        conn = module.sqlite_connect(alias_home / "state_5.sqlite", readonly=True)
        try:
            candidates = module.active_session_candidates(conn, alias_home, 10)
        finally:
            conn.close()
        assert len(candidates) == 1


def assert_apply_mode(module) -> None:
    with tempfile.TemporaryDirectory() as td:
        paths = make_fake_home(Path(td))
        backup = Path(td) / "backup-apply"
        args = argparse.Namespace(
            apply=True,
            backup_only=False,
            details=False,
            wait_for_codex_exit=False,
            codex_home=str(paths["codex_home"]),
            backup_root=str(backup),
            archive_older_than_days=10,
            worktree_older_than_days=7,
            rotate_logs_above_mb=0,
        )
        assert module.run(args) == 0

        conn = sqlite3.connect(paths["state_db"])
        archived, archived_at, rollout_path, cwd = conn.execute(
            "select archived, archived_at, rollout_path, cwd from threads where id=?",
            ("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",),
        ).fetchone()
        conn.close()

        assert archived == 1
        assert archived_at is not None
        assert "archived_sessions" in rollout_path
        assert cwd == r"C:\DefinitelyMissingKeepCodexFast"
        assert not paths["rollout"].exists()
        assert not paths["worktree"].exists()
        assert not paths["log_file"].exists()
        assert "DefinitelyMissingKeepCodexFast" not in (paths["codex_home"] / "config.toml").read_text(
            encoding="utf-8"
        )
        assert (backup / "restore-sessions.py").exists()
        assert (backup / "moved-sessions.jsonl").exists()
        assert (backup / "moved-worktrees.jsonl").exists()


def main() -> int:
    module = load_module()
    assert_inventory_failure_regression(module)
    assert_inventory_states(module)
    assert_inventory_semantics(module)
    assert_report_mode(module)
    assert_backup_only_mode(module)
    assert_session_alias_detection(module)
    assert_apply_mode(module)
    print("smoke tests passed: 7 groups (including 20 input-state cases)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
