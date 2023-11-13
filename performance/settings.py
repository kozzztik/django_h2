"""
Django settings for performance testing project
"""

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = 'django-insecure-76%9@s#k$_uo)n(@$d%jjcpw85m+zn9!z@v983$9#r!q9qylyc'

DEBUG = False
ALLOWED_HOSTS = ['127.0.0.1']

INSTALLED_APPS = []
MIDDLEWARE = []
TEMPLATES = []
AUTH_PASSWORD_VALIDATORS = []

ROOT_URLCONF = 'performance.project.urls'

STATIC_URL = '/static/'  # TODO disable static

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
