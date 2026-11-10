# Candidate attack sets

Public material that could widen this set. **None of it has been imported.** Licences are from each repository's GitHub metadata on 2026-11-09; read the licence itself before copying anything. Nothing here has been checked for how well it converts.

Good candidates are a tool call in the form an agent sends it, or a technique that can be written as one. A set of prompts aimed at a model is useful only after the tool calls it produces are written out.

| Set | Licence | What it has | Notes on fit |
|---|---|---|---|
| [AgentDojo](https://github.com/ethz-spylab/agentdojo) | MIT | Prompt-injection tasks against agents with tools | Attacks are prompts and tool environments; its tool calls are not shell or file calls and would need writing out |
| [InjecAgent](https://github.com/uiuc-kang-lab/InjecAgent) | MIT | Indirect prompt-injection cases for tool-using agents | The attacker tools are simulated, so each case needs mapping to a real tool call. Last pushed in 2024 |
| [Agent Security Bench (ASB)](https://github.com/agiresearch/ASB) | MIT | Attacks and defences on LLM agents | As above: scenarios are agent-level and would need mapping to tool calls |
| [BIPIA](https://github.com/microsoft/BIPIA) | GitHub reports no licence it can identify | Benchmark of indirect prompt injection | Archived; check the licence by hand before using it. Text attacks, not tool calls |
| [Atomic Red Team](https://github.com/redcanaryco/atomic-red-team) | MIT | Small tests mapped to MITRE ATT&CK, many of them plain shell commands | Close to the step format for persistence and credential access |
| [PayloadsAllTheThings](https://github.com/swisskyrepo/PayloadsAllTheThings) | MIT | Payloads and bypass techniques | Useful for shell obfuscation variants |
| [GTFOBins](https://github.com/GTFOBins/GTFOBins.github.io) | GPL-3.0 | Ways ordinary Unix programs read files and run commands | Strong source for reading secrets through ordinary tools. GPL: reference techniques rather than copying text into this MIT OR Apache-2.0 repository, or keep any copied data apart under its own licence |
| [LOLBAS](https://github.com/LOLBAS-Project/LOLBAS) | GPL-3.0 | The same for Windows binaries | Windows only, so it does not fit a `sh`-based runner yet. Same licence care as GTFOBins |
| [garak](https://github.com/NVIDIA/garak) | Apache-2.0 | A scanner of probes against language models | Aimed at model replies, not hook decisions. Listed as a source of ideas only |
