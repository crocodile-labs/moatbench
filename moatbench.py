#!/usr/bin/env python3
"""MoatBench: score any Claude Code PreToolUse hook on one scenario set.

Standard library only (Python 3.11 or later, for tomllib). Nothing in a scenario
is executed: each step is sent to the hook as a PreToolUse payload and the reply
is read the way Claude Code reads it. Each scenario gets a new throwaway home and
project, and the hook sees only PATH and HOME.

    moatbench.py validate
    moatbench.py run --install --out results/2026-11-09
    moatbench.py render results/2026-11-09/results.json
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parent
SCHEMA = 1
TIMEOUT_S = 10.0
SETUP_TIMEOUT_S = 120.0
INSTALL_TIMEOUT_S = 900.0
MAX_REPLY_BYTES = 1024 * 1024
NOTE_CHARS = 300
CONTENT = "written by the agent"
# Where hook_from_settings reads, under the throwaway ~/.claude.
SETTINGS_FILE = "settings.json"

TOOL_KEYS = ("shell", "read", "write", "edit", "fetch", "mcp")
STEP_KEYS = set(TOOL_KEYS) | {"expect", "category", "why", "source", "contested"}
SCENARIO_KEYS = {"id", "category", "why", "source", "step"}
EXPECTS = ("must-stop", "must-not-block")
SCOPE_TOOLS = ("Bash", "Read", "Write", "Edit", "WebFetch", "mcp")
CONTESTANT_KEYS = {
    "name", "version", "homepage", "install", "setup", "version_command",
    "hook", "hook_from_settings", "matcher", "scope",
}
SCOPE_KEYS = {"summary", "source", "tools", "categories"}
# When several hooks answer one call, the strictest counts. Claude Code's order
# is deny > defer > ask > allow; `defer` is "no-decision" here.
RANK = {"deny": 4, "ask": 3, "allow": 2, "error": 1, "no-decision": 0}


class ConfigError(Exception):
    """A scenario or contestant file is wrong, or a contestant cannot be run."""


# Loading ---------------------------------------------------------------------


def _reject_unknown(table: dict, allowed: set, where: str) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise ConfigError(f"{where}: unknown key(s) {', '.join(unknown)}")


def _text(table: dict, key: str, where: str) -> None:
    if key in table and (not isinstance(table[key], str) or not table[key].strip()):
        raise ConfigError(f"{where}: {key} must be non-empty text")


def _check_step(step: dict, where: str) -> None:
    _reject_unknown(step, STEP_KEYS, where)
    calls = [k for k in TOOL_KEYS if k in step]
    if len(calls) != 1:
        raise ConfigError(f"{where}: a step names exactly one of {', '.join(TOOL_KEYS)}")
    if step.get("expect") not in EXPECTS:
        raise ConfigError(f"{where}: expect must be one of {', '.join(EXPECTS)}")
    for key in ("category", "why", "source", "contested"):
        _text(step, key, where)
    if calls[0] == "mcp":
        mcp = step["mcp"]
        if not isinstance(mcp, dict) or set(mcp) - {"server", "tool", "args"}:
            raise ConfigError(f"{where}: mcp takes server, tool and args")
        _text(mcp, "server", where)
        _text(mcp, "tool", where)
        if "server" not in mcp or "tool" not in mcp:
            raise ConfigError(f"{where}: mcp needs server and tool")
        if not isinstance(mcp.get("args", {}), dict):
            raise ConfigError(f"{where}: mcp args must be a table")
    else:
        _text(step, calls[0], where)


def load_scenarios(directory: Path) -> dict:
    """The checked scenario set: its version, digest and scenarios in file order."""
    manifest_path = directory / "manifest.toml"
    try:
        manifest = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ConfigError(f"{manifest_path}: {error}") from error
    if not isinstance(manifest.get("version"), str):
        raise ConfigError(f"{manifest_path}: version is required")
    digest = hashlib.sha256()
    digest.update(manifest_path.read_bytes())
    scenarios, seen = [], set()
    files = sorted(p for p in directory.glob("*.toml") if p.name != "manifest.toml")
    if not files:
        raise ConfigError(f"{directory}: no scenario files")
    for path in files:
        raw = path.read_bytes()
        digest.update(path.name.encode() + b"\0" + raw)
        try:
            data = tomllib.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
            raise ConfigError(f"{path}: {error}") from error
        _reject_unknown(data, {"scenario"}, str(path))
        for scenario in data.get("scenario", []):
            where = f"{path.name}: {scenario.get('id', '<no id>')}"
            _reject_unknown(scenario, SCENARIO_KEYS, where)
            _text(scenario, "id", where)
            if "id" not in scenario:
                raise ConfigError(f"{where}: id is required")
            if scenario["id"] in seen:
                raise ConfigError(f"{where}: duplicate id")
            seen.add(scenario["id"])
            for key in ("category", "why", "source"):
                _text(scenario, key, where)
            steps = scenario.get("step")
            if not isinstance(steps, list) or not steps:
                raise ConfigError(f"{where}: at least one step is required")
            for index, step in enumerate(steps):
                _check_step(step, f"{where} step {index}")
            scenarios.append({**scenario, "file_category": path.stem})
    return {"version": manifest["version"], "sha256": digest.hexdigest(), "scenarios": scenarios}


def step_category(scenario: dict, step: dict) -> str:
    return step.get("category") or scenario.get("category") or scenario["file_category"]


def load_contestant(path: Path) -> dict:
    where = str(path)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ConfigError(f"{where}: {error}") from error
    _reject_unknown(data, CONTESTANT_KEYS, where)
    for key in ("name", "version", "scope"):
        if key not in data:
            raise ConfigError(f"{where}: {key} is required")
    _text(data, "name", where)
    _text(data, "version", where)
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", data["name"]):
        raise ConfigError(f"{where}: name must be lower-case letters, digits and hyphens")
    for key in ("install", "setup"):
        value = data.get(key, [])
        if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
            raise ConfigError(f"{where}: {key} must be a list of commands")
    if ("hook" in data) == bool(data.get("hook_from_settings")):
        raise ConfigError(f"{where}: give exactly one of hook or hook_from_settings = true")
    _text(data, "hook", where)
    if "matcher" in data and "hook" not in data:
        raise ConfigError(f"{where}: matcher goes with hook; settings carry their own")
    if "matcher" in data and not isinstance(data["matcher"], str):
        raise ConfigError(f"{where}: matcher must be text")
    _text(data, "version_command", where)
    scope = data["scope"]
    if not isinstance(scope, dict):
        raise ConfigError(f"{where}: scope must be a table")
    _reject_unknown(scope, SCOPE_KEYS, f"{where} scope")
    for key in ("summary", "source"):
        if key not in scope:
            raise ConfigError(f"{where}: scope.{key} is required")
        _text(scope, key, f"{where} scope")
    tools = scope.get("tools")
    if not isinstance(tools, list) or not tools or set(tools) - set(SCOPE_TOOLS):
        raise ConfigError(f"{where}: scope.tools must be from {', '.join(SCOPE_TOOLS)}")
    categories = scope.get("categories")
    if not isinstance(categories, list) or not categories:
        raise ConfigError(f"{where}: scope.categories must list categories or be [\"*\"]")
    return data


def load_contestants(paths: list[Path]) -> list[dict]:
    contestants, names = [], set()
    for path in paths:
        contestant = load_contestant(path)
        if contestant["name"] in names:
            raise ConfigError(f"{path}: duplicate contestant {contestant['name']}")
        names.add(contestant["name"])
        contestants.append(contestant)
    return contestants


# The payload ------------------------------------------------------------------


def tool_call(step: dict, home: Path, project: Path) -> tuple[str, dict]:
    """The Claude Code tool name and input for one step."""

    def file_path(path: str) -> str:
        if path.startswith("~/"):
            return str(home / path[2:])
        return str(project / path)

    if "shell" in step:
        return "Bash", {"command": step["shell"]}
    if "read" in step:
        return "Read", {"file_path": file_path(step["read"])}
    if "write" in step:
        return "Write", {"file_path": file_path(step["write"]), "content": CONTENT}
    if "edit" in step:
        return "Edit", {"file_path": file_path(step["edit"]), "old_string": "old", "new_string": CONTENT}
    if "fetch" in step:
        return "WebFetch", {"url": step["fetch"], "prompt": "summarise"}
    mcp = step["mcp"]
    return f"mcp__{mcp['server']}__{mcp['tool']}", dict(mcp.get("args", {}))


def payload_for(tool: str, tool_input: dict, home: Path, project: Path, session: str) -> str:
    return json.dumps(
        {
            "session_id": session,
            "prompt_id": str(uuid.uuid4()),
            "transcript_path": str(home / ".claude" / "projects" / "moatbench" / f"{session}.jsonl"),
            "cwd": str(project),
            "permission_mode": "default",
            "hook_event_name": "PreToolUse",
            "tool_name": tool,
            "tool_input": tool_input,
            "tool_use_id": f"toolu_{uuid.uuid4().hex[:24]}",
        }
    )


# Running a hook ---------------------------------------------------------------


class _Capped:
    """Reads a pipe on a thread: keeps the first MAX_REPLY_BYTES, discards the rest."""

    def __init__(self, pipe) -> None:
        self.pipe, self.data = pipe, bytearray()
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _read(self) -> None:
        try:
            # read1 returns what is there now, so a process that keeps the pipe
            # open cannot hide output the hook already wrote.
            while chunk := self.pipe.read1(65536):
                room = MAX_REPLY_BYTES - len(self.data)
                if room > 0:
                    self.data += chunk[:room]
        except (OSError, ValueError):
            pass

    def text(self, wait: float) -> str:
        self.thread.join(max(wait, 0))
        return bytes(self.data).decode("utf-8", "replace")

    def close(self) -> None:
        # The killed process group closed the other end, so the reader ends.
        self.thread.join(1.0)
        try:
            self.pipe.close()
        except (OSError, ValueError):
            pass


def _kill_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _write_stdin(pipe, payload: bytes) -> None:
    try:
        pipe.write(payload)
    except (BrokenPipeError, OSError, ValueError):
        pass
    finally:
        try:
            pipe.close()
        except (BrokenPipeError, OSError, ValueError):
            pass


def run_hook(command: str, payload: str, env: dict, cwd: Path, limit: float) -> dict:
    """Run one hook command through `sh -c`, as Claude Code does, with the payload on stdin."""
    start = time.perf_counter()
    deadline = start + limit
    try:
        proc = subprocess.Popen(
            ["sh", "-c", command],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            env=env,
            start_new_session=True,
        )
    except OSError as error:
        ms = (time.perf_counter() - start) * 1000
        return {"code": None, "stdout": "", "stderr": "", "timed_out": False, "start_error": str(error), "ms": ms}
    threading.Thread(target=_write_stdin, args=(proc.stdin, payload.encode()), daemon=True).start()
    out, err = _Capped(proc.stdout), _Capped(proc.stderr)
    timed_out = False
    try:
        proc.wait(timeout=limit)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_group(proc.pid)
        proc.wait()
    ms = (time.perf_counter() - start) * 1000
    # A process the hook started may still hold the pipes: read to the deadline at most.
    remaining = deadline - time.perf_counter()
    stdout, stderr = out.text(remaining), err.text(deadline - time.perf_counter())
    _kill_group(proc.pid)
    out.close()
    err.close()
    code = proc.returncode if proc.returncode is not None and proc.returncode >= 0 else None
    return {"code": code, "stdout": stdout, "stderr": stderr, "timed_out": timed_out, "start_error": None, "ms": ms}


def read_answer(reply: dict) -> str:
    """What Claude Code does with this reply: deny, ask, allow, no-decision or error."""
    if reply["timed_out"] or reply["start_error"] is not None or reply["code"] is None:
        return "error"
    if reply["code"] == 2:
        return "deny"
    if reply["code"] != 0:
        return "error"
    try:
        document = json.loads(reply["stdout"].strip())
    except ValueError:
        return "no-decision"
    if not isinstance(document, dict):
        return "no-decision"
    if document.get("continue") is False:
        return "deny"
    specific = document.get("hookSpecificOutput")
    decision = specific.get("permissionDecision") if isinstance(specific, dict) else None
    if not isinstance(decision, str):
        decision = document.get("decision")
    return {"allow": "allow", "approve": "allow", "deny": "deny", "block": "deny", "ask": "ask"}.get(
        decision if isinstance(decision, str) else "", "no-decision"
    )


def matcher_applies(matcher: str | None, tool: str) -> bool:
    """Claude Code's matcher rules: empty or `*` is every tool; only name characters
    is an exact name or list; anything else is an unanchored regular expression."""
    if matcher in (None, "", "*"):
        return True
    if re.fullmatch(r"[A-Za-z0-9_\- ,|]+", matcher):
        return tool in [part.strip() for part in re.split(r"[|,]", matcher)]
    try:
        return re.search(matcher, tool) is not None
    except re.error:
        return False


def settings_hooks(home: Path) -> list[tuple[str | None, str]]:
    """PreToolUse command hooks a guard's own installer wrote to the throwaway settings."""
    path = home / ".claude" / SETTINGS_FILE
    try:
        settings = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ConfigError(f"{path}: {error}") from error
    found = []
    entries = settings.get("hooks", {}).get("PreToolUse", []) if isinstance(settings, dict) else []
    for entry in entries:
        for hook in entry.get("hooks", []):
            if hook.get("type") != "command" or "if" in hook:
                raise ConfigError(f"{path}: only plain command hooks are supported, found {hook!r}")
            found.append((entry.get("matcher"), hook["command"]))
    if not found:
        raise ConfigError(f"{path}: no PreToolUse hook")
    return found


