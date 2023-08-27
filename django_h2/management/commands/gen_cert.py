from django.core.management.base import BaseCommand

from django_h2 import gen_cert


class Command(BaseCommand):
    def handle(self, *args, **options):
        gen_cert.cert_gen()
