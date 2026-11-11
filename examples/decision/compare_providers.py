"""Evaluate identical labelled cases locally, optionally also sending them to OpenRouter."""

import argparse
import json
from pathlib import Path
import statistics
import time

from nova.core.decision import NovaEvaluationError
from nova.sdk import Nova
from evaluate_quality import CASES


def assess(detector, cases):
    metrics = {"cases": 0, "false_positives": 0, "misses": 0, "indeterminate": 0,
               "errors": 0, "http_requests": 0, "reported_api_cost": None}
    rows, durations = [], []
    for name, goal, action, expected in cases:
        started = time.perf_counter()
        reasons, result, matched = [], None, None
        if not goal:
            reasons = ["missing_approved_task"]
        else:
            try:
                result = detector.scan(action, decision_state={"approved_task": goal,
                                       "current_action": action, "previous_steps": []})
                matched = bool(result.matches)
            except NovaEvaluationError as error:
                result, reasons = error.result, error.causes
        elapsed = (time.perf_counter() - started) * 1000
        durations.append(elapsed)
        metrics["cases"] += 1
        metrics["indeterminate"] += int(matched is None)
        metrics["false_positives"] += int(matched is True and expected is False)
        metrics["misses"] += int(matched is False and expected is True)
        error_count = 0
        if result is not None:
            error_count = sum(entry["status"] in ("error", "unavailable")
                              for entries in result.decision_results.values() for entry in entries.values())
            for batches in result.decision_batches.values():
                for batch in batches:
                    metrics["http_requests"] += batch.get("request_count", 0)
                    cost = batch.get("usage", {}).get("cost")
                    if type(cost) in (float, int):
                        metrics["reported_api_cost"] = (metrics["reported_api_cost"] or 0) + cost
        metrics["errors"] += error_count
        rows.append({"case": name, "expected": expected, "matched": matched, "latency_ms": elapsed,
                     "reasons": reasons, "results": result.decision_results if result is not None else {}})
    metrics["first_case_ms"] = durations[0]
    metrics["subsequent_median_ms"] = statistics.median(durations[1:]) if len(durations) > 1 else None
    return {"metrics": metrics, "cases": rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ollama-model", required=True, help="Decision model already available in Ollama")
    parser.add_argument("--ollama-host", default="", help="Ollama URL; otherwise uses OLLAMA_HOST or localhost:11434")
    parser.add_argument("--openrouter", action="store_true", help="Also send these synthetic cases to OpenRouter")
    parser.add_argument("--openrouter-model", default="typesafe/jev-1.14")
    parser.add_argument("--limit", type=int, default=3, choices=range(1, len(CASES) + 1))
    args = parser.parse_args()
    configs = [{"enabled": True, "provider": "ollama", "model": args.ollama_model, "host": args.ollama_host}]
    if args.openrouter:
        configs.append({"enabled": True, "provider": "openrouter", "model": args.openrouter_model})
    output = {"note": "Small illustrative dataset; results do not establish general detection quality.", "providers": {}}
    for config in configs:
        nova = Nova(rules_path=Path(__file__).with_name("agent_action_risk.nov"), decision_config=config)
        output["providers"][config["provider"]] = assess(nova, CASES[:args.limit])
    print(json.dumps(output, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
