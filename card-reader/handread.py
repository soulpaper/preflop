"""상대 핸드 리딩(레인지 추정) — 상대가 이번 핸드에서 한 액션으로 가능한 보유 핸드를 추정해 한국어로 요약한다.

공개 API
  estimate(seats, board, players, dead=(), profiles=None, stack_bb=100) -> {seat_key: result}
  describe(result) -> [str, ...]      UI 에 보여줄 2~4줄 한국어 요약

입력
  seats   {seat_key: {"pos": "CO", "preflop": [...], "flop": [...], "turn": [...], "river": [...],
                      "opener": "UTG"(선택: 오픈/3벳한 사람 포지션), "profile": {...}(선택)}}
          액션은 문자열 또는 {"a": "bet", "size": 0.66} (size = 팟 대비 비율, 모르면 생략).
          프리플랍: open limp call 3bet call_3bet 4bet jam call_jam check blind
          포스트플랍: check bet call raise checkraise allin fold   (call 의 size 는 '받은 벳' 크기)
  board   카드 리스트(["As","7d","2c",...] / 정수 / "As7d2c" 문자열) 또는 {"flop":[3장],"turn":카드,"river":카드}.
          최종 보드만 알면 되고, 스트리트별 액션은 그 보드의 앞부분(플랍=3장, 턴=4장...)에서 적용한다.
          해당 스트리트 보드가 없으면 그 스트리트 액션은 건너뛰고 신뢰도를 낮춘다.
  dead    내 카드 등 죽은 카드. 보드와 함께 모든 레인지에서 제외한다.
  profiles {seat_key: {hands, vpip, pfr, threebet, fold_to_bet, af, wtsd, reliable}} (퍼센트 0~100)

결과 (좌석별 dict)
  range        {콤보('AhKd'): 확률}  합 1
  width_pct    유효 폭(%) = 가중치 분포의 '유효 콤보 수' / 1326 — 프리플랍 레인지 폭과 같은 척도
  preflop_width_pct  프리플랍 액션만 반영한 폭
  summary      "TT+, AJs+, KQs, AQo+ 외 12%"  (가장 가능성 높은 핸드 표기 + 표기에 빠진 확률)
  classes      {분류: 확률(%)}   보드 있음: 스트레이트 이상/셋·트립스/투페어/탑페어+/중간·약한 페어/드로우/노페어
                                 보드 없음(프리플랍): 프리미엄 페어/중간 페어/작은 페어/A-x 수딧/브로드웨이/수딧 커넥터/기타
  top_hands    [(핸드코드, 확률%), ...] 최대 5개
  confidence   "low" | "medium" | "high"
  note         한 줄 한국어 설명
  그 밖에 pos, label(예: "CO 오픈→콜"), preflop_action, street(마지막 반영 스트리트)

휴리스틱 (설명 가능한 가중치 곱셈)
  프리플랍: rangeq.villain_range (프로필이 신뢰되면 rangeq 가 폭을 섞어 줌).
  포스트플랍: 스트리트별로 그 시점 보드에서 각 콤보를 5단계(N 넛급 / W 투페어 / T 탑페어+ / M 중간·약한 / A 에어, 드로우는
  에어에 부분 가중)로 분류하고 액션별 배율표를 곱한다.
    작은 벳 병합 · 큰 벳/레이즈 양극화(중간 페어 크게 감소) · 체크레이즈 강+양극화 ·
    콜 응축(중간·드로우 유지, 최강 약간 감소, 에어 크게 감소) · 체크 캡(최강 일부 감소, 슬로플레이 잔존)
  프로필(신뢰될 때만, 가중치 = hands/(hands+50)):
    fold_to_bet 높음 → 계속하는 레인지가 더 강함 / 낮음(콜링스테이션) → 약한 패가 더 남음
    AF 높음 → 벳 레인지에 블러프 증가, 낮음 → 벳이 강함 (콜/체크에서는 반대로 최강 패 비중 조정)
  상대 간 카드 제거 효과는 무시하고 상대마다 독립 추정한다.
"""
import math

