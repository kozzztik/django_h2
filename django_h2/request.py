import asyncio

from datetime import datetime
import io

from django.utils.functional import cached_property
from django.http import HttpRequest, QueryDict, parse_cookie

from django_h2.base_protocol import H2Protocol


class H2Request(HttpRequest):
    _body = None  # To disable inherited body legacy magic
    h2_task: asyncio.Task = None
    h2_bytes_send = 0

    def __init__(
            self, protocol: H2Protocol, stream_id: int, headers, root_path):
        self.start_time = datetime.now()
        self.h2_protocol = protocol
        self.h2_stream_id = stream_id
        self.scope = scope = {}
        for h, v in headers:
            if h in scope:
                scope[h] += "," + v
            else:
                scope[h] = v
        self._post_parse_error = False
        self._read_started = False
        self.resolver_match = None
        self.script_name = root_path
        path, *rest = scope[':path'].split("?", 2)
        query_string = rest[0] if rest else ''
        if root_path and path.startswith(root_path):
            self.path_info = path[len(root_path):]
        else:
            self.path_info = path
        # The Django path is different from ASGI scope path args, it should
        # combine with script name.
        if root_path:
            self.path = "%s/%s" % (
                root_path.rstrip("/"),
                path.replace("/", "", 1),
            )
        else:
            self.path = path
        # HTTP basics.
        self.method = self.scope[":method"].upper()
        # Ensure query string is encoded correctly.
        peer_sock = \
            protocol.transport.get_extra_info('peername') or ('127.0.0.1', 0)
        local_sock = \
            protocol.transport.get_extra_info('peername') or ("unknown", 0)
        self.META = {
            "HTTP_HOST": scope.get(":authority"),
            "REQUEST_METHOD": self.method,
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
        # Headers go into META.
        for name, value in headers:
            if name == "content-length":
                corrected_name = "CONTENT_LENGTH"
            elif name == "content-type":
                corrected_name = "CONTENT_TYPE"
            else:
                corrected_name = "HTTP_%s" % name.upper().replace("-", "_")
            # HTTP/2 say only ASCII chars are allowed in headers, but decode
            # latin1 just in case.
            if corrected_name in self.META:
                value = self.META[corrected_name] + "," + value
            self.META[corrected_name] = value
        # Pull out request encoding, if provided.
        self._set_content_type_params(self.META)
        # Directly assign the body file to be our stream.
        self._stream = io.BytesIO()

    def stream_complete(self):
        self._stream.seek(0)
        self._body = self._stream.read()
        self._stream.seek(0)

    @cached_property
    def GET(self):
        return QueryDict(self.META["QUERY_STRING"])

    def _get_scheme(self):
        return self.scope.get(":scheme") or super()._get_scheme()

    def _get_post(self):
        if not hasattr(self, "_post"):
            self._load_post_and_files()
        return self._post

    def _set_post(self, post):
        self._post = post

    def _get_files(self):
        if not hasattr(self, "_files"):
            self._load_post_and_files()
        return self._files

    POST = property(_get_post, _set_post)
    FILES = property(_get_files)

    @cached_property
    def COOKIES(self):
        return parse_cookie(self.META.get("HTTP_COOKIE", ""))
