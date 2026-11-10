"""Tests of the runner, against fake hooks only: no real guard or agent runs here."""

from __future__ import annotations

import json
import shlex
import sys
import tempfile
import textwrap
import time
import tomllib
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import moatbench  # noqa: E402

HOOK = REPO / "tests" / "fakes" / "hook.sh"
WRITE_SETTINGS = REPO / "tests" / "fakes" / "write-settings.sh"


def hook_command(mode: str) -> str:
    return f"sh {shlex.quote(str(HOOK))} {mode}"


def reply(code=0, stdout="", stderr="", timed_out=False, start_error=None):
    return {"code": code, "stdout": stdout, "stderr": stderr, "timed_out": timed_out, "start_error": start_error, "ms": 1.0}


class ReadAnswerTests(unittest.TestCase):
    """The reply table: what Claude Code does with each answer shape."""

    def test_answer_shapes(self):
        specific = lambda d: json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": d}})
        cases = [
            (reply(stdout=specific("allow")), "allow"),
            (reply(stdout=specific("deny")), "deny"),
            (reply(stdout=specific("ask")), "ask"),
            (reply(stdout=specific("defer")), "no-decision"),
            (reply(stdout=specific("maybe")), "no-decision"),
            (reply(stdout='{"decision":"approve"}'), "allow"),
            (reply(stdout='{"decision":"block"}'), "deny"),
            (reply(stdout='{"continue":false}'), "deny"),
            (reply(stdout=""), "no-decision"),
            (reply(stdout="not json"), "no-decision"),
            (reply(stdout="[1,2]"), "no-decision"),
            (reply(stdout='{"other":1}'), "no-decision"),
            (reply(code=2), "deny"),
            (reply(code=2, stdout=specific("allow")), "deny"),
            (reply(code=1), "error"),
            (reply(code=3, stdout=specific("allow")), "error"),
            (reply(code=None), "error"),
            (reply(timed_out=True), "error"),
            (reply(start_error="no such file"), "error"),
        ]
        for given, want in cases:
            with self.subTest(given=given):
                self.assertEqual(moatbench.read_answer(given), want)


