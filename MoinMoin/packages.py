# -*- coding: iso-8859-1 -*-
"""
    MoinMoin - Package Installer

    @copyright: 2005 by MoinMoin:AlexanderSchremmer
    @license: GNU GPL, see COPYING for details.
"""

import os
import sys
import zipfile

from MoinMoin import config, wikiutil, caching
from MoinMoin.Page import Page
from MoinMoin.PageEditor import PageEditor

MOIN_PACKAGE_FILE = 'MOIN_PACKAGE'
MAX_VERSION = 1

# Exceptions
class PackageException(Exception):
    """ Raised if the package is broken. """
    pass

class ScriptException(Exception):
    """ Raised when there is a problem in the script. """

    def __unicode__(self):
        """ Return unicode error message """
        if isinstance(self.args[0], str):
            return unicode(self.args[0], config.charset)
        else:
            return unicode(self.args[0])

class RuntimeScriptException(ScriptException):
    """ Raised when the script problem occurs at runtime. """

class ScriptExit(Exception):
    """ Raised by the script commands when the script should quit. """

# Parsing and (un)quoting for script files
def packLine(list):
    return '|'.join([x.replace('\\', '\\\\').replace('|', r'\|') for x in list])

def unpackLine(string):
    result = []
    token = None
    escaped = False
    for x in string:
        if token is None:
            token = ""
        if escaped and x in ('\\', '|'):
            token += x
            escaped = False
            continue
        escaped = (x == '\\')
        if escaped:
            continue
        if x == '|':
            result.append(token)
            token = ""
        else:
            token += x
    if token is not None:
        result.append(token)
    return result

