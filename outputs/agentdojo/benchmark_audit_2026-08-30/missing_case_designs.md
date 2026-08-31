# Missing matched-case designs

No tasks were constructed in this audit. The following controlled variants are needed before a system-effect ablation:

1. Identical `send_email` call and arguments under two environment/policy states where the same destination identity is trusted in one and external/untrusted in the other. Record the trust evidence on the actual operation.
2. Identical read-then-send trace where the same attachment reference resolves to a protected object/version in one fresh environment and a benign object/version in the other. Verify actual `payload_of`; do not infer it from the preceding read.
3. Identical banking call and arguments with controlled recipient/account resolution that yields a benign intended transfer in one environment and a high-risk destination in the other.
4. Identical mutation call against two initial states where one produces the intended mutation and the other produces a no-op, rejection, or mutation of a different concrete resource.

Each pair needs identical prompts, model configuration, tool-call names and arguments, explicit security ground truth, fresh environments, and a predeclared comparison of tool-level versus system-effect features. These designs are proposals only, not evidence from this run.
