import sys

import sphinx_rtd_theme
import pkg_resources


extensions = [
    'sphinx.ext.autodoc',
    'sphinx.ext.intersphinx',
    'sphinx.ext.viewcode',
    'sphinx.ext.napoleon'
]

templates_path = ['_templates']
source_suffix = '.rst'

master_doc = 'index'

project = 'bloop'
copyright = '2016, Joe Cross'
author = 'Joe Cross'


try:
    release = pkg_resources.get_distribution('bloop').version
except pkg_resources.DistributionNotFound:
    print('To build the documentation, The distribution information of bloop')
    print('Has to be available.  Either install the package into your')
    print('development environment or run "setup.py develop" to setup the')
    print('metadata.  A virtualenv is recommended!')
    sys.exit(1)
del pkg_resources
version = '.'.join(release.split('.')[:2])

language = 'en'

exclude_patterns = ['_build']

pygments_style = 'sphinx'

html_theme = 'sphinx_rtd_theme'
html_theme_path = [sphinx_rtd_theme.get_html_theme_path()]

intersphinx_mapping = {
    'python': ('https://docs.python.org/3.6', None),
    'arrow': ('https://arrow.readthedocs.io/en/latest/', None),
    'PIL': ('https://pillow.readthedocs.io/en/3.4.x', None),
    'blinker': ('https://pythonhosted.org/blinker/', None),
    'boto3': ('http://boto3.readthedocs.io/en/latest/index.html', None)
}
