# -*- coding: iso-8859-1 -*-
"""
    MoinMoin - Call the GUI editor (FCKeditor)

    @copyright: (c) Bastian Blank, Florian Festi, Thomas Waldmann
    @license: GNU GPL, see COPYING for details.
"""

from MoinMoin import PageEditor
import os, time, codecs, re

from MoinMoin import caching, config, user, util, wikiutil, error
from MoinMoin.Page import Page
from MoinMoin.widget import html
from MoinMoin.widget.dialog import Status
from MoinMoin.logfile import editlog, eventlog
from MoinMoin.util import filesys
import MoinMoin.util.web
import MoinMoin.util.mail
import MoinMoin.util.datetime
from MoinMoin.parser.wiki import Parser

from StringIO import StringIO

def execute(pagename, request):
    if not request.user.may.write(pagename):
        _ = request.getText
        Page(request, pagename).send_page(request,
            msg = _('You are not allowed to edit this page.'))
        return

    PageGraphicalEditor(request, pagename).sendEditor()


class PageGraphicalEditor(PageEditor.PageEditor):

    def word_rule(self):
        regex = re.compile(r"\(\?<![^)]*?\)")
        word_rule = regex.sub("", Parser.word_rule)
        return repr(word_rule)[1:]
    
    def sendEditor(self, **kw):
        """
        Send the editor form page.

        @keyword preview: if given, show this text in preview mode
        @keyword staytop: don't go to #preview
        @keyword comment: comment field (when preview is true)
        """
        from MoinMoin import i18n
        try:
            from MoinMoin.action import SpellCheck
        except ImportError:
            SpellCheck = None

        request = self.request
        form = self.request.form
        _ = self._
        self.request.http_headers(self.request.nocache)

        msg = None
        conflict_msg = None
        edit_lock_message = None
        preview = kw.get('preview', None)
        staytop = kw.get('staytop', 0)

        # check edit permissions
        if not self.request.user.may.write(self.page_name):
            msg = _('You are not allowed to edit this page.')
        elif not self.isWritable():
            msg = _('Page is immutable!')
        elif self.rev:
            # Trying to edit an old version, this is not possible via
            # the web interface, but catch it just in case...
            msg = _('Cannot edit old revisions!')
        else:
            # try to acquire edit lock
            ok, edit_lock_message = self.lock.acquire()
            if not ok:
                # failed to get the lock
                if preview is not None:
                    edit_lock_message = _('The lock you held timed out, be prepared for editing conflicts!'
                        ) + "<br>" + edit_lock_message
                else:
                    msg = edit_lock_message

        # Did one of the prechecks fail?
        if msg:
            self.send_page(self.request, msg=msg)
            return

        # Check for preview submit
        if preview is None:
            title = _('Edit "%(pagename)s"')
        else:
            title = _('Preview of "%(pagename)s"')
            self.set_raw_body(preview, modified=1)

        # send header stuff
        lock_timeout = self.lock.timeout / 60
        lock_page = wikiutil.escape(self.page_name, quote=1)
        lock_expire = _("Your edit lock on %(lock_page)s has expired!") % {'lock_page': lock_page}
        lock_mins = _("Your edit lock on %(lock_page)s will expire in # minutes.") % {'lock_page': lock_page}
        lock_secs = _("Your edit lock on %(lock_page)s will expire in # seconds.") % {'lock_page': lock_page}
                
        # get request parameters
        try:
            text_rows = int(form['rows'][0])
        except StandardError:
            text_rows = self.cfg.edit_rows
            if self.request.user.valid:
                text_rows = int(self.request.user.edit_rows)

        if preview is not None:
            # Propagate original revision
            rev = int(form['rev'][0])
            
            # Check for editing conflicts
            if not self.exists():
                # page does not exist, are we creating it?
                if rev:
                    conflict_msg = _('Someone else deleted this page while you were editing!')
            elif rev != self.current_rev():
                conflict_msg = _('Someone else changed this page while you were editing!')
                if self.mergeEditConflict(rev):
                    conflict_msg = _("""Someone else saved this page while you were editing!
Please review the page and save then. Do not save this page as it is!
Have a look at the diff of %(difflink)s to see what has been changed.""") % {
                        'difflink': self.link_to(self.request,
                                                 querystr='action=diff&rev=%d' % rev)
                        }
                    rev = self.current_rev()
            if conflict_msg:
                # We don't show preview when in conflict
                preview = None
                
        elif self.exists():
            # revision of existing page
            rev = self.current_rev()
        else:
            # page creation
            rev = 0

        # Page editing is done using user language
        self.request.setContentLanguage(self.request.lang)

        # Setup status message
        status = [kw.get('msg', ''), conflict_msg, edit_lock_message]
        status = [msg for msg in status if msg]
        status = ' '.join(status)
        status = Status(self.request, content=status)
        
        wikiutil.send_title(self.request,
            title % {'pagename': self.split_title(self.request),},
            page=self,
            pagename=self.page_name, msg=status,
            body_onload=self.lock.locktype and 'countdown()' or '', # broken / bug in Mozilla 1.5, when using #preview
            html_head=self.lock.locktype and (
                PageEditor._countdown_js % {
                     'lock_timeout': lock_timeout,
                     'lock_expire': lock_expire,
                     'lock_mins': lock_mins,
                     'lock_secs': lock_secs,
                    }) or '',
            editor_mode=1,
        )
        
        self.request.write(self.request.formatter.startContent("content"))
        
        # get the text body for the editor field
        if form.has_key('template'):
            # "template" parameter contains the name of the template page
            template_page = wikiutil.unquoteWikiname(form['template'][0])
            if self.request.user.may.read(template_page):
                raw_body = Page(self.request, template_page).get_raw_body()
                if raw_body:
                    self.request.write(_("[Content of new page loaded from %s]") % (template_page,), '<br>')
                else:
                    self.request.write(_("[Template %s not found]") % (template_page,), '<br>')
            else:
                raw_body = ''
                self.request.write(_("[You may not read %s]") % (template_page,), '<br>')
        else:
            raw_body = self.get_raw_body()

        # Make backup on previews - but not for new empty pages
        if preview and raw_body:
            self._make_backup(raw_body)

        # Generate default content for new pages
        if not raw_body:
            raw_body = _('Describe %s here.') % (self.page_name,)

        # send text above text area
        #self.request.write('<p>')
        #self.request.write(wikiutil.getSysPage(self.request, 'HelpOnFormatting').link_to(self.request))
        #self.request.write(" | ", wikiutil.getSysPage(self.request, 'InterWiki').link_to(self.request))
        #if preview is not None and not staytop:
        #    self.request.write(' | <a href="#preview">%s</a>' % _('Skip to preview'))
        #self.request.write(' ')
        #self.request.write(_('[current page size \'\'\'%(size)d\'\'\' bytes]') % {'size': self.size()})
        #self.request.write('</p>')
        
        # send form
        self.request.write('<form id="editor" method="post" action="%s/%s#preview">' % (
            self.request.getScriptname(),
            wikiutil.quoteWikinameURL(self.page_name),
            ))

        # yet another weird workaround for broken IE6 (it expands the text
        # editor area to the right after you begin to type...). IE sucks...
        # http://fplanque.net/2003/Articles/iecsstextarea/
        self.request.write('<fieldset style="border:none;padding:0;">')
        
        self.request.write(unicode(html.INPUT(type="hidden", name="action", value="edit")))

        # Send revision of the page our edit is based on
        self.request.write('<input type="hidden" name="rev" value="%d">' % (rev,))

        # Save backto in a hidden input
        backto = form.get('backto', [None])[0]
        if backto:
            self.request.write(unicode(html.INPUT(type="hidden", name="backto", value=backto)))

        # button bar
        button_spellcheck = (SpellCheck and
            '<input class="button" type="submit" name="button_spellcheck" value="%s">'
                % _('Check Spelling')) or ''

        save_button_text = _('Save Changes')
        cancel_button_text = _('Cancel')
        
        if self.cfg.page_license_enabled:
            self.request.write('<p><em>', _(
"""By hitting '''%(save_button_text)s''' you put your changes under the %(license_link)s.
If you don't want that, hit '''%(cancel_button_text)s''' to cancel your changes.""") % {
                'save_button_text': save_button_text,
                'cancel_button_text': cancel_button_text,
                'license_link': wikiutil.getSysPage(self.request, self.cfg.page_license_page).link_to(self.request),
            }, '</em></p>')

        self.request.write('''
<input class="button" type="submit" name="button_save" value="%s">
<input class="button" type="submit" name="button_preview" value="%s">
<input class="button" type="submit" name="button_switch" value="%s">
%s
<input class="button" type="submit" name="button_cancel" value="%s">
<input type="hidden" name="editor" value="gui">
''' % (save_button_text, _('Preview'), _('Text mode'), button_spellcheck, cancel_button_text,))

        # Add textarea with page text

        # TODO: currently self.language is None at this point. We have
        # to do processing instructions parsing earlier, or move page
        # language into meta file.
        lang = self.language or self.request.cfg.default_lang
        contentlangdirection = i18n.getDirection(lang) # 'ltr' or 'rtl'
        uilanguage = self.request.lang
        url_prefix = self.request.cfg.url_prefix
        wikipage = wikiutil.quoteWikinameURL(self.page_name)
        fckbasepath = url_prefix + '/applets/FCKeditor'
        wikiurl = request.getScriptname()
        if not wikiurl or wikiurl[-1] != '/':
            wikiurl += '/'
        themepath = '%s/%s' % (url_prefix, self.request.theme.name)
        smileypath = themepath + '/img'
        # auto-generating a list for SmileyImages does NOT work from here!
        editor_size = int(request.user.edit_rows) * 22 # 22 height_pixels/line
        word_rule = self.word_rule() 

        self.request.write("""
<script type="text/javascript" src="%(fckbasepath)s/fckeditor.js"></script>
<script type="text/javascript">
<!--
    var oFCKeditor = new FCKeditor( 'savetext', '100%%', %(editor_size)s, 'MoinDefault' ) ;
    oFCKeditor.BasePath= '%(fckbasepath)s/' ;
    oFCKeditor.Config['WikiBasePath'] = '%(wikiurl)s' ;
    oFCKeditor.Config['WikiPage'] = '%(wikipage)s' ;
    oFCKeditor.Config['PluginsPath'] = '%(url_prefix)s/applets/moinFCKplugins/' ;
    oFCKeditor.Config['CustomConfigurationsPath'] = '%(url_prefix)s/applets/moinfckconfig.js'  ;
    oFCKeditor.Config['WordRule'] = %(word_rule)s ;
    oFCKeditor.Config['SmileyPath'] = '%(smileypath)s/' ;
    oFCKeditor.Config['EditorAreaCSS'] = '%(themepath)s/css/common.css' ;
    oFCKeditor.Config['SkinPath'] = '%(fckbasepath)s/editor/skins/silver/' ;
    oFCKeditor.Config['AutoDetectLanguage'] = false ;
    oFCKeditor.Config['DefaultLanguage'] = '%(uilanguage)s' ;
    oFCKeditor.Config['ContentLangDirection']  = '%(contentlangdirection)s' ;
    oFCKeditor.Value= """ % locals())

        from MoinMoin.formatter.text_gedit import Formatter
        self.formatter = Formatter(request)
        self.formatter.page = self

        output = StringIO()
        request.redirect(output)
        self.send_page_content(request, Parser, raw_body, do_cache=False)
        request.redirect()
        output = repr(output.getvalue())
        if output[0] == 'u':
            output = output[1:]
        request.write(output)
        self.request.write(""" ;
    oFCKeditor.Create() ;
//-->
</script>
""")
        self.request.write("<p>")
        self.request.write(_("Comment:"),
            ' <input id="editor-comment" type="text" name="comment" value="%s" maxlength="80">' % (
                wikiutil.escape(kw.get('comment', ''), 1), ))
        self.request.write("</p>")

        # Category selection
        filter = re.compile(self.cfg.page_category_regex, re.UNICODE).search
        cat_pages = self.request.rootpage.getPageList(filter=filter)
        cat_pages.sort()
        cat_pages = [wikiutil.pagelinkmarkup(p) for p in cat_pages]
        cat_pages.insert(0, ('', _('<No addition>', formatted=False)))
        self.request.write("<p>")
        self.request.write(_('Add to: %(category)s') % {
            'category': unicode(util.web.makeSelection('category', cat_pages)),
        })
        if self.cfg.mail_enabled:
            self.request.write('''
&nbsp;
<input type="checkbox" name="trivial" id="chktrivial" value="1" %(checked)s>
<label for="chktrivial">%(label)s</label> ''' % {
                'checked': ('', 'checked')[form.get('trivial',['0'])[0] == '1'],
                'label': _("Trivial change"),
                })

        self.request.write('''
&nbsp;
<input type="checkbox" name="rstrip" id="chkrstrip" value="1" %(checked)s>
<label for="chkrstrip">%(label)s</label>
</p> ''' % {
            'checked': ('', 'checked')[form.get('rstrip',['0'])[0] == '1'],
            'label': _('Remove trailing whitespace from each line')
            })

        self.request.write("</p>")

        badwords_re = None
        if preview is not None:
            if SpellCheck and (
                    form.has_key('button_spellcheck') or
                    form.has_key('button_newwords')):
                badwords, badwords_re, msg = SpellCheck.checkSpelling(self, self.request, own_form=0)
                self.request.write("<p>%s</p>" % msg)
        self.request.write('</fieldset>')
        self.request.write("</form>")
        

        if preview is not None:
            if staytop:
                content_id = 'previewbelow'
            else:
                content_id = 'preview'
            self.send_page(self.request, content_id=content_id, content_only=1,
                           hilite_re=badwords_re)

        self.request.write(self.request.formatter.endContent()) # end content div
        self.request.theme.emit_custom_html(self.cfg.page_footer1)
        self.request.theme.emit_custom_html(self.cfg.page_footer2)
