import asyncio
import os
import socket
import ssl
import threading
from importlib.resources import files
import logging
from unittest import mock

import django
import pytest
from django import urls
from django.conf import ENVIRONMENT_VARIABLE
from django.http import HttpResponse
from django.test import override_settings
import h2.connection
import h2.config
import h2.events

from django_h2 import signals
from django_h2.gunicorn.app import DjangoGunicornApp
from django_h2.gunicorn.worker import H2Worker
from tests import empty_settings

logger = logging.getLogger(__name__)
logger.level = logging.DEBUG
logger.trace = logger.debug
logger.addHandler(logging.StreamHandler())


def ping_view(request):
    return HttpResponse(str(dict(request.GET.items())).encode("utf-8"))


class UrlConf:
    urlpatterns = [
        urls.re_path(r'^ping/$', ping_view),
    ]


@pytest.fixture(name="django_config")
def django_config_fixture():
    os.environ[ENVIRONMENT_VARIABLE] = empty_settings.__name__
    django.setup()
    with override_settings(ROOT_URLCONF=UrlConf):
        yield


class Response:
    status_code = None
    raw_headers: list[tuple[str, str]] = None
    headers: dict[str, str] = None
    body = b''

    def __getitem__(self, item):
        return self.headers[item]


class Worker(H2Worker):
    def __init__(self, server_socket, app, thread):
        self.thread = thread
        super().__init__(0, 0, [server_socket], app, 1, app.cfg, app.logger)

    def notify(self):
        self.thread.started.set()


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

    def make_request(self, headers, data=None, stream_id=1) -> Response:
        self._connect()
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


@pytest.fixture(name="server_sock")
def server_sock_fixture():
    sock_server = socket.socket()
    sock_server.bind(('127.0.0.1', 0))
    yield sock_server
    sock_server.close()


def test_worker_init(django_config, server_sock):
    with mock.patch('sys.argv', ['path']):
        app = DjangoGunicornApp()

    with WorkerThread(server_sock, app) as thread:
        response = thread.make_request([
            (':authority', '127.0.0.1'),
            (':scheme', 'http'),
            (':method', 'GET'),
            (':path', '/ping/?foo=bar')]
        )
    assert response.status_code == 200
    assert response.body == b"{'foo': 'bar'}"


def test_worker_ssl(django_config, server_sock):
    crt_file = str(files('django_h2').joinpath('default.crt'))
    with mock.patch('sys.argv', ['path', '--certfile', crt_file]):
        app = DjangoGunicornApp()

    with WorkerThread(server_sock, app) as thread:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        thread.connect_ssl(context)
        response = thread.make_request([
            (':authority', '127.0.0.1'),
            (':scheme', 'https'),
            (':method', 'GET'),
            (':path', '/ping/?foo2=bar3')]
        )
    assert response.status_code == 200
    assert response.body == b"{'foo2': 'bar3'}"


def test_worker_ssl_min_version(django_config, server_sock):
    """
    RFC 7540 Section 9.2: Implementations of HTTP/2 MUST use TLS version 1.2
    """
    crt_file = str(files('django_h2').joinpath('default.crt'))
    with mock.patch('sys.argv', ['path', '--certfile', crt_file]):
        app = DjangoGunicornApp()

    with WorkerThread(server_sock, app) as thread:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        with pytest.deprecated_call():
            context.maximum_version = ssl.TLSVersion.TLSv1_1
        with pytest.raises(ssl.SSLError) as e:
            thread.connect_ssl(context)
        assert 'no protocols available' in str(e)
        context.maximum_version = ssl.TLSVersion.TLSv1_2
        thread.connect_ssl(context)
        response = thread.make_request([
            (':authority', '127.0.0.1'),
            (':scheme', 'https'),
            (':method', 'GET'),
            (':path', '/ping/?foo4=bar5')]
        )
    assert response.status_code == 200
    assert response.body == b"{'foo4': 'bar5'}"


def test_config_post_request_exception(django_config, server_sock):
    with mock.patch('sys.argv', ['path']):
        app = DjangoGunicornApp()

    def post_request(sender, reqeust, env, resp):
        raise ValueError("foobar")
    app.cfg.set("post_request", post_request)
    with WorkerThread(server_sock, app) as thread:
        with mock.patch.object(app.logger, "exception") as logger_mock:
            response = thread.make_request([
                (':authority', '127.0.0.1'),
                (':scheme', 'http'),
                (':method', 'GET'),
                (':path', '/ping/?foo=bar')]
            )
    assert response.status_code == 200
    assert response.body == b"{'foo': 'bar'}"
    assert logger_mock.called
    assert logger_mock.call_args[0][0] == "Exception in post_request hook %s"
    assert isinstance(logger_mock.call_args[0][1], ValueError)
    assert logger_mock.call_args[0][1].args == ('foobar',)


