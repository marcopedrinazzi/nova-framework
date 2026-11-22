"""Scope of fail-closed behaviour: unrun checks are reported, runtime failures raise.

A typed rule whose answer is needed while Decision is disabled or skipped must not
break scans for unrelated rules. A check that ran and failed still raises
NovaEvaluationError, and nothing reacts to a scan before that exception.
"""

import asyncio
import logging
from unittest.mock import Mock

import pytest

from nova.core.decision import NovaEvaluationError
from nova.core.parser import NovaParser
from nova.core.scanner import NovaScanner
from nova.sdk import Nova, Action, NovaBlockedError
from nova.sdk.exceptions import NovaConfigError
from test_decision_integration import FixtureEvaluator, rule

KEYWORD_RULE = NovaParser().parse('rule Keyword {\nkeywords:\n$bad = "attack"\ncondition:\nkeywords.$bad\n}')


class FailingEvaluator:
    def __init__(self):
        self.calls = 0

    def evaluate_many(self, patterns, state):
        self.calls += 1
        raise RuntimeError("provider exploded")


def test_disabled_typed_rule_does_not_break_unrelated_rules():
    nova = Nova(rules=[KEYWORD_RULE, rule("llm.$scope")], default_action=Action.BLOCK)
    result = nova.scan("an attack")
    assert result.blocked and [m.rule_name for m in result.matches] == ["Keyword"]
    # The confirmed BLOCK skipped model calls, so the scan is complete.
    assert result.evaluation_complete

    result = nova.scan("ordinary text")
    assert not result.matches and not result.blocked
    assert result.incomplete_rules == {"Risk": ["llm.$scope: disabled"]}
    assert not result.evaluation_complete and not result.allowed and not result.clean
    assert "llm.$scope: disabled" in result.rule_warnings["Risk"]


def test_scanner_reports_incomplete_rules_without_raising():
    scanner = NovaScanner([KEYWORD_RULE, rule("llm.$scope")])
    details = scanner.scan_with_details("an attack")
    assert details["matched_any"] and details["matches"][0]["rule_name"] == "Keyword"
    assert details["incomplete_rules"] == {"Risk": ["llm.$scope: disabled"]}
    assert details["evaluation_complete"] is False
    assert [r["rule_name"] for r in scanner.scan("an attack")] == ["Keyword"]


def test_condition_settled_without_model_is_complete_even_when_disabled():
    r = rule("keywords.$key and llm.$scope", extra='keywords:\n$key = "secret"\n')
    result = Nova(rules=[r]).scan("ordinary")
    assert result.evaluation_complete and result.clean and not result.incomplete_rules


def test_runtime_failure_still_raises_with_partial_result():
    failing = FailingEvaluator()
    nova = Nova(rules=[KEYWORD_RULE, rule("llm.$scope")], decision_config={"enabled": True},
                decision_evaluator=failing)
    with pytest.raises(NovaEvaluationError) as caught:
        nova.scan("an attack")
    assert failing.calls == 1
    assert caught.value.causes == ["evaluator_error"]
    partial = caught.value.partial_result
    assert [m.rule_name for m in partial.matches] == ["Keyword"]
    assert partial.incomplete_rules == {"Risk": ["llm.$scope: evaluator_error"]}
    assert not partial.allowed and not partial.clean


def test_callbacks_never_run_for_a_scan_that_raises():
    on_flag, policy_callback = Mock(), Mock()
    policy = {"Keyword": {"action": "flag", "callback": policy_callback}}
    nova = Nova(rules=[KEYWORD_RULE, rule("llm.$scope")], policy=policy, on_flag=on_flag,
                decision_config={"enabled": True}, decision_evaluator=FailingEvaluator())
    with pytest.raises(NovaEvaluationError):
        nova.scan("an attack")
    on_flag.assert_not_called()
    policy_callback.assert_not_called()

    working = Nova(rules=[KEYWORD_RULE, rule("llm.$scope")], policy=policy, on_flag=on_flag,
                   decision_config={"enabled": True}, decision_evaluator=FixtureEvaluator(noul=.1))
    result = working.scan("an attack")
    assert result.flagged and result.evaluation_complete
    on_flag.assert_called_once_with(result)
    policy_callback.assert_called_once()


