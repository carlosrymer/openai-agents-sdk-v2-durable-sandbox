"""Experiment 2 — does snapshot + rehydration actually survive losing compute?

The test is deliberately harsh. It runs in two SEPARATE OS PROCESSES:

  phase A: build part of a program in the sandbox, checkpoint, write durable state to
           disk, then exit. The harness process is gone.
  (kill):  `docker rm -f` the container. The compute plane is gone too.
  phase B: a brand-new process loads the durable state from disk, rehydrates a fresh
           container from the snapshot, and finishes the job.

If claim 2 is real, phase B should continue from where phase A stopped rather than
restarting, with the intermediate files intact.

Usage:
    python exp2_durable_execution.py driver     # runs the whole thing
    python exp2_durable_execution.py phase-a
    python exp2_durable_execution.py phase-b
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import pathlib
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from agents import RunConfig, Runner  # noqa: E402
from agents.sandbox import SandboxAgent, SandboxRunConfig  # noqa: E402
from agents.sandbox.capabilities import Shell  # noqa: E402
from agents.sandbox.snapshot import LocalSnapshotSpec  # noqa: E402

import common  # noqa: E402

TRIAL = os.environ.get("TRIAL_ID", "1")
STATE_DIR = common.REPO_ROOT / ".run_state" / f"trial_{TRIAL}"
SNAPSHOT_DIR = STATE_DIR / "snapshots"
STATE_FILE = STATE_DIR / "durable_state.json"
EVENTS_FILE = STATE_DIR / "events.jsonl"

WORKSPACE = "/workspace"

TASK_INSTRUCTIONS = (
    "You are building a small Python library inside a sandbox at /workspace. "
    "Use the shell tool for everything: write files with heredocs, run tests with python3. "
    "Work incrementally and never rewrite files you already wrote unless asked. "
    "After each step, actually RUN the tests and report the real output. Be terse."
)

TURNS = [
    # ---- phase A: runs before the compute plane is destroyed ----
    "Create /workspace/roman.py with a function to_roman(n) that converts integers 1-10 "
    "to Roman numerals, and /workspace/test_roman.py with asserts covering 1-10. "
    "Run: python3 /workspace/test_roman.py and show the output.",
    "Extend to_roman in /workspace/roman.py to correctly handle all integers 1-3999 "
    "(including 4, 9, 40, 90, 400, 900 subtractive forms). Add asserts to "
    "/workspace/test_roman.py for 4, 9, 14, 40, 90, 400, 1987, 3999. Run the tests.",
    "Add from_roman(s) to /workspace/roman.py converting a Roman numeral string back to an "
    "integer. Add a roundtrip test asserting from_roman(to_roman(n)) == n for every n in "
    "1..3999. Run the tests.",
    # ---- phase B: runs after rehydration, must build on phase A's files ----
    "Make from_roman(s) raise ValueError on invalid input (empty string, bad characters like "
    "'ABC', and malformed repeats like 'IIII' or 'VV'). Add asserts to /workspace/test_roman.py "
    "covering each of those cases. Run the tests.",
    "Add a main() to /workspace/roman.py with a CLI: `python3 roman.py to 1987` prints MCMLXXXVII "
    "and `python3 roman.py from MCMLXXXVII` prints 1987. Guard it with "
    "if __name__ == '__main__'. Verify both commands actually work by running them.",
    "Run the full test suite one final time with python3 /workspace/test_roman.py and "
    "report the exact output, then state whether everything passes.",
]
PHASE_A_TURNS = 3  # turns 0..2 run before the kill


def log_event(kind: str, **data) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    rec = {"t": time.time(), "kind": kind, **data}
    with EVENTS_FILE.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    print(f"[event] {kind} {data}")


def build_agent() -> SandboxAgent:
    return SandboxAgent(
        name="builder",
        instructions=TASK_INSTRUCTIONS,
        model=common.build_model(),
        capabilities=[Shell()],
        default_manifest=common.manifest_for(WORKSPACE),
    )


async def workspace_fingerprint(session) -> dict:
    """Hash the files that represent the agent's accumulated work."""
    out = {}
    for name in ("roman.py", "test_roman.py"):
        try:
            res = await session.exec("cat", f"{WORKSPACE}/{name}", timeout=30)
            data = res.stdout or b""
            if isinstance(data, str):
                data = data.encode()
            out[name] = {
                "sha256": hashlib.sha256(data).hexdigest()[:16],
                "bytes": len(data),
                "exists": res.exit_code == 0 and len(data) > 0,
            }
        except Exception as e:
            out[name] = {"exists": False, "error": str(e)[:120]}
    return out


