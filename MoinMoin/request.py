# -*- coding: iso-8859-1 -*-
"""
    MoinMoin - Data associated with a single Request

    @copyright: 2001-2003 by J�rgen Hermann <jh@web.de>
    @copyright: 2003-2004 by Thomas Waldmann
    @license: GNU GPL, see COPYING for details.
"""

import os, time, sys, cgi, StringIO

from MoinMoin import config, wikiutil, user, error
from MoinMoin.util import MoinMoinNoFooter, IsWin9x
import MoinMoin.error


# Timing ---------------------------------------------------------------

class Clock:
    """ Helper class for code profiling
        we do not use time.clock() as this does not work across threads
    """

    def __init__(self):
        self.timings = {'total': time.time()}

    def start(self, timer):
        self.timings[timer] = time.time() - self.timings.get(timer, 0)

    def stop(self, timer):
        self.timings[timer] = time.time() - self.timings[timer]

    def value(self, timer):
        return "%.3f" % (self.timings[timer],)

    def dump(self):
        outlist = []
        for timing in self.timings.items():
            outlist.append("%s = %.3fs" % timing)
        outlist.sort()
        return outlist


# Utilities

def cgiMetaVariable(header, scheme='http'):
    """ Return CGI meta variable for header name
    
    e.g 'User-Agent' -> 'HTTP_USER_AGENT'    
    See http://www.faqs.org/rfcs/rfc3875.html section 4.1.18
    """
    var = '%s_%s' % (scheme, header)
    return var.upper().replace('-', '_')
    

# Request Base ----------------------------------------------------------

