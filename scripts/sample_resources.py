#!/usr/bin/env python3
"""Phase 0 resource baseline sampler (REFACTOR_AND_OPTIMIZATION_PLAN.md §3).

Samples, on a fixed interval, for each named Docker container:
  - cgroup v2 counters: memory.current / memory.peak / memory.events /
    memory.swap.current / io.stat  (raw text preserved verbatim)
  - process tree: VmRSS / VmSwap / Threads for the container's main python
    process and any ffmpeg children (matched by /proc comm == "ffmpeg")
and on the host:
  - /proc/vmstat pswpin / pswpot cumulative counters (per-sample deltas)
  - /proc/meminfo MemTotal / SwapTotal (run header)

Every sample carries a UTC ISO timestamp, the active annotation (task-type
label given with --annotation) and an `incomplete` list naming every counter
that could not be read. Missing data is NEVER zero-filled (plan §3.2.1);
samples with missing counters are flagged, and the summary report marks any
task type whose samples were incomplete.

Output directory layout:
  <out>/run_meta.json     header: image digests, container ids, host meminfo,
                          interval, annotation, start time
  <out>/samples.jsonl     one raw sample per line
  <out>/summary.json      per-annotation P50/P95/max/sample-count report +
                          oom event totals + io totals

Exit codes: 0 = clean stop, 2 = could not resolve containers/cgroups at start.
No third-party dependencies; container resolution shells out to `docker`
inspect only.
"""

import argparse
import json
import os
import signal
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

