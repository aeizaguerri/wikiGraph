import pytest

from wikigraph.app import LaunchLimits, create_app


def test_launch_limits_default_and_invalid_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WIKIGRAPH_RUN_ATTEMPT_QUOTA", raising=False)
    assert LaunchLimits.from_environment().run_attempt_quota == 120
    for invalid in ("0", "-1", "not-an-integer"):
        monkeypatch.setenv("WIKIGRAPH_RUN_ATTEMPT_QUOTA", invalid)
        with pytest.raises(RuntimeError, match="WIKIGRAPH_RUN_ATTEMPT_QUOTA"):
            create_app()