class RequestBase(object):
    """ A collection for all data associated with ONE request. """

    # Header set to force misbehaved proxies and browsers to keep their
    # hands off a page
    # Details: http://support.microsoft.com/support/kb/articles/Q234/0/67.ASP
    nocache = [
        "Pragma: no-cache",
        "Cache-Control: no-cache",
        "Expires: -1",
    ]

    # Defaults (used by sub classes)
    http_accept_language = 'en'
    server_name = 'localhost'
    server_port = '80'

    # Extra headers we support. Both standalone and twisted store
    # headers as lowercase.
    moin_location = 'x-moin-location'
    proxy_host = 'x-forwarded-host'
    
    def __init__(self, properties={}):
        # Decode values collected by sub classes
        self.path_info = self.decodePagename(self.path_info)

        self.failed = 0
        self._available_actions = None
        self._known_actions = None

        # Pages meta data that we collect in one request
        self.pages = {}
              
        self.sent_headers = 0
        self.user_headers = []
        self.cacheable = 0 # may this output get cached by http proxies/caches?
        self.page = None
        self._dicts = None
        
        # Fix dircaching problems on Windows 9x
        if IsWin9x():
            import dircache
            dircache.reset()

        # Check for dumb proxy requests
        # TODO relying on request_uri will not work on all servers, especially
        # not on external non-Apache servers
        self.forbidden = False
        if self.request_uri.startswith('http://'):
            self.makeForbidden()

        # Init
        else:
            self.writestack = []
            self.clock = Clock()
            # order is important here!
            self._load_multi_cfg()
            
            # Set decode charsets.  Input from the user is always in
            # config.charset, which is the page charsets. Except
            # path_info, which may use utf-8, and handled by decodePagename.
            self.decode_charsets = [config.charset]
            
            # hierarchical wiki - set rootpage
            from MoinMoin.Page import Page
            #path = self.getPathinfo()
            #if path.startswith('/'):
            #    pages = path[1:].split('/')
            #    if 0: # len(path) > 1:
            #        ## breaks MainPage/SubPage on flat storage
            #        rootname = u'/'.join(pages[:-1])
            #    else:
            #        # this is the usual case, as it ever was...
            #        rootname = u""
            #else:
            #    # no extra path after script name
            #    rootname = u""

            self.args = {}
            self.form = {}

            rootname = u''
            self.rootpage = Page(self, rootname, is_rootpage=1)

            self.user = self.get_user()

            from MoinMoin import i18n

            # Set theme - forced theme, user theme or wiki default
            if self.cfg.theme_force:
                theme_name = self.cfg.theme_default
            else:
                theme_name = self.user.theme_name
            self.loadTheme(theme_name)
            
            self.logger = None
            self.pragma = {}
            self.mode_getpagelinks = 0
            self.no_closing_html_code = 0

            self.__dict__.update(properties)

            self.i18n = i18n
            self.lang = i18n.requestLanguage(self) 
            # Language for content. Page content should use the wiki
            # default lang, but generated content like search results
            # should use the user language.
            self.content_lang = self.cfg.default_lang
            self.getText = lambda text, i18n=self.i18n, request=self, lang=self.lang, **kv: i18n.getText(text, request, lang, kv.get('formatted', True))

            self.opened_logs = 0
            self.reset()
        
    def getDicts(self):
        """ Lazy initialize the dicts on the first access """
        if self._dicts is None:
            from MoinMoin import wikidicts
            dicts = wikidicts.GroupDict(self)
            dicts.scandicts()
            self._dicts = dicts
        return self._dicts
        
    def delDicts(self):
        """ Delete the dicts, used by some tests """
        del self._dicts
        self._dicts = None

    dicts = property(getDicts, None, delDicts)
  
    def _load_multi_cfg(self):
        # protect against calling multiple times
        if not hasattr(self, 'cfg'):
            from MoinMoin import multiconfig
            self.cfg = multiconfig.getConfig(self.url)
            
    def setAcceptedCharsets(self, accept_charset):
        """ Set accepted_charsets by parsing accept-charset header

        Set self.accepted_charsets to an ordered list based on
        http_accept_charset. 
        
        Reference: http://www.w3.org/Protocols/rfc2616/rfc2616.txt

        TODO: currently no code use this value.

        @param accept_charset: accept-charset header
        """        
        charsets = []
        if accept_charset:
            accept_charset = accept_charset.lower()
            # Add iso-8859-1 if needed
            if (not '*' in accept_charset and
                accept_charset.find('iso-8859-1') < 0):
                accept_charset += ',iso-8859-1'

            # Make a list, sorted by quality value, using Schwartzian Transform
            # Create list of tuples (value, name) , sort, extract names  
            for item in accept_charset.split(','):
                if ';' in item:
                    name, qval = item.split(';')
                    qval = 1.0 - float(qval.split('=')[1])
                else:
                    name, qval = item, 0
                charsets.append((qval, name))                 
            charsets.sort()
            # Remove *, its not clear what we should do with it later
            charsets = [name for qval, name in charsets if name != '*']

        self.accepted_charsets = charsets
          
    def _setup_vars_from_std_env(self, env):
        """ Set common request variables from CGI environment
        
        Parse a standard CGI environment as created by common web
        servers. Reference: http://www.faqs.org/rfcs/rfc3875.html

        @param env: dict like object containing cgi meta variables
        """
        # Values we can just copy
        self.env = env
        self.http_accept_language = env.get('HTTP_ACCEPT_LANGUAGE',
                                            self.http_accept_language)
        self.server_name = env.get('SERVER_NAME', self.server_name)
        self.server_port = env.get('SERVER_PORT', self.server_port)
        self.saved_cookie = env.get('HTTP_COOKIE', '')
        self.script_name = env.get('SCRIPT_NAME', '')
        self.path_info = env.get('PATH_INFO', '')
        self.query_string = env.get('QUERY_STRING', '')
        self.request_method = env.get('REQUEST_METHOD', None)
        self.remote_addr = env.get('REMOTE_ADDR', '')
        self.http_user_agent = env.get('HTTP_USER_AGENT', '')

        # REQUEST_URI is not part of CGI spec, but an addition of
        # Apache.
        self.request_uri = env.get('REQUEST_URI', '')
        
        # Values that need more work
        self.setHttpReferer(env.get('HTTP_REFERER'))
        self.setIsSSL(env)
        self.setHost(env.get('HTTP_HOST'))
        self.fixURI(env)
        self.setURL(env)
        
        ##self.debugEnvironment(env)

    def setHttpReferer(self, referer):
        """ Set http_referer, making sure its ascii
        
        IE might send non-ascii value.
        """
        value = ''
        if referer:
            value = unicode(referer, 'ascii', 'replace')
            value = value.encode('ascii', 'replace')
        self.http_referer = value

    def setIsSSL(self, env):
        """ Set is_ssl 
        
        @param env: dict like object containing cgi meta variables
        """
        self.is_ssl = (env.get('SSL_PROTOCOL') or
                       env.get('SSL_PROTOCOL_VERSION') or
                       env.get('HTTPS') == 'on')

    def setHost(self, host=None):
        """ Set http_host 
        
        Create from server name and port if missing. Previous code
        default to localhost.
        """
        if not host:
            port = ''
            standardPort = ('80', '443')[self.is_ssl]
            if self.server_port != standardPort:
                port = ':' + self.server_port
            host = self.server_name + port
        self.http_host = host
        
    def fixURI(self, env):
        """ Fix problems with script_name and path_info
        
        Handle the strange charset semantics on Windows and other non
        posix systems. path_info is transformed into the system code
        page by the web server. Additionally, paths containing dots let
        most webservers choke.
        
        Broken environment variables in different environments:
                path_info script_name
        Apache1     X          X      PI does not contain dots
        Apache2     X          X      PI is not encoded correctly
        IIS         X          X      path_info include script_name
        Other       ?          -      ? := Possible and even RFC-compatible.
                                      - := Hopefully not.

        @param env: dict like object containing cgi meta variables
        """ 
        # Fix the script_name when using Apache on Windows.
        server_software = env.get('SERVER_SOFTWARE', '')
        if os.name == 'nt' and server_software.find('Apache/') != -1:
            # Removes elements ending in '.' from the path.
            self.script_name = '/'.join([x for x in self.script_name.split('/') 
                                         if not x.endswith('.')])

        # Fix path_info
        if os.name != 'posix' and self.request_uri != '':
            # Try to recreate path_info from request_uri.
            import urlparse, urllib
            scriptAndPath = urlparse.urlparse(self.request_uri)[2]
            path = scriptAndPath.replace(self.script_name, '', 1)            
            self.path_info = urllib.unquote(path)
        elif os.name == 'nt':
            # Recode path_info to utf-8
            path = wikiutil.decodeWindowsPath(self.path_info)
            self.path_info = path.encode("utf-8")
            
            # Fix bug in IIS/4.0 when path_info contain script_name
            if self.path_info.startswith(self.script_name):
                self.path_info = self.path_info[len(self.script_name):]

    def setURL(self, env):
        """ Set url, used to locate wiki config 
        
        This is the place to manipulate url parts as needed.
        
        @param env: dict like object containing cgi meta variables or
            http headers.
        """
        # If we serve on localhost:8000 and use a proxy on
        # example.com/wiki, our urls will be example.com/wiki/pagename
        # Same for the wiki config - they must use the proxy url.
        self.rewriteHost(env)
        self.rewriteURI(env)
        
        if not self.request_uri:
            self.request_uri = self.makeURI()
        self.url = self.http_host + self.request_uri

    def rewriteHost(self, env):
        """ Rewrite http_host transparently
        
        Get the proxy host using 'X-Forwarded-Host' header, added by
        Apache 2 and other proxy software.
        
        TODO: Will not work for Apache 1 or others that don't add this
        header.
        
        TODO: If we want to add an option to disable this feature it
        should be in the server script, because the config is not
        loaded at this point, and must be loaded after url is set.
        
        @param env: dict like object containing cgi meta variables or
            http headers.
        """
        proxy_host = (env.get(self.proxy_host) or
                      env.get(cgiMetaVariable(self.proxy_host)))
        if proxy_host:
            self.http_host = proxy_host

    def rewriteURI(self, env):
        """ Rewrite request_uri, script_name and path_info transparently
        
        Useful when running mod python or when running behind a proxy,
        e.g run on localhost:8000/ and serve as example.com/wiki/.

        Uses private 'X-Moin-Location' header to set the script name.
        This allow setting the script name when using Apache 2
        <location> directive::

            <Location /my/wiki/>
                RequestHeader set X-Moin-Location /my/wiki/
            </location>
        
        TODO: does not work for Apache 1 and others that do not allow
        setting custom headers per request.
        
        @param env: dict like object containing cgi meta variables or
            http headers.
        """
        location = (env.get(self.moin_location) or 
                    env.get(cgiMetaVariable(self.moin_location)))
        if location is None:
            return
        
        scriptAndPath = self.script_name + self.path_info
        location = location.rstrip('/')
        self.script_name = location
        
        # This may happen when using mod_python
        if scriptAndPath.startswith(location):
            self.path_info = scriptAndPath[len(location):]

        # Recreate the URI from the modified parts
        if self.request_uri:
            self.request_uri = self.makeURI()

    def makeURI(self):
        """ Return uri created from uri parts """
        import urllib
        uri = self.script_name + urllib.quote(self.path_info)
        if self.query_string:
            uri += '?' + self.query_string
        return uri

    def splitURI(self, uri):
        """ Return path and query splited from uri
        
        Just like CGI environment, the path is unquoted, the query is
        not.
        """
        import urllib
        if '?' in uri:
            path, query = uri.split('?', 1)
        else:
            path, query = uri, ''
        return urllib.unquote(path), query        
                
    def get_user(self):
        for auth in self.cfg.auth:
            the_user = auth(self)
            if the_user: return the_user

        # XXX create
        return user.User(self)

    def reset(self):
        """ Reset request state.

        Called after saving a page, before serving the updated
        page. Solves some practical problems with request state
        modified during saving.

        """
        # This is the content language and has nothing to do with
        # The user interface language. The content language can change
        # during the rendering of a page by lang macros
        self.current_lang = self.cfg.default_lang

        self._footer_fragments = {}
        self._all_pages = None
        # caches unique ids
        self._page_ids = {}
        # keeps track of pagename/heading combinations
        # parsers should use this dict and not a local one, so that
        # macros like TableOfContents in combination with Include
        # can work
        self._page_headings = {}

        if hasattr(self, "_fmt_hd_counters"):
            del self._fmt_hd_counters

    def loadTheme(self, theme_name):
        """ Load the Theme to use for this request.

        @param theme_name: the name of the theme
        @type theme_name: str
        @returns: 0 on success, 1 if user theme could not be loaded,
                  2 if a hard fallback to modern theme was required.
        @rtype: int
        @return: success code
        """
        fallback = 0
        if theme_name == "<default>":
            theme_name = self.cfg.theme_default
        Theme = wikiutil.importPlugin(self.cfg, 'theme', theme_name, 'Theme')
        if Theme is None:
            fallback = 1
            Theme = wikiutil.importPlugin(self.cfg, 'theme',
                                          self.cfg.theme_default, 'Theme')
            if Theme is None:
                fallback = 2
                from MoinMoin.theme.modern import Theme
        self.theme = Theme(self)

        return fallback

    def setContentLanguage(self, lang):
        """ Set the content language, used for the content div

        Actions that generate content in the user language, like search,
        should set the content direction to the user language before they
        call send_title!
        """
        self.content_lang = lang
        self.current_lang = lang

    def add2footer(self, key, htmlcode):
        """ Add a named HTML fragment to the footer, after the default links
        """
        self._footer_fragments[key] = htmlcode

    def getPragma(self, key, defval=None):
        """ Query a pragma value (#pragma processing instruction)

            Keys are not case-sensitive.
        """
        return self.pragma.get(key.lower(), defval)

    def setPragma(self, key, value):
        """ Set a pragma value (#pragma processing instruction)

            Keys are not case-sensitive.
        """
        self.pragma[key.lower()] = value

    def getPathinfo(self):
        """ Return the remaining part of the URL. """
        return self.path_info

    def getScriptname(self):
        """ Return the scriptname part of the URL ('/path/to/my.cgi'). """
        if self.script_name == '/':
            return ''
        return self.script_name

    def getPageNameFromQueryString(self):
        """ Try to get pagename from the query string
        
        Support urls like http://netloc/script/?page_name. Allow
        solving path_info encodig problems by calling with the page
        name as a query.
        """
        import urllib
        pagename = urllib.unquote(self.query_string)
        pagename = self.decodePagename(pagename)
        pagename = self.normalizePagename(pagename)
        return pagename
    
    def getKnownActions(self):
        """ Create a dict of avaiable actions

        Return cached version if avaiable.
       
        @rtype: dict
        @return: dict of all known actions
        """
        try:
            self.cfg._known_actions # check
        except AttributeError:
            from MoinMoin import wikiaction
            # Add built in  actions from wikiaction
            actions = [name[3:] for name in wikiaction.__dict__
                       if name.startswith('do_')]

            # Add plugins           
            dummy, plugins = wikiaction.getPlugins(self)
            actions.extend(plugins)

            # Add extensions
            from MoinMoin.action import extension_actions
            actions.extend(extension_actions)           
           
            # TODO: Use set when we require Python 2.3
            actions = dict(zip(actions, [''] * len(actions)))            
            self.cfg._known_actions = actions

        # Return a copy, so clients will not change the dict.
        return self.cfg._known_actions.copy()        

    def getAvailableActions(self, page):
        """ Get list of avaiable actions for this request

        The dict does not contain actions that starts with lower
        case. Themes use this dict to display the actions to the user.

        @param page: current page, Page object
        @rtype: dict
        @return: dict of avaiable actions
        """
        if self._available_actions is None:
            # Add actions for existing pages only, including deleted pages.
            # Fix *OnNonExistingPage bugs.
            if not (page.exists(includeDeleted=1) and
                    self.user.may.read(page.page_name)):
                return []

            # Filter non ui actions (starts with lower case letter)
            actions = self.getKnownActions()
            for key in actions.keys():
                if key[0].islower():
                    del actions[key]

            # Filter wiki excluded actions
            for key in self.cfg.actions_excluded:
                if key in actions:
                    del actions[key]                

            # Filter actions by page type, acl and user state
            excluded = []
            if ((page.isUnderlayPage() and not page.isStandardPage()) or
                not self.user.may.write(page.page_name)):
                # Prevent modification of underlay only pages, or pages
                # the user can't write to
                excluded = [u'RenamePage', u'DeletePage',] # AttachFile must NOT be here!
            elif not self.user.valid:
                # Prevent rename and delete for non registered users
                excluded = [u'RenamePage', u'DeletePage']
            for key in excluded:
                if key in actions:
                    del actions[key]                

            self._available_actions = actions

        # Return a copy, so clients will not change the dict.
        return self._available_actions.copy()

    def redirectedOutput(self, function, *args, **kw):
        """ Redirect output during function, return redirected output """
        buffer = StringIO.StringIO()
        self.redirect(buffer)
        try:
            function(*args, **kw)
        finally:
            self.redirect()
        text = buffer.getvalue()
        buffer.close()        
        return text

    def redirect(self, file=None):
        """ Redirect output to file, or restore saved output """
        if file:
            self.writestack.append(self.write)
            self.write = file.write
        else:
            self.write = self.writestack.pop()

    def reset_output(self):
        """ restore default output method
            destroy output stack
            (useful for error messages)
        """
        if self.writestack:
            self.write = self.writestack[0]
            self.writestack = []

    def log(self, msg):
        """ Log to stderr, which may be error.log """
        msg = msg.strip()
        # Encode unicode msg
        if isinstance(msg, unicode):
            msg = msg.encode(config.charset)
        # Add time stamp
        msg = '[%s] %s\n' % (time.asctime(), msg)
        sys.stderr.write(msg)
    
    def write(self, *data):
        """ Write to output stream.
        """
        raise NotImplementedError

    def encode(self, data):
        """ encode data (can be both unicode strings and strings),
            preparing for a single write()
        """
        wd = []
        for d in data:
            try:
                if isinstance(d, unicode):
                    # if we are REALLY sure, we can use "strict"
                    d = d.encode(config.charset, 'replace') 
                wd.append(d)
            except UnicodeError:
                print >>sys.stderr, "Unicode error on: %s" % repr(d)
        return ''.join(wd)
    
    def decodePagename(self, name):
        """ Decode path, possibly using non ascii characters

        Does not change the name, only decode to Unicode.

        First split the path to pages, then decode each one. This enables
        us to decode one page using config.charset and another using
        utf-8. This situation happens when you try to add to a name of
        an existing page.

        See http://www.w3.org/TR/REC-html40/appendix/notes.html#h-B.2.1
        
        @param name: page name, string
        @rtype: unicode
        @return decoded page name
        """
        # Split to pages and decode each one
        pages = name.split('/')
        decoded = []
        for page in pages:
            # Recode from utf-8 into config charset. If the path
            # contains user typed parts, they are encoded using 'utf-8'.
            if config.charset != 'utf-8':
                try:
                    page = unicode(page, 'utf-8', 'strict')
                    # Fit data into config.charset, replacing what won't
                    # fit. Better have few "?" in the name than crash.
                    page = page.encode(config.charset, 'replace')
                except UnicodeError:
                    pass
                
            # Decode from config.charset, replacing what can't be decoded.
            page = unicode(page, config.charset, 'replace')
            decoded.append(page)

        # Assemble decoded parts
        name = u'/'.join(decoded)
        return name

    def normalizePagename(self, name):
        """ Normalize page name 

        Convert '_' to spaces - allows using nice URLs with spaces, with no
        need to quote.

        Prevent creating page names with invisible characters or funny
        whitespace that might confuse the users or abuse the wiki, or
        just does not make sense.

        Restrict even more group pages, so they can be used inside acl
        lines.
        
        @param name: page name, unicode
        @rtype: unicode
        @return: decoded and sanitized page name
        """
        # Replace underscores with spaces
        name = name.replace(u'_', u' ')

        # Strip invalid characters
        name = config.page_invalid_chars_regex.sub(u'', name)

        # Split to pages and normalize each one
        pages = name.split(u'/')
        normalized = []
        for page in pages:            
            # Ignore empty or whitespace only pages
            if not page or page.isspace():
                continue

            # Cleanup group pages.
            # Strip non alpha numeric characters, keep white space
            if wikiutil.isGroupPage(self, page):
                page = u''.join([c for c in page
                                 if c.isalnum() or c.isspace()])

            # Normalize white space. Each name can contain multiple 
            # words separated with only one space. Split handle all
            # 30 unicode spaces (isspace() == True)
            page = u' '.join(page.split())
            
            normalized.append(page)            
        
        # Assemble components into full pagename
        name = u'/'.join(normalized)
        return name
        
    def read(self, n):
        """ Read n bytes from input stream.
        """
        raise NotImplementedError

    def flush(self):
        """ Flush output stream.
        """
        raise NotImplementedError
        
    def isForbidden(self):
        """ check for web spiders and refuse anything except viewing """
        forbidden = 0
        # we do not have a parsed query string here
        # so we can just do simple matching
        if ((self.query_string != '' or self.request_method != 'GET') and
            self.query_string != 'action=rss_rc' and not
            # allow spiders to get attachments and do 'show'
            (self.query_string.find('action=AttachFile') >= 0 and self.query_string.find('do=get') >= 0) and not
            (self.query_string.find('action=show') >= 0)
            ):
            from MoinMoin.util import web
            forbidden = web.isSpiderAgent(self)

        if not forbidden and self.cfg.hosts_deny:
            ip = self.remote_addr
            for host in self.cfg.hosts_deny:
                if ip == host or host[-1] == '.' and ip.startswith(host):
                    forbidden = 1
                    break
        return forbidden

    def setup_args(self, form=None):
        """ Return args dict 
        
        In POST request, invoke _setup_args_from_cgi_form to handle
        possible file uploads. For other request simply parse the query
        string.
        
        Warning: calling with a form might fail, depending on the type
        of the request! Only the request know which kind of form it can
        handle.
        
        TODO: The form argument should be removed in 1.5.
        """
        if form is not None or self.request_method == 'POST':
            return self._setup_args_from_cgi_form(form)
        args = cgi.parse_qs(self.query_string, keep_blank_values=1)
        return self.decodeArgs(args)

    def _setup_args_from_cgi_form(self, form=None):
        """ Return args dict from a FieldStorage
        
        Create the args from a standard cgi.FieldStorage or from given
        form. Each key contain a list of values.

        @keyword form: a cgi.FieldStorage
        @rtype: dict
        @return dict with form keys, each contains a list of values
        """
        if form is None:
            form = cgi.FieldStorage()

        args = {}
        for key in form:
            values = form[key]
            if not isinstance(values, list):
                values = [values]
            fixedResult = []
            for item in values:
                fixedResult.append(item.value)
                if isinstance(item, cgi.FieldStorage) and item.filename:
                    # Save upload file name in a separate key
                    args[key + '__filename__'] = item.filename            
            args[key] = fixedResult
            
        return self.decodeArgs(args)

    def decodeArgs(self, args):
        """ Decode args dict 
        
        Decoding is done in a separate path because it is reused by
        other methods and sub classes.
        """
        decode = wikiutil.decodeUserInput
        result = {}
        for key in args:
            if key + '__filename__' in args:
                # Copy file data as is
                result[key] = args[key]
            elif key.endswith('__filename__'):
                result[key] = decode(args[key], self.decode_charsets)
            else:
                result[key] = [decode(value, self.decode_charsets)
                               for value in args[key]]
        return result

    def getBaseURL(self):
        """ Return a fully qualified URL to this script. """
        return self.getQualifiedURL(self.getScriptname())

    def getQualifiedURL(self, uri=''):
        """ Return an absolute URL starting with schema and host.

        Already qualified urls are returned unchanged.

        @param uri: server rootted uri e.g /scriptname/pagename. It
            must start with a slash. Must be ascii and url encoded.
        """
        import urlparse
        scheme = urlparse.urlparse(uri)[0]
        if scheme:
            return uri

        schema = ('http', 'https')[self.is_ssl]
        result = "%s://%s%s" % (schema, self.http_host, uri)

        # This might break qualified urls in redirects!
        # e.g. mapping 'http://netloc' -> '/'
        return wikiutil.mapURL(self, result)

    def getUserAgent(self):
        """ Get the user agent. """
        return self.http_user_agent

    def makeForbidden(self):
        self.forbidden = True
        self.http_headers([
            'Status: 403 FORBIDDEN',
            'Content-Type: text/plain'
        ])
        self.write('You are not allowed to access this!\r\n')
        self.setResponseCode(403)
        
    def run(self):
        # __init__ may have failed
        if self.failed or self.forbidden:
            return self.finish()
        
        if self.isForbidden():
            self.makeForbidden()
            if self.forbidden:
                return self.finish()

        self.open_logs()
        _ = self.getText
        self.clock.start('run')

        # Imports
        from MoinMoin.Page import Page

        if self.query_string == 'action=xmlrpc':
            from MoinMoin.wikirpc import xmlrpc
            xmlrpc(self)
            return self.finish()
        
        if self.query_string == 'action=xmlrpc2':
            from MoinMoin.wikirpc import xmlrpc2
            xmlrpc2(self)
            return self.finish()

        # parse request data
        try:
            self.args = self.setup_args()
            self.form = self.args    
            action = self.form.get('action',[None])[0]

            # Get pagename
            # The last component in path_info is the page name, if any
            path = self.getPathinfo()
            if path.startswith('/'):
                pagename = self.normalizePagename(path)
            else:
                pagename = None
        except: # catch and print any exception
            self.reset_output()
            self.http_headers()
            self.print_exception()
            return self.finish()
        
        try:
            # Handle request. We have these options:
            
            # 1. If user has a bad user name, delete its bad cookie and
            # send him to UserPreferences to make a new account.
            if not user.isValidName(self, self.user.name):
                msg = _("""Invalid user name {{{'%s'}}}.
Name may contain any Unicode alpha numeric character, with optional one
space between words. Group page name is not allowed.""") % self.user.name
                self.deleteCookie()
                page = wikiutil.getSysPage(self, 'UserPreferences')
                page.send_page(self, msg=msg)

            # 2. Or jump to page where user left off
            elif not pagename and not action and self.user.remember_last_visit:
                pagetrail = self.user.getTrail()
                if pagetrail:
                    # Redirect to last page visited
                    if ":" in pagetrail[-1]:
                        wikitag, wikiurl, wikitail, error = wikiutil.resolve_wiki(self, pagetrail[-1]) 
                        url = wikiurl + wikitail
                    else:
                        url = Page(self, pagetrail[-1]).url(self)
                else:
                    # Or to localized FrontPage
                    url = wikiutil.getFrontPage(self).url(self)
                self.http_redirect(url)
                return self.finish()
            
            # 3. Or save drawing
            elif (self.form.has_key('filepath') and
                self.form.has_key('noredirect')):
                # looks like user wants to save a drawing
                from MoinMoin.action.AttachFile import execute
                # TODO: what if pagename is None?
                execute(pagename, self)
                raise MoinMoinNoFooter           

            # 4. Or handle action
            elif action:
                # Use localized FrontPage if pagename is empty
                if not pagename:
                    self.page = wikiutil.getFrontPage(self)
                else:
                    self.page = Page(self, pagename)

                # Complain about unknown actions
                if not action in self.getKnownActions():
                    self.http_headers()
                    self.write(u'<html><body><h1>Unknown action %s</h1></body>' % wikiutil.escape(action))

                # Disallow non available actions
                elif (action[0].isupper() and
                      not action in self.getAvailableActions(self.page)):
                    # Send page with error
                    msg = _("You are not allowed to do %s on this page.") % wikiutil.escape(action)
                    if not self.user.valid:
                        # Suggest non valid user to login
                        login = wikiutil.getSysPage(self, 'UserPreferences')
                        login = login.link_to(self, _('Login'))
                        msg += _(" %s and try again.", formatted=0) % login
                    self.page.send_page(self, msg=msg)

                # Try action
                else:
                    from MoinMoin.wikiaction import getHandler
                    handler = getHandler(self, action)
                    handler(self.page.page_name, self)

            # 5. Or redirect to another page
            elif self.form.has_key('goto'):
                self.http_redirect(Page(self, self.form['goto'][0]).url(self))
                return self.finish()

            # 6. Or (at last) visit pagename
            else:
                if not pagename and self.query_string:
                    pagename = self.getPageNameFromQueryString()                    
                # pagename could be empty after normalization e.g. '///' -> ''
                if not pagename:
                    pagename = wikiutil.getFrontPage(self).page_name

                # Visit pagename
                self.page = Page(self, pagename)
                self.page.send_page(self, count_hit=1)

            # generate page footer (actions that do not want this footer
            # use raise util.MoinMoinNoFooter to break out of the
            # default execution path, see the "except MoinMoinNoFooter"
            # below)

            self.clock.stop('run')
            self.clock.stop('total')

            # Close html code
            if not self.no_closing_html_code:
                if (self.cfg.show_timings and
                    self.form.get('action', [None])[0] != 'print'):
                    self.write('<ul id="timings">\n')
                    for t in self.clock.dump():
                        self.write('<li>%s</li>\n' % t)
                    self.write('</ul>\n')

                self.write('</body>\n</html>\n\n')
            
        except MoinMoinNoFooter:
            pass

        except MoinMoin.error.FatalError, err:
            self.fail(err)
            return self.finish()
            
        except: 
            # Catch and print any exception
            saved_exc = sys.exc_info()
            self.reset_output()
            
            # Send 500 error code
            self.http_headers(['Status: 500 MoinMoin Internal Error'])
            self.setResponseCode(500)
            self.http_headers()
            
            self.write(u"\n<!-- ERROR REPORT FOLLOWS -->\n")
            try:
                from MoinMoin.support import cgitb
            except:
                # no cgitb, for whatever reason
                self.print_exception(*saved_exc)
            else:
                try:
                    cgitb.Hook(file=self).handle(saved_exc)
                    # was: cgitb.handler()
                except:
                    self.print_exception(*saved_exc)
                    self.write("\n\n<hr>\n")
                    self.write("<p><strong>Additionally, cgitb raised this exception:</strong></p>\n")
                    self.print_exception()
            del saved_exc

        return self.finish()

    def http_redirect(self, url):
        """ Redirect to a fully qualified, or server-rooted URL
        
        @param url: relative or absolute url, ascii using url encoding.
        """
        url = self.getQualifiedURL(url)
        self.http_headers(["Status: 302", "Location: %s" % url])

    def setHttpHeader(self, header):
        """ Save header for later send. """
        self.user_headers.append(header)

    def setResponseCode(self, code, message=None):
        pass

    def fail(self, err):
        """ Fail with nice error message when we can't continue

        Log the error, then try to print nice error message. Send 500
        status code with the error name. Reference: 
        http://www.w3.org/Protocols/rfc2616/rfc2616-sec6.html#sec6.1.1

        @param err: MoinMoin.error.FatalError instance or subclass.
        """
        self.failed = 1 # save state for self.run()
        self.log(err.asLog())
        self.http_headers(['Status: 500 %(name)s' % err])
        self.setResponseCode(500)
        self.write(err.asHTML())
            
    def print_exception(self, type=None, value=None, tb=None, limit=None):
        if type is None:
            type, value, tb = sys.exc_info()
        import traceback
        self.write("<h2>request.print_exception handler</h2>\n")
        self.write("<h3>Traceback (most recent call last):</h3>\n")
        list = traceback.format_tb(tb, limit) + \
               traceback.format_exception_only(type, value)
        self.write("<pre>%s<strong>%s</strong></pre>\n" % (
            wikiutil.escape("".join(list[:-1])),
            wikiutil.escape(list[-1]),))
        del tb

    def open_logs(self):
        pass

    def makeUniqueID(self, base):
        """
        Generates a unique ID using a given base name. Appends a
        running count to the base.

        @param base: the base of the id
        @type base: unicode

        @returns: an unique id
        @rtype: unicode
        """
        if not isinstance(base, unicode):
            base = unicode(str(base), 'ascii', 'ignore')
        count = self._page_ids.get(base, -1) + 1
        self._page_ids[base] = count
        if count == 0:
            return base
        return u'%s_%04d' % (base, count)

    def httpDate(self, when=None, rfc='1123'):
        """ Returns http date string, according to rfc2068

        See http://www.cse.ohio-state.edu/cgi-bin/rfc/rfc2068.html#sec-3.3

        A http 1.1 server should use only rfc1123 date, but cookie's
        "expires" field should use the older obsolete rfc850 date.

        Note: we can not use strftime() because that honors the locale
        and rfc2822 requires english day and month names.

        We can not use email.Utils.formatdate because it formats the
        zone as '-0000' instead of 'GMT', and creates only rfc1123
        dates. This is a modified version of email.Utils.formatdate
        from Python 2.4.

        @param when: seconds from epoch, as returned by time.time()
        @param rfc: conform to rfc ('1123' or '850')
        @rtype: string
        @return: http date conforming to rfc1123 or rfc850
        """
        if when is None:
            when = time.time()
        now = time.gmtime(when)
        month = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul',
                 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'][now.tm_mon - 1]
        if rfc == '1123':
            day = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'][now.tm_wday]
            date = '%02d %s %04d' % (now.tm_mday, month, now.tm_year)
        elif rfc == '850':
            day = ["Monday", "Tuesday", "Wednesday", "Thursday",
                    "Friday", "Saturday", "Sunday"][now.tm_wday]
            date = '%02d-%s-%s' % (now.tm_mday, month, str(now.tm_year)[-2:])
        else:
            raise ValueError("Invalid rfc value: %s" % rfc)
        
        return '%s, %s %02d:%02d:%02d GMT' % (day, date, now.tm_hour,
                                              now.tm_min, now.tm_sec)
    
    def disableHttpCaching(self):
        """ Prevent caching of pages that should not be cached

        This is important to prevent caches break acl by providing one
        user pages meant to be seen only by another user, when both users
        share the same caching proxy.
        """
        # Run only once
        if hasattr(self, 'http_caching_disabled'):
            return
        self.http_caching_disabled = 1

        # Set Cache control header for http 1.1 caches
        # See http://www.cse.ohio-state.edu/cgi-bin/rfc/rfc2109.html#sec-4.2.3
        # and http://www.cse.ohio-state.edu/cgi-bin/rfc/rfc2068.html#sec-14.9
        self.setHttpHeader('Cache-Control: no-cache="set-cookie"')
        self.setHttpHeader('Cache-Control: private')
        self.setHttpHeader('Cache-Control: max-age=0')       

        # Set Expires for http 1.0 caches (does not support Cache-Control)
        yearago = time.time() - (3600 * 24 * 365)
        self.setHttpHeader('Expires: %s' % self.httpDate(when=yearago))

        # Set Pragma for http 1.0 caches
        # See http://www.cse.ohio-state.edu/cgi-bin/rfc/rfc2068.html#sec-14.32
        self.setHttpHeader('Pragma: no-cache')
       
    def setCookie(self):
        """ Set cookie for the current user
        
        cfg.cookie_lifetime and the user 'remember_me' setting set the
        lifetime of the cookie. lifetime in int hours, see table:
        
        value   cookie lifetime
        ----------------------------------------------------------------
         = 0    forever, ignoring user 'remember_me' setting
         > 0    n hours, or forever if user checked 'remember_me'
         < 0    -n hours, ignoring user 'remember_me' setting

        TODO: do we really need this cookie_lifetime setting?
        """
        # Calculate cookie maxage and expires
        lifetime = int(self.cfg.cookie_lifetime) * 3600 
        forever = 10*365*24*3600 # 10 years
        now = time.time()
        if not lifetime:
            maxage = forever
        elif lifetime > 0:
            if self.user.remember_me:
                maxage = forever
            else:
                maxage = lifetime
        elif lifetime < 0:
            maxage = (-lifetime)
        expires = now + maxage
        
        # Set the cookie
        from Cookie import SimpleCookie
        c = SimpleCookie()
        c['MOIN_ID'] = self.user.id
        c['MOIN_ID']['max-age'] = maxage
        if self.cfg.cookie_domain:
            c['MOIN_ID']['domain'] = self.cfg.cookie_domain
        if self.cfg.cookie_path:
            c['MOIN_ID']['path'] = self.cfg.cookie_path
        else:
            c['MOIN_ID']['path'] = self.getScriptname()
        # Set expires for older clients
        c['MOIN_ID']['expires'] = self.httpDate(when=expires, rfc='850')        
        self.setHttpHeader(c.output())

        # Update the saved cookie, so other code works with new setup
        self.saved_cookie = c.output()

        # IMPORTANT: Prevent caching of current page and cookie
        self.disableHttpCaching()

    def deleteCookie(self):
        """ Delete the user cookie by sending expired cookie with null value

        According to http://www.cse.ohio-state.edu/cgi-bin/rfc/rfc2109.html#sec-4.2.2
        Deleted cookie should have Max-Age=0. We also have expires
        attribute, which is probably needed for older browsers.

        Finally, delete the saved cookie and create a new user based on
        the new settings.
        """
        # Set cookie
        from Cookie import SimpleCookie
        c = SimpleCookie()
        c['MOIN_ID'] = ''
        if self.cfg.cookie_domain:
            c['MOIN_ID']['domain'] = self.cfg.cookie_domain
        c['MOIN_ID']['path'] = self.getScriptname()
        c['MOIN_ID']['max-age'] = 0
        # Set expires to one year ago for older clients
        yearago = time.time() - (3600 * 24 * 365)
        c['MOIN_ID']['expires'] = self.httpDate(when=yearago, rfc='850')
        self.setHttpHeader(c.output())

        # Update saved cookie and set new unregistered user
        self.saved_cookie = ''
        self.user = user.User(self)

        # IMPORTANT: Prevent caching of current page and cookie        
        self.disableHttpCaching()

    def finish(self):
        """ General cleanup on end of request
        
        Delete circular references - all object that we create using
        self.name = class(self)
        This helps Python to collect these objects and keep our
        memory footprint lower
        """
        try:
            del self.user
            del self.theme
            del self.dicts
        except:
            pass

    # ------------------------------------------------------------------
    # Debug

    def debugEnvironment(self, env):
        """ Environment debugging aid """
        # Keep this one name per line so its easy to comment stuff
        names = [
#             'http_accept_language',
#             'http_host',
#             'http_referer',
#             'http_user_agent',
#             'is_ssl',
            'path_info',
            'query_string',
#             'remote_addr',
            'request_method',
#             'request_uri',
#             'saved_cookie',
            'script_name',
#             'server_name',
#             'server_port',
            ]
        names.sort()
        attributes = []
        for name in names:
            attributes.append('  %s = %r\n' % (name, 
                                               getattr(self, name, None)))
        attributes = ''.join(attributes)
        
        environment = []
        names = env.keys()
        names.sort()
        for key in names:
            environment.append('  %s = %r\n' % (key, env[key]))
        environment = ''.join(environment)
        
        data = '\nRequest Attributes\n%s\nEnviroment\n%s' % (attributes,
                                                             environment)        
        f = open('/tmp/env.log','a')
        try:
            f.write(data)
        finally:
            f.close()
  