@pytest.fixture
def decision_log():
    # NOVA's logger may stop propagation to the root logger, so listen directly.
    records = []
    handler = logging.Handler(logging.WARNING)
    handler.emit = records.append
    logger = logging.getLogger("nova.decision")
    logger.addHandler(handler)
    yield records
    logger.removeHandler(handler)


def test_disabled_decision_is_logged_once_at_load(decision_log):
    nova = Nova(rules=[KEYWORD_RULE, rule("llm.$scope")])
    messages = [r.getMessage() for r in decision_log]
    assert len(messages) == 1 and "Risk" in messages[0] and "enabled" in messages[0]

    decision_log.clear()
    nova.add_rule(rule("llm.$scope", name="Added"))
    Nova(rules=[rule("llm.$scope")], decision_config={"enabled": True}, decision_evaluator=FixtureEvaluator())
    Nova(rules=[KEYWORD_RULE])
    messages = [r.getMessage() for r in decision_log]
    assert len(messages) == 1 and "Added" in messages[0]


@pytest.mark.parametrize("async_mode", [False, True])
def test_protection_still_refuses_unevaluated_checks_with_specific_reasons(async_mode):
    nova = Nova(rules=[rule("llm.$scope")])
    calls = []

    if async_mode:
        @nova.protect(raise_on_block=False)
        async def tool(prompt):
            calls.append(prompt)
    else:
        @nova.protect(raise_on_block=False)
        def tool(prompt):
            calls.append(prompt)

    with pytest.raises(NovaEvaluationError) as caught:
        asyncio.run(tool("synthetic")) if async_mode else tool("synthetic")
    assert caught.value.causes == ["llm.$scope: disabled"]
    assert not calls


def test_protect_rejects_missing_parameter_at_decoration_time():
    nova = Nova(rules=[KEYWORD_RULE])
    with pytest.raises(NovaConfigError, match="no parameter 'prompt'.*question"):
        @nova.protect()
        def ask(question):
            return question

    @nova.protect(param_name="question")
    def ask_named(question):
        return question

    assert ask_named("hello") == "hello"
    with pytest.raises(NovaBlockedError):
        nova.protect(action="block", param_name="question")(lambda question: question)("an attack")


def test_protect_scans_text_passed_through_var_keyword():
    nova = Nova(rules=[KEYWORD_RULE])
    seen = []

    @nova.protect(action="block")
    def handler(**kwargs):
        seen.append(kwargs)
        return "ran"

    assert handler(prompt="hello", user="u1") == "ran"
    assert seen == [{"prompt": "hello", "user": "u1"}]
    with pytest.raises(NovaBlockedError):
        handler(prompt="an attack")
    with pytest.raises(NovaEvaluationError, match="missing_scan_text"):
        handler(user="u1")
    assert len(seen) == 1


def test_protect_writes_redacted_text_back_through_var_keyword():
    nova = Nova(rules=[KEYWORD_RULE], default_action=Action.REDACT)

    @nova.protect()
    def handler(**kwargs):
        return kwargs["prompt"]

    assert handler(prompt="an attack now") == "an [REDACTED] now"


GOOD = 'rule Good {\nkeywords:\n$x = "good"\ncondition:\nkeywords.$x\n}\n'
BROKEN = 'rule Broken {\nmeta:\ndescription = "unclosed\nkeywords:\n$y = "y"\ncondition:\nkeywords.$y\n}\n'


def test_one_malformed_rule_does_not_hide_valid_siblings(tmp_path):
    path = tmp_path / "pack.nov"
    path.write_text("// rule pack header\n" + GOOD + BROKEN, encoding="utf-8")
    nova = Nova(rules_path=path, ignore_invalid_rules=True)
    assert [r.name for r in nova._rules] == ["Good"]
    with pytest.raises(NovaConfigError, match="rule block #2 failed to parse.*Unclosed string"):
        Nova(rules_path=path)


def test_stray_text_before_first_rule_is_still_reported(tmp_path):
    path = tmp_path / "pack.nov"
    path.write_text("not a rule\n" + GOOD + BROKEN, encoding="utf-8")
    with pytest.raises(NovaConfigError, match="rule block #1"):
        Nova(rules_path=path)
