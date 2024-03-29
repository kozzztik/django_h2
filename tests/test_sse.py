import asyncio
from contextlib import aclosing
from unittest import mock

import pytest
from django import urls
from django.test import override_settings
from django.test.client import AsyncClient
from django.core.signals import request_finished

from django_h2.gunicorn.app import DjangoGunicornApp
from django_h2.sse.response import SSEResponse, Event
from tests.utils import WorkerThread, read_events


async def single_event():
    yield Event('foo', 'bar', None)


async def events_source():
    for x in range(100):
        yield Event('some_name', 'some_data', x + 1)
        await asyncio.sleep(0.1)

a_context = mock.MagicMock()


async def context_source():
    # pylint: disable=not-async-context-manager
    async with a_context:
        for x in range(100):
            yield Event('some_name', 'some_data', x + 1)
            await asyncio.sleep(0.1)


class UrlConf:  # pylint: disable=too-few-public-methods
    urlpatterns = [
        urls.re_path(r'^single/$', lambda x: SSEResponse(single_event())),
        urls.re_path(r'^sse/$', lambda x: SSEResponse(
            events_source(), headers={'foo': 'bar'})),
        urls.re_path(r'^context/$', lambda x: SSEResponse(context_source())),
    ]


@pytest.fixture(name="app")
def app_fixture():
    with mock.patch('sys.argv', ['path']):
        with override_settings(ROOT_URLCONF=UrlConf):
            yield DjangoGunicornApp()


@pytest.fixture(name="thread")
def thread_fixture(server_sock, app):
    with WorkerThread(server_sock, app) as thread:
        yield thread


@pytest.fixture(name="request_finished_signal")
def request_finished_signal_fixture():
    signal_calls = []

    def receiver(**kwargs):
        signal_calls.append(kwargs)
    request_finished.connect(receiver)
    try:
        yield signal_calls
    finally:
        request_finished.disconnect(receiver)


def test_single_event(thread):
    response = thread.make_request([
                (':authority', '127.0.0.1'),
                (':scheme', 'http'),
                (':method', 'GET'),
                (':path', '/single/')
    ])
    assert response.headers == {
        ':status': '200',
        'cache-control': 'no-cache',
        'content-type': 'text/event-stream',
        'x-accel-buffering': 'no'
    }
    assert response.body == b'event: foo\ndata: bar\n\n'


def test_sse_streaming(thread):
    sock, conn = thread.connect()
    stream_id = conn.get_next_available_stream_id()
    conn.send_headers(
        stream_id,
        [
            (':authority', '127.0.0.1'),
            (':scheme', 'http'),
            (':method', 'GET'),
            (':path', '/sse/')
        ],
        end_stream=True
    )
    response_headers, response_data = read_events(sock, conn, 2)
    assert response_headers == {
        b':status': b'200',
        b'cache-control': b'no-cache',
        b'content-type': b'text/event-stream',
        b'foo': b'bar',
        b'x-accel-buffering': b'no'
    }
    assert response_data == [
        b'event: some_name\ndata: some_data\nid: 1\n\n',
        b'event: some_name\ndata: some_data\nid: 2\n\n'
    ]


def test_sse_context_closing(
        server_sock, app, post_request_signal, request_finished_signal):
    # check need to be done after thread close
    with WorkerThread(server_sock, app) as thread:
        sock, conn = thread.connect()
        stream_id = conn.get_next_available_stream_id()
        conn.send_headers(
            stream_id,
            [
                (':authority', '127.0.0.1'),
                (':scheme', 'http'),
                (':method', 'GET'),
                (':path', '/context/')
            ],
            end_stream=True
        )
        response_headers, response_data = read_events(sock, conn, 2)
    assert response_headers == {
        b':status': b'200',
        b'cache-control': b'no-cache',
        b'content-type': b'text/event-stream',
        b'x-accel-buffering': b'no'
    }
    assert response_data == [
        b'event: some_name\ndata: some_data\nid: 1\n\n',
        b'event: some_name\ndata: some_data\nid: 2\n\n'
    ]
    assert a_context.__aexit__.called
    assert len(post_request_signal) == 1
    assert len(request_finished_signal) == 1


@pytest.mark.asyncio
@override_settings(ROOT_URLCONF=UrlConf)
async def test_django_client():
    client = AsyncClient()
    response = await client.get('/sse/')
    assert response.status_code == 200
    async with aclosing(aiter(response)) as content:
        data = await anext(content)
        assert data == b'event: some_name\ndata: some_data\nid: 1\n\n'
        data = await anext(content)
        assert data == b'event: some_name\ndata: some_data\nid: 2\n\n'
