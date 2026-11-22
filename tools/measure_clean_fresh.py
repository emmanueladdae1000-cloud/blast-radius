"""Compare full baseline memory test in fork/exec workers under a real selector heap."""
import gc
import json
import os
from pathlib import Path
import resource
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("BR_SRC", "/app/src")
os.environ.setdefault("BR_DATA", str(ROOT / "sample"))
import selector
import cmpprune
import verify


def main():
    maps = selector.Maps()
    selection = selector.Selector(maps)
    # Build the same baseline coverage/raw-fact heap that a real batch retains.
    # No tests, patches against disk, or challenge answers are executed/read here.
    for path in sorted((ROOT / "sample" / "commits").glob("*.patch")):
        text = path.read_text(encoding="utf-8")
        selection.select(text)
        cmpprune.safe_tests(text, selector.SRC)
    gc.collect()
    report = {"baseline_commit": "eb11aec4be8cb2122afe713a4816662c3f0fb2fa",
              "parent_tracked_objects": len(gc.get_objects()),
              "parent_max_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
              "loaded_coverage_files": len(maps._src), "workers": []}
    test_id = "tests/test_generator.py::TestGenerator::test_generator_memory"
    # The explicit30s measurement budget allows the complete fork baseline to
    # finish; production selector jobs remain independently capped at <=5s.
    job = (2026, {}, [0], 60.0, 30.0)
    for mode in ("0", "1"):
        os.environ["BR_FRESH_EXEC"] = mode
        messages = []
        started = time.monotonic()
        initial_cpu = verify._children_cpu()
        result = verify.run_fresh([job], [test_id], selector.SRC, time.time() + 65,
                                  lambda *words: messages.append(list(words)))
        row = {"mode": "fork" if mode == "0" else "clean_exec",
               "worker_cpu_s": round(verify._children_cpu() - initial_cpu, 6),
               "wall_s": round(time.monotonic() - started, 6),
               "result": result, "log": messages}
        report["workers"].append(row)
        print(json.dumps(row), flush=True)
    target = ROOT / "reports" / "clean-fresh-baseline-memory.json"
    target.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "workers"}), flush=True)


if __name__ == "__main__":
    main()
