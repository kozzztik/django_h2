from django.apps import AppConfig

from django_h2 import runserver


class DjangoH2Config(AppConfig):
    name = 'django_h2'
    label = 'django_h2'
    verbose_name = 'Django HTTP2'

    def ready(self):
        runserver.patch()
