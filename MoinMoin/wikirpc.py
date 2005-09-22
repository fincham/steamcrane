# -*- coding: iso-8859-1 -*-
"""
    MoinMoin - Wiki XMLRPC v1 and v2 Interface

    If you want to use wikirpc function "putPage", read the comments in
    xmlrpc_putPage or it won't work!
    
    Parts of this code are based on J�rgen Hermann's wikirpc.py,
    Les Orchard's "xmlrpc.cgi" and further work by Gustavo Niemeyer.

    See http://www.ecyrd.com/JSPWiki/Wiki.jsp?page=WikiRPCInterface
    and http://www.decafbad.com/twiki/bin/view/Main/XmlRpcToWiki
    for specs on many of the functions here.

    See also http://www.jspwiki.org/Wiki.jsp?page=WikiRPCInterface2
    for the new stuff.

    The main difference between v1 and v2 is that v2 relies on utf-8
    as transport encoding. No url-encoding and no base64 anymore, except
    when really necessary (like for transferring binary files like
    attachments maybe).

    @copyright: 2003-2005 by Thomas Waldmann
    @license: GNU GPL, see COPYING for details
"""

import sys, urllib, time, xmlrpclib

from MoinMoin import config, user, wikiutil
from MoinMoin.Page import Page
from MoinMoin.PageEditor import PageEditor
from MoinMoin.logfile import editlog
from MoinMoin.formatter import text_html

_debug = 0

