# -*- coding: iso-8859-1 -*-
"""
    MoinMoin - New slimmed down WSGI Request.

    @copyright: 2008-2008 MoinMoin:FlorianKrupicka
    @license: GNU GPL, see COPYING for details.
"""

import re, warnings
from io import StringIO

from werkzeug.wrappers import Request as RequestBase
from werkzeug.wrappers import Response
from werkzeug.wrappers import ResponseStream
from werkzeug.datastructures import EnvironHeaders, Headers, HeaderSet
from werkzeug.urls import url_encode, url_join
from werkzeug.test import create_environ, Client
from werkzeug.utils import cached_property

from MoinMoin import config

from MoinMoin import log
logging = log.getLogger(__name__)

class Href:
    """Implements a callable that constructs URLs with the given base. The
    function can be called with any number of positional and keyword
    arguments which than are used to assemble the URL.  Works with URLs
    and posix paths.
    Positional arguments are appended as individual segments to
    the path of the URL:
    >>> href = Href('/foo')
    >>> href('bar', 23)
    '/foo/bar/23'
    >>> href('foo', bar=23)
    '/foo/foo?bar=23'
    If any of the arguments (positional or keyword) evaluates to `None` it
    will be skipped.  If no keyword arguments are given the last argument
    can be a :class:`dict` or :class:`MultiDict` (or any other dict subclass),
    otherwise the keyword arguments are used for the query parameters, cutting
    off the first trailing underscore of the parameter name:
    >>> href(is_=42)
    '/foo?is=42'
    >>> href({'foo': 'bar'})
    '/foo?foo=bar'
    Combining of both methods is not allowed:
    >>> href({'foo': 'bar'}, bar=42)
    Traceback (most recent call last):
      ...
    TypeError: keyword arguments and query-dicts can't be combined
    Accessing attributes on the href object creates a new href object with
    the attribute name as prefix:
    >>> bar_href = href.bar
    >>> bar_href("blub")
    '/foo/bar/blub'
    If `sort` is set to `True` the items are sorted by `key` or the default
    sorting algorithm:
    >>> href = Href("/", sort=True)
    >>> href(a=1, b=2, c=3)
    '/?a=1&b=2&c=3'
    .. deprecated:: 2.0
        Will be removed in Werkzeug 2.1. Use :mod:`werkzeug.routing`
        instead.
    .. versionadded:: 0.5
        `sort` and `key` were added.
    """

    def __init__(  # type: ignore
        self, base="./", charset="utf-8", sort=False, key=None
    ):
        warnings.warn(
            "'Href' is deprecated and will be removed in Werkzeug 2.1."
            " Use 'werkzeug.routing' instead.",
            DeprecationWarning,
            stacklevel=2,
        )

        if not base:
            base = "./"
        self.base = base
        self.charset = charset
        self.sort = sort
        self.key = key

    def __getattr__(self, name):  # type: ignore
        if name[:2] == "__":
            raise AttributeError(name)
        base = self.base
        if base[-1:] != "/":
            base += "/"
        return Href(url_join(base, name), self.charset, self.sort, self.key)

    def __call__(self, *path, **query):  # type: ignore
        if path and isinstance(path[-1], dict):
            if query:
                raise TypeError("keyword arguments and query-dicts can't be combined")
            query, path = path[-1], path[:-1]
        elif query:
            query = {k[:-1] if k.endswith("_") else k: v for k, v in query.items()}
        path = "/".join(
            [
                x
                for x in path
                if x is not None
            ]
        ).lstrip("/")
        rv = self.base
        if path:
            if not rv.endswith("/"):
                rv += "/"
            rv = url_join(rv, f"./{path}")
        if query:
            rv += "?" + _to_str(
                url_encode(query, self.charset, sort=self.sort, key=self.key), "ascii"
            )
        return rv

class MoinMoinFinish(Exception):
    """ Raised to jump directly to end of run() function, where finish is called """

class ModifiedResponseStreamMixin(object):
    """
    to avoid .stream attributes name collision when we mix together Request
    and Response, we use "out_stream" instead of "stream" in the original
    ResponseStreamMixin
    """
    @cached_property
    def out_stream(self):
        """The response iterable as write-only stream."""
        return ResponseStream(self)

class ResponseBase(Response, ModifiedResponseStreamMixin):
    """
    similar to werkzeug.Response, but with ModifiedResponseStreamMixin
    """

class Request(ResponseBase, RequestBase):
    """ A full featured Request/Response object.

    To better distinguish incoming and outgoing data/headers,
    incoming versions are prefixed with 'in_' in contrast to
    original Werkzeug implementation.
    """
    charset = config.charset
    encoding_errors = 'replace'
    default_mimetype = 'text/html'
    given_config = None # if None, load wiki config from disk

    # get rid of some inherited descriptors
    headers = None

    def __init__(self, environ, populate_request=True, shallow=False):
        ResponseBase.__init__(self)
        RequestBase.__init__(self, environ, populate_request, shallow)
        self.href = Href(self.script_root or '/', self.charset)
        self.abs_href = Href(self.url_root, self.charset)
        self.headers = Headers([('Content-Type', 'text/html')])
        self.response = []
        self.status_code = 200

    # Note: we inherit a .stream attribute from RequestBase and this needs
    # to refer to the input stream because inherited functionality of werkzeug
    # base classes will access it as .stream.
    # The output stream is .out_stream (see above).
    # TODO keep request and response separate, don't mix them together

    @cached_property
    def in_headers(self):
        return EnvironHeaders(self.environ)


class TestRequest(Request):
    """ Request with customized `environ` for test purposes. """
    def __init__(self, path="/", query_string=None, method='GET',
                 content_type=None, content_length=0, form_data=None,
                 environ_overrides=None):
        """
        For parameter reference see the documentation of the werkzeug
        package, especially the functions `url_encode` and `create_environ`.
        """
        input_stream = None

        if form_data is not None:
            form_data = url_encode(form_data)
            content_type = 'application/x-www-form-urlencoded'
            content_length = len(form_data)
            input_stream = StringIO(form_data)
        environ = create_environ(path=path, query_string=query_string,
                                 method=method, input_stream=input_stream,
                                 content_type=content_type,
                                 content_length=content_length)

        environ['HTTP_USER_AGENT'] = 'MoinMoin/TestRequest'
        # must have reverse lookup or tests will be extremely slow:
        environ['REMOTE_ADDR'] = '127.0.0.1'

        if environ_overrides:
            environ.update(environ_overrides)

        super(TestRequest, self).__init__(environ)

def evaluate_request(request):
    """ Evaluate a request and returns a tuple of application iterator,
    status code and list of headers. This method is meant for testing
    purposes.
    """
    output = []
    headers_set = []
    def start_response(status, headers, exc_info=None):
        headers_set[:] = [status, headers]
        return output.append
    result = request(request.environ, start_response)

    # any output via (WSGI-deprecated) write-callable?
    if output:
        result = output
    return (result, headers_set[0], headers_set[1])

