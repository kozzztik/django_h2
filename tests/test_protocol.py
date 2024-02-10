import os
import threading
from unittest import mock

import django
import pytest
from django import urls
from django.conf import ENVIRONMENT_VARIABLE
from django.http import HttpResponse
from django.test import override_settings
from h2 import events
from h2.settings import SettingCodes

from django_h2.base_protocol import BaseH2Protocol
from django_h2.gunicorn.app import DjangoGunicornApp
from django_h2.signals import request_exception
from tests import empty_settings
from tests.utils import WorkerThread


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


@pytest.fixture(name="app")
def app_fixture(django_config):
    with mock.patch('sys.argv', ['path']):
        return DjangoGunicornApp()


@pytest.fixture(name="request_exception_signal")
def request_exception_signal_fixture():
    signal_calls = []

    def receiver(**kwargs):
        signal_calls.append(kwargs)
    request_exception.connect(receiver)
    try:
        yield signal_calls
    finally:
        request_exception.disconnect(receiver)


def test_flow_control_global(app, server_sock):
    with WorkerThread(server_sock, app) as thread:
        sock, conn = thread.connect()
        conn.update_settings({SettingCodes.INITIAL_WINDOW_SIZE: 2})
        sock.sendall(conn.data_to_send())
        stream_id = conn.get_next_available_stream_id()
        conn.send_headers(
            stream_id,
            [
                (':authority', '127.0.0.1'),
                (':scheme', 'http'),
                (':method', 'GET'),
                (':path', '/ping/?foo5=bar6')
            ],
            end_stream=True
        )
        sock.sendall(conn.data_to_send())
        response_headers = {}
        response_data = []
        stream_reading = True
        while stream_reading:
            data = sock.recv(65536 * 1024)
            if not data:
                break
            data_events = conn.receive_data(data)
            for event in data_events:
                if isinstance(event, events.DataReceived):
                    conn.acknowledge_received_data(
                        event.flow_controlled_length,
                        event.stream_id)
                    response_data.append(event.data)
                elif isinstance(event, events.StreamEnded):
                    stream_reading = False
                elif isinstance(event, events.ResponseReceived):
                    response_headers = event.headers
            sock.sendall(conn.data_to_send())
    assert response_headers == [
        (b':status', b'200'),
        (b'content-type', b'text/html; charset=utf-8')
    ]
    # Response data arrived in parts of 2 bytes
    assert response_data == [
        b"{'", b'fo', b'o5', b"':", b" '", b'ba', b'r6', b"'}"
    ]


def test_flow_control_per_stream(app, server_sock):
    with WorkerThread(server_sock, app) as thread:
        sock, conn = thread.connect()
        conn.update_settings({SettingCodes.INITIAL_WINDOW_SIZE: 2})
        sock.sendall(conn.data_to_send())
        stream_id = conn.get_next_available_stream_id()
        conn.send_headers(
            stream_id,
            [
                (':authority', '127.0.0.1'),
                (':scheme', 'http'),
                (':method', 'GET'),
                (':path', '/ping/?foo5=bar6')
            ],
            end_stream=True
        )
        conn.increment_flow_control_window(1, stream_id)
        sock.sendall(conn.data_to_send())
        response_headers = {}
        response_data = []
        stream_reading = True
        while stream_reading:
            data = sock.recv(65536 * 1024)
            if not data:
                break
            data_events = conn.receive_data(data)
            for event in data_events:
                if isinstance(event, events.DataReceived):
                    # if not response_data:
                    #     from h2.connection import WindowUpdateFrame
                    #     frame = WindowUpdateFrame(stream_id, 3)
                    #     sock.sendall(frame.serialize())
                    conn.acknowledge_received_data(
                        event.flow_controlled_length,
                        event.stream_id)
                    response_data.append(event.data)
                elif isinstance(event, events.StreamEnded):
                    stream_reading = False
                elif isinstance(event, events.ResponseReceived):
                    response_headers = event.headers
            sock.sendall(conn.data_to_send())
    assert response_headers == [
        (b':status', b'200'),
        (b'content-type', b'text/html; charset=utf-8')
    ]
    # Response data arrived in parts of 3 bytes
    assert response_data == [b"{'f", b'oo5', b"': ", b"'ba", b"r6'", b'}']


def wait_conn_processed(sock, conn):
    """ wait worker to process headers and create stream """
    f = threading.Event()

    def on_init(*args, **kwargs):
        f.set()

    with mock.patch('django_h2.request.H2Request.__init__', on_init):
        sock.sendall(conn.data_to_send())
        f.wait()


