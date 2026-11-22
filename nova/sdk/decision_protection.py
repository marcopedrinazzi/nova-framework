"""Argument-bound pre-execution checks shared by all SDK protection decorators."""

import asyncio
import functools
import inspect

from nova.core.decision import NovaEvaluationError
from .exceptions import NovaBlockedError, NovaConfigError
from .result import ScanResult


def _scan_parameter(func, signature, param_name):
    """Resolve where the scanned argument lives, failing at decoration time if nowhere."""
    parameters = signature.parameters
    named = parameters.get(param_name)
    if named is not None and named.kind not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
        return param_name, None
    var_keyword = next((p.name for p in parameters.values() if p.kind is inspect.Parameter.VAR_KEYWORD), None)
    if var_keyword is not None:
        return None, var_keyword
    available = ", ".join(p.name for p in parameters.values()
                          if p.kind not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD))
    raise NovaConfigError(
        f"protect(): {getattr(func, '__qualname__', func)!s} has no parameter '{param_name}' to scan; "
        f"pass param_name= one of: {available or '(none)'}"
    )


def protect_decision(nova, func, action, severity, param_name, on_block, raise_on_block,
                skip_llm, skip_decision, state_factory):
    signature = inspect.signature(func)
    named, var_keyword = _scan_parameter(func, signature, param_name)

    def arguments_holding_text(bound):
        return bound.arguments if named else bound.arguments.setdefault(var_keyword, {})

    def prepare(args, kwargs):
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        text = arguments_holding_text(bound).get(param_name)
        if not isinstance(text, str):
            raise NovaEvaluationError(ScanResult("", "", evaluation_complete=False), ["missing_scan_text"])
        try:
            state = state_factory(dict(bound.arguments)) if state_factory else None
        except Exception:
            raise NovaEvaluationError(ScanResult(text, text, evaluation_complete=False), ["invalid_context"]) from None
        return bound, text, state

    def check(result, bound):
        if not result.evaluation_complete:
            reasons = sorted({entry for entries in result.incomplete_rules.values() for entry in entries})
            raise NovaEvaluationError(result, reasons or ["incomplete_evaluation"])
        if nova._should_block(result, action, severity):
            if on_block:
                return False, on_block(result)
            if raise_on_block:
                raise NovaBlockedError(result)
            return False, None
        if result.redacted:
            arguments_holding_text(bound)[param_name] = result.sanitized_text
        return True, None

    @functools.wraps(func)
    def sync_wrapper(*args, **kwargs):
        bound, text, state = prepare(args, kwargs)
        result = nova.scan(text, decision_state=state, skip_decision=skip_decision, skip_llm=skip_llm)
        proceed, response = check(result, bound)
        return func(*bound.args, **bound.kwargs) if proceed else response

    @functools.wraps(func)
    async def async_wrapper(*args, **kwargs):
        bound, text, state = prepare(args, kwargs)
        result = await nova.scan_async(text, decision_state=state, skip_decision=skip_decision, skip_llm=skip_llm)
        proceed, response = check(result, bound)
        return await func(*bound.args, **bound.kwargs) if proceed else response

    return async_wrapper if asyncio.iscoroutinefunction(func) else sync_wrapper
