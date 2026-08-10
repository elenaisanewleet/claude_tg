import pytest

from claude_tg.config import ConfigError, Settings

BASE = {"TELEGRAM_BOT_TOKEN": "123:abc", "OWNER_USER_ID": "41082373"}


def test_requires_token():
    with pytest.raises(ConfigError):
        Settings.from_env({"OWNER_USER_ID": "1"})


def test_requires_owner():
    with pytest.raises(ConfigError):
        Settings.from_env({"TELEGRAM_BOT_TOKEN": "123:abc"})


def test_owner_must_be_numeric():
    with pytest.raises(ConfigError):
        Settings.from_env({**BASE, "OWNER_USER_ID": "@lena"})


def test_owner_is_always_allowed():
    settings = Settings.from_env(BASE)
    assert settings.owner_id in settings.allowed_user_ids


def test_allowed_ids_parsing():
    settings = Settings.from_env({**BASE, "ALLOWED_USER_IDS": "10, 20;30 40"})
    assert {10, 20, 30, 40} <= set(settings.allowed_user_ids)


def test_bad_allowed_id_raises():
    with pytest.raises(ConfigError):
        Settings.from_env({**BASE, "ALLOWED_USER_IDS": "10,абв"})


def test_bool_flags():
    settings = Settings.from_env({**BASE, "SHOW_THINKING": "no", "AUTO_UPDATE": "да"})
    assert settings.default_show_thinking is False
    assert settings.auto_update is True


def test_defaults():
    settings = Settings.from_env(BASE)
    assert settings.default_model == "opus"
    assert settings.default_effort == "high"
    assert settings.default_permission_mode == "default"
    assert settings.default_thinking == "adaptive"
