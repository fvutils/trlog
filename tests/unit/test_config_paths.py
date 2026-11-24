"""get_libdirs/get_incdirs/get_svdirs and `trlog config` in a source checkout.

The installed-wheel layout is checked by tests/wheel_smoke.py.
"""

import os

import trlog
from trlog.cli import main


def test_incdirs_hold_headers():
    assert any(os.path.isfile(os.path.join(d, "trl", "trl.h")) for d in trlog.get_incdirs())


def test_svdirs_hold_package():
    assert any(os.path.isfile(os.path.join(d, "trl_pkg.sv")) for d in trlog.get_svdirs())


def test_libdirs_hold_libtrl():
    from trlog._libpath import lib_name
    dirs = trlog.get_libdirs()
    if not dirs:
        return  # no native build in this checkout
    assert os.path.isfile(os.path.join(dirs[0], lib_name("trl")))


def test_config_cli(capsys):
    assert main(["config", "--svdir"]) == 0
    assert capsys.readouterr().out.splitlines() == trlog.get_svdirs()
    assert main(["config", "--version"]) == 0
    assert capsys.readouterr().out.strip() == trlog.__version__


def test_config_cli_needs_an_option(capsys):
    assert main(["config"]) == 2
