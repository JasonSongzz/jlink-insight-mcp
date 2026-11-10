"""SystemView protocol, accounting and transport tests / 协议、统计及采集测试."""

import base64
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from jlink_mcp.systemview import SYNC, SystemViewDecoder, analyze_systemview
from jlink_mcp.tools import rtt, systemview

pytestmark = pytest.mark.unit


def u(value):
    out = bytearray()
    while value >= 128:
        out.append((value & 127) | 128)
        value >>= 7
    out.append(value)
    return bytes(out)


def packet(event_id, values=(), delta=0, payload=None):
    data = b"".join(u(value) for value in values) if payload is None else payload
    return u(event_id) + (u(len(data)) if event_id >= 24 else b"") + data + u(delta)


def trace():
    return (
        SYNC
        + packet(10)
        + packet(24, (1000000, 48000000, 0x20000000, 2))
        + packet(9, payload=u(123) + u(5) + b"\x04work")
        + packet(4, (123,))
        + packet(2, (15,), 10)
        + packet(2, (7,), 5)
        + packet(3, delta=3)
        + packet(3, delta=2)
        + packet(17, delta=10)
        + packet(11, delta=20)
    )


def test_exclusive_nested_isr_accounting():
    report = analyze_systemview(trace())
    assert report["window_ticks"] == 50
    assert report["contexts"]["task:123"]["ticks"] == 20
    assert report["contexts"]["isr:15"]["ticks"] == 7
    assert report["contexts"]["isr:7"]["ticks"] == 3
    assert report["contexts"]["idle"]["ticks"] == 20
    assert report["tasks"][0]["name"] == "work"
    assert report["tasks"][0]["duration_us"] == 20
    assert sum(x["ticks"] for x in report["contexts"].values()) == 50
    assert report["quality"]["warnings"] == []


@pytest.mark.parametrize("chunk_size", [1, 2, 3, 7, 16, 1000])
def test_every_chunk_boundary(chunk_size):
    raw = trace()
    expected = SystemViewDecoder().feed(raw)
    decoder = SystemViewDecoder()
    events = []
    for i in range(0, len(raw), chunk_size):
        events.extend(decoder.feed(raw[i : i + chunk_size]))
    assert events == expected
    assert decoder.pending == b""


def test_official_documented_overflow_vector():
    # Packet example from SEGGER_SYSVIEW.c protocol introduction.
    report = analyze_systemview(SYNC + bytes.fromhex("01 f4 03 80 80 10"))
    assert report["quality"]["lost_events"] == 500
    assert report["window_ticks"] == 0x40000
    assert report["contexts"]["unknown"]["ticks"] == 0x40000


def test_opaque_os_event_does_not_desynchronize():
    report = analyze_systemview(SYNC + packet(4, (1,)) + packet(1024, delta=500, payload=b"\xff\x80"))
    assert report["events"][-1]["payload_hex"] == "ff80"
    assert report["contexts"]["task:1"]["ticks"] == 500


def test_loss_invalidates_context_until_observed_again():
    report = analyze_systemview(
        SYNC + packet(4, (1,)) + packet(1, (2,), 10) + packet(15, (3,), 20) + packet(4, (2,), 5) + packet(11, delta=7)
    )
    assert report["contexts"]["unknown"]["ticks"] == 35
    assert report["contexts"]["task:2"]["ticks"] == 7


def test_missing_sync_and_truncated_packet_are_reported():
    report = analyze_systemview(b"stale-data" + trace()[:-1])
    assert report["quality"]["discarded_bytes"] == 10
    assert report["quality"]["pending_bytes"] > 0
    assert analyze_systemview(b"abc")["event_count"] == 0


def test_malformed_input_resumes_only_at_sync():
    report = analyze_systemview(SYNC + b"\xff" * 5 + trace())
    assert report["quality"]["decode_errors"] == 1
    assert report["quality"]["segments"] == 2
    assert report["tasks"][0]["name"] == "work"


def test_repeated_sync_does_not_join_time_windows():
    report = analyze_systemview(trace() + trace())
    assert report["window_ticks"] == 100
    assert report["quality"]["segments"] == 2


def test_extended_string_length_is_big_endian_not_varint():
    name = b"x" * 300
    raw = SYNC + packet(9, payload=u(2) + u(1) + b"\xff\x01\x2c" + name)
    assert analyze_systemview(raw)["tasks"][0]["name"] == "x" * 300