import evaluator as ev
import preflop as pf
import rangeq as rq

CLASSES_POST = ["스트레이트 이상", "셋/트립스", "투페어", "탑페어+", "중간·약한 페어", "드로우", "노페어"]
CLASSES_PRE = ["프리미엄 페어", "중간 페어", "작은 페어", "A-x 수딧", "브로드웨이", "수딧 커넥터", "기타"]

# 분류 버킷 인덱스
N, W, T, M, A = range(5)
_RK = "23456789TJQKA"
_STREETS = (("flop", 3, "플랍"), ("turn", 4, "턴"), ("river", 5, "리버"))

# --- 액션별 배율표 [N, W, T, M, A] -------------------------------------------------------
#   N 스트레이트 이상/셋·트립스, W 투페어, T 탑페어+(오버페어 포함), M 중간·약한 페어, A 에어(드로우는 A 와 DRAW 사이를 보간)
#   DRAW = 순수 드로우 콤보의 배율, 페어+드로우는 max(버킷 배율, DRAW*0.95) 쪽으로 끌어올린다.
MULT = {
    "bet_small":  ([1.00, 1.00, 1.00, 0.75, 0.40], 0.80),
    "bet_med":    ([1.00, 1.00, 1.00, 0.50, 0.30], 0.90),
    "bet_big":    ([1.00, 0.90, 0.80, 0.18, 0.25], 0.85),
    "raise":      ([1.00, 0.85, 0.45, 0.08, 0.10], 0.60),
    "checkraise": ([1.00, 0.85, 0.40, 0.06, 0.10], 0.70),
    "allin":      ([1.00, 0.80, 0.45, 0.07, 0.10], 0.45),
    "call_small": ([0.55, 0.80, 1.00, 1.00, 0.20], 0.95),
    "call_med":   ([0.70, 0.85, 0.95, 1.00, 0.15], 0.95),
    "call_big":   ([0.75, 0.90, 0.90, 0.65, 0.08], 0.85),
    "check":      ([0.40, 0.65, 0.85, 1.00, 1.00], 1.00),
}
_AGGR = ("bet_small", "bet_med", "bet_big", "raise", "checkraise", "allin")
_CALL = ("call_small", "call_med", "call_big")
SMALL_BET, BIG_BET, DEFAULT_SIZE = 0.40, 0.75, 0.55

KIND_KO = {"bet_small": "작은 벳", "bet_med": "벳", "bet_big": "큰 벳", "raise": "레이즈", "checkraise": "체크레이즈",
           "allin": "올인", "call_small": "콜", "call_med": "콜", "call_big": "콜", "check": "체크"}
KIND_NOTE = {
    "bet_small": "얇은 밸류와 약한 패가 섞인 넓은 범위",
    "bet_med": "밸류 위주, 일부 드로우·블러프",
    "bet_big": "강한 패 또는 드로우 위주",
    "raise": "아주 강한 패 또는 블러프로 양극화",
    "checkraise": "강한 패 위주 (양극화)",
    "allin": "강한 패 또는 블러프",
    "call_small": "중간 패·드로우 위주",
    "call_med": "중간 패·드로우 위주 (최상위 패는 레이즈했을 수도)",
    "call_big": "강한 패·드로우 위주 (약한 페어는 줄어듦)",
    "check": "캡된 범위 (최강 패는 줄어듦)",
}
PRE_KO = {"open": "오픈", "limp": "림프", "call": "콜", "call_open": "콜", "3bet": "3벳", "call_3bet": "3벳콜",
          "4bet": "4벳", "jam": "올인", "call_jam": "올인콜", "check": "체크", "blind": "블라인드"}

# --- 입력 정규화 -------------------------------------------------------------------------
_PRE_ALIAS = {"raise": "open", "cold_call": "call", "limp_call": "limp", "3-bet": "3bet", "threebet": "3bet",
              "4-bet": "4bet", "fourbet": "4bet", "5bet": "jam", "allin": "jam", "all_in": "jam", "call_open": "call",
              "call_allin": "call_jam"}
