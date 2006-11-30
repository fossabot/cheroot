"""WSGI interface (see PEP 333)."""

import StringIO as _StringIO
import sys as _sys

import cherrypy as _cherrypy
from cherrypy import _cperror, wsgiserver
from cherrypy.lib import http as _http


class InternalRedirector(object):
    """WSGI middleware which handles cherrypy.InternalRedirect.
    
    When cherrypy.InternalRedirect is raised, this middleware traps it,
    rewrites the WSGI environ using the new path and query_string,
    and calls the next application again. Because the wsgi.input stream
    may have already been consumed by the next application, the redirected
    call will always be of HTTP method "GET", and therefore any params must
    be passed in the InternalRedirect object's query_string attribute.
    If you need something more complicated, make and raise your own
    exception and your own WSGI middleware to trap it. ;)
    
    It would be a bad idea to raise InternalRedirect after you've already
    yielded response content, although an enterprising soul could choose
    to abuse this.
    
    nextapp: the next application callable in the WSGI chain.
    
    recursive: if False (the default), each URL (path + qs) will be
    stored, and, if the same URL is requested again, RuntimeError will
    be raised. If 'recursive' is True, no such error will be raised.
    """
    
    def __init__(self, nextapp, recursive=False):
        self.nextapp = nextapp
        self.recursive = recursive
    
    def __call__(self, environ, start_response):
        redirections = []
        
        env = environ.copy()
        path = env.get('PATH_INFO', '')
        qs = env.get('QUERY_STRING', '')
        
        while True:
            try:
                for chunk in self.nextapp(env, start_response):
                    yield chunk
                break
            except _cherrypy.InternalRedirect, ir:
                if not self.recursive:
                    if ir.path in redirections:
                        raise RuntimeError("InternalRedirector visited the "
                                           "same URL twice: %r" % ir.path)
                    else:
                        # Add the *previous* path_info + qs to redirections.
                        if qs:
                            qs = "?" + qs
                        redirections.append(env.get('SCRIPT_NAME', '') + path + qs)
                
                # Munge environment and try again.
                env['REQUEST_METHOD'] = "GET"
                env['PATH_INFO'] = path = ir.path
                env['QUERY_STRING'] = qs = ir.query_string
                env['wsgi.input'] = _StringIO.StringIO()


class CPWSGIApp(object):
    """A WSGI application object for a CherryPy Application.
    
    pipeline: a list of (name, wsgiapp) pairs. Each 'wsgiapp' MUST be a
        constructor that takes an initial, positional 'nextapp' argument,
        plus optional keyword arguments, and returns a WSGI application
        (that takes environ and start_response arguments). The 'name' can
        be any you choose, and will correspond to keys in self.config.
    
    head: rather than nest all apps in the pipeline on each call, it's only
        done the first time, and the result is memoized into self.head. Set
        this to None again if you change self.pipeline after calling self.
    
    config: a dict whose keys match names listed in the pipeline. Each
        value is a further dict which will be passed to the corresponding
        named WSGI callable (from the pipeline) as keyword arguments.
    """
    
    pipeline = [('iredir', InternalRedirector)]
    head = None
    config = {}
    
    throws = (KeyboardInterrupt, SystemExit, _cherrypy.InternalRedirect)
    
    headerNames = {'HTTP_CGI_AUTHORIZATION': 'Authorization',
                   'CONTENT_LENGTH': 'Content-Length',
                   'CONTENT_TYPE': 'Content-Type',
                   'REMOTE_HOST': 'Remote-Host',
                   'REMOTE_ADDR': 'Remote-Addr',
                   }
    
    def translate_headers(self, environ):
        """Translate CGI-environ header names to HTTP header names."""
        for cgiName in environ:
            # We assume all incoming header keys are uppercase already.
            if cgiName in self.headerNames:
                yield self.headerNames[cgiName], environ[cgiName]
            elif cgiName[:5] == "HTTP_":
                # Hackish attempt at recovering original header names.
                translatedHeader = cgiName[5:].replace("_", "-")
                yield translatedHeader, environ[cgiName]
    
    def __init__(self, cpapp, pipeline=None):
        self.cpapp = cpapp
        self.pipeline = self.pipeline[:]
        if pipeline:
            self.pipeline.extend(pipeline)
        self.config = self.config.copy()
    
    def get_request(self, environ):
        env = environ.get
        
        local = _http.Host('', int(env('SERVER_PORT', 80)),
                           env('SERVER_NAME', ''))
        remote = _http.Host(env('REMOTE_ADDR', ''),
                            int(env('REMOTE_PORT', -1)),
                            env('REMOTE_HOST', ''))
        scheme = env('wsgi.url_scheme')
        sproto = env('ACTUAL_SERVER_PROTOCOL', "HTTP/1.1")
        request = _cherrypy.engine.request(local, remote, scheme, sproto)
        
        # LOGON_USER is served by IIS, and is the name of the
        # user after having been mapped to a local account.
        # Both IIS and Apache set REMOTE_USER, when possible.
        request.login = env('LOGON_USER') or env('REMOTE_USER') or None
        request.multithread = environ['wsgi.multithread']
        request.multiprocess = environ['wsgi.multiprocess']
        request.wsgi_environ = environ
        request.app = self.cpapp
        request.prev = env('cherrypy.request')
        environ['cherrypy.request'] = request
        return request
    
    def tail(self, environ, start_response):
        """WSGI application callable for the actual CherryPy application.
        
        You probably shouldn't call this; call self.__call__ instead,
        so that any WSGI middleware in self.pipeline can run first.
        """
        request = None
        try:
            request = self.get_request(environ)
            
            meth = environ['REQUEST_METHOD']
            path = environ.get('SCRIPT_NAME', '') + environ.get('PATH_INFO', '')
            qs = environ.get('QUERY_STRING', '')
            rproto = environ.get('SERVER_PROTOCOL')
            headers = self.translate_headers(environ)
            rfile = environ['wsgi.input']
            
            response = request.run(meth, path, qs, rproto, headers, rfile)
            s, h, b = response.status, response.header_list, response.body
            exc = None
        except self.throws, ex:
            self._close_req(request)
            raise ex
        except:
            if request and request.throw_errors:
                raise
            
            tb = _cperror.format_exc()
            _cherrypy.log(tb)
            if request and not request.show_tracebacks:
                tb = ""
            s, h, b = _cperror.bare_error(tb)
            
            exc = _sys.exc_info()
        
        try:
            start_response(s, h, exc)
            for chunk in b:
                # WSGI requires all data to be of type "str". This coercion should
                # not take any time at all if chunk is already of type "str".
                # If it's unicode, it could be a big performance hit (x ~500).
                if not isinstance(chunk, str):
                    chunk = chunk.encode("ISO-8859-1")
                yield chunk
            self._close_req(request)
        except self.throws, ex:
            self._close_req(request)
            raise ex
        except:
            _cherrypy.log(traceback=True)
            self._close_req(request)
            
            # CherryPy test suite expects bare_error body to be output,
            # so don't call start_response (which, according to PEP 333,
            # may raise its own error at that point).
            s, h, b = _cperror.bare_error()
            for chunk in b:
                if not isinstance(chunk, str):
                    chunk = chunk.encode("ISO-8859-1")
                yield chunk
    
    def _close_req(self, request):
        if hasattr(request, "close"):
            try:
                request.close()
            except:
                _cherrypy.log(traceback=True)
    
    def __call__(self, environ, start_response):
        head = self.head
        if head is None:
            # Create and nest the WSGI apps in our pipeline (in reverse order).
            head = self.tail
            for name, callable in self.pipeline[::-1]:
                conf = self.config.get(name, {})
                head = callable(head, **conf)
            self.head = head
        return head(environ, start_response)
    
    def namespace_handler(self, k, v):
        """Config handler for the 'wsgi' namespace."""
        if k == "pipeline":
            # Note this allows multiple 'wsgi.pipeline' config entries
            # (but each entry will be processed in a 'random' order).
            # It should also allow developers to set default middleware
            # in code (passed to self.__init__) that deployers can add to
            # (but not remove) via config.
            self.pipeline.extend(v)
        else:
            name, arg = k.split(".", 1)
            bucket = self.config.setdefault(name, {})
            bucket[arg] = v



