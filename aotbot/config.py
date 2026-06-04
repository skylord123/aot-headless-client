"""Runtime configuration for the Age of Time bot.

Configuration is loaded from environment variables, optionally seeded from a
`.env` file via python-dotenv. ``Config`` is a frozen dataclass; build it with
``Config.load()``. Required values (server host/port, credentials) are validated
and a clear ``ConfigError`` is raised when any are missing or malformed.

The variable list lives in ``.env.example`` at the repo root — keep them in sync.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, fields, replace
from typing import Any, Mapping

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dependency is declared, but be friendly.
    load_dotenv = None  # type: ignore[assignment]


__all__ = ["Config", "ConfigError"]

# Sentinel used in redacted views so logs never leak the real password.
_REDACTED = "***redacted***"

_TRUTHY = {"1", "true", "yes", "on", "y", "t"}
_FALSY = {"0", "false", "no", "off", "n", "f", ""}


class ConfigError(ValueError):
    """Raised when configuration is missing or invalid."""


def _str2bool(value: str, *, var: str) -> bool:
    norm = value.strip().lower()
    if norm in _TRUTHY:
        return True
    if norm in _FALSY:
        return False
    raise ConfigError(
        f"{var}={value!r} is not a valid boolean "
        f"(expected one of true/false/1/0/yes/no/on/off)."
    )


def _to_int(value: str, *, var: str) -> int:
    try:
        return int(value.strip())
    except ValueError as exc:
        raise ConfigError(f"{var}={value!r} is not a valid integer.") from exc


@dataclass(frozen=True, slots=True)
class Config:
    """Effective runtime configuration.

    Construct via :meth:`load`. Instances are immutable; use
    :func:`dataclasses.replace` (or pass ``overrides`` to :meth:`load`) to derive
    a modified copy.
    """

    # Age of Time game server (required).
    aot_server_host: str
    aot_server_port: int

    # Bot account credentials (required).
    aot_username: str
    aot_password: str

    # Node-RED TCP bridge.
    nodered_host: str = "localhost"
    nodered_port: int = 1881

    # Auto-create the character if login fails with "Character does not exist!".
    # Mirrors $SKYLORD::ENV::BOT::AUTO_LOGIN::CREATE_USER::* in the in-game bot.
    aot_create_user: bool = False
    aot_create_overwrite: bool = False
    aot_create_abilities: str = ""  # "" -> server/GUI default "1 1 1 1 1 1 1"
    # Appearance fields: raw strings; "" / "-1" mean "randomize" (see env.cs).
    aot_create_gender: str = ""       # 0=Male, 1=Female
    aot_create_posture: str = ""      # 0.0-1.0
    aot_create_chest: str = ""        # 0.0-1.0
    aot_create_x_scale: str = ""      # 0.9-1.1
    aot_create_y_scale: str = ""      # 0.9-1.1
    aot_create_z_scale: str = ""      # 0.9-1.1
    aot_create_skin_tone: str = ""    # 0-9
    aot_create_lip_tone: str = ""     # 0-9 (clamped >= skin_tone)
    aot_create_hair_style: str = ""   # 0=Part,1=Up,2=Exotic
    aot_create_hair_color: str = ""   # 0=Blonde,1=Brown,2=Black,3=Red,4=White
    aot_create_eye_color: str = ""    # 0=Blue,1=Green,2=Brown,3=Black
    aot_create_face: str = ""         # 0=A,1=B
    aot_create_ears: str = ""         # 0=Human,1=Elf
    aot_create_glasses: str = ""      # 0=Off,1=On

    # Behavior / debugging.
    log_level: str = "info"
    dump_packets: bool = False
    aot_skip_lighting: bool = True
    # Live-entity telemetry: when True, decode the ongoing ghost section fully and
    # maintain a scoped-object registry (positions/shapes/etc.). When False (the
    # default), the ghost section is left untouched (minimal CPU) -- chat/login
    # still work since the ghost section is last in each packet.
    aot_track_objects: bool = False

    @classmethod
    def load(
        cls,
        *,
        env: Mapping[str, str] | None = None,
        dotenv_path: str | None = None,
        load_env_file: bool = True,
        overrides: Mapping[str, Any] | None = None,
    ) -> "Config":
        """Build a :class:`Config` from environment variables.

        Args:
            env: Source mapping for variables. Defaults to ``os.environ``.
                When provided, the ``.env`` file is *not* loaded (the caller is
                in full control of the environment — handy for tests).
            dotenv_path: Optional explicit path to a ``.env`` file.
            load_env_file: When True (and ``env`` is None), load a ``.env`` file
                into ``os.environ`` before reading. Existing env vars win.
            overrides: Optional mapping of dataclass field names to values that
                take precedence over the environment (e.g. parsed CLI flags).
                ``None`` values are ignored so partial CLI overrides are easy.

        Raises:
            ConfigError: If a required variable is missing or a value is malformed.
        """
        if env is None:
            if load_env_file and load_dotenv is not None:
                # Existing process env vars take precedence over the file.
                load_dotenv(dotenv_path=dotenv_path, override=False)
            env = os.environ

        host = _require(env, "AOT_SERVER_HOST")
        username = _require(env, "AOT_USERNAME")
        password = _require(env, "AOT_PASSWORD")

        port = _to_int(_require(env, "AOT_SERVER_PORT"), var="AOT_SERVER_PORT")

        nodered_host = env.get("NODERED_HOST", "localhost").strip() or "localhost"
        nodered_port = _to_int(
            env.get("NODERED_PORT", "1881"), var="NODERED_PORT"
        )

        log_level = (env.get("LOG_LEVEL", "info").strip() or "info").lower()
        dump_packets = _str2bool(
            env.get("DUMP_PACKETS", "false"), var="DUMP_PACKETS"
        )
        skip_lighting = _str2bool(
            env.get("AOT_SKIP_LIGHTING", "true"), var="AOT_SKIP_LIGHTING"
        )
        track_objects = _str2bool(
            env.get("AOT_TRACK_OBJECTS", "false"), var="AOT_TRACK_OBJECTS"
        )
        create_user = _str2bool(
            env.get("AOT_CREATE_USER", "false"), var="AOT_CREATE_USER"
        )
        create_overwrite = _str2bool(
            env.get("AOT_CREATE_OVERWRITE", "false"), var="AOT_CREATE_OVERWRITE"
        )
        create_abilities = env.get("AOT_CREATE_ABILITIES", "").strip()
        # Appearance fields stay raw strings ("" / "-1" => randomize downstream).
        ck = lambda var: env.get(var, "").strip()  # noqa: E731

        cfg = cls(
            aot_server_host=host,
            aot_server_port=port,
            aot_username=username,
            aot_password=password,
            nodered_host=nodered_host,
            nodered_port=nodered_port,
            aot_create_user=create_user,
            aot_create_overwrite=create_overwrite,
            aot_create_abilities=create_abilities,
            aot_create_gender=ck("AOT_CREATE_GENDER"),
            aot_create_posture=ck("AOT_CREATE_POSTURE"),
            aot_create_chest=ck("AOT_CREATE_CHEST"),
            aot_create_x_scale=ck("AOT_CREATE_X_SCALE"),
            aot_create_y_scale=ck("AOT_CREATE_Y_SCALE"),
            aot_create_z_scale=ck("AOT_CREATE_Z_SCALE"),
            aot_create_skin_tone=ck("AOT_CREATE_SKIN_TONE"),
            aot_create_lip_tone=ck("AOT_CREATE_LIP_TONE"),
            aot_create_hair_style=ck("AOT_CREATE_HAIR_STYLE"),
            aot_create_hair_color=ck("AOT_CREATE_HAIR_COLOR"),
            aot_create_eye_color=ck("AOT_CREATE_EYE_COLOR"),
            aot_create_face=ck("AOT_CREATE_FACE"),
            aot_create_ears=ck("AOT_CREATE_EARS"),
            aot_create_glasses=ck("AOT_CREATE_GLASSES"),
            log_level=log_level,
            dump_packets=dump_packets,
            aot_skip_lighting=skip_lighting,
            aot_track_objects=track_objects,
        )
        cfg._validate()

        if overrides:
            known = {f.name for f in fields(cls)}
            applied = {
                k: v for k, v in overrides.items() if v is not None and k in known
            }
            unknown = set(overrides) - known
            if unknown:
                raise ConfigError(
                    f"Unknown config override(s): {', '.join(sorted(unknown))}"
                )
            if applied:
                cfg = replace(cfg, **applied)
                cfg._validate()

        return cfg

    def _validate(self) -> None:
        if not (0 < self.aot_server_port < 65536):
            raise ConfigError(
                f"AOT_SERVER_PORT must be in 1..65535, got {self.aot_server_port}."
            )
        if not (0 < self.nodered_port < 65536):
            raise ConfigError(
                f"NODERED_PORT must be in 1..65535, got {self.nodered_port}."
            )
        valid_levels = {"debug", "info", "warning", "error", "critical"}
        if self.log_level not in valid_levels:
            raise ConfigError(
                f"LOG_LEVEL={self.log_level!r} invalid; "
                f"expected one of {', '.join(sorted(valid_levels))}."
            )

    def redacted(self) -> "Config":
        """Return a copy with the password masked, safe to log/repr."""
        return replace(self, aot_password=_REDACTED)


def _require(env: Mapping[str, str], var: str) -> str:
    value = env.get(var)
    if value is None or value.strip() == "":
        raise ConfigError(
            f"Required configuration variable {var} is missing or empty. "
            f"Set it in your environment or .env file (see .env.example)."
        )
    return value.strip()