class ScriptEngine:
    """
    The script engine supplies the needed commands to execute the installation
    script.
    """

    def _toBoolean(string):
        """
        Converts the parameter to a boolean value by recognising different
        truth literals.
        """
        return (string.lower() in ('yes', 'true', '1'))
    _toBoolean = staticmethod(_toBoolean)

    def _extractToFile(self, source, target):
        """ Extracts source and writes the contents into target. """
        # TODO, add file dates
        f = open(target, "wb")
        f.write(self.extract_file(source))
        f.close()

    def __init__(self):
        self.themename = None
        self.ignoreExceptions = False
        self.goto = 0

    def do_print(self, *param):
        """ Prints the parameters into output of the script. """
        self.msg += '; '.join(param) + "\n"

    def do_exit(self):
        """ Exits the script. """
        raise ScriptExit

    def do_ignoreexceptions(self, boolean):
        """ Sets the ignore exceptions setting. If exceptions are ignored, the
        script does not stop if one is encountered. """
        self.ignoreExceptions = self._toBoolean(boolean)

    def do_ensureversion(self, version, lines=0):
        """ Ensures that the version of MoinMoin is greater or equal than
            version. If lines is unspecified, the script aborts. Otherwise,
            the next lines (amount specified by lines) are not executed.

        @param version: required version of MoinMoin (e.g. "1.3.4")
        @param lines:   lines to ignore
        """
        from MoinMoin.version import release
        version_int = [int(x) for x in version.split(".")]
        release = [int(x) for x in release.split(".")]
        if version_int > release:
            if lines > 0:
                self.goto = lines
            else:
                raise RuntimeScriptException(_("The package needs a newer version"
                                               " of MoinMoin (at least %s).") %
                                             version)

    def do_setthemename(self, themename):
        """ Sets the name of the theme which will be altered next. """
        self.themename = wikiutil.taintfilename(str(themename))

    def do_copythemefile(self, filename, type, target):
        """ Copies a theme-related file (CSS, PNG, etc.) into a directory of the
        current theme.

        @param filename: name of the file in this package
        @param type:   the subdirectory of the theme directory, e.g. "css"
        @param target: filename, e.g. "screen.css"
        """
        _ = self.request.getText
        if self.themename is None:
            raise RuntimeScriptException(_("The theme name is not set."))
        sa = getattr(self.request, "sareq", None)
        if sa is None:
            raise RuntimeScriptException(_("Installing theme files is only supported "
                                           "for standalone type servers."))
        htdocs_dir = sa.server.htdocs
        theme_file = os.path.join(htdocs_dir, self.themename,
                                  wikiutil.taintfilename(type),
                                  wikiutil.taintfilename(target))
        theme_dir = os.path.dirname(theme_file)
        if not os.path.exists(theme_dir):
            os.makedirs(theme_dir, 0777 & config.umask)
        self._extractToFile(filename, theme_file)

    def do_installplugin(self, filename, visibility, ptype, target):
        """
        Installs a python code file into the appropriate directory.

        @param filename: name of the file in this package
        @param visibility: 'local' will copy it into the plugin folder of the
            current wiki. 'global' will use the folder of the MoinMoin python
            package.
        @param ptype: the type of the plugin, e.g. "parser"
        @param target: the filename of the plugin, e.g. wiki.py
        """
        visibility = visibility.lower()
        ptype = wikiutil.taintfilename(ptype.lower())

        if visibility == 'global':
            basedir = os.path.dirname(__import__("MoinMoin").__file__)
        elif visibility == 'local':
            basedir = self.request.cfg.plugin_dir

        target = os.path.join(basedir, ptype, wikiutil.taintfilename(target))

        self._extractToFile(filename, target)
        wikiutil._wiki_plugins = {}

    def do_installpackage(self, pagename, filename):
        """
        Installs a package.

        @param pagename: Page where the file is attached. Or in 2.0, the file itself.
        @param filename: Filename of the attachment (just applicable for MoinMoin < 2.0)
        """
        _ = self.request.getText

        attachments = Page(self.request, pagename).getPagePath("attachments", check_create=0)
        package = ZipPackage(self.request, os.path.join(attachments, wikiutil.taintfilename(filename)))

        if package.isPackage():
            if not package.installPackage():
                raise RuntimeScriptException(_("Installation of '%(filename)s' failed.") % {
                    'filename': filename} + "\n" + package.msg)
        else:
            raise RuntimeScriptException(_('The file %s is not a MoinMoin package file.' % filename))

        self.msg += package.msg

    def do_addrevision(self, filename, pagename, author=u"Scripting Subsystem", comment=u"", trivial = u"No"):
        """ Adds a revision to a page.

        @param filename: name of the file in this package
        @param pagename: name of the target page
        @param author:   user name of the editor (optional)
        @param comment:  comment related to this revision (optional)
        @param trivial:  boolean, if it is a trivial edit
        """
        _ = self.request.getText
        trivial = self._toBoolean(trivial)

        page = PageEditor(self.request, pagename, do_editor_backup=0, uid_override=author)
        page.saveText(self.extract_file(filename), 0, trivial=trivial, comment=comment)

        page.clean_acl_cache()

    def do_deletepage(self, pagename, comment="Deleted by the scripting subsystem."):
        """ Marks a page as deleted (like the DeletePage action).

        @param pagename: page to delete
        @param comment:  the related comment (optional)
        """
        _ = self.request.getText
        page = PageEditor(self.request, pagename, do_editor_backup=0)
        if not page.exists():
            raise RuntimeScriptException(_("The page %s does not exist.") % pagename)

        page.deletePage(comment)

    def do_replaceunderlay(self, filename, pagename):
        """ Overwrites underlay pages. Implementational detail: This needs to be
            kept in sync with the page class.

        @param filename: name of the file in the package
        @param pagename: page to be overwritten
        """
        page = Page(self.request, pagename)

        pagedir = page.getPagePath(use_underlay=1, check_create=1)

        revdir = os.path.join(pagedir, 'revisions')
        cfn = os.path.join(pagedir,'current')

        revstr = '%08d' % 1
        if not os.path.exists(revdir):
            os.mkdir(revdir)
            os.chmod(revdir, 0777 & config.umask)

        f = open(cfn, 'w')
        f.write(revstr + "\n")
        f.close()
        os.chmod(cfn, 0666 & config.umask)

        pagefile = os.path.join(revdir, revstr)
        self._extractToFile(filename, pagefile)
        os.chmod(pagefile, 0666 & config.umask)

        # Clear caches
        try:
            del self.request.cfg.DICTS_DATA
        except AttributeError:
            pass
        self.request.pages = {}
        caching.CacheEntry(self.request, 'wikidicts', 'dicts_groups').remove()
        page.clean_acl_cache()

    def runScript(self, commands):
        """ Runs the commands.

        @param commands: list of strings which contain a command each
        @return True on success
        """
        _ = self.request.getText

        headerline = unpackLine(commands[0])

        if headerline[0].lower() != "MoinMoinPackage".lower():
            raise PackageException(_("Invalid package file header."))

        self.revision = int(headerline[1])
        if self.revision > MAX_VERSION:
            raise PackageException(_("Package file format unsupported."))

        lineno = 1
        success = True

        for line in commands[1:]:
            lineno += 1
            if self.goto > 0:
                self.goto -= 1
                continue

            if line.startswith("#"):
                continue
            elements = unpackLine(line)
            fnname = elements[0].strip().lower()
            if fnname == '':
                continue
            try:
                fn = getattr(self, "do_" + fnname)
            except AttributeError:
                self.msg += u"Exception RuntimeScriptException (line %i): %s\n" % (
                    lineno, _("Unknown function %s in line %i.") % (elements[0], lineno))
                success = False
                break

            try:
                fn(*elements[1:])
            except ScriptExit:
                break
            except TypeError, e:
                self.msg += u"Exception %s (line %i): %s\n" % (e.__class__.__name__, lineno, unicode(e))
                success = False
                break
            except RuntimeScriptException, e:
                if not self.ignoreExceptions:
                    self.msg += u"Exception %s (line %i): %s\n" % (e.__class__.__name__, lineno, unicode(e))
                    success = False
                    break

        return success

