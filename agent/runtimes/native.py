"""Native runtime control path, used as the SDK comparison baseline."""
from .contract import Action, Executor, Outcome
from dataclasses import asdict
from .tracing import runtime_receipt


def run(executor: Executor, action: Action) -> dict:
    outcome = executor.execute(action)
    return runtime_receipt("native", "1", asdict(action), asdict(outcome))
