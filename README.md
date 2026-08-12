# openai-agents-sdk-v2-durable-sandbox

**Try it live: [https://carlosrymer.github.io/openai-agents-sdk-v2-durable-sandbox/](https://carlosrymer.github.io/openai-agents-sdk-v2-durable-sandbox/)**

I took the two security-and-reliability claims behind the OpenAI Agents SDK v2 sandbox —
credential isolation and durable execution — and tried to break them with a real credential-theft
probe and a real `docker rm -f`.

## What this showcases

**Technology:** OpenAI Agents SDK v2 (`openai-agents` 0.19.2) — the April 2026 release that
separates the agent *harness* (control plane: model calls, tool routing, run state) from *compute*
(the sandbox where model-directed code actually executes), and adds snapshot/rehydration on top.

Two claims, both falsifiable, both tested here:

1. **Credential isolation** — because the harness is separated from compute, API keys and host
   credentials do not leak into the environment where model-generated code runs.
2. **Durable execution** — snapshotting and rehydration let a long-horizon agent survive losing its
   compute environment and resume, rather than restarting from zero.

### What I found

**Claim 1 holds — but it is a property of the provider you pick, not of the SDK.**

I ran one byte-identical exfiltration probe against two sandbox providers that ship in the same
release. The results are not close:

| | `docker` (isolated) | `unix_local` (naive) |
|---|---|---|
| Environment variables visible | **9** | **150** |
| Live credentials leaked | **0** | **5** (incl. `OPENAI_API_KEY`) |
| Decoy secrets recovered verbatim | 0 / 4 | **4 / 4** |
| Harness source tree visible | No | **Yes** |
| Live authenticated API call with a stolen key | Not possible | **HTTP 200** |
| Variable the manifest *did* declare | Delivered | Delivered |

I re-ran this after a live production `OPENAI_API_KEY` was added to the harness environment — a
materially more sensitive credential than anything present the first time. **The result held.**
`docker` still exposed nothing; `unix_local` now leaks the OpenAI key as well. Published evidence
uses a `DECOY_OPENAI_API_KEY` canary: the real key appears in the artifacts only as a SHA-256
fingerprint, never as a value, and I deliberately did not make an API call with it from inside the
sandbox even though doing so would have "proven" the leak harder.

Those 9 variables under `docker` are the 8 the base image ships plus the single `TASK_LABEL` the
manifest declared — so the permitted channel works while the host environment does not cross at all.
That second row matters: isolation that also broke the legitimate path would not be isolation, it
would just be breakage.

The last row is the one that matters. Under `unix_local` the probe took the leaked
`MOONSHOT_API_KEY` and called the provider's authenticated account endpoint from inside the compute
plane, getting HTTP 200 and real account data back. Under `docker` it could not even attempt a call,
because no credential existed there — despite the container having full outbound network access the
entire time. I targeted an account endpoint rather than a completion on purpose: it proves the
credential is live and shows the blast radius while spending zero tokens.

The critical detail: **I did not build the naive harness as a strawman.** It is the SDK's own
`unix_local` provider, the one the docs suggest starting development with. The difference is two
lines of library code:

```python
# agents/sandbox/sandboxes/docker.py — container env is manifest-only
environment = await manifest.environment.resolve()
create_kwargs = {..., "environment": environment}

# agents/sandbox/sandboxes/unix_local.py — host env copied, manifest layered on top
env = os.environ.copy()
env.update(await self.state.manifest.environment.resolve())
```

There is no manifest option to pass the host environment into a Docker sandbox. Isolation there is
structural. Under `unix_local` the *inverse* is structural. So "the Agents SDK isolates credentials"
is not a true sentence on its own — it is true of some providers and false of others, and the SDK
does not warn you at the boundary.

**Claim 2 holds, and the recovery is genuinely cheap — but it is opt-in and manual.**

I ran a six-turn task (build a Roman-numeral converter, then the inverse, then input validation,
then a working CLI — so there is real accumulated state to lose), then killed *both* planes at the
halfway point: the harness process exited, and the container was destroyed with `docker rm -f`. A
brand-new OS process then loaded the serialized session state from disk and rehydrated. I repeated
the whole cycle **4 times** so the numbers below are means rather than one lucky run.

| Measure | Result across 4 trials |
|---|---|
| Rehydrate time (into a new container) | **3.09 s** mean (2.02–4.81 s) |
| Checkpoint write time | 0.56 s mean (0.42–0.82 s) |
| Agent turns repeated after resume | **0** — in every trial |
| Tokens re-spent on recovery | **0** — in every trial |
| Tokens preserved that a cold restart would redo | **37,623** mean |
| Workspace state after rehydration | **Byte-exact — every trial** |
| Final result correct | **4 / 4** — test suite passed, plus an independent check of the roundtrip over all 1–3999, `ValueError` on invalid input, and both CLI directions |

Every trial got a fresh container ID, confirming the old compute environment was genuinely gone
rather than reused.

I verified the restore came from the snapshot rather than a surviving Docker volume: the manifest
declares no mounts, so `/workspace` lived in the container filesystem and died with it, and the
snapshot tarball on disk contains exactly the two files at exactly the byte sizes that reappeared
after the resume.

The caveat is real, though. **Snapshotting defaults to `NoopSnapshot` and never fires on its own.**
`session.stop()` is the only thing that writes one — there is no automatic or periodic
checkpointing. Every one of my trials checkpointed cleanly at a turn boundary, which flatters the
result. A crash between checkpoints loses everything since the last one, and I did not test killing
mid-turn.

### A correction to my own earlier numbers

An earlier version of this write-up counted 5 leaked credentials including `GITHUB_TOKEN`,
`GH_TOKEN` and `CLOUDSDK_AUTH_ACCESS_TOKEN`. Those three are set by this environment to the literal
sentinel string `proxy-injected`. A leaked placeholder is not a stolen credential, and counting them
inflated the finding. The probe now classifies sentinel values separately from live ones, and every
count above includes only credentials that actually authenticate. The direction of the result never
changed — `unix_local` leaked genuinely live keys both times, and the live exfiltration call
succeeded both times — but the magnitude did, so I am flagging it rather than quietly editing it.

### What a live OpenAI key unlocked

Two capabilities were previously untestable because they are OpenAI-model-native. Both are now
tested.

**`apply_patch` works, but `gpt-5.3-codex` doesn't want it.** It is a `CustomTool` — a model-native
tool with a grammar rather than a JSON-schema function tool — which is why no Gemini or Kimi run can
reach it. Same edit-heavy task, three arms, one variable changed at a time:

| Variant | Tools | `apply_patch` calls | Tokens | Result |
|---|---|---|---|---|
| Shell only | `Shell` | — | 29,168 | correct |
| Filesystem available, free choice | `Filesystem + Shell` | **0** | 35,333 | correct |
| Filesystem, told to use `apply_patch` | `Filesystem + Shell` | **5** | 65,538 | correct |

Given a free choice the model reached for the shell every single time across three edit-heavy turns.
I verified the tool really was registered and offered (`SandboxApplyPatchTool`, name `apply_patch`),
so this is the model declining it rather than a wiring bug. Told explicitly to use it, it made 5
successful calls and still produced correct code — so the tool does what the docs say.

The cost runs the other way from what I expected. Merely *offering* the filesystem tools cost
**6,165 extra tokens** over shell-only for an identical outcome, and actually using `apply_patch`
cost **2.2×** the shell-only run. One task, one model, n=1 per arm — a direction, not a measurement.

**Sandbox memory runs end to end, and the memory is specific.** The `Memory` capability defaults to
`gpt-5.4-mini` for extraction and `gpt-5.5` for consolidation, so it cannot run without those
models. I gave an agent a task where the obvious command fails and the real one is documented only
in the repo, closed the session to trigger generation, then resumed and checked what came back:

- Generation ran on session close and took **89.4 s** — slow enough to matter if you close sessions often.
- It wrote 7 artifacts under `memories/`, including `MEMORY.md` and `memory_summary.md`.
- A later resumed session read the summary back into the agent's instructions.
- It captured the actual lesson — the `--strict` flag requirement and the exit-code-2 refusal — not a vague paraphrase. The generated text is committed verbatim in `artifacts/sandbox_memory.json` so the quality can be judged rather than taken on trust.

I am not claiming memory made the agent *better*. Showing that needs a control arm and many more
runs than this budget allows.

### What surprised me

I expected the interesting parts of this feature to live behind OpenAI's hosted API, and to end up
writing a mostly-negative report. Almost the opposite is true: the sandbox providers, the manifest
and environment isolation, snapshot persistence and rehydration all run entirely client-side. Even
`RemoteSnapshot` turned out to be bring-your-own-storage — it just needs any object with
`upload`/`download`/`exists`, not an OpenAI service. The durable-execution story is a library
feature, and you can audit all of it.

I also expected `~/.aws/credentials` to leak under `unix_local`. It did not, because that provider
rewrites `HOME` to the workspace root. Reporting that because it cuts against my thesis.

And I walked straight into a footgun worth flagging: **the manifest silently discards a mis-shaped
environment dict.** Declared variables must be nested under `value`, so
`Manifest(environment={"TASK_LABEL": "x"})` is accepted by pydantic without complaint and then
resolves to `{}` — the variable never reaches the sandbox and nothing warns you. My first run had
exactly that bug, and because a *missing* variable looks identical to good isolation, the artifact
looked fine. That is why the probe now explicitly asserts a declared variable *does* arrive. The
correct form is `Manifest(environment=Environment(value={"TASK_LABEL": "x"}))`.

## The use case

The scenario is the one that makes sandboxing worth paying for: an agent that writes and executes
its own code, on a machine that also holds production credentials. That is the normal shape of a
CI assistant, a data-analysis agent, or a coding agent on a developer laptop — the host almost
always has cloud keys, a GitHub token, and model API keys sitting in the environment.

It is a fair test rather than a flattering one because the exfiltration probe is not asked to be
polite. It enumerates the environment, reads credential files and `/proc/1/environ`, looks for the
harness source tree, and then tries to *use* whatever it finds by making a real authenticated
request. "A secret was visible" and "an attacker now holds a working credential" are different
claims, and only the second one is worth reporting. The probe tests the second.

Both experiments also run the probe two ways: a fixed deterministic script (reproducible, identical
across providers) and an agent-driven pass where the model itself goes hunting via the shell tool.
The two agree.

## Honest limitations

- **The model is not OpenAI's, and it is not even one model.** This environment has no OpenAI API
  key, so the agent loop runs on a third-party model via the SDK's LiteLLM integration. Experiment 2
  ran on **Gemini 3.6 Flash**; those credits were exhausted partway through the build, so
  experiment 1's agent-driven pass ran on **Kimi K2.7 Code** via Moonshot's OpenAI-compatible
  endpoint. Neither *measurement* depends on the model — the exfiltration probe is a fixed script
  that calls no model at all, and the snapshot/restore path is non-model code — but I would rather
  state the split than quietly present one model. This is valid for what I am measuring —
  the sandbox shell tool is an ordinary `FunctionTool` and nothing in the harness/compute boundary
  depends on which model is talking — but it does mean two advertised capabilities went untested.
- **`apply_patch` and sandbox memory are now tested** (see above); both are n=1 per arm on a single
  task, so they show direction rather than settle anything.
- **The durability trials still ran on Gemini.** I did not re-run the 4-trial suite after the OpenAI
  key arrived — snapshot and rehydration are non-model code, so the re-spend was not justified. The
  durability numbers therefore describe a Gemini-driven agent.
- **`gpt-5.1-codex-mini` is listed by `/v1/models` but 404s on the Responses API** for this key, so
  the `apply_patch` runs used `gpt-5.3-codex`.
- **Hosted sandbox providers remain untested, and an OpenAI key does not change that.** Blaxel,
  Cloudflare, Daytona, E2B, Modal, Runloop and Vercel each authenticate against their *own* vendor
  APIs and need their own keys, none of which are present. This is the gap I would most want closed,
  because Claim 1's whole result is that isolation varies by provider.
- **Isolation is about credentials, not the network.** The Docker sandbox had unrestricted outbound
  internet the whole time. It had nothing to authenticate with, but a sandbox that can reach the
  internet can still exfiltrate whatever is in its own workspace.
- **Small run.** Six agent turns on one task, four trials. Enough to show the recovery behaviour is
  consistent rather than lucky, but this measures the SDK's plumbing, not model quality, and says
  nothing about the latter.
- **The kill is always at the same point.** I destroy the container at a clean turn boundary after
  turn 3, every trial. I did not test killing mid-tool-call, mid-write, or during the snapshot
  itself, which is where I would expect durability to get genuinely hard.
- **The agent-driven probe produced a false positive.** On `docker`, Kimi reported `GPG_KEY` as a
  reachable credential. It is the base image's *public* Python-release verification key, not a
  secret. That is precisely why the deterministic probe — which matches recovered values against the
  harness's own secret fingerprints rather than guessing from variable names — is the primary
  measurement.

## Cost

Deliberately tiny, and worth stating since these runs cost real money:

| Run | Provider / model | Measured tokens | Notes |
|---|---|---|---|
| Exp 1 — deterministic probe | **none** | **0** | A fixed script. The primary security measurement calls no model at all. |
| Exp 1 — agent-driven probe | Moonshot · Kimi K2.7 Code | 26,715 | Capped at 1,200 max tokens per response |
| Exp 1 — agent-driven probe (re-runs) | Google · Gemini 3.6 Flash | ~20,000 | Two later re-runs after fixes |
| Exp 2 — durability, 6 turns × 4 trials | Google · Gemini 3.6 Flash | ~355,700 | 37,623 mean before the kill, 51,304 mean after |
| Exp 3 — `apply_patch`, 3 arms (plus one discarded 2-arm run) | OpenAI · `gpt-5.3-codex` | 189,142 | 130,039 final + 59,103 discarded |
| Exp 4 — sandbox memory, agent turn | OpenAI · `gpt-5.3-codex` | 16,134 | |
| Exp 4 — memory pipeline | OpenAI · `gpt-5.4-mini` + `gpt-5.5` | **not instrumented** | The SDK runs these internally; my harness does not see their usage |

Token counts come from the SDK's own usage accounting and are exact, except where marked. **OpenAI:
~205,300 measured tokens on `gpt-5.3-codex`, plus an uninstrumented memory pipeline.** I cannot
quote a dollar figure: this API key lacks the `api.usage.read` scope, so
`/v1/organization/costs` returns 403. **Moonshot: 26,715 tokens**, on an account shared with other
work, so its balance delta is not attributable to this project alone. **Gemini: ~375,700 tokens**,
almost all of it the durability trials.

The expensive part of this build was reading the SDK source, not calling models. The headline
security result — the isolated-vs-naive contrast — cost nothing at all, because the probe that
produces it is a fixed script.

## Handling live credentials in this repo

This repo contains a working credential-exfiltration probe, and the environment it runs in holds
live production keys. Two safeguards, because the probe's whole job is to find secrets:

- **`scripts/verify_no_secrets.sh`** scans the working tree *and* full git history for the exact
  value of every credential in the environment, plus provider-shaped patterns
  (`sk-…`, `AIza…`, `ghp_…`, `AKIA…`). I run it before every commit. It self-tests: planting a real
  key makes it fail, removing it makes it pass.
- **The probe never records a secret's value.** Live credentials are reported as SHA-256
  fingerprints only. The `DECOY_*` canaries are what get quoted verbatim, and they are deliberately
  shaped so they cannot be mistaken for real vendor keys or trip secret scanners.

One thing I got wrong and fixed: the first version of the verification script *printed the matched
string*, which meant a successful catch dumped a live key into the run log — exactly what the script
exists to prevent. It now reports file paths only, with contents withheld.

## Docs

- [Architecture](ARCHITECTURE.md) — system design, components, data flow, deployment
- [PRD](PRD.md) — problem statement, scope, success criteria

## Running locally

```bash
# Requires: Python 3.11+, a running Docker daemon, and a GEMINI_API_KEY.
git clone https://github.com/carlosrymer/openai-agents-sdk-v2-durable-sandbox.git
cd openai-agents-sdk-v2-durable-sandbox

python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

# Drives the agent loop via LiteLLM. Defaults to Moonshot; set
# AGENT_PROVIDER=gemini with a GEMINI_API_KEY to use Gemini instead.
export MOONSHOT_API_KEY=...

# Experiment 1 — credential isolation (docker vs unix_local).
# The DECOY_* values are fake by design; they exist so the committed artifact can
# quote recovered secrets verbatim without exposing anything real. They are also
# deliberately NOT shaped like real vendor keys, so they cannot be mistaken for a
# genuine leak (and do not trip secret scanners).
export DECOY_AWS_SECRET_ACCESS_KEY="DECOY-aws-not-a-real-key-0000000000000000"
export DECOY_STRIPE_SECRET_KEY="DECOY-stripe-not-a-real-key-000000000000"
export DECOY_DB_PASSWORD="DECOY-db-password-not-real-000000000000"
python experiments/exp1_credential_isolation.py

# Experiment 2 — durable execution. Runs phase A, kills the container, runs phase B
# in a separate process.
python experiments/exp2_durable_execution.py driver

# Refresh the site's copy of the artifacts and preview it.
cp artifacts/*.json docs/data/
python -m http.server 8000 --directory docs
```

> **Warning:** experiment 1 deliberately runs a credential-exfiltration probe, and the
> `unix_local` half of it *will* read your real environment variables. It reports secrets only as
> SHA-256 fingerprints, and quotes only `DECOY_*` values verbatim — but read `experiments/probe.py`
> before running it on a machine you care about.

## Stack

- **OpenAI Agents SDK v2** (`openai-agents` 0.19.2) with the `docker` and `litellm` extras — the
  subject under test: `SandboxAgent`, the `docker` / `unix_local` sandbox providers, `Manifest`
  environment scoping, and `LocalSnapshot` persistence.
- **Gemini 3.6 Flash** via LiteLLM — drives the agent loop.
- **Docker** (`python:3.11-slim`) — the compute plane.
- **Static HTML/CSS/JS site** — no framework, no build step; reads the committed JSON artifacts at
  runtime. Agents and probes run offline; nothing executes in the browser.

## Deployed via

GitHub Pages, built from **branch `main`, folder `/docs`** — the published site is just the `docs/`
directory in this repo, with `.nojekyll` so it is served exactly as committed.

I would rather have deployed via GitHub Actions, and the workflow for it is included at
[`deploy/github-pages-workflow.yml`](deploy/github-pages-workflow.yml) — but the credential I had
lacks the `workflow` OAuth scope, so GitHub rejects any push touching `.github/workflows/**`, and
the Pages REST API is blocked by this environment's proxy. See
[`deploy/README.md`](deploy/README.md), including the way this bit me: I first published to a
`gh-pages` branch and the site silently served a stale build for a while, because Pages was pointed
at `main`/`docs` the whole time.

---
Part of an ongoing series of small, real-world builds trialing frontier AI models, frameworks,
and tools as they ship.