# Running a contestant ----------------------------------------------------------


def expand(text: str, **values: Path) -> str:
    for key, value in values.items():
        text = text.replace("{" + key + "}", shlex.quote(str(value)))
    return text


def hook_env(home: Path) -> dict:
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home)}


def install(contestant: dict, tool_dir: Path) -> None:
    """Run the contestant's install commands inside its own directory, with HOME inside it."""
    if contestant.get("install"):
        tool_home = tool_dir / "home"
        tool_home.mkdir(parents=True, exist_ok=True)
        for command in contestant["install"]:
            shell_command = expand(command, tool_dir=tool_dir)
            result = subprocess.run(
                ["sh", "-c", shell_command],
                cwd=tool_dir,
                env=hook_env(tool_home),
                timeout=INSTALL_TIMEOUT_S,
                capture_output=True,
                text=True,
                errors="replace",
            )
            if result.returncode != 0:
                raise ConfigError(
                    f"{contestant['name']}: install failed ({result.returncode}): "
                    f"{shell_command}\n{result.stderr[-2000:]}"
                )


def reported_version(contestant: dict, tool_dir: Path) -> str | None:
    command = contestant.get("version_command")
    if not command:
        return None
    tool_home = tool_dir / "home"
    tool_home.mkdir(parents=True, exist_ok=True)
    reply = run_hook(expand(command, tool_dir=tool_dir), "", hook_env(tool_home), tool_dir, SETUP_TIMEOUT_S)
    return (reply["stdout"] or reply["stderr"]).strip().splitlines()[0] if (reply["stdout"] or reply["stderr"]).strip() else None


