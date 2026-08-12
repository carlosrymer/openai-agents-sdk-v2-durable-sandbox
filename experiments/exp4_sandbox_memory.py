"""Experiment 4 — sandbox memory, the other capability an OpenAI key unlocks.

The `Memory` capability runs a two-phase pipeline whose models default to `gpt-5.4-mini`
(per-rollout extraction) and `gpt-5.5` (consolidation). Those defaults are why this was
untestable without an OpenAI key.

What I check, in order of how much I trust it:

  1. Does generation actually run and write files? (verifiable on the filesystem)
  2. What does it write? (committed verbatim, so anyone can judge the quality)
  3. Does a LATER session read that memory back into the agent's instructions?
     (verifiable by calling the capability's own instructions() against a resumed session)

I am deliberately not claiming a behavioural improvement. Showing that memory changes what
an agent does would need a control arm and many more runs than this budget allows.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from agents import RunConfig, Runner  # noqa: E402
from agents.sandbox import SandboxAgent, SandboxRunConfig  # noqa: E402
from agents.sandbox.capabilities import Filesystem, Memory, Shell  # noqa: E402
from agents.sandbox.snapshot import LocalSnapshotSpec  # noqa: E402

import common  # noqa: E402

WORKSPACE = "/workspace"
STATE_DIR = common.REPO_ROOT / ".run_state" / "memory"
SNAPSHOT_DIR = STATE_DIR / "snapshots"

# A task with a genuinely memorable lesson in it: the obvious command fails, and there is
# a project-specific way to do it that only shows up by reading the repo.
SETUP_FILES = {
    "README.md": (
        "# calc\n\n"
        "IMPORTANT PROJECT CONVENTION: do NOT run the tests with `python3 test_calc.py`.\n"
        "That entrypoint is disabled and will fail. Tests must be run with:\n\n"
        "    python3 run_tests.py --strict\n\n"
        "The --strict flag is required; without it the runner refuses to start.\n"
    ),
    "calc.py": "def add(a, b):\n    return a + b\n",
    "test_calc.py": (
        "import sys\n"
        "print('This entrypoint is disabled. See README.md for the project convention.')\n"
        "sys.exit(3)\n"
    ),
    "run_tests.py": (
        "import sys\n"
        "if '--strict' not in sys.argv:\n"
        "    print('refusing to start without --strict')\n"
        "    sys.exit(2)\n"
        "sys.path.insert(0, '/workspace')\n"
        "import calc\n"
        "assert calc.add(2, 3) == 5\n"
        "print('all tests passed (strict mode)')\n"
    ),
}


async def seed_workspace(session) -> None:
    import io
    for name, body in SETUP_FILES.items():
        await session.write(f"{WORKSPACE}/{name}", io.BytesIO(body.encode()))


def build_memory_capability() -> Memory:
    # Defaults are used deliberately: the point is to test the SDK's own configuration,
    # including its default gpt-5.4-mini / gpt-5.5 model choices.
    return Memory()


async def read_dir(session, path: str) -> dict:
    res = await session.exec("sh", "-lc", f"ls -1 {path} 2>/dev/null || true", timeout=60)
    out = res.stdout or b""
    if isinstance(out, bytes):
        out = out.decode(errors="replace")
    names = [x.strip() for x in out.splitlines() if x.strip()]
    files = {}
    for n in names:
        r = await session.exec("sh", "-lc", f"cat {path}/{n}", timeout=60)
        c = r.stdout or b""
        if isinstance(c, bytes):
            c = c.decode(errors="replace")
        files[n] = c[:4000]
    return files


async def main() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    model_id = common.OPENAI_MODEL
    print(f"[exp4] agent model={model_id}; memory models are the SDK defaults")

    client = common.docker_client()
    options = common.docker_options()
    manifest = common.manifest_for(WORKSPACE)
    snapshot_spec = LocalSnapshotSpec(base_path=SNAPSHOT_DIR)

    record: dict = {
        "experiment": "sandbox_memory",
        "agent_model": model_id,
        "memory_models": {
            "phase_one_extraction": "gpt-5.4-mini (SDK default)",
            "phase_two_consolidation": "gpt-5.5 (SDK default)",
        },
        "why_this_needed_an_openai_key": (
            "MemoryGenerateConfig defaults phase_one_model to gpt-5.4-mini and "
            "phase_two_model to gpt-5.5, so the memory pipeline cannot run at all without "
            "access to those OpenAI models."
        ),
    }

    # ---------- Session A: hit the failure, learn the convention ----------
    session = await client.create(manifest=manifest, options=options, snapshot=snapshot_spec)
    await session.start()
    await seed_workspace(session)

    agent = SandboxAgent(
        name="calc-dev",
        instructions=(
            "You are working in a small Python project at /workspace. Use the shell. "
            "If a command fails, read the repo to work out the right way to do it. Be terse."
        ),
        model=model_id,
        capabilities=[Shell(), Filesystem(), build_memory_capability()],
        default_manifest=manifest,
    )

    t0 = time.time()
    res = await Runner.run(
        agent,
        "Run the test suite for this project and report the exact output.",
        run_config=RunConfig(
            sandbox=SandboxRunConfig(client=client, session=session, options=options),
            tracing_disabled=True,
        ),
        max_turns=20,
    )
    u = res.context_wrapper.usage
    record["session_a"] = {
        "wall_clock_s": round(time.time() - t0, 2),
        "agent_tokens": getattr(u, "total_tokens", 0),
        "final_output": (res.final_output or "")[:600],
        "discovered_strict_convention": "--strict" in (res.final_output or ""),
    }
    print(f"[session A] done in {record['session_a']['wall_clock_s']}s, "
          f"{record['session_a']['agent_tokens']} agent tokens")

    state_payload = session.state.model_dump(mode="json")

    # Closing the session is what triggers memory extraction + consolidation.
    print("[session A] closing session -> memory generation should run")
    t0 = time.time()
    await session.stop()
    await session.aclose()
    record["memory_generation_seconds"] = round(time.time() - t0, 2)
    print(f"[memory] generation took {record['memory_generation_seconds']}s")

    try:
        await client.delete(session)
    except Exception:
        pass

    # ---------- Session B: resume and see what memory persisted ----------
    state = client.deserialize_session_state(state_payload)
    session_b = await client.resume(state)
    await session_b.start()

    memories = await read_dir(session_b, f"{WORKSPACE}/memories")
    sessions = await read_dir(session_b, f"{WORKSPACE}/sessions")
    record["memory_files"] = {
        "memories_dir": {k: v for k, v in memories.items()},
        "sessions_dir_filenames": sorted(sessions.keys()),
        "memories_generated": len(memories) > 0,
        "rollout_segments_written": len(sessions) > 0,
    }
    print(f"[memory] memories/={sorted(memories)} sessions/={sorted(sessions)}")

    # Does the capability actually inject that memory into a later run's instructions?
    cap = build_memory_capability()
    cap.bind(session_b)
    injected = None
    try:
        injected = await cap.instructions(manifest)
    except Exception as e:
        injected = f"(instructions() raised: {str(e)[:200]})"
    record["memory_injected_into_later_session"] = {
        "instructions_returned": bool(injected),
        "instructions_text": (injected or "")[:2000],
        "mentions_strict_convention": "--strict" in (injected or ""),
    }
    print(f"[memory] injected into later session: {bool(injected)}")

    try:
        await session_b.shutdown()
        await client.delete(session_b)
    except Exception:
        pass

    record["verdict"] = {
        "generation_ran": record["memory_files"]["memories_generated"],
        "read_back_on_later_session": bool(injected),
        "captured_the_project_convention": (
            record["memory_injected_into_later_session"]["mentions_strict_convention"]
            or any("--strict" in v for v in memories.values())
        ),
    }
    common.write_artifact("sandbox_memory.json", record)
    print("\n=== RESULT ===")
    print(json.dumps(record["verdict"], indent=2))


if __name__ == "__main__":
    asyncio.run(main())