_POST_ALIAS = {"check_raise": "checkraise", "check-raise": "checkraise", "cr": "checkraise", "xr": "checkraise",
               "all_in": "allin", "all-in": "allin", "jam": "allin", "donk": "bet", "lead": "bet", "cbet": "bet",
               "c-bet": "bet", "limp": "call", "x": "check"}


def _split_action(a):
    if isinstance(a, dict):
        name = a.get("a", a.get("action", ""))
        size = a.get("size")
    else:
        name, size = a, None
    name = str(name or "").lower().strip()
    try:
        size = float(size) if size is not None else None
        if size is not None and (size != size or size <= 0):
            size = None
    except (TypeError, ValueError):
        size = None
    return name, size


def _norm_pre(actions):
    out = []
    for a in actions or []:
        n, _ = _split_action(a)
        n = _PRE_ALIAS.get(n, n)
        if n:
            out.append(n)
    return out


def _norm_post(actions):
    """스트리트 액션 → [(kind, size)] ; fold 제외, 체크→레이즈는 체크레이즈 하나로 합친다."""
    raw = []
    for a in actions or []:
        n, s = _split_action(a)
        n = _POST_ALIAS.get(n, n)
        if n in ("check", "bet", "call", "raise", "checkraise", "allin"):
            raw.append((n, s))
    out = []
    for n, s in raw:
        if n in ("raise", "allin") and out and out[-1][0] == "check":
            out[-1] = ("checkraise" if n == "raise" else "allin", s)   # 체크 후 올인은 올인 그대로
            continue
        out.append((n, s))
    kinds = []
    for n, s in out:
        if n == "bet":
            sz = DEFAULT_SIZE if s is None else s
            kinds.append(("bet_small" if sz <= SMALL_BET else "bet_big" if sz >= BIG_BET else "bet_med", s))
        elif n == "call":
            sz = DEFAULT_SIZE if s is None else s
            kinds.append(("call_small" if sz <= SMALL_BET else "call_big" if sz >= BIG_BET else "call_med", s))
        else:
            kinds.append((n, s))
    return kinds


def _to_card(c):
    if isinstance(c, int):
        return c if 0 <= c < 52 else None
    try:
        return ev.parse_card(str(c))
    except (ValueError, TypeError, IndexError):
        return None


def _cards(seq):
    if isinstance(seq, str):
        seq = [seq[i:i + 2] for i in range(0, len(seq) - len(seq) % 2, 2)] if " " not in seq.strip() else seq.split()
    out = []
    for c in seq or []:
        x = _to_card(c)
        if x is not None and x not in out:
            out.append(x)
    return out


def _board_cards(board):
    if isinstance(board, dict):
        seq = []
        for k in ("flop", "turn", "river"):
            v = board.get(k)
            if v is None:
                continue
            seq.extend(_cards(v if isinstance(v, (list, tuple, str)) else [v]))   # 'As7d2c' 문자열도 (전엔 통째로 버려짐)
        return _cards(seq)
    return _cards(board)


# --- 콤보 / 분류 캐시 --------------------------------------------------------------------
def _code(hi, lo):
    r1, r2 = hi >> 2, lo >> 2
    if r1 == r2:
        return _RK[r1] * 2
    if r1 < r2:
        r1, r2 = r2, r1
        hi, lo = lo, hi
    return _RK[r1] + _RK[r2] + ("s" if (hi & 3) == (lo & 3) else "o")


_CODE_CACHE = {}


def _combo_code(hi, lo):
    k = (hi, lo)
    v = _CODE_CACHE.get(k)
    if v is None:
        v = _CODE_CACHE[k] = _code(hi, lo)
    return v


# 스트레이트 창(비트0 = 낮은 A, 비트1..13 = 랭크 2..A)
_WINDOWS = [0b11111 << i for i in range(10)]


