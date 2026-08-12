# PRD — openai-agents-sdk-v2-durable-sandbox

## Problem statement

The OpenAI Agents SDK v2 (April 2026) is marketed on a specific architectural idea: separating the
agent **harness** (control plane) from **compute** (the sandbox where model-directed code runs). Two
concrete benefits are claimed for that split:

1. **Credential isolation** — API keys and host credentials do not reach the environment where
   model-generated code executes.
2. **Durable execution** — snapshotting and rehydration let a long-horizon agent survive losing its
   compute environment and resume, rather than restarting from zero.

Both are the kind of claim that gets repeated in blog posts and adopted as an assumption, and both
are the kind that quietly decides whether an incident happens. They are also both falsifiable, which
makes them worth actually testing rather than reasoning about.

The gap this fills: I could find plenty of description of what the SDK is supposed to do, and no
adversarial measurement of whether it does. "Sandboxed" is a word that gets applied to setups
offering very different amounts of protection.

## Target user

Engineers evaluating the Agents SDK v2 for work where an agent writes and runs its own code on
infrastructure that also holds real credentials — CI assistants, data-analysis agents, coding agents
on developer laptops. Specifically the person who has to answer "if this agent goes rogue or gets
prompt-injected, what can it actually reach?" and "if the box dies four hours into a run, how much
work is gone?"

Secondarily: anyone deciding *which* sandbox provider to configure, since that turns out to be the
decision that matters most.

## Goals

- Test claim 1 adversarially: run a real credential-theft probe inside the compute plane and
  determine not just what is *visible* but what is *usable*.
- Test claim 2 destructively: kill a real container mid-task and measure whether the run resumes with
  intermediate state intact, and at what cost.
- Establish precisely which parts of the sandbox stack are library-side versus OpenAI-hosted — the
  distinction that determines whether any of this is testable or auditable without a vendor key.
- Produce committed, auditable run artifacts so every number published can be checked against raw
  output.
- Publish an interactive static page that makes the isolated-vs-naive contrast and the recovery cost
  legible at a glance.
- Be explicit about every sub-claim that went untested and what testing it would require.

## Non-goals

- Benchmarking model quality. The agent loop here is plumbing exercise, not an eval.
- Testing all nine sandbox providers. Seven need vendor keys I do not have.
- Building a reusable sandbox-security test suite or a product. This is one measurement, published.
- Testing prompt-injection resistance. I test what a compromised agent can *reach*, not how easily it
  becomes compromised.
- Performance benchmarking of the sandbox (throughput, cold-start at scale).

## Scope (MVP)

**Experiment 1 — credential isolation.** One byte-identical exfiltration probe, executed via the same
`session.exec` path the model's shell tool uses, against two providers shipping in the same release:
`docker` and `unix_local`. The probe enumerates the environment, fingerprints credential-shaped
values, quotes fake `DECOY_*` values verbatim, reads credential files and `/proc/1/environ`, checks
harness-source visibility, tests network egress, and attempts a real authenticated API call with any
key it recovers. Run twice: deterministic payload (primary, reproducible) and agent-driven (realism
check).

**Experiment 2 — durable execution.** A six-turn task with genuine accumulating state: build a Roman
numeral converter, extend it to the full subtractive range, add the inverse with a roundtrip test,
add `ValueError` handling for invalid input, add a working CLI, then run everything. After turn 3:
hash the workspace, checkpoint via `session.stop()`, serialize session state to disk, exit the
process. Destroy the container with `docker rm -f` and confirm. A fresh process rehydrates and
completes turns 4–6, each of which must build on files written before the kill. Measure turns
repeated, tokens re-spent, wall-clock recovery, and whether the final result is correct — with
correctness verified by the harness, not trusted from the model. The whole cycle is repeated across
several independent trials so the recovery figures are means, not one anecdote.

**Deliverable.** Committed JSON artifacts, a static GitHub Pages site reading them, and README /
ARCHITECTURE / PRD.

Deliberately kept small: two experiments, one task, six turns, four durability trials. Token spend
is real and the questions are architectural, not statistical — and the primary security measurement
calls no model at all, so the headline result costs nothing to reproduce.

## User stories

- As an **engineer evaluating the SDK for a coding agent**, I want to see what a hostile probe can
  actually retrieve from inside the sandbox, so that I can decide whether the isolation is real
  before putting it near production credentials.
- As an **engineer choosing a sandbox provider**, I want a side-by-side of two shipping providers, so
  that I understand the choice is load-bearing rather than a matter of convenience.
- As an **engineer planning for long-running agents**, I want the real cost of recovering from a
  destroyed compute environment in turns, tokens and seconds, so that I can decide whether to design
  for durability or just retry.
- As a **skeptical reader**, I want the raw run artifacts in the repo, so that I can check the
  published numbers rather than trusting a screenshot.
- As a **reader deciding whether this generalises**, I want an explicit list of what went untested
  and why, so that I do not over-read the result.

## Success criteria

The build succeeds if it answers both claims with evidence and states its own limits honestly. It
does.

**Claim 1 — holds, conditionally, and the condition is the headline.** Under the `docker` provider
the compute plane saw 9 environment variables — the 8 the base image ships plus the single one the
manifest declared — zero real harness secrets, and zero of three decoys;
the harness source tree was invisible and the exfiltration call could not be attempted for lack of
any credential. Under `unix_local` the same probe saw 150 variables, leaked 5 real secrets and all 3
decoys verbatim, could read the harness source, and **successfully made a real authenticated API
call from inside the compute plane (HTTP 200), retrieving live account data with the stolen
key**.