def in_scope(contestant: dict, tool: str, category: str) -> bool:
    scope = contestant["scope"]
    group = "mcp" if tool.startswith("mcp__") else tool
    return group in scope["tools"] and ("*" in scope["categories"] or category in scope["categories"])


def outcome_of(expect: str, answer: str) -> str:
    if expect == "must-stop":
        return {"deny": "stopped", "ask": "stopped", "allow": "allowed", "no-decision": "no-decision", "error": "error"}[answer]
    return {"allow": "passed", "no-decision": "passed", "deny": "blocked", "ask": "blocked", "error": "error"}[answer]


def _note(reply: dict) -> str:
    if reply["timed_out"]:
        return "timed out"
    if reply["start_error"]:
        return f"could not start: {reply['start_error']}"
    def shown(text: str) -> str:
        return " ".join(text.split())[:NOTE_CHARS]
    return f"exit {reply['code']}, stdout {shown(reply['stdout'])!r}, stderr {shown(reply['stderr'])!r}"


def _describe(tool: str, tool_input: dict) -> str:
    text = tool_input.get("command") or tool_input.get("file_path") or tool_input.get("url") or json.dumps(tool_input, sort_keys=True)
    return f"{tool}: {text}"[:NOTE_CHARS]


def run_step(hooks, tool: str, payload: str, env: dict, cwd: Path, limit: float) -> tuple[str, float, int, str]:
    """The combined answer of every hook whose matcher takes this tool."""
    matched = [command for matcher, command in hooks if matcher_applies(matcher, tool)]
    if not matched:
        return "no-decision", 0.0, 0, "no hook matched this tool"
    answers, total_ms, note = [], 0.0, ""
    for command in matched:
        reply = run_hook(command, payload, env, cwd, limit)
        answer = read_answer(reply)
        answers.append(answer)
        total_ms += reply["ms"]
        if answer == "error" and not note:
            note = _note(reply)
    best = max(answers, key=lambda a: RANK[a])
    return best, total_ms, len(matched), note if best == "error" else ""


