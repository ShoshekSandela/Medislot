"""Mandatory test 3 (deployed): forced failure during release leaves exactly one appointment.

NOT IMPLEMENTED. Forcing the release step to fail on a deployed stack needs a fault-injection
hook. No such hook exists in the Lambda code, and a production test hook was deliberately not
added. The equivalent behaviour is proven by tests/test_m6_reschedule.py (fault injected at the
release, commit and re-claim steps). This test is skipped with that reason until a test-only
fault path is designed and approved (see docs/M6_M7_STATUS.md).
"""

import pytest


@pytest.mark.skip(reason="Deployed fault-injection hook not implemented; see docs/M6_M7_STATUS.md")
def test_forced_release_failure_keeps_exactly_one_appointment():
    raise AssertionError("unreachable until a fault-injection path is approved")
