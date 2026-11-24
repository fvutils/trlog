# trlog agent usability check (design §9), after Q2

Date: 2026-11-21. This is the first run of the §9 check.

## Setup

- **Agent:** a fresh subagent (Sonnet), told to use only `trlog help agent`
  and `trlog help <topic>` as documentation. It could not read the source,
  the tests, the design docs, or the trace through Python.
- **Trace:** the query corpus (`tests/query/corpus.py`): 19 txns on 4 streams.
- **Questions:** 14 of the §2 questions, adapted to the corpus. These were
  Q1-Q14 and Q19, minus Q8 (the corpus has no source-file metadata), and
  only those that Q1/Q2 verbs could answer.
- **Raw output:** `logs/usability/report.md` and `transcript.txt`. These are
  untracked. Rebuild the trace with
  `PYTHONPATH=python/src:. python -c "from tests.query.corpus import build; build('logs/usability/sim.trl')"`.

## Result

| Measure | Value |
|---------|-------|
| Questions answered correctly | 14 / 14 (checked against the corpus layout) |
| `trlog` calls | 20 (1 `help agent` + 19 queries), about 1.4 per question |
| Output read | 19.5 KB in total |
| Errors hit | 0 |
| Wasted calls | 1 (`around --window 200ns` covered the whole 70 ns trace) |

Several flows took one call:
- the root-cause chain (`links --up all --kind cause`);
- `find ... --first`;
- `active @T`;
- `around --same-stream`;
- `count --by resp`.

## Findings and what was done

| # | Finding | Action |
|---|---------|--------|
| 1 | The help did not say whether `-t A..B` overlap is inclusive, so the agent was unsure about a txn ending exactly at A | **Done.** `help agent` and `trlog help` now say "overlapping [A, B], both ends inclusive" |
| 2 | The `around --window 200ns` example swallowed a 70 ns trace | **Done.** `help agent` and `help around` now say to size W from the span in `info`, or to use `-n`. A warning when the window covers the whole span is still open |
| 3 | There is no direct way to get a txn's siblings; it took `tree`, then `show` on the parent | **Open.** Candidate: `tree --siblings`, or a `siblings` list in `show` |
| 4 | `schema --sample` left out a 4-state value (`bus=0xzf`) | **Fixed (bug).** `_observe` dropped `LogicVal` from the top-k and `distinct` counts. Text output now also prints `(N distinct)` when there are more values than the top 5 shown |
| 5 | `count --by` gives a missing value its own `null` group, while comparisons treat missing as false; the agent had to reconcile the two | **Done.** The `help agent` ANALYZE line says so |
| 6 | No `-T a,b` example | **Done.** The FIND example uses `-T axi_wr,axi_rd` |

## Second run, after Q3 (2026-11-21)

- **Setup:** same as the first run, plus a req/rsp trace `bus.trl`
  (`build_bus` in `tests/query/test_analysis.py`).
- **Questions:** 7, covering
  - Q15 (`stats`), Q16 (`hist`), Q17 (`gaps`);
  - Q18 (`match`), and `match` with `--within`;
  - a misspelled field;
  - export to SQLite, then SQL on the result.
- **Raw output:** `logs/usability/q3/` (untracked).

| Measure | Value |
|---------|-------|
| Questions answered correctly | 7 / 7 |
| `trlog` calls | 19, including 2 `help VERB` |
| Output read | 20.3 KB |
| Errors | 1 `trlog` (the misspelled field, fixed in one step via `did_you_mean`); 3 from a missing `sqlite3` CLI |

| # | Finding | Action |
|---|---------|--------|
| 1 | The export join key (`attr_<type>.id` = `txn.id`) and the `txn_<type>` view columns were not documented | **Done.** `help export` lists the columns of each table and view. `help agent` points at the views |
| 2 | `match --within` can change which pairs form (an a that is too old is dropped), not just drop pairs | **Done.** Stated in `help match` |
| 3 | `gaps` prev/next and the `unmatched` refs need a follow-up `show` to see fields | **Partly.** `help agent` and `help match` now say that `--unmatched a\|b` lists full records. `gaps --with fields` is **open** |
| 4 | `type == 'req'` also matched a third stream with the same type | **Open.** Candidate: a summary note when a side spans streams the other side doesn't |
| 5 | Percentiles over very small n look meaningful | **Open**, low priority |
| 6 | No `sqlite3` CLI on the host | **Done.** `help export` says Python's `sqlite3` module works |
