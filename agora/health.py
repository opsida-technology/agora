"""
agora.health — Pre-flight health check for CLI backends.

    python -m agora.health
    python -m agora.health configs/manufacturing_models.yaml
"""
import json
import shutil
import subprocess
import sys

import yaml


def check_cli(name: str, test_cmd: list[str], timeout: int = 30) -> dict:
    """Check if a CLI tool is installed, responsive, and authenticated."""
    result = {"name": name, "installed": False, "responds": False,
              "auth": False, "detail": ""}

    path = shutil.which(name)
    if not path:
        result["detail"] = "not found in PATH"
        return result
    result["installed"] = True
    result["detail"] = path

    try:
        r = subprocess.run(
            test_cmd, capture_output=True, text=True, timeout=timeout)
        if r.returncode == 0 and r.stdout.strip():
            result["responds"] = True
            result["auth"] = True
            result["detail"] = f"OK — {r.stdout.strip()[:80]}"
        elif r.returncode != 0:
            stderr = r.stderr.strip()[:120]
            if "auth" in stderr.lower() or "login" in stderr.lower() or "key" in stderr.lower():
                result["responds"] = True
                result["detail"] = f"auth issue — {stderr}"
            else:
                result["detail"] = f"exit {r.returncode} — {stderr}"
    except subprocess.TimeoutExpired:
        result["detail"] = f"timeout after {timeout}s"
    except Exception as e:
        result["detail"] = str(e)

    return result


CLI_TESTS = {
    "claude": ["claude", "-p", "Reply with exactly: HEALTH_OK"],
    "codex":  ["codex", "exec", "Reply with exactly: HEALTH_OK"],
    "gemini": ["gemini", "--prompt", "Reply with exactly: HEALTH_OK"],
}


def run_health(cfg_path: str | None = None, as_json: bool = False) -> list[dict]:
    """Run health checks. If cfg_path given, only check CLIs used in config."""
    if cfg_path:
        with open(cfg_path) as f:
            cfg = yaml.safe_load(f)
        needed = set()
        for a in cfg["agents"]:
            cmd = a.get("command", [])
            if cmd:
                needed.add(cmd[0])
    else:
        needed = set(CLI_TESTS.keys())

    results = []
    for name in sorted(needed):
        test_cmd = CLI_TESTS.get(name, [name, "--version"])
        results.append(check_cli(name, test_cmd))

    if as_json:
        print(json.dumps(results, indent=2))
    else:
        for r in results:
            icon = "✓" if r["auth"] else ("⚠" if r["installed"] else "✗")
            status = "ready" if r["auth"] else ("installed but not responding" if r["installed"] else "missing")
            print(f"  {icon} {r['name']:10s} {status:30s} {r['detail']}")

    return results


if __name__ == "__main__":
    cfg = sys.argv[1] if len(sys.argv) > 1 else None
    results = run_health(cfg)
    all_ok = all(r["auth"] for r in results)
    sys.exit(0 if all_ok else 1)