def _rmask(ranks):
    m = 0
    for r in ranks:
        m |= 1 << (r + 1)
        if r == 12:
            m |= 1
    return m


def _straight_outs(allmask, bmask):
    """보드만으로는 안 되고 내 카드가 기여하는 스트레이트 완성 랭크 수 (갓샷=1, 양차=2)."""
    outs = set()
    for w in _WINDOWS:
        if bin(w & allmask).count("1") == 4:
            miss = w & ~allmask
            if (w & bmask) == (w & ~miss):    # 보드가 이미 4장 → 내 카드 기여 없음
                continue
            outs.add(12 if miss == 1 else miss.bit_length() - 2)
    return len(outs)


_BOARD_CACHE = {}


class _BoardInfo:
    __slots__ = ("board", "score", "ranks", "rset", "rmax", "bmask", "suits", "cache", "n")

    def __init__(self, board):
        self.board = list(board)
        self.n = len(board)
        self.score = ev.evaluate(self.board)
        self.ranks = [c >> 2 for c in board]
        self.rset = set(self.ranks)
        self.rmax = max(self.ranks)
        self.bmask = _rmask(self.rset)
        self.suits = [0] * 4
        for c in board:
            self.suits[c & 3] += 1
        self.cache = {}


def _board_info(board):
    key = tuple(board)
    bi = _BOARD_CACHE.get(key)
    if bi is None:
        if len(_BOARD_CACHE) > 300:
            _BOARD_CACHE.clear()
        bi = _BOARD_CACHE[key] = _BoardInfo(board)
    return bi


def _classify(bi, hi, lo):
    """콤보 → (버킷, 클래스 인덱스(CLASSES_POST), 드로우 정도 0/0.5/1)."""
    got = bi.cache.get((hi, lo))
    if got is not None:
        return got
    score = ev.evaluate([hi, lo] + bi.board)
    cat, bcat = score[0], bi.score[0]
    r1, r2 = hi >> 2, lo >> 2
    bucket = cls = None
    if cat >= 4 and (cat > bcat or score != bi.score):
        bucket, cls = N, 0
    elif cat == 3 and bcat < 3:
        bucket, cls = N, 1
    elif cat == 2 and bcat < 2:
        if bcat == 1:
            # 보드 페어 + 내 원페어 = 실제로는 원페어 강도: 내 페어(포켓 페어 또는 보드와 맞춘 카드)로 나눈다.
            # 예전엔 전부 '투페어'(T 버킷)로 봐서 A9439 보드에서 범위가 '투페어 91%' 로 표시되고, 보드 아래 포켓 페어(88-55)가
            # 벳 액션에 탑페어처럼 살아남아 범위가 엉뚱하게 좁혀졌다 (2026-10-30 라이브)
            own = r1 if r1 == r2 else (r1 if r1 in bi.rset else r2)
            top = own > bi.rmax if r1 == r2 else own == bi.rmax
            bucket, cls = (T, 3) if top else (M, 4)
        else:
            bucket, cls = W, 2
    if bucket is None:
        if r1 == r2:
            if r1 > bi.rmax:
                bucket, cls = T, 3
            elif r1 not in bi.rset:
                bucket, cls = M, 4
        elif (r1 in bi.rset and r1 == bi.rmax) or (r2 in bi.rset and r2 == bi.rmax):
            bucket, cls = T, 3
        elif r1 in bi.rset or r2 in bi.rset:
            bucket, cls = M, 4
    dflag = 0.0
    if bucket in (None, M, T) and bi.n < 5:
        s1, s2 = hi & 3, lo & 3
        if (bi.suits[s1] + 1 + (s1 == s2) == 4) or (s1 != s2 and bi.suits[s2] + 1 == 4):
            dflag = 1.0
        else:
            k = _straight_outs(_rmask(bi.rset | {r1, r2}), bi.bmask)
            dflag = 1.0 if k >= 2 else 0.5 if k == 1 else 0.0
    if bucket is None:
        bucket = A
        cls = 5 if dflag > 0 else 6
    out = (bucket, cls, dflag)
    bi.cache[(hi, lo)] = out
    return out