def test_frequency_and_event_bounds():
    assert analyze_systemview(trace(), 2000000, 0)["window_us"] == 25
    with pytest.raises(ValueError):
        analyze_systemview(trace(), 0)
    with pytest.raises(ValueError):
        analyze_systemview(trace(), event_limit=1001)
    assert not systemview.systemview_decode("not base64!")["success"]
    encoded = base64.b64encode(trace()).decode()
    assert systemview.systemview_decode(encoded)["data"]["window_ticks"] == 50


@pytest.fixture
def link(monkeypatch):
    mock = Mock()
    mock.halted.return_value = False
    mock.rtt_get_num_up_buffers.return_value = 2
    mock.rtt_get_num_down_buffers.return_value = 1
    mock.rtt_get_buf_descriptor.side_effect = lambda i, up: SimpleNamespace(
        acName=b"Terminal" if up and i == 0 else b"SysView", BufferIndex=i
    )
    mock.rtt_write.return_value = 1
    mock.rtt_read.side_effect = [[], list(trace())] + [[]] * 100
    monkeypatch.setattr(systemview.jlink_manager, "get_jlink", lambda: mock)
    monkeypatch.setattr(rtt, "_rtt_started", False)
    return mock


def test_capture_discovers_separate_up_and_down_channels(link):
    result = systemview.systemview_capture(duration_ms=1)
    assert result["success"]
    assert result["data"]["channel"] == 1
    assert result["data"]["down_channel"] == 0
    assert result["data"]["analysis"]["window_ticks"] == 50
    link.rtt_start.assert_called_once_with(block_address=None)
    assert link.rtt_write.call_args_list[0].args == (0, [1])
    assert link.rtt_write.call_args_list[-1].args == (0, [2])
    link.rtt_stop.assert_called_once()
    link.halt.assert_not_called()
    link.reset.assert_not_called()


def test_cleanup_on_read_failure_preserves_partial_data(link):
    link.rtt_read.side_effect = [[], list(trace()), RuntimeError("USB disconnected")]
    result = systemview.systemview_capture(duration_ms=100)
    assert not result["success"]
    assert base64.b64decode(result["partial_raw_base64"]) == trace()
    assert link.rtt_write.call_args_list[-1].args == (0, [2])
    link.rtt_stop.assert_called_once()


def test_short_start_write_still_attempts_stop(link):
    link.rtt_write.side_effect = [0, 1]
    assert not systemview.systemview_capture(duration_ms=1)["success"]
    assert link.rtt_write.call_args_list[-1].args == (0, [2])
    link.rtt_stop.assert_called_once()


def test_active_normal_rtt_and_halted_target_are_rejected(link, monkeypatch):
    monkeypatch.setattr(rtt, "_rtt_started", True)
    assert not systemview.systemview_capture()["success"]
    link.rtt_start.assert_not_called()
    monkeypatch.setattr(rtt, "_rtt_started", False)
    link.halted.return_value = True
    assert not systemview.systemview_capture()["success"]
    link.rtt_start.assert_not_called()


def test_rtt_consumer_lock(link):
    with rtt._rtt_lock:
        assert not systemview.systemview_capture()["success"]
        assert not rtt.rtt_read()["success"]
        assert not rtt.rtt_stop()["success"]
    link.rtt_start.assert_not_called()


def test_normal_rtt_uses_pylink_signature_and_list_bytes(link):
    assert rtt.rtt_start(buffer_index=1)["success"]
    link.rtt_start.assert_called_once_with()
    link.rtt_read.side_effect = None
    link.rtt_read.return_value = [104, 105]
    assert rtt.rtt_read(buffer_index=1)["data"] == "hi"
    link.rtt_read.assert_called_once_with(1, 1024)
    assert rtt.rtt_stop()["success"]


def test_float_sample_uses_varint_encoded_bits():
    report = analyze_systemview(SYNC + packet(23, (3, 0x3F800000), 2) + packet(11, delta=4))
    assert report["events"][1]["values"] == [3, 0x3F800000]
    assert report["window_ticks"] == 6


def test_cleanup_failures_are_visible(link):
    link.rtt_write.side_effect = [1, RuntimeError("stop write failed")]
    link.rtt_stop.side_effect = RuntimeError("stop RTT failed")
    result = systemview.systemview_capture(duration_ms=1)
    assert len(result["cleanup_warnings"]) == 2


@pytest.mark.asyncio
async def test_mcp_tools_are_registered():
    from jlink_mcp.server import mcp

    tools = {tool.name: tool for tool in await mcp.list_tools()}
    assert "systemview_capture" in tools
    assert "systemview_decode" in tools
    assert "duration_ms" in tools["systemview_capture"].inputSchema["properties"]