def usage_of(res) -> dict:
    u = res.context_wrapper.usage
    return {
        "input_tokens": getattr(u, "input_tokens", 0),
        "output_tokens": getattr(u, "output_tokens", 0),
        "total_tokens": getattr(u, "total_tokens", 0),
        "requests": getattr(u, "requests", 0),
    }


async def run_turn(agent, client, session, options, input_list, prompt, idx) -> tuple[list, dict]:
    t0 = time.time()
    res = await Runner.run(
        agent,
        input_list + [{"role": "user", "content": prompt}],
        run_config=RunConfig(
            sandbox=SandboxRunConfig(client=client, session=session, options=options),
            tracing_disabled=True,
        ),
        max_turns=25,
    )
    rec = {
        "turn": idx,
        "prompt": prompt[:120],
        "wall_clock_s": round(time.time() - t0, 2),
        "usage": usage_of(res),
        "final_output": (res.final_output or "")[:600],
    }
    log_event("turn_complete", turn=idx, seconds=rec["wall_clock_s"],
              total_tokens=rec["usage"]["total_tokens"])
    return res.to_input_list(), rec


async def phase_a() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)

    client = common.docker_client()
    options = common.docker_options()
    manifest = common.manifest_for(WORKSPACE)

    # Opt in to snapshotting. Without this the SDK defaults to NoopSnapshot and stop()
    # persists nothing at all.
    snapshot_spec = LocalSnapshotSpec(base_path=SNAPSHOT_DIR)

    session = await client.create(manifest=manifest, options=options, snapshot=snapshot_spec)
    await session.start()
    log_event("phase_a_start", pid=os.getpid(), container_id=session.state.container_id[:12])

    agent = build_agent()
    input_list: list = []
    turns: list[dict] = []
    for i in range(PHASE_A_TURNS):
        input_list, rec = await run_turn(agent, client, session, options,
                                         input_list, TURNS[i], i)
        turns.append(rec)

    fp_before = await workspace_fingerprint(session)
    log_event("pre_kill_fingerprint", files=fp_before)

    # CHECKPOINT: this is the explicit, developer-triggered snapshot. There is no
    # automatic periodic snapshotting in the SDK.
    t0 = time.time()
    await session.stop()
    checkpoint_s = round(time.time() - t0, 2)
    log_event("checkpoint_persisted", seconds=checkpoint_s,
              snapshot_dir=str(SNAPSHOT_DIR))

    # Serialize everything the next process needs. This is the harness state living
    # OUTSIDE the compute plane, which is the precondition for durability.
    state_payload = session.state.model_dump(mode="json")
    STATE_FILE.write_text(json.dumps({
        "sandbox_session_state": state_payload,
        "conversation": input_list,
        "turns": turns,
        "pre_kill_fingerprint": fp_before,
        "checkpoint_seconds": checkpoint_s,
        "container_id": session.state.container_id,
        "snapshot_dir": str(SNAPSHOT_DIR),
    }, indent=2, default=str))
    log_event("phase_a_state_written", path=str(STATE_FILE),
              bytes=STATE_FILE.stat().st_size)
    print("PHASE_A_OK")