# CGI ---------------------------------------------------------------

class RequestCGI(RequestBase):
    """ specialized on CGI requests """

    def __init__(self, properties={}):
        self.open_logs()
        try:
            self._setup_vars_from_std_env(os.environ)
            RequestBase.__init__(self, properties)

            # force input/output to binary
            if sys.platform == "win32":
                import msvcrt
                msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
                msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)

        except error.FatalError, err:
            self.fail(err)
            
    def open_logs(self):
        # create log file for catching stderr output
        if not self.opened_logs:
            sys.stderr = open(os.path.join(self.cfg.data_dir, 'error.log'), 'at')
            self.opened_logs = 1

    def read(self, n=None):
        """ Read from input stream.
        """
        if n is None:
            return sys.stdin.read()
        else:
            return sys.stdin.read(n)

    def write(self, *data):
        """ Write to output stream.
        """
        sys.stdout.write(self.encode(data))

    def flush(self):
        sys.stdout.flush()
        
    def finish(self):
        RequestBase.finish(self)
        # flush the output, ignore errors caused by the user closing the socket
        try:
            sys.stdout.flush()
        except IOError, ex:
            import errno
            if ex.errno != errno.EPIPE: raise

    # Headers ----------------------------------------------------------
    
    def http_headers(self, more_headers=[]):
        # Send only once
        if getattr(self, 'sent_headers', None):
            return
        
        self.sent_headers = 1
        have_ct = 0

        # send http headers
        for header in more_headers + getattr(self, 'user_headers', []):
            if header.lower().startswith("content-type:"):
                # don't send content-type multiple times!
                if have_ct: continue
                have_ct = 1
            if type(header) is unicode:
                header = header.encode('ascii')
            self.write("%s\r\n" % header)

        if not have_ct:
            self.write("Content-type: text/html;charset=%s\r\n" % config.charset)

        self.write('\r\n')

        #from pprint import pformat
        #sys.stderr.write(pformat(more_headers))
        #sys.stderr.write(pformat(self.user_headers))