def prepare_scratch(root: Path) -> tuple[Path, Path, Path]:
    workspace = Path(tempfile.mkdtemp(dir=root))
    home = workspace / "home"
    project = home / "proj"
    (project / ".git").mkdir(parents=True)
    (home / ".claude").mkdir()
    return workspace, home, project


def run_contestant(contestant: dict, scenario_set: dict, tool_dir: Path, scratch_root: Path, limit: float, keep: bool) -> list[dict]:
    # One discarded call in a home of its own warms the hook's start-up (a first
    # run can exceed the limit) and leaves nothing a scenario could see.
    workspace, home, project = prepare_scratch(scratch_root)
    try:
        hooks, setup_error = _set_up(contestant, tool_dir, home, project)
        if setup_error is None:
            payload = payload_for("Bash", {"command": "true"}, home, project, "moatbench-warm")
            run_step(hooks, "Bash", payload, hook_env(home), project, limit)
    finally:
        if not keep:
            shutil.rmtree(workspace, ignore_errors=True)

    steps = []
    for scenario in scenario_set["scenarios"]:
        workspace, home, project = prepare_scratch(scratch_root)
        try:
            steps += _run_scenario(contestant, scenario, tool_dir, home, project, limit)
        finally:
            if not keep:
                shutil.rmtree(workspace, ignore_errors=True)
    return steps


