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
}

# Real credential names the harness process legitimately holds.
REAL_SECRET_NAMES = ["GEMINI_API_KEY", "MOONSHOT_API_KEY", "GITHUB_TOKEN"]


def fingerprint(value: str) -> str:
    """Stable, non-reversible identifier for a secret value."""
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()[:16]


def secret_fingerprints() -> dict[str, str]:
    """Fingerprints of the real secrets the harness holds, for leak detection."""
    out: dict[str, str] = {}
    for name in REAL_SECRET_NAMES:
        val = os.environ.get(name)
        if val:
            out[name] = fingerprint(val)
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
    return Manifest(
        root=root,
        environment={"TASK_LABEL": "frontier-showcase-sandbox"},
    )
