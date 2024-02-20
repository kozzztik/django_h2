import io

from django.utils.functional import cached_property
from django.http import HttpRequest, QueryDict, parse_cookie
from django.utils.datastructures import CaseInsensitiveMapping

from django_h2.base_protocol import BaseStream


class HttpHeaders(CaseInsensitiveMapping):
    def __init__(self, headers_data):
        headers = {}
        for header, value in headers_data:
            name = header.replace("_", "-").lower()
            if name in headers:
                headers[name] += ',' + value
            else:
                headers[name] = value
        super().__init__(headers)

    def __getitem__(self, key):
        """Allow header lookup using underscores in place of hyphens."""
        return super().__getitem__(key.replace("_", "-"))

    def __setitem__(self, key, value):
        self._store[key.lower()] = (key, value)


class H2Request(HttpRequest):
    _body = None  # To disable inherited body legacy magic
    headers = None
    _post_parse_error = False
    _read_started = False
    resolver_match = None

    # pylint: disable=super-init-not-called
    def __init__(self, stream: BaseStream, headers, root_path):
        self.stream = stream
        self.headers = HttpHeaders(headers)
        # override scheme header with real data
        if stream.transport.get_extra_info('sslcontext'):
            self.headers[':scheme'] = 'https'
        else:
            self.headers[':scheme'] = 'http'
        self.script_name = root_path

        path, *rest = self.headers[':path'].split("?", 2)
        query_string = rest[0] if rest else ''
        if root_path and path.startswith(root_path):
            self.path_info = path[len(root_path):]
        else:
            self.path_info = path
        # The Django path is different from ASGI scope path args, it should
        # combine with script name.
        if root_path:
            self.path = f'{root_path.rstrip("/")}/{path.replace("/", "", 1)}'
        else:
            self.path = path
        # HTTP basics.
        self.method = self.headers[":method"].upper()
        # Ensure query string is encoded correctly.
        peer_sock = \
            stream.transport.get_extra_info('peername') or ('127.0.0.1', 0)
        local_sock = \
            stream.transport.get_extra_info('peername') or ("unknown", 0)
        self.META = {
            "SERVER_PROTOCOL": "HTTP2",
            "REQUEST_METHOD": self.method,
            "RAW_URI": self.headers[':path'],
            "QUERY_STRING": query_string,
            "SCRIPT_NAME": self.script_name,
            "PATH_INFO": self.path_info,
            "REMOTE_ADDR": peer_sock[0],
            "REMOTE_HOST": peer_sock[0],
            "REMOTE_PORT": peer_sock[1],
            "SERVER_NAME": local_sock[0],
            "SERVER_PORT": local_sock[1],
            # WSGI-expecting code will need these for a while
            "wsgi.multithread": True,
            "wsgi.multiprocess": True,
        }
        if ':authority' in self.headers:
            self.META["HTTP_HOST"] = self.headers[":authority"]
        # Headers go into META.
        for name, value in self.headers.items():
            if name == "content-length":
                corrected_name = "CONTENT_LENGTH"
            elif name == "content-type":
                corrected_name = "CONTENT_TYPE"
            else:
                corrected_name = f'HTTP_{name.upper().replace("-", "_")}'
            self.META[corrected_name] = value
        # Pull out request encoding, if provided.
        self._set_content_type_params(self.META)

    def stream_complete(self, body: io.BytesIO):
        body.seek(0)
        self._body = body.read()

    # Triggers that on base init is overriden, but it is not called
    # pylint: disable=method-hidden,invalid-name
    @cached_property
    def GET(self):
        return QueryDict(self.META["QUERY_STRING"])

    def _get_scheme(self):
        return self.headers.get(":scheme") or super()._get_scheme()

    def _get_post(self):
        if not hasattr(self, "_post"):
            self._load_post_and_files()
        return self._post

    def _set_post(self, post):
        self._post = post  # pylint: disable=attribute-defined-outside-init

    def _get_files(self):
        if not hasattr(self, "_files"):
            self._load_post_and_files()
        return self._files

    POST = property(_get_post, _set_post)
    FILES = property(_get_files)

    @cached_property
    def COOKIES(self):
        return parse_cookie(self.headers.get("cookie", ""))