class Package:
    """ A package consists of a bunch of files which can be installed. """
    def __init__(self, request):
        self.request = request
        self.msg = ""

    def installPackage(self):
        """ Opens the package and executes the script. """

        _ = self.request.getText

        if not self.isPackage():
            raise PackageException(_("The file %s was not found in the package.") % MOIN_PACKAGE_FILE)

        commands = self.getScript().splitlines()

        return self.runScript(commands)

    def getScript(self):
        """ Returns the script. """
        return self.extract_file(MOIN_PACKAGE_FILE).decode("utf-8").replace(u"\ufeff", "")

    def extract_file(self, filename):
        """ Returns the contents of a file in the package. """
        raise NotImplementedException

    def filelist(self):
        """ Returns a list of all files. """
        raise NotImplementedException

    def isPackage(self):
        """ Returns true if this package is recognised. """
        raise NotImplementedException

class ZipPackage(Package, ScriptEngine):
    """ A package that reads its files from a .zip file. """
    def __init__(self, request, filename):
        """ Initialise the package.

        @param request RequestBase instance
        @param filename filename of the .zip file
        """

        Package.__init__(self, request)
        ScriptEngine.__init__(self)
        self.filename = filename

        self._isZipfile = zipfile.is_zipfile(filename)
        if self._isZipfile:
            self.zipfile = zipfile.ZipFile(filename)
        # self.zipfile.getinfo(name)

    def extract_file(self, filename):
        """ Returns the contents of a file in the package. """
        _ = self.request.getText
        try:
            return self.zipfile.read(filename.encode("cp437"))
        except KeyError:
            raise RuntimeScriptException(_(
                "The file %s was not found in the package.") % filename)

    def filelist(self):
        """ Returns a list of all files. """
        return self.zipfile.namelist()

    def isPackage(self):
        """ Returns true if this package is recognised. """
        return self._isZipfile and MOIN_PACKAGE_FILE in self.zipfile.namelist()

if __name__ == '__main__':
    args = sys.argv
    if len(args)-1 not in (2, 3) or args[1] not in ('l', 'i'):
        print >>sys.stderr, """MoinMoin Package Installer v%(version)i

%(myname)s action packagefile [request URL]

action      - Either "l" for listing the script or "i" for installing.
packagefile - The path to the file containing the MoinMoin installer package
request URL - Just needed if you are running a wiki farm, used to differentiate
              the correct wiki.

Example:

%(myname)s i ../package.zip

""" % {"version": MAX_VERSION, "myname": os.path.basename(args[0])}
        raise SystemExit

    packagefile = args[2]
    if len(args) > 3:
        request_url = args[3]
    else:
        request_url = "localhost/"

    # Setup MoinMoin environment
    from MoinMoin.request import RequestCLI
    request = RequestCLI(url = 'localhost/')
    request.form = request.args = request.setup_args()

    package = ZipPackage(request, packagefile)
    if not package.isPackage():
        print "The specified file %s is not a package." % packagefile
        raise SystemExit

    if args[1] == 'l':
        print package.getScript()
    elif args[1] == 'i':
        if package.installPackage():
            print "Installation was successful!"
        else:
            print "Installation failed."
        if package.msg:
            print package.msg