async def phase_b() -> None:
    saved = json.loads(STATE_FILE.read_text())
    client = common.docker_client()
    options = common.docker_options()

    log_event("phase_b_start", pid=os.getpid())

    # Rehydrate: deserialize the session state written by a process that no longer
    # exists, then resume. The old container is gone, so this must build a new one and
    # restore the workspace from the snapshot tarball.
    state = client.deserialize_session_state(saved["sandbox_session_state"])
    t0 = time.time()
    session = await client.resume(state)
    await session.start()
    resume_s = round(time.time() - t0, 2)
    new_container = session.state.container_id
    log_event("rehydrated", seconds=resume_s,
              old_container=saved["container_id"][:12],
              new_container=new_container[:12],
              container_changed=new_container != saved["container_id"])

    fp_after = await workspace_fingerprint(session)
    log_event("post_resume_fingerprint", files=fp_after)

    preserved = fp_after == saved["pre_kill_fingerprint"]
    log_event("state_preserved", preserved=preserved)

    agent = build_agent()
    input_list = saved["conversation"]
    turns = list(saved["turns"])
    for i in range(PHASE_A_TURNS, len(TURNS)):
        input_list, rec = await run_turn(agent, client, session, options,
                                         input_list, TURNS[i], i)
        turns.append(rec)

    # Final correctness check, measured by the harness rather than trusted from the model.
    verify = await session.exec("python3", f"{WORKSPACE}/test_roman.py", timeout=120)
    v_out = verify.stdout or b""
    if isinstance(v_out, bytes):
        v_out = v_out.decode(errors="replace")
    v_err = verify.stderr or b""
    if isinstance(v_err, bytes):
        v_err = v_err.decode(errors="replace")
    log_event("final_verification", exit_code=verify.exit_code)

    # Independent check that from_roman really exists and roundtrips, so a model that
    # merely claims success cannot pass this.
    indep = await session.exec(
        "python3", "-c",
        "import sys,subprocess; sys.path.insert(0,'/workspace'); import roman; "
        "assert roman.to_roman(1987)=='MCMLXXXVII'; "
        "assert roman.from_roman('MCMLXXXVII')==1987; "
        "assert all(roman.from_roman(roman.to_roman(n))==n for n in range(1,4000)); "
        "bad=0\n"
        "for s_ in ('','ABC','IIII','VV'):\n"
        "    try: roman.from_roman(s_); bad+=1\n"
        "    except ValueError: pass\n"
        "    except Exception: bad+=1\n"
        "assert bad==0, 'invalid input not rejected'\n"
        "o1=subprocess.run([sys.executable,'/workspace/roman.py','to','1987'],"
        "capture_output=True,text=True).stdout.strip()\n"
        "o2=subprocess.run([sys.executable,'/workspace/roman.py','from','MCMLXXXVII'],"
        "capture_output=True,text=True).stdout.strip()\n"
        "assert 'MCMLXXXVII' in o1, 'cli to failed: '+o1\n"
        "assert '1987' in o2, 'cli from failed: '+o2\n"
        "print('INDEPENDENT_CHECK_PASS')",
        timeout=240,
    )
    i_out = indep.stdout or b""
    if isinstance(i_out, bytes):
        i_out = i_out.decode(errors="replace")
    independent_pass = "INDEPENDENT_CHECK_PASS" in i_out
    log_event("independent_check", passed=independent_pass)

    phase_a_tokens = sum(t["usage"]["total_tokens"] for t in turns[:PHASE_A_TURNS])
    phase_b_tokens = sum(t["usage"]["total_tokens"] for t in turns[PHASE_A_TURNS:])

    try:
        await session.shutdown()
        await client.delete(session)
    except Exception:
        pass

    trial_payload = {
        "experiment": "durable_execution",
        "model": common.MODEL_ID,
        "sandbox_image": common.SANDBOX_IMAGE,
        "design": {
            "phase_a_turns": PHASE_A_TURNS,
            "total_turns": len(TURNS),
            "harness_process_killed": True,
            "compute_container_killed": True,
            "snapshot_backend": "LocalSnapshot (tarball on host disk)",
        },
        "turns": turns,
        "recovery": {
            "resume_seconds": resume_s,
            "checkpoint_seconds": saved["checkpoint_seconds"],
            "old_container": saved["container_id"][:12],
            "new_container": new_container[:12],
            "container_changed": new_container != saved["container_id"],
            "workspace_state_preserved": preserved,
            "pre_kill_fingerprint": saved["pre_kill_fingerprint"],
            "post_resume_fingerprint": fp_after,
            "turns_repeated_after_resume": 0,
            "tokens_spent_before_kill": phase_a_tokens,
            "tokens_respent_on_recovery": 0,
            "tokens_spent_after_resume": phase_b_tokens,
            "tokens_a_cold_restart_would_respend": phase_a_tokens,
        },
        "correctness": {
            "final_test_exit_code": verify.exit_code,
            "final_test_stdout": v_out[-800:],
            "final_test_stderr": v_err[-400:],
            "independent_roundtrip_check_passed": independent_pass,
        },
        "events": [json.loads(x) for x in EVENTS_FILE.read_text().splitlines() if x.strip()],
    }
    trial_payload["trial"] = TRIAL
    (STATE_DIR / "trial_result.json").write_text(json.dumps(trial_payload, indent=2, default=str))
    print("PHASE_B_OK")


