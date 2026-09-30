#!/usr/bin/env python3
"""Measure llama-server settings for leo-llm and pick the fastest that fits (§13).

Runs on the host (stdlib only). Restarts leo-llm with overridden LEO_* env vars
through compose.debug.yaml (loopback port 18080), times fixed prompts, and
writes docs/TUNING.md. Proposes .env changes; writes them only with --apply
after confirmation.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"
URL = "http://127.0.0.1:18080"
COMPOSE = [
    "docker",
    "compose",
    "--project-directory",
    str(ROOT),
    "-f",
    str(ROOT / "compose.yaml"),
    "-f",
    str(ROOT / "compose.debug.yaml"),
]
MIN_FREE_VRAM_MIB = 400
GEN_TOKENS = 300

# Deterministic filler text, ~1 token per 4-5 chars. Sized by the server tokenizer.
PARAGRAPH = (
    "Let G = (V, E) be a directed graph with non-negative edge weights w. Dijkstra's "
    "algorithm maintains a set S of vertices whose final shortest-path distances from "
    "the source s are known, and a priority queue keyed by tentative distance d(v). "
    "Each iteration extracts the vertex u with minimum d(u), adds it to S, and relaxes "
    "every edge (u, v): if d(u) + w(u, v) < d(v) then d(v) is decreased. The invariant "
    "is that for every u in S, d(u) equals the true distance delta(s, u). With a binary "
    "heap the running time is O((|V| + |E|) log |V|); with a Fibonacci heap it is "
    "O(|E| + |V| log |V|). The master theorem solves T(n) = a T(n/b) + f(n). "
)

MATH_CHECKS = [  # (prompt, regex that must match the final answer)
    ("What is 17 * 23? Answer with the number only.", r"\b391\b"),
    ("What is the sum of the integers from 1 to 100? Number only.", r"\b5050\b"),
    ("How many edges does the complete graph K_7 have? Number only.", r"\b21\b"),
    ("What is 2^20? Number only.", r"\b1048576\b"),
    ("Solve T(n) = 2T(n/2) + n with the master theorem. Give the Theta bound only.", r"n\s*\\?log|n\s*lg|n log n"),
    ("What is the derivative of x^3 at x = 2? Number only.", r"\b12\b"),
    ("How many binary strings of length 8 have exactly 3 ones? Number only.", r"\b56\b"),
    ("What is gcd(462, 1071)? Number only.", r"\b21\b"),
    ("What is 10! / 8!? Number only.", r"\b90\b"),
    ("What is the minimum number of comparisons to find the max of 64 numbers? Number only.", r"\b63\b"),
]


def env_get(key: str) -> str:
    for line in ENV_FILE.read_text().splitlines():
        if line.startswith(f"{key}="):
            return re.sub(r"\s+#.*$", "", line.split("=", 1)[1]).strip()
    raise KeyError(key)


def sh(cmd: list[str], env: dict[str, str] | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, env={**os.environ, **(env or {})}, text=True, capture_output=True, check=check)


def free_vram_mib() -> int:
    out = sh(["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"]).stdout
    return int(out.strip().splitlines()[0])


def http(path: str, body: dict | None = None, timeout: float = 600) -> dict:
    headers = {"Authorization": f"Bearer {env_get('LLM_API_KEY')}", "Content-Type": "application/json"}
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(URL + path, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


@dataclass
class Result:
    config: dict[str, str]
    ok: bool
    free_vram: int = 0
    gen_tps: list[float] = field(default_factory=list)
    pp_tps_2k: list[float] = field(default_factory=list)
    pp_tps_8k: list[float] = field(default_factory=list)
    note: str = ""

    @property
    def gen(self) -> float:
        return statistics.median(self.gen_tps) if self.gen_tps else 0.0

    @property
    def pp2k(self) -> float:
        return statistics.median(self.pp_tps_2k) if self.pp_tps_2k else 0.0

    @property
    def pp8k(self) -> float:
        return statistics.median(self.pp_tps_8k) if self.pp_tps_8k else 0.0


def start(overrides: dict[str, str]) -> tuple[bool, str]:
    """Recreate leo-llm with overrides; wait for /health. Returns (ok, note)."""
    sh(COMPOSE + ["up", "-d", "--force-recreate", "--no-deps", "leo-llm"], env=overrides)
    deadline = time.time() + 900
    while time.time() < deadline:
        state = sh(
            ["docker", "inspect", "-f", "{{.State.Status}} {{.State.ExitCode}}", "leo-leo-llm-1"], check=False
        ).stdout.strip()
        if state.startswith(("exited", "restarting", "dead")):
            logs = sh(["docker", "logs", "--tail", "40", "leo-leo-llm-1"], check=False)
            text = logs.stdout + logs.stderr
            oom = "out of memory" in text.lower() or "failed to allocate" in text.lower()
            return False, "OOM" if oom else f"exited: {text.strip().splitlines()[-1][:160] if text.strip() else state}"
        try:
            with urllib.request.urlopen(URL + "/health", timeout=3) as r:
                if r.status == 200:
                    return True, ""
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            pass
        time.sleep(3)
    return False, "timeout waiting for /health"


_prompt_cache: dict[int, str] = {}


def prompt_of(tokens: int) -> str:
    if tokens not in _prompt_cache:
        per = len(http("/tokenize", {"content": PARAGRAPH})["tokens"])
        _prompt_cache[tokens] = PARAGRAPH * max(1, tokens // per)
    return _prompt_cache[tokens]


def run_once(prompt_tokens: int, gen: int) -> tuple[float, float]:
    body = {
        "model": "leo",
        "messages": [
            {"role": "system", "content": "You are a concise algorithms tutor."},
            {"role": "user", "content": prompt_of(prompt_tokens) + "\n\nSummarize the key ideas above in detail."},
        ],
        "max_tokens": gen,
        "ignore_eos": True,
        "cache_prompt": False,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    t = http("/v1/chat/completions", body)["timings"]
    return float(t["predicted_per_second"]), float(t["prompt_per_second"])


def measure(overrides: dict[str, str], runs: int = 3, with_8k: bool = True) -> Result:
    label = " ".join(f"{k.removeprefix('LEO_')}={v}" for k, v in overrides.items())
    print(f"── {label}", flush=True)
    ok, note = start(overrides)
    res = Result(overrides, ok, note=note)
    if not ok:
        print(f"   ✘ {note}", flush=True)
        return res
    try:
        run_once(256, 16)  # warm-up
        res.free_vram = free_vram_mib()
        for _ in range(runs):
            g, p = run_once(2048, GEN_TOKENS)
            res.gen_tps.append(g)
            res.pp_tps_2k.append(p)
        if with_8k:
            _, p8 = run_once(8192, 32)
            res.pp_tps_8k.append(p8)
    except Exception as e:  # noqa: BLE001 — a failed config is data, not a crash
        res.ok, res.note = False, f"request failed: {e}"
        print(f"   ✘ {res.note}", flush=True)
        return res
    print(
        f"   ✔ gen {res.gen:.1f} tok/s · pp2k {res.pp2k:.0f} · pp8k {res.pp8k:.0f} · free VRAM {res.free_vram} MiB",
        flush=True,
    )
    return res


def quality(overrides: dict[str, str]) -> int:
    ok, _ = start(overrides)
    if not ok:
        return -1
    score = 0
    for q, rx in MATH_CHECKS:
        body = {
            "model": "leo",
            "messages": [{"role": "user", "content": q}],
            "max_tokens": 64,
            "temperature": 0,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        ans = http("/v1/chat/completions", body)["choices"][0]["message"]["content"]
        score += bool(re.search(rx, ans, re.IGNORECASE))
    return score


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write the proposed values to .env (asks first)")
    ap.add_argument("--start-moe", type=int, default=26)
    ap.add_argument("--quick", action="store_true", help="1 run per config, skip 8k prompt")
    args = ap.parse_args()
    runs = 1 if args.quick else 3

    base = {k: env_get(k) for k in ("LEO_N_CPU_MOE", "LEO_UBATCH", "LEO_MTP", "LEO_MTP_DRAFT_MAX", "LEO_MODEL_FILE")}
    results: list[Result] = []
    ubatch0 = base["LEO_UBATCH"]

    # 1. Lowest stable LEO_N_CPU_MOE (more experts on GPU = faster).
    best: Result | None = None
    moe = args.start_moe
    while moe >= 0:
        r = measure({"LEO_N_CPU_MOE": str(moe), "LEO_UBATCH": ubatch0, "LEO_MTP": "false"}, runs, not args.quick)
        results.append(r)
        if not r.ok:
            break
        if r.free_vram < MIN_FREE_VRAM_MIB:
            r.note = f"free VRAM < {MIN_FREE_VRAM_MIB} MiB — rejected"
            print(f"   free VRAM below {MIN_FREE_VRAM_MIB} MiB — keeping the previous value", flush=True)
            break
        best = r
        moe -= 2
    if best is None:
        print("No configuration started. Check `docker logs leo-leo-llm-1`.", file=sys.stderr)
        return 1
    best_moe = int(best.config["LEO_N_CPU_MOE"])

    # 2. LEO_UBATCH: best prompt speed costing at most 1 extra expert layer on CPU.
    ub_best = best
    for ub in ("512", "1024", "2048"):
        if ub == ubatch0:
            continue
        for m in (best_moe, best_moe + 1):
            r = measure({"LEO_N_CPU_MOE": str(m), "LEO_UBATCH": ub, "LEO_MTP": "false"}, runs, not args.quick)
            results.append(r)
            if r.ok and r.free_vram >= MIN_FREE_VRAM_MIB:
                if (r.pp8k or r.pp2k) > (ub_best.pp8k or ub_best.pp2k) and r.gen >= 0.97 * best.gen:
                    ub_best = r
                break
    chosen = ub_best

    # 3. MTP on/off, if an MTP GGUF was fetched (data/models/mtp/<same file>).
    mtp_file = f"mtp/{base['LEO_MODEL_FILE']}"
    mtp_note = "not tested (no MTP GGUF in data/models/mtp/)"
    if (ROOT / "data/models" / mtp_file).exists():
        mtp_best = chosen
        for n in ("2", "3"):
            cfg = {**chosen.config, "LEO_MODEL_FILE": mtp_file, "LEO_MTP": "true", "LEO_MTP_DRAFT_MAX": n}
            r = measure(cfg, 3, False)
            results.append(r)
            if r.ok and r.gen >= 1.10 * chosen.gen and r.gen > mtp_best.gen:
                mtp_best = r
        mtp_note = "kept (≥10% gain)" if mtp_best is not chosen else "off (gain < 10%)"
        chosen = mtp_best

    # 4. Optional quant A/B.
    iq3 = "Qwen3.6-35B-A3B-UD-IQ3_XXS.gguf"
    quant_note = "not tested (IQ3_XXS not downloaded)"
    if (ROOT / "data/models" / iq3).exists():
        q_base = quality(chosen.config)
        r = measure({**chosen.config, "LEO_MODEL_FILE": iq3, "LEO_MTP": "false"}, runs, not args.quick)
        results.append(r)
        q_iq3 = quality({**chosen.config, "LEO_MODEL_FILE": iq3, "LEO_MTP": "false"})
        quant_note = f"IQ4_XS {q_base}/10 vs IQ3_XXS {q_iq3}/10 on math checks; IQ3_XXS gen {r.gen:.1f} tok/s. " + (
            "Recommend IQ3_XXS." if q_iq3 >= q_base and r.gen > chosen.gen else "Keep IQ4_XS."
        )

    # Restore the configured settings without the debug port.
    sh(COMPOSE[:6] + ["up", "-d", "--force-recreate", "--no-deps", "leo-llm"])

    proposal = {k: v for k, v in chosen.config.items() if base.get(k) != v}
    write_report(results, chosen, proposal, mtp_note, quant_note)
    print(f"\nChosen: {chosen.config} → gen {chosen.gen:.1f} tok/s (target ≥ 25)")
    if not proposal:
        print("Current .env already matches the best configuration.")
    else:
        print("Proposed .env changes: " + ", ".join(f"{k}={v}" for k, v in proposal.items()))
        if args.apply and input("Write these to .env? [y/N] ").strip().lower() == "y":
            text = ENV_FILE.read_text()
            for k, v in proposal.items():
                text = re.sub(rf"^{k}=[^\s#]*", f"{k}={v}", text, flags=re.MULTILINE)
            ENV_FILE.write_text(text)
            print("Updated .env — run `make up` to apply.")
    return 0


def write_report(results: list[Result], chosen: Result, proposal: dict[str, str], mtp: str, quant: str) -> None:
    gpu = sh(["nvidia-smi", "--query-gpu=name,driver_version,memory.total", "--format=csv,noheader"]).stdout.strip()
    lines = [
        "# Tuning results",
        "",
        f"Generated by `make tune` on {datetime.now().astimezone():%Y-%m-%d %H:%M %Z}. GPU: {gpu}. Image: `{env_get('LLAMACPP_CUDA_TAG')}`.",
        "",
        f"Workload: 2k-token prompt + {GEN_TOKENS}-token answer (median of runs, `ignore_eos`, prompt cache off), plus one 8k-token RAG-sized prompt. Think off.",
        "",
        "| N_CPU_MOE | UBATCH | Model | MTP | Gen tok/s | PP 2k tok/s | PP 8k tok/s | Free VRAM MiB | Note |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        c = r.config
        lines.append(
            f"| {c.get('LEO_N_CPU_MOE', '')} | {c.get('LEO_UBATCH', '')} | {c.get('LEO_MODEL_FILE', 'default')} "
            f"| {c.get('LEO_MTP', 'false')}{'/' + c['LEO_MTP_DRAFT_MAX'] if c.get('LEO_MTP') == 'true' else ''} "
            f"| {r.gen:.1f} | {r.pp2k:.0f} | {r.pp8k:.0f} | {r.free_vram or ''} | {r.note or ('✔' if r.ok else '')} |"
        )
    lines += [
        "",
        (
            f"**Chosen:** `{' '.join(f'{k}={v}' for k, v in chosen.config.items())}` → **{chosen.gen:.1f} tok/s** generation "
            f"({'meets' if chosen.gen >= 25 else 'BELOW'} the ≥ 25 tok/s target)."
        ),
        "",
        f"- MTP: {mtp}",
        f"- Quant A/B: {quant}",
        f"- Proposed `.env` changes: {', '.join(f'`{k}={v}`' for k, v in proposal.items()) or 'none'}",
        "",
    ]
    (ROOT / "docs" / "TUNING.md").write_text("\n".join(lines))
    print("Wrote docs/TUNING.md")


if __name__ == "__main__":
    sys.exit(main())
