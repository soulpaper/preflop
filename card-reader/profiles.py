"""상대별 통계 저장소.

    store = profiles.Store()                       # BASE/profiles.db + BASE/hands/YYYY-MM-DD.jsonl
    store.observe(record)                          # handlog.Tracker 가 낸 핸드 기록
    store.stats("홍길동")                          # {'hands', 'vpip', 'pfr', 'threebet', 'fold_to_bet', 'af', 'wtsd', 'shown', 'reliable', ...}
    store.get_for_table({"seat_UR": ("홍길동", False), "seat_BL": ("User84", True)}, session=tracker.session)

  - 잘리지 않은 닉네임: SQLite (exe 옆 profiles.db) 에 누적.
  - 잘린 닉네임('User84…'): DB 에 절대 쓰지 않음. 메모리에서 (세션, 좌석 키, 잘린 이름) 으로만 누적하고,
    그 좌석 이름이 바뀌거나 새 세션(다시 보정/새 테이블)의 기록이 오면 버린다.
  - confidence='low' 기록은 통계에 넣지 않고 JSONL 에만 (표시해서) 남긴다.
  - JSONL: 잘리지 않은 이름은 그대로(로컬 전용), 잘린 이름은 'seat-local:<좌석 키>' 자리표시로.
  - 글자 지문: names.NameReader 의 resolver/on_confirm 으로 연결하면 OCR 이 조금 다르게 읽어도 같은 사람으로 묶음.

  - 게임 '유저 정보' 팝업 통계 (userinfo.py): import_site_stats(name, stats, tab) 로 (이름, 게임 탭) 별 저장.
    stats()/get_for_table() 는 사이트 통계를 ~150핸드 분량의 강한 사전분포로 쓰고 우리 관측을 표본 수로 가중해 섞는다
    (source='site', reliable=True). fold_to_bet/af 는 우리 관측만. ats/style 은 사이트 값.
    잘린 이름('User84')이 있는 좌석에 접두사가 같은 전체 이름의 팝업이 읽히면 그 세션 동안 그 좌석을 전체 이름으로 연결한다.

비율은 퍼센트 0..100 (소수 첫째 자리; handread 와 같은 단위), 표본이 없으면 None. af = (벳+레이즈)/콜.
reliable = hands ≥ 30.
모든 공개 메서드는 예외를 밖으로 내지 않는다 (실패하면 errors 증가, 빈 값 반환).
"""
import json
import sqlite3
import sys
import threading
import time
from pathlib import Path

import numpy as np

BASE = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent
RELIABLE_HANDS = 30
SITE_PRIOR_HANDS = 150     # 사이트 통계(vpip/pfr)의 사전분포 크기 = 150핸드 분량
SITE_PRIOR_3BET_OPP = 30   # 3벳은 기회 수가 단위 (150핸드 x 약 20% 기회) 로 환산
DEFAULT_TAB = "홀덤"       # tab 을 안 주면 홀덤 우선, 없으면 가장 최근 것 (게임 종류 판별은 호출하는 쪽 몫)
SHOWN_KEEP = 20            # 이름별로 보관할 공개 핸드 수
FP_KEEP = 4                # 이름별 글자 지문 수
FP_MATCH = 0.75            # 지문이 이 이상 닮으면 같은 닉네임 (names.FP_SAME 보다 조금 엄격)

COUNTERS = ("hands", "vpip", "pfr", "threebet", "threebet_opp", "faced_bet", "fold_to_bet", "agg", "calls",
            "saw_flop", "showdown")

