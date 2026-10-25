# Security Policy

## Reporting a Vulnerability

Wizard operates as a secure, sandboxed code-execution platform relying on dynamic local inference. We treat sandbox escapes, prompt-injection bypasses, and data-leakage vulnerabilities with the utmost severity.

If you discover a security vulnerability in **Wizard**, please report it responsibly by following these exact steps:

1. **Do not** disclose the issue or proof-of-concept (PoC) code publicly until an official patch is released.
2. Create a **private security advisory** on GitHub or immediately email the core maintainers.
3. Provide a clear, detailed description of the vulnerability, step-by-step reproduction instructions, and the perceived scope of impact (e.g., "Docker Host Volume Escape via Pandas Evaluation").
4. We will acknowledge receipt within 48 hours and coordinate the patching phase directly with you.

## Supported Versions

We operate on a rapid-iteration cycle. We actively maintain and supply security patches exclusively for the latest generation, **Wizard w2**.

| Generation | Supported | Architecture Scope |
| ---------- | --------- | ------------------- |
| **w2 (current, v4.x)** | ✅ Yes | Host-primary execution with OS-native sandboxing (Landlock/seccomp on Linux, `sandbox-exec` on macOS, a restricted job object on Windows) by default; Docker opt-in via `EXECUTION_BACKEND=docker`. Local providers (Ollama, LM Studio) and cloud providers (Anthropic, OpenAI, gateways), gated by an explicit `local-only`/`cloud-only`/`hybrid` data mode. |
| w1 (legacy, v3.x and earlier) | ❌ No | Docker-only execution (no OS-native sandbox, no host-subprocess path); Ollama/LM Studio only, no cloud providers. |

## Security Best Practices Built-in

To secure the execution environment and the underlying repository, Wizard employs several strict layers of defense:

- **Per-session runtime isolation:** Generated code never runs in the API process. The default `EXECUTION_BACKEND=host` gives each session its own subprocess, restricted by the OS — Landlock and seccomp on Linux, a `sandbox-exec` profile on macOS, a job object and a Low integrity level on Windows — confining writes to the session workspace, denying outbound network (loopback aside) and capping memory and process counts. `EXECUTION_BACKEND=docker` gives each session a restricted-permission container instead, and remains the strongest option for data or questions you did not write yourself.
- **Containment is reported, not assumed:** `/settings` lists what this machine can enforce with a reason for every gap (outbound network is not enforced on Windows, and says so), and `GET /api/sandbox/selftest` spawns a probe that attempts each forbidden operation and reports what stopped it.
- **AST Guardrail Validation:** Before passing to the Python subset runner, we parse the abstract syntax tree to blacklist dangerous execution tokens (`exec`, `eval`, `os.system`, `subprocess`).
- **Data Cataloging Privacy:** The `CatalogEngine` automatically flags and masks standard PII payloads before loading dataframes into the cognitive memory layer.
- **CI/CD Static Analysis:** Github Actions implement continuous **CodeQL** and dependency auditing (`pip-audit` on every Python lockfile, `pnpm audit` on the frontend) on every pull request.

## Responsible Disclosure

We strictly enforce responsible disclosure practices. If you determine an exploit methodology:

- Limit your proof-of-concept strictly to a safe testing environment. Do not target production or hosted demonstration environments.
- Follow the **reporting** hierarchy detailed above.
- Restrict public posts about the finding until we have issued an explicit CVE or standard patch release notice.

For any immediate, high-severity security concerns, please contact our core integration team directly at [aniketsahaworkspace@gmail.com](mailto:aniketsahaworkspace@gmail.com).