# Twisted -----------------------------------------------------------

class RequestTwisted(RequestBase):
    """ specialized on Twisted requests """

    def __init__(self, twistedRequest, pagename, reactor, properties={}):
        try:
            self.twistd = twistedRequest
            self.reactor = reactor
            
            # Copy headers
            self.http_accept_language = self.twistd.getHeader('Accept-Language')
            self.saved_cookie = self.twistd.getHeader('Cookie')
            self.http_user_agent = self.twistd.getHeader('User-Agent')
            
            # Copy values from twisted request
            self.server_protocol = self.twistd.clientproto
            self.server_name = self.twistd.getRequestHostname().split(':')[0]
            self.server_port = str(self.twistd.getHost()[2])
            self.is_ssl = self.twistd.isSecure()
            self.path_info = '/' + '/'.join([pagename] + self.twistd.postpath)
            self.request_method = self.twistd.method
            self.remote_host = self.twistd.getClient()
            self.remote_addr = self.twistd.getClientIP()
            self.request_uri = self.twistd.uri
            self.script_name = "/" + '/'.join(self.twistd.prepath[:-1])

            # Values that need more work
            self.query_string = self.splitURI(self.twistd.uri)[1]
            self.setHttpReferer(self.twistd.getHeader('Referer'))
            self.setHost()
            self.setURL(self.twistd.getAllHeaders())

            ##self.debugEnvironment(twistedRequest.getAllHeaders())
            
            RequestBase.__init__(self, properties)

        except error.FatalError, err:
            self.delayedError = err

    def run(self):
        """ Handle delayed errors then invoke base class run """
        if hasattr(self, 'delayedError'):
            self.fail(self.delayedError)
            return self.finish()
        RequestBase.run(self)
            
    def setup_args(self, form=None):
        """ Return args dict 
        
        Twisted already parsed args, including __filename__ hacking,
        but did not decoded the values.
        """
        return self.decodeArgs(self.twistd.args)
        
    def read(self, n=None):
        """ Read from input stream.
        """
        # XXX why is that wrong?:
        #rd = self.reactor.callFromThread(self.twistd.read)
        
        # XXX do we need self.reactor.callFromThread with that?
        # XXX if yes, why doesn't it work?
        self.twistd.content.seek(0, 0)
        if n is None:
            rd = self.twistd.content.read()
        else:
            rd = self.twistd.content.read(n)
        #print "request.RequestTwisted.read: data=\n" + str(rd)
        return rd
    
    def write(self, *data):
        """ Write to output stream.
        """
        #print "request.RequestTwisted.write: data=\n" + wd
        self.reactor.callFromThread(self.twistd.write, self.encode(data))

    def flush(self):
        pass # XXX is there a flush in twisted?

    def finish(self):
        RequestBase.finish(self)
        self.reactor.callFromThread(self.twistd.finish)

    def open_logs(self):
        return
        # create log file for catching stderr output
        if not self.opened_logs:
            sys.stderr = open(os.path.join(self.cfg.data_dir, 'error.log'), 'at')
            self.opened_logs = 1

    # Headers ----------------------------------------------------------

    def __setHttpHeader(self, header):
        if type(header) is unicode:
            header = header.encode('ascii')
        key, value = header.split(':',1)
        value = value.lstrip()
        if key.lower()=='set-cookie':
            key, value = value.split('=',1)
            self.twistd.addCookie(key, value)
        else:
            self.twistd.setHeader(key, value)
        #print "request.RequestTwisted.setHttpHeader: %s" % header

    def http_headers(self, more_headers=[]):
        if getattr(self, 'sent_headers', None):
            return
        self.sent_headers = 1
        have_ct = 0

        # set http headers
        for header in more_headers + getattr(self, 'user_headers', []):
            if header.lower().startswith("content-type:"):
                # don't send content-type multiple times!
                if have_ct: continue
                have_ct = 1
            self.__setHttpHeader(header)

        if not have_ct:
            self.__setHttpHeader("Content-type: text/html;charset=%s" % config.charset)

    def http_redirect(self, url):
        """ Redirect to a fully qualified, or server-rooted URL 
        
        @param url: relative or absolute url, ascii using url encoding.
        """
        url = self.getQualifiedURL(url)
        self.twistd.redirect(url)
        # calling finish here will send the rest of the data to the next
        # request. leave the finish call to run()
        #self.twistd.finish()
        raise MoinMoinNoFooter

    def setResponseCode(self, code, message=None):
        self.twistd.setResponseCode(code, message)
        