SCHEMA = """
CREATE TABLE IF NOT EXISTS players (
    name TEXT PRIMARY KEY,
    hands INTEGER DEFAULT 0, vpip INTEGER DEFAULT 0, pfr INTEGER DEFAULT 0,
    threebet INTEGER DEFAULT 0, threebet_opp INTEGER DEFAULT 0,
    faced_bet INTEGER DEFAULT 0, fold_to_bet INTEGER DEFAULT 0,
    agg INTEGER DEFAULT 0, calls INTEGER DEFAULT 0,
    saw_flop INTEGER DEFAULT 0, showdown INTEGER DEFAULT 0,
    first_seen REAL, last_seen REAL
);
CREATE TABLE IF NOT EXISTS shown (
    name TEXT, hand_id TEXT, cards TEXT, board TEXT, t REAL
);
CREATE INDEX IF NOT EXISTS shown_name ON shown(name);
CREATE TABLE IF NOT EXISTS observed (hand_id TEXT PRIMARY KEY, t REAL);
CREATE TABLE IF NOT EXISTS fingerprints (name TEXT, w INTEGER, h INTEGER, fp BLOB, t REAL);
CREATE INDEX IF NOT EXISTS fp_name ON fingerprints(name);
CREATE TABLE IF NOT EXISTS site_stats (
    name TEXT, tab TEXT, vpip REAL, pfr REAL, ats REAL, threebet REAL, style TEXT, t REAL,
    PRIMARY KEY (name, tab)
);
"""


def _ratio(num, den):
    """퍼센트 0..100 (소수 첫째 자리), 표본 없으면 None. handread.estimate 의 profiles 입력과 같은 단위."""
    return round(100.0 * num / den, 1) if den else None


def summarize(c, shown=(), site=None):
    """카운터 dict → 화면용 통계. site = 사이트 통계 dict(vpip, pfr, ats, threebet, style, tab, t) 가 있으면 섞음:
    value = (사전 크기 x 사이트 값 + 우리 표본 수 x 우리 값) / (사전 크기 + 우리 표본 수)
    (vpip/pfr 의 우리 표본 수 = hands, threebet = threebet_opp; 사전 크기 150핸드 / 3벳 30기회). hands 는 우리 관측만."""
    hands = c.get("hands", 0)
    calls, agg = c.get("calls", 0), c.get("agg", 0)
    out = {
        "hands": hands,
        "vpip": _ratio(c.get("vpip", 0), hands),
        "pfr": _ratio(c.get("pfr", 0), hands),
        "threebet": _ratio(c.get("threebet", 0), c.get("threebet_opp", 0)),
        "ats": None,
        "style": None,
        "fold_to_bet": _ratio(c.get("fold_to_bet", 0), c.get("faced_bet", 0)),
        "af": round(agg / calls, 2) if calls else (float(agg) if agg else None),
        "wtsd": _ratio(c.get("showdown", 0), c.get("saw_flop", 0)),
        "shown": list(shown),
        "reliable": hands >= RELIABLE_HANDS,
        "source": "observed" if hands else None,
        "site": None,
        "counts": {k: c.get(k, 0) for k in COUNTERS},
    }
    if site:
        for key, n, prior in (("vpip", hands, SITE_PRIOR_HANDS), ("pfr", hands, SITE_PRIOR_HANDS),
                              ("threebet", c.get("threebet_opp", 0), SITE_PRIOR_3BET_OPP)):
            sv = site.get(key)
            if sv is None:
                continue
            own = out[key]
            out[key] = round(float(sv) if own is None or not n else (prior * sv + n * own) / (prior + n), 1)
        out["ats"] = site.get("ats")
        out["style"] = site.get("style")
        out["reliable"] = True
        out["source"] = "site"
        out["site"] = dict(site)
        out["effective_hands"] = SITE_PRIOR_HANDS + hands
    return out


