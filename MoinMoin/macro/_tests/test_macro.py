# -*- coding: iso-8859-1 -*-
"""
    MoinMoin - MoinMoin.macro Tests

    @copyright: 2003-2004 by Juergen Hermann <jh@web.de>,
                2006 MoinMoin:ThomasWaldmann
    @license: GNU GPL, see COPYING for details.
"""

from MoinMoin import macro
from MoinMoin.parser.text import Parser
from MoinMoin.formatter.text_html import Formatter


class TestMacro:
    def testTrivialMacro(self):
        """macro: trivial macro works"""
        m = self._make_macro()
        expected = m.formatter.linebreak(0)
        result = m.execute("BR", "")
        assert result == expected

    def _make_macro(self):
        """Test helper"""
        p = Parser("##\n", self.request)
        p.formatter = Formatter(self.request)
        self.request.formatter = p.formatter
        p.form = self.request.form
        m = macro.Macro(p)
        return m


coverage_modules = ['MoinMoin.macro']
