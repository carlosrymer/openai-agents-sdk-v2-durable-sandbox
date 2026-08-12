"""Experiment 3 — apply_patch, the one capability an OpenAI key unlocks.

`apply_patch` is a `CustomTool`: a model-native tool with a grammar, not an ordinary
JSON-schema function tool. That makes it unreachable for non-OpenAI models, which is why
my earlier runs could not test it and fell back to shell heredocs.

This is an A/B on one variable. Same model, same task, same sandbox:

  * filesystem : SandboxAgent with Filesystem() -> the model gets `apply_patch`
  * shell_only : SandboxAgent with Shell() only -> the model must use heredocs

What I want to know is whether apply_patch actually gets used, whether it works, and
what it costs relative to doing the same edits through a shell.
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
from agents.sandbox.capabilities import Filesystem, Shell  # noqa: E402

import common  # noqa: E402

WORKSPACE = "/workspace"

# Deliberately edit-heavy: a create, then two modifications of an existing file, which is
# the shape apply_patch exists for.
TURNS = [
    "Create /workspace/stats.py with a function mean(xs) returning the arithmetic mean of "
    "a list of numbers, raising ValueError on an empty list. Also create "
    "/workspace/test_stats.py asserting mean([1,2,3])==2 and that mean([]) raises "
    "ValueError. Run: python3 /workspace/test_stats.py",
    "Add median(xs) to /workspace/stats.py (handling both odd and even length lists) and "
    "extend /workspace/test_stats.py to cover median([1,3,2])==2 and median([1,2,3,4])==2.5. "
    "Run the tests.",
    "Add stdev(xs) (population standard deviation) to /workspace/stats.py and extend the "
    "tests to assert stdev([2,4,4,4,5,5,7,9])==2.0. Run the tests and report the output.",
]


def tool_calls_from(res) -> list[str]:
    """Extract the name of every tool the model actually invoked."""
    names: list[str] = []
    for item in res.new_items:
        if type(item).__name__ != "ToolCallItem":
            continue
        raw = getattr(item, "raw_item", None)
        name = getattr(raw, "name", None)
        if name is None:
            t = getattr(raw, "type", "")
            # custom tool calls surface their identity on `.name`; fall back to type
            name = t or type(raw).__name__
        names.append(str(name))
    return names


DIRECTED_INSTRUCTIONS = (
    "You are building a small Python library inside a sandbox at /workspace. "
    "You MUST use the `apply_patch` tool for ALL file creation and editing. Do not write "
    "or modify files with shell heredocs, cat, sed, or python -c. Use the shell ONLY to run "
    "the tests. Work incrementally. After each step actually RUN the tests and report the "
    "real output. Be terse."
)

BASE_INSTRUCTIONS = (
    "You are building a small Python library inside a sandbox at /workspace. "
    "Work incrementally and do not rewrite files you have already written unless "
    "asked. After each step actually RUN the tests and report the real output. "
    "Be terse."
)


async def run_variant(variant: str, model_id: str) -> dict:
    client = common.docker_client()
    options = common.docker_options()
    manifest = common.manifest_for(WORKSPACE)

    caps = [Shell()] if variant == "shell_only" else [Filesystem(), Shell()]
    instructions = DIRECTED_INSTRUCTIONS if variant == "filesystem_directed" else BASE_INSTRUCTIONS

    agent = SandboxAgent(
        name=f"builder-{variant}",
        instructions=instructions,
        model=model_id,
        capabilities=caps,
        default_manifest=manifest,
    )

    session = await client.create(manifest=manifest, options=options)
    await session.start()

    turns: list[dict] = []
    all_tools: list[str] = []
    input_list: list = []
    started = time.time()
    error = None
    try:
        for i, prompt in enumerate(TURNS):
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
            input_list = res.to_input_list()
            calls = tool_calls_from(res)
            all_tools += calls
            u = res.context_wrapper.usage
            turns.append({
                "turn": i,
                "wall_clock_s": round(time.time() - t0, 2),
                "tool_calls": calls,
                "total_tokens": getattr(u, "total_tokens", 0),
                "final_output": (res.final_output or "")[:400],
            })
            print(f"  [{variant}] turn {i}: {calls} ({turns[-1]['total_tokens']} tok)")

        # Correctness verified by the harness, not trusted from the model.
        verify = await session.exec(
            "python3", "-c",
            "import sys; sys.path.insert(0,'/workspace'); import stats; "
            "assert stats.mean([1,2,3])==2; "
            "assert stats.median([1,3,2])==2; assert stats.median([1,2,3,4])==2.5; "
            "assert abs(stats.stdev([2,4,4,4,5,5,7,9])-2.0)<1e-9; "
            "print('VERIFY_PASS')",
            timeout=120,
        )
        vout = verify.stdout or b""
        if isinstance(vout, bytes):
            vout = vout.decode(errors="replace")
        verified = "VERIFY_PASS" in vout
    except Exception as e:
        error = str(e)[:500]
        verified = False
        print(f"  [{variant}] ERROR: {error}")
    finally:
        try:
            await session.shutdown()
            await client.delete(session)
        except Exception:
            pass

    counts: dict[str, int] = {}
    for n in all_tools:
        counts[n] = counts.get(n, 0) + 1

    return {
        "variant": variant,
        "capabilities": ["Shell"] if variant == "shell_only" else ["Filesystem", "Shell"],
        "instructions_directed_to_use_apply_patch": variant == "filesystem_directed",
        "model": model_id,
        "error": error,
        "turns": turns,
        "tool_call_counts": counts,
        "apply_patch_calls": counts.get("apply_patch", 0),
        "total_tokens": sum(t["total_tokens"] for t in turns),
        "wall_clock_s": round(time.time() - started, 2),
        "verified_correct": verified,
    }


async def main() -> None:
    model_id = common.OPENAI_MODEL
    print(f"[exp3] model={model_id}")

    results = {}
    for variant in ("filesystem", "shell_only", "filesystem_directed"):
        print(f"[run] {variant}")
        results[variant] = await run_variant(variant, model_id)

    fs, sh, fd = (results["filesystem"], results["shell_only"],
                  results["filesystem_directed"])
    common.write_artifact("apply_patch.json", {
        "experiment": "apply_patch",
        "model": model_id,
        "sandbox_image": common.SANDBOX_IMAGE,
        "why_this_needed_an_openai_key": (
            "apply_patch is a CustomTool - a model-native tool with a grammar rather than a "
            "JSON-schema function tool - so it is only available on OpenAI models. Runs driven "
            "by Gemini or Kimi cannot use it and fall back to shell heredocs."
        ),
        "variants": results,
        "comparison": {
            "apply_patch_offered_but_unused_when_free_choice": (
                fs["apply_patch_calls"] == 0
            ),
            "apply_patch_used_when_directed": fd["apply_patch_calls"] > 0,
            "apply_patch_calls_free_choice": fs["apply_patch_calls"],
            "apply_patch_calls_directed": fd["apply_patch_calls"],
            "directed_tokens": fd["total_tokens"],
            "directed_verified": fd["verified_correct"],
            "apply_patch_used": fs["apply_patch_calls"] > 0,
            "apply_patch_call_count": fs["apply_patch_calls"],
            "filesystem_tokens": fs["total_tokens"],
            "shell_only_tokens": sh["total_tokens"],
            "token_delta": fs["total_tokens"] - sh["total_tokens"],
            "filesystem_seconds": fs["wall_clock_s"],
            "shell_only_seconds": sh["wall_clock_s"],
            "both_verified_correct": fs["verified_correct"] and sh["verified_correct"],
            "filesystem_verified": fs["verified_correct"],
            "shell_only_verified": sh["verified_correct"],
        },
    })

    print("\n=== RESULT ===")
    print(f"filesystem : apply_patch x{fs['apply_patch_calls']}, tools={fs['tool_call_counts']}, "
          f"{fs['total_tokens']} tok, verified={fs['verified_correct']}")
    print(f"shell_only : tools={sh['tool_call_counts']}, "
          f"{sh['total_tokens']} tok, verified={sh['verified_correct']}")
    print(f"directed   : apply_patch x{fd['apply_patch_calls']}, tools={fd['tool_call_counts']}, "
          f"{fd['total_tokens']} tok, verified={fd['verified_correct']}")


if __name__ == "__main__":
    asyncio.run(main())
