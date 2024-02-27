import asyncio
import threading
import socket
import ssl
import logging
import json

import h2.connection
import h2.config
import h2.events

from django_h2.gunicorn.worker import H2Worker
from django_h2 import signals


logger = logging.getLogger(__name__)
logger.level = logging.DEBUG
logger.trace = logger.debug
logger.addHandler(logging.StreamHandler())


class Response:
    status_code = None
    raw_headers: list[tuple[str, str]] = None
    headers: dict[str, str] = None
    body = b''

    def __getitem__(self, item):
        return self.headers[item]

    def json(self):
        return json.loads(self.body)


class BaseWorkerThread(threading.Thread):
    _sock: socket.socket | None = None
    _conn: h2.connection.H2Connection | None = None
    exception = None

    def __init__(self):
        self._stopper = threading.Event()
        self.started = threading.Event()
        super().__init__()

    async def stopping_task(self, loop):
        while not self._stopper.is_set():
            await asyncio.sleep(0.1)
        loop.stop()

    def stop(self):
        self._stopper.set()

    def _on_start(self, handler, **kwargs):
        self.started.set()
        loop = asyncio.get_event_loop()
        loop.create_task(self.stopping_task(loop))

    def _internal_run(self):
        raise NotImplementedError()

    def run(self):
        signals.server_started.connect(self._on_start)
        try:
            self._internal_run()
        except BaseException as e:
            if not self.started.is_set():
                self.started.set()
            self.exception = e
            logging.exception(e)
        finally:
            signals.server_started.disconnect(self._on_start)

    def __enter__(self):
        self.start()
        self.started.wait(5)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()
        if self._sock:
            self._sock.close()
            self._sock = None
            self._conn = None
        self.join(5)

    def get_server_addr(self):
        raise NotImplementedError()

    def connect(self) -> tuple[socket.socket, h2.connection.H2Connection]:
        if self._sock:
            return self._sock, self._conn
        self._sock = socket.create_connection(
            self.get_server_addr(), timeout=10)
        self._sock.settimeout(10)
        config = h2.config.H2Configuration()
        self._conn = h2.connection.H2Connection(config=config)
        self._conn.initiate_connection()
        self._sock.sendall(self._conn.data_to_send())
        return self._sock, self._conn

    def connect_ssl(self, context: ssl.SSLContext):
        if self._sock:
            return
        self._sock = socket.create_connection(
            self.get_server_addr(), timeout=10)
        try:
            self._sock = context.wrap_socket(
                self._sock, server_hostname='127.0.0.1')
        except ssl.SSLError:
            self._sock.close()
            self._sock = None
            raise
        self._sock.settimeout(10)
        config = h2.config.H2Configuration()
        self._conn = h2.connection.H2Connection(config=config)
        self._conn.initiate_connection()
        self._sock.sendall(self._conn.data_to_send())

    def make_request(self, headers, data=None, stream_id=None) -> Response:
        self.connect()
        if stream_id is None:
            stream_id = self._conn.get_next_available_stream_id()
        self._conn.send_headers(stream_id,  headers, end_stream=data is None)
        self._sock.sendall(self._conn.data_to_send())
        if data is not None:
            self._conn.send_data(stream_id, data, end_stream=True)
            self._sock.sendall(self._conn.data_to_send())
        response_stream_ended = False
        resp = Response()
        while not response_stream_ended:
            # read raw data from the socket
            data = self._sock.recv(65536 * 1024)
            if not data:
                break

            # feed raw data into h2, and process resulting events
            events = self._conn.receive_data(data)
            for event in events:
                if isinstance(event, h2.events.DataReceived):
                    # update flow control so the server doesn't starve us
                    self._conn.acknowledge_received_data(
                        event.flow_controlled_length,
                        event.stream_id)
                    # more response body data received
                    resp.body += event.data
                elif isinstance(
                        event, (h2.events.StreamEnded, h2.events.StreamReset)):
                    # response body completed, let's exit the loop
                    response_stream_ended = True
                    break
                elif isinstance(event, h2.events.ResponseReceived):
                    resp.raw_headers = event.headers
                    resp.headers = {
                        k.decode('utf-8'): v.decode('utf-8')
                        for k, v in event.headers
                    }
                    resp.status_code = int(resp.headers.get(':status'))
            # send any pending data to the server
            self._sock.sendall(self._conn.data_to_send())
        return resp


class WorkerThread(BaseWorkerThread):
    worker_class = H2Worker

    def __init__(self, server_socket, app):
        self.worker = self.worker_class(
            0, 0, [server_socket], app, 1, app.cfg, app.logger)
        super().__init__()

    def _internal_run(self):
        self.worker.load_wsgi()
        self.worker.protocol_logger = logger
        self.worker.run()

    def get_server_addr(self):
        return self.worker.sockets[0].getsockname()

    def _on_start(self, handler, **kwargs):
        if handler is self.worker.handler:
            super()._on_start(handler, **kwargs)


def do_receive_response(sock, conn):
    """Same as make response but returns data by frames"""
    response_headers = {}
    response_data = []
    stream_reading = True
    while stream_reading:
        data = sock.recv(65536 * 1024)
        if not data:
            break
        data_events = conn.receive_data(data)
        for event in data_events:
            if isinstance(event, h2.events.DataReceived):
                conn.acknowledge_received_data(
                    event.flow_controlled_length,
                    event.stream_id)
                response_data.append(event.data)
            elif isinstance(event, h2.events.StreamEnded):
                stream_reading = False
            elif isinstance(event, h2.events.ResponseReceived):
                response_headers = event.headers
        sock.sendall(conn.data_to_send())
    return response_headers, response_data


def read_events(sock, conn, count):
    response_headers = {}
    response_data = []
    stream_reading = True
    while stream_reading:
        data = sock.recv(65536 * 1024)
        if not data:
            break
        data_events = conn.receive_data(data)
        for event in data_events:
            if isinstance(event, h2.events.DataReceived):
                conn.acknowledge_received_data(
                    event.flow_controlled_length,
                    event.stream_id)
                response_data.append(event.data)
                if len(response_data) >= count:
                    stream_reading = False
            elif isinstance(event, h2.events.StreamEnded):
                stream_reading = False
            elif isinstance(event, h2.events.ResponseReceived):
                response_headers = dict(event.headers)
        sock.sendall(conn.data_to_send())
    return response_headers, response_data


def read_headers(sock, conn):
    response_headers = {}
    stream_reading = True
    while stream_reading:
        data = sock.recv(65536 * 1024)
        if not data:
            break
        data_events = conn.receive_data(data)
        for event in data_events:
            if isinstance(event, h2.events.ResponseReceived):
                response_headers = dict(event.headers)
                stream_reading = False
            else:
                assert isinstance(event, (
                    h2.events.SettingsAcknowledged,
                    h2.events.RemoteSettingsChanged)), "Not expected event"
        sock.sendall(conn.data_to_send())
    return response_headers