def _combo_class_pre(code):
    if len(code) == 2:
        r = _RK.index(code[0])
        return 0 if r >= 9 else 1 if r >= 5 else 2           # JJ+ / 77-TT / 22-66
    r1, r2, suited = _RK.index(code[0]), _RK.index(code[1]), code[2] == "s"
    if suited and r1 == 12:
        return 3
    if r1 >= 8 and r2 >= 8:
        return 4
    if suited and r1 - r2 <= 2:
        return 5
    return 6


# --- 프리플랍 레인지 ----------------------------------------------------------------------
_PRE_CACHE = {}


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None


def _pre_action(seq):
    """프리플랍 액션 열 → rangeq 액션 (모르면 None)."""
    if not seq:
        return None
    last = seq[-1]
    if last in ("call_jam", "jam", "4bet", "call_3bet", "3bet", "open", "limp"):
        return last
    if last == "call":
        if "open" in seq or "3bet" in seq:
            return "call_3bet"
        if "limp" in seq:
            return "limp"
        return "call_open"
    if last in ("check", "blind"):
        return "blind"
    return None


def _pre_range(action, pos, players, stack_bb, profile, opener, ante=False):
    """(코드→가중치, 액션 인식 여부)"""
    prof_key = None
    if profile and rq.profile_reliable(profile):
        prof_key = tuple(_num(profile.get(k)) for k in ("hands", "vpip", "pfr", "threebet"))
    key = (action, pos, players, stack_bb, opener, prof_key, bool(ante))
    got = _PRE_CACHE.get(key)
    if got is not None:
        return got
    if action is None:
        vpip = _num((profile or {}).get("vpip")) if profile and rq.profile_reliable(profile) else None
        if vpip:
            rng = {h: 1.0 for h in pf.top_pct(max(3.0, min(100.0, vpip)))}
        else:
            rng = rq.villain_range("call_open", pos, players, stack_bb, profile, opener=opener, ante=bool(ante))
        res = (rng, False)
    else:
        res = (rq.villain_range(action, pos, players, stack_bb, profile, opener=opener, ante=bool(ante)), True)
    if len(_PRE_CACHE) > 500:
        _PRE_CACHE.clear()
    _PRE_CACHE[key] = res
    return res


def _clip(x, lo, hi):
    return max(lo, min(hi, x))


# --- 프로필 보정 ---------------------------------------------------------------------------
def _profile_factors(profile):
    """(adj_f, afx): adj_f>0 = 폴드 많음, afx>0 = 공격적. 신뢰도 가중 적용. 신뢰 못 하면 (0, 0)."""
    if not profile or not rq.profile_reliable(profile):
        return 0.0, 0.0
    w = rq.profile_weight(profile)
    f, af = _num(profile.get("fold_to_bet")), _num(profile.get("af"))
    adj_f = _clip((f - 45.0) / 35.0, -1, 1) * w if f is not None else 0.0
    afx = _clip((af - 2.0) / 2.0, -1, 1) * w if af is not None else 0.0
    return adj_f, afx


def _kind_mults(kind, adj_f, afx):
    base, draw = MULT[kind]
    m = list(base)
    d = draw
    if kind in _AGGR:
        weak = _clip(1 - 0.4 * adj_f, 0.5, 1.5)
        m[M] *= weak
        m[A] *= weak * _clip(1 + 0.6 * afx, 0.4, 1.6)
        d *= _clip(1 + 0.3 * afx, 0.6, 1.3)
        strong = _clip(1 - 0.1 * afx, 0.8, 1.2)
        for i in (N, W, T):
            m[i] *= strong
    elif kind in _CALL:
        weak = _clip(1 - 0.4 * adj_f, 0.5, 1.5)
        m[M] *= weak
        m[A] *= weak
        m[N] *= _clip(1 - 0.15 * afx, 0.8, 1.2)
        m[W] *= _clip(1 - 0.1 * afx, 0.9, 1.1)
    elif kind == "check":
        m[N] *= _clip(1 - 0.3 * afx, 0.6, 1.4)
        m[W] *= _clip(1 - 0.2 * afx, 0.7, 1.3)
    return m, d


