"""Navigation API (phase Q2): graph, nlinks/nchildren, tree, links, around,
active. Expected values follow the corpus layout in tests/query/corpus.py."""

import pytest

import trlog.query as tq
from trlog.query import QueryError


@pytest.fixture
def q(corpus_path):
    with tq.open(corpus_path) as trace:
        yield trace


def ids(txns):
    return [t.id for t in txns]


def nodes(res):
    return [(d, t.id) for d, t in res.nodes]


# -- graph and edge counts ----------------------------------------------------

def test_graph_children_and_cache(q):
    g = q.graph()
    assert g[1].children == [10, 11, 12, 13]
    assert g[3].children == [30, 31]
    assert g[4].children == [32]
    assert g[100].children == []
    s = tq.ScanStats()
    assert q.graph(s) is g and s.cached and s.blocks_read == 0
    # queries after the graph exists are served from it
    r = q.find(types="axi_wr")
    assert ids(r.txns) == [10, 11, 12, 13, 30] and r.stats.cached


def test_nchildren(q):
    assert ids(q.find("nchildren > 0").txns) == [1, 2, 3, 4]
    assert ids(q.find("nchildren == 2").txns) == [3]
    # children live on other streams than the parent
    assert ids(q.find("nchildren == 4", streams="top.env.seqr").txns) == [1, 2]
    assert ids(q.find("parent.nchildren == 2").txns) == [30, 31]


def test_nlinks(q):
    assert ids(q.find("nlinks > 0").txns) == [3, 30, 101, 31, 102, 32, 103]
    assert ids(q.find("nlinks == 2").txns) == [30]
    # link records sit in other streams' blocks and later in time
    assert ids(q.find("nlinks == 2", streams="top.env.axi1.mon").txns) == [30]
    assert ids(q.find("nlinks == 2", time=(None, 43000)).txns) == [30]


# -- tree -----------------------------------------------------------------------

def test_tree_up_to_root(q):
    r = q.tree(10)
    assert nodes(r) == [(-1, 1), (0, 10)]
    assert r.root.id == 1 and r.focus.id == 10
    assert not r.truncated and r.hidden_children == 0


def test_tree_down(q):
    assert nodes(q.tree(3, down=None)) == [(0, 3), (1, 30), (1, 31)]
    r = q.tree(31, up=0, down=0)
    assert nodes(r) == [(0, 31)]


def test_tree_limit_cuts_breadth_first(q):
    r = q.tree(1, down=None, limit=3)
    assert nodes(r) == [(0, 1), (1, 10), (1, 11)]
    assert r.truncated and r.hidden_children == 2


def test_tree_hidden_children_by_depth(q):
    r = q.tree(1, down=0)
    assert nodes(r) == [(0, 1)]
    assert r.hidden_children == 4 and not r.truncated


def test_tree_not_found(q):
    with pytest.raises(QueryError) as e:
        q.tree(999)
    assert e.value.info["error"] == "not_found"


# -- links ----------------------------------------------------------------------

def test_links_cause_chain_up(q):
    r = q.links(101, up=None, down=0)
    assert nodes(r) == [(-2, 3), (-1, 30), (0, 101)]
    assert r.edges == [(3, 30, "cause", "stimulus"), (30, 101, "cause", "resp")]
    assert r.missing == [] and not r.truncated


def test_links_default_one_hop_each_way(q):
    assert nodes(q.links(30)) == [(-1, 3), (0, 30), (1, 101)]
    assert nodes(q.links(3, up=1, down=None)) == [(0, 3), (1, 30), (2, 101)]


def test_links_kind_filter(q):
    r = q.links(30, kinds=["related"])
    assert nodes(r) == [(0, 30)] and r.edges == []
    assert nodes(q.links(32, kinds=["related"])) == [(0, 32), (1, 103)]
    with pytest.raises(QueryError) as e:
        q.links(30, kinds=["causes"])
    assert e.value.info["error"] == "unknown_link_kind"
    assert "cause" in e.value.info["valid_values"]


def test_links_limit(q):
    r = q.links(101, up=None, down=0, limit=2)
    assert nodes(r) == [(-1, 30), (0, 101)] and r.truncated


# -- active / around ------------------------------------------------------------

def test_active(q):
    assert ids(q.active(43000).txns) == [3, 30]
    # a txn that ends exactly at T is not active
    assert ids(q.active(45000).txns) == [3]
    assert ids(q.active(65000).txns) == [4, 32]
    assert ids(q.active(65000, types="axi_rd").txns) == [32]


def test_around_n(q):
    r = q.around(anchor=31, n=2)
    assert ids(r.txns) == [30, 101, 31, 102, 4]
    assert r.n_before == 2 and r.more_before and r.more_after
    assert r.at == 47000


def test_around_same_stream(q):
    r = q.around(anchor=31, n=2, same_stream=True)
    assert ids(r.txns) == [30, 31, 32]
    assert not r.more_before and not r.more_after


def test_around_time(q):
    r = q.around(at=45000, n=1)
    assert ids(r.txns) == [30, 101] and r.n_before == 1 and r.focus is None


def test_around_window(q):
    # 31 spans 47000..49500; +-1000 overlaps 3 (long), 31 and 102 (@50500)
    r = q.around(anchor=31, window=1000)
    assert ids(r.txns) == [3, 31, 102] and r.n_before is None


def test_around_same_stream_needs_txn(q):
    with pytest.raises(QueryError):
        q.around(at=45000, same_stream=True)
