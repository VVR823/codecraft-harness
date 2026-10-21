import importlib
import json

from app.runtime.settings import Settings


def test_all_attempts_contribute_to_cost_and_success():
    bench = importlib.import_module("app.bench")
    items = [
        {
            "task_id": "t",
            "sample": 0,
            "attempt": 0,
            "status": "failed",
            "verified": False,
            "known_tokens": 7,
            "unknown_calls": 1,
            "duration_s": 2,
            "tool_calls": 1,
        },
        {
            "task_id": "t",
            "sample": 0,
            "attempt": 1,
            "status": "done",
            "verified": True,
            "known_tokens": 5,
            "unknown_calls": 0,
            "duration_s": 3,
            "tool_calls": 2,
        },
    ]
    report = bench.summarize(items, evidence_kind="offline_demo", options={})
    assert report["known_tokens"] == 12 and report["unknown_calls"] == 1 and report["duration_s"] == 5
    assert report["first_pass_successes"] == 0 and report["verified_successes"] == 1
    assert len(report["attempts"]) == 2
    assert report["evidence_kind"] == "offline_demo" and report["schema_version"] == 1
    assert report["git_revision"]


def test_offline_benchmark_writes_parseable_evidence(tmp_path):
    bench = importlib.import_module("app.bench")
    report = bench.run_benchmark(Settings(data_dir=tmp_path / "data"), ["t1_single_fix"], tmp_path / "report")
    assert report["verified_successes"] == 1
    assert report["evidence_kind"] == "offline_demo"
    assert json.loads((tmp_path / "report/report.json").read_text(encoding="utf-8"))["attempts"]
    assert (tmp_path / "report/report.md").is_file()


def test_benchmark_pipeline_preserves_failed_attempt(tmp_path):
    from contextlib import nullcontext
    from app import bench

    class Attempts:
        def __init__(self):
            self.n = 0

        def create(self, *args):
            self.n += 1
            return {"run_id": str(self.n)}

        def execute(self, run_id):
            return {
                "run_id": run_id,
                "task_id": "t",
                "status": "failed" if self.n == 1 else "done",
                "verified": self.n == 2,
                "known_tokens": 7 if self.n == 1 else 5,
                "unknown_calls": 0,
                "error_code": "",
            }

        def database(self):
            return nullcontext()

    report = bench.run_benchmark(
        Settings(data_dir=tmp_path / "data"), ["t"], tmp_path / "report", retries=1, service=Attempts()
    )
    assert len(report["attempts"]) == 2 and report["known_tokens"] == 12
    assert report["first_pass_successes"] == 0 and report["verified_successes"] == 1


def test_cli_offline_and_invalid_parameters(tmp_path, monkeypatch):
    from app import bench

    monkeypatch.delenv("CODECRAFT_BASE_URL", raising=False)
    common = [
        "--data-dir",
        str(tmp_path / "data"),
        "--output",
        str(tmp_path / "reports"),
        "--task",
        "t1_single_fix",
    ]
    assert bench.main([*common, "--mode", "local"]) == 2
    assert bench.main([*common, "--repeat", "0"]) == 1
    assert bench.main(common) == 0


def test_context_estimate_measures_message_content(tmp_path, monkeypatch):
    from scripts import measure_compress_scaling
    import sys

    output = tmp_path / "estimate.json"
    monkeypatch.setattr(sys, "argv", ["measure_compress_scaling", "--steps", "2,8", "--output", str(output)])
    measure_compress_scaling.main()
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["evidence_kind"] == "local_estimate"
    assert report["rows"][0]["estimated_full_tokens"] > 500
    assert report["rows"][1]["estimated_full_tokens"] > report["rows"][1]["estimated_compressed_tokens"]
