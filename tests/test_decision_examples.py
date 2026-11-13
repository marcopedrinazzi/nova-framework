import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("mode", ["prompt", "typed", "agent"])
def test_fixture_demos_are_executable(mode):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    result = subprocess.run([sys.executable, str(ROOT / "examples/decision/demo.py"), mode],
                            capture_output=True, text=True, env=env, check=True)
    assert "FIXTURE OUTPUT" in result.stdout
    if mode == "typed":
        assert '"score": 2.2' in result.stdout and '"confidence": 0.8' in result.stdout
    if mode == "agent":
        assert "STUB BLOCKED before execution" in result.stdout
        assert "STUB EXECUTED" not in result.stdout


def test_fixture_quality_harness_marks_its_limits():
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    result = subprocess.run([sys.executable, str(ROOT / "examples/decision/evaluate_quality.py"), "--limit", "9"],
                            capture_output=True, text=True, env=env, check=True)
    assert '"http_requests": 0' in result.stdout
    assert '"indeterminate": 1' in result.stdout
    assert "not detection-quality measurements" in result.stdout


def test_provider_comparison_retains_negative_and_incomplete_results(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "examples/decision"))
    from compare_providers import assess
    from test_decision_integration import FixtureEvaluator, rule
    from nova.sdk import Nova
    for confidence, noul, condition, indeterminate in [(.8, .1, "decision.$scope", 1), (.1, .8, "decision.$operation", 2)]:
        nova = Nova(rules=[rule(condition)], decision_config={"enabled": True},
                    decision_evaluator=FixtureEvaluator(confidence=confidence, noul=noul))
        report = assess(nova, [("sample", "Read", "Read", False), ("missing", "", "Read", None)])
        assert report["cases"][0]["results"]["Risk"]
        assert report["metrics"]["indeterminate"] == indeterminate
        assert report["metrics"]["reported_api_cost"] is None


@pytest.mark.parametrize("arguments, expected", [
    (["--openrouter-model", "typesafe/jev-1.14"], [("openrouter", "typesafe/jev-1.14")]),
    (["--ollama-model", "laya:latest", "nimble"], [("ollama", "laya:latest"), ("ollama", "nimble")]),
    (["--openrouter-model", "model-a", "model-b"], [("openrouter", "model-a"), ("openrouter", "model-b")]),
    (["--ollama-model", "shared", "--openrouter-model", "shared"], [("ollama", "shared"), ("openrouter", "shared")]),
    (["--ollama-model", "laya:latest", "--openrouter"], [("ollama", "laya:latest"), ("openrouter", "typesafe/jev-1.14")]),
    (["--openrouter"], [("openrouter", "typesafe/jev-1.14")]),
    (["--ollama-model", "one", "--ollama-model", "two"], [("ollama", "one"), ("ollama", "two")]),
    (["--openrouter-model", "one", "--openrouter-model", "two"], [("openrouter", "one"), ("openrouter", "two")]),
    (["--openrouter", "--openrouter-model", "custom"], [("openrouter", "custom")]),
    (["--ollama-model", "same", "same"], [("ollama", "same")]),
])
def test_comparison_runs_requested_models(arguments, expected, monkeypatch, capsys):
    import json
    monkeypatch.syspath_prepend(str(ROOT / "examples/decision"))
    import compare_providers
    from demo import FixtureEvaluator
    from nova.sdk import Nova

    configs = []

    def detector(**kwargs):
        configs.append(kwargs["decision_config"])
        return Nova(**kwargs, decision_evaluator=FixtureEvaluator())

    monkeypatch.setattr(compare_providers, "Nova", detector)
    monkeypatch.setattr(sys, "argv", ["compare_providers.py", *arguments, "--limit", "1"])
    compare_providers.main()
    report = json.loads(capsys.readouterr().out)
    assert [(c["provider"], c["model"]) for c in configs] == expected
    assert {(provider, model) for provider, models in report["providers"].items() for model in models} == set(expected)
    for provider, model in expected:
        assert report["providers"][provider][model]["metrics"]["cases"] == 1


def test_comparison_requires_a_model(monkeypatch, capsys):
    monkeypatch.syspath_prepend(str(ROOT / "examples/decision"))
    from compare_providers import main
    monkeypatch.setattr(sys, "argv", ["compare_providers.py"])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    assert "Specify --ollama-model, --openrouter-model or --openrouter" in capsys.readouterr().err


def test_comparison_loads_explicit_config(tmp_path, monkeypatch, capsys):
    monkeypatch.syspath_prepend(str(ROOT / "examples/decision"))
    import compare_providers
    import nova.utils.config as config_module
    from demo import FixtureEvaluator
    from nova.sdk import Nova

    config = tmp_path / "nova-config"
    config.write_text("[api_keys]\nopenrouter = test-key\n")
    monkeypatch.setattr(config_module, "_config", None)

    def detector(**kwargs):
        assert config_module.get_config().get("api_keys", "openrouter") == "test-key"
        return Nova(**kwargs, decision_evaluator=FixtureEvaluator())

    monkeypatch.setattr(compare_providers, "Nova", detector)
    monkeypatch.setattr(sys, "argv", ["compare_providers.py", "--openrouter", "--config", str(config), "--limit", "1"])
    compare_providers.main()
    assert '"test-key"' not in capsys.readouterr().out
