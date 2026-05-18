import pytest
import pathlib


@pytest.fixture
def tmp_trl(tmp_path):
    return tmp_path / "test.trl"


@pytest.fixture(params=["pure"])
def writer_cls(request):
    if request.param == "native":
        pytest.importorskip("trlog._native")
        from trlog._native import TrlWriter as W
        return W
    from trlog._writer import TrlWriter as W
    return W


@pytest.fixture(params=["pure"])
def reader_cls(request):
    if request.param == "native":
        pytest.importorskip("trlog._native")
        from trlog._native import TrlReader as R
        return R
    from trlog._reader import TrlReader as R
    return R