class XmlRpcBase:
    def __init__(self, request):
        """
        Initialize an XmlRpcBase object.
        @param request: the request object
        """
        self.request = request
        self.version = None # this has to be defined in derived class
        self.cfg = request.cfg

    #############################################################################
    ### Helper functions           
    #############################################################################

    def _instr(self, text):
        """ Convert inbound string from utf-8.
        
        @param text: the text to convert
        @rtype: str
        @return: string in config.charset
        """
        raise "NotImplementedError"
    
    def _outstr(self, text):
        """ Convert outbound string to utf-8.

        @param text: the text to convert XXX unicode? str? both?
        @rtype: str
        @return: string in utf-8
        """
        raise "NotImplementedError"
    
    def _inlob(self, text):
        """ Convert inbound base64-encoded utf-8 to Large OBject.
        
        @param text: the text to convert
        @rtype: unicode
        @return: text
        """
        text = text.data #this is a already base64-decoded 8bit string
        text = unicode(text, 'utf-8')
        return text

    def _outlob(self, text):
        """ Convert outbound Large OBject to base64-encoded utf-8.
        
        @param text: the text, either unicode or utf-8 string
        @rtype: str
        @return: xmlrpc Binary object
        """
        if isinstance(text, unicode):
            text = text.encode('utf-8')
        else:
            if config.charset != 'utf-8':
                text = unicode(text, config.charset).encode('utf-8')
        return xmlrpclib.Binary(text)
                    
    def _dump_exc(self):
        """ Convert an exception to a string.
        
        @rtype: str
        @return: traceback as string
        """
        import traceback

        return "%s: %s\n%s" % (
            sys.exc_info()[0],
            sys.exc_info()[1],
            '\n'.join(traceback.format_tb(sys.exc_info()[2])),
        )


    #############################################################################
    ### Interface implementation
    #############################################################################

    def xmlrpc_getRPCVersionSupported(self):
        """ Returns version of the Wiki API.

        @rtype: int
        @return: 1 or 2 (wikirpc version)
        """
        return self.version

    def xmlrpc_getAllPages(self):
        """ Get all pages readable by current user

        @rtype: list
        @return: a list of all pages. The result is a list of utf-8 strings.
        """
        pagelist = self.request.rootpage.getPageList()
        return map(self._outstr, pagelist)

    def xmlrpc_getRecentChanges(self, date):
        """ Get RecentChanges since date
        
        @param date: date since when rc will be listed
        @rtype: list
        @return: a list of changed pages since date, which should be in
            UTC. The result is a list, where each element is a struct:
            * name (string) :
                Name of the page. The name is in UTF-8.
            * lastModified (date) :
                Date of last modification, in UTC.
            * author (string) :
                Name of the author (if available). UTF-8.
            * version (int) :
                Current version.
        """
        
        return_items = []
        
        edit_log = editlog.EditLog(self.request)
        for log in edit_log.reverse():
            # get last-modified UTC (DateTime) from log
            gmtuple = tuple(time.gmtime(wikiutil.version2timestamp(log.ed_time_usecs)))
            lastModified_date = xmlrpclib.DateTime(gmtuple)

            # skip if older than "date"
            if lastModified_date < date:
                break
            
            # skip if knowledge not permitted
            if not self.request.user.may.read(log.pagename):
                continue
            
            # get page name (str) from log
            pagename_str = self._outstr(log.pagename)

            # get user name (str) from log
            author_str = log.hostname
            if log.userid:
                userdata = user.User(self.request, log.userid)
                if userdata.name:
                    author_str = userdata.name
            author_str = self._outstr(author_str)

            return_item = { 'name':  pagename_str,
                            'lastModified': lastModified_date,
                            'author': author_str,
                            'version': int(log.rev) }
            return_items.append(return_item)
        
        return return_items

    def xmlrpc_getPageInfo(self, pagename):
        """ Invoke xmlrpc_getPageInfoVersion with rev=None """
        return self.xmlrpc_getPageInfoVersion(pagename, rev=None)

    def xmlrpc_getPageInfoVersion(self, pagename, rev):
        """ Return page information for specific revision
        
        @param pagename: the name of the page (utf-8)
        @param rev: revision to get info about (XXX int?)
        @rtype: dict XXX ??
        @return: page information
            * name (string): the canonical page name, UTF-8.
            * lastModified (date): Last modification date, UTC.
            * author (string): author name, UTF-8.
            * version (int): current version

        """
        pn = self._instr(pagename)

        # User may read this page?
        if not self.request.user.may.read(pn):
            return self.notAllowedFault()

        if rev != None:
            page = Page(self.request, pn, rev=rev)
        else:
            page = Page(self.request, pn)
            rev = page.current_rev()

        # Non existing page?
        if not page.exists():
            return self.noSuchPageFault()

        # Get page info
        last_edit = page.last_edit(self.request)           
        mtime = wikiutil.version2timestamp(long(last_edit['timestamp'])) # must be long for py 2.2.x
        gmtuple = tuple(time.gmtime(mtime))
        
        version = rev # our new rev numbers: 1,2,3,4,....

        #######################################################################
        # BACKWARDS COMPATIBILITY CODE - remove when 1.2.x is regarded stone age
        # as we run a feed for BadContent on MoinMaster, we want to stay
        # compatible here for a while with 1.2.x moins asking us for BadContent
        # 1.3 uses the lastModified field for checking for updates, so it
        # should be no problem putting the old UNIX timestamp style of version
        # number in the version field
        if self.request.cfg.sitename == 'MoinMaster' and pagename == 'BadContent':
            version = int(mtime)
        #######################################################################
            
        return {
            'name': self._outstr(page.page_name),
            'lastModified' : xmlrpclib.DateTime(gmtuple),
            'author': self._outstr(last_edit['editor']),
            'version': version,
            }

    def xmlrpc_getPage(self, pagename):
        """ Invoke xmlrpc_getPageVersion with rev=None """
        return self.xmlrpc_getPageVersion(pagename, rev=None)

    def xmlrpc_getPageVersion(self, pagename, rev):
        """ Get raw text from specific revision of pagename
        
        @param pagename: pagename (utf-8)
        @param rev: revision number (int)
        @rtype: str
        @return: utf-8 encoded page data
        """    
        pagename = self._instr(pagename)

        # User may read page?
        if not self.request.user.may.read(pagename):
            return self.notAllowedFault()

        if rev != None:
            page = Page(self.request, pagename, rev=rev)
        else:
            page = Page(self.request, pagename)

        # Non existing page?
        if not page.exists():
            return self.noSuchPageFault()

        # Return page raw text
        if self.version == 2:
            return self._outstr(page.get_raw_body())
        elif self.version == 1:
            return self._outlob(page.get_raw_body())

    def xmlrpc_getPageHTML(self, pagename):
        """ Invoke xmlrpc_getPageHTMLVersion with rev=None """
        return self.xmlrpc_getPageHTMLVersion(pagename, rev=None)

    def xmlrpc_getPageHTMLVersion(self, pagename, rev):
        """ Get HTML of from specific revision of pagename
        
        @param pagename: the page name (utf-8)
        @param rev: revision number (int)
        @rtype: str
        @return: page in rendered HTML (utf-8)
        """
        pagename = self._instr(pagename)

        # User may read page?
        if not self.request.user.may.read(pagename):
            return self.notAllowedFault()

        if rev != None:
            page = Page(self.request, pagename, rev=rev)
        else:
            page = Page(self.request, pagename)

        # Non existing page?
        if not page.exists():
            return self.noSuchPageFault()
        
        # Render page into a buffer
        result = self.request.redirectedOutput(page.send_page, self.request,
                                               content_only=1)
        
        # Return rendered page
        if self.version == 2:
            return self._outstr(result)
        elif self.version == 1:
            return xmlrpclib.Binary(result)

    def xmlrpc_listLinks(self, pagename):
        """
        list links for a given page
        @param pagename: the page name
        @rtype: list
        @return: links of the page, structs, with the following elements
            * name (string) : The page name or URL the link is to, UTF-8 encoding.
            * type (int) : The link type. Zero (0) for internal Wiki
              link, one (1) for external link (URL - image link, whatever).
        """
        pagename = self._instr(pagename)

        # User may read page?
        if not self.request.user.may.read(pagename):
            return self.notAllowedFault()

        page = Page(self.request, pagename)

        # Non existing page?
        if not page.exists():
            return self.noSuchPageFault()
        
        links_out = []
        for link in page.getPageLinks(self.request):
            links_out.append({ 'name': self._outstr(link), 'type': 0 })
        return links_out

    def xmlrpc_putPage(self, pagename, pagetext):
        """
        save a page / change a page to a new text
        @param pagename: the page name (unicode or utf-8)
        @param pagetext: the new page text (content, unicode or utf-8)
        @rtype: bool
        @return: true on success
        """
        # READ THIS OR IT WILL NOT WORK ===================================
        
        # we use a test page instead of using the requested pagename, if
        # xmlrpc_putpage_enabled was not set in wikiconfig.
        
        if self.request.cfg.xmlrpc_putpage_enabled:
            pagename = self._instr(pagename)
        else:
            pagename = u"PutPageTestPage"

        # By default, only authenticated (trusted) users may use putPage!
        # Trusted currently means being authenticated by http auth.
        # if you also want untrusted users to be able to write pages, then
        # change your wikiconfig to have xmlrpc_putpage_trusted_only = 0
        # and make very very sure that nobody untrusted can access your wiki
        # via network or somebody will raid your wiki some day!
        
        if self.request.cfg.xmlrpc_putpage_trusted_only and not self.request.user.trusted:
            return xmlrpclib.Fault(1, "You are not allowed to edit this page")

        # also check ACLs
        if not self.request.user.may.write(pagename):
            return xmlrpclib.Fault(1, "You are not allowed to edit this page")

        # =================================================================

        page = PageEditor(self.request, pagename)
        try:
            if self.version == 2:
                newtext = self._instr(pagetext)
            elif self.version == 1:
                newtext = self._inlob(pagetext)
            msg = page.saveText(newtext, 0)
        except page.SaveError, msg:
            pass
        if _debug and msg:
            sys.stderr.write("Msg: %s\n" % msg)

        # Update pagelinks cache
        page.getPageLinks(self.request)

        return xmlrpclib.Boolean(1)

    def xmlrpc_searchPages(self, query_string):
        from MoinMoin import search
        query = search.QueryParser().parse_query(query_string)
        results = search.searchPages(self.request, query)
        results.formatter = text_html.Formatter(self.request)
        results.request = self.request
        return [(self._outstr(hit.page_name),
                 self._outstr(results.formatContext(hit, 180, 1)))
                for hit in results.hits]

    def process(self):
        """ xmlrpc v1 and v2 dispatcher """
        try:
            data = self.request.read()
            params, method = xmlrpclib.loads(data)
    
            if _debug:
                sys.stderr.write('- XMLRPC ' + '-' * 70 + '\n')
                sys.stderr.write('%s(%s)\n\n' % (method, repr(params)))
            
            try:
                fn = getattr(self, 'xmlrpc_' + method)
            except AttributeError:
                fn = wikiutil.importPlugin(self.request.cfg, 'xmlrpc', method,
                                           'execute')
                if fn is None:
                    response = xmlrpclib.Fault(1, "No such method: %s." %
                                               method)
                else:
                    response = fn(self, *params)
            else:
                response = fn(*params)
        except:
            # report exception back to server
            response = xmlrpclib.dumps(xmlrpclib.Fault(1, self._dump_exc()))
        else:
            # wrap response in a singleton tuple
            response = (response,)

            # serialize it
            response = xmlrpclib.dumps(response, methodresponse=1)

        self.request.http_headers([
            "Content-Type: text/xml;charset=utf-8",
            "Content-Length: %d" % len(response),
        ])
        self.request.write(response)

        if _debug:
            sys.stderr.write('- XMLRPC ' + '-' * 70 + '\n')
            sys.stderr.write(response + '\n\n')

    # Common faults -----------------------------------------------------
    
    def notAllowedFault(self):
        return xmlrpclib.Fault(1, "You are not allowed to read this page.")

    def noSuchPageFault(self):
        return xmlrpclib.Fault(1, "No such page was found.")        