The important part is that the naive side is not a strawman: it is the SDK's own `unix_local`
provider, the one the docs suggest developing against, and the difference is two lines of library
code (`docker.py` builds container env from the manifest alone; `unix_local.py` starts from
`os.environ.copy()`). So the honest verdict is that "the Agents SDK isolates credentials" is not a
true sentence unqualified — it is a property of the provider, and the SDK does not warn you at the
boundary when you pick the leaky one.

**Claim 2 — holds, and recovery is cheap, but durability is opt-in.** Across **4 of 4 trials**, with
both the harness process and the container destroyed each time, a fresh process rehydrated in
**3.09 s mean** (2.02–4.81 s) into a new container with a **byte-exact** workspace, repeated **0**
turns, re-spent **0** tokens, and preserved **37,623** tokens on average. Every trial finished with a
passing test suite plus an independent check covering the roundtrip over all 3,999 values,
`ValueError` on invalid input, and both CLI directions. I confirmed the restore came from the
snapshot tarball rather than a surviving Docker volume. The qualifiers: snapshotting defaults to
`NoopSnapshot` and only ever fires when the developer calls `session.stop()`, and every trial killed
the container at the same clean turn boundary — I did not test killing mid-tool-call or during the
snapshot itself.

**The result held under a live production credential.** The isolation experiment was re-run after a
production `OPENAI_API_KEY` entered the harness environment. `docker` still exposed nothing;
`unix_local` leaked the OpenAI key along with everything else. Published evidence uses a canary; the
real key appears only as a fingerprint.

**Two previously-untestable capabilities are now covered.** `apply_patch` works when the model is
told to use it (5 successful calls, correct output) but `gpt-5.3-codex` never chose it
spontaneously, preferring the shell across three edit-heavy turns — and offering it cost 6,165 extra
tokens for an identical result, while using it cost 2.2×. Sandbox memory runs end to end: generation
fires on session close (89.4 s), writes 7 artifacts, is read back into a resumed session, and
captured the specific project convention rather than a paraphrase.

**Where it still falls short.** Seven hosted sandbox providers remain untested because each needs
its own vendor key — which stings precisely because Claim 1's result is that provider choice decides
the outcome. The durability suite was not re-run on an OpenAI model. And I corrected my own earlier
leak count downward after discovering three of the "leaked credentials" were the sentinel string
`proxy-injected`.

Secondary criteria, all met: artifacts committed and auditable; the site reads them rather than
hardcoding numbers; the page is responsive and dark/light aware; no unfilled placeholders in the docs.

## Risks / open questions

- **Generalisation risk.** Two providers tested, nine exist. Whether E2B, Modal, Daytona et al.
  behave like `docker` or like `unix_local` is unknown, and given the finding, it is exactly the
  question a reader will have next.
- **The `unix_local` result could be read as a bug report.** I do not think it is one — running on the
  host is what that provider is *for*, and it is documented as a development convenience. The risk is
  that the convenience is a footgun with no guardrail: nothing in the API makes the security
  difference visible at the call site, and the two providers are otherwise interchangeable by design.
- **Checkpoint granularity is the open durability question.** "0 tokens re-spent" is true of a clean
  checkpoint at a turn boundary. Nobody has measured the expected loss under a realistic crash
  distribution, and the SDK gives no periodic-checkpoint primitive to make that cheap.
- **Model substitution.** Using third-party models rather than a GPT-5-series model is sound for the
  boundary questions I measured (the shell tool is a plain `FunctionTool`, and the primary probe
  calls no model at all), but it does mean the OpenAI-model-native paths are a blind spot, and those
  are the paths most users will take. Two models also appear across the two experiments — Gemini 3.6
  Flash for durability, Kimi K2.7 Code for the agent-driven probe after Gemini credits ran out.
- **`/proc/1/environ` was readable in both providers.** Harmless here, but it is an artifact of how
  these containers start rather than a guarantee, and a different image or entrypoint could change
  that.
- **A mis-shaped manifest env dict is discarded silently.** `Manifest(environment={...})` type-checks
  and then resolves to `{}`; declared variables must be nested under `value`. My first run hit this,
  and the failure mode is nasty precisely because a variable that never arrived is indistinguishable
  from successful isolation in the results. The probe now asserts the permitted channel works.
- **Snapshot contents are not encrypted.** The tarball sits in plaintext on host disk. Fine for this
  demo; worth thinking about for a workspace that has handled sensitive data.

## Timeline

Single sitting, roughly in this order:

1. **Research** — read the shipped package rather than the marketing: map `agents/sandbox/`, find the
   provider interface, locate where container environment is actually constructed, determine what
   `RemoteSnapshot` needs. This is where the two key code sites turned up and where the plan changed
   (no custom Docker provider needed; no strawman harness needed).
2. **Smoke test** — prove Gemini can drive a real Docker sandbox end to end before building anything
   on that assumption.
3. **Experiment 1** — probe, harness, two providers, deterministic + agent-driven. One correction
   mid-flight: the first exfiltration attempt returned 404 on a model id unavailable to my key, which
   would have read as a false negative for isolation; fixed and re-ran.
4. **Experiment 2** — two-phase durability test, plus verification that the snapshot (not a Docker
   volume) was the restore source.
5. **Site** — static page reading the artifacts; validated the palette, screenshotted light/dark/mobile,
   fixed an icon-sizing bug found by looking at the render.
6. **Docs and deploy** — README / ARCHITECTURE / PRD, Pages workflow, verify live.