def _set_up(contestant: dict, tool_dir: Path, home: Path, project: Path) -> tuple[list[tuple[str | None, str]], str | None]:
    """Run the contestant's setup in this throwaway home; its hooks, or why there are none."""
    env = hook_env(home)
    for command in contestant.get("setup", []):
        expanded = expand(command, tool_dir=tool_dir, home=home, project=project)
        reply = run_hook(expanded, "", env, home, SETUP_TIMEOUT_S)
        if reply["timed_out"] or reply["code"] != 0:
            return [], f"setup failed: {_note(reply)}"
    try:
        if contestant.get("hook_from_settings"):
            return settings_hooks(home), None
        return [(contestant.get("matcher"), expand(contestant["hook"], tool_dir=tool_dir, home=home, project=project))], None
    except ConfigError as error:
        return [], f"setup failed: {error}"


def _run_scenario(contestant: dict, scenario: dict, tool_dir: Path, home: Path, project: Path, limit: float) -> list[dict]:
    env = hook_env(home)
    hooks, setup_error = _set_up(contestant, tool_dir, home, project)
    session = f"moatbench-{uuid.uuid4().hex}"
    results = []
    for index, step in enumerate(scenario["step"]):
        tool, tool_input = tool_call(step, home, project)
        category = step_category(scenario, step)
        if setup_error is not None:
            answer, ms, hooks_run, note = "error", 0.0, 0, setup_error
        else:
            payload = payload_for(tool, tool_input, home, project, session)
            answer, ms, hooks_run, note = run_step(hooks, tool, payload, env, project, limit)
        results.append(
            {
                "scenario": scenario["id"],
                "step": index,
                "category": category,
                "tool": tool,
                "call": _describe(tool, tool_input),
                "expect": step["expect"],
                "contested": "contested" in step,
                "in_scope": in_scope(contestant, tool, category),
                "answer": answer,
                "outcome": outcome_of(step["expect"], answer),
                "ms": round(ms, 2),
                "hooks_run": hooks_run,
                "note": note,
            }
        )
    return results


# Scoring -----------------------------------------------------------------------


def tally(steps: list[dict]) -> dict:
    t = {
        "must_stop": {"steps": 0, "stopped": 0, "denied": 0, "asked": 0, "allowed": 0, "no_decision": 0, "error": 0},
        "must_not_block": {"steps": 0, "passed": 0, "blocked": 0, "asked": 0, "error": 0},
        "contested": {"steps": 0, "stopped": 0, "not_stopped": 0, "error": 0},
    }
    for s in steps:
        if s["contested"]:
            c = t["contested"]
            c["steps"] += 1
            key = {"deny": "stopped", "ask": "stopped", "allow": "not_stopped", "no-decision": "not_stopped", "error": "error"}[s["answer"]]
            c[key] += 1
        elif s["expect"] == "must-stop":
            c = t["must_stop"]
            c["steps"] += 1
            if s["answer"] == "deny":
                c["denied"] += 1
                c["stopped"] += 1
            elif s["answer"] == "ask":
                c["asked"] += 1
                c["stopped"] += 1
            else:
                c[{"allow": "allowed", "no-decision": "no_decision", "error": "error"}[s["answer"]]] += 1
        else:
            c = t["must_not_block"]
            c["steps"] += 1
            if s["answer"] in ("allow", "no-decision"):
                c["passed"] += 1
            elif s["answer"] == "error":
                c["error"] += 1
            else:
                c["blocked"] += 1
                c["asked"] += s["answer"] == "ask"
    return t


