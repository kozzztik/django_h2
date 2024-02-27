import asyncio
import threading
from unittest import mock

import pytest
from django import urls
from django.http import HttpResponse
from django.test import override_settings
from h2 import events
from h2.connection import H2ConnectionStateMachine, ConnectionState

from django_h2.gunicorn.app import DjangoGunicornApp
from django_h2.sse.response import Event, SSEResponse
from tests.utils import WorkerThread, do_receive_response, read_events


# Fix H2 Client that not understands GoAway frame as graceful shutdown
# pylint: disable=protected-access
transitions = H2ConnectionStateMachine._transitions


def fix_transitions():
    orig = {
        k: v for k, v in transitions.items()
        if k[0] == ConnectionState.CLIENT_OPEN
    }
    for (_, event), value in orig.items():
        transitions[(ConnectionState.CLOSED, event)] = value


fix_transitions()  # so tmp variables will not be in global scope
ready_event = threading.Event()
waiter_event = threading.Event()


def blocked_view(_):
    ready_event.set()
    waiter_event.wait(10)
    return HttpResponse(b'success')


@pytest.fixture(name='ready')
def ready_fixture():
    ready_event.clear()
    return ready_event


@pytest.fixture(name='waiter')
def waiter_fixture():
    waiter_event.clear()
    return waiter_event


def fast_view(_):
    return HttpResponse(b'fast success')


async def events_source():
    for x in range(100):
        yield Event('some_name', 'some_data', x + 1)
        await asyncio.sleep(0.1)


class UrlConf:  # pylint: disable=too-few-public-methods
    urlpatterns = [
        urls.re_path(r'^blocked_view/$', blocked_view),
        urls.re_path(r'^fast_view/$', fast_view),
        urls.re_path(r'^sse/$', lambda x: SSEResponse(events_source())),
    ]


@pytest.fixture(name="app")
def app_fixture():
    with mock.patch(
            'sys.argv', ['path', '--max-requests', '1', '--threads', '2']):
        return DjangoGunicornApp()


@override_settings(ROOT_URLCONF=UrlConf)
def test_correctly_restarting_worker(app, server_sock, ready, waiter):
    with WorkerThread(server_sock, app) as thread:
        sock, conn = thread.connect()
        stream_id_1 = conn.get_next_available_stream_id()
        conn.send_headers(
            stream_id_1,
            [
                (':authority', '127.0.0.1'),
                (':scheme', 'http'),
                (':method', 'GET'),
                (':path', '/blocked_view/')
            ],
            end_stream=True
        )
        sock.sendall(conn.data_to_send())
        ready.wait(10)
        stream_id_2 = conn.get_next_available_stream_id()
        conn.send_headers(
            stream_id_2,
            [
                (':authority', '127.0.0.1'),
                (':scheme', 'http'),
                (':method', 'GET'),
                (':path', '/fast_view/')
            ],
            end_stream=True
        )
        sock.sendall(conn.data_to_send())
        headers, data = do_receive_response(sock, conn)
        assert headers == [
            (b':status', b'200'),
            (b'content-type', b'text/html; charset=utf-8')
        ]
        assert data == [b'fast success']
        waiter.set()
        headers, data = do_receive_response(sock, conn)
        final_data = sock.recv(65535)
        assert final_data == b''  # connection closed by remote side correctly
    assert headers == [
        (b':status', b'200'),
        (b'content-type', b'text/html; charset=utf-8')
    ]
    assert data == [b'success']


@override_settings(ROOT_URLCONF=UrlConf)
def test_restart_streaming(app, server_sock):
    with WorkerThread(server_sock, app) as thread:
        sock, conn = thread.connect()
        stream_id_1 = conn.get_next_available_stream_id()
        conn.send_headers(
            stream_id_1,
            [
                (':authority', '127.0.0.1'),
                (':scheme', 'http'),
                (':method', 'GET'),
                (':path', '/sse/')
            ],
            end_stream=True
        )
        sock.sendall(conn.data_to_send())
        headers, data = read_events(sock, conn, 1)
        assert headers == {
            b':status': b'200',
            b'cache-control': b'no-cache',
            b'content-type': b'text/event-stream',
            b'x-accel-buffering': b'no'
        }
        assert data == [b'event: some_name\ndata: some_data\nid: 1\n\n']
        stream_id_2 = conn.get_next_available_stream_id()
        conn.send_headers(
            stream_id_2,
            [
                (':authority', '127.0.0.1'),
                (':scheme', 'http'),
                (':method', 'GET'),
                (':path', '/fast_view/')
            ],
            end_stream=True
        )
        sock.sendall(conn.data_to_send())
        headers = {}
        response_data = []
        ended_streams = set()
        connection_terminated = False
        while True:
            data = sock.recv(65536 * 1024)
            if not data:
                # stop reading only on connection close
                break
            for event in conn.receive_data(data):
                if isinstance(event, events.DataReceived):
                    conn.acknowledge_received_data(
                        event.flow_controlled_length,
                        event.stream_id)
                    response_data.append(event.data)
                elif isinstance(event, events.StreamEnded):
                    ended_streams.add(event.stream_id)
                elif isinstance(event, events.ResponseReceived):
                    headers = event.headers
                elif isinstance(event, events.ConnectionTerminated):
                    connection_terminated = True
            sock.sendall(conn.data_to_send())
        assert headers == [
            (b':status', b'200'),
            (b'content-type', b'text/html; charset=utf-8')
        ]
        assert response_data == [b'fast success', b'']
        assert ended_streams == {stream_id_1, stream_id_2}
        assert connection_terminated is True
