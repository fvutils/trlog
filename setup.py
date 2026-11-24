"""Supplies the version to setuptools.

Everything else about the package stays in pyproject.toml. This file exists
so the version can live in python/src/trlog/__version__.py: it exec()s that
file in an empty namespace and reads _pkg_version back, so trlog never has to
be importable at build time (the same arrangement as dv-solve's setup.py).

The build_ext override is for `python setup.py build_ext --inplace`: the
stock build_ext does nothing when there are no ext_modules (libtrl is loaded
through ctypes), so it runs the same CmakeBuilder the ivpm_build backend
uses. Under the backend this reconfigures and finds nothing to rebuild.
ivpm_build is imported inside run() because it is only installed where a
build happens.
"""

import os

from setuptools import setup
from setuptools.command.build_ext import build_ext as _build_ext

proj_dir = os.path.dirname(os.path.abspath(__file__))


def _get_version():
    version_file = os.path.join(proj_dir, "python", "src", "trlog", "__version__.py")
    glb = {}
    with open(version_file) as f:
        exec(f.read(), glb)
    return glb["_pkg_version"]


class build_ext(_build_ext):

    def run(self):
        from ivpm_build.cmake.cmake_builder import CmakeBuilder
        CmakeBuilder(proj_dir, debug=bool(self.debug)).run()
        super().run()


setup(version=_get_version(), cmdclass={"build_ext": build_ext})
