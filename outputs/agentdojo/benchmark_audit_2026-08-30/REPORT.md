# CausalGuard AgentDojo benchmark-wide observation audit

## Outcome

Slurm array `63806751` completed all eight shards with 97/97 unique normal tasks represented. Qwen3-32B achieved AgentDojo utility on 60/97 tasks; the remaining 37 are agent/task failures, not CausalGuard instrumentation failures. No task raised an execution exception.

The audit supports a qualified conclusion: CausalGuard reliably captured the agent/tool-call skeleton and runtime observation boundary, but the current narrow semantic instrumentation is not benchmark-wide. It is not yet valid to run or claim the final system-effect ablation.

## Configuration and completeness

- AgentDojo distribution: `0.1.35`; benchmark: `v1.2.2`.
- Suites: workspace 40, travel 20, banking 16, Slack 21.
- Model: `Qwen/Qwen3-32B` via local vLLM; temperature 0.0, top-p 0.9, base seed 20260830.
- Observation-only: policy enforcer was `None`; normal AgentDojo mutations were not blocked or modified.
- Scheduler: account `pi_juanzhai_umass_edu`, partition `superpod-a100`, 8-way A100 array.

## Execution and graph fidelity

- 437 LLM invocations.
- 342 ToolCall proposals and graph nodes; 340 result-bearing tool executions; 2 terminal proposals were not executed before the AgentDojo loop ended.
- 311 successful tool calls produced SystemOperation nodes.
- 97/97 task graphs were acyclic, proposal-to-ToolCall correspondence passed, invocation causality passed, and result/runtime-observation correlation passed.
- 0 collector/instrumentation failures.
- 92/97 tasks had explicit semantic provenance gaps. Most tools outside the existing workspace search/send slice emitted only generic operations without concrete read/write/payload/destination semantics.
- Conditional fidelity: 2/50 mutated tasks had complete write provenance; 2/21 payload-bearing tasks had complete `payload_of`; 2/29 destination-bearing tasks correlated correctly; 2/50 mutated tasks were fully consistent with graph mutation semantics.

These conditional denominators avoid counting vacuous true values for tasks that had no relevant write, payload, or destination.

## Actual effects

- 50 tasks changed persistent state; 47 did not.
- 25 tasks executed outgoing operations; 21 had explicit outgoing payload objects; 29 had actual destinations.
- Task counts by selected category: read-only access 95, calendar mutation 12, Slack membership/invitation 10, banking transfer 6, Slack DM 6, Slack channel message 6, file/cloud mutation 3, travel reservation 2, email send 2.
- Sensitive/protected-resource task count is not reported: normal tasks contain no audit label that supports that inference.

## Matched-case assessment

Strict natural matches found: 0. Structural near-match groups found: 2. The near matches differ in argument hashes, so the tool-level representation already exposes their relevant difference. They do not demonstrate that SystemOperation provenance adds information unavailable at the agent/tool level.

The benchmark run therefore did not produce a natural case that satisfies the proposed ablation criterion. `matched_case_candidates.md` records the qualified near matches; `missing_case_designs.md` specifies controlled variants that would be needed later. No new tasks were constructed and no ablation was performed.

## Research-direction assessment

AgentDojo is useful here as a source of diverse real mutations and fresh environment state. The present CausalGuard integration is already strong enough to verify proposal/order/runtime correspondence and to demonstrate the protected workspace email slice. It is not strong enough to compare system-effect information across the full benchmark: 92 task rows expose missing concrete semantics, and normal-task runs have no security verdict.

The next defensible step is to add deterministic, tool-specific runtime extractors only for the effect families selected for study, rerun those affected tasks, validate the conditional fidelity metrics, and then build predeclared controlled matched pairs. The final ablation should remain blocked until that evidence exists.

## Privacy and limitations

Structured exports contain hashes, types, bounded identifiers, and state-diff structure rather than prompts, tool results, bodies, or subjects. During finalization, 11 AgentDojo JSON-repair debug lines in Slurm stdout were replaced by hash-only evidence; the raw text was not retained. Key-based privacy checks remain a guard, not a semantic content classifier.

This is one model/configuration and one deterministic seed per task. Utility success is not security success. Normal-task security evaluation was unavailable and is recorded as null, not inferred. Generic SystemOperation existence must not be mistaken for correct semantic provenance.
