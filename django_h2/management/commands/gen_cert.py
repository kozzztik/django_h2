from django.core.management.base import BaseCommand

from django_h2 import gen_cert


class Command(BaseCommand):
    def handle(self, *args, **options):
        kwargs = {}
        for name in ('email_address', 'key_file', 'cert_file'):
            if name in options:
                kwargs[name] = options[name]
        gen_cert.save_cert_and_key(**kwargs)

    def add_arguments(self, parser):
        self.add_base_argument(
            parser,
            "--email_address",
            help="Email address of certificate issuer",
        )
        self.add_base_argument(
            parser,
            "--key_file",
            help="File name to store certificate private key",
        )
        self.add_base_argument(
            parser,
            "--cert_file",
            help="File name to store created self-signed certificate",
        )
