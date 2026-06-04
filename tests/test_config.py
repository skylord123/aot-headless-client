"""Tests for aotbot.config.Config loading, validation, and redaction."""

from __future__ import annotations

import dataclasses

import pytest

from aotbot.config import Config, ConfigError

REQUIRED = {
    "AOT_SERVER_HOST": "game.example.com",
    "AOT_SERVER_PORT": "28000",
    "AOT_USERNAME": "botuser",
    "AOT_PASSWORD": "s3cret",
}


def _set_env(monkeypatch, **values):
    """Clear all aotbot vars, then set the provided ones."""
    for var in (
        "AOT_SERVER_HOST",
        "AOT_SERVER_PORT",
        "AOT_USERNAME",
        "AOT_PASSWORD",
        "NODERED_HOST",
        "NODERED_PORT",
        "LOG_LEVEL",
        "DUMP_PACKETS",
        "AOT_SKIP_LIGHTING",
    ):
        monkeypatch.delenv(var, raising=False)
    for key, val in values.items():
        monkeypatch.setenv(key, val)


def _load_from(env):
    """Load directly from a mapping (bypasses .env file)."""
    return Config.load(env=env)


# --- Happy path -----------------------------------------------------------


def test_load_required_only_uses_defaults():
    cfg = _load_from(REQUIRED)
    assert cfg.aot_server_host == "game.example.com"
    assert cfg.aot_server_port == 28000
    assert cfg.aot_username == "botuser"
    assert cfg.aot_password == "s3cret"
    # Defaults.
    assert cfg.nodered_host == "localhost"
    assert cfg.nodered_port == 1881
    assert cfg.log_level == "info"
    assert cfg.dump_packets is False
    assert cfg.aot_skip_lighting is True


def test_load_full_env():
    cfg = _load_from(
        {
            **REQUIRED,
            "NODERED_HOST": "nr.local",
            "NODERED_PORT": "2000",
            "LOG_LEVEL": "DEBUG",
            "DUMP_PACKETS": "true",
            "AOT_SKIP_LIGHTING": "false",
        }
    )
    assert cfg.nodered_host == "nr.local"
    assert cfg.nodered_port == 2000
    assert cfg.log_level == "debug"  # normalized to lowercase
    assert cfg.dump_packets is True
    assert cfg.aot_skip_lighting is False


def test_config_is_frozen():
    cfg = _load_from(REQUIRED)
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.aot_password = "nope"  # type: ignore[misc]


def test_load_reads_os_environ(monkeypatch):
    _set_env(monkeypatch, **REQUIRED)
    cfg = Config.load(load_env_file=False)
    assert cfg.aot_username == "botuser"


def test_values_are_stripped():
    env = {**REQUIRED, "AOT_SERVER_HOST": "  game.example.com  "}
    cfg = _load_from(env)
    assert cfg.aot_server_host == "game.example.com"


# --- Required-var validation ---------------------------------------------


@pytest.mark.parametrize("missing", list(REQUIRED))
def test_missing_required_raises(missing):
    env = {k: v for k, v in REQUIRED.items() if k != missing}
    with pytest.raises(ConfigError) as exc:
        _load_from(env)
    assert missing in str(exc.value)


@pytest.mark.parametrize("var", list(REQUIRED))
def test_empty_required_raises(var):
    env = {**REQUIRED, var: "   "}
    with pytest.raises(ConfigError):
        _load_from(env)


# --- Type / range validation ---------------------------------------------


def test_non_integer_port_raises():
    env = {**REQUIRED, "AOT_SERVER_PORT": "notaport"}
    with pytest.raises(ConfigError):
        _load_from(env)


@pytest.mark.parametrize("port", ["0", "65536", "-1", "99999"])
def test_out_of_range_port_raises(port):
    env = {**REQUIRED, "AOT_SERVER_PORT": port}
    with pytest.raises(ConfigError):
        _load_from(env)


def test_bad_nodered_port_raises():
    env = {**REQUIRED, "NODERED_PORT": "70000"}
    with pytest.raises(ConfigError):
        _load_from(env)


def test_invalid_log_level_raises():
    env = {**REQUIRED, "LOG_LEVEL": "verbose"}
    with pytest.raises(ConfigError):
        _load_from(env)


@pytest.mark.parametrize("val", ["maybe", "2", "tru"])
def test_invalid_bool_raises(val):
    env = {**REQUIRED, "DUMP_PACKETS": val}
    with pytest.raises(ConfigError):
        _load_from(env)


@pytest.mark.parametrize(
    "val,expected",
    [
        ("true", True), ("True", True), ("1", True), ("yes", True),
        ("on", True), ("Y", True),
        ("false", False), ("0", False), ("no", False), ("off", False),
        ("", False),
    ],
)
def test_bool_parsing(val, expected):
    env = {**REQUIRED, "DUMP_PACKETS": val}
    cfg = _load_from(env)
    assert cfg.dump_packets is expected


# --- Redaction ------------------------------------------------------------


def test_redacted_masks_password():
    cfg = _load_from(REQUIRED)
    red = cfg.redacted()
    assert red.aot_password != "s3cret"
    assert "s3cret" not in repr(red)
    assert "s3cret" not in str(red)
    # Non-secret fields are preserved.
    assert red.aot_username == "botuser"
    assert red.aot_server_host == "game.example.com"
    # Original is untouched.
    assert cfg.aot_password == "s3cret"


# --- CLI overrides --------------------------------------------------------


def test_overrides_apply():
    cfg = Config.load(
        env=REQUIRED,
        overrides={"aot_server_host": "10.0.0.5", "aot_server_port": 7777},
    )
    assert cfg.aot_server_host == "10.0.0.5"
    assert cfg.aot_server_port == 7777


def test_none_overrides_ignored():
    cfg = Config.load(
        env=REQUIRED,
        overrides={"aot_server_host": None, "dump_packets": True},
    )
    assert cfg.aot_server_host == "game.example.com"  # unchanged
    assert cfg.dump_packets is True


def test_override_revalidates():
    with pytest.raises(ConfigError):
        Config.load(env=REQUIRED, overrides={"aot_server_port": 0})


def test_unknown_override_raises():
    with pytest.raises(ConfigError):
        Config.load(env=REQUIRED, overrides={"bogus_field": "x"})
