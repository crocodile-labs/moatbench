# Adding or contesting a scenario

This is written for someone who builds or uses a guard and thinks this benchmark is wrong about it. That is what it most needs. You do not need to know OpenMoat. The scenarios started as OpenMoat's, so assume the mistakes favour OpenMoat until shown otherwise.

## Check your change locally

```sh
python3 moatbench.py validate
python3 moatbench.py run --contestant contestants/your-guard.toml --install --out results/test
python3 -m unittest discover -s tests
```

## Contest a scenario

A step is wrong if a secure tool should answer it differently from what `expect` says. Open an issue or a pull request that:

1. Names the scenario id and step.
2. Says what the step should be and why: `must-stop`, `must-not-block`, or `contested`.
3. Gives your evidence: what the attack does, or why the call is ordinary work, and any real-world report or documentation.

The possible changes:

- Change `expect`.
- Add `contested = "<reason>"`. Use this when both answers are defensible. Say what the two sides are. The step is still run, and is scored apart.
- Remove `contested`, when a step should count after all. Contested marks can favour any guard, including OpenMoat, so challenge them in either direction.
- Remove a step or scenario that is unrealistic, a duplicate, or aimed at one guard's own files without being marked contested.

Say so if you work on a guard the change helps. That is welcome, and readers need to know it.

Every change to a scenario file bumps `version` in `scenarios/manifest.toml`, so results stay comparable: two results with different set versions do not measure the same thing.

## Add a scenario

Add it to the category's file in `scenarios/`, or a new file for a new category.

- A step is one tool call, in the form an agent sends it. Do not write anything that needs a guard's own rule ids.
- `expect` is what a secure tool should do, not what any particular guard happens to do. If you are not sure, mark it `contested` and say why.
- Give `why` (what the agent was steered into, or why the work is ordinary) and `source` (where the attack came from: a report, a paper, a public test set, or "written for this set by <you>"). Do not copy from a source whose licence does not allow it; [CANDIDATES.md](CANDIDATES.md) notes licences for some public sets.
- Use example hosts (`example.com`, `example.net`, `example.org`) and fake credentials in the shape of the real ones. Nothing here is ever run, but other people read it.
- Keep ordinary work too. A guard that blocks everything would otherwise score well.

## Add your guard

Add `contestants/your-guard.toml` (format in [README.md](README.md#contestants)) with a pinned version and your guard's real install path and hook command. Nothing in the runner may be specific to your guard, and nothing in it is specific to OpenMoat.

The `scope` table is your guard's own statement of what it covers: link to where you say it. Scope is what keeps the comparison fair to a guard that deliberately does one thing, so set it from what your guard documents, not from what scores well.

Then add a line to [Who has run this](README.md#who-has-run-this) if you ran it, with the set version and a link to your `results.json`.