class RunHookTests(unittest.TestCase):
    """Real child processes in each answer shape."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cwd = Path(self.tmp.name)
        self.env = moatbench.hook_env(self.cwd)

    def answer(self, mode, limit=5.0):
        got = moatbench.run_hook(hook_command(mode), '{"x":1}', self.env, self.cwd, limit)
        return got, moatbench.read_answer(got)

    def test_each_mode(self):
        for mode, want in [
            ("allow-json", "allow"),
            ("ask-json", "ask"),
            ("deny-json", "deny"),
            ("defer-json", "no-decision"),
            ("legacy-approve", "allow"),
            ("legacy-block", "deny"),
            ("continue-false", "deny"),
            ("exit2", "deny"),
            ("exit2-with-allow", "deny"),
            ("exit0-silent", "no-decision"),
            ("exit0-text", "no-decision"),
            ("exit0-bad-json", "no-decision"),
            ("exit1", "error"),
            ("exit3-with-allow", "error"),
            ("killed", "error"),
            ("ignore-stdin", "deny"),
        ]:
            with self.subTest(mode=mode):
                self.assertEqual(self.answer(mode)[1], want)

    def test_a_missing_program_is_an_error(self):
        got = moatbench.run_hook("/no/such/hook", "{}", self.env, self.cwd, 5.0)
        self.assertEqual(moatbench.read_answer(got), "error")

    def test_timeout_kills_and_reports(self):
        start = time.monotonic()
        got, answer = self.answer("timeout", limit=0.5)
        self.assertTrue(got["timed_out"])
        self.assertEqual(answer, "error")
        self.assertLess(time.monotonic() - start, 5)

    def test_big_output_is_capped(self):
        got, answer = self.answer("big-output")
        self.assertEqual(answer, "no-decision")
        self.assertLessEqual(len(got["stdout"]), moatbench.MAX_REPLY_BYTES)

    def test_a_background_process_cannot_hold_the_wait(self):
        start = time.monotonic()
        _, answer = self.answer("background-holds-pipe", limit=2.0)
        self.assertEqual(answer, "deny")
        self.assertLess(time.monotonic() - start, 5)


class MatcherTests(unittest.TestCase):
    def test_rules(self):
        for matcher, tool, want in [
            (None, "Bash", True),
            ("", "Bash", True),
            ("*", "mcp__x__y", True),
            ("Bash", "Bash", True),
            ("Bash", "BashOutput", False),
            ("Edit|Write", "Write", True),
            ("Edit,Write", "Write", True),
            ("Edit|Write", "NotebookEdit", False),
            ("mcp__.*", "mcp__x__y", True),
            ("mcp__.*", "Bash", False),
            ("Edit.*", "NotebookEdit", True),
            ("^Edit$", "NotebookEdit", False),
            ("Bash|mcp__.*", "Bash", True),
            ("(", "Bash", False),
        ]:
            with self.subTest(matcher=matcher, tool=tool):
                self.assertEqual(moatbench.matcher_applies(matcher, tool), want)


class PayloadTests(unittest.TestCase):
    def test_tool_calls(self):
        home, project = Path("/h"), Path("/h/proj")
        cases = [
            ({"shell": "ls"}, "Bash", {"command": "ls"}),
            ({"read": "~/.ssh/k"}, "Read", {"file_path": "/h/.ssh/k"}),
            ({"read": "src/a"}, "Read", {"file_path": "/h/proj/src/a"}),
            ({"write": "a"}, "Write", {"file_path": "/h/proj/a", "content": moatbench.CONTENT}),
            ({"edit": "~/a"}, "Edit", {"file_path": "/h/a", "old_string": "old", "new_string": moatbench.CONTENT}),
            ({"fetch": "https://x"}, "WebFetch", {"url": "https://x", "prompt": "summarise"}),
            ({"mcp": {"server": "s", "tool": "t", "args": {"a": 1}}}, "mcp__s__t", {"a": 1}),
            ({"mcp": {"server": "s", "tool": "t"}}, "mcp__s__t", {}),
        ]
        for step, tool, tool_input in cases:
            with self.subTest(step=step):
                self.assertEqual(moatbench.tool_call(step, home, project), (tool, tool_input))

    def test_document(self):
        doc = json.loads(moatbench.payload_for("Bash", {"command": "ls"}, Path("/h"), Path("/h/proj"), "s1"))
        self.assertEqual(doc["hook_event_name"], "PreToolUse")
        self.assertEqual(doc["tool_name"], "Bash")
        self.assertEqual(doc["cwd"], "/h/proj")
        self.assertEqual(doc["session_id"], "s1")
        self.assertEqual(doc["permission_mode"], "default")
        self.assertTrue(doc["tool_use_id"].startswith("toolu_"))


MINI = """
[[scenario]]
id = "attack"
why = "two attack steps and a contested one"
source = "test"

[[scenario.step]]
shell = "cat secret"
expect = "must-stop"

[[scenario.step]]
read = "~/.ssh/k"
expect = "must-stop"

[[scenario.step]]
shell = "npm install x"
expect = "must-stop"
contested = "policy choice"

[[scenario]]
id = "safe"

[[scenario.step]]
shell = "ls"
expect = "must-not-block"

