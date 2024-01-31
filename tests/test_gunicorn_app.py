import os
import tempfile
from unittest import mock

from django.conf import ENVIRONMENT_VARIABLE
import pytest

from django_h2.gunicorn.app import DjangoGunicornApp, run
from django_h2.gunicorn.worker import H2Worker


@pytest.fixture(autouse=True)
def disable_default_config():
    with mock.patch(
            'gunicorn.app.base.get_default_config_file',
            return_value=None):
        yield


def test_django_settigns_config_args(capsys):
    with mock.patch('sys.argv', ['path', 'foobar1']):
        app = DjangoGunicornApp()
    assert app.app_uri == 'foobar1'
    assert os.environ[ENVIRONMENT_VARIABLE] == 'foobar1'

    # set by env variable
    os.environ[ENVIRONMENT_VARIABLE] = 'foobar2'
    with mock.patch('sys.argv', ['path']):
        app = DjangoGunicornApp()
    assert app.app_uri == 'foobar2'
    assert os.environ[ENVIRONMENT_VARIABLE] == 'foobar2'

    # set by config file
    with tempfile.TemporaryDirectory() as folder:
        path = os.path.join(folder, 'gunicorn.conf.py')
        with open(path, 'w', encoding='utf-8') as f:
            f.write('django_settings="foobar3"\n')
            f.close()
        with mock.patch('sys.argv', ['path', f'--config={path}']):
            app = DjangoGunicornApp()
    assert app.app_uri == 'foobar3'
    assert os.environ[ENVIRONMENT_VARIABLE] == 'foobar3'

    # if not set, error
    os.environ[ENVIRONMENT_VARIABLE] = ''
    with mock.patch('sys.argv', ['path']):
        with mock.patch('sys.exit', mock.MagicMock()) as exit_mock:
            DjangoGunicornApp()
    assert exit_mock.called
    errs = capsys.readouterr().err
    assert errs.split('\n')[-2] == 'Error: Django settings not configured'


def test_django_settings_list(capsys):
    """ Check that files list converted to actual existing file name """
    path = __name__
    with mock.patch('sys.argv', ['path', f'foobar,{path}']):
        app = DjangoGunicornApp()
    assert app.app_uri == path
    assert os.environ[ENVIRONMENT_VARIABLE] == path

    # order is not important
    os.environ[ENVIRONMENT_VARIABLE] = ''
    with mock.patch('sys.argv', ['path', f'{path},barfoo']):
        app = DjangoGunicornApp()
    assert app.app_uri == path
    assert os.environ[ENVIRONMENT_VARIABLE] == path

    # if none is available
    os.environ[ENVIRONMENT_VARIABLE] = ''
    with mock.patch('sys.argv', ['path', 'bar,foo']):
        with mock.patch('sys.exit', mock.MagicMock()) as exit_mock:
            DjangoGunicornApp()
    assert exit_mock.called
    errs = capsys.readouterr().err
    assert errs.split('\n')[-2] == 'Error: Django settings not available'


def test_django_serving_static():
    os.environ[ENVIRONMENT_VARIABLE] = 'foobar'

    # default is false
    with mock.patch('sys.argv', ['path']):
        app = DjangoGunicornApp()
    assert app.cfg.serve_static is False

    # set by args
    with mock.patch('sys.argv', ['path', '--serve_static']):
        app = DjangoGunicornApp()
    assert app.cfg.serve_static is True

    # set by config
    with tempfile.TemporaryDirectory() as folder:
        path = os.path.join(folder, 'gunicorn.conf.py')
        with open(path, 'w', encoding='utf-8') as f:
            f.write('serve_static=True\n')
            f.close()
        with mock.patch('sys.argv', ['path', f'--config={path}']):
            app = DjangoGunicornApp()
    assert app.cfg.serve_static is True


def test_run_with_preload():
    os.environ[ENVIRONMENT_VARIABLE] = 'foobar'
    # by default preload not works
    with mock.patch('sys.argv', ['path']):
        with mock.patch('django.setup') as setup_mock:
            with mock.patch('gunicorn.arbiter.Arbiter.run') as run_mock:
                run()
    assert not setup_mock.called
    assert run_mock.called

    # check preload
    with mock.patch('sys.argv', ['path', '--preload']):
        with mock.patch('django.setup') as setup_mock:
            with mock.patch('gunicorn.arbiter.Arbiter.run') as run_mock:
                run()
    assert setup_mock.called
    assert run_mock.called


def test_default_worker():
    with mock.patch('sys.argv', ['path', 'foobar1']):
        app = DjangoGunicornApp()
    assert app.cfg.worker_class is H2Worker
