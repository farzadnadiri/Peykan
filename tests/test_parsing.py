import pytest

from peykan import j1939
from peykan.parsing import parse_int


@pytest.mark.parametrize(
    "value, expected",
    [
        (61444, 61444),
        ("61444", 61444),
        ("0xF004", 0xF004),
        ("0XF004", 0xF004),
        ("F004", 0xF004),
        ("f004", 0xF004),
        (" 0x0D ", 0x0D),
        ("10", 10),  # bare digits are decimal, never hex
        ("0x18FECA00", 0x18FECA00),
    ],
)
def test_parse_int_accepts_decimal_and_hex(value, expected):
    assert parse_int(value) == expected


def test_parse_int_rejects_garbage_with_a_helpful_message():
    with pytest.raises(ValueError, match="decimal .* or hex"):
        parse_int("engine speed")


@pytest.mark.parametrize(
    "value, expected",
    [
        ("EEC1", j1939.PGN_EEC1),  # also valid hex (0xEEC1): acronym must win
        ("dd1", j1939.PGN_DD1),  # likewise 0xDD1
        ("ET1", j1939.PGN_ET1),
        ("0xFEEE", j1939.PGN_ET1),
        ("FEEE", j1939.PGN_ET1),
        (65262, j1939.PGN_ET1),
    ],
)
def test_resolve_pgn_accepts_acronyms_hex_and_ints(value, expected):
    assert j1939.resolve_pgn(value) == expected


def test_data_bytes_use_one_rule_per_string():
    from peykan.parsing import parse_data_bytes

    # Space-separated is hex, like candump -- including all-digit bytes.
    assert parse_data_bytes("00 00 00 20 4e") == [0, 0, 0, 0x20, 0x4E]
    assert parse_data_bytes("e8 03 a0 32") == [0xE8, 0x03, 0xA0, 0x32]
    # Comma-separated is decimal unless prefixed.
    assert parse_data_bytes("232,3,160,0x32") == [232, 3, 160, 0x32]
    with pytest.raises(ValueError, match="0-255"):
        parse_data_bytes("1ff")
    with pytest.raises(ValueError, match="0-255"):
        parse_data_bytes("1,256")
