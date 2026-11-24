"""Fail-closed guards: dev-only approval shortcuts are disabled whenever live dispatch is unrestricted."""
import pytest

from app.config import Settings


def make(**overrides) -> Settings:
    base = {"APP_ENV": "development"}
    base.update(overrides)
    return Settings(**base)


def test_role_override_disabled_by_default():
    assert make().allow_dev_role_override is False
    assert make().effective_allow_dev_role_override is False


def test_role_override_allowed_only_in_dev_without_live_dispatch():
    assert make(ALLOW_DEV_ROLE_OVERRIDE=True).effective_allow_dev_role_override is True


@pytest.mark.parametrize("flag", ["ALLOW_DEV_ROLE_OVERRIDE", "ALLOW_DEV_SHARED_APPROVAL"])
def test_dev_shortcuts_forced_off_under_unrestricted_live_dispatch(flag):
    s = make(**{flag: True, "OUTREACH_DISPATCH_ENABLED": True, "OUTREACH_CANARY_MODE": False})
    assert s.is_unrestricted_live_dispatch is True
    assert s.effective_allow_dev_role_override is False
    assert s.effective_allow_dev_shared_approval is False


def test_canary_mode_is_not_unrestricted_live_dispatch():
    s = make(OUTREACH_DISPATCH_ENABLED=True, OUTREACH_CANARY_MODE=True, ALLOW_DEV_ROLE_OVERRIDE=True)
    assert s.is_unrestricted_live_dispatch is False
    assert s.effective_allow_dev_role_override is True


@pytest.mark.parametrize("env", ["production", "staging"])
def test_dev_shortcuts_forced_off_in_production(env):
    s = make(APP_ENV=env, ALLOW_DEV_ROLE_OVERRIDE=True, ALLOW_DEV_SHARED_APPROVAL=True)
    assert s.effective_allow_dev_role_override is False
    assert s.effective_allow_dev_shared_approval is False
