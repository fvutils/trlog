# Configuration file for the Sphinx documentation builder.
import os
import sys
sys.path.insert(0, os.path.abspath('../python/src'))

project = 'zuspec-trace'
copyright = '2024, ZuSpec Contributors'
author = 'ZuSpec Contributors'
release = '0.1.0'

extensions = [
    'sphinx.ext.autodoc',
    'sphinx.ext.napoleon',
    'sphinx.ext.viewcode',
    'sphinx.ext.intersphinx',
    'myst_parser',
]

intersphinx_mapping = {'python': ('https://docs.python.org/3', None)}

templates_path = ['_templates']
exclude_patterns = ['_build', 'Thumbs.db', '.DS_Store']

html_theme = 'sphinx_rtd_theme'
html_static_path = ['_static']

autodoc_member_order = 'bysource'
napoleon_google_docstring = True
napoleon_numpy_docstring = False
