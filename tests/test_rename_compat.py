"""This project was called mcp-can until 0.2.0; old setups must keep working."""
import os

from typer.testing import CliRunner

from peykan import cli, config


def test_legacy_env_prefix_is_still_read(monkeypatch):
    monkeypatch.delenv("PEYKAN_CAN_CHANNEL", raising=False)
    monkeypatch.setenv("MCP_CAN_CAN_CHANNEL", "legacy_channel")
    try:
        assert config.get_settings().can_channel == "legacy_channel"
    finally:
        # get_settings copied it to PEYKAN_CAN_CHANNEL; drop that directly
        # (monkeypatch.delenv would record and later *restore* it).
        os.environ.pop("PEYKAN_CAN_CHANNEL", None)


def test_new_prefix_wins_over_legacy(monkeypatch):
    monkeypatch.setenv("MCP_CAN_CAN_CHANNEL", "old")
    monkeypatch.setenv("PEYKAN_CAN_CHANNEL", "new")
    assert config.get_settings().can_channel == "new"


def test_legacy_command_still_works(monkeypatch, capsys):
    called = []
    monkeypatch.setattr(cli, "app", lambda: called.append(True))
    cli.legacy_main()
    assert called and "use `peykan` instead" in capsys.readouterr().err


def test_peykan_command_help():
    result = CliRunner().invoke(cli.app, ["--help"])
    assert result.exit_code == 0 and "Peykan" in result.output
