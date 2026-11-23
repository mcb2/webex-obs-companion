"""Bounded transport for the pinned OBS WebSocket v5 client."""

import ipaddress
from threading import current_thread

from obswebsocket import obsws
from obswebsocket.core import RecvThread
from websocket import WebSocket, WebSocketTimeoutException


def loopback_address(host: str) -> str:
    """Use a numeric loopback address, including for locally forwarded ports."""
    address = ipaddress.ip_address("127.0.0.1" if host.casefold() == "localhost" else host)
    if not address.is_loopback:
        raise ValueError("OBS WebSocket requires a local loopback address")
    return f"[{address}]" if address.version == 6 else str(address)


class _ReceiverSocket(WebSocket):
    # After authentication, the receiver should keep polling through idle reads.
    receiving_events = False

    def recv(self):
        try:
            return super().recv()
        except WebSocketTimeoutException:
            if not self.receiving_events:
                raise
            return ""


class OBSWebSocket(obsws):
    """Keep socket I/O and the v5 handshake bounded separately from RPC waits."""

    def __init__(self, host: str, port: int, password: str, *, legacy: bool = False):
        super().__init__(host, port, password, legacy=legacy, timeout=3)

    def connect(self) -> None:
        url = f"ws://{loopback_address(self.host)}:{self.port}"
        self.ws = _ReceiverSocket()
        try:
            self.ws.connect(url, timeout=self.timeout, http_no_proxy=["*"])
            self._auth()
        except BaseException:
            self.ws.shutdown()
            raise
        self.ws.receiving_events = True
        self.thread_recv = RecvThread(self)
        self.thread_recv.daemon = True
        self.thread_recv.start()

    def disconnect(self) -> None:
        receiver = self.thread_recv
        if receiver is not None:
            receiver.running = False
        if self.ws is not None:
            self.ws.shutdown()
        if receiver is not None and receiver is not current_thread():
            receiver.join(timeout=self.timeout)
        self.thread_recv = None
