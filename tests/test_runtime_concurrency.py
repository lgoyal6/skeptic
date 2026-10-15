"""Check overlapping action delivery at the shared executor boundary.

Native and SDK adapters share agent/runtimes/contract.py for policy and effects.
A second callback must wait for the first action's identity and budget decision.
"""
from concurrent.futures import ThreadPoolExecutor
from threading import Event, current_thread

from agent.runtimes.contract import Action, Executor


def test_overlapping_duplicate_invokes_effect_once():
    first_admitted, release_first, second_done = Event(), Event(), Event()
    effects = []

    def clock():
        if current_thread().name.endswith("_0"):
            first_admitted.set()
            assert release_first.wait(2)
        return 0

    executor = Executor({"write": lambda payload: effects.append(payload) or {"ok": True}},
                        allowed={"write"}, clock=clock)
    action = Action("same", "write", {}, approval_expires_at=10)

    def duplicate():
        try:
            return executor.execute(action)
        finally:
            second_done.set()

    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="delivery") as pool:
        first = pool.submit(executor.execute, action)
        assert first_admitted.wait(2)
        second = pool.submit(duplicate)
        # Before the fix, the duplicate can complete while the first callback
        # is admitted but has not persisted its running receipt.
        second_done.wait(0.2)
        release_first.set()
        outcomes = [first.result(timeout=2), second.result(timeout=2)]
    assert len(effects) == 1
    assert sum(outcome.replayed for outcome in outcomes) == 1