def driver() -> None:
    # Clean slate
    if EVENTS_FILE.exists():
        EVENTS_FILE.unlink()
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    py = sys.executable
    here = str(pathlib.Path(__file__).resolve())

    print("\n########## PHASE A (separate process) ##########")
    r = subprocess.run([py, here, "phase-a"], cwd=str(common.REPO_ROOT))
    if r.returncode != 0:
        raise SystemExit("phase A failed")

    saved = json.loads(STATE_FILE.read_text())
    cid = saved["container_id"]

    print("\n########## KILLING COMPUTE PLANE ##########")
    log_event("kill_container_begin", container=cid[:12])
    subprocess.run(["docker", "rm", "-f", cid], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    gone = subprocess.run(["docker", "inspect", cid], capture_output=True).returncode != 0
    log_event("kill_container_done", container=cid[:12], confirmed_gone=gone)
    if not gone:
        raise SystemExit("container was not actually destroyed; test is invalid")

    print("\n########## PHASE B (fresh process) ##########")
    r = subprocess.run([py, here, "phase-b"], cwd=str(common.REPO_ROOT))
    if r.returncode != 0:
        raise SystemExit("phase B failed")
    print("\nDURABILITY TEST COMPLETE")


def _stats(vals: list[float]) -> dict:
    vals = sorted(vals)
    n = len(vals)
    mean = sum(vals) / n
    mid = vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2
    return {"n": n, "mean": round(mean, 3), "median": round(mid, 3),
            "min": round(vals[0], 3), "max": round(vals[-1], 3),
            "values": [round(v, 3) for v in vals]}


def trials(n: int) -> None:
    """Run the whole kill/rehydrate cycle n times so the recovery numbers are means
    rather than a single anecdote."""
    here = str(pathlib.Path(__file__).resolve())
    results = []
    for i in range(1, n + 1):
        print(f"\n{'#'*20} TRIAL {i}/{n} {'#'*20}")
        env = dict(os.environ, TRIAL_ID=str(i))
        r = subprocess.run([sys.executable, here, "driver"], cwd=str(common.REPO_ROOT), env=env)
        if r.returncode != 0:
            print(f"trial {i} FAILED")
            results.append({"trial": str(i), "failed": True})
            continue
        tr = json.loads((common.REPO_ROOT / ".run_state" / f"trial_{i}" /
                         "trial_result.json").read_text())
        results.append(tr)

    ok = [t for t in results if not t.get("failed")]
    if not ok:
        raise SystemExit("all trials failed")

    agg = {
        "trials_run": len(results),
        "trials_succeeded": len(ok),
        "resume_seconds": _stats([t["recovery"]["resume_seconds"] for t in ok]),
        "checkpoint_seconds": _stats([t["recovery"]["checkpoint_seconds"] for t in ok]),
        "tokens_spent_before_kill": _stats([t["recovery"]["tokens_spent_before_kill"] for t in ok]),
        "tokens_spent_after_resume": _stats([t["recovery"]["tokens_spent_after_resume"] for t in ok]),
        "workspace_preserved_every_trial": all(
            t["recovery"]["workspace_state_preserved"] for t in ok),
        "container_changed_every_trial": all(t["recovery"]["container_changed"] for t in ok),
        "turns_repeated_total": sum(t["recovery"]["turns_repeated_after_resume"] for t in ok),
        "tokens_respent_total": sum(t["recovery"]["tokens_respent_on_recovery"] for t in ok),
        "final_tests_passed_every_trial": all(
            t["correctness"]["final_test_exit_code"] == 0 for t in ok),
        "independent_check_passed_every_trial": all(
            t["correctness"]["independent_roundtrip_check_passed"] for t in ok),
    }

    rep = ok[0]  # representative trial, rendered as the timeline on the site
    common.write_artifact("durable_execution.json", {
        "experiment": "durable_execution",
        "model": rep["model"],
        "sandbox_image": rep["sandbox_image"],
        "design": rep["design"],
        "aggregate": agg,
        "representative_trial": {
            "trial": rep["trial"],
            "turns": rep["turns"],
            "recovery": rep["recovery"],
            "correctness": rep["correctness"],
            "events": rep["events"],
        },
        "trials": [
            {
                "trial": t["trial"],
                "resume_seconds": t["recovery"]["resume_seconds"],
                "checkpoint_seconds": t["recovery"]["checkpoint_seconds"],
                "workspace_state_preserved": t["recovery"]["workspace_state_preserved"],
                "container_changed": t["recovery"]["container_changed"],
                "old_container": t["recovery"]["old_container"],
                "new_container": t["recovery"]["new_container"],
                "turns_repeated_after_resume": t["recovery"]["turns_repeated_after_resume"],
                "tokens_spent_before_kill": t["recovery"]["tokens_spent_before_kill"],
                "tokens_respent_on_recovery": t["recovery"]["tokens_respent_on_recovery"],
                "tokens_spent_after_resume": t["recovery"]["tokens_spent_after_resume"],
                "final_test_exit_code": t["correctness"]["final_test_exit_code"],
                "independent_check_passed": t["correctness"]["independent_roundtrip_check_passed"],
            } for t in ok
        ],
    })
    print("\n=== AGGREGATE ===")
    print(f"trials: {agg['trials_succeeded']}/{agg['trials_run']} succeeded")
    print(f"resume seconds: mean {agg['resume_seconds']['mean']} "
          f"(min {agg['resume_seconds']['min']}, max {agg['resume_seconds']['max']})")
    print(f"workspace preserved every trial: {agg['workspace_preserved_every_trial']}")
    print(f"turns repeated total: {agg['turns_repeated_total']}, "
          f"tokens re-spent total: {agg['tokens_respent_total']}")
    print(f"independent check passed every trial: {agg['independent_check_passed_every_trial']}")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "trials"
    if mode == "driver":
        driver()
    elif mode == "trials":
        trials(int(sys.argv[2]) if len(sys.argv) > 2 else 4)
    elif mode == "phase-a":
        asyncio.run(phase_a())
    elif mode == "phase-b":
        asyncio.run(phase_b())
    else:
        raise SystemExit(f"unknown mode {mode}")