#                            Server components                            #


class CPHTTPRequest(wsgiserver.HTTPRequest):
    
    def parse_request(self):
        mhs = _cherrypy.server.max_request_header_size
        if mhs > 0:
            self.rfile = _http.SizeCheckWrapper(self.rfile, mhs)
        
        try:
            wsgiserver.HTTPRequest.parse_request(self)
        except _http.MaxSizeExceeded:
            self.simple_response("413 Request Entity Too Large")
            _cherrypy.log(traceback=True)
    
    def decode_chunked(self):
        """Decode the 'chunked' transfer coding."""
        if isinstance(self.rfile, _http.SizeCheckWrapper):
            self.rfile = self.rfile.rfile
        mbs = _cherrypy.server.max_request_body_size
        if mbs > 0:
            self.rfile = _http.SizeCheckWrapper(self.rfile, mbs)
        try:
            return wsgiserver.HTTPRequest.decode_chunked(self)
        except _http.MaxSizeExceeded:
            self.simple_response("413 Request Entity Too Large")
            _cherrypy.log(traceback=True)
            return False


class CPHTTPConnection(wsgiserver.HTTPConnection):
    
    RequestHandlerClass = CPHTTPRequest


class CPWSGIServer(wsgiserver.CherryPyWSGIServer):
    
    """Wrapper for wsgiserver.CherryPyWSGIServer.
    
    wsgiserver has been designed to not reference CherryPy in any way,
    so that it can be used in other frameworks and applications. Therefore,
    we wrap it here, so we can set our own mount points from cherrypy.tree.
    
    """
    
    ConnectionClass = CPHTTPConnection
    
    def __init__(self):
        server = _cherrypy.server
        sockFile = server.socket_file
        if sockFile:
            bind_addr = sockFile
        else:
            bind_addr = (server.socket_host, server.socket_port)
        
        s = wsgiserver.CherryPyWSGIServer
        # We could just pass cherrypy.tree, but by passing tree.apps,
        # we get correct SCRIPT_NAMEs as early as possible.
        s.__init__(self, bind_addr, _cherrypy.tree.apps.items(),
                   server.thread_pool,
                   server.socket_host,
                   request_queue_size = server.socket_queue_size,
                   timeout = server.socket_timeout,
                   )
        self.protocol = server.protocol_version
        self.ssl_certificate = server.ssl_certificate
        self.ssl_private_key = server.ssl_private_key

