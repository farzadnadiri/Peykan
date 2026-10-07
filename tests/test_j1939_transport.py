from peykan import j1939

TWO_DTCS = [j1939.J1939Dtc(spn=1322, fmi=31), j1939.J1939Dtc(spn=651, fmi=7)]


def _frames(pairs):
    return [
        {"arbitration_id": cid, "data": data, "timestamp": i}
        for i, (cid, data) in enumerate(pairs)
    ]


def test_short_payloads_stay_single_frame():
    payload = j1939.build_dm1([TWO_DTCS[0]], mil_on=True)
    frames = j1939.frames_for_pgn(j1939.PGN_DM1, payload, j1939.SA_ENGINE)
    assert len(frames) == 1
    assert j1939.parse_can_id(frames[0][0]).pgn == j1939.PGN_DM1


def test_bam_announcement_and_packets():
    payload = bytes(range(20))
    frames = j1939.build_bam(j1939.PGN_DM1, payload, j1939.SA_ENGINE)
    announce_id, announce = frames[0]
    assert j1939.parse_can_id(announce_id).pgn == j1939.PGN_TP_CM
    assert announce[0] == j1939.TP_CM_BAM
    assert announce[1] | (announce[2] << 8) == 20
    assert announce[3] == 3  # ceil(20 / 7)
    assert announce[5] | (announce[6] << 8) | (announce[7] << 16) == j1939.PGN_DM1
    assert [data[0] for _, data in frames[1:]] == [1, 2, 3]
    assert frames[-1][1][-1] == 0xFF  # last packet padded


def test_reassembler_round_trip():
    payload = bytes(range(30))
    reassembler = j1939.TransportReassembler()
    results = [reassembler.feed(cid, data) for cid, data in j1939.build_bam(0xFEE5, payload, 0x17)]
    assert results[:-1] == [None] * (len(results) - 1)
    assert results[-1] == (0xFEE5, 0x17, payload)


def test_reassembler_drops_out_of_sequence_sessions():
    frames = j1939.build_bam(j1939.PGN_DM1, bytes(range(20)), 0)
    reassembler = j1939.TransportReassembler()
    reassembler.feed(*frames[0])
    reassembler.feed(*frames[2])  # packet 2 before packet 1
    assert reassembler.feed(*frames[3]) is None


def test_latest_dm1_handles_bam_and_single_frames():
    multi = j1939.build_dm1(TWO_DTCS, mil_on=True)
    bam = j1939.frames_for_pgn(j1939.PGN_DM1, multi, j1939.SA_ENGINE)
    single = j1939.frames_for_pgn(j1939.PGN_DM1, j1939.build_dm1([]), j1939.SA_ENGINE)

    latest = j1939.latest_dm1(_frames(bam))
    assert latest["dtcs"] == TWO_DTCS
    assert latest["lamps"]["malfunction_indicator"] == "on"

    # A later all-clear DM1 supersedes the BAM one.
    latest = j1939.latest_dm1(_frames(bam + single))
    assert latest["dtcs"] == []
    assert j1939.latest_dm1([]) is None


def test_vep1_battery_voltage_round_trip():
    data = j1939.encode_pgn(j1939.PGN_VEP1, {"BATTERY_POTENTIAL_POWER_INPUT_1": 13.85})
    decoded = j1939.decode_pgn(j1939.PGN_VEP1, data)
    assert abs(decoded["BATTERY_POTENTIAL_POWER_INPUT_1"] - 13.85) < 0.05
    assert j1939.resolve_pgn("VEP1") == j1939.PGN_VEP1


def test_dm1s_from_several_ecus_are_all_reported():
    # Engine (SA 0x00) with a misfire via BAM, brakes (0x0B) with one DTC,
    # transmission (0x03) all clear: none of them may hide another.
    engine = j1939.frames_for_pgn(
        j1939.PGN_DM1, j1939.build_dm1(TWO_DTCS, mil_on=True), j1939.SA_ENGINE
    )
    brakes = j1939.frames_for_pgn(
        j1939.PGN_DM1, j1939.build_dm1([j1939.J1939Dtc(spn=84, fmi=5)]), j1939.SA_BRAKES
    )
    trans = j1939.frames_for_pgn(j1939.PGN_DM1, j1939.build_dm1([]), 0x03)
    by_source = j1939.latest_dm1_by_source(_frames(engine + brakes + trans))
    assert set(by_source) == {0x00, 0x0B, 0x03}
    merged = j1939.merge_dm1s(by_source)
    assert {(sa, d.spn) for sa, d in merged["dtcs"]} == {(0x00, 1322), (0x00, 651), (0x0B, 84)}
    assert merged["lamps"]["malfunction_indicator"] == "on"  # from the engine only