def test_connection_lost(app, server_sock, request_exception_signal):
    with WorkerThread(server_sock, app) as thread:
        sock, conn = thread.connect()
        conn.send_headers(
            conn.get_next_available_stream_id(),
            [
                (':authority', '127.0.0.1'),
                (':scheme', 'http'),
                (':method', 'GET'),
                (':path', '/ping/?foo5=bar6')
            ],
            end_stream=False
        )
        wait_conn_processed(sock, conn)
        sock.close()
    # check that stream was closed
    assert len(request_exception_signal) == 1
    assert 'exc' in request_exception_signal[0]


def test_protocol_error(app, server_sock, request_exception_signal):
    with mock.patch('django_h2.protocol.Stream.close') as close_mock:
        with WorkerThread(server_sock, app) as thread:
            sock, conn = thread.connect()
            conn.send_headers(
                conn.get_next_available_stream_id(),
                [
                    (':authority', '127.0.0.1'),
                    (':scheme', 'http'),
                    (':method', 'GET'),
                    (':path', '/ping/?foo5=bar6')
                ],
                end_stream=False
            )
            wait_conn_processed(sock, conn)
            sock.sendall(b'\x00'*10)  # break protocol
            sock.recv(1024)
    # check that stream was closed. As request is not received yet exception is
    # not reported
    assert close_mock.called
    assert request_exception_signal == []


def test_connection_terminated(app, server_sock, request_exception_signal):
    with mock.patch('django_h2.protocol.Stream.close') as close_mock:
        with WorkerThread(server_sock, app) as thread:
            sock, conn = thread.connect()
            conn.send_headers(
                conn.get_next_available_stream_id(),
                [
                    (':authority', '127.0.0.1'),
                    (':scheme', 'http'),
                    (':method', 'GET'),
                    (':path', '/ping/?foo5=bar6')
                ],
                end_stream=False
            )
            wait_conn_processed(sock, conn)
            conn.close_connection()
            sock.sendall(conn.data_to_send())
            sock.recv(1024)
    # check that stream was closed. As request is not received yet exception is
    # not reported
    assert close_mock.called
    assert request_exception_signal == []


def test_stream_reset(app, server_sock, request_exception_signal):
    with mock.patch('django_h2.protocol.Stream.close') as close_mock:
        with WorkerThread(server_sock, app) as thread:
            sock, conn = thread.connect()
            stream_id = conn.get_next_available_stream_id()
            conn.send_headers(
                stream_id,
                [
                    (':authority', '127.0.0.1'),
                    (':scheme', 'http'),
                    (':method', 'GET'),
                    (':path', '/ping/?foo5=bar6')
                ],
                end_stream=False
            )
            wait_conn_processed(sock, conn)
            conn.reset_stream(stream_id)
            sock.sendall(conn.data_to_send())
            sock.recv(1024)
    # check that stream was closed. As request is not received yet exception is
    # not reported
    assert close_mock.called
    assert request_exception_signal == []


def test_disconnect_under_flow_control(
        app, server_sock, request_exception_signal):
    with WorkerThread(server_sock, app) as thread:
        sock, conn = thread.connect()
        conn.update_settings({SettingCodes.INITIAL_WINDOW_SIZE: 2})
        sock.sendall(conn.data_to_send())
        stream_id = conn.get_next_available_stream_id()
        conn.send_headers(
            stream_id,
            [
                (':authority', '127.0.0.1'),
                (':scheme', 'http'),
                (':method', 'GET'),
                (':path', '/ping/?foo5=bar6')
            ],
            end_stream=True
        )
        sock.sendall(conn.data_to_send())
        response_headers = {}
        response_data = []
        stream_reading = True
        while stream_reading:
            data = sock.recv(65536 * 1024)
            if not data:
                break
            data_events = conn.receive_data(data)
            for event in data_events:
                if isinstance(event, events.DataReceived):
                    response_data.append(event.data)
                    if len(response_data) == 2:
                        stream_reading = False
                    else:
                        conn.acknowledge_received_data(
                            event.flow_controlled_length,
                            event.stream_id)
                elif isinstance(event, events.StreamEnded):
                    stream_reading = False
                elif isinstance(event, events.ResponseReceived):
                    response_headers = event.headers
            sock.sendall(conn.data_to_send())
    assert response_headers == [
        (b':status', b'200'),
        (b'content-type', b'text/html; charset=utf-8')
    ]
    # Response data arrived in parts of 3 bytes
    assert response_data == [b"{'", b'fo']
    assert request_exception_signal
    # flow control correctly closed
    assert request_exception_signal[0]['sender']._flow_control_future is None


def test_async_stream_calls():
    """ Calls on closed streams not raising errors """
    protocol = BaseH2Protocol()
    protocol.conn = mock.MagicMock()
    protocol.stream_complete(100)
    protocol.receive_data(b'',100)
    assert protocol.conn.reset_stream.called
    protocol.stream_reset(100)
    protocol.window_updated(100, 0)
