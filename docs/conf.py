# Configuration file for the Sphinx documentation builder.
import os
import sys
sys.path.insert(0, os.path.abspath('../python/src'))

project = 'trlog'
copyright = '2024-2026, Matthew Ballance and contributors'
author = 'Matthew Ballance and contributors'
release = '0.1.0'

extensions = [
    'sphinx.ext.autodoc',
    'sphinx.ext.napoleon',
    'sphinx.ext.viewcode',
    'sphinx.ext.intersphinx',
    'myst_parser',
    'sphinx_systemverilog',
]

# SystemVerilog (sphinx-systemverilog). trl_pkg.sv is the one root: the
# per-class .svh files are `include'd into it and are not standalone, so
# they must not be picked up by a directory scan (sv_source_dirs).
_sv_src = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'sv', 'src'))
sv_build_units = [
    os.path.join(_sv_src, 'trl_pkg.sv'),
    os.path.join(_sv_src, 'trl_final.sv'),
]
sv_include_dirs = [_sv_src]
sv_doc_style = 'native'
# Pygments' SystemVerilog lexer cannot tokenize `import "DPI-C"` lines, so
# the source listing of trl_sv_dpi.svh falls back to relaxed highlighting.
suppress_warnings = ['misc.highlighting_failure']

intersphinx_mapping = {'python': ('https://docs.python.org/3', None)}

templates_path = ['_templates']
exclude_patterns = ['_build', 'Thumbs.db', '.DS_Store']

html_theme = 'sphinx_rtd_theme'

autodoc_member_order = 'bysource'
napoleon_google_docstring = True
napoleon_numpy_docstring = False