CGROUP_FILES = (
    "memory.current",
    "memory.peak",
    "memory.events",
    "memory.swap.current",
    "io.stat",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def read_text(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return None


def parse_memory_events(text: str | None) -> dict[str, int] | None:
    """`oom 0\\noom_kill 0\\n...` -> {event: count}; None when unreadable."""
    if text is None:
        return None
    events = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("-").isdigit():
            events[parts[0]] = int(parts[1])
    return events or None


def parse_io_stat(text: str | None) -> dict[str, int] | None:
    """cgroup v2 io.stat lines like `8:0 rbytes=1024 wbytes=512 ...` ->
    summed totals {rbytes, wbytes, ios} across devices; None when unreadable."""
    if text is None:
        return None
    totals: dict[str, int] = {}
    for line in text.splitlines():
        for field in line.split()[1:]:
            if "=" in field:
                key, _, value = field.partition("=")
                if value.lstrip("-").isdigit():
                    totals[key] = totals.get(key, 0) + int(value)
    return totals or None


def read_cgroup_snapshot(cgroup_dir: Path) -> tuple[dict, list[str]]:
    """Raw + parsed counters for one container cgroup.

    Returns (snapshot, missing_file_names). Missing files are reported, never
    zero-filled.
    """
    snapshot: dict = {}
    missing: list[str] = []
    for name in CGROUP_FILES:
        raw = read_text(cgroup_dir / name)
        if raw is None:
            missing.append(name)
            continue
        entry = {"raw": raw}
        if name == "memory.events":
            entry["events"] = parse_memory_events(raw)
        elif name == "io.stat":
            entry["totals"] = parse_io_stat(raw)
        elif name in ("memory.current", "memory.peak", "memory.swap.current"):
            entry["bytes"] = int(raw) if raw.lstrip("-").isdigit() else None
            if entry["bytes"] is None:
                missing.append(name)
        snapshot[name] = entry
    return snapshot, missing


def read_proc_status(pid: int) -> dict | None:
    """VmRSS/VmSwap(kB)/Threads from /proc/<pid>/status, or None if gone."""
    values: dict = {"pid": pid}
    try:
        with open(f"/proc/{pid}/status") as fh:
            for line in fh:
                if line.startswith(("VmRSS:", "VmSwap:", "Threads:")):
                    key, _, rest = line.partition(":")
                    digits = rest.strip().split()[0]
                    if not digits.isdigit():
                        return None
                    values[key.rstrip(":")] = int(digits)
    except OSError:
        return None
    if "VmRSS" not in values:
        return None
    return values


def find_comm_pids(comm: str) -> list[int]:
    """PIDs whose /proc/<pid>/comm == comm (used for ffmpeg children)."""
    pids = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        name = read_text(Path("/proc") / entry / "comm")
        if name == comm:
            pids.append(int(entry))
    return sorted(pids)


def read_vmstat_swap() -> tuple[int, int] | None:
    text = read_text(Path("/proc/vmstat"))
    if text is None:
        return None
    pswpin = pswpout = None
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].isdigit():
            if parts[0] == "pswpin":
                pswpin = int(parts[1])
            elif parts[0] == "pswpout":
                pswpout = int(parts[1])
    if pswpin is None or pswpout is None:
        return None
    return pswpin, pswpout


def resolve_container(name: str) -> dict:
    """docker inspect one container -> {id, image, cgroup_dir, main_pid}."""
    def inspect_field(fmt: str) -> str:
        out = subprocess.run(
            ["docker", "inspect", "--format", fmt, name],
            capture_output=True, text=True, timeout=15,
        )
        if out.returncode != 0:
            raise RuntimeError(f"docker inspect failed for {name}: {out.stderr.strip()}")
        return out.stdout.strip()

    container_id = inspect_field("{{.Id}}")
    image = inspect_field("{{.Image}}")
    state = json.loads(inspect_field("{{json .State}}"))
    main_pid = int(state.get("Pid") or 0)
    if main_pid == 0:
        raise RuntimeError(f"container {name} is not running (no Pid)")

    cgroup_root = Path("/sys/fs/cgroup")
    candidates = [
        cgroup_root / "system.slice" / f"docker-{container_id}.scope",
        cgroup_root / f"docker-{container_id}.scope",
        cgroup_root / "system.slice" / f"docker-{container_id[:12]}.scope",
    ]
    for cgroup_dir in candidates:
        if (cgroup_dir / "memory.current").exists():
            return {"name": name, "id": container_id, "image": image,
                    "cgroup_dir": str(cgroup_dir), "main_pid": main_pid}
    raise RuntimeError(
        f"cgroup v2 dir not found for {name} (tried: "
        + ", ".join(str(c) for c in candidates) + ")"
    )


def percentile(values: list[float], p: float) -> float:
    """Linear-interpolation percentile (same definition as numpy default)."""
    if not values:
        raise ValueError("percentile of empty list")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (p / 100) * (len(ordered) - 1)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    frac = rank - low
    return ordered[low] * (1 - frac) + ordered[high] * frac


def summarize(samples: list[dict]) -> dict:
    """Group raw samples by annotation; P50/P95/max per metric; oom totals.

    Metrics with ANY incomplete samples are flagged in
    metrics_with_missing_data so the baseline report cannot over-claim.
    """
    groups: dict[str, list[dict]] = {}
    for sample in samples:
        groups.setdefault(sample.get("annotation") or "unannotated", []).append(sample)

    report: dict = {"generated_at": utc_now(), "annotations": {}}
    for annotation, group in sorted(groups.items()):
        entry: dict = {"sample_count": len(group)}
        flagged: set[str] = set()
        mem_series: list[float] = []
        swap_series: list[float] = []
        oom_total = oom_kill_total = 0
        io_read = io_written = 0
        for sample in group:
            flagged.update(sample.get("incomplete", []))
            for container in sample.get("containers", []):
                current = (container.get("memory.current") or {}).get("bytes")
                if current is not None:
                    mem_series.append(current)
                swap = (container.get("memory.swap.current") or {}).get("bytes")
                if swap is not None:
                    swap_series.append(swap)
                events = (container.get("memory.events") or {}).get("events") or {}
                oom_total += events.get("oom", 0)
                oom_kill_total += events.get("oom_kill", 0)
                io = (container.get("io.stat") or {}).get("totals") or {}
                io_read += io.get("rbytes", 0)
                io_written += io.get("wbytes", 0)

        for label, series in (("memory_current_bytes", mem_series),
                              ("memory_swap_current_bytes", swap_series)):
            if series:
                entry[label] = {
                    "p50": round(percentile(series, 50)),
                    "p95": round(percentile(series, 95)),
                    "max": max(series),
                    "count": len(series),
                }
        entry["memory_events_oom_total"] = oom_total
        entry["memory_events_oom_kill_total"] = oom_kill_total
        entry["io_rbytes_total"] = io_read
        entry["io_wbytes_total"] = io_written
        entry["metrics_with_missing_data"] = sorted(flagged)
        report["annotations"][annotation] = entry
    return report


def take_sample(containers: list[dict], annotation: str,
                prev_swap: tuple[int, int] | None) -> tuple[dict, tuple[int, int] | None]:
    sample: dict = {"ts": utc_now(), "annotation": annotation, "containers": [],
                    "incomplete": []}
    for container in containers:
        cgroup_dir = Path(container["cgroup_dir"])
        snapshot, missing = read_cgroup_snapshot(cgroup_dir)
        for name in missing:
            sample["incomplete"].append(f"{container['name']}:{name}")
        processes = []
        main_status = read_proc_status(container["main_pid"])
        if main_status is None:
            sample["incomplete"].append(f"{container['name']}:main_proc_status")
        else:
            processes.append(main_status)
        for pid in find_comm_pids("ffmpeg"):
            status = read_proc_status(pid)
            if status is not None:
                processes.append({"ffmpeg": True, **status})
        sample["containers"].append({
            "name": container["name"], "cgroup": str(cgroup_dir),
            "main_pid": container["main_pid"],
            **snapshot, "processes": processes,
        })
    swap = read_vmstat_swap()
    if swap is None:
        sample["incomplete"].append("host:vmstat")
        sample["host_pswpin_delta"] = None
        sample["host_pswpout_delta"] = None
    else:
        sample["host_pswpin_delta"] = None if prev_swap is None else swap[0] - prev_swap[0]
        sample["host_pswpout_delta"] = None if prev_swap is None else swap[1] - prev_swap[1]
    return sample, swap


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--container", action="append", required=True,
                        help="docker container name (repeatable)")
    parser.add_argument("--interval", type=float, default=5.0,
                        help="sample interval seconds (default 5, plan §3.2.1)")
    parser.add_argument("--annotation", default="idle",
                        help="task-type label stored on every sample (default idle)")
    parser.add_argument("--out", required=True, help="output directory")
    parser.add_argument("--duration", type=float, default=0,
                        help="stop after N seconds (0 = run until SIGINT/SIGTERM)")
    args = parser.parse_args()

    try:
        containers = [resolve_container(name) for name in args.container]
    except (RuntimeError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
        print(f"startup failed: {exc}", file=sys.stderr)
        return 2

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "started_at": utc_now(),
        "interval_s": args.interval,
        "annotation": args.annotation,
        "host": {
            "MemTotal_kB": None, "SwapTotal_kB": None,
        },
        "containers": containers,
    }
    meminfo = read_text(Path("/proc/meminfo")) or ""
    for line in meminfo.splitlines():
        parts = line.split()
        if parts and parts[0] == "MemTotal:" and len(parts) > 1:
            meta["host"]["MemTotal_kB"] = int(parts[1])
        elif parts and parts[0] == "SwapTotal:" and len(parts) > 1:
            meta["host"]["SwapTotal_kB"] = int(parts[1])
    (out_dir / "run_meta.json").write_text(json.dumps(meta, indent=2) + "\n")

    samples_path = out_dir / "samples.jsonl"
    stop = {"requested": False}

    def request_stop(_sig, _frame):
        stop["requested"] = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    samples: list[dict] = []
    prev_swap: tuple[int, int] | None = None
    deadline = time.monotonic() + args.duration if args.duration > 0 else None
    print(f"sampling {len(containers)} container(s) every {args.interval}s "
          f"annotation={args.annotation!r} -> {samples_path}")
    while not stop["requested"] and (deadline is None or time.monotonic() < deadline):
        sample, prev_swap = take_sample(containers, args.annotation, prev_swap)
        samples.append(sample)
        with samples_path.open("a") as fh:
            fh.write(json.dumps(sample) + "\n")
        if sample["incomplete"]:
            print(f"WARNING incomplete sample, missing: {sample['incomplete']}",
                  file=sys.stderr)
        if deadline is None:
            time.sleep(args.interval)
        else:
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(args.interval, remaining))

    summary = summarize(samples)
    summary["stopped_at"] = utc_now()
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(f"wrote {len(samples)} samples and summary to {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
