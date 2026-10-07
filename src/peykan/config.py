import logging
import os
from pathlib import Path
from typing import List, Literal, Optional

from pydantic_settings import BaseSettings, SettingsConfigDict

# Sample DBC shipped inside the package, so `pip install peykan` works from
# any directory without pointing PEYKAN_DBC_PATH at a checkout.
DEFAULT_DBC_PATH = str(Path(__file__).parent / "data" / "vehicle.dbc")


class Settings(BaseSettings):
    can_interface: str = "virtual"
    can_channel: str = "bus0"
    # Passed to python-can when set (real hardware); the virtual bus has none.
    can_bitrate: Optional[int] = None
    dbc_path: str = DEFAULT_DBC_PATH
    # --- Transmit safety (see safety.py) ---
    # None = decide by interface: allowed on the virtual simulator bus,
    # refused on real hardware until explicitly enabled.
    allow_transmit: Optional[bool] = None
    # Services that change ECU state (clear DTCs, reset, write, routines,
    # fault injection, log replay). Same None default as allow_transmit, and
    # never allowed unless transmitting is.
    allow_write_services: Optional[bool] = None
    # Arbitration IDs (e.g. ["0x7DF", "0x7E0"]) the tools may transmit on;
    # empty means no ID restriction once transmitting is allowed.
    transmit_allowlist: List[str] = []
    # Append every transmit attempt (sent or blocked) here as JSON lines.
    transmit_log_path: Optional[str] = None
    # The simulator broadcasts fake ECU traffic; on a real bus that would
    # corrupt a vehicle's network, so it refuses unless this is set (bench
    # setups with two adapters wired together).
    simulator_on_hardware: bool = False
    # Directory the log-analysis tools may read from (MCP clients pass paths
    # relative to it; anything outside is refused).
    log_dir: str = "."
    # Loopback only by default: the tools are unauthenticated and some
    # transmit on the bus. Docker sets 0.0.0.0 so the host can reach it.
    mcp_host: str = "127.0.0.1"
    mcp_port: int = 6278
    mcp_transport: Literal["sse", "streamable-http", "stdio"] = "sse"
    max_duration_s: float = 30.0
    log_level: str = "INFO"
    # Run the SAE J1939 (heavy-duty, 29-bit extended ID) side of the
    # simulator alongside the light-vehicle 11-bit signals. On by default;
    # set PEYKAN_J1939_ENABLED=false for an 11-bit-only bus.
    j1939_enabled: bool = True
    # Wildcard by default for the zero-friction demo experience (e.g. MCP
    # Inspector connecting from a browser); override before any real
    # deployment. Credentialed CORS is only enabled once this is narrowed to
    # specific origins -- allow_credentials=True with a wildcard origin is a
    # combination browsers reject outright, so it's never turned on for "*".
    cors_allow_origins: List[str] = ["*"]

    model_config = SettingsConfigDict(
        env_prefix="PEYKAN_",
        env_file=".env",
        extra="ignore",
    )

    @property
    def is_virtual(self) -> bool:
        return self.can_interface == "virtual"

    @property
    def transmit_allowed(self) -> bool:
        return self.is_virtual if self.allow_transmit is None else self.allow_transmit

    @property
    def write_services_allowed(self) -> bool:
        if not self.transmit_allowed:
            return False
        return self.is_virtual if self.allow_write_services is None else self.allow_write_services


# This project was called mcp-can until 0.2.0, with an MCP_CAN_ prefix.
LEGACY_ENV_PREFIX = "MCP_CAN_"
ENV_PREFIX = "PEYKAN_"
_legacy_warned = False


def _adopt_legacy_env() -> None:
    """Honour MCP_CAN_* variables (warning once) wherever the matching
    PEYKAN_* one isn't set, so existing setups keep working."""
    global _legacy_warned
    adopted = []
    for key, value in list(os.environ.items()):
        if key.upper().startswith(LEGACY_ENV_PREFIX):
            new_key = ENV_PREFIX + key[len(LEGACY_ENV_PREFIX):]
            if new_key not in os.environ:
                os.environ[new_key] = value
                adopted.append(key)
    if adopted and not _legacy_warned:
        _legacy_warned = True
        logging.getLogger(__name__).warning(
            "Using deprecated environment variable(s) %s; rename the MCP_CAN_ prefix "
            "to PEYKAN_ (this project was renamed from mcp-can to Peykan).",
            ", ".join(sorted(adopted)),
        )


def get_settings() -> Settings:
    _adopt_legacy_env()
    return Settings()


_logging_configured = False


def configure_logging(settings: Optional[Settings] = None) -> None:
    """Configure root logging once, using rich's colorized handler if available.

    Safe to call multiple times (e.g. from both the CLI entrypoint and a
    server/simulator `main()` used standalone) — only the first call takes
    effect.
    """
    global _logging_configured
    if _logging_configured:
        return
    settings = settings or get_settings()
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    try:
        from rich.logging import RichHandler

        logging.basicConfig(
            level=level,
            format="%(message)s",
            datefmt="[%X]",
            handlers=[RichHandler(rich_tracebacks=True, show_path=False)],
        )
    except ImportError:
        logging.basicConfig(
            level=level,
            format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        )
    _logging_configured = True
