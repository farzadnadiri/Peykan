import subprocess
import sys


def test_logs_go_to_stderr_not_stdout():
    # stdout carries command output (JSON) and, over the stdio transport,
    # the MCP protocol itself; a log line there corrupts both.
    code = (
        "import logging; from peykan.config import configure_logging; configure_logging(); "
        "logging.getLogger('peykan.test').warning('hello-from-log'); "
        "logging.getLogger('Connection').info('chatter'); print('OUTPUT')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
    )
    assert result.stdout.strip() == "OUTPUT"
    assert "hello-from-log" in result.stderr
    assert "chatter" not in result.stderr  # udsoncan's INFO noise is quieted
