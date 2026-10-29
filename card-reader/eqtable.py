"""미리 계산한 에쿼티 재사용: 프리플랍 표 + 포스트플랍/레인지 디스크 캐시.

공개 API
  preflop(hero_cards, opponents) -> {equity, win, tie, lose, exact: False, samples, source: 'table'} | None
      169 시작 핸드 x 상대 1~9명, 랜덤 핸드 기준. 표(eqtable/preflop_random.json)가 없거나 범위 밖이면 None.
      hero_cards: 카드 정수 2개 (또는 'Ah' 문자열). 실제 카드 -> 169 코드('AKs','T9o','77') 는 hand_code().
  hand_code(c1, c2) -> '77' / 'AKs' / 'T9o'
  canonical(hero, board) -> (대표형, [최소를 만드는 무늬 순열 번호들])   무늬를 바꿔도 같은 키가 나오게 정규화
  post_key(hero, board, opponents) -> str             range_key(hero, board, descs) -> str
  cached_equity(...)      evaluator.equity 가 쓰는 포스트플랍 캐시 경로
  cached_range_equity(...) rangeq.equity_vs_ranges 가 쓰는 레인지 캐시 경로
  Store(path)             sqlite 저장소 (스레드마다 연결 하나 + 쓰기 락). 기본 저장소는 get_store().

저장 위치: exe/스크립트 옆 eqtable/ (환경변수 PREFLOP_EQ_DIR 로 변경, PREFLOP_EQ_CACHE=0 이면 캐시 끔).
  eqtable/preflop_random.json   (표, 배포용 ~50KB)       eqtable/postflop_cache.sqlite (캐시, 테이블 post / rng)
캐시 정책
  - 키는 무늬 정규화: 무늬 24가지 순열 중 (히어로, 보드) 표현이 사전순 최소인 것으로 맞춘다.
    보드는 집합이다 (플랍 순서/턴/리버 순서는 랜덤 상대 에쿼티에 영향이 없다).
  - 항목에 표본이 쌓인다: 새로 계산한 결과는 기존 항목과 표본 수로 가중 합쳐 저장 (전수 계산 exact 결과는 그대로 고정).
  - 호출자가 요구한 iterations 와 TRUST 표본(포스트플랍 10000 / 레인지 6000) 이상이 쌓인 항목만 즉시 반환한다.
    그보다 적으면 새 표본 iterations 개를 계산해 캐시에 합치고, 호출자에게는 그 새 표본 결과만 돌려준다
    (reader 가 빠른 1차 + 조각 결과를 merge_equity 로 합치므로, 같은 표본이 두 번 세어지지 않게).
  - 항목 수 상한(post 200k / rng 50k): used(최근 사용 시각) 가 가장 오래된 것부터 지운다.
"""
import hashlib
import itertools
import json
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path

RANKS = "23456789TJQKA"
FROZEN = bool(getattr(sys, "frozen", False))
BASE = Path(sys.executable if FROZEN else __file__).resolve().parent
DIR = Path(os.environ.get("PREFLOP_EQ_DIR") or (BASE / "eqtable"))
TABLE_NAME = "preflop_random.json"
DB_NAME = "postflop_cache.sqlite"
MAX_OPP = 9

POST_TRUST = 10000      # 포스트플랍 캐시 항목이 이만큼 쌓이면 즉시 반환
RANGE_TRUST = 6000      # 레인지 캐시
POST_CAP = 200_000
RANGE_CAP = 50_000


# --- 카드 / 핸드 코드 ---------------------------------------------------------------
def _card(c):
    if isinstance(c, int):
        return c
    t = str(c).strip()
    if t[:2] == "10":
        t = "T" + t[2:]
    return RANKS.index(t[0].upper()) * 4 + "shdc".index(t[1].lower())


def hand_code(c1, c2):
    r1, r2 = c1 >> 2, c2 >> 2
    if r1 < r2:
        r1, r2 = r2, r1
    if r1 == r2:
        return RANKS[r1] * 2
    return RANKS[r1] + RANKS[r2] + ("s" if (c1 & 3) == (c2 & 3) else "o")


def code_cards(code):
    """핸드 코드 -> 대표 카드 정수 2개 (에쿼티는 같은 코드면 모두 같다)."""
    r1, r2 = RANKS.index(code[0]), RANKS.index(code[1])
    if len(code) == 2:
        return [r1 * 4, r1 * 4 + 1]
    if code[2] == "s":
        return [r1 * 4, r2 * 4]
    return [r1 * 4, r2 * 4 + 1]