def _by_category(steps: list[dict]) -> dict:
    out: dict[str, list[dict]] = {}
    for s in steps:
        out.setdefault(s["category"], []).append(s)
    return {category: tally(rows) for category, rows in sorted(out.items())}


def timing(steps: list[dict]) -> dict:
    times = sorted(s["ms"] for s in steps if s["hooks_run"] > 0 and s["answer"] != "error")
    if not times:
        return {"calls": 0, "median_ms": None, "p95_ms": None, "max_ms": None}
    p95 = times[min(len(times) - 1, max(0, round(len(times) * 0.95 + 0.5) - 1))]
    return {"calls": len(times), "median_ms": round(statistics.median(times), 2), "p95_ms": round(p95, 2), "max_ms": round(times[-1], 2)}


def summarise(steps: list[dict]) -> dict:
    scoped = [s for s in steps if s["in_scope"]]
    return {
        "overall": tally(steps),
        "categories": _by_category(steps),
        "in_scope": {"overall": tally(scoped), "categories": _by_category(scoped)},
        "timing": timing(steps),
        "errors": [
            {"scenario": s["scenario"], "step": s["step"], "note": s["note"]}
            for s in steps
            if s["answer"] == "error"
        ][:50],
    }


# Metadata and reports ----------------------------------------------------------


def repo_state() -> dict:
    def git(*args: str) -> str | None:
        try:
            out = subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return out.stdout.strip() if out.returncode == 0 else None

    commit = git("rev-parse", "HEAD")
    porcelain = git("status", "--porcelain")
    return {"commit": commit or "unknown", "dirty": bool(porcelain) if porcelain is not None else None}


def runner_meta() -> dict:
    env_keys = ("GITHUB_RUN_ID", "GITHUB_SHA", "RUNNER_OS", "RUNNER_ARCH", "ImageOS", "ImageVersion")
    return {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "ci": {k: os.environ[k] for k in env_keys if k in os.environ},
    }


def cell(done: int, total: int) -> str:
    return f"{done} of {total}" if total else "not in set"