def test_keyboard_interrupt(django_config, server_sock):
    with mock.patch('sys.argv', ['path']):
        app = DjangoGunicornApp()

    class InterruptedWorker(Worker):
        async def interrupt_task(self):
            raise KeyboardInterrupt()

        async def notify_task(self):
            self.loop.create_task(self.interrupt_task())
            await super().notify_task()

    class InterruptedThread(WorkerThread):
        worker_class = InterruptedWorker

    with InterruptedThread(server_sock, app) as thread:
        thread.join(5)
        assert thread.worker.loop.is_closed()


def test_exceptions_logging(django_config, server_sock):
    with mock.patch('sys.argv', ['path']):
        app = DjangoGunicornApp()

    def failing_receiver(*args, **kwargs):
        raise ValueError("foobar")

    signals.post_request.connect(failing_receiver)
    try:
        with mock.patch.object(app.logger, "exception") as logger_mock:
            with WorkerThread(server_sock, app) as thread:
                response = thread.make_request([
                    (':authority', '127.0.0.1'),
                    (':scheme', 'http'),
                    (':method', 'GET'),
                    (':path', '/ping/?foo=bar')]
                )
    finally:
        signals.post_request.disconnect(failing_receiver)
    assert response.status_code == 200
    assert response.body == b"{'foo': 'bar'}"
    assert logger_mock.called
    assert isinstance(logger_mock.call_args[0][0], ValueError)
    assert logger_mock.call_args[0][0].args == ('foobar',)


def test_failed_loading_django_with_reload(django_config, server_sock):
    with mock.patch('sys.argv', ['path', '--reload']):
        app = DjangoGunicornApp()

    with mock.patch.object(app.logger, "exception") as logger_mock:
        with mock.patch("django.setup", side_effect=ValueError('foobar')):
            with WorkerThread(server_sock, app) as thread:
                response = thread.make_request([
                    (':authority', '127.0.0.1'),
                    (':scheme', 'http'),
                    (':method', 'GET'),
                    (':path', '/ping/?foo=bar')]
                )
    assert response.status_code == 500
    assert b'raise effect' in response.body
    assert logger_mock.called
    assert isinstance(logger_mock.call_args[0][0], ValueError)
    assert logger_mock.call_args[0][0].args == ('foobar',)


def test_failed_loading_django_no_reload(django_config, server_sock):
    with mock.patch('sys.argv', ['path']):
        app = DjangoGunicornApp()

    with mock.patch.object(logging, "exception") as logger_mock:
        with mock.patch("django.setup", side_effect=ValueError('foobar')):
            with WorkerThread(server_sock, app) as thread:
                thread.join(1)
    assert logger_mock.called
    assert isinstance(logger_mock.call_args[0][0], ValueError)
    assert logger_mock.call_args[0][0].args == ('foobar',)


@override_settings(STATICFILES_DIRS=[files('django_h2')], STATIC_URL='/static/')
def test_serving_static(django_config, server_sock):
    with mock.patch('sys.argv', ['path', '--serve_static']):
        app = DjangoGunicornApp()

    with WorkerThread(server_sock, app) as thread:
        response = thread.make_request([
            (':authority', '127.0.0.1'),
            (':scheme', 'http'),
            (':method', 'GET'),
            (':path', '/static/default.crt')]
        )
        assert response.status_code == 200
        assert response['content-disposition'] == 'inline; filename="default.crt"'
        assert len(response.body) > 0
        # app still works
        response = thread.make_request([
            (':authority', '127.0.0.1'),
            (':scheme', 'http'),
            (':method', 'GET'),
            (':path', '/ping/?foo5=bar6')],
            stream_id=3
        )
        assert response.status_code == 200
        assert response.body == b"{'foo5': 'bar6'}"


@override_settings(STATICFILES_DIRS=[files('django_h2')], STATIC_URL='/static/')
def test_not_serving_static_by_default(django_config, server_sock):
    with mock.patch('sys.argv', ['path']):
        app = DjangoGunicornApp()

    with WorkerThread(server_sock, app) as thread:
        response = thread.make_request([
            (':authority', '127.0.0.1'),
            (':scheme', 'http'),
            (':method', 'GET'),
            (':path', '/static/default.crt')]
        )
    assert response.status_code == 404
