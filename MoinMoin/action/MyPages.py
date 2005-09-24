# -*- coding: iso-8859-1 -*-
"""
    MoinMoin - MyPages - assisting creation of Homepage subpages

    @copyright: (c) Bastian Blank, Florian Festi, Thomas Waldmann
    @license: GNU GPL, see COPYING for details.
"""

def execute(pagename, request):
    from MoinMoin import wikiutil
    from MoinMoin.Page import Page

    _ = request.getText
    thispage = Page(request, pagename)
    
    if request.user.valid:
        username = request.user.name
    else:
        username = ''

    if not username:
        return thispage.send_page(request,
            msg = _('Please log in first.'))

    userhomewiki = request.cfg.user_homewiki
    if userhomewiki != 'Self' and userhomewiki != request.cfg.interwikiname:
        interwiki = wikiutil.getInterwikiHomePage(request, username=username)
        wikitag, wikiurl, wikitail, wikitag_bad = wikiutil.resolve_wiki(request, '%s:%s' % interwiki)
        wikiurl = wikiutil.mapURL(request, wikiurl)
        homepageurl = wikiutil.join_wiki(wikiurl, wikitail)
        request.http_redirect('%s?action=MyPages' % homepageurl)
        
    homepage = Page(request, username)
    if not homepage.exists():
        return homepage.send_page(request,
            msg = _('Please first create a homepage before creating additional pages.'))

    pagecontent = _("""\
You can add some additional sub pages to your already existing homepage here.

You can choose how open to other readers or writers those pages shall be,
access is controlled by group membership of the corresponding group page.

Just enter the sub page's name and click on the button to create a new page.

Before creating access protected pages, make sure the corresponding group page
exists and has the appropriate members in it. Use HomepageGroupsTemplate for creating
the group pages.

||'''Add a new personal page:'''||'''Related access control list group:'''||
||[[NewPage(HomepageReadWritePageTemplate,read-write page,%(username)s)]]||["%(username)s/ReadWriteGroup"]||
||[[NewPage(HomepageReadPageTemplate,read-only page,%(username)s)]]||["%(username)s/ReadGroup"]||
||[[NewPage(HomepagePrivatePageTemplate,private page,%(username)s)]]||%(username)s only||

""") % locals()

    pagecontent = pagecontent.replace('\n', '\r\n')

    from MoinMoin.Page import Page
    from MoinMoin.parser.wiki import Parser
    from MoinMoin.formatter.text_html import Formatter
    pagename = username
    request.http_headers()
    
    # This action generate data using the user language
    request.setContentLanguage(request.lang)

    wikiutil.send_title(request, _('MyPages management'), pagename=pagename)
        
    # Start content - IMPORTANT - without content div, there is no
    # direction support!
    request.write(request.formatter.startContent("content"))

    parser = Parser(pagecontent, request)
    formatter = Formatter(request)
    reqformatter = None
    if hasattr(request, 'formatter'):
        reqformatter = request.formatter
    request.formatter = formatter
    p = Page(request, "$$$")
    formatter.setPage(p)
    parser.format(formatter)
    if reqformatter == None:
        del request.formatter
    else:
        request.formatter = reqformatter

    # End content and send footer
    request.write(request.formatter.endContent())
    wikiutil.send_footer(request, pagename)