# CLI ------------------------------------------------------------------

class RequestCLI(RequestBase):
    """ specialized on command line interface and script requests """

    def __init__(self, url='CLI', pagename='', properties={}):
        self.saved_cookie = ''
        self.path_info = '/' + pagename
        self.query_string = ''
        self.remote_addr = '127.0.0.1'
        self.is_ssl = 0
        self.http_user_agent = 'CLI/Script'
        self.url = url
        self.request_method = 'GET'
        self.request_uri = '/' + pagename # TODO check
        self.http_host = 'localhost'
        self.http_referer = ''
        self.script_name = '.'
        RequestBase.__init__(self, properties)
        self.cfg.caching_formats = [] # don't spoil the cache
  
    def read(self, n=None):
        """ Read from input stream.
        """
        if n is None:
            return sys.stdin.read()
        else:
            return sys.stdin.read(n)

    def write(self, *data):
        """ Write to output stream.
        """
        sys.stdout.write(self.encode(data))

    def flush(self):
        sys.stdout.flush()
        
    def finish(self):
        RequestBase.finish(self)
        # flush the output, ignore errors caused by the user closing the socket
        try:
            sys.stdout.flush()
        except IOError, ex:
            import errno
            if ex.errno != errno.EPIPE: raise

    def isForbidden(self):
        """ Nothing is forbidden """
        return 0

    # Accessors --------------------------------------------------------

    def getQualifiedURL(self, uri=None):
        """ Return a full URL starting with schema and host
        
        TODO: does this create correct pages when you render wiki pages
        within a cli request?!
        """
        return uri

    # Headers ----------------------------------------------------------

    def setHttpHeader(self, header):
        pass

    def http_headers(self, more_headers=[]):
        pass

    def http_redirect(self, url):
        """ Redirect to a fully qualified, or server-rooted URL 
        
        TODO: Does this work for rendering redirect pages?
        """
        raise Exception("Redirect not supported for command line tools!")