class XmlRpc1(XmlRpcBase):
    
    def __init__(self, request):
        XmlRpcBase.__init__(self, request)
        self.version = 1

    def _instr(self, text):
        """ Convert string we get from xmlrpc into internal representation

        @param text: quoted text (str or unicode object)
        @rtype: unicode
        @return: text
        """
        if isinstance(text, unicode):
            text = text.encode('utf-8') # ascii should also work
        text = urllib.unquote(text)
        text = unicode(text, 'utf-8')
        return text

    def _outstr(self, text):
        """ Convert string from internal representation to xmlrpc

        @param text: unicode or string in config.charset
        @rtype: str
        @return: text encoded in utf-8 and quoted
        """
        if isinstance(text, unicode):
            text = text.encode('utf-8')
        elif config.charset != 'utf-8':        
            text = unicode(text, config.charset).encode('utf-8')
        text = urllib.quote(text)
        return text

    
class XmlRpc2(XmlRpcBase):
    
    def __init__(self, request):
        XmlRpcBase.__init__(self, request)
        self.version = 2

    def _instr(self, text):
        """ Convert string we get from xmlrpc into internal representation

        @param text: unicode or utf-8 string
        @rtype: unicode
        @return: text
        """
        if not isinstance(text, unicode):
            text = unicode(text, 'utf-8')
        return text

    def _outstr(self, text):
        """ Convert string from internal representation to xmlrpc

        @param text: unicode or string in config.charset
        @rtype: str
        @return: text encoded in utf-8
        """
        if isinstance(text, unicode):
            text = text.encode('utf-8')           
        elif config.charset != 'utf-8':        
            text = unicode(text, config.charset).encode('utf-8')               
        return text


def xmlrpc(request):
    XmlRpc1(request).process()


def xmlrpc2(request):
    XmlRpc2(request).process()

