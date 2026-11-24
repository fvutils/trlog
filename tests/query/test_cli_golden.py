"""Golden output of the trlog CLI on the query corpus (see golden_cases.py).

Every case runs on both scan engines against the same golden file: the C
engine is an optional accelerator and must not change any output.
"""

import pytest

from trlog.query import _cengine

from .golden_cases import CASES, golden_path, run_case

ENGINES = ["python", pytest.param("c", marks=pytest.mark.skipif(
    _cengine.load() is None, reason="libtrl with trl_qscan not built"))]


@pytest.mark.parametrize("engine", ENGINES)
@pytest.mark.parametrize("name,argv,stdin", [(c[0], c[1], c[2] if len(c) > 2 else None)
                                             for c in CASES],
                         ids=[c[0] for c in CASES])
def test_golden(name, argv, stdin, engine, corpus_path, monkeypatch):
    monkeypatch.setenv("TRLOG_ENGINE", engine)
    expected = golden_path(name, argv)
    assert expected.exists(), (
        f"missing {expected.name}; run PYTHONPATH=python/src:. "
        "python -m tests.query.golden_cases")
    assert run_case(argv, corpus_path, stdin) == expected.read_text(encoding="utf-8")
