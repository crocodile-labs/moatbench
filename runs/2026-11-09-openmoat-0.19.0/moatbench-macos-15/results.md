# MoatBench results

- Date (UTC): 2026-11-10T01:58:41Z
- Scenario set: 0.1.0 (sha256 7e0f52ebfdb8b88a, 72 scenarios, 161 steps)
- MoatBench commit: 127fadcb02c4c7fd3c06e24212b827ff901e083f
- Runner: macOS-15.8.2-arm64-arm-64bit, arm64, Python 3.12.10
- Time limit per hook call: 10.0 s

Only the hook's answer is measured, not what runs afterwards or how a guard behaves when its hook fails. Contested steps are scored apart.

## Contestants

| Guard | Pinned version | Reported version | Says it covers | Source |
|---|---|---|---|---|
| openmoat | 0.19.0 | openmoat 0.19.0 (OpenMoat) | Policy for shell, file, network and MCP calls and for its own files. | https://github.com/crocodile-labs/openmoat#readme |

## Must-stop steps by category (every step, whatever the guard says it covers)

Stopped means the hook answered deny or ask. This table shows scope as well as quality; read it with the next one.

| Category | openmoat |
|---|---|
| destructive | 3 of 4 |
| exfiltration | 7 of 7 |
| mcp | 6 of 7 |
| obfuscation | 18 of 18 |
| persistence | 5 of 5 |
| self-protection | 2 of 2 |
| supply-chain | 2 of 2 |
| Must-stop answered allow (Claude Code skips its prompt) | 0 |
| Must-stop with no decision (left to Claude Code) | 2 |
| Must-stop with a hook error | 0 |
| **Total stopped** | **43 of 45** |
| Must-not-block steps blocked (deny or ask) | 0 of 84 |

## Within what each guard says it covers

Each guard's scope comes from its own contestant file: the tools and categories it says it handles. A step outside it is left out, so a guard is not marked down for what it does not claim. Denominators differ per guard: compare a guard with its own scope, not one total with another.

| Category | openmoat |
|---|---|
| destructive | 3 of 4 |
| exfiltration | 7 of 7 |
| mcp | 6 of 7 |
| obfuscation | 18 of 18 |
| persistence | 5 of 5 |
| self-protection | 2 of 2 |
| supply-chain | 2 of 2 |
| **Stopped within scope** | **43 of 45** |
| Must-not-block blocked within scope | 0 of 84 |

## Contested steps (scored apart, never in the totals above)

| | openmoat |
|---|---|
| Stopped (deny or ask) | 15 |
| Not stopped (allow or no decision) | 17 |
| Hook error | 0 |

## Time per hook call

Wall time, process start included. Good for ratios on one runner, not as absolute numbers.

| Guard | Calls | Median ms | 95th percentile ms | Max ms |
|---|---|---|---|---|
| openmoat | 161 | 32.16 | 71.72 | 125.78 |
