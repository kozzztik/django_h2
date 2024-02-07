import os
import ssl
from importlib.resources import files
from unittest import mock

import django
import pytest
from django import urls
from django.conf import ENVIRONMENT_VARIABLE
from django.http import HttpResponse, JsonResponse
from django.test import override_settings
from django.urls import get_script_prefix

from django_h2.gunicorn.app import DjangoGunicornApp
from django_h2.request import H2Request
from tests import empty_settings
from tests.utils import WorkerThread


def files_view(request):
    return JsonResponse({
        name: request.FILES[name].read().decode() for name in request.FILES
    })


class UrlConf:
    urlpatterns = [
        urls.re_path(r'^ping/$', lambda x: JsonResponse(dict(x.GET))),
        urls.re_path(
            r'^script_prefix/$', lambda x: HttpResponse(get_script_prefix())),
        urls.re_path(r'^headers/$', lambda x: JsonResponse(dict(x.headers))),
        urls.re_path(r'^meta/$', lambda x: JsonResponse(dict(x.META))),
        urls.re_path(r'^scheme/$', lambda x: HttpResponse(x.scheme)),
        urls.re_path(r'^post/$', lambda x: JsonResponse(dict(x.POST))),
        urls.re_path(r'^files/$', files_view),

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
        yield DjangoGunicornApp()


@pytest.fixture(name="thread")
def thread_fixture(server_sock, app):
    with WorkerThread(server_sock, app) as thread:
        yield thread


def test_root_path_environ(server_sock, app):
    os.environ['SCRIPT_NAME'] = '/root_path'
    try:
        with WorkerThread(server_sock, app) as thread:
            response = thread.make_request([
                (':authority', '127.0.0.1'),
                (':scheme', 'http'),
                (':method', 'GET'),
                (':path', '/root_path/ping/?foo=bar')]
            )
            assert response.status_code == 200
            assert response.body == b'{"foo": ["bar"]}'
            # without prefix works too
            response = thread.make_request([
                (':authority', '127.0.0.1'),
                (':scheme', 'http'),
                (':method', 'GET'),
                (':path', '/ping/?foo2=bar2')]
            )
            assert response.status_code == 200
            assert response.body == b'{"foo2": ["bar2"]}'
    finally:
        del os.environ['SCRIPT_NAME']


@override_settings(FORCE_SCRIPT_NAME='/root_path2/')
def test_root_path_force_setting(server_sock, app):
    with WorkerThread(server_sock, app) as thread:
        response = thread.make_request([
            (':authority', '127.0.0.1'),
            (':scheme', 'http'),
            (':method', 'GET'),
            (':path', '/script_prefix/')]
        )
    assert response.status_code == 200
    assert response.body == b"/root_path2/"


def test_headers_with_same_name(thread):
    response = thread.make_request([
        (':authority', '127.0.0.1'),
        (':scheme', 'http'),
        (':method', 'GET'),
        (':path', '/headers/'),
        ('FOO', 'bar1'),
        ('fOo', 'bar2')
    ])
    assert response.status_code == 200
    assert response.json()['foo'] == 'bar1,bar2'


def test_meta_special_headers(thread):
    response = thread.make_request([
        (':authority', '127.0.0.1'),
        (':scheme', 'http'),
        (':method', 'GET'),
        (':path', '/meta/'),
        ('FOO', 'bar1'),
        ('content-length', '42'),
        ('content-type', 'foobar')
    ])
    assert response.status_code == 200
    data = response.json()
    assert data['HTTP_FOO'] == 'bar1'
    assert data['CONTENT_LENGTH'] == '42'
    assert data['CONTENT_TYPE'] == 'foobar'


def test_scheme_http(thread):
    response = thread.make_request([
        (':authority', '127.0.0.1'),
        (':scheme', 'http'),
        (':method', 'GET'),
        (':path', '/scheme/'),
    ])
    assert response.status_code == 200
    assert response.body == b'http'


def test_scheme_https(django_config, server_sock):
    crt_file = str(files('django_h2').joinpath('default.crt'))
    with mock.patch('sys.argv', ['path', '--certfile', crt_file]):
        app = DjangoGunicornApp()

    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with WorkerThread(server_sock, app) as thread:
        thread.connect_ssl(context)
        response = thread.make_request([
            (':authority', '127.0.0.1'),
            (':scheme', 'http'),  # provide incorrect client data
            (':method', 'GET'),
            (':path', '/scheme/'),
        ])
    assert response.status_code == 200
    assert response.body == b'https'


def test_post_form_encoded(thread):
    response = thread.make_request([
        (':authority', '127.0.0.1'),
        (':scheme', 'http'),
        (':method', 'POST'),
        (':path', '/post/'),
        ('content-type', 'application/x-www-form-urlencoded')
    ], data=b'foo6=bar7&foo6=bar8')
    assert response.status_code == 200
    data = response.json()
    assert data == {'foo6': ['bar7', 'bar8']}


def test_files(thread):
    data = b"""--smth
Content-Disposition: form-data; name="uploadedfile"; filename="hello.o"
Content-Type: application/x-object

foobar
--smth
""".replace(b'\n', b'\r\n')
    response = thread.make_request([
        (':authority', '127.0.0.1'),
        (':scheme', 'http'),
        (':method', 'POST'),
        (':path', '/files/'),
        ('content-type', 'multipart/form-data; boundary=smth'),
        ('content-length', str(len(data)))
    ], data=data)
    assert response.status_code == 200
    data = response.json()
    assert data == {'uploadedfile': 'foobar'}


def test_set_post(django_config):
    request = H2Request(
        mock.MagicMock(),
        [(':method', 'GET'), (':path', '')],
        '')
    assert not request.POST
    request.POST = {'foo': 'bar'}
    assert request.POST['foo'] == 'bar'
