import time
from unittest import mock

import pytest
from django import urls
from django.conf import settings
from django.http import HttpResponse
from django.test import override_settings

from django_h2.gunicorn.app import DjangoGunicornApp
from django_h2.sse.response import SSEResponse, Event

from django_h2.sse.redis_server import RedisServer
from django_h2.sse.channel import Channel
from tests.utils import WorkerThread, read_events, read_headers


server = RedisServer(
    settings.REDIS_HOST, settings.REDIS_PORT, settings.REDIS_DB)


class GeneralChannel(Channel):
    name_template = 'general'


general_channel = GeneralChannel()


class UserChannel(Channel):
    name_template = 'user_{0}'


def user_events_view(request):
    channel = UserChannel(request.user.pk)
    if request.POST:
        listeners = server.publish_message(
            channel, Event(request.POST["name"], request.POST["data"])
        )
        return HttpResponse(str(listeners).encode("utf-8"))
    return SSEResponse(server.events_source(request, channel))


class UrlConf:  # pylint: disable=too-few-public-methods
    urlpatterns = [
        urls.re_path(r'^general/$', server.as_view(general_channel)),
        urls.re_path(r'^user/$', user_events_view),
    ]


@pytest.fixture(name="thread")
def thread_fixture(server_sock):
    with mock.patch('sys.argv', ['path']):
        with override_settings(ROOT_URLCONF=UrlConf):
            with WorkerThread(server_sock, DjangoGunicornApp()) as thread:
                yield thread


def test_events_streaming(thread):
    sock, conn = thread.connect()
    stream_id = conn.get_next_available_stream_id()
    conn.send_headers(
        stream_id,
        [
            (':authority', '127.0.0.1'),
            (':scheme', 'http'),
            (':method', 'GET'),
            (':path', '/general/')
        ],
        end_stream=True
    )
    headers = read_headers(sock, conn)
    assert headers == {
        b':status': b'200',
        b'cache-control': b'no-cache',
        b'content-type': b'text/event-stream',
        b'x-accel-buffering': b'no'
    }
    # wait for client connection to be established
    time.sleep(0.1)
    listeners = server.publish_message(general_channel, Event("foo", "bar", 1))
    assert listeners == 1
    headers, response_data = read_events(sock, conn, 1)
    assert response_data == [
        b'event: foo\ndata: bar\nid: 1\n\n',
    ]