def _apply_kind(weights, bi, kind, adj_f, afx, combos):
    m, d = _kind_mults(kind, adj_f, afx)
    cls = _classify
    for i, key in enumerate(combos):
        w = weights[i]
        if w <= 0:
            continue
        b, _, df = cls(bi, key[0], key[1])
        f = m[b]
        if df:
            if b == A:
                f = f + df * (d - f)
            else:
                f = f + df * max(0.0, d * 0.95 - f)
        weights[i] = w * f


# --- 요약 ---------------------------------------------------------------------------------
def _runs(idx):
    idx = sorted(idx)
    runs = []
    for i in idx:
        if runs and runs[-1][1] == i - 1:
            runs[-1][1] = i
        else:
            runs.append([i, i])
    return runs


_R = "AKQJT98765432"


def _tokens(codes):
    """핸드코드 집합 → [(토큰, 포함 코드 집합)] (표준 표기, 페어 → 수딧 → 오프수트 순)."""
    out = []
    pairs = {_R.index(c[0]) for c in codes if len(c) == 2}
    for a, b in _runs(pairs):
        cs = {_R[i] * 2 for i in range(a, b + 1)}
        if a == b:
            tok = _R[a] * 2
        elif a == 0:
            tok = _R[b] * 2 + "+"
        else:
            tok = _R[a] * 2 + "-" + _R[b] * 2
        out.append((tok, cs))
    for suf in ("s", "o"):
        by_hi = {}
        for c in codes:
            if len(c) == 3 and c[2] == suf:
                by_hi.setdefault(_R.index(c[0]), set()).add(_R.index(c[1]))
        for hi in sorted(by_hi):
            for a, b in _runs(by_hi[hi]):
                cs = {_R[hi] + _R[i] + suf for i in range(a, b + 1)}
                if a == b:
                    tok = _R[hi] + _R[a] + suf
                elif a == hi + 1:
                    tok = _R[hi] + _R[b] + suf + "+"
                else:
                    tok = _R[hi] + _R[a] + suf + "-" + _R[hi] + _R[b] + suf
                out.append((tok, cs))
    return out


def _summary(mass, live):
    """mass: 코드→가중치 합, live: 코드→살아있는 콤보 수."""
    total = sum(mass.values())
    if total <= 0:
        return ""
    dens = {c: mass[c] / live[c] for c in mass if live.get(c)}
    peak = max(dens.values())
    chosen = {c for c, d in dens.items() if d >= 0.4 * peak}
    toks = _tokens(chosen)
    toks.sort(key=lambda t: 0)  # 안정 정렬 유지
    if len(toks) > 7:
        keep = sorted(range(len(toks)), key=lambda i: -sum(mass[c] for c in toks[i][1]))[:7]
        toks = [toks[i] for i in sorted(keep)]
    covered = sum(mass[c] for _, cs in toks for c in cs)
    rest = max(0.0, 100.0 * (1 - covered / total))
    text = ", ".join(t for t, _ in toks)
    return text + (f" 외 {rest:.0f}%" if rest >= 0.5 else "")


# --- 한 좌석 추정 ---------------------------------------------------------------------------
def _label(pos, pre_raw):
    acts = [PRE_KO.get(a, a) for a in pre_raw]
    return (pos or "?") + (" " + "→".join(acts) if acts else "")


