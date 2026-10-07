from typing import List, Union

IntLike = Union[int, str]


def parse_int(value: IntLike) -> int:
    """Parse an ID/code given as an int or as a decimal/hex string.

    Accepts ``61444``, ``"61444"``, ``"0xF004"`` and bare hex like ``"F004"``.
    Bare digits are always decimal (``"10"`` is 10, not 16). Used by both the
    CLI and the MCP tools: LLMs -- small local ones especially -- tend to
    think in the hex notation automotive docs use and get the hex-to-decimal
    conversion wrong, so tools take the hex string as-is instead.
    """
    if isinstance(value, int):
        return value
    text = value.strip().replace("_", "")
    try:
        if text.lower().startswith("0x"):
            return int(text, 16)
        if text.isdigit():
            return int(text)
        return int(text, 16)
    except ValueError:
        raise ValueError(
            f"{value!r} is not a number; use decimal (61444) or hex ('0xF004')"
        ) from None


def parse_data_bytes(data: str) -> List[int]:
    """Parse a frame payload typed on the command line.

    Space-separated tokens are hex, as candump and most CAN tools print them
    ("e8 03 a0 32"); comma-separated tokens are decimal ("232,3,160,50").
    Either form accepts an explicit "0x" prefix. One rule for the whole
    string, never a per-byte guess: "20 4e" must not mean 20 decimal next
    to 0x4E.
    """
    if "," in data:
        tokens = [t.strip() for t in data.split(",") if t.strip()]
        values = [int(t, 16) if t.lower().startswith("0x") else int(t) for t in tokens]
    else:
        values = [int(t, 16) for t in data.split() if t]
    bad = [v for v in values if not 0 <= v <= 0xFF]
    if bad:
        raise ValueError(f"byte values must be 0-255, got {bad}")
    return values
