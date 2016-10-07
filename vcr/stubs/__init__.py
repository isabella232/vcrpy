'''Stubs for patching HTTP and HTTPS requests'''

import logging
import os
import six
from six.moves.http_client import (
    HTTPConnection,
    HTTPSConnection,
    HTTPResponse,
)
from six import BytesIO
from vcr.request import Request
from vcr.errors import CannotOverwriteExistingCassetteException
from . import compat

log = logging.getLogger(__name__)

# This is only done once for performance reasons.
# This could be moved down to init if we need to support proxy changes at runtime.
proxied_protocols = set()
for proxy_key in ('http_proxy', 'https_proxy'):
    if os.environ.get(proxy_key) or os.environ.get(proxy_key.upper()):
        proxied_protocols.add(proxy_key.split('_')[0])


class VCRFakeSocket(object):
    """
    A socket that doesn't do anything!
    Used when playing back casssettes, when there
    is no actual open socket.
    """

    def close(self):
        pass

    def settimeout(self, *args, **kwargs):
        pass

    def fileno(self):
        """
        This is kinda crappy.  requests will watch
        this descriptor and make sure it's not closed.
        Return file descriptor 0 since that's stdin.
        """
        return 0  # wonder how bad this is....


def transform_proxy_headers(header_list):
    """Return a new dictionary, coercing proxy-specific headers into their non-proxied form.

    This is needed for the recorded headers to match across proxied and non-proxied runs.

    Specifically:
      * proxy-connection is renamed to connection
      * proxy-authorization is removed
    """
    filtered_headers = {}
    for key, values in header_list.items():
        if key.lower() == 'proxy-connection':
            filtered_headers[key.split('-')[1]] = values
        elif key.lower() != 'proxy-authorization':
            filtered_headers[key] = values

    return filtered_headers


def parse_headers(header_list):
    """
    Convert headers from our serialized dict with lists for keys to a
    HTTPMessage
    """
    header_string = b""
    for key, values in transform_proxy_headers(header_list).items():
        for v in values:
            header_string += \
                key.encode('utf-8') + b":" + v.encode('utf-8') + b"\r\n"
    return compat.get_httpmessage(header_string)


def serialize_headers(response):
    out = {}
    for key, values in compat.get_headers(response.msg):
        out.setdefault(key, [])
        out[key].extend(values)
    return transform_proxy_headers(out)


class VCRHTTPResponse(HTTPResponse):
    """
    Stub reponse class that gets returned instead of a HTTPResponse
    """
    def __init__(self, recorded_response):
        self.recorded_response = recorded_response
        self.reason = recorded_response['status']['message']
        self.status = self.code = recorded_response['status']['code']
        self.version = None
        self._content = BytesIO(self.recorded_response['body']['string'])
        self._closed = False

        headers = self.recorded_response['headers']
        # Since we are loading a response that has already been serialized, our
        # response is no longer chunked.  That means we don't want any
        # libraries trying to process a chunked response.  By removing the
        # transfer-encoding: chunked header, this should cause the downstream
        # libraries to process this as a non-chunked response.
        te_key = [h for h in headers.keys() if h.upper() == 'TRANSFER-ENCODING']
        if te_key:
            del headers[te_key[0]]
        self.headers = self.msg = parse_headers(headers)

        self.length = compat.get_header(self.msg, 'content-length') or None

    @property
    def closed(self):
        # in python3, I can't change the value of self.closed.  So I'
        # twiddling self._closed and using this property to shadow the real
        # self.closed from the superclas
        return self._closed

    def read(self, *args, **kwargs):
        return self._content.read(*args, **kwargs)

    def readline(self, *args, **kwargs):
        return self._content.readline(*args, **kwargs)

    def close(self):
        self._closed = True
        return True

    def getcode(self):
        return self.status

    def isclosed(self):
        return self.closed

    def info(self):
        return parse_headers(self.recorded_response['headers'])

    def getheaders(self):
        message = parse_headers(self.recorded_response['headers'])
        return list(compat.get_header_items(message))

    def getheader(self, header, default=None):
        values = [v for (k, v) in self.getheaders() if k.lower() == header.lower()]

        if values:
            return ', '.join(values)
        else:
            return default


