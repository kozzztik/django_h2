import os

DEBUG = True
USE_TZ = False

INSTALLED_APPS = (
    'django_h2.DjangoH2Config',
)

SECRET_KEY = 'SECRET_KEY'

REDIS_HOST = os.environ.get("REDIS_HOST", '127.0.0.1')
REDIS_PORT = os.environ.get("REDIS_PORT", 6379)
REDIS_DB = os.environ.get("REDIS_DB", 8)