def all_codes():
    """169 코드 (페어, 같은 무늬, 다른 무늬 순; 높은 랭크 먼저)."""
    out = []
    for i in range(12, -1, -1):
        for j in range(12, -1, -1):
            if i == j:
                out.append(RANKS[i] * 2)
            elif i > j:
                out.append(RANKS[i] + RANKS[j] + "s")
            else:
                out.append(RANKS[j] + RANKS[i] + "o")
    return out


# --- 프리플랍 표 ---------------------------------------------------------------------
_table = None
_table_lock = threading.Lock()
_table_loaded = False


def _table_paths():
    paths = [DIR / TABLE_NAME]
    mei = getattr(sys, "_MEIPASS", None)
    if mei:
        paths.append(Path(mei) / "eqtable" / TABLE_NAME)
    return paths


def _load_table():
    global _table, _table_loaded
    with _table_lock:
        if _table_loaded:
            return _table
        _table_loaded = True
        for p in _table_paths():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                hands = data["hands"]
                if len(hands) == 169:
                    _table = hands
                    break
            except (OSError, ValueError, KeyError):
                continue
        return _table


def reload_table():
    """표 파일을 다시 읽는다 (빌드가 끝난 뒤 / 테스트용)."""
    global _table_loaded
    with _table_lock:
        _table_loaded = False
    return _load_table() is not None


def table_available():
    return _load_table() is not None


def preflop(hero_cards, opponents):
    """랜덤 핸드 opponents 명(1~9) 대비 프리플랍 에쿼티. 표가 없거나 범위 밖이면 None."""
    tab = _table if _table_loaded else _load_table()
    if tab is None:
        return None
    try:
        a, b = (_card(c) for c in hero_cards)
        n = int(opponents)
        if a == b or not (0 <= a < 52 and 0 <= b < 52) or not (1 <= n <= MAX_OPP):
            return None
        row = tab[hand_code(a, b)][n - 1]
    except (ValueError, TypeError, KeyError, IndexError):
        return None
    return {"equity": row[0], "win": row[1], "tie": row[2], "lose": row[3], "exact": False,
            "samples": int(row[4]), "source": "table"}


# --- 무늬 정규화 ----------------------------------------------------------------------
_PERMS = list(itertools.permutations(range(4)))
_PERM_MAP = [[(c >> 2) * 4 + p[c & 3] for c in range(52)] for p in _PERMS]


def canonical(hero, board):
    """(히어로 2장, 보드) 를 무늬 순열 24가지로 바꿔 본 표현 중 사전순 최소 ((hero..), (board..))와
    그것을 만드는 순열 번호 리스트 (무늬가 대칭이면 여러 개)."""
    best, which = None, []
    for i, m in enumerate(_PERM_MAP):
        rep = (tuple(sorted([m[c] for c in hero])), tuple(sorted([m[c] for c in board])))
        if best is None or rep < best:
            best, which = rep, [i]
        elif rep == best:
            which.append(i)
    return best, which


def post_key(hero, board, opponents):
    (h, b), _ = canonical(hero, board)
    return "%s|%s|%d" % (",".join(map(str, h)), ",".join(map(str, b)), opponents)


def _digest(obj):
    return hashlib.blake2b(repr(obj).encode(), digest_size=8).hexdigest()


def range_key(hero, board, descs):
    """레인지 캐시 키. descs: 상대별 ("c", 클래스 레인지 다이제스트) — 무늬에 대칭이라 순열 불필요 —
    또는 ("x", [(c1, c2, w), ...]) — 콤보 목록(죽은 카드 제외, 정규화 가중치). 상대 순서는 무관(정렬)."""
    (h, b), which = canonical(hero, board)
    best = None
    for pi in which:
        m = _PERM_MAP[pi]
        parts = []
        for kind, d in descs:
            if kind == "c":
                parts.append("c" + d)
            else:
                parts.append("x" + _digest(sorted((min(m[x], m[y]), max(m[x], m[y]), w) for x, y, w in d)))
        parts.sort()
        t = tuple(parts)
        if best is None or t < best:
            best = t
        if all(k == "c" for k, _ in descs):
            break
    return "%s|%s|%s" % (",".join(map(str, h)), ",".join(map(str, b)), _digest(best))