[[scenario.step]]
fetch = "https://example.com"
expect = "must-not-block"
category = "other"
"""


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.scenarios = self.root / "scenarios"
        self.scenarios.mkdir()
        (self.scenarios / "manifest.toml").write_text('version = "t1"\n')
        (self.scenarios / "mini.toml").write_text(MINI)
        self.contestants = self.root / "contestants"
        self.contestants.mkdir()
        self.tools = self.root / "tools"
        self.scratch = self.root / "scratch"

    def contestant(self, name, hook=None, extra="", tools='["Bash", "Read", "Write", "Edit", "WebFetch", "mcp"]', categories='["*"]', settings=False):
        wiring = "hook_from_settings = true" if settings else f"hook = {json.dumps(hook)}"
        body = textwrap.dedent(
            f"""\
            name = "{name}"
            version = "1.0"
            {wiring}
            {extra}

            [scope]
            summary = "s"
            source = "src"
            tools = {tools}
            categories = {categories}
            """
        )
        path = self.contestants / f"{name}.toml"
        path.write_text(body)
        return path

    def run_bench(self, *paths, timeout=5.0):
        out = self.root / "out"
        argv = ["run", "--scenarios", str(self.scenarios), "--tools-dir", str(self.tools), "--scratch", str(self.scratch), "--out", str(out), "--timeout", str(timeout)]
        for path in paths:
            argv += ["--contestant", str(path)]
        self.assertEqual(moatbench.main(argv), 0)
        return json.loads((out / "results.json").read_text()), (out / "results.md").read_text()

    def by_name(self, results, name):
        return next(c for c in results["contestants"] if c["name"] == name)

    def test_counts_for_each_answer(self):
        paths = [
            self.contestant("denier", hook_command("deny-json")),
            self.contestant("allower", hook_command("allow-json")),
            self.contestant("asker", hook_command("ask-json")),
            self.contestant("silent", hook_command("exit0-silent")),
            self.contestant("broken", hook_command("exit1")),
        ]
        results, md = self.run_bench(*paths)
        denier = self.by_name(results, "denier")["summary"]["overall"]
        self.assertEqual(denier["must_stop"], {"steps": 2, "stopped": 2, "denied": 2, "asked": 0, "allowed": 0, "no_decision": 0, "error": 0})
        self.assertEqual(denier["must_not_block"], {"steps": 2, "passed": 0, "blocked": 2, "asked": 0, "error": 0})
        self.assertEqual(denier["contested"], {"steps": 1, "stopped": 1, "not_stopped": 0, "error": 0})

        allower = self.by_name(results, "allower")["summary"]["overall"]
        self.assertEqual(allower["must_stop"]["allowed"], 2)
        self.assertEqual(allower["must_not_block"]["passed"], 2)
        self.assertEqual(allower["contested"]["not_stopped"], 1)

        asker = self.by_name(results, "asker")["summary"]["overall"]
        self.assertEqual(asker["must_stop"]["asked"], 2)
        self.assertEqual(asker["must_stop"]["stopped"], 2)
        self.assertEqual(asker["must_not_block"]["blocked"], 2)
        self.assertEqual(asker["must_not_block"]["asked"], 2)

        silent = self.by_name(results, "silent")["summary"]["overall"]
        self.assertEqual(silent["must_stop"]["no_decision"], 2)
        self.assertEqual(silent["must_not_block"]["passed"], 2)

        broken = self.by_name(results, "broken")
        self.assertEqual(broken["summary"]["overall"]["must_stop"]["error"], 2)
        self.assertEqual(broken["summary"]["overall"]["must_not_block"]["error"], 2)
        self.assertTrue(broken["summary"]["errors"])
        self.assertIn("exit 1", broken["summary"]["errors"][0]["note"])

        self.assertIn("Contested steps", md)
        self.assertIn("Within what each guard says it covers", md)
        self.assertIn("2 of 2", md)

    def test_results_carry_their_provenance(self):
        results, _ = self.run_bench(self.contestant("denier", hook_command("deny-json")))
        self.assertEqual(results["schema"], moatbench.SCHEMA)
        self.assertEqual(results["scenarios"]["version"], "t1")
        self.assertEqual(results["scenarios"]["scenario_count"], 2)
        self.assertEqual(results["scenarios"]["step_count"], 5)
        self.assertEqual(len(results["scenarios"]["sha256"]), 64)
        self.assertRegex(results["date"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertIn("commit", results["repo"])
        self.assertIn("platform", results["runner"])
        self.assertEqual(self.by_name(results, "denier")["version"], "1.0")

    def test_scope_tables_leave_out_what_a_guard_does_not_claim(self):
        path = self.contestant("bashonly", hook_command("deny-json"), tools='["Bash"]', categories='["mini"]')
        results, md = self.run_bench(path)
        contestant = self.by_name(results, "bashonly")
        scoped = contestant["summary"]["in_scope"]["overall"]
        # Of the two must-stop steps only the shell one is in scope; the fetch is another category.
        self.assertEqual(scoped["must_stop"]["steps"], 1)
        self.assertEqual(scoped["must_stop"]["stopped"], 1)
        self.assertEqual(scoped["must_not_block"]["steps"], 1)
        self.assertEqual(contestant["summary"]["overall"]["must_stop"]["steps"], 2)
        self.assertNotIn("outside stated scope", md)

    def test_a_category_outside_a_guards_scope_says_so(self):
        wide = self.contestant("wide", hook_command("deny-json"))
        narrow = self.contestant("narrow", hook_command("deny-json"), tools='["WebFetch"]')
        results, md = self.run_bench(wide, narrow)
        self.assertEqual(self.by_name(results, "narrow")["summary"]["in_scope"]["overall"]["must_stop"]["steps"], 0)
        self.assertIn("outside stated scope", md)

    def test_timing_counts_only_calls_a_hook_answered(self):
        path = self.contestant("matched", hook_command("deny-json"), extra='matcher = "Bash"')
        results, _ = self.run_bench(path)
        c = self.by_name(results, "matched")
        self.assertEqual(c["summary"]["timing"]["calls"], 3)  # Bash steps only
        read = next(s for s in c["steps"] if s["tool"] == "Read")
        self.assertEqual((read["hooks_run"], read["answer"]), (0, "no-decision"))

    def test_only_path_and_home_reach_the_hook_and_the_home_is_throwaway(self):
        path = self.contestant("recorder", hook_command("record"))
        results, _ = self.run_bench(path)
        self.assertEqual(self.by_name(results, "recorder")["summary"]["overall"]["must_stop"]["steps"], 2)
        # Inspect with the scratch directory kept.
        scratch = self.root / "kept"
        out = self.root / "out2"
        argv = ["run", "--scenarios", str(self.scenarios), "--tools-dir", str(self.tools), "--scratch", str(scratch), "--out", str(out), "--contestant", str(path), "--keep-scratch"]
        self.assertEqual(moatbench.main(argv), 0)
        homes = sorted(p.parent for p in scratch.glob("recorder/*/home/env.txt"))
        self.assertEqual(len(homes), 3, "the warm-up home and one home per scenario")
        home = next(h for h in homes if len(list(h.glob("payload-*.json"))) == 3)
        env = dict(line.split("=", 1) for line in (home / "env.txt").read_text().splitlines() if "=" in line)
        ignored = {"PWD", "OLDPWD", "SHLVL", "_"}  # set by sh itself
        self.assertEqual(set(env) - ignored, {"PATH", "HOME"})
        self.assertEqual(env["HOME"], str(home))
        self.assertEqual((home / "cwd.txt").read_text().strip(), str(home / "proj"))
        payloads = [json.loads(p.read_text()) for p in sorted(home.glob("payload-*.json"))]
        self.assertEqual(len({p["session_id"] for p in payloads}), 1, "one session within a scenario")
        read = next(p for p in payloads if p["tool_name"] == "Read")
        self.assertEqual(read["tool_input"]["file_path"], str(home / ".ssh" / "k"))

    def test_steps_share_state_within_a_scenario_but_not_across(self):
        results, _ = self.run_bench(self.contestant("stateful", hook_command("stateful")))
        steps = self.by_name(results, "stateful")["steps"]
        first_of_each = [s for s in steps if s["step"] == 0]
        later = [s for s in steps if s["step"] > 0]
        self.assertTrue(all(s["answer"] == "no-decision" for s in first_of_each))
        self.assertTrue(all(s["answer"] == "deny" for s in later))

    def test_warm_up_call_is_sent_once_and_discarded(self):
        log = self.root / "sessions.txt"
        path = self.contestant("warm", f"{hook_command('warm-counter')} {shlex.quote(str(log))}")
        self.run_bench(path)
        lines = log.read_text().splitlines()
        self.assertEqual(sum("moatbench-warm" in line for line in lines), 1)
        self.assertEqual(len(lines), 6)  # warm-up + five steps

    def test_timeout_is_an_error_not_a_stop(self):
        results, _ = self.run_bench(self.contestant("slow", hook_command("timeout")), timeout=0.2)
        summary = self.by_name(results, "slow")["summary"]["overall"]
        self.assertEqual(summary["must_stop"]["stopped"], 0)
        self.assertEqual(summary["must_stop"]["error"], 2)

    def check_settings_mode(self, file_name):
        setup = f"sh {shlex.quote(str(WRITE_SETTINGS))} {shlex.quote(str(HOOK))} 'Bash|mcp__.*' deny-json {file_name}"
        path = self.contestant("installer", settings=True, extra=f"setup = [{json.dumps(setup)}]")
        results, _ = self.run_bench(path)
        c = self.by_name(results, "installer")
        by_tool = {s["tool"]: s for s in c["steps"]}
        self.assertEqual(by_tool["Bash"]["answer"], "deny", by_tool["Bash"]["note"])
        self.assertEqual(by_tool["Read"]["hooks_run"], 0)

    def test_hook_from_settings_uses_the_written_matcher(self):
        # A sandbox may refuse any file named settings.json under .claude, so
        # this test reads the same content from another file name.
        with mock.patch.object(moatbench, "SETTINGS_FILE", "settings.test.json"):
            self.check_settings_mode("settings.test.json")

    def test_hook_from_settings_with_the_real_file_name(self):
        probe = self.root / "probe" / ".claude"
        probe.mkdir(parents=True)
        try:
            (probe / "settings.json").write_text("{}")
        except PermissionError:
            self.skipTest("this sandbox refuses to write a .claude/settings.json file")
        self.check_settings_mode("settings.json")

    def test_settings_with_a_conditional_hook_are_refused(self):
        home = self.root / "h"
        (home / ".claude").mkdir(parents=True)
        document = {"hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": "true", "if": "Bash(rm *)"}]}]}}
        (home / ".claude" / "settings.test.json").write_text(json.dumps(document))
        with mock.patch.object(moatbench, "SETTINGS_FILE", "settings.test.json"):
            with self.assertRaises(moatbench.ConfigError):
                moatbench.settings_hooks(home)

    def test_settings_in_exec_form_keep_their_arguments(self):
        home = self.root / "h"
        (home / ".claude").mkdir(parents=True)
        entry = {"type": "command", "command": "/opt/a b/guard", "args": ["run", "--home", "/x y"]}
        document = {"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [entry]}]}}
        (home / ".claude" / "settings.test.json").write_text(json.dumps(document))
        with mock.patch.object(moatbench, "SETTINGS_FILE", "settings.test.json"):
            found = moatbench.settings_hooks(home)
        self.assertEqual(found, [("*", "'/opt/a b/guard' run --home '/x y'")])

    def test_settings_mode_without_a_hook_reports_a_setup_failure(self):
        path = self.contestant("nohook", settings=True, extra='setup = ["true"]')
        results, _ = self.run_bench(path)
        c = self.by_name(results, "nohook")
        self.assertTrue(all(s["answer"] == "error" for s in c["steps"]))
        self.assertIn("setup failed", c["steps"][0]["note"])

    def test_a_failing_setup_is_recorded_per_step(self):
        path = self.contestant("badsetup", hook_command("deny-json"), extra='setup = ["exit 7"]')
        results, _ = self.run_bench(path)
        self.assertIn("setup failed", self.by_name(results, "badsetup")["steps"][0]["note"])

    def test_refuses_the_real_home_as_scratch(self):
        with self.assertRaises(moatbench.ConfigError):
            moatbench.check_scratch(Path.home())
        with self.assertRaises(moatbench.ConfigError):
            moatbench.check_scratch(Path.home().parent)
        moatbench.check_scratch(self.root)

    def test_uninstalled_contestant_is_refused_without_install(self):
        path = self.contestant("needsinstall", "true", extra='install = ["true"]')
        argv = ["run", "--scenarios", str(self.scenarios), "--tools-dir", str(self.tools), "--scratch", str(self.scratch), "--out", str(self.root / "o"), "--contestant", str(path)]
        self.assertEqual(moatbench.main(argv), 2)

    def test_install_runs_in_its_own_directory_with_its_own_home(self):
        marker = "touch {tool_dir}/installed; echo $HOME > {tool_dir}/home.txt"
        path = self.contestant("installed", "true", extra=f"install = [{json.dumps(marker)}]")
        out = self.root / "o"
        argv = ["run", "--scenarios", str(self.scenarios), "--tools-dir", str(self.tools), "--scratch", str(self.scratch), "--out", str(out), "--contestant", str(path), "--install"]
        self.assertEqual(moatbench.main(argv), 0)
        self.assertTrue((self.tools / "installed" / "installed").exists())
        self.assertEqual((self.tools / "installed" / "home.txt").read_text().strip(), str(self.tools / "installed" / "home"))

    def test_render_matches_the_written_markdown(self):
        self.run_bench(self.contestant("denier", hook_command("deny-json")))
        out = self.root / "out"
        rendered = moatbench.render_markdown(json.loads((out / "results.json").read_text()))
        self.assertEqual(rendered, (out / "results.md").read_text())


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        (self.dir / "manifest.toml").write_text('version = "1"\n')

    def check(self, text):
        (self.dir / "x.toml").write_text(text)
        return moatbench.load_scenarios(self.dir)

    def test_bad_scenarios_are_refused(self):
        for bad in [
            '[[scenario]]\nid="a"\n[[scenario.step]]\nshell="x"\n',  # no expect
            '[[scenario]]\nid="a"\n[[scenario.step]]\nshell="x"\nexpect="deny"\n',  # OpenMoat verdict
            '[[scenario]]\nid="a"\n[[scenario.step]]\nshell="x"\nread="y"\nexpect="must-stop"\n',  # two calls
            '[[scenario]]\nid="a"\n[[scenario.step]]\nexpect="must-stop"\n',  # no call
            '[[scenario]]\nid="a"\nsteps=[]\n',  # unknown key
            '[[scenario]]\nid="a"\n[[scenario.step]]\nshell="x"\nexpect="must-stop"\nrule="secrets-paths"\n',  # rule id
            '[[scenario]]\nid="a"\n[[scenario.step]]\nmcp={server="s"}\nexpect="must-stop"\n',  # no tool
            '[[scenario]]\nid="a"\n[[scenario.step]]\nshell="x"\nexpect="must-stop"\ncontested=""\n',  # empty reason
        ]:
            with self.subTest(bad=bad):
                with self.assertRaises(moatbench.ConfigError):
                    self.check(bad)

    def test_duplicate_ids_are_refused(self):
        with self.assertRaises(moatbench.ConfigError):
            self.check('[[scenario]]\nid="a"\n[[scenario.step]]\nshell="x"\nexpect="must-stop"\n[[scenario]]\nid="a"\n[[scenario.step]]\nshell="x"\nexpect="must-stop"\n')

    def test_category_defaults_to_the_file_and_a_step_can_override(self):
        text = '[[scenario]]\nid="a"\ncategory="cat"\n[[scenario.step]]\nshell="x"\nexpect="must-stop"\n[[scenario]]\nid="b"\n[[scenario.step]]\nshell="y"\nexpect="must-stop"\ncategory="z"\n[[scenario.step]]\nshell="z"\nexpect="must-stop"\n'
        data = self.check(text)["scenarios"]
        self.assertEqual(moatbench.step_category(data[0], data[0]["step"][0]), "cat")
        self.assertEqual(moatbench.step_category(data[1], data[1]["step"][0]), "z")
        self.assertEqual(moatbench.step_category(data[1], data[1]["step"][1]), "x")

    def write_contestant(self, body):
        path = self.dir / "c.toml"
        path.write_text(body)
        return moatbench.load_contestant(path)

    def test_bad_contestants_are_refused(self):
        good = 'name="n"\nversion="1"\nhook="true"\n[scope]\nsummary="s"\nsource="u"\ntools=["Bash"]\ncategories=["*"]\n'
        self.write_contestant(good)
        for bad in [
            good.replace('version="1"\n', ""),
            good.replace('hook="true"', ""),
            good.replace('hook="true"', 'hook="true"\nhook_from_settings=true'),
            good.replace('name="n"', 'name="Bad Name"'),
            good.replace('tools=["Bash"]', 'tools=["Shell"]'),
            good.replace('source="u"\n', ""),
            good.replace('hook="true"', 'hook_from_settings=true\nmatcher="Bash"'),
            good + "extra=1\n",
        ]:
            with self.subTest(bad=bad):
                with self.assertRaises(moatbench.ConfigError):
                    self.write_contestant(bad)


class ShippedDataTests(unittest.TestCase):
    """The scenario set and contestant files in this repository."""

    def test_scenarios_and_contestants_validate(self):
        data = moatbench.load_scenarios(REPO / "scenarios")
        self.assertTrue(data["scenarios"])
        contestants = moatbench.load_contestants(sorted((REPO / "contestants").glob("*.toml")))
        self.assertEqual({c["name"] for c in contestants}, {"openmoat"})

    def test_every_contested_step_gives_a_reason_and_no_step_names_a_verdict(self):
        data = moatbench.load_scenarios(REPO / "scenarios")
        for scenario in data["scenarios"]:
            for step in scenario["step"]:
                self.assertIn(step["expect"], moatbench.EXPECTS)
                self.assertNotIn("rule", step)
                if "contested" in step:
                    self.assertGreater(len(step["contested"]), 20, scenario["id"])

    def test_every_scenario_says_where_it_came_from(self):
        data = moatbench.load_scenarios(REPO / "scenarios")
        for scenario in data["scenarios"]:
            self.assertIn("source", scenario, scenario["id"])

    def test_contestant_scope_categories_exist(self):
        data = moatbench.load_scenarios(REPO / "scenarios")
        known = {moatbench.step_category(s, st) for s in data["scenarios"] for st in s["step"]}
        for path in sorted((REPO / "contestants").glob("*.toml")):
            contestant = moatbench.load_contestant(path)
            for category in contestant["scope"]["categories"]:
                if category != "*":
                    self.assertIn(category, known, path.name)

    def test_openmoat_gets_no_special_path(self):
        data = tomllib.loads((REPO / "contestants" / "openmoat.toml").read_text())
        self.assertTrue(data["hook_from_settings"])
        self.assertNotIn("openmoat", Path(moatbench.__file__).read_text().lower())


if __name__ == "__main__":
    unittest.main()