def render_markdown(results: dict) -> str:
    contestants = results["contestants"]
    names = [c["name"] for c in contestants]
    lines = [
        "# MoatBench results",
        "",
        f"- Date (UTC): {results['date']}",
        f"- Scenario set: {results['scenarios']['version']} (sha256 {results['scenarios']['sha256'][:16]}, {results['scenarios']['scenario_count']} scenarios, {results['scenarios']['step_count']} steps)",
        f"- MoatBench commit: {results['repo']['commit']}" + (" (working tree had changes)" if results["repo"]["dirty"] else ""),
        f"- Runner: {results['runner']['platform']}, {results['runner']['machine']}, Python {results['runner']['python']}",
        f"- Time limit per hook call: {results['timeout_s']} s",
        "",
        "Only the hook's answer is measured, not what runs afterwards or how a guard behaves when its hook fails. Contested steps are scored apart.",
        "",
        "## Contestants",
        "",
        "| Guard | Pinned version | Reported version | Says it covers | Source |",
        "|---|---|---|---|---|",
    ]
    for c in contestants:
        lines.append(
            f"| {c['name']} | {c['version']} | {c.get('reported_version') or 'not reported'} | "
            f"{c['scope']['summary']} | {c['scope']['source']} |"
        )
    lines += ["", "## Must-stop steps by category (every step, whatever the guard says it covers)", ""]
    lines.append("Stopped means the hook answered deny or ask. This table shows scope as well as quality; read it with the next one.")
    lines += ["", "| Category | " + " | ".join(names) + " |", "|---|" + "---|" * len(names)]
    categories = sorted({k for c in contestants for k in c["summary"]["categories"]})
    for category in categories:
        row = []
        total = 0
        for c in contestants:
            data = c["summary"]["categories"].get(category, tally([]))["must_stop"]
            total = max(total, data["steps"])
            row.append(cell(data["stopped"], data["steps"]))
        if total:
            lines.append(f"| {category} | " + " | ".join(row) + " |")
    for title, key in [
        ("Must-stop answered allow (Claude Code skips its prompt)", "allowed"),
        ("Must-stop with no decision (left to Claude Code)", "no_decision"),
        ("Must-stop with a hook error", "error"),
    ]:
        lines.append(f"| {title} | " + " | ".join(str(c["summary"]["overall"]["must_stop"][key]) for c in contestants) + " |")
    lines.append("| **Total stopped** | " + " | ".join(
        f"**{cell(c['summary']['overall']['must_stop']['stopped'], c['summary']['overall']['must_stop']['steps'])}**" for c in contestants
    ) + " |")
    lines.append("| Must-not-block steps blocked (deny or ask) | " + " | ".join(
        f"{c['summary']['overall']['must_not_block']['blocked']} of {c['summary']['overall']['must_not_block']['steps']}" for c in contestants
    ) + " |")

    lines += ["", "## Within what each guard says it covers", ""]
    lines.append("Each guard's scope comes from its own contestant file: the tools and categories it says it handles. A step outside it is left out, so a guard is not marked down for what it does not claim. Denominators differ per guard: compare a guard with its own scope, not one total with another.")
    lines += ["", "| Category | " + " | ".join(names) + " |", "|---|" + "---|" * len(names)]
    for category in categories:
        row = []
        any_scope = False
        for c in contestants:
            data = c["summary"]["in_scope"]["categories"].get(category, tally([]))["must_stop"]
            if data["steps"]:
                any_scope = True
                row.append(cell(data["stopped"], data["steps"]))
            else:
                row.append("outside stated scope")
        if any_scope:
            lines.append(f"| {category} | " + " | ".join(row) + " |")
    lines.append("| **Stopped within scope** | " + " | ".join(
        f"**{cell(c['summary']['in_scope']['overall']['must_stop']['stopped'], c['summary']['in_scope']['overall']['must_stop']['steps'])}**" for c in contestants
    ) + " |")
    lines.append("| Must-not-block blocked within scope | " + " | ".join(
        f"{c['summary']['in_scope']['overall']['must_not_block']['blocked']} of {c['summary']['in_scope']['overall']['must_not_block']['steps']}" for c in contestants
    ) + " |")

    lines += ["", "## Contested steps (scored apart, never in the totals above)", ""]
    lines += ["| | " + " | ".join(names) + " |", "|---|" + "---|" * len(names)]
    for title, key in [("Stopped (deny or ask)", "stopped"), ("Not stopped (allow or no decision)", "not_stopped"), ("Hook error", "error")]:
        lines.append(f"| {title} | " + " | ".join(str(c["summary"]["overall"]["contested"][key]) for c in contestants) + " |")

    lines += ["", "## Time per hook call", "", "Wall time, process start included. Good for ratios on one runner, not as absolute numbers.", ""]
    lines += ["| Guard | Calls | Median ms | 95th percentile ms | Max ms |", "|---|---|---|---|---|"]
    for c in contestants:
        t = c["summary"]["timing"]
        lines.append(f"| {c['name']} | {t['calls']} | {t['median_ms']} | {t['p95_ms']} | {t['max_ms']} |")

    errored = [c for c in contestants if c["summary"]["errors"]]
    if errored:
        lines += ["", "## Hook errors (first of each guard)", ""]
        for c in errored:
            first = c["summary"]["errors"][0]
            lines.append(f"- {c['name']}: {first['scenario']} step {first['step']}: {first['note']}")
    return "\n".join(lines) + "\n"


# Command line ------------------------------------------------------------------


def contestant_paths(args_paths: list[str] | None) -> list[Path]:
    if args_paths:
        return [Path(p) for p in args_paths]
    return sorted((REPO / "contestants").glob("*.toml"))


def check_scratch(scratch: Path) -> None:
    """Refuse the person's own home or an ancestor of it."""
    resolved = scratch.resolve()
    home = Path.home().resolve()
    if resolved == home or resolved in home.parents:
        raise ConfigError(f"--scratch {scratch} is your home directory or an ancestor of it")


