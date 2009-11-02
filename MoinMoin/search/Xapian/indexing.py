# -*- coding: iso-8859-1 -*-
"""
    MoinMoin - xapian search engine indexing

    @copyright: 2006-2008 MoinMoin:ThomasWaldmann,
                2006 MoinMoin:FranzPletz
                2009 MoinMoin:DmitrijsMilajevs
    @license: GNU GPL, see COPYING for details.
"""

import os, re
import xapian

from MoinMoin import log
logging = log.getLogger(__name__)

from MoinMoin.support import xappy
from MoinMoin.search.builtin import BaseIndex
from MoinMoin.search.Xapian.tokenizer import WikiAnalyzer

from MoinMoin.Page import Page
from MoinMoin import config, wikiutil


class Query(xapian.Query):
    pass


class UnicodeQuery(xapian.Query):
    """ Xapian query object which automatically encodes unicode strings """

    def __init__(self, *args, **kwargs):
        """
        @keyword encoding: specifiy the encoding manually (default: value of config.charset)
        """
        self.encoding = kwargs.get('encoding', config.charset)

        nargs = []
        for term in args:
            if isinstance(term, unicode):
                term = term.encode(self.encoding)
            elif isinstance(term, list) or isinstance(term, tuple):
                term = [t.encode(self.encoding) for t in term]
            nargs.append(term)

        Query.__init__(self, *nargs, **kwargs)


class MoinSearchConnection(xappy.SearchConnection):

    def get_all_documents(self):
        """
        Return all the documents in the xapian index.
        """
        document_count = self.get_doccount()
        query = self.query_all()
        hits = self.search(query, 0, document_count)
        return hits

    def get_all_documents_with_field(self, field, field_value):
        document_count = self.get_doccount()
        query = self.query_field(field, field_value)
        hits = self.search(query, 0, document_count)
        return hits


class MoinIndexerConnection(xappy.IndexerConnection):

    def __init__(self, *args, **kwargs):

        super(MoinIndexerConnection, self).__init__(*args, **kwargs)

        self._define_fields_actions()

    def _define_fields_actions(self):
        SORTABLE = xappy.FieldActions.SORTABLE
        INDEX_EXACT = xappy.FieldActions.INDEX_EXACT
        INDEX_FREETEXT = xappy.FieldActions.INDEX_FREETEXT
        STORE_CONTENT = xappy.FieldActions.STORE_CONTENT

        self.add_field_action('wikiname', INDEX_EXACT)
        self.add_field_action('wikiname', STORE_CONTENT)
        self.add_field_action('pagename', INDEX_EXACT)
        self.add_field_action('pagename', STORE_CONTENT)
        self.add_field_action('pagename', SORTABLE)
        self.add_field_action('attachment', INDEX_EXACT)
        self.add_field_action('attachment', STORE_CONTENT)
        self.add_field_action('mtime', INDEX_EXACT)
        self.add_field_action('mtime', STORE_CONTENT)
        self.add_field_action('revision', STORE_CONTENT)
        self.add_field_action('revision', INDEX_EXACT)
        self.add_field_action('mimetype', INDEX_EXACT)
        self.add_field_action('mimetype', STORE_CONTENT)
        self.add_field_action('title', INDEX_FREETEXT, weight=100)
        self.add_field_action('title', STORE_CONTENT)
        self.add_field_action('content', INDEX_FREETEXT, spell=True)
        self.add_field_action('fulltitle', INDEX_EXACT)
        self.add_field_action('fulltitle', STORE_CONTENT)
        self.add_field_action('domain', INDEX_EXACT)
        self.add_field_action('domain', STORE_CONTENT)
        self.add_field_action('lang', INDEX_EXACT)
        self.add_field_action('lang', STORE_CONTENT)
        self.add_field_action('stem_lang', INDEX_EXACT)
        self.add_field_action('author', INDEX_EXACT)
        self.add_field_action('linkto', INDEX_EXACT)
        self.add_field_action('linkto', STORE_CONTENT)
        self.add_field_action('category', INDEX_EXACT)
        self.add_field_action('category', STORE_CONTENT)


