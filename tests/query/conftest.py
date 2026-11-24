import pytest

from .corpus import build


@pytest.fixture(scope="session")
def corpus_path(tmp_path_factory):
    p = tmp_path_factory.mktemp("query") / "query_corpus.trl"
    build(p)
    return p