# --- sqlite 저장소 ---------------------------------------------------------------------
_SCHEMA = """
CREATE TABLE IF NOT EXISTS {t} (
  k TEXT PRIMARY KEY, eq REAL, win REAL, tie REAL, lose REAL, n INTEGER, exact INTEGER, used REAL
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS {t}_used ON {t}(used);
"""


class Store:
    """스레드마다 연결 하나(threading.local), 쓰기는 락으로 직렬화. 실패하면 조용히 비활성(캐시는 부가 기능)."""

    TABLES = ("post", "rng")

    def __init__(self, path, post_cap=POST_CAP, range_cap=RANGE_CAP):
        self.path = str(path)
        self.caps = {"post": post_cap, "rng": range_cap}
        self.disabled = False
        self._local = threading.local()
        self._conns = []
        self._wlock = threading.Lock()
        self._touch = {}
        self._adds = {t: 0 for t in self.TABLES}
        self._init_lock = threading.Lock()
        self._ready = False

    # -- 연결 --
    def _open(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None, check_same_thread=False)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.Error:
            pass
        return conn

    def _conn(self):
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            return conn
        with self._init_lock:
            try:
                conn = self._open()
                if not self._ready:
                    try:
                        for t in self.TABLES:
                            conn.executescript(_SCHEMA.format(t=t))
                    except sqlite3.DatabaseError:   # 손상된 파일 -> 버리고 새로
                        conn.close()
                        for suf in ("", "-wal", "-shm"):
                            try:
                                os.remove(self.path + suf)
                            except OSError:
                                pass
                        conn = self._open()
                        for t in self.TABLES:
                            conn.executescript(_SCHEMA.format(t=t))
                    self._ready = True
            except (sqlite3.Error, OSError):
                self.disabled = True
                raise
            self._conns.append(conn)
        self._local.conn = conn
        return conn

    def close(self):
        """모든 스레드의 연결을 닫는다 (테스트에서 임시 폴더를 지우기 전)."""
        with self._init_lock:
            for c in self._conns:
                try:
                    c.close()
                except sqlite3.Error:
                    pass
            self._conns.clear()
            self._local = threading.local()
            self._ready = False

    # -- 읽기 / 쓰기 --
    def get(self, table, key):
        if self.disabled:
            return None
        try:
            row = self._conn().execute(f"SELECT eq, win, tie, lose, n, exact FROM {table} WHERE k=?", (key,)).fetchone()
        except (sqlite3.Error, OSError):
            return None
        if row is None:
            return None
        self._touch[(table, key)] = time.time()
        if len(self._touch) >= 64:
            self._flush_touch()
        return {"equity": row[0], "win": row[1], "tie": row[2], "lose": row[3], "samples": row[4],
                "exact": bool(row[5]), "source": "cache"}

    def _flush_touch(self):
        touches, self._touch = self._touch, {}
        if not touches or self.disabled:
            return
        try:
            conn = self._conn()
            with self._wlock:
                for (table, key), ts in touches.items():
                    conn.execute(f"UPDATE {table} SET used=? WHERE k=?", (ts, key))
        except (sqlite3.Error, OSError):
            pass

    def add(self, table, key, res):
        """res(equity/win/tie/lose/samples/exact) 를 기존 항목과 표본 수로 가중 합쳐 저장. exact 는 덮어써 고정."""
        if self.disabled or not res.get("samples"):
            return
        now = time.time()
        vals = (key, res["equity"], res["win"], res["tie"], res["lose"], int(res["samples"]),
                1 if res.get("exact") else 0, now)
        try:
            conn = self._conn()
            with self._wlock:
                if res.get("exact"):
                    conn.execute(f"INSERT OR REPLACE INTO {table}(k,eq,win,tie,lose,n,exact,used) VALUES(?,?,?,?,?,?,?,?)", vals)
                else:
                    conn.execute(
                        f"INSERT INTO {table}(k,eq,win,tie,lose,n,exact,used) VALUES(?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(k) DO UPDATE SET "
                        "eq=(eq*n+excluded.eq*excluded.n)/(n+excluded.n), win=(win*n+excluded.win*excluded.n)/(n+excluded.n), "
                        "tie=(tie*n+excluded.tie*excluded.n)/(n+excluded.n), lose=(lose*n+excluded.lose*excluded.n)/(n+excluded.n), "
                        "n=n+excluded.n, used=excluded.used WHERE exact=0", vals)
                self._adds[table] += 1
                if self._adds[table] % 200 == 0:
                    self._evict(conn, table)
        except (sqlite3.Error, OSError):
            return
        if self._touch:
            self._flush_touch()

    def _evict(self, conn, table):
        cap = self.caps[table]
        n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        if n > cap:
            drop = n - int(cap * 0.95)   # 5% 여유를 두고 지워 자주 안 돌게
            conn.execute(f"DELETE FROM {table} WHERE k IN (SELECT k FROM {table} ORDER BY used LIMIT ?)", (drop,))

    def evict(self, table):
        with self._wlock:
            self._evict(self._conn(), table)

    def count(self, table):
        try:
            return self._conn().execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except (sqlite3.Error, OSError):
            return 0