class StemmedField(xappy.Field):

    def __init__(self, name, value, request):

        analyzer = WikiAnalyzer(request=request, language=request.cfg.language_default)

        value = ' '.join(unicode('%s %s' % (word, stemmed)).strip() for word, stemmed in analyzer.tokenize(value))

        super(StemmedField, self).__init__(name, value)


class XapianIndex(BaseIndex):


    def _main_dir(self):
        """ Get the directory of the xapian index """
        if self.request.cfg.xapian_index_dir:
            return os.path.join(self.request.cfg.xapian_index_dir,
                    self.request.cfg.siteid)
        else:
            return os.path.join(self.request.cfg.cache_dir, 'xapian')

    def exists(self):
        """ Check if the Xapian index exists """
        return BaseIndex.exists(self) and os.listdir(self.dir)

    def _search(self, query, sort='weight', historysearch=0):
        """
        Perform the search using xapian (read-lock acquired)

        @param query: the search query objects
        @keyword sort: the sorting of the results (default: 'weight')
        @keyword historysearch: whether to search in all page revisions (default: 0) TODO: use/implement this
        """
        while True:
            try:
                searcher, timestamp = self.request.cfg.xapian_searchers.pop()
                if timestamp != self.mtime():
                    searcher.close()
                else:
                    break
            except IndexError:
                searcher = MoinSearchConnection(self.dir)
                timestamp = self.mtime()
                break

        # Refresh connection, since it may be outdated.
        searcher.reopen()
        query = query.xapian_term(self.request, searcher)

        # Get maximum possible amount of hits from xappy, which is number of documents in the index.
        document_count = searcher.get_doccount()

        kw = {}
        if sort == 'page_name':
            kw['sortby'] = 'pagename'

        hits = searcher.search(query, 0, document_count, **kw)

        self.request.cfg.xapian_searchers.append((searcher, timestamp))
        return hits

    def _do_queued_updates(self, request, amount=5):
        """ Assumes that the write lock is acquired """
        self.touch()
        connection = MoinIndexerConnection(self.dir)
        # do all page updates
        pages = self.update_queue.pages()[:amount]
        for name in pages:
            self._index_page(request, connection, name, mode='update')
            self.update_queue.remove([name])

        # do page/attachment removals
        items = self.remove_queue.pages()[:amount]
        for item in items:
            assert len(item.split('//')) == 2
            pagename, attachment = item.split('//')
            page = Page(request, pagename)
            self._remove_item(request, connection, page, attachment)
            self.remove_queue.remove([item])

        connection.close()

    def _get_document(self, connection, doc_id, mtime, mode):
        do_index = False

        if mode == 'update':
            try:
                doc = connection.get_document(doc_id)
                docmtime = long(doc.data['mtime'][0])
            except KeyError:
                do_index = True
            else:
                do_index = mtime > docmtime
        elif mode == 'add':
            do_index = True

        if do_index:
            document = xappy.UnprocessedDocument()
            document.id = doc_id
        else:
            document = None
        return document

    def _add_fields_to_document(self, request, document, fields=None, multivalued_fields=None):

        fields_to_stem = ['title', 'content']

        if fields is None:
            fields = {}
        if multivalued_fields is None:
            multivalued_fields = {}

        for field, value in fields.iteritems():
            document.fields.append(xappy.Field(field, value))
            if field in fields_to_stem:
                document.fields.append(StemmedField(field, value, request))

        for field, values in multivalued_fields.iteritems():
            for value in values:
                document.fields.append(xappy.Field(field, value))

    def _index_file(self, request, connection, filename, mode='update'):
        """ index a file as it were a page named pagename
            Assumes that the write lock is acquired
        """
        fields = {}
        multivalued_fields = {}

        wikiname = request.cfg.interwikiname or u"Self"
        fs_rootpage = 'FS' # XXX FS hardcoded

        try:
            itemid = "%s:%s" % (wikiname, os.path.join(fs_rootpage, filename))
            mtime = wikiutil.timestamp2version(os.path.getmtime(filename))

            doc = self._get_document(connection, itemid, mtime, mode)
            logging.debug("%s %r" % (filename, doc))

            if doc:
                mimetype, file_content = self.contentfilter(filename)

                fields['wikiname'] = wikiname
                fields['pagename'] = fs_rootpage
                fields['attachment'] = filename # XXX we should treat files like real pages, not attachments
                fields['mtime'] = str(mtime)
                fields['revision'] = '0'
                fields['title'] = " ".join(os.path.join(fs_rootpage, filename).split("/"))
                fields['content'] = file_content

                multivalued_fields['mimetype'] = [mt for mt in [mimetype] + mimetype.split('/')]

                self._add_fields_to_document(request, doc, fields, multivalued_fields)

                connection.replace(doc)

        except (OSError, IOError, UnicodeError):
            logging.exception("_index_file crashed:")

    def _get_languages(self, page):
        """ Get language of a page and the language to stem it in

        @param page: the page instance
        """
        lang = None
        default_lang = page.request.cfg.language_default

        # if we should stem, we check if we have stemmer for the language available
        if page.request.cfg.xapian_stemming:
            lang = page.pi['language']
            try:
                xapian.Stem(lang)
                # if there is no exception, lang is stemmable
                return (lang, lang)
            except xapian.InvalidArgumentError:
                # lang is not stemmable
                pass

        if not lang:
            # no lang found at all.. fallback to default language
            lang = default_lang

        # return actual lang and lang to stem in
        return (lang, default_lang)

    def _get_categories(self, page):
        """ Get all categories the page belongs to through the old
            regular expression

        @param page: the page instance
        """
        body = page.get_raw_body()

        prev, next = (0, 1)
        pos = 0
        while next:
            if next != 1:
                pos += next.end()
            prev, next = next, re.search(r'-----*\s*\r?\n', body[pos:])

        if not prev or prev == 1:
            return []
        # for CategoryFoo, group 'all' matched CategoryFoo, group 'key' matched just Foo
        return [m.group('all') for m in self.request.cfg.cache.page_category_regex.finditer(body[pos:])]

    def _get_domains(self, page):
        """ Returns a generator with all the domains the page belongs to

        @param page: page
        """
        if page.isUnderlayPage():
            yield 'underlay'
        if page.isStandardPage():
            yield 'standard'
        if wikiutil.isSystemPage(self.request, page.page_name):
            yield 'system'

    def _index_page(self, request, connection, pagename, mode='update'):
        """ Index a page - assumes that the write lock is acquired

        @arg connection: the Indexer connection object
        @arg pagename: a page name
        @arg mode: 'add' = just add, no checks
                   'update' = check if already in index and update if needed (mtime)
        """
        page = Page(request, pagename)
        if request.cfg.xapian_index_history:
            for rev in page.getRevList():
                updated = self._index_page_rev(request, connection, Page(request, pagename, rev=rev), mode=mode)
                logging.debug("updated page %r rev %d (updated==%r)" % (pagename, rev, updated))
                if not updated:
                    # we reached the revisions that are already present in the index
                    break
        else:
            self._index_page_rev(request, connection, page, mode=mode)

        self._index_attachments(request, connection, pagename, mode)

    def _index_attachments(self, request, connection, pagename, mode='update'):
        from MoinMoin.action import AttachFile

        fields = {}
        multivalued_fields = {}

        wikiname = request.cfg.interwikiname or u"Self"
        page = Page(request, pagename)

        for att in AttachFile._get_files(request, pagename):
            itemid = "%s:%s//%s" % (wikiname, pagename, att)
            filename = AttachFile.getFilename(request, pagename, att)
            mtime = wikiutil.timestamp2version(os.path.getmtime(filename))

            doc = self._get_document(connection, itemid, mtime, mode)
            logging.debug("%s %s %r" % (pagename, att, doc))

            if doc:
                mimetype, att_content = self.contentfilter(filename)

                fields['wikiname'] = wikiname
                fields['pagename'] = pagename
                fields['attachment'] = att
                fields['mtime'] = str(mtime)
                fields['revision'] = '0'
                fields['title'] = '%s/%s' % (pagename, att)
                fields['content'] = att_content
                fields['fulltitle'] = pagename
                fields['lang'], fields['stem_lang'] = self._get_languages(page)

                multivalued_fields['mimetype'] = [mt for mt in [mimetype] + mimetype.split('/')]
                multivalued_fields['domain'] = self._get_domains(page)

                self._add_fields_to_document(request, doc, fields, multivalued_fields)

                connection.replace(doc)

    def _index_page_rev(self, request, connection, page, mode='update'):
        """ Index a page revision - assumes that the write lock is acquired

        @arg connection: the Indexer connection object
        @arg page: a page object
        @arg mode: 'add' = just add, no checks
                   'update' = check if already in index and update if needed (mtime)
        """
        request.page = page
        pagename = page.page_name

        fields = {}
        multivalued_fields = {}

        wikiname = request.cfg.interwikiname or u"Self"
        revision = str(page.get_real_rev())
        itemid = "%s:%s:%s" % (wikiname, pagename, revision)
        mtime = page.mtime_usecs()

        doc = self._get_document(connection, itemid, mtime, mode)
        logging.debug("%s %r" % (pagename, doc))

        if doc:
            mimetype = 'text/%s' % page.pi['format']  # XXX improve this

            fields['wikiname'] = wikiname
            fields['pagename'] = pagename
            fields['attachment'] = '' # this is a real page, not an attachment
            fields['mtime'] = str(mtime)
            fields['revision'] = revision
            fields['title'] = pagename
            fields['content'] = page.get_raw_body()
            fields['fulltitle'] = pagename
            fields['lang'], fields['stem_lang'] = self._get_languages(page)
            fields['author'] = page.edit_info().get('editor', '?')

            multivalued_fields['mimetype'] = [mt for mt in [mimetype] + mimetype.split('/')]
            multivalued_fields['domain'] = self._get_domains(page)
            multivalued_fields['linkto'] = page.getPageLinks(request)
            multivalued_fields['category'] = self._get_categories(page)

            self._add_fields_to_document(request, doc, fields, multivalued_fields)

            try:
                connection.replace(doc)
            except xappy.IndexerError, err:
                logging.warning("IndexerError at %r %r %r (%s)" % (
                    wikiname, pagename, revision, str(err)))

        return bool(doc)

    def _remove_item(self, request, connection, page, attachment=None):
        wikiname = request.cfg.interwikiname or u'Self'
        pagename = page.page_name

        if not attachment:

            search_connection = MoinSearchConnection(self.dir)
            docs_to_delete = search_connection.get_all_documents_with_field('fulltitle', pagename)
            ids_to_delete = [d.id for d in docs_to_delete]
            search_connection.close()

            for id_ in ids_to_delete:
                connection.delete(id_)
                logging.debug('%s removed from xapian index' % pagename)
        else:
            # Only remove a single attachment
            id_ = "%s:%s//%s" % (wikiname, pagename, attachment)
            connection.delete(id_)

            logging.debug('attachment %s from %s removed from index' % (attachment, pagename))

    def _index_pages(self, request, files=None, mode='update', pages=None):
        """ Index pages (and all given files)

        This should be called from indexPages or indexPagesInNewThread only!

        This may take some time, depending on the size of the wiki and speed
        of the machine.

        When called in a new thread, lock is acquired before the call,
        and this method must release it when it finishes or fails.

        @param request: the current request
        @param files: an optional list of files to index
        @param mode: how to index the files, either 'add', 'update' or 'rebuild'
        @param pages: list of pages to index, if not given, all pages are indexed

        """
        if pages is None:
            # Index all pages
            pages = request.rootpage.getPageList(user='', exists=1)

        # rebuilding the DB: delete it and add everything
        if mode == 'rebuild':
            for fname in os.listdir(self.dir):
                os.unlink(os.path.join(self.dir, fname))
            mode = 'add'

        connection = MoinIndexerConnection(self.dir)
        try:
            self.touch()
            logging.debug("indexing all (%d) pages..." % len(pages))
            for pagename in pages:
                self._index_page(request, connection, pagename, mode=mode)
            if files:
                logging.debug("indexing all files...")
                for fname in files:
                    fname = fname.strip()
                    self._index_file(request, connection, fname, mode)
            connection.flush()
        finally:
            connection.close()
