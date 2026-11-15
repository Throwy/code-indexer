"""
Query-side benchmark for code-indexer.

Times each search/navigation call the way the MCP server issues it: a fresh
get_db() (connect + schema script + migration) per call. Also times the open
alone and prints EXPLAIN QUERY PLAN for the hot queries.

    python bench/bench_queries.py <indexed-dir> [--branch main] [-n 30]

Point LOCALAPPDATA (Windows) / XDG_CACHE_HOME (Linux/macOS) at a scratch dir
holding a copy of the index to avoid touching your real store.
"""
import argparse, statistics, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from indexer import searcher
from indexer.indexer import get_db


def timeit(fn, n):
    fn()  # warm OS file cache; each timed call still opens a fresh connection
    xs = []
    for _ in range(n):
        t = time.perf_counter(); fn(); xs.append((time.perf_counter() - t) * 1000)
    xs.sort()
    return statistics.median(xs), xs[int(len(xs) * 0.95) - 1 if len(xs) > 1 else 0]


def pick_terms(db):
    """Realistic probe names from the index: common, mid and rare symbol names, plus a base class."""
    names = [r[0] for r in db.conn.execute(
        "SELECT name FROM symbols WHERE kind IN ('function','method','class') GROUP BY name ORDER BY COUNT(*) DESC")]
    names = [n for n in names if len(n) > 3 and n != "<anon>"]
    common, mid, rare = names[0], names[len(names) // 2], names[-1]
    base = db.conn.execute("SELECT base_classes FROM symbols WHERE base_classes IS NOT NULL LIMIT 1").fetchone()
    import json
    base = json.loads(base[0])[0] if base else "Object"
    return common, mid, rare, base


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("directory", type=Path)
    ap.add_argument("--branch")
    ap.add_argument("-n", type=int, default=30)
    a = ap.parse_args()
    d, br, n = a.directory.resolve(), a.branch, a.n

    db = get_db(d, br)
    st = db.stats()
    common, mid, rare, base = pick_terms(db)
    size = db.db_path.stat().st_size / 1e6
    print(f"{db.db_path.name}: {st['files']} files, {st['symbols']} symbols, {size:.1f} MB")
    print(f"probes: common={common!r} mid={mid!r} rare={rare!r} base={base!r}\n")
    db.close()

    cases = [("open only (get_db+close)", lambda: get_db(d, br).close())]
    for label, term in (("common", common), ("mid", mid), ("rare", rare)):
        cases += [
            (f"fts_search {label}", lambda t=term: searcher.fts_search(d, t, 20, br)),
            (f"goto_symbol {label}", lambda t=term: searcher.goto_symbol(d, t, None, br)),
            (f"find_usages {label}", lambda t=term: searcher.find_usages(d, t, 20, br)),
        ]
    cases += [("fts_search multi-term", lambda: searcher.fts_search(d, f"{common} {mid} {rare}", 20, br)),
              ("find_implementations", lambda: searcher.find_implementations(d, base, br)),
              ("find_implementations (none)", lambda: searcher.find_implementations(d, "NoSuchBase__", br))]

    print(f"{'case':34}{'median ms':>11}{'p95 ms':>9}")
    for name, fn in cases:
        med, p95 = timeit(fn, n)
        print(f"{name:34}{med:11.2f}{p95:9.2f}")

    print("\nQUERY PLANS")
    db = get_db(d, br)
    plans = {
        "goto_symbol": ("SELECT s.*, f.path FROM symbols s JOIN files f ON f.id=s.file_id WHERE s.name=? ORDER BY f.path, s.start_line", (common,)),
        "find_usages": ("SELECT s.*, f.path FROM symbols s JOIN files f ON f.id=s.file_id WHERE s.body LIKE ? AND s.name != ? ORDER BY f.path, s.start_line LIMIT 20", (f"%{common}%", common)),
        "find_implementations": ("SELECT s.* FROM symbols s JOIN files f ON f.id=s.file_id WHERE s.base_classes IS NOT NULL AND s.base_classes LIKE ?", (f'%"{base}"%',)),
        "fts_search": ("SELECT s.*, f.path, rank FROM symbols_fts JOIN symbols s ON s.id=symbols_fts.rowid JOIN files f ON f.id=s.file_id WHERE symbols_fts MATCH ? ORDER BY rank LIMIT 20", (f'"{common}"',)),
    }
    for k, (sql, p) in plans.items():
        print(f"- {k}")
        for r in db.conn.execute("EXPLAIN QUERY PLAN " + sql, p):
            print("    ", r[3])
    db.close()


if __name__ == "__main__":
    main()