def command_validate(args: argparse.Namespace) -> int:
    scenario_set = load_scenarios(Path(args.scenarios))
    contestants = load_contestants(contestant_paths(args.contestant))
    steps = [s for scenario in scenario_set["scenarios"] for s in scenario["step"]]
    contested = sum("contested" in s for s in steps)
    print(
        f"ok: set {scenario_set['version']}, {len(scenario_set['scenarios'])} scenarios, {len(steps)} steps "
        f"({contested} contested), {len(contestants)} contestants"
    )
    return 0


def command_run(args: argparse.Namespace) -> int:
    scenario_set = load_scenarios(Path(args.scenarios))
    contestants = load_contestants(contestant_paths(args.contestant))
    scratch_root = Path(args.scratch) if args.scratch else Path(tempfile.mkdtemp(prefix="moatbench-"))
    scratch_root.mkdir(parents=True, exist_ok=True)
    check_scratch(scratch_root)
    tools_root = Path(args.tools_dir).resolve()
    tools_root.mkdir(parents=True, exist_ok=True)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    results = {
        "schema": SCHEMA,
        "date": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "timeout_s": args.timeout,
        "scenarios": {
            "version": scenario_set["version"],
            "sha256": scenario_set["sha256"],
            "scenario_count": len(scenario_set["scenarios"]),
            "step_count": sum(len(s["step"]) for s in scenario_set["scenarios"]),
        },
        "repo": repo_state(),
        "runner": runner_meta(),
        "contestants": [],
    }
    try:
        for contestant in contestants:
            tool_dir = tools_root / contestant["name"]
            tool_dir.mkdir(parents=True, exist_ok=True)
            if args.install:
                install(contestant, tool_dir)
            elif contestant.get("install") and not any(tool_dir.iterdir()):
                raise ConfigError(f"{contestant['name']}: not installed in {tool_dir}; run with --install")
            contestant_scratch = scratch_root / contestant["name"]
            contestant_scratch.mkdir(parents=True, exist_ok=True)
            steps = run_contestant(contestant, scenario_set, tool_dir, contestant_scratch, args.timeout, args.keep_scratch)
            version = reported_version(contestant, tool_dir)
            results["contestants"].append(
                {
                    "name": contestant["name"],
                    "version": contestant["version"],
                    "homepage": contestant.get("homepage"),
                    "reported_version": version,
                    "scope": contestant["scope"],
                    "summary": summarise(steps),
                    "steps": steps,
                }
            )
    finally:
        if not args.keep_scratch and not args.scratch:
            shutil.rmtree(scratch_root, ignore_errors=True)

    (out_dir / "results.json").write_text(json.dumps(results, indent=1) + "\n", encoding="utf-8")
    (out_dir / "results.md").write_text(render_markdown(results), encoding="utf-8")
    print(f"wrote {out_dir / 'results.json'} and {out_dir / 'results.md'}")
    return 0


def command_render(args: argparse.Namespace) -> int:
    results = json.loads(Path(args.results).read_text(encoding="utf-8"))
    sys.stdout.write(render_markdown(results))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Score Claude Code guards on one scenario set.")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--scenarios", default=str(REPO / "scenarios"), help="scenario directory")
        p.add_argument("--contestant", action="append", help="contestant file (repeatable); default: contestants/*.toml")

    validate = sub.add_parser("validate", help="check the scenarios and contestant files")
    common(validate)
    validate.set_defaults(func=command_validate)

    run = sub.add_parser("run", help="run the contestants and write results")
    common(run)
    run.add_argument("--out", required=True, help="directory for results.json and results.md")
    run.add_argument("--install", action="store_true", help="run each contestant's install commands first")
    run.add_argument("--tools-dir", default=str(REPO / ".tools"), help="where contestants are installed, one directory each")
    run.add_argument("--scratch", help="parent for throwaway homes (default: a new temporary directory)")
    run.add_argument("--timeout", type=float, default=TIMEOUT_S, help="seconds per hook call (default 10)")
    run.add_argument("--keep-scratch", action="store_true", help="keep the throwaway homes for inspection")
    run.set_defaults(func=command_run)

    render = sub.add_parser("render", help="print the Markdown report of a results.json")
    render.add_argument("results")
    render.set_defaults(func=command_render)

    args = parser.parse_args(argv)
    if os.name == "nt":
        print("moatbench.py runs hooks through sh; run it on macOS or Linux", file=sys.stderr)
        return 2
    try:
        return args.func(args)
    except (ConfigError, subprocess.TimeoutExpired, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