# StandAlone Server ----------------------------------------------------

class RequestStandAlone(RequestBase):
    """
    specialized on StandAlone Server (MoinMoin.server.standalone) requests
    """
    script_name = ''
    
    def __init__(self, sa, properties={}):
        """
        @param sa: stand alone server object
        @param properties: ...
        """
        try:
            self.sareq = sa
            self.wfile = sa.wfile
            self.rfile = sa.rfile
            self.headers = sa.headers
            self.is_ssl = 0
            
            # TODO: remove in 1.5
            #accept = []
            #for line in sa.headers.getallmatchingheaders('accept'):
            #    if line[:1] in string.whitespace:
            #        accept.append(line.strip())
            #    else:
            #        accept = accept + line[7:].split(',')
            #
            #env['HTTP_ACCEPT'] = ','.join(accept)

            # Copy headers
            self.http_accept_language = (sa.headers.getheader('accept-language') 
                                         or self.http_accept_language)
            self.http_user_agent = sa.headers.getheader('user-agent', '')            
            co = filter(None, sa.headers.getheaders('cookie'))
            self.saved_cookie = ', '.join(co) or ''
            
            # Copy rest from standalone request   
            self.server_name = sa.server.server_name
            self.server_port = str(sa.server.server_port)
            self.request_method = sa.command
            self.request_uri = sa.path
            self.remote_addr = sa.client_address[0]

            # Values that need more work                        
            self.path_info, self.query_string = self.splitURI(sa.path)
            self.setHttpReferer(sa.headers.getheader('referer'))
            self.setHost(sa.headers.getheader('host'))
            self.setURL(sa.headers)

            # TODO: remove in 1.5
            # from standalone script:
            # XXX AUTH_TYPE
            # XXX REMOTE_USER
            # XXX REMOTE_IDENT
            #env['PATH_TRANSLATED'] = uqrest #self.translate_path(uqrest)
            #host = self.address_string()
            #if host != self.client_address[0]:
            #    env['REMOTE_HOST'] = host
            # env['SERVER_PROTOCOL'] = self.protocol_version

            ##self.debugEnvironment(sa.headers)
            
            RequestBase.__init__(self, properties)

        except error.FatalError, err:
            self.fail(err)

    def _setup_args_from_cgi_form(self, form=None):
        """ Override to create standlone form """
        form = cgi.FieldStorage(self.rfile,
                                headers=self.headers,
                                environ={'REQUEST_METHOD': 'POST'})
        return RequestBase._setup_args_from_cgi_form(self, form)
        
    def read(self, n=None):
        """ Read from input stream
        
        Since self.rfile.read() will block, content-length will be used
        instead.
        
        TODO: test with n > content length, or when calling several times
        with smaller n but total over content length.
        """
        if n is None:
            try:
                n = int(self.headers.get('content-length'))
            except (TypeError, ValueError):
                import warnings
                warnings.warn("calling request.read() when content-length is "
                              "not available will block")
                return self.rfile.read()
        return self.rfile.read(n)

    def write(self, *data):
        """ Write to output stream.
        """
        self.wfile.write(self.encode(data))

    def flush(self):
        self.wfile.flush()
        
    def finish(self):
        RequestBase.finish(self)
        self.wfile.flush()

    # Headers ----------------------------------------------------------

    def http_headers(self, more_headers=[]):
        if getattr(self, 'sent_headers', None):
            return
        
        self.sent_headers = 1
        user_headers = getattr(self, 'user_headers', [])
        
        # check for status header and send it
        our_status = 200
        for header in more_headers + user_headers:
            if header.lower().startswith("status:"):
                try:
                    our_status = int(header.split(':',1)[1].strip().split(" ", 1)[0]) 
                except:
                    pass
                # there should be only one!
                break
        # send response
        self.sareq.send_response(our_status)

        # send http headers
        have_ct = 0
        for header in more_headers + user_headers:
            if type(header) is unicode:
                header = header.encode('ascii')
            if header.lower().startswith("content-type:"):
                # don't send content-type multiple times!
                if have_ct: continue
                have_ct = 1

            self.write("%s\r\n" % header)

        if not have_ct:
            self.write("Content-type: text/html;charset=%s\r\n" % config.charset)

        self.write('\r\n')

        #from pprint import pformat
        #sys.stderr.write(pformat(more_headers))
        #sys.stderr.write(pformat(self.user_headers))


