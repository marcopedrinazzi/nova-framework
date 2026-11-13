"""Compare identical labelled cases across Ollama and/or OpenRouter models."""

import argparse
import json
from pathlib import Path
import statistics
import time

from nova.core.decision import NovaEvaluationError
from nova.sdk import Nova
from nova.utils.config import get_config
from evaluate_quality import CASES


def assess(detector, cases):
    metrics = {"cases": 0, "false_positives": 0, "misses": 0, "indeterminate": 0,
               "errors": 0, "http_requests": 0, "reported_api_cost": None}
    rows, durations = [], []
    for name, goal, action_to_check, expected in cases:
        started = time.perf_counter()
        reasons, result, matched = [], None, None
        if not goal:
            reasons = ["missing_approved_task"]
        else:
            try:
                result = detector.scan(action_to_check, decision_state={"approved_task": goal,
                                       "current_action": action_to_check, "previous_steps": []})
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
    parser.add_argument("--ollama-model", nargs="+", action="extend", default=[],
                        help="One or more Decision models already available in Ollama; repeatable")
    parser.add_argument("--ollama-host", default="", help="Ollama URL; otherwise uses OLLAMA_HOST or localhost:11434")
    parser.add_argument("--openrouter", action="store_true", help="Use OpenRouter (defaults to typesafe/jev-1.14)")
    parser.add_argument("--openrouter-model", nargs="+", action="extend", default=[],
                        help="One or more OpenRouter Decision model IDs; enables OpenRouter; repeatable")
    parser.add_argument("--config", help="Path to Nova configuration file (including API keys)")
    parser.add_argument("--limit", type=int, default=3, choices=range(1, len(CASES) + 1))
    args = parser.parse_args()
    if args.openrouter and not args.openrouter_model:
        args.openrouter_model = ["typesafe/jev-1.14"]
    if not args.ollama_model and not args.openrouter_model:
        parser.error("Specify --ollama-model, --openrouter-model or --openrouter")
    if args.config:
        get_config(args.config)
    configs = [{"enabled": True, "provider": "ollama", "model": model, "host": args.ollama_host}
               for model in dict.fromkeys(args.ollama_model)]
    configs.extend({"enabled": True, "provider": "openrouter", "model": model}
                   for model in dict.fromkeys(args.openrouter_model))
    output = {"note": "Small illustrative dataset; results do not establish general detection quality.", "providers": {}}
    for config in configs:
        nova = Nova(rules_path=Path(__file__).with_name("agent_action_risk.nov"), decision_config=config)
        output["providers"].setdefault(config["provider"], {})[config["model"]] = assess(nova, CASES[:args.limit])
    print(json.dumps(output, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