_store = None
_store_lock = threading.Lock()


def get_store():
    """기본 저장소 (DIR/postflop_cache.sqlite). PREFLOP_EQ_CACHE=0 이거나 열 수 없으면 None."""
    global _store
    if _store is not None:
        return None if _store.disabled else _store
    if os.environ.get("PREFLOP_EQ_CACHE") == "0":
        return None
    with _store_lock:
        if _store is None:
            _store = Store(DIR / DB_NAME)
        return None if _store.disabled else _store


def set_dir(path):
    """저장 위치 변경 (표 + 캐시). 테스트용."""
    global DIR, _store, _table_loaded, _table
    with _store_lock:
        if _store is not None:
            _store.close()
        _store = None
    DIR = Path(path)
    with _table_lock:
        _table_loaded = False
        _table = None


def set_store(store):
    """기본 저장소를 직접 지정 (None 이면 다시 기본값으로)."""
    global _store
    with _store_lock:
        if _store is not None and _store is not store:
            _store.close()
        _store = store


def _valid_cards(cards):
    return all(isinstance(c, int) and 0 <= c < 52 for c in cards)


# --- 캐시 경로 -------------------------------------------------------------------------
def cached_equity(hero, board, opponents, iterations, exact_limit, rng, simulate):
    """evaluator.equity(보드 3~5장, 랜덤 상대) 의 캐시 경로. simulate(hero, board, opponents, iterations, exact_limit, rng)."""
    hero, board = list(hero), list(board)
    store = None
    if (len(hero) == 2 and isinstance(opponents, int) and opponents >= 1 and _valid_cards(hero + board)
            and len(set(hero + board)) == len(hero) + len(board)):
        store = get_store()
    if store is None:
        return simulate(hero, board, opponents, iterations, exact_limit, rng)
    key = post_key(hero, board, opponents)
    hit = store.get("post", key)
    if hit is not None and (hit["exact"] or hit["samples"] >= max(iterations, POST_TRUST)):
        return hit
    res = simulate(hero, board, opponents, iterations, exact_limit, rng)
    store.add("post", key, res)
    return res


def range_descs(ranges, hero, board, expand, combos_of_class):
    """rangeq 레인지 -> range_key 용 설명자 리스트. expand: rangeq.expand."""
    dead = set(hero) | set(board)
    descs = []
    for r in ranges:
        if isinstance(r, dict) and all(len(k) != 4 for k in r):
            items = [(k, float(w)) for k, w in r.items() if w and w > 0]
            top = max((w for _, w in items), default=0.0)
            norm = sorted((k, round(w / top, 4)) for k, w in items) if top > 0 else []
            descs.append(("c", _digest(norm)))
            continue
        acc = {}
        for c1, c2, w in expand(r):
            if c1 in dead or c2 in dead or not w > 0:
                continue
            kk = (min(c1, c2), max(c1, c2))
            acc[kk] = acc.get(kk, 0.0) + w
        top = max(acc.values(), default=0.0)
        combos = [(a, b, round(w / top, 4)) for (a, b), w in acc.items()] if top > 0 else []
        descs.append(("x", [t for t in combos if t[2] > 0]))
    return descs


def cached_range_equity(hero, board, ranges, iterations, expand, compute):
    """rangeq.equity_vs_ranges 의 캐시 경로. compute(iterations) -> 결과 dict."""
    store = get_store()
    if store is None or not _valid_cards(list(hero) + list(board)):
        return compute(iterations)
    try:
        key = range_key(hero, board, range_descs(ranges, hero, board, expand, None)) + "|%d" % len(ranges)
    except (ValueError, TypeError, KeyError, AttributeError):
        return compute(iterations)
    hit = store.get("rng", key)
    if hit is not None and hit["samples"] >= max(iterations, RANGE_TRUST):
        return hit
    res = compute(iterations)
    store.add("rng", key, res)
    return res
