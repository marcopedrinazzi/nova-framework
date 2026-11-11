import pytest
from nova.core.parser import NovaParser
from nova.core.decision import Predicate
from nova.evaluators.decision.condition import compile_condition
from test_decision_parser import source

T, F, U = Predicate.TRUE, Predicate.FALSE, Predicate.UNKNOWN


@pytest.mark.parametrize("condition,values,expected", [
    ("not decision.$scope", {}, U),
    ("decision.$scope or decision.$risk", {"decision.$scope": T}, T),
    ("decision.$scope and decision.$risk", {"decision.$scope": F}, F),
    ("decision.$scope and decision.$risk", {"decision.$scope": T}, U),
    ("decision.$scope or decision.$risk", {"decision.$scope": F}, U),
    ("2 of decision", {"decision.$scope": T, "decision.$risk": T}, T),
    ("2 of decision", {"decision.$scope": F, "decision.$risk": F}, F),
    ("2 of decision", {"decision.$scope": T}, U),
    ("all of decision", {"decision.$scope": F}, F),
    ("decision.*", {}, U),
    ("all of ($s*)", {"decision.$scope": T}, T),
    ("decision.$s*", {"decision.$scope": T}, T),
])
def test_partial_truth(condition, values, expected):
    node = compile_condition(NovaParser().parse(source(condition)))
    assert node.evaluate(values) is expected


def test_prunes_irrelevant_subexpression():
    node = compile_condition(NovaParser().parse(source("(decision.$scope and decision.$risk) or decision.$operation")))
    assert node.needed({"decision.$scope": F}) == {"decision.$operation"}


def test_predicates_cannot_coerce_uncertainty_to_false():
    with pytest.raises(TypeError):
        bool(U)
