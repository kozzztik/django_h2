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
from h2.exceptions import ProtocolError, StreamClosedError

from django_h2.base_protocol import BaseH2Protocol, BaseStream
from django_h2.gunicorn.app import DjangoGunicornApp
from django_h2.signals import request_exception
from tests import empty_settings
from tests.utils import WorkerThread


def ping_view(request):
    return HttpResponse(str(dict(request.GET.items())).encode("utf-8"))


def cookie_view(request):
    response = HttpResponse()
    for key, value in request.GET.items():
        response.set_cookie(key, value)
    return response


class UrlConf:
    urlpatterns = [
        urls.re_path(r'^ping/$', ping_view),
        urls.re_path(r'^empty/$', lambda x: HttpResponse()),
        urls.re_path(r'^body/$', lambda x: HttpResponse(x.body)),
        urls.re_path(r'^cookie/$', cookie_view),
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
    return response_headers, response_data


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
        response_headers, response_data = do_receive_response(sock, conn)
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
        response_headers, response_data = do_receive_response(sock, conn)
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


def test_disconnect_under_flow_control(app, server_sock, post_request_signal):
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
    assert post_request_signal
    # flow control correctly closed
    assert post_request_signal[0]['sender']._flow_control_future is None


@pytest.mark.parametrize('exc', (ProtocolError, StreamClosedError))
def test_sending_protocol_error(
        app, server_sock, request_exception_signal, exc):
    with mock.patch('django_h2.protocol.Stream.close') as close_mock:
        with mock.patch(
                'h2.connection.H2Connection.send_data',
                side_effect=exc) as send_mock:
            with WorkerThread(server_sock, app) as thread:
                resp = thread.make_request([
                    (':authority', '127.0.0.1'),
                    (':scheme', 'http'),
                    (':method', 'GET'),
                    (':path', '/ping/?foo5=bar6')
                ])
    assert send_mock.called
    assert close_mock.called
    assert resp.status_code == 200
    assert resp.body == b''  # body is not send, but headers are ok


def test_empty_body_response(app, server_sock):
    """Check that empty body does not create data frames."""
    with WorkerThread(server_sock, app) as thread:
        sock, conn = thread.connect()
        stream_id = conn.get_next_available_stream_id()
        conn.send_headers(
            stream_id,
            [
                (':authority', '127.0.0.1'),
                (':scheme', 'http'),
                (':method', 'GET'),
                (':path', '/empty/')
            ],
            end_stream=True
        )
        sock.sendall(conn.data_to_send())
        response_headers, response_data = do_receive_response(sock, conn)
    assert response_headers == [
        (b':status', b'200'),
        (b'content-type', b'text/html; charset=utf-8')
    ]
    assert response_data == [b'']  # only one frame with "stream end" flag


def test_in_flow_too_large_body(app, server_sock):
    with mock.patch('django_h2.protocol.Stream.close') as close_mock:
        with override_settings(FILE_UPLOAD_MAX_MEMORY_SIZE=3):
            with WorkerThread(server_sock, app) as thread:
                sock, conn = thread.connect()
                stream_id = conn.get_next_available_stream_id()
                conn.send_headers(
                    stream_id,
                    [
                        (':authority', '127.0.0.1'),
                        (':scheme', 'http'),
                        (':method', 'GET'),
                        (':path', '/empty/')
                    ],
                    end_stream=False
                )
                wait_conn_processed(sock, conn)
                conn.send_data(stream_id, b'1111', end_stream=True)
                sock.sendall(conn.data_to_send())
                stream_reading = True
                initial_size = None
                while stream_reading:
                    data = sock.recv(65536 * 1024)
                    if not data:
                        break
                    data_events = conn.receive_data(data)
                    for event in data_events:
                        if isinstance(event, events.RemoteSettingsChanged):
                            initial_size = event.changed_settings[
                                SettingCodes.INITIAL_WINDOW_SIZE].new_value
                        elif isinstance(event, events.SettingsAcknowledged):
                            pass
                        else:
                            # no other events expected here, we brake
                            # protocol by sending more data than window expects
                            assert isinstance(
                                event, events.ConnectionTerminated)
                            stream_reading = False
    assert close_mock.called
    # we received
    assert initial_size == 3


def test_in_flow_too_large_body_stream_level(app, server_sock):
    with mock.patch('django_h2.protocol.Stream.close') as close_mock:
        with WorkerThread(server_sock, app) as thread:
            sock, conn = thread.connect()
            data = sock.recv(65536 * 1024)
            data_events = conn.receive_data(data)
            assert len(data_events) == 1
            assert isinstance(data_events[0], events.RemoteSettingsChanged)
            assert data_events[0].changed_settings[
                SettingCodes.INITIAL_WINDOW_SIZE].new_value > 65535
            with override_settings(FILE_UPLOAD_MAX_MEMORY_SIZE=3):
                stream_id = conn.get_next_available_stream_id()
                conn.send_headers(
                    stream_id,
                    [
                        (':authority', '127.0.0.1'),
                        (':scheme', 'http'),
                        (':method', 'GET'),
                        (':path', '/empty/')
                    ],
                    end_stream=False
                )
                conn.send_data(stream_id, b'1111', end_stream=True)
                sock.sendall(conn.data_to_send())
                stream_reading = True
                while stream_reading:
                    data = sock.recv(65536 * 1024)
                    if not data:
                        break
                    data_events = conn.receive_data(data)
                    for event in data_events:
                        if isinstance(event, events.SettingsAcknowledged):
                            pass
                        else:
                            # no other events expected here
                            # connection alive, but stream is reset
                            assert isinstance(event, events.StreamReset)
                            stream_reading = False
    assert close_mock.called


def test_in_flow_control_happy_path(server_sock, app):
    with override_settings(FILE_UPLOAD_MAX_MEMORY_SIZE=4):
        with WorkerThread(server_sock, app) as thread:
            sock, conn = thread.connect()
            data = sock.recv(65536 * 1024)
            data_events = conn.receive_data(data)
            # here can be also SettingsAck sometimes
            assert len(data_events) >= 1
            assert isinstance(data_events[0], events.RemoteSettingsChanged)
            assert data_events[0].changed_settings[
                       SettingCodes.INITIAL_WINDOW_SIZE].new_value == 4
            stream_id = conn.get_next_available_stream_id()
            conn.send_headers(
                stream_id,
                [
                    (':authority', '127.0.0.1'),
                    (':scheme', 'http'),
                    (':method', 'GET'),
                    (':path', '/body/')
                ],
                end_stream=False
            )
            data_to_send = b'1234'
            # send data flow
            while data_to_send:
                conn.send_data(
                    stream_id, data_to_send[:1],
                    end_stream=not bool(data_to_send[1:])
                )
                sock.sendall(conn.data_to_send())
                data_to_send = data_to_send[1:]
                if data:
                    data = sock.recv(65536 * 1024)
                    data_events = conn.receive_data(data)
                    for event in data_events:
                        if isinstance(event, events.SettingsAcknowledged):
                            pass
                        else:
                            assert isinstance(event, events.WindowUpdated)
                            # we update window only on connection level
                            assert event.stream_id == 0
                            assert event.delta == 1
            response_headers, response_data = do_receive_response(sock, conn)
    assert response_headers == [
        (b':status', b'200'),
        (b'content-type', b'text/html; charset=utf-8')
    ]
    assert response_data == [b'1234']


def test_cookie(server_sock, app):
    with WorkerThread(server_sock, app) as thread:
        response = thread.make_request([
            (':authority', '127.0.0.1'),
            (':scheme', 'http'),
            (':method', 'GET'),
            (':path', '/cookie/?foo=bar')
        ])
    assert response.status_code == 200
    assert response.body == b''
    assert response.headers == {
        ':status': '200',
        'content-type': 'text/html; charset=utf-8',
        'set-cookie': 'foo=bar; Path=/'
    }


def test_protocol_ping(server_sock, app):
    with WorkerThread(server_sock, app) as thread:
        sock, conn = thread.connect()
        data = sock.recv(65536 * 1024)
        data_events = conn.receive_data(data)
        assert len(data_events) == 1
        assert isinstance(data_events[0], events.RemoteSettingsChanged)
        conn.ping(b'foobar42')
        sock.sendall(conn.data_to_send())
        pong_event = None
        reading = True
        while reading:
            data = sock.recv(65536 * 1024)
            data_events = conn.receive_data(data)
            for event in data_events:
                if isinstance(event, events.SettingsAcknowledged):
                    pass
                else:
                    assert isinstance(event, events.PingAckReceived)
                    pong_event = event
                    reading = False
    assert pong_event.ping_data == b'foobar42'


def test_async_stream_calls():
    """ Calls on closed streams not raising errors """
    protocol = BaseH2Protocol()
    protocol.conn = mock.MagicMock()
    protocol.stream_complete(100)
    protocol.receive_data(b'',100)
    assert protocol.conn.reset_stream.called
    protocol.stream_reset(100)
    protocol.window_updated(100, 0)


def test_not_implemented():
    """Just cover stings of code and be sure that they are not implemented."""
    stream = BaseStream(mock.MagicMock(), 0, [])
    with pytest.raises(NotImplementedError):
        stream.event_stream_complete()
    with pytest.raises(NotImplementedError):
        stream.event_receive_data(b'')
