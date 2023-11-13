import os

import django.core.handlers.wsgi

os.environ.setdefault("DJANGO_SETTINGS_MODULE", 'performance.settings')

application = django.core.handlers.wsgi.WSGIHandler()