class Store:
    def __init__(self, path=None, hands_dir=None, write_jsonl=True):
        self.path = Path(path) if path else BASE / "profiles.db"
        self.hands_dir = Path(hands_dir) if hands_dir else self.path.parent / "hands"
        self.write_jsonl = write_jsonl
        self.lock = threading.RLock()
        self.errors = 0
        self.last_error = None
        self.session = None
        self.mem = {}                  # (session, seat_key, 잘린 이름) -> 카운터 dict
        self.mem_shown = {}            # 같은 키 -> 공개 핸드
        self.seat_names = {}           # (session, seat_key) -> 잘린 이름 (이름이 바뀌면 그 좌석 기억 버림)
        self.links = {}                # (session, seat_key) -> 팝업으로 확인된 전체 이름 (잘린 이름 접두사 일치, 세션 한정)
        self._fp_cache = None
        self.db = sqlite3.connect(str(self.path), check_same_thread=False, timeout=5)
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self):
        with self.lock:
            try:
                self.db.close()
            except Exception:
                pass

    def _fail(self, exc):
        self.errors += 1
        self.last_error = repr(exc)

    # --- 세션 (잘린 닉네임 기억) ---------------------------------------------------------------------
    def new_session(self, session):
        """새 보정/새 테이블: 잘린 닉네임 기억을 모두 버림."""
        with self.lock:
            self.session = session
            self.mem.clear()
            self.mem_shown.clear()
            self.seat_names.clear()
            self.links.clear()

    def _session_seat(self, session, seat, name, truncated):
        """좌석 이름이 바뀌면 그 좌석의 잘린 이름 기억을 버림."""
        if session != self.session:
            self.new_session(session)
        key = (session, seat)
        cur = self.seat_names.get(key)
        new = name if truncated else None
        if cur is not None and cur != new:
            self.mem.pop((session, seat, cur), None)
            self.mem_shown.pop((session, seat, cur), None)
            self.links.pop((session, seat), None)
        if new is None:
            self.seat_names.pop(key, None)
        else:
            self.seat_names[key] = new

    # --- 기록 ------------------------------------------------------------------------------------
    def observe(self, rec):
        """핸드 기록 하나를 통계(신뢰할 때만)와 JSONL 에 반영. 통계에 넣었으면 True."""
        try:
            with self.lock:
                return self._observe(rec)
        except Exception as exc:
            self._fail(exc)
            return False

    def _observe(self, rec):
        if not isinstance(rec, dict) or "seats" not in rec:
            return False
        self._jsonl(rec)
        session = rec.get("session")
        for k, seat in rec["seats"].items():
            if seat.get("name"):
                self._session_seat(session, k, seat["name"], seat.get("truncated"))
        if rec.get("confidence") != "ok":
            return False
        hid = rec.get("id")
        if hid and self.db.execute("SELECT 1 FROM observed WHERE hand_id=?", (hid,)).fetchone():
            return False  # 같은 핸드 두 번 (replay 등)
        now = float(rec.get("end") or time.time())
        board = " ".join(rec.get("board") or [])
        for k, seat in rec["seats"].items():
            name, st = seat.get("name"), seat.get("stats")
            if not name or not st:
                continue
            shown = seat.get("shown")
            if seat.get("truncated") and self.links.get((session, k)) and self.seat_names.get((session, k)) == name:
                name = self.links[(session, k)]      # 팝업으로 전체 이름이 확인된 좌석: 이제부터 DB 에 전체 이름으로 기록
            elif seat.get("truncated"):
                mk = (session, k, name)
                c = self.mem.setdefault(mk, {x: 0 for x in COUNTERS})
                for x in COUNTERS:
                    c[x] += int(st.get(x, 0))
                if shown:
                    self.mem_shown.setdefault(mk, []).append({"cards": shown, "board": board, "hand": hid})
                continue
            self.db.execute("INSERT OR IGNORE INTO players(name, first_seen, last_seen) VALUES (?, ?, ?)",
                            (name, now, now))
            sets = ", ".join(f"{x} = {x} + ?" for x in COUNTERS)
            self.db.execute(f"UPDATE players SET {sets}, last_seen = ? WHERE name = ?",
                            [int(st.get(x, 0)) for x in COUNTERS] + [now, name])
            if shown:
                self.db.execute("INSERT INTO shown VALUES (?, ?, ?, ?, ?)", (name, hid, " ".join(shown), board, now))
        if hid:
            self.db.execute("INSERT OR IGNORE INTO observed VALUES (?, ?)", (hid, now))
        self.db.commit()
        return True

    def _jsonl(self, rec):
        if not self.write_jsonl:
            return
        out = json.loads(json.dumps(rec, ensure_ascii=False, default=str))
        for k, seat in out.get("seats", {}).items():
            if seat.get("truncated"):
                seat["name"] = None
                seat["placeholder"] = f"seat-local:{k}"
        day = time.strftime("%Y-%m-%d", time.localtime(float(rec.get("start") or time.time())))
        self.hands_dir.mkdir(parents=True, exist_ok=True)
        with open(self.hands_dir / f"{day}.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(out, ensure_ascii=False) + "\n")

    # --- 조회 ------------------------------------------------------------------------------------
    def stats(self, name, truncated=False, seat=None, session=None, tab=None):
        """이름 하나의 통계. 잘린 이름은 (session, seat) 가 있어야 하고 그 세션 메모리에서만 찾음
        (팝업으로 전체 이름이 연결된 좌석은 전체 이름의 DB 통계 + 연결 전 세션 메모리를 합침).
        tab: 사이트 통계의 게임 종류 ('토너먼트'/'홀덤'). 없으면 홀덤 우선."""
        try:
            with self.lock:
                if not name:
                    return summarize({})
                if truncated:
                    session = session if session is not None else self.session
                    key = (session, seat, name)
                    shown = [s["cards"] for s in self.mem_shown.get(key, [])][-SHOWN_KEEP:]
                    full = self.links.get((session, seat)) if self.seat_names.get((session, seat)) == name else None
                    if full:
                        out = self._full_stats(full, tab, self.mem.get(key), shown)
                        out["linked_name"] = full
                        return out
                    return summarize(self.mem.get(key, {}), shown)
                return self._full_stats(name, tab)
        except Exception as exc:
            self._fail(exc)
            return summarize({})

    def _full_stats(self, name, tab, extra=None, extra_shown=()):
        row = self.db.execute(f"SELECT {', '.join(COUNTERS)} FROM players WHERE name = ?", (name,)).fetchone()
        c = dict(zip(COUNTERS, row)) if row else {}
        for k, v in (extra or {}).items():
            c[k] = c.get(k, 0) + v
        shown = [r[0].split() for r in self.db.execute(
            "SELECT cards FROM shown WHERE name = ? ORDER BY t DESC LIMIT ?", (name, SHOWN_KEEP))]
        if extra_shown:
            shown = (shown + list(extra_shown))[-SHOWN_KEEP:]
        return summarize(c, shown, self._site_row(name, tab))

    def get_for_table(self, names, session=None, tab=None):
        """{seat_key: (name, truncated)} → {seat_key: stats}. tab: 이 테이블의 게임 종류 ('토너먼트'/'홀덤')."""
        out = {}
        for k, v in (names or {}).items():
            if not v or not v[0]:
                continue
            out[k] = self.stats(v[0], bool(v[1]), seat=k, session=session, tab=tab)
        return out

    # --- 사이트('유저 정보' 팝업) 통계 ---------------------------------------------------------------
    def _site_row(self, name, tab=None):
        """(이름, 탭) 의 사이트 통계 dict 또는 None. tab 이 없으면 홀덤 → 가장 최근 순."""
        cols = "tab, vpip, pfr, ats, threebet, style, t"
        q = f"SELECT {cols} FROM site_stats WHERE name = ?"
        if tab:
            r = self.db.execute(q + " AND tab = ?", (name, tab)).fetchone()
        else:
            r = self.db.execute(q + " AND tab = ?", (name, DEFAULT_TAB)).fetchone() or \
                self.db.execute(q + " ORDER BY t DESC LIMIT 1", (name,)).fetchone()
        return dict(zip(("tab", "vpip", "pfr", "ats", "threebet", "style", "t"), r)) if r else None

    def site_stats(self, name, tab=None):
        try:
            with self.lock:
                return self._site_row(name, tab)
        except Exception as exc:
            self._fail(exc)
            return None

    def import_site_stats(self, name, stats, tab, seat=None, session=None):
        """팝업에서 읽은 (전체 이름, 통계, 게임 탭) 저장 (같은 이름+탭은 덮어씀). 저장했으면 True.
        stats: vpip/pfr/ats/threebet (0..100) + style. 잘린 이름 좌석 연결: 지금 좌석 중 잘린 이름이 name 의 접두사인 곳이
        정확히 하나면 (또는 seat 로 지정하면) 이 세션 동안 그 좌석을 name 으로 연결한다 (여럿이면 모호해서 연결 안 함).
        연결된 좌석은 이후 관측이 DB 에 전체 이름으로 쌓이고, 연결 전 세션 메모리 관측도 조회 때 합쳐 보여준다."""
        try:
            with self.lock:
                if not name or not tab or not isinstance(stats, dict):
                    return False
                vals = {}
                for k in ("vpip", "pfr", "ats", "threebet"):
                    v = stats.get(k)
                    if v is not None and not 0 <= float(v) <= 100:
                        return False
                    vals[k] = None if v is None else float(v)
                if all(v is None for v in vals.values()):
                    return False
                self.db.execute("INSERT OR REPLACE INTO site_stats VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                (name, tab, vals["vpip"], vals["pfr"], vals["ats"], vals["threebet"],
                                 stats.get("style"), time.time()))
                self.db.commit()
                self._link_truncated(name, seat, session)
                return True
        except Exception as exc:
            self._fail(exc)
            return False

    def _link_truncated(self, full, seat=None, session=None):
        session = session if session is not None else self.session
        if seat is not None:
            tn = self.seat_names.get((session, seat))
            cands = [seat] if tn and full.startswith(tn) else []
        else:
            cands = [k for (sess, k), tn in self.seat_names.items()
                     if sess == session and len(tn) >= 2 and len(full) > len(tn) and full.startswith(tn)]
        if len(cands) == 1:
            self.links[(session, cands[0])] = full
        return cands

    def linked_name(self, seat, session=None):
        """좌석이 팝업으로 연결된 전체 이름 (없으면 None)."""
        with self.lock:
            return self.links.get((session if session is not None else self.session, seat))

    # --- 글자 지문 (names.NameReader 와 연결) -----------------------------------------------------------
    def _fps(self):
        if self._fp_cache is None:
            self._fp_cache = []
            for name, w, h, blob in self.db.execute("SELECT name, w, h, fp FROM fingerprints"):
                try:
                    self._fp_cache.append((name, np.frombuffer(blob, np.uint8).reshape(h, w)))
                except Exception:
                    pass
        return self._fp_cache

    def resolve_name(self, name, fp):
        """OCR 이름 + 글자 지문 → DB 에 지문이 닮은 닉네임이 있으면 그 이름, 아니면 그대로."""
        if fp is None:
            return name
        try:
            import names as names_mod
            with self.lock:
                best, score = None, 0.0
                for n, f in self._fps():
                    s = names_mod.fp_similarity(fp, f)
                    if s > score:
                        best, score = n, s
            return best if best is not None and score >= FP_MATCH else name
        except Exception as exc:
            self._fail(exc)
            return name

    def learn_fingerprint(self, key, name, truncated, fp):
        """확정된 잘리지 않은 닉네임의 글자 지문을 저장 (잘린 이름은 저장 안 함)."""
        if truncated or not name or fp is None:
            return
        try:
            fp = np.ascontiguousarray(fp, np.uint8)
            with self.lock:
                rows = self.db.execute("SELECT rowid FROM fingerprints WHERE name = ? ORDER BY t DESC", (name,)).fetchall()
                import names as names_mod
                if any(names_mod.fp_similarity(fp, f) >= 0.97 for n, f in self._fps() if n == name):
                    return  # 거의 같은 것 이미 있음
                for (rid,) in rows[FP_KEEP - 1:]:
                    self.db.execute("DELETE FROM fingerprints WHERE rowid = ?", (rid,))
                self.db.execute("INSERT INTO fingerprints VALUES (?, ?, ?, ?, ?)",
                                (name, fp.shape[1], fp.shape[0], fp.tobytes(), time.time()))
                self.db.commit()
                self._fp_cache = None
        except Exception as exc:
            self._fail(exc)
