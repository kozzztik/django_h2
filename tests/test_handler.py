import asyncio
from unittest import mock

from django import urls
from django.http import JsonResponse
from django.test import override_settings
import pytest

from django_h2.gunicorn.app import DjangoGunicornApp
from tests.utils import WorkerThread


class UrlConf:  # pylint: disable=too-few-public-methods
    urlpatterns = [
        urls.re_path(r'^ping/$', lambda x: JsonResponse(dict(x.GET))),
    ]


@pytest.fixture(name="app")
def app_fixture():
    with mock.patch('sys.argv', ['path']):
        return DjangoGunicornApp()


@override_settings(ROOT_URLCONF=UrlConf)
def test_request_timeout_error(server_sock, app, request_exception_signal):
    with WorkerThread(server_sock, app) as thread:
        with mock.patch(
                'asyncio.timeouts.Timeout.__aenter__',
                side_effect=asyncio.TimeoutError) as timeout_mock:
            response = thread.make_request([
                (':authority', '127.0.0.1'),
                (':scheme', 'https'),
                (':method', 'GET'),
                (':path', '/ping/?foo=bar'),
            ])
    assert response.status_code == 504
    assert response.body == b''
    assert timeout_mock.called
    assert len(request_exception_signal) == 1
    assert isinstance(request_exception_signal[0]["exc"], asyncio.TimeoutError)


@override_settings(ROOT_URLCONF=UrlConf)
def test_request_exception(server_sock, app, request_exception_signal):
    with WorkerThread(server_sock, app) as thread:
        with mock.patch(
                'django_h2.handler.H2Handler.get_response',
                side_effect=ValueError()) as timeout_mock:
            response = thread.make_request([
                (':authority', '127.0.0.1'),
                (':scheme', 'https'),
                (':method', 'GET'),
                (':path', '/ping/?foo=bar'),
            ])
    assert response.status_code == 500
    assert response.body == b''
    assert timeout_mock.called
    assert len(request_exception_signal) == 1
    assert isinstance(request_exception_signal[0]["exc"], ValueError)