def _estimate_one(seat, board, players, dead, profile, stack_bb, ante=False):
    pos = seat.get("pos")
    pre_raw = _norm_pre(seat.get("preflop"))
    action, recognized = _pre_action(pre_raw), True
    players = players or 9
    prof = profile if profile else seat.get("profile")
    rng, recognized = _pre_range(action, pos, players, stack_bb, prof, seat.get("opener"), ante)
    blocked = set(board) | set(dead)

    combos, weights, codes = [], [], []
    for code, w in rng.items():
        if w <= 0:
            continue
        for c1, c2 in rq.COMBOS[code]:
            if c1 in blocked or c2 in blocked:
                continue
            combos.append((c1, c2) if c1 > c2 else (c2, c1))
            weights.append(float(w))
    if not combos:   # 레인지가 전부 죽은 카드와 겹침 → 랜덤으로 대체
        for code in rq.G:
            for c1, c2 in rq.COMBOS[code]:
                if c1 not in blocked and c2 not in blocked:
                    combos.append((c1, c2) if c1 > c2 else (c2, c1))
                    weights.append(1.0)
    pre_width = _eff_width(weights)

    adj_f, afx = _profile_factors(prof)
    nb = len(board)
    last_kind = last_street = None
    n_actions = 0
    skipped = False
    for name, need, ko in _STREETS:
        kinds = _norm_post(seat.get(name))
        if not kinds:
            continue
        if nb < need:
            skipped = True
            continue
        bi = _board_info(board[:need])
        for kind, _ in kinds:
            _apply_kind(weights, bi, kind, adj_f, afx, combos)
            last_kind, last_street = kind, ko
            n_actions += 1
        if sum(weights) <= 0:
            weights = [1.0] * len(weights)

    total = sum(weights)
    if total <= 0:
        weights, total = [1.0] * len(combos), float(len(combos))
    # 극소 가중치 정리
    cut = max(weights) * 1e-4
    keep = [i for i, w in enumerate(weights) if w >= cut]
    combos = [combos[i] for i in keep]
    weights = [weights[i] for i in keep]
    total = sum(weights)

    mass, live = {}, {}
    for (hi, lo), w in zip(combos, weights):
        c = _combo_code(hi, lo)
        mass[c] = mass.get(c, 0.0) + w
    for code in mass:
        live[code] = sum(1 for a, b in rq.COMBOS[code] if a not in blocked and b not in blocked)

    # 분류
    if nb >= 3:
        names = CLASSES_POST
        bi = _board_info(board)
        acc = [0.0] * 7
        for (hi, lo), w in zip(combos, weights):
            acc[_classify(bi, hi, lo)[1]] += w
    else:
        names = CLASSES_PRE
        acc = [0.0] * 7
        for code, m_ in mass.items():
            acc[_combo_class_pre(code)] += m_
    classes = {n: round(a / total * 100, 1) for n, a in zip(names, acc)}

    top = sorted(mass.items(), key=lambda kv: -kv[1])[:5]
    top_hands = [(c, round(m_ / total * 100, 1)) for c, m_ in top]

    # 신뢰도
    info = (1.5 if (recognized and pos) else 1.0 if recognized else 0.3)
    info += 1.0 * n_actions
    if skipped:
        info -= 0.3
    if adj_f or afx:
        info += 0.5
    confidence = "high" if info >= 3.0 else "medium" if info >= 1.5 else "low"

    width = _eff_width(weights)
    out = {
        "range": {ev.card_str(h) + ev.card_str(l): w / total for (h, l), w in zip(combos, weights)},
        "width_pct": round(width, 1),
        "preflop_width_pct": round(pre_width, 1),
        "summary": _summary(mass, live),
        "classes": classes,
        "top_hands": top_hands,
        "confidence": confidence,
        "pos": pos,
        "label": _label(pos, pre_raw),
        "preflop_action": action,
        "street": last_street,
        "n_actions": n_actions,
    }
    out["note"] = _note(out, last_kind, last_street, recognized, skipped, bool(adj_f or afx))
    return out


def _eff_width(weights):
    s = sum(weights)
    s2 = sum(w * w for w in weights)
    return (s * s / s2) / 1326 * 100 if s2 > 0 else 0.0


