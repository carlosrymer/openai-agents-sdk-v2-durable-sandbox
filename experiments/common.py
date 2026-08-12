"""Shared harness plumbing for both experiments.

The point of this module is that it is the *harness* (control plane). Everything in
here runs in the trusted process that holds real credentials. None of it is supposed
to reach the compute plane (the sandbox).
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import time
from typing import Any

import docker as dockerlib

from agents.extensions.models.litellm_model import LitellmModel
from agents.sandbox import Manifest
from agents.sandbox.manifest import Environment
from agents.sandbox.sandboxes import (
    DockerSandboxClient,
    DockerSandboxClientOptions,
    UnixLocalSandboxClient,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
ARTIFACTS = REPO_ROOT / "artifacts"

# Model driving the agent loop. There is no OpenAI key in this environment, so the
# harness is pointed at a third-party model through LiteLLM. The SDK's sandbox machinery
# is provider-agnostic for the shell capability, which is what makes this substitution
# valid.
#
# Experiment 2 was run against Gemini 3.6 Flash. Those API credits were exhausted
# partway through this build, so experiment 1's agent-driven pass runs on Moonshot's
# Kimi K2.7 Code via the OpenAI-compatible endpoint. Neither experiment's *measurement*
# depends on the model: the exfiltration probe and the snapshot/restore path are
# non-model code.
MODEL_PROVIDER = os.environ.get("AGENT_PROVIDER", "moonshot")

if MODEL_PROVIDER == "gemini":
    MODEL_ID = "gemini/gemini-3.6-flash"
    MODEL_BASE_URL = None
    MODEL_KEY_ENV = "GEMINI_API_KEY"
else:
    MODEL_ID = "openai/kimi-k2.7-code"
    MODEL_BASE_URL = "https://api.moonshot.ai/v1"
    MODEL_KEY_ENV = "MOONSHOT_API_KEY"

SANDBOX_IMAGE = "python:3.11-slim"

# OpenAI model, used only where the SDK genuinely requires one: apply_patch (a model-native
# CustomTool) and sandbox memory. Kept to a small/cheap model deliberately.
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5.3-codex")

# Decoy secrets. These are fake values that exist only so the exfiltration probe has
# something safe to quote verbatim in a committed artifact. Real credentials are only
# ever reported as a SHA-256 fingerprint, never as a value.
# These are deliberately NOT shaped like real credentials of their respective vendors —
# a decoy that matches a live key pattern trips secret scanners on every push, and a
# committed artifact that quotes it is indistinguishable from a genuine leak.
DECOYS = {
    "DECOY_AWS_SECRET_ACCESS_KEY": "DECOY-aws-not-a-real-key-0000000000000000",
    "DECOY_STRIPE_SECRET_KEY": "DECOY-stripe-not-a-real-key-000000000000",
    "DECOY_DB_PASSWORD": "DECOY-db-password-not-real-000000000000",
    # Canary standing in for a model-provider key. The REAL OPENAI_API_KEY is only ever
    # reported by fingerprint; this decoy is what demonstrates verbatim recoverability
    # in a public artifact.
    "DECOY_OPENAI_API_KEY": "DECOY-openai-not-a-real-key-00000000000000",
}

# Credential-shaped env vars the harness process holds. Not all of these are real:
# this environment routes git through a proxy that replaces some tokens with the
# sentinel string "proxy-injected". A sentinel that leaks is not a stolen credential,
# so they are classified rather than counted together. Getting this wrong once already
# inflated my own published leak count.
CANDIDATE_SECRET_NAMES = [
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "MOONSHOT_API_KEY",
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "CLOUDSDK_AUTH_ACCESS_TOKEN",
    "AWS_SECRET_ACCESS_KEY",
    "BFL_API_KEY",
]

# Values that look like credentials but are placeholders injected by the environment.
PLACEHOLDER_VALUES = {"proxy-injected", "", "unset", "none", "changeme"}


def is_live_credential(value: str) -> bool:
    """A value only counts as a real credential if it is not an obvious sentinel."""
    return bool(value) and value.strip().lower() not in PLACEHOLDER_VALUES and len(value) >= 20


def fingerprint(value: str) -> str:
    """Stable, non-reversible identifier for a secret value."""
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()[:16]


def secret_fingerprints() -> dict[str, str]:
    """Fingerprints of the LIVE credentials the harness holds, for leak detection.

    Placeholder-valued vars are excluded: a leaked sentinel is not a stolen credential.
    """
    out: dict[str, str] = {}
    for name in CANDIDATE_SECRET_NAMES:
        val = os.environ.get(name)
        if val and is_live_credential(val):
            out[name] = fingerprint(val)
    return out


def placeholder_credentials() -> dict[str, str]:
    """Credential-shaped vars that are actually sentinels, reported for honesty."""
    out: dict[str, str] = {}
    for name in CANDIDATE_SECRET_NAMES:
        val = os.environ.get(name)
        if val and not is_live_credential(val):
            out[name] = val if val in PLACEHOLDER_VALUES else "(short/non-credential value)"
    return out


def build_model() -> LitellmModel:
    key = os.environ.get(MODEL_KEY_ENV)
    if not key:
        raise RuntimeError(f"{MODEL_KEY_ENV} must be set in the harness process")
    return LitellmModel(model=MODEL_ID, api_key=key, base_url=MODEL_BASE_URL)


def docker_client() -> DockerSandboxClient:
    return DockerSandboxClient(dockerlib.from_env())


def docker_options() -> DockerSandboxClientOptions:
    return DockerSandboxClientOptions(image=SANDBOX_IMAGE)


def unix_local_client() -> UnixLocalSandboxClient:
    return UnixLocalSandboxClient()


def write_artifact(name: str, payload: dict[str, Any]) -> pathlib.Path:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    path = ARTIFACTS / name
    payload.setdefault("generated_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n")
    print(f"[artifact] wrote {path}")
    return path


def manifest_for(root: str) -> Manifest:
    """A manifest that declares exactly one non-secret env var.

    This is the whole security model: whatever is NOT in here should not exist in the
    compute plane. Note there is no way to say "pass through the host environment".
    """
    # NOTE the nesting. `environment={"TASK_LABEL": ...}` is accepted by pydantic and then
    # silently resolves to {} — the variable never reaches the sandbox and nothing warns
    # you. The declared vars have to live under `value`. I got this wrong first time round.
    return Manifest(
        root=root,
        environment=Environment(value={"TASK_LABEL": "agents-sdk-v2-sandbox-demo"}),
    )
