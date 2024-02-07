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


logger = logging.getLogger(__name__)
logger.level = logging.DEBUG
logger.trace = logger.debug
logger.addHandler(logging.StreamHandler())


class Worker(H2Worker):
    def __init__(self, server_socket, app, thread):
        self.thread = thread
        super().__init__(0, 0, [server_socket], app, 1, app.cfg, app.logger)

    def notify(self):
        self.thread.started.set()


class Response:
    status_code = None
    raw_headers: list[tuple[str, str]] = None
    headers: dict[str, str] = None
    body = b''

    def __getitem__(self, item):
        return self.headers[item]

    def json(self):
        return json.loads(self.body)


class WorkerThread(threading.Thread):
    _sock: socket.socket | None = None
    _conn: h2.connection.H2Connection | None = None
    exception = None
    worker_class = Worker

    def __init__(self, server_socket, app):
        self.worker = self.worker_class(server_socket, app, self)
        self._stopper = threading.Event()
        self.started = threading.Event()
        super().__init__()

    def run(self):
        try:
            self.worker.load_wsgi()
            self.worker.loop.create_task(self.stopping_task())
            self.worker.server.logger = logger
            self.worker.run()
        except BaseException as e:
            self.exception = e
            logging.exception(e)

    async def stopping_task(self):
        while not self._stopper.is_set():
            await asyncio.sleep(0.1)
        self.worker.loop.stop()

    def stop(self):
        self._stopper.set()

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

    def _connect(self):
        if self._sock:
            return
        self._sock = socket.create_connection(
            self.worker.sockets[0].getsockname(), timeout=10)
        self._sock.settimeout(10)
        config = h2.config.H2Configuration()
        self._conn = h2.connection.H2Connection(config=config)
        self._conn.initiate_connection()
        self._sock.sendall(self._conn.data_to_send())

    def connect_ssl(self, context: ssl.SSLContext):
        if self._sock:
            return
        self._sock = socket.create_connection(
            self.worker.sockets[0].getsockname(), timeout=10)
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
        self._connect()
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
                elif isinstance(event, h2.events.StreamEnded):
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
