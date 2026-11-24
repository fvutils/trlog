# The single source of this package's version, in the same shape as
# dv-solve's and pssparser's __version__.py.
#
# BASE is the released version and is what a `v<BASE>` tag must match; the
# release job rejects a tag that disagrees with it, and stamps the same
# version into the npm package. SUFFIX is rewritten by CI for non-release
# builds so a development wheel can never collide with a release.

BASE = "0.1.0"
SUFFIX = ""

__version__ = (BASE, SUFFIX)

# Read by setup.py, which exec()s this file rather than importing trlog: the
# package is not importable while the ivpm_build backend computes build
# requirements, so a [tool.setuptools.dynamic] attr: entry would fail there.
_pkg_version = BASE + SUFFIX