# mod_python/Apache ----------------------------------------------------

class RequestModPy(RequestBase):
    """ specialized on mod_python requests """

    def __init__(self, req):
        """ Saves mod_pythons request and sets basic variables using
            the req.subprocess_env, cause this provides a standard
            way to access the values we need here.

            @param req: the mod_python request instance
        """
        try:
            # flags if headers sent out contained content-type or status
            self._have_ct = 0
            self._have_status = 0

            req.add_common_vars()
            self.mpyreq = req
            # some mod_python 2.7.X has no get method for table objects,
            # so we make a real dict out of it first.
            if not hasattr(req.subprocess_env,'get'):
                env=dict(req.subprocess_env)
            else:
                env=req.subprocess_env
            self._setup_vars_from_std_env(env)
            RequestBase.__init__(self)

        except error.FatalError, err:
            self.fail(err)
            
    def rewriteURI(self, env):
        """ Use PythonOption directive to rewrite URI
        
        This is needed when using Apache 1 or other server which does
        not support adding custom headers per request. With mod python we
        can the PythonOption directive:
        
            <Location /url/to/mywiki/>
                PythonOption X-Moin-Location /url/to/mywiki/
            </location>            
        """
        # Be compatible with release 1.3.5 "Location" option 
        # TODO: Remove in later release, we should have one option only.
        old_location = 'Location'
        options = self.mpyreq.get_options()
        location = options.get(self.moin_location) or options.get(old_location)
        if location:
            env[self.moin_location] = location
        RequestBase.rewriteURI(self, env)

    def _setup_args_from_cgi_form(self, form=None):
        """ Override to use mod_python.util.FieldStorage 
        
        Its little different from cgi.FieldStorage, so we need to
        duplicate the conversion code.
        """
        from mod_python import util
        if form is None:
            form = util.FieldStorage(self.mpyreq)

        args = {}
        for key in form.keys():
            values = form[key]
            if not isinstance(values, list):
                values = [values]
            fixedResult = []

            for item in values:
                # Remember filenames with a name hack
                if hasattr(item, 'filename') and item.filename:
                    args[key + '__filename__'] = item.filename
                # mod_python 2.7 might return strings instead of Field
                # objects.
                if hasattr(item, 'value'):
                    item = item.value
                fixedResult.append(item)                
            args[key] = fixedResult
            
        return self.decodeArgs(args)

    def run(self, req):
        """ mod_python calls this with its request object. We don't
            need it cause its already passed to __init__. So ignore
            it and just return RequestBase.run.

            @param req: the mod_python request instance
        """
        return RequestBase.run(self)

    def setup_args(self, form=None):
        return {}

    def read(self, n=None):
        """ Read from input stream.
        """
        if n is None:
            return self.mpyreq.read()
        else:
            return self.mpyreq.read(n)

    def write(self, *data):
        """ Write to output stream.
        """
        self.mpyreq.write(self.encode(data))

    def flush(self):
        """ We can't flush it, so do nothing.
        """
        pass
        
    def finish(self):
        """ Just return apache.OK. Status is set in req.status.
        """
        RequestBase.finish(self)
        # is it possible that we need to return something else here?
        from mod_python import apache
        return apache.OK

    # Headers ----------------------------------------------------------

    def setHttpHeader(self, header):
        """ Filters out content-type and status to set them directly
            in the mod_python request. Rest is put into the headers_out
            member of the mod_python request.

            @param header: string, containing valid HTTP header.
        """
        if type(header) is unicode:
            header = header.encode('ascii')
        key, value = header.split(':',1)
        value = value.lstrip()
        if key.lower() == 'content-type':
            # save content-type for http_headers
            if not self._have_ct:
                # we only use the first content-type!
                self.mpyreq.content_type = value
                self._have_ct = 1
        elif key.lower() == 'status':
            # save status for finish
            try:
                self.mpyreq.status = int(value.split(' ',1)[0])
            except:
                pass
            else:
                self._have_status = 1
        else:
            # this is a header we sent out
            self.mpyreq.headers_out[key]=value

    def http_headers(self, more_headers=[]):
        """ Sends out headers and possibly sets default content-type
            and status.

            @keyword more_headers: list of strings, defaults to []
        """
        for header in more_headers + getattr(self, 'user_headers', []):
            self.setHttpHeader(header)
        # if we don't had an content-type header, set text/html
        if self._have_ct == 0:
            self.mpyreq.content_type = "text/html;charset=%s" % config.charset
        # if we don't had a status header, set 200
        if self._have_status == 0:
            self.mpyreq.status = 200
        # this is for mod_python 2.7.X, for 3.X it's a NOP
        self.mpyreq.send_http_header()

