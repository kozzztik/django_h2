import os
import importlib

from gunicorn.app.base import Application
from gunicorn import config

import django
from django.conf import ENVIRONMENT_VARIABLE


class DjangoGunicornApp(Application):
    app_uri = None

    def init(self, parser, opts, args):
        if len(args) > 0:
            self.app_uri = args[0]

    def load_default_config(self):
        super().load_default_config()
        self.cfg.set("worker_class", "django_h2.gunicorn.worker.H2Worker")

    def load_config(self):
        super().load_config()
        self.logger = self.cfg.logger_class(self.cfg)
        # reopen files
        if 'GUNICORN_FD' in os.environ:
            self.logger.reopen_files()

        if self.app_uri is None:
            if self.cfg.django_settings is not None:
                self.app_uri = self.cfg.django_settings.strip()
            else:
                self.app_uri = os.environ[ENVIRONMENT_VARIABLE]

        if self.app_uri is not None:
            if ',' in self.app_uri:
                for path in self.app_uri.split(','):
                    try:
                        importlib.import_module(path.strip())
                    except ImportError:
                        continue
                    else:
                        self.app_uri = path.strip()
                        break
                else:
                    raise config.ConfigError("Django settings not available")

        if not self.app_uri:
            raise config.ConfigError("Django settings not configured")
        os.environ[ENVIRONMENT_VARIABLE] = self.app_uri
        self.logger.info("Using django settings %s", self.app_uri)
        if self.cfg.serve_static:
            self.logger.info("Serving django static.")

    def load(self):
        pass

    def wsgi(self):
        django.setup()


class DjangoSettings(config.Setting):
    name = "django_settings"
    section = "Config File"
    meta = "STRING"
    validator = config.validate_string
    default = None
    desc = """Django settings module import path."""


class ServeStatic(config.Setting):
    name = "serve_static"
    section = "Config File"
    cli = ["--serve_static"]
    action = "store_true"
    validator = config.validate_bool
    default = False
    desc = """Serve django static files"""


def run():
    """\
    The ``gunicorn`` command line runner for launching Gunicorn with
    Django applications.
    """
    DjangoGunicornApp("%(prog)s [OPTIONS] [APP_MODULE]").run()
