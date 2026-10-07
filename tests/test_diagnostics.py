from peykan.diagnostics import (
    SERVICE_IDS,
    ecu_name_from_response_message,
    handle_service,
    response_code_name,
)


def test_start_session_and_reset_are_acknowledged_ok():
    for service in (SERVICE_IDS["START_DIAGNOSTIC_SESSION"], SERVICE_IDS["RESET_ECU"]):
        code, data = handle_service(service, parameter_id=0, data_field=0)
        assert response_code_name(code) == "OK"
        assert data == 0


def test_read_data_by_id_returns_deterministic_value():
    code, data = handle_service(SERVICE_IDS["READ_DATA_BY_ID"], parameter_id=5, data_field=0)
    assert response_code_name(code) == "OK"
    assert data == (5 * 37) % 0xFFFFFFFFFF


def test_unsupported_service_is_rejected():
    code, data = handle_service(SERVICE_IDS["WRITE_MEMORY"], parameter_id=0, data_field=0)
    assert response_code_name(code) == "SERVICE_NOT_SUPPORTED"
    assert data == 0


def test_unknown_response_code_is_labeled():
    assert response_code_name(255) == "UNKNOWN(0xff)"


def test_ecu_name_from_response_message():
    assert ecu_name_from_response_message("DIAGNOSTIC_RESPONSE_ENGINE") == "ENGINE"


def test_service_ids_match_the_dbc():
    # SERVICE_IDS once said WRITE_MEMORY=0x30 while the DBC says 50 (0x32).
    from peykan.config import DEFAULT_DBC_PATH
    from peykan.dbc import load_dbc

    request = load_dbc(DEFAULT_DBC_PATH).get_message_by_name("DIAGNOSTIC_REQUEST")
    signal = request.get_signal_by_name("SERVICE_ID")
    assert {str(name): value for value, name in signal.choices.items()} == SERVICE_IDS


def test_only_known_read_services_count_as_reads():
    from peykan.diagnostics import is_write_service

    reads = ("START_DIAGNOSTIC_SESSION", "READ_DATA_BY_ID", "READ_MEMORY")
    writes = ("RESET_ECU", "ROUTINE_CONTROL", "WRITE_MEMORY")
    assert not any(is_write_service(SERVICE_IDS[n]) for n in reads)
    assert all(is_write_service(SERVICE_IDS[n]) for n in writes)
    assert is_write_service(0x99)  # unknown: default-deny