def _note(res, kind, street, recognized, skipped, used_profile):
    if kind:
        text = f"{street} {KIND_KO[kind]} → {KIND_NOTE[kind]}"
    elif recognized and res["preflop_action"]:
        text = f"{res['label']} → 프리플랍 상위 약 {res['width_pct']:.0f}% 핸드"
    else:
        text = "액션 정보 부족 → 포지션 기준 일반 범위로 추정"
    if skipped:
        text += " (일부 액션은 보드 정보가 없어 미반영)"
    if used_profile:
        text += " [성향 반영]"
    return text


def estimate(seats, board, players, dead=(), profiles=None, stack_bb=100, ante=False):
    """상대별 핸드 레인지 추정. 자세한 입출력은 모듈 docstring 참고."""
    board_c = _board_cards(board)
    dead_c = [c for c in _cards(dead) if c not in board_c]
    try:
        players = int(players) if players else 9
    except (TypeError, ValueError):
        players = 9
    results = {}
    for key, seat in (seats or {}).items():
        seat = seat if isinstance(seat, dict) else {}
        try:
            results[key] = _estimate_one(seat, board_c, players, dead_c, (profiles or {}).get(key),
                                         stack_bb or 100, ante)
        except Exception as e:  # UI 가 죽지 않도록: 일반 레인지로 대체
            results[key] = _fallback(seat, board_c, dead_c, e)
    return results


def _fallback(seat, board, dead, err):
    blocked = set(board) | set(dead)
    rng = {}
    for code in rq.G:
        for c1, c2 in rq.COMBOS[code]:
            if c1 not in blocked and c2 not in blocked:
                rng[ev.card_str(max(c1, c2)) + ev.card_str(min(c1, c2))] = 1.0
    n = len(rng) or 1
    return {"range": {k: 1.0 / n for k in rng}, "width_pct": 100.0, "preflop_width_pct": 100.0, "summary": "전체 핸드",
            "classes": {}, "top_hands": [], "confidence": "low", "pos": seat.get("pos"), "label": seat.get("pos") or "?",
            "preflop_action": None, "street": None, "n_actions": 0, "note": "추정 실패 → 랜덤 핸드로 가정",
            "error": repr(err)}


# --- UI 요약 ------------------------------------------------------------------------------
_CONF_KO = {"low": "낮음", "medium": "보통", "high": "높음"}


def describe(result):
    """결과 → UI 에 보여줄 2~4줄 한국어."""
    lines = []
    summ = result.get("summary") or ""
    lines.append(f"{result.get('label', '?')}: 예상 상위 {result.get('width_pct', 0):.0f}%"
                 + (f" ({summ})" if summ else ""))
    classes = result.get("classes") or {}
    shown = [(n, p) for n, p in sorted(classes.items(), key=lambda kv: -kv[1]) if p >= 5][:3]
    if shown:
        head = "보드 기준" if CLASSES_POST[0] in classes else "프리플랍 기준"
        lines.append(f"{head}: " + " · ".join(f"{n} {p:.0f}%" for n, p in shown))
    th = result.get("top_hands") or []
    if th:
        lines.append("가능성 높은 핸드: " + ", ".join(f"{c} {p:.0f}%" for c, p in th[:4]))
    lines.append(f"{result.get('note', '')} (신뢰도 {_CONF_KO.get(result.get('confidence'), '낮음')})")
    return lines[:4]


if __name__ == "__main__":
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    demo = {
        "UTG": {"pos": "UTG", "preflop": ["open"], "flop": [{"a": "bet", "size": 0.8}]},
        "CO": {"pos": "CO", "preflop": ["open", "call"], "flop": ["check"], "turn": ["check"]},
        "BTN": {"pos": "BTN", "preflop": ["limp"]},
    }
    for k, r in estimate(demo, ["Ad", "7s", "2c"], 9, dead=["Kh", "Qh"]).items():
        print(k)
        for ln in describe(r):
            print("  ", ln)