class VCRConnection(object):
    # A reference to the cassette that's currently being patched in
    cassette = None

    def _port_postfix(self):
        """
        Returns empty string for the default port and ':port' otherwise
        """
        default_port = {'https': 443, 'http': 80}[self._protocol]

        if self._proxied:
            port = self._proxied_port
        else:
            port = self.real_connection.port

        return ':{0}'.format(port) if port != default_port else ''

    def _uri(self, url):
        """Returns request absolute URI"""

        if self._proxied and not self._proxied_host:
            if self._protocol == 'http':
                # We're not running with connect tunneling, so the url is already absolute.
                return url
            else:
                raise AssertionError('we should be proxied, but the client never called set_tunnel')

        uri = "{0}://{1}{2}{3}".format(
            self._protocol,
            self._proxied_host if self._proxied else self.real_connection.host,
            self._port_postfix(),
            url,
        )
        return uri

    def _url(self, uri):
        """Returns request selector url from absolute URI"""
        prefix = "{0}://{1}{2}".format(
            self._protocol,
            self.real_connection.host,
            self._port_postfix(),
        )
        return uri.replace(prefix, '', 1)

    def request(self, method, url, body=None, headers=None):
        '''Persist the request metadata in self._vcr_request'''
        self._vcr_request = Request(
            method=method,
            uri=self._uri(url),
            body=body,
            headers=transform_proxy_headers(headers or {})
        )
        log.debug('Got {0}'.format(self._vcr_request))

        # Note: The request may not actually be finished at this point, so
        # I'm not sending the actual request until getresponse().  This
        # allows me to compare the entire length of the response to see if it
        # exists in the cassette.

    def putrequest(self, method, url, *args, **kwargs):
        """
        httplib gives you more than one way to do it.  This is a way
        to start building up a request.  Usually followed by a bunch
        of putheader() calls.
        """
        self._vcr_request = Request(
            method=method,
            uri=self._uri(url),
            body="",
            headers={}
        )
        log.debug('Got {0}'.format(self._vcr_request))

    def putheader(self, header, *values):
        self._vcr_request.headers.update(transform_proxy_headers({header: values}))

    def send(self, data):
        '''
        This method is called after request(), to add additional data to the
        body of the request.  So if that happens, let's just append the data
        onto the most recent request in the cassette.
        '''
        self._vcr_request.body = self._vcr_request.body + data \
            if self._vcr_request.body else data

    def close(self):
        # Note: the real connection will only close if it's open, so
        # no need to check that here.
        self.real_connection.close()

    def endheaders(self, message_body=None):
        """
        Normally, this would actually send the request to the server.
        We are not sending the request until getting the response,
        so bypass this part and just append the message body, if any.
        """
        if message_body is not None:
            self._vcr_request.body = message_body

    def getresponse(self, _=False, **kwargs):
        '''Retrieve the response'''
        # Check to see if the cassette has a response for this request. If so,
        # then return it
        if self.cassette.can_play_response_for(self._vcr_request):
            log.info(
                "Playing response for {0} from cassette".format(
                    self._vcr_request
                )
            )
            response = self.cassette.play_response(self._vcr_request)
            return VCRHTTPResponse(response)
        else:
            if self.cassette.write_protected and self.cassette.filter_request(
                self._vcr_request
            ):
                raise CannotOverwriteExistingCassetteException(
                    "No match for the request (%r) was found. "
                    "Can't overwrite existing cassette (%r) in "
                    "your current record mode (%r)."
                    % (self._vcr_request, self.cassette._path,
                       self.cassette.record_mode)
                )

            # Otherwise, we should send the request, then get the response
            # and return it.

            log.info(
                "{0} not in cassette, sending to real server".format(
                    self._vcr_request
                )
            )
            # This is imported here to avoid circular import.
            # TODO(@IvanMalison): Refactor to allow normal import.
            from vcr.patch import force_reset
            with force_reset():
                self.real_connection.request(
                    method=self._vcr_request.method,
                    url=self._url(self._vcr_request.uri),
                    body=self._vcr_request.body,
                    headers=self._vcr_request.headers,
                )

            # get the response
            response = self.real_connection.getresponse()

            if self._proxied:
                self.auto_open = 0

            # put the response into the cassette
            response = {
                'status': {
                    'code': response.status,
                    'message': response.reason
                },
                'headers': serialize_headers(response),
                'body': {'string': response.read()},
            }
            self.cassette.append(self._vcr_request, response)
        return VCRHTTPResponse(response)

    def set_debuglevel(self, *args, **kwargs):
        self.real_connection.set_debuglevel(*args, **kwargs)

    def connect(self, *args, **kwargs):
        """
        httplib2 uses this.  Connects to the server I'm assuming.

        Only pass to the baseclass if we don't have a recorded response
        and are not write-protected.
        """

        if hasattr(self, '_vcr_request') and \
                self.cassette.can_play_response_for(self._vcr_request):
            # We already have a response we are going to play, don't
            # actually connect
            return

        if self.cassette.write_protected:
            # Cassette is write-protected, don't actually connect
            return

        return self.real_connection.connect(*args, **kwargs)

    @property
    def sock(self):
        if self.real_connection.sock:
            return self.real_connection.sock
        elif self._proxied:
            # urllib3 depends on sock being None to know when to call set_tunnel; see
            # https://github.com/shazow/urllib3/blob/dfd582c13ceb0287a71ccff1c742424c58ca2105/urllib3/connectionpool.py#L586
            return None

        return VCRFakeSocket()

    @sock.setter
    def sock(self, value):
        if self.real_connection.sock:
            self.real_connection.sock = value

    def __init__(self, *args, **kwargs):
        if six.PY3:
            kwargs.pop('strict', None)  # apparently this is gone in py3

        # need to temporarily reset here because the real connection
        # inherits from the thing that we are mocking out.  Take out
        # the reset if you want to see what I mean :)
        from vcr.patch import force_reset
        with force_reset():
            self.real_connection = self._baseclass(*args, **kwargs)

        self._proxied = self._protocol in proxied_protocols

        # Set after a call to set_tunnel, which sets up https proxying.
        # Will remain None if proxying http (since then they're specified in the url).
        self._proxied_host = None
        self._proxied_port = None

    def __setattr__(self, name, value):
        """
        We need to define this because any attributes that are set on the
        VCRConnection need to be propogated to the real connection.

        For example, urllib3 will set certain attributes on the connection,
        such as 'ssl_version'. These attributes need to get set on the real
        connection to have the correct and expected behavior.

        TODO: Separately setting the attribute on the two instances is not
        ideal. We should switch to a proxying implementation.
        """
        try:
            setattr(self.real_connection, name, value)
        except AttributeError:
            # raised if real_connection has not been set yet, such as when
            # we're setting the real_connection itself for the first time
            pass

        super(VCRConnection, self).__setattr__(name, value)

    def set_tunnel(self, host, port=None, headers=None):
        """Set up connect tunneling for https proxying."""

        # We won't read the host and port unless self._proxied is set accordingly.
        assert self._proxied, 'set_tunnel was called without the https_proxy env var being set'

        self._proxied_host = host
        self._proxied_port = port or {'https': 443, 'http': 80}[self._protocol]

        self.real_connection.set_tunnel(host, port, headers)


class VCRHTTPConnection(VCRConnection):
    '''A Mocked class for HTTP requests'''
    _baseclass = HTTPConnection
    _protocol = 'http'


class VCRHTTPSConnection(VCRConnection):
    '''A Mocked class for HTTPS requests'''
    _baseclass = HTTPSConnection
    _protocol = 'https'
    is_verified = True
