from unittest.mock import Mock

import pytest
from websocket import WebSocketTimeoutException

from webex_obs.obs_socket import OBSWebSocket, _ReceiverSocket, loopback_address


@pytest.mark.parametrize(("host", "address"), [
    ("localhost", "127.0.0.1"), ("127.0.0.2", "127.0.0.2"), ("::1", "[::1]"),
])
def test_loopback_addresses(host, address):
    assert loopback_address(host) == address


@pytest.mark.parametrize("host", ["192.0.2.1", "obs.example.test", "0.0.0.0"])
def test_remote_obs_control_is_rejected_before_opening_socket(host, monkeypatch):
    socket_factory = Mock()
    monkeypatch.setattr("webex_obs.obs_socket._ReceiverSocket", socket_factory)
    with pytest.raises(ValueError):
        OBSWebSocket(host, 4455, "").connect()
    socket_factory.assert_not_called()


def test_handshake_timeout_closes_unidentified_socket(monkeypatch):
    socket = Mock()
    socket.recv.side_effect = WebSocketTimeoutException()
    monkeypatch.setattr("webex_obs.obs_socket._ReceiverSocket", lambda: socket)
    client = OBSWebSocket("localhost", 4455, "")
    with pytest.raises(WebSocketTimeoutException):
        client.connect()
    socket.connect.assert_called_once_with("ws://127.0.0.1:4455", timeout=3, http_no_proxy=["*"])
    socket.shutdown.assert_called_once()
    assert client.thread_recv is None


def test_established_receiver_keeps_polling_after_idle_timeout(monkeypatch):
    def timeout(_self):
        raise WebSocketTimeoutException()

    monkeypatch.setattr("websocket.WebSocket.recv", timeout)
    socket = _ReceiverSocket()
    with pytest.raises(WebSocketTimeoutException):
        socket.recv()
    socket.receiving_events = True
    assert socket.recv() == ""