# FastCGI -----------------------------------------------------------

class RequestFastCGI(RequestBase):
    """ specialized on FastCGI requests """

    def __init__(self, fcgRequest, env, form, properties={}):
        """ Initializes variables from FastCGI environment and saves
            FastCGI request and form for further use.

            @param fcgRequest: the FastCGI request instance.
            @param env: environment passed by FastCGI.
            @param form: FieldStorage passed by FastCGI.
        """
        try:
            self.fcgreq = fcgRequest
            self.fcgenv = env
            self.fcgform = form
            self._setup_vars_from_std_env(env)
            RequestBase.__init__(self, properties)

        except error.FatalError, err:
            self.fail(err)

    def _setup_args_from_cgi_form(self, form=None):
        """ Override to use FastCGI form """
        if form is None:
            form = self.fcgform
        return RequestBase._setup_args_from_cgi_form(self, form)

    def read(self, n=None):
        """ Read from input stream.
        """
        if n is None:
            return self.fcgreq.stdin.read()
        else:
            return self.fcgreq.stdin.read(n)

    def write(self, *data):
        """ Write to output stream.
        """
        self.fcgreq.out.write(self.encode(data))

    def flush(self):
        """ Flush output stream.
        """
        self.fcgreq.flush_out()

    def finish(self):
        """ Call finish method of FastCGI request to finish handling
            of this request.
        """
        RequestBase.finish(self)
        self.fcgreq.finish()

    # Headers ----------------------------------------------------------

    def http_headers(self, more_headers=[]):
        """ Send out HTTP headers. Possibly set a default content-type.
        """
        if getattr(self, 'sent_headers', None):
            return
        self.sent_headers = 1
        have_ct = 0

        # send http headers
        for header in more_headers + getattr(self, 'user_headers', []):
            if type(header) is unicode:
                header = header.encode('ascii')
            if header.lower().startswith("content-type:"):
                # don't send content-type multiple times!
                if have_ct: continue
                have_ct = 1
            self.write("%s\r\n" % header)

        if not have_ct:
            self.write("Content-type: text/html;charset=%s\r\n" % config.charset)

        self.write('\r\n')

        #from pprint import pformat
        #sys.stderr.write(pformat(more_headers))
        #sys.stderr.write(pformat(self.user_headers))

# WSGI --------------------------------------------------------------

class RequestWSGI(RequestBase):
    def __init__(self, env):
        try:
            self.env = env
            self.hasContentType = False
            
            self.stdin = env['wsgi.input']
            self.stdout = StringIO.StringIO()
            
            self.status = '200 OK'
            self.headers = []
            
            self._setup_vars_from_std_env(env)
            RequestBase.__init__(self, {})
        
        except error.FatalError, err:
            self.fail(err)
    
    def setup_args(self, form=None):
        if form is None:
            form = cgi.FieldStorage(fp=self.stdin, environ=self.env, keep_blank_values=1)
        return self._setup_args_from_cgi_form(form)
    
    def read(self, n=None):
        if n is None:
            return self.stdin.read()
        else:
            return self.stdin.read(n)
    
    def write(self, *data):
        self.stdout.write(self.encode(data))
    
    def reset_output(self):
        self.stdout = StringIO.StringIO()
    
    def setHttpHeader(self, header):
        if type(header) is unicode:
            header = header.encode('ascii')
        
        key, value = header.split(':', 1)
        value = value.lstrip()
        if key.lower() == 'content-type':
            # save content-type for http_headers
            if self.hasContentType:
                # we only use the first content-type!
                return
            else:
                self.hasContentType = True
        
        elif key.lower() == 'status':
            # save status for finish
            self.status = value
            return
            
        self.headers.append((key, value))
    
    def http_headers(self, more_headers=[]):
        for header in more_headers:
            self.setHttpHeader(header)
        
        if not self.hasContentType:
            self.headers.insert(0, ('Content-Type', 'text/html;charset=%s' % config.charset))
    
    def flush(self):
        pass
    
    def finish(self):
        pass
    
    def output(self):
        return self.stdout.getvalue()

