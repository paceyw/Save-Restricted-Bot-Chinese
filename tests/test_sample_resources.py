import importlib.util
import json
import sys
from pathlib import Path

# Load the sampler script as a module without a package (scripts/ has no __init__).
_SPEC = importlib.util.spec_from_file_location(
    "sample_resources", Path(__file__).resolve().parent.parent / "scripts" / "sample_resources.py"
)
sample_resources = importlib.util.module_from_spec(_SPEC)
sys.modules["sample_resources"] = sample_resources
_SPEC.loader.exec_module(sample_resources)


def test_parse_memory_events():
    text = "low 0\nhigh 5\nmax 0\noom 0\noom_kill 0\n"
    parsed = sample_resources.parse_memory_events(text)
    assert parsed == {"low": 0, "high": 5, "max": 0, "oom": 0, "oom_kill": 0}


def test_parse_memory_events_garbage_returns_none():
    assert sample_resources.parse_memory_events("") is None
    assert sample_resources.parse_memory_events(None) is None


def test_parse_io_stat_sums_devices():
    text = "8:0 rbytes=1024 wbytes=512 ios=4\n8:16 rbytes=2048 wbytes=0 ios=1\n"
    assert sample_resources.parse_io_stat(text) == {
        "rbytes": 3072, "wbytes": 512, "ios": 5,
    }


def test_read_cgroup_snapshot_marks_missing_files_never_zero(tmp_path):
    cgroup = tmp_path / "docker-x.scope"
    cgroup.mkdir()
    (cgroup / "memory.current").write_text("123456\n")
    (cgroup / "memory.events").write_text("oom 0\noom_kill 0\n")

    snapshot, missing = sample_resources.read_cgroup_snapshot(cgroup)

    assert snapshot["memory.current"]["bytes"] == 123456
    assert snapshot["memory.events"]["events"] == {"oom": 0, "oom_kill": 0}
    assert set(missing) == {"memory.peak", "memory.swap.current", "io.stat"}
    # missing counters must not appear as zero-filled entries
    assert "memory.peak" not in snapshot


def test_percentile_matches_known_values():
    assert sample_resources.percentile([1.0], 95) == 1.0
    values = [float(i) for i in range(1, 101)]  # 1..100
    assert sample_resources.percentile(values, 50) == 50.5
    assert sample_resources.percentile(values, 95) == 95.05
    assert sample_resources.percentile(values, 100) == 100.0


def _sample(annotation, mem_bytes, swap_bytes, oom=0, oom_kill=0, incomplete=()):
    return {
        "annotation": annotation,
        "incomplete": list(incomplete),
        "containers": [{
            "name": "bot",
            "memory.current": {"bytes": mem_bytes},
            "memory.swap.current": {"bytes": swap_bytes},
            "memory.events": {"events": {"oom": oom, "oom_kill": oom_kill}},
            "io.stat": {"totals": {"rbytes": 10, "wbytes": 20}},
        }],
    }


def test_summarize_groups_by_annotation_and_flags_missing():
    samples = [
        _sample("idle", 100, 0, incomplete=["bot:io.stat"]),
        _sample("idle", 300, 5),
        _sample("hls-burn", 1000, 50, oom=1, oom_kill=1),
    ]
    report = sample_resources.summarize(samples)

    idle = report["annotations"]["idle"]
    assert idle["sample_count"] == 2
    assert idle["memory_current_bytes"]["max"] == 300
    assert idle["memory_current_bytes"]["p50"] == 200
    assert idle["metrics_with_missing_data"] == ["bot:io.stat"]

    burn = report["annotations"]["hls-burn"]
    assert burn["memory_events_oom_kill_total"] == 1
    assert burn["memory_current_bytes"]["max"] == 1000


def test_summarize_empty_samples_is_valid_empty_report():
    report = sample_resources.summarize([])
    assert report["annotations"] == {}
    assert "generated_at" in report
