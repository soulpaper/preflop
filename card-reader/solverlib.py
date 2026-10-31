"""GTO 포스트플랍 솔루션 라이브러리 (TexasSolver 로 미리 푼 헤즈업 SRP 플랍) + 조회 API.

토너먼트(9max, 앤티) 싱글 레이즈 팟 헤즈업 스팟을 TexasSolver v0.2.0 (AGPL-3.0, 별도 실행 파일) 로 미리 풀어
`gto_lib/<spot>_<stack>bb.json.gz` 에 콤보별 전략만 압축 저장한다. 실행 중에는 솔버를 돌리지 않는다 (조회만).
솔브 실행은 build_gto_lib.py (원본 덤프는 preflop-tools/solves/, 저장소 밖).

스팟 (opener 오픈 → caller 콜, 헤즈업)
  BTN_BB, CO_BB, HJ_BB, UTG_BB : 오프너 IP, BB OOP.  SB_BB : SB OOP, BB IP.  BTN_SB : BTN IP, SB OOP.
  팟 = 오픈×2 + 죽은 블라인드 + 앤티(9 × 0.125BB), 남은 스택 = 스택 − 오픈.  오픈 크기는 advisor 와 같다
  (CO/BTN 2.2BB, 그 외 2.5BB, SB 3.0BB).  스택 20 / 30 / 40 / 60BB.
  레인지는 항상 preflop.ranges 를 그 자리에서 읽는다 (오픈 레인지 / vsopen 의 '콜' 부분, 9명, ante=True).
  레인지가 바뀌면 range_hash 가 달라져 build_gto_lib.py 가 그 스팟을 다시 푼다 (--check-ranges 로 확인).

트리 (토너먼트용으로 작게)
  플랍: 벳 33% / 75%, 레이즈 50%팟(≈ 33% 벳에 3.5x, 75% 벳에 2.7x), 올인 임계 67%
  턴: 벳 50% / 100%, 레이즈 60%     리버: 벳 50% / 100% + 올인, 레이즈는 올인만.  OOP 의 턴/리버 동크 벳 없음.

플랍 서브셋 / 근사 (SUBSET)
  1755개 전략적으로 다른 플랍(수트 동형 클래스)을 실제 출현 빈도로 가중해, 거리(아래)의 가중 평균이 가장 많이 줄어드는
  플랍을 하나씩 고른 순서 목록이다 (욕심쟁이 facility location). 앞에서부터 아무 길이나 잘라도 좋은 서브셋이 된다.
  조회 시에는 그 스팟 파일에 실제로 풀려 있는 플랍 중 가장 가까운 것으로 보낸다.
  거리: 페어 여부(다르면 금지) · 수트 패턴(모노/투톤/레인보우, 다르면 +20~30, 투톤의 어느 두 장이 같은 무늬인지 +2.5)
        · 랭크 2.0×|high| + 1.4×|mid| + 1.0×|low| · 스트레이트 연결성 1.2×차이 · 브로드웨이 장수 0.8×차이.
  카드 근사: 보드 랭크 → 대상 보드 랭크를 앵커로 히어로 카드 랭크를 단조 보간한다 (탑페어는 탑페어, 오버카드는 오버카드로),
  무늬는 보드의 무늬 그룹(플러시 무늬 → 플러시 무늬)을 맞춘 순열을 히어로 카드에도 똑같이 적용한다 (플러시 드로우/블로커 보존).
  예상 오차 (빈도 가중 평균 거리, 서브셋 길이별): 10개 7.6 · 25개 5.3 · 49개 3.9 · 95개 2.9 · 150개 2.2 · 200개 1.9.
  거리 2 ≈ 하이카드 한 칸 차이, 3~4 ≈ 미들 카드까지 한두 칸 / 연결성 한 단계 차이.
  build_gto_lib.py --eval-mapping 은 풀린 플랍끼리 서로 근사했을 때의 콤보 전략 차이(빈도 절대 오차 평균)를 실제로 잰다.
  보드가 정확히 같은 클래스면(exact_flop) 근사 없음.
  수렴: 0.8% (팟 대비 exploitability). K72r 실측: 1.6% 에서 멈추면 0.5% 대비 콤보 전략 차이 평균 ~15% → 0.8% 를 쓴다.

턴 (선택, gto_lib/turn/<spot>_<stack>bb/<flop>.json.gz, 플랍당 ~0.5MB)
  같은 솔브(dump_rounds 2)에서 턴 49장 × 플랍 라인(x_x, x_bet33_c, x_bet75_c, bet33_c, bet75_c)의 첫 액션/벳 대응 노드.
  턴 카드도 같은 랭크 보간 + 무늬 순열로 근사한다. ctx 에 플랍 액션이 없으면 팟 증가 배수로 라인을 추정한다.
  리버는 저장하지 않는다.

공개 API
  lookup(hero_pos, villain_pos, stack_bb, hero_cards, board, node, players=9, spr=None, call_ratio=None,
         line=None, pot_growth=None) -> dict | None      (board 3장 = 플랍, 4장 = 턴)
      node: 'oop_first' / 'ip_vs_check' / ('oop_vs_bet', 0.5) / 'ip_vs_bet(75)' / 'oop_vs_raise' / 'ip_vs_raise'
      -> {actions: {name: freq}, ev: None, mapped_flop, exact_flop, exact_spot, spot, stack_used, ...}
  gto_advice(ctx) -> advisor 형식 dict | None   (헤즈업 플랍/턴 결정, 풀린 스팟이 있을 때만)
  spot_ranges(spot, stack) / to_ts_range / parse_ts_range / canon_flop / map_hand / available()
"""
import glob
import gzip
import hashlib
import itertools
import json
import math
import os
import re
import sys
import time

RANKS = "23456789TJQKA"   # evaluator 와 같은 순서 / 정수: rank*4 + suit (= evaluator 카드 정수)
SUITS = "shdc"
HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS_DIR = os.environ.get("PREFLOP_TOOLS") or os.path.normpath(os.path.join(HERE, "..", "..", "preflop-tools"))
SOLVER_DIR = os.path.join(TOOLS_DIR, "TexasSolver")
SOLVES_DIR = os.path.join(TOOLS_DIR, "solves")
LIB_DIR = os.path.join(HERE, "gto_lib")
LIB_VERSION = 1

ANTE_TOTAL = 9 * 0.125     # 9명 × 0.125BB
PLAYERS = 9
STACKS = (20, 30, 40, 60)
SCALE = 100                # 솔버 칩 = BB × 100 (솔버가 금액을 정수로 반올림하므로)
SPOTS = {
    "BTN_BB": {"opener": "BTN", "caller": "BB", "open": 2.2, "dead": 0.5},
    "CO_BB": {"opener": "CO", "caller": "BB", "open": 2.2, "dead": 0.5},
    "HJ_BB": {"opener": "HJ", "caller": "BB", "open": 2.5, "dead": 0.5},
    "UTG_BB": {"opener": "UTG", "caller": "BB", "open": 2.5, "dead": 0.5},
    "SB_BB": {"opener": "SB", "caller": "BB", "open": 3.0, "dead": 0.0},
    "BTN_SB": {"opener": "BTN", "caller": "SB", "open": 2.2, "dead": 1.0},
}
# 풀린 파일이 없을 때 대신 쓸 스팟 (같은 IP/OOP 구조)
SPOT_FALLBACK = {
    "BTN_BB": ["CO_BB", "HJ_BB"], "CO_BB": ["BTN_BB", "HJ_BB"], "HJ_BB": ["CO_BB", "UTG_BB", "BTN_BB"],
    "UTG_BB": ["HJ_BB", "CO_BB"], "SB_BB": [], "BTN_SB": ["BTN_BB", "CO_BB"],
}
TREE = {
    "flop": {"bet": [33, 75], "raise": [50], "allin": False},
    "turn": {"bet": [50, 100], "raise": [60], "allin": False},
    "river": {"bet": [50, 100], "raise": [], "allin": True},
    "allin_threshold": 0.67,
}
ACCURACY = 0.8        # 팟 대비 % (exploitability)
MAX_ITER = 200
STALE_PENALTY = 3.0  # 예전 레인지로 푼 플랍은 거리 +3 (새 레인지 솔루션 우선)

NODES = ("oop_first", "ip_vs_check", "oop_vs_bet", "ip_vs_bet", "oop_vs_raise", "ip_vs_raise")
NODE_PLAYER = {"oop_first": "oop", "oop_vs_bet": "oop", "oop_vs_raise": "oop",
               "ip_vs_check": "ip", "ip_vs_bet": "ip", "ip_vs_raise": "ip"}
ALPH = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"   # 64 단계 (1/63 ≈ 1.6%)
QMAX = len(ALPH) - 1
AIDX = {ch: i for i, ch in enumerate(ALPH)}
MISSING = "!"

# 빈도 가중 욕심쟁이 순서 (앞에서부터 자르면 서브셋). _generate_subset() 으로 다시 만들 수 있다.
SUBSET = ["Qs8h4s", "Qs8h4d", "Js7h7d", "Js7s7h", "Qs8s4s", "9s5h3h", "AsJs7h", "9s5h3d", "AsJh7d", "7s4h4d",
    "7s4s4h", "Ks6s3h", "KsKh9d", "KsKh9s", "Js9h5h", "7s5h3s", "Ks6h3d", "KsJh4h", "Ts7s4h", "Js9h6d", "7s5h3d",
    "KsJh9s", "9s5s3s", "KsJh3d", "AsJs7s", "As8h4h", "Js4h2s", "9s7h5s", "KsJh9d", "Ts6h3d", "Qs3h3d", "Qs3s3h",
    "QsTs6h", "JsJh5d", "JsJh5s", "8s8h8d", "AsQh4s", "5s4h2h", "As9h5d", "9s7h5d", "AsQhTh", "8s8h6d", "8s8h6s",
    "As5h3s", "Js9h8s", "Qs4h2d", "Ks9h6s", "8s6s2h", "Qs6h3h", "5s4h2d", "AsQhTd", "QsTh5d", "5s3h3d", "5s3s3h",
    "Js8h3s", "AsQh4d", "KsTs2h", "As5h2d", "Ts8h4d", "Ks6s3s", "8s7h5h", "8s6h2d", "AsQh8h", "Js9h8d", "AsAh9d",
    "AsAh9s", "Ks8s6h", "Ks9h6d", "QsThTd", "QsTsTh", "Ts5h3h", "Js7h3d", "Ts8s6h", "As7h4d", "QsTh9h", "QsTh3s",
    "8s4h2s", "As7h4s", "Ks4h2h", "Js9s6s", "7s5s3s", "6s5s3h", "Qs6s3h", "AsQs3h", "As6h6d", "As6s6h", "9s7h3s",
    "Ks9h6h", "KsJh7d", "KsJs3s", "KsKh3d", "KsKh3s", "TsTh6d", "TsTh6s", "Js9s3h", "Js9h2d", "Ts8h3h", "Js6h4s",
    "9s9h6d", "9s9h6s", "AsTh7s", "8s6h5d", "6s6h4d", "6s6h4s", "Js4h2d", "As9s3h", "KsQsTh", "QsQhQd", "9s3h3d",
    "9s3s3h", "AsKh8d", "9s7h3d", "8s4h2d", "QsTh9d", "KsJh5s", "Ks8h2d", "Ks6h3s", "AsQh3h", "Js4s2h", "KsJs9s",
    "Qs6h3d", "7s4h2h", "AsKsJh", "Ts6s3s", "AsTh2d", "QsTh3h", "3s3h3d", "Ts8h6d", "AsQhQd", "AsQsQh", "Ks8h8d",
    "Ks8s8h", "8s6s4h", "Ks7h5h", "9s8h6h", "Ts8h3s", "9s5s2h", "KsJs7h", "QsQh5d", "QsQh5s", "As4h2h", "6s5h3d",
    "AsTh7h", "AsQh8s", "Ts5h5d", "Ts5s5h", "As9s5s", "9s7s5s", "Js7h4h", "Qs8s6h", "Ks7h5d", "6s4h2h", "Js9h7h",
    "9s8h6d", "KsQhTd", "7s7h4d", "7s7h4s", "7s6h4h", "QsTh7s", "9s8h3h", "Js5h4d", "JsTh5d", "As7s4h", "Ks8h2h",
    "As9h3s", "6s4h2d", "AsKs6h", "Js7s4h", "AsTh8d", "Ks4h2d", "Ts4h2d", "Qs9h7d", "5s3h2s", "AsAh4d", "AsAh4s",
    "QsJh3d", "QsTs8h", "4s2h2d", "4s2s2h", "Ts9h7h", "Qs4s2s", "KsJh7h", "Js7h5d", "Qs6h3s", "Ts5h3s", "KsQh5d",
    "As6h3h", "As9s8h", "As8h7d", "9s7s3h", "KsJs4h", "8s6h3h", "AsKh3d", "Js9h5s", "Qs8h6h", "QsJsTh", "8s6h4d",
    "Qs2h2d", "Qs2s2h", "5s4s2s"]


# --- 카드 ---------------------------------------------------------------------------
def card(c):
    """'Ah' / evaluator 정수 → 정수 (rank*4 + suit)."""
    if isinstance(c, int):
        if not 0 <= c < 52:
            raise ValueError(c)
        return c
    s = str(c).strip()
    if len(s) != 2 or s[0].upper() not in RANKS or s[1].lower() not in SUITS:
        raise ValueError(f"bad card {c!r}")
    return RANKS.index(s[0].upper()) * 4 + SUITS.index(s[1].lower())


def cstr(i):
    return RANKS[i >> 2] + SUITS[i & 3]


def cards(v):
    if isinstance(v, str):
        return parse_board(v)
    return [card(c) for c in v]


def board_str(b):
    return "".join(cstr(c) for c in b)


def parse_board(s):
    """'AsTd2c' / 'As,Td,2c' / ['As', ...] → 정수 리스트."""
    if isinstance(s, (list, tuple)):
        return cards(s)
    s = s.replace(",", "").replace(" ", "")
    return [card(s[i:i + 2]) for i in range(0, len(s), 2)]


def combo_key(a, b):
    """두 카드 → 정렬된 (높은 정수, 낮은 정수)."""
    return (a, b) if a > b else (b, a)


def combo_str(a, b):
    a, b = combo_key(a, b)
    return cstr(a) + cstr(b)


def hand_code(a, b):
    a, b = combo_key(a, b)
    ra, rb = RANKS[a >> 2], RANKS[b >> 2]
    if ra == rb:
        return ra + rb
    if (b >> 2) > (a >> 2):
        ra, rb = rb, ra
    return ra + rb + ("s" if (a & 3) == (b & 3) else "o")


def hand_combos(code):
    """'AKs' → [(c1, c2), ...] (정수)."""
    r1, r2 = RANKS.index(code[0]), RANKS.index(code[1])
    out = []
    if r1 == r2:
        for s1, s2 in itertools.combinations(range(4), 2):
            out.append(combo_key(r1 * 4 + s1, r2 * 4 + s2))
    elif code[2] == "s":
        for s in range(4):
            out.append(combo_key(r1 * 4 + s, r2 * 4 + s))
    else:
        for s1 in range(4):
            for s2 in range(4):
                if s1 != s2:
                    out.append(combo_key(r1 * 4 + s1, r2 * 4 + s2))
    return out


_ORDER_CACHE = {}


def combo_order(board):
    """보드와 겹치지 않는 1176(또는 1128) 콤보의 고정 순서 → {combo: index}, list."""
    key = tuple(sorted(board))
    hit = _ORDER_CACHE.get(key)
    if hit is None:
        dead = set(board)
        lst = [combo_key(a, b) for a, b in itertools.combinations(range(52), 2) if a not in dead and b not in dead]
        lst.sort()
        hit = ({c: i for i, c in enumerate(lst)}, lst)
        if len(_ORDER_CACHE) > 256:
            _ORDER_CACHE.clear()
        _ORDER_CACHE[key] = hit
    return hit


# --- 레인지 ---------------------------------------------------------------------------
def _pf():
    import preflop
    return preflop


def _hand_order():
    try:
        return {h: i for i, h in enumerate(_pf().DATA["G"])}
    except Exception:
        return {}


def as_weights(x):
    """set/list 핸드코드 → {핸드: 1.0}, dict → {핸드: 가중치}. 가중치 0 은 뺀다."""
    if isinstance(x, dict):
        return {h: float(w) for h, w in x.items() if float(w) > 0}
    return {h: 1.0 for h in x}


def _fmt_w(w):
    if w >= 0.9995:
        return ""
    return ":" + (f"{w:.3f}".rstrip("0").rstrip("."))


def to_ts_range(weights):
    """{핸드코드: 가중치} → TexasSolver 레인지 문자열 'AA,KK,AKs:0.5' (강도 순)."""
    w = as_weights(weights)
    order = _hand_order()
    hands = sorted(w, key=lambda h: (order.get(h, 999), h))
    return ",".join(h + _fmt_w(w[h]) for h in hands if round(w[h], 3) > 0)


def parse_ts_range(text):
    """TexasSolver 레인지 문자열 → {핸드코드: 가중치}. 'QQ+', 'A2s-A5s' 같은 축약도 preflop 규칙으로 펼친다."""
    out = {}
    for tok in (text or "").replace("\n", ",").split(","):
        tok = tok.strip()
        if not tok:
            continue
        w = 1.0
        if ":" in tok:
            tok, ws = tok.split(":", 1)
            w = float(ws)
        tok = tok.strip()
        if re.fullmatch(r"[AKQJT2-9]{2}[so]?", tok):
            hands = [tok] if (len(tok) == 3 or tok[0] == tok[1]) else [tok + "s", tok + "o"]
        else:
            hands = sorted(_pf()._expand(tok))
        for h in hands:
            if w > 0:
                out[h] = w
    return out


def spot_info(spot, stack):
    s = SPOTS[spot]
    ip, oop = (s["caller"], s["opener"]) if s["opener"] == "SB" else (s["opener"], s["caller"])
    pot = 2 * s["open"] + s["dead"] + ANTE_TOTAL
    behind = stack - s["open"]
    return {"spot": spot, "stack": stack, "opener": s["opener"], "caller": s["caller"], "ip": ip, "oop": oop,
            "open": s["open"], "pot": round(pot, 4), "behind": round(behind, 4), "spr": behind / pot}


def spot_ranges(spot, stack, players=PLAYERS):
    """현재 preflop.py 레인지로 {'ip': {핸드: w}, 'oop': {...}} (호출할 때마다 새로 읽는다)."""
    pf = _pf()
    s = SPOTS[spot]
    opener = pf.ranges("open", s["opener"], None, players, stack, ante=True)
    opener = as_weights(opener[0][1])
    vs = pf.ranges("vsopen", s["caller"], s["opener"], players, stack, ante=True)
    call = next((h for (_n, h, c) in vs if c == "call"), None)
    if call is None:
        call = vs[-1][1]
    call = as_weights(call)
    info = spot_info(spot, stack)
    if info["ip"] == s["opener"]:
        return {"ip": opener, "oop": call}
    return {"ip": call, "oop": opener}


def range_hash(rngs):
    txt = "ip=" + to_ts_range(rngs["ip"]) + "|oop=" + to_ts_range(rngs["oop"])
    return hashlib.sha1(txt.encode()).hexdigest()[:12]


def tree_hash():
    txt = json.dumps({"tree": TREE, "ante": ANTE_TOTAL, "spots": SPOTS, "scale": SCALE}, sort_keys=True)
    return hashlib.sha1(txt.encode()).hexdigest()[:8]


def solver_input(spot, stack, board, dump_path, threads, rngs=None, accuracy=ACCURACY, max_iter=MAX_ITER,
                 dump_rounds=1, tree=None, print_interval=10):
    """TexasSolver console 입력 파일 내용."""
    tree = tree or TREE
    info = spot_info(spot, stack)
    rngs = rngs or spot_ranges(spot, stack)
    b = parse_board(board)
    lines = [
        f"set_pot {info['pot'] * SCALE:.2f}",
        f"set_effective_stack {info['behind'] * SCALE:.2f}",
        "set_board " + ",".join(cstr(c) for c in b),
        "set_range_ip " + to_ts_range(rngs["ip"]),
        "set_range_oop " + to_ts_range(rngs["oop"]),
    ]
    for street in ("flop", "turn", "river"):
        st = tree[street]
        for who in ("oop", "ip"):
            if st.get("bet"):
                lines.append(f"set_bet_sizes {who},{street},bet," + ",".join(str(x) for x in st["bet"]))
            if st.get("raise"):
                lines.append(f"set_bet_sizes {who},{street},raise," + ",".join(str(x) for x in st["raise"]))
            if who == "oop" and st.get("donk"):
                lines.append(f"set_bet_sizes oop,{street},donk," + ",".join(str(x) for x in st["donk"]))
            if st.get("allin"):
                lines.append(f"set_bet_sizes {who},{street},allin")
    lines += [
        f"set_allin_threshold {tree['allin_threshold']}",
        "build_tree",
        f"set_thread_num {threads}",
        f"set_accuracy {accuracy}",
        f"set_max_iteration {max_iter}",
        f"set_print_interval {print_interval}",
        "set_use_isomorphism 1",
        "start_solve",
        f"set_dump_rounds {dump_rounds}",
        "dump_result " + dump_path.replace("\\", "/"),
    ]
    return "\n".join(lines) + "\n"


# --- 플랍 정규화 / 특징 / 거리 -------------------------------------------------------
_PERMS = list(itertools.permutations(range(4)))


def canon_flop(board):
    """플랍 → (정규 키 'Ks7h2d', 순열 perm: 실제 무늬 → 정규 무늬). 수트 동형 플랍은 같은 키."""
    b = cards(board)
    best = None
    for p in _PERMS:
        m = sorted(((c >> 2) * 4 + p[c & 3] for c in b), key=lambda c: (-(c >> 2), c & 3))
        t = tuple(m)
        if best is None or t < best[0]:
            best = (t, p)
    return board_str(best[0]), best[1]


def _sorted_board(b):
    return sorted(b, key=lambda c: (-(c >> 2), c & 3))


def _map_order(b, hero):
    """무늬 순열(_suit_map)용 보드 순서. _sorted_board 와 같되 같은 랭크(페어 보드)끼리는 히어로(높은 카드 먼저)와
    무늬를 공유하는 카드를 앞에 둔다 — 무늬 번호 순으로 정하면 근사 플랍에서 결과가 실제 무늬 이름에 따라 달라진다."""
    hs = [c & 3 for c in sorted(hero, key=lambda c: -(c >> 2))]
    return sorted(b, key=lambda c: (-(c >> 2), hs.index(c & 3) if (c & 3) in hs else len(hs), c & 3))


def flop_features(board):
    b = _sorted_board(parse_board(board) if isinstance(board, str) else cards(board))
    r = [c >> 2 for c in b]
    s = [c & 3 for c in b]
    distinct = len(set(r))
    ptype = {3: 0, 2: 1, 1: 2}[distinct]
    ns = len(set(s))
    scat = "m" if ns == 1 else ("t" if ns == 2 else "r")
    pat = ""
    if scat == "t":
        if ptype == 1:
            pat = "pk"   # 페어 보드 투톤은 항상 페어 한 장 + 키커
        else:
            if s[0] == s[1]:
                pat = "hm"
            elif s[0] == s[2]:
                pat = "hl"
            else:
                pat = "ml"
    rs = set(r)
    if 12 in rs:
        rs_low = (rs - {12}) | {-1}
    else:
        rs_low = None
    conn = 0
    if ptype == 0:
        for lo in range(-1, 9):
            win = set(range(lo, lo + 5))
            if rs <= win or (rs_low is not None and rs_low <= win):
                conn += 1
    bw = sum(1 for x in r if x >= 8)
    return (r[0], r[1], r[2], ptype, scat, pat, conn, bw)


def feature_distance(fa, fb):
    if fa[3] != fb[3]:
        return 999.0
    d = 0.0
    if fa[4] != fb[4]:
        d += 30.0 if "m" in (fa[4], fb[4]) else 20.0
    elif fa[4] == "t" and fa[5] != fb[5]:
        d += 2.5
    d += 2.0 * abs(fa[0] - fb[0]) + 1.4 * abs(fa[1] - fb[1]) + 1.0 * abs(fa[2] - fb[2])
    d += 1.2 * abs(fa[6] - fb[6]) + 0.8 * abs(fa[7] - fb[7])
    return d


def flop_distance(a, b):
    return feature_distance(flop_features(a), flop_features(b))


def nearest_flop(board, candidates, penalty=None):
    """후보(정규 키 목록) 중 가장 가까운 플랍 → (키, 거리). 정확히 같은 클래스면 거리 0.
    penalty: {키: 추가 거리} (예: 예전 레인지로 푼 stale 플랍은 +STALE_PENALTY → 새 레인지 솔루션 우선)."""
    key = canon_flop(board)[0]
    penalty = penalty or {}
    fa = flop_features(board)
    best = None
    for k in candidates:
        d = 0.0 if k == key else feature_distance(fa, flop_features(k))
        score = d + penalty.get(k, 0.0)
        if best is None or score < best[2]:
            best = (k, d, score)
    return (best[0], best[1]) if best else (None, None)


def _suit_map(src, dst):
    """src 보드 → dst 보드 무늬 순열 (dict 실제 무늬 → 대상 무늬). 같은 크기의 무늬 그룹끼리, 같은 크기면 랭크 위치 순."""
    def groups(b):
        g = {}
        for pos, c in enumerate(b):
            g.setdefault(c & 3, []).append(pos)
        return sorted(g.items(), key=lambda kv: (-len(kv[1]), kv[1][0]))
    ga, gb = groups(src), groups(dst)
    m = {}
    for (sa, _pa), (sb, _pb) in zip(ga, gb):
        m[sa] = sb
    rest_a = [s for s in range(4) if s not in m]
    rest_b = [s for s in range(4) if s not in m.values()]
    for sa, sb in zip(rest_a, rest_b):
        m[sa] = sb
    return m


def _rank_map(src_ranks, dst_ranks, r):
    """보드 랭크를 앵커로 단조 보간. (대상 랭크, 보드와 강제로 겹쳤는지)."""
    pairs = sorted(set(zip(src_ranks, dst_ranks)))
    for a, t in pairs:
        if a == r:
            return t, False
    lo = (-1, -1)
    hi = (13, 13)
    for a, t in pairs:
        if a < r and a >= lo[0]:
            lo = (a, t)
        if a > r and a <= hi[0]:
            hi = (a, t)
    (al, tl), (au, tu) = lo, hi
    x = tl + (r - al) * (tu - tl) / (au - al)
    t = int(math.floor(x + 0.5))
    if tu - tl >= 2:
        t = min(max(t, tl + 1), tu - 1)
        return min(max(t, 0), 12), False
    t = tl if abs(x - tl) <= abs(x - tu) else tu
    t = min(max(t, 0), 12)
    return t, True


def map_hand(hero, board, target):
    """실제 (hero, board) → 대상 플랍 target 위의 hero 카드. (카드 2장, 메모 목록). 보드는 플랍 3장."""
    h = cards(hero)
    b = _map_order(cards(board), h)
    t = _sorted_board(parse_board(target))
    smap = _suit_map(b, t)
    src_r, dst_r = [c >> 2 for c in b], [c >> 2 for c in t]
    notes = []
    out_r = []
    for c in h:
        r, forced = _rank_map(src_r, dst_r, c >> 2)
        if forced:
            notes.append("rank_collision")
        out_r.append(r)
    if h[0] >> 2 != h[1] >> 2 and out_r[0] == out_r[1]:   # 서로 다른 랭크가 같은 랭크로 → 한 칸 떼어 놓기
        hi_i = 0 if h[0] >> 2 > h[1] >> 2 else 1
        for idx, cand in ((hi_i, out_r[hi_i] + 1), (1 - hi_i, out_r[1 - hi_i] - 1)):
            if 0 <= cand <= 12 and cand not in dst_r:
                out_r[idx] = cand
                break
    mapped = [r * 4 + smap[c & 3] for r, c in zip(out_r, h)]
    return mapped, notes


# --- 덤프 → 압축 ----------------------------------------------------------------------
def _amount(action):
    m = re.search(r"([0-9.]+)", action)
    return float(m.group(1)) if m else 0.0


def _bet_label(amount, pot, stack_left, sizes):
    if amount >= stack_left - 0.51:
        return "allin"
    pct = amount / pot * 100
    best = min(sizes, key=lambda s: abs(s - pct))
    return f"bet{best}"


def _child(node, name):
    return (node or {}).get("childrens", {}).get(name)


def _board_perms(board):
    """보드를 그대로 두는 무늬 순열 (동형 콤보 채우기용)."""
    bs = set(board)
    out = []
    for p in _PERMS:
        if p == (0, 1, 2, 3):
            continue
        if {(c >> 2) * 4 + p[c & 3] for c in board} == bs:
            out.append(p)
    return out


def _pack(node, board):
    """노드 전략 → (actions, packed str, agg). 콤보 순서는 combo_order(board)."""
    strat = node.get("strategy") or {}
    acts = strat.get("actions") or node.get("actions") or []
    table = {}
    for k, v in (strat.get("strategy") or {}).items():
        try:
            table[combo_key(card(k[:2]), card(k[2:4]))] = v
        except ValueError:
            continue
    perms = _board_perms(board)
    _idx, lst = combo_order(board)
    n = len(acts)
    parts = []
    agg = [0.0] * n
    tot = 0.0
    for c in lst:
        v = table.get(c)
        if v is None and perms:
            for p in perms:
                v = table.get(combo_key((c[0] >> 2) * 4 + p[c[0] & 3], (c[1] >> 2) * 4 + p[c[1] & 3]))
                if v is not None:
                    break
        if v is None:
            parts.append(MISSING * (n - 1 if n > 1 else 1))
            continue
        s = sum(v) or 1.0
        f = [x / s for x in v]
        for i in range(n):
            agg[i] += f[i]
        tot += 1
        parts.append("".join(ALPH[int(round(x * QMAX))] for x in f[:-1]) if n > 1 else ALPH[QMAX])
    return acts, "".join(parts), [round(a / tot, 4) if tot else 0.0 for a in agg]


def _street_nodes(root, board, P, B, sizes):
    """한 스트리트 시작 노드(OOP 차례)에서 핵심 노드들을 압축.
    P = 스트리트 시작 팟, B = 남은 스택 (솔버 칩).  덤프 규칙: player 1 = OOP, 0 = IP,
    'BET x' = 벳 금액, 'RAISE x' = 이 액션으로 더 넣는 칩 (스트리트 총액 = 이미 넣은 칩 + x)."""
    nodes = {}

    def add(key, node, player, committed, call, extra):
        if not node or node.get("node_type") != "action_node" or "strategy" not in node:
            return
        if len(node.get("actions") or []) < 2:   # 선택지 하나 (예: 동크 없는 턴 OOP 체크) → 저장 안 함
            return
        acts, packed, agg = _pack(node, board)
        labels, amts = [], []
        for a in acts:
            A = a.split()[0].upper()
            amt = _amount(a)
            if A == "CHECK":
                labels.append("check"); amts.append(0.0)
            elif A == "FOLD":
                labels.append("fold"); amts.append(0.0)
            elif A == "CALL":
                labels.append("call"); amts.append(round(call / SCALE, 2))
            elif A == "BET":
                labels.append(_bet_label(amt, P, B, sizes)); amts.append(round(amt / SCALE, 2))
            elif A == "RAISE":
                tot = committed + amt
                labels.append("allin" if tot >= B - 0.51 else "raise"); amts.append(round(tot / SCALE, 2))
            elif A == "ALLIN":
                labels.append("allin"); amts.append(round(B / SCALE, 2))
            else:
                labels.append(a.lower()); amts.append(round(amt / SCALE, 2))
        d = {"p": player, "a": labels, "amt": amts, "s": packed, "agg": agg}
        d.update(extra)
        nodes[key] = d

    add("oop_first", root, "oop", 0.0, 0.0, {})
    chk = _child(root, "CHECK")
    add("ip_vs_check", chk, "ip", 0.0, 0.0, {})
    for first, n0, me, other in ((chk, chk, "oop", "ip"), (root, root, "ip", "oop")):
        if not n0:
            continue
        for name in n0.get("actions", []):
            if not name.upper().startswith("BET"):
                continue
            x = _amount(name)
            lab = _bet_label(x, P, B, sizes)
            n1 = _child(n0, name)
            add(f"{me}_vs_bet:{lab}", n1, me, 0.0, x, {"bet": round(x / P, 4), "cr": round(x / (P + x), 4)})
            for r in (n1 or {}).get("actions", []) if n1 else []:
                if r.upper().startswith("RAISE"):
                    y = _amount(r)   # 0 에서 시작했으니 y = 레이즈 총액
                    add(f"{other}_vs_raise:{lab}", _child(n1, r), other, x, y - x,
                        {"bet": round(x / P, 4), "cr": round((y - x) / (P + x + y), 4)})
    return nodes


def compact_dump(dump, spot, stack, board):
    """TexasSolver 덤프(dict) → 플랍 핵심 노드만의 압축 dict."""
    info = spot_info(spot, stack)
    b = parse_board(board)
    nodes = _street_nodes(dump, b, info["pot"] * SCALE, info["behind"] * SCALE, TREE["flop"]["bet"])
    return {"board": board_str(b), "nodes": nodes}


def flop_lines(dump, spot, stack):
    """턴으로 넘어가는 플랍 라인 → (이름, 찬스 노드, 턴 시작 팟, 남은 스택) (솔버 칩).
    이름: 'x_x' (체크-체크), 'x_bet33_c' (OOP 체크, IP 벳, OOP 콜), 'bet33_c' (OOP 벳, IP 콜)."""
    info = spot_info(spot, stack)
    P, B = info["pot"] * SCALE, info["behind"] * SCALE
    out = []
    chk = _child(dump, "CHECK")
    if chk and _child(chk, "CHECK"):
        out.append(("x_x", _child(chk, "CHECK"), P, B))
    for n0, prefix in ((chk, "x_"), (dump, "")):
        if not n0:
            continue
        for name in n0.get("actions", []):
            if not name.upper().startswith("BET"):
                continue
            x = _amount(name)
            lab = _bet_label(x, P, B, TREE["flop"]["bet"])
            if lab == "allin":
                continue
            call = _child(_child(n0, name), "CALL")
            if call and call.get("dealcards"):
                out.append((f"{prefix}{lab}_c", call, P + 2 * x, B - x))
    return out


def compact_turn(dump, spot, stack, board):
    """덤프(dump_rounds 2) → 턴 카드별 핵심 노드 {'board', 'lines': {line: {'pot','behind','cards': {카드: nodes}}}}.
    pot/behind 는 BB. 턴 노드 이름은 플랍과 같다 (oop_first 는 OOP 가 벳할 수 있을 때만 = x_x 라인)."""
    b = parse_board(board)
    lines = {}
    for name, chance, P2, B2 in flop_lines(dump, spot, stack):
        cards_ = {}
        for cs, troot in (chance.get("dealcards") or {}).items():
            try:
                tc = card(cs)
            except ValueError:
                continue
            if tc in b or not troot or troot.get("node_type") != "action_node":
                continue
            nodes = _street_nodes(troot, b + [tc], P2, B2, TREE["turn"]["bet"])
            nodes = {k: v for k, v in nodes.items() if "_vs_raise" not in k}   # 턴 레이즈 대응은 크기 때문에 뺀다
            if nodes:
                cards_[cstr(tc)] = nodes
        if cards_:
            lines[name] = {"pot": round(P2 / SCALE, 3), "behind": round(B2 / SCALE, 3), "cards": cards_}
    return {"board": board_str(b), "lines": lines}


# --- 라이브러리 파일 ---------------------------------------------------------------------
def lib_name(spot, stack):
    return f"{spot}_{stack}bb.json.gz"


def lib_dirs():
    out = []
    env = os.environ.get("GTO_LIB_DIR")
    if env:
        out.append(env)
    if getattr(sys, "frozen", False):
        out.append(os.path.join(os.path.dirname(sys.executable), "gto_lib"))
        mp = getattr(sys, "_MEIPASS", None)
        if mp:
            out.append(os.path.join(mp, "gto_lib"))
    out.append(LIB_DIR)
    seen, res = set(), []
    for d in out:
        d = os.path.normpath(d)
        if d not in seen:
            seen.add(d)
            res.append(d)
    return res


_AVAIL = {"t": 0.0, "v": {}}
_LIBS = {}


def available(refresh=False):
    """{spot: {stack: path}} — 30초 캐시."""
    now = time.time()
    if not refresh and now - _AVAIL["t"] < 30:
        return _AVAIL["v"]
    res = {}
    for d in reversed(lib_dirs()):   # 앞(우선) 디렉터리가 덮어쓴다
        for p in glob.glob(os.path.join(d, "*_*bb.json.gz")):
            m = re.fullmatch(r"(.+)_(\d+)bb\.json\.gz", os.path.basename(p))
            if m and m.group(1) in SPOTS:
                res.setdefault(m.group(1), {})[int(m.group(2))] = p
    _AVAIL.update(t=now, v=res)
    return res


def load_lib(path):
    """mtime 이 바뀌면 다시 읽는다 (배치가 밤새 파일을 갱신)."""
    try:
        mt = os.path.getmtime(path)
    except OSError:
        return None
    hit = _LIBS.get(path)
    if hit and hit[0] == mt:
        return hit[1]
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError, EOFError):
        return hit[1] if hit else None
    _LIBS[path] = (mt, data)
    return data


def decode(node, combo, board):
    """노드에서 콤보 하나의 빈도 리스트 (없으면 None)."""
    idx, _lst = combo_order(board)
    i = idx.get(combo_key(*combo))
    if i is None:
        return None
    n = len(node["a"])
    w = n - 1 if n > 1 else 1
    chunk = node["s"][i * w:(i + 1) * w]
    if not chunk or MISSING in chunk:
        return None
    if n == 1:
        return [1.0]
    vals = [AIDX[ch] / QMAX for ch in chunk]
    last = max(0.0, 1.0 - sum(vals))
    vals.append(last)
    s = sum(vals) or 1.0
    return [v / s for v in vals]


def decode_sym(node, combo, board):
    """decode 의 수트 동형 평균: 보드를 그대로 두는 무늬 순열로 옮긴 콤보(전략상 같은 핸드)들의 빈도 평균.
    TexasSolver 덤프는 동형 콤보끼리도 전략이 크게 다를 수 있어(무차별한 핸드가 서로 다른 순수 전략으로 수렴,
    예: 9s5s3s 에서 Jh3h 콜 100% / Jd3d 폴드 100%) 그대로 쓰면 결과가 실제 무늬 이름에 따라 달라진다."""
    base = decode(node, combo, board)
    if base is None:
        return None
    key = combo_key(*combo)
    seen = {key}
    acc, k = list(base), 1
    for p in _board_perms(board):
        c2 = combo_key((key[0] >> 2) * 4 + p[key[0] & 3], (key[1] >> 2) * 4 + p[key[1] & 3])
        if c2 in seen:
            continue
        seen.add(c2)
        f = decode(node, c2, board)
        if f is None:
            continue
        acc = [a + b for a, b in zip(acc, f)]
        k += 1
    return [a / k for a in acc]


# --- 스팟 / 노드 해석 ------------------------------------------------------------------
CANON_OPENER = {2: "BTN", 3: "CO", 4: "HJ", 5: "HJ"}   # 뒤 인원 → 대표 오프너 (6 이상 UTG)


def _seats(players):
    try:
        return _pf().seats(players)
    except Exception:
        return ["UTG", "U1", "U2", "LJ", "HJ", "CO", "BTN", "SB", "BB"]


def _behind(pos, players):
    for n in [players] + [x for x in (9, 8, 7, 6, 10) if x != players]:
        s = _seats(n)
        if pos in s:
            return len(s) - 1 - s.index(pos), n
    return None, None


def resolve_spot(hero_pos, villain_pos, players=PLAYERS):
    """(spot, hero 역할 'ip'/'oop', exact, 메모) 또는 None."""
    if not hero_pos or not villain_pos or hero_pos == villain_pos:
        return None
    hp, vp = str(hero_pos).upper(), str(villain_pos).upper()
    blinds = {"SB", "BB"}
    notes = []
    if hp in blinds and vp in blinds:
        spot, opener = "SB_BB", "SB"
    elif hp in blinds or vp in blinds:
        blind, opener = (hp, vp) if hp in blinds else (vp, hp)
        b, n = _behind(opener, players)
        if b is None:
            return None
        canon = CANON_OPENER.get(b, "UTG")
        if canon != opener:
            notes.append(f"{opener}→{canon} 오프너")
        if blind == "BB":
            spot = f"{canon}_BB"
        else:
            spot = "BTN_SB"
            if canon != "BTN":
                notes.append(f"{canon}→BTN (SB 콜)")
        if players != PLAYERS and n != PLAYERS:
            notes.append(f"{players}명 → 9명 레인지")
    else:
        return None
    info = spot_info(spot, 40)
    s = SPOTS[spot]
    hero_is_opener = hp == opener
    hero_role_pos = s["opener"] if hero_is_opener else s["caller"]
    role = "ip" if info["ip"] == hero_role_pos else "oop"
    return spot, role, not notes, notes


def parse_node(node):
    """→ (기본 이름, 크기 or None).  크기는 팟 대비 % (33, 75) 또는 'allin' 또는 'bet33' 같은 라벨."""
    size = None
    if isinstance(node, (tuple, list)):
        base = node[0]
        size = node[1] if len(node) > 1 else None
    else:
        m = re.fullmatch(r"\s*([a-z_]+)\s*(?:[(:]\s*([^)]*?)\s*\)?)?\s*", str(node))
        if not m:
            raise ValueError(f"bad node {node!r}")
        base, size = m.group(1), m.group(2) or None
    if base not in NODES:
        raise ValueError(f"bad node {node!r}")
    if isinstance(size, str):
        s = size.strip().lower().rstrip("%")
        if s in ("allin", "all-in", "jam"):
            size = "allin"
        elif s.startswith("bet") or s.startswith("raise"):
            size = s
        else:
            try:
                size = float(s)
            except ValueError:
                size = None
    if isinstance(size, (int, float)) and not isinstance(size, bool):
        size = float(size) * 100 if size <= 3 else float(size)   # 0.33 → 33
    return base, size


def _pick_node(nodes, base, size, call_ratio=None):
    if base in ("oop_first", "ip_vs_check"):
        return (base, nodes.get(base)) if base in nodes else (None, None)
    cands = {k: v for k, v in nodes.items() if k.split(":")[0] == base}
    if not cands:
        return None, None
    if isinstance(size, str) and f"{base}:{size}" in cands:
        k = f"{base}:{size}"
        return k, cands[k]
    if call_ratio is not None:
        k = min(cands, key=lambda k: abs(math.log(max(cands[k].get("cr", 0.01), 1e-3) / max(call_ratio, 1e-3))))
        return k, cands[k]
    if isinstance(size, (int, float)):
        f = size / 100.0
        k = min(cands, key=lambda k: abs(math.log(max(cands[k].get("bet", 0.01), 1e-3) / max(f, 1e-3))))
        return k, cands[k]
    k = sorted(cands)[0]
    return k, cands[k]


def _pick_stack(stacks, stack_bb, spr, spot):
    if spr:
        return min(stacks, key=lambda s: abs(math.log(spot_info(spot, s)["spr"] / max(spr, 0.05))))
    if stack_bb:
        return min(stacks, key=lambda s: abs(math.log(s / max(stack_bb, 1.0))))
    return 40 if 40 in stacks else sorted(stacks)[0]


def _alt_combos(mapped, tboard, nodeflop_board):
    """매핑된 콤보가 범위 밖/보드와 겹칠 때: 랭크 ±1/±2, 플러시 무관 무늬 바꾸기 순으로 대안."""
    dead = set(tboard)
    suit_cnt = {}
    for c in tboard:
        suit_cnt[c & 3] = suit_cnt.get(c & 3, 0) + 1
    r = [c >> 2 for c in mapped]
    s = [c & 3 for c in mapped]
    pair = r[0] == r[1]
    out = []
    deltas = [(0, 0)]
    for d in (1, -1, 2, -2):
        if pair:
            deltas.append((d, d))
        else:
            deltas += [(d, 0), (0, d)]
    if not pair:
        deltas += [(d1, d2) for d1 in (1, -1) for d2 in (1, -1)]

    def suit_opts(si, ri, d):
        flushy = suit_cnt.get(si, 0) >= 2
        alts = [x for x in range(4) if x != si and ((suit_cnt.get(x, 0) >= 2) == flushy)]
        opts = [si] + alts
        if d == 0 and ri * 4 + si in dead:   # 보드 카드와 겹친 카드: 랭크(보드와 페어)를 지키는 게 무늬 성질보다 우선
            opts += [x for x in range(4) if x not in opts]
        return opts

    for d1, d2 in deltas:
        r1, r2 = r[0] + d1, r[1] + d2
        if not (0 <= r1 <= 12 and 0 <= r2 <= 12):
            continue
        for s1 in suit_opts(s[0], r1, d1):
            for s2 in suit_opts(s[1], r2, d2):
                c1, c2 = r1 * 4 + s1, r2 * 4 + s2
                if c1 == c2 or c1 in dead or c2 in dead:
                    continue
                if (s[0] == s[1]) != (s1 == s2):
                    continue
                out.append((c1, c2))
    seen, res = set(), []
    for c in out:
        k = combo_key(*c)
        if k not in seen:
            seen.add(k)
            res.append(k)
    return res


def _nearest_by_strength(nd, combo, board):
    """노드에 있는 콤보 중 보드 위 강도(evaluator 순위)가 combo 와 가장 가까운 것."""
    try:
        import evaluator as ev
    except ImportError:
        return None
    if set(combo) & set(board) or combo[0] == combo[1]:
        return None
    _idx, lst = combo_order(board)
    me = ev.evaluate(list(combo) + list(board))
    scored = sorted((ev.evaluate(list(c) + list(board)), c) for c in lst)
    pos = {c: i for i, (_s, c) in enumerate(scored)}
    mine = sum(1 for s, _c in scored if s < me)
    score = {c: s for s, c in scored}
    best = None
    for c in lst:
        if decode(nd, c, board) is None:
            continue
        # 같은 족보 카테고리 우선, 그 다음 순위 거리 (같으면 약한 쪽)
        d = (score[c][0] != me[0], abs(pos[c] - mine), pos[c] > mine)
        if best is None or d < best[0]:
            best = (d, c)
    return best[1] if best else None


def turn_path(spot, stack, flop):
    for d in lib_dirs():
        p = os.path.join(d, "turn", f"{spot}_{stack}bb", f"{flop}.json.gz")
        if os.path.exists(p):
            return p
    return None


def _turn_stacks(spot, stacks):
    return [s for s in stacks if any(glob.glob(os.path.join(d, "turn", f"{spot}_{s}bb", "*.json.gz")) for d in lib_dirs())]


def _map_turn(turn, b, tboard, hero_mapped, hero=()):
    """실제 턴 카드 → 대상 플랍 위의 턴 카드 (랭크 보간 + 같은 무늬 순열). 겹치면 가까운 대안. (카드, 바뀜 여부)
    hero: 실제 히어로 카드 — map_hand 와 같은 무늬 순열을 쓰도록 같은 보드 순서를 만든다."""
    bs = _map_order(b, hero)
    ts = _sorted_board(tboard)
    smap = _suit_map(bs, ts)
    r, _f = _rank_map([c >> 2 for c in bs], [c >> 2 for c in ts], turn >> 2)
    s = smap[turn & 3]
    dead = set(tboard) | set(hero_mapped)

    def flushy(x):
        return sum(1 for c in tboard if c & 3 == x) >= 2
    order = [s] + [x for x in range(4) if x != s and flushy(x) == flushy(s)] + \
        [x for x in range(4) if flushy(x) != flushy(s)]
    for dr in (0, 1, -1, 2, -2):
        rr = r + dr
        if not 0 <= rr <= 12:
            continue
        for ss in order:
            c = rr * 4 + ss
            if c not in dead:
                return c, (dr != 0 or ss != s)
    return None, True


def _default_line(lines, spot, pot_growth):
    """턴 라인 추정: 팟 증가 배수(턴 시작 팟 / SRP 플랍 팟)가 가장 가까운 라인. 비슷하면 프리플랍 레이저가 벳한 라인."""
    if not lines:
        return None
    if not pot_growth:
        return "x_x" if "x_x" in lines else sorted(lines)[0]
    info = spot_info(spot, 40)
    opener_ip = info["ip"] == SPOTS[spot]["opener"]
    pot0 = info["pot"]

    def score(k):
        g = lines[k]["pot"] / pot0
        pref = 0.0 if (k == "x_x" or k.startswith("x_") == opener_ip) else 0.05
        return abs(math.log(g / max(pot_growth, 0.2))) + pref
    return min(lines, key=score)


def lookup(hero_pos, villain_pos, stack_bb, hero_cards, board, node, players=PLAYERS, spr=None, call_ratio=None,
           line=None, pot_growth=None):
    """풀린 스팟에서 hero 콤보의 전략.  board: 플랍 3장, 또는 턴 4장 (턴 파일이 있을 때).
    stack_bb: 시작 유효 스택(BB) — 가장 가까운 풀린 스택을 쓴다 (spr: 플랍 시작 SPR 을 주면 그 기준).
    턴: line = 'x_x' / 'x_bet33_c' / 'x_bet75_c' / 'bet33_c' / 'bet75_c' (플랍 액션), 없으면 pot_growth
        (턴 시작 팟 / 플랍 시작 팟) 로 추정, 그것도 없으면 체크-체크.
    반환: {actions: {이름: 빈도}, ev: None, mapped_flop, actual_flop, exact_flop, exact_spot, spot, stack_used,
           node, role, range_actions, combo, combo_exact, notes, distance, exploitability, amounts, street, ...}"""
    try:
        h = cards(hero_cards)
        b = cards(parse_board(board) if isinstance(board, str) else board)
    except (ValueError, TypeError):
        return None
    if len(h) != 2 or len(b) not in (3, 4) or len(set(h + b)) != 2 + len(b):
        return None
    turn = b[3] if len(b) == 4 else None
    b = b[:3]
    base, size = parse_node(node)
    rs = resolve_spot(hero_pos, villain_pos, players)
    if not rs:
        return None
    spot, role, exact_spot, notes = rs
    if NODE_PLAYER[base] != role:
        return None
    avail = available()
    chain = [spot] + SPOT_FALLBACK.get(spot, [])
    if turn is not None:
        chain = [s for s in chain if avail.get(s) and _turn_stacks(s, avail[s])]
    use = next((s for s in chain if avail.get(s)), None)
    if use is None:
        return None
    if use != spot:
        notes = notes + [f"{spot}→{use} 스팟"]
        exact_spot = False
    stacks = sorted(avail[use]) if turn is None else _turn_stacks(use, sorted(avail[use]))
    stack = _pick_stack(stacks, stack_bb, spr, use)
    lib = load_lib(avail[use][stack])
    if not lib or not lib.get("flops"):
        return None
    flop_keys = list(lib["flops"])
    if turn is not None:
        flop_keys = [k for k in flop_keys if turn_path(use, stack, k)]
    rh = lib.get("range_hash")
    pen = {k: STALE_PENALTY for k in flop_keys if lib["flops"][k].get("rh") != rh}
    tkey, dist = nearest_flop(b, flop_keys, pen)
    if tkey is None or dist >= 900:
        return None
    fl = lib["flops"][tkey]
    tboard = parse_board(tkey)
    mapped, mnotes = map_hand(h, b, tkey)
    street, line_used, td = "flop", None, None
    if turn is None:
        nodes = fl["nodes"]
        dboard = tboard
    else:
        street = "turn"
        td = load_lib(turn_path(use, stack, tkey))
        if not td or not td.get("lines"):
            return None
        line_used = line if line in td["lines"] else _default_line(td["lines"], use, pot_growth)
        if line is None and line_used != "x_x":
            notes = notes + [f"플랍 라인 추정 {line_used}"]
        L = td["lines"][line_used]
        tt, tchanged = _map_turn(turn, b, tboard, mapped, h)
        if tt is None:
            return None
        if tchanged:
            notes = notes + ["턴 카드 근사"]
        nodes = L["cards"].get(cstr(tt))
        if nodes is None:   # 동형으로 빠진 턴 카드: 플랍을 지키는 무늬 순열로 찾기
            for p in _board_perms(tboard):
                alt = (tt >> 2) * 4 + p[tt & 3]
                if cstr(alt) in L["cards"]:
                    nodes = L["cards"][cstr(alt)]
                    tt = alt
                    mapped = [(c >> 2) * 4 + p[c & 3] for c in mapped]
                    break
        if nodes is None:
            return None
        dboard = tboard + [tt]
        if base == "oop_first" and "oop_first" not in nodes:   # 동크 없는 라인: OOP 는 체크만
            nodes = dict(nodes, oop_first={"p": "oop", "a": ["check"], "amt": [0.0], "s": "", "agg": [1.0]})
    nkey, nd = _pick_node(nodes, base, size, call_ratio)
    if nd is None:
        return None
    combo_exact = True
    freqs = None
    used = combo_key(*mapped)
    if len(nd["a"]) == 1:
        freqs = [1.0]
    elif len(set(mapped)) == 2 and not (set(mapped) & set(dboard)):
        freqs = decode_sym(nd, mapped, dboard)
    if freqs is None:
        combo_exact = False
        # 첫 후보가 mapped 자신일 때만 건너뛴다 (mapped 가 보드와 겹치면 첫 후보는 이미 다른 콤보 → 버리면 안 됨)
        for alt in _alt_combos(mapped, dboard, tkey):
            if alt == used:
                continue
            freqs = decode_sym(nd, alt, dboard)
            if freqs is not None:
                used = alt
                break
    if freqs is None:   # 그래도 없으면 (예: 프리플랍에 3벳했어야 할 핸드) 보드 위 족보 강도 순위가 가장 가까운 범위 안 콤보
        alt = _nearest_by_strength(nd, mapped, dboard)
        if alt is not None:
            freqs, used = decode_sym(nd, alt, dboard), alt
    if freqs is None:
        return None
    if "rank_collision" in mnotes:
        notes = notes + ["랭크 근사 충돌"]
    if not combo_exact:
        notes = notes + [f"콤보 범위 밖 → {combo_str(*used)}"]
    acts, amounts, rng = {}, {}, {}
    for a, f, amt in zip(nd["a"], freqs, nd.get("amt", [0] * len(freqs))):
        acts[a] = acts.get(a, 0.0) + f
        amounts[a] = amt
    for a, f in zip(nd["a"], nd.get("agg", [])):
        rng[a] = rng.get(a, 0.0) + f
    if stack_bb and abs(math.log(stack / max(stack_bb, 1))) > 0.15 and not spr:
        notes.append(f"스택 {stack_bb:.0f}→{stack}BB")
    out = {
        "actions": acts, "ev": None, "mapped_flop": tkey, "actual_flop": board_str(_sorted_board(b)),
        "exact_flop": dist == 0, "exact_spot": exact_spot, "spot": use, "stack_used": stack, "node": nkey,
        "role": role, "range_actions": rng, "combo": combo_str(*used), "combo_exact": combo_exact,
        "notes": notes, "distance": round(dist, 2), "exploitability": fl.get("exploit"), "amounts": amounts,
        "pot": lib.get("pot"), "behind": lib.get("behind"), "stale": fl.get("rh") != lib.get("range_hash"),
        "street": street,
    }
    if street == "turn":
        L = td["lines"][line_used]
        out.update(line=line_used, mapped_turn=cstr(dboard[3]), actual_turn=cstr(turn), pot=L["pot"], behind=L["behind"])
    return out


# --- advisor 연동 ------------------------------------------------------------------------
LABEL_KO = {"check": "체크", "fold": "폴드", "call": "콜", "raise": "레이즈", "allin": "올인"}
ACTION_KO = {"check": "체크", "fold": "폴드", "call": "콜", "raise": "레이즈", "allin": "올인", "bet": "벳"}
COLOR = {"check": "check", "fold": "fold", "call": "call", "raise": "raise", "allin": "jam", "bet": "raise"}
POS_IP_ORDER = None


def label_ko(a):
    if a.startswith("bet"):
        return f"벳 {a[3:]}%"
    return LABEL_KO.get(a, a)


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None


def _bb(x):
    x = round(x, 1)
    return f"{x:.0f}BB" if x == int(x) else f"{x:.1f}BB"


def _freq_text(acts, min_f=0.01):
    items = sorted(((a, f) for a, f in acts.items() if f >= min_f), key=lambda kv: -kv[1])
    return " · ".join(f"{label_ko(a)} {f * 100:.0f}%" for a, f in items)


def _is_ip(hero_pos, villain_pos, players):
    try:
        pf = _pf()
        n = players if players and hero_pos in pf.seats(int(players)) else None
        if n is None:
            for k in (9, 8, 7, 6, 10):
                if hero_pos in pf.seats(k) and villain_pos in pf.seats(k):
                    n = k
                    break
        return pf.postflop_order(hero_pos, n) > pf.postflop_order(villain_pos, n)
    except Exception:
        return None


def gto_advice(ctx):
    """헤즈업 플랍(턴 파일이 있으면 턴도) 결정이고 맞는 풀린 스팟이 있으면 advisor 형식 dict, 아니면 None.
    턴: ctx['flop_line'] ('x_x', 'x_bet33_c', 'bet75_c' …) 이 있으면 그 라인, 없으면 팟 증가 배수로 추정."""
    g = (ctx or {}).get
    try:
        hero = cards(g("hero") or [])
        board = cards(g("board") or [])
    except (ValueError, TypeError):
        return None
    if len(hero) != 2 or len(board) not in (3, 4):
        return None
    street = g("street")
    if street and street != {3: "flop", 4: "turn"}[len(board)]:
        return None
    opp = _num(g("opponents_in_hand"))
    if opp is not None and int(opp) != 1:
        return None
    if g("situation") in ("vs3bet", "vs4bet", "jam", "jamcall"):
        return None
    if g("to_act") is False or g("facing_allin"):
        return None
    hp, vp = g("hero_pos"), g("villain_pos")
    if not hp or not vp:
        return None
    players = int(_num(g("players")) or PLAYERS)
    rs = resolve_spot(hp, vp, players)
    if not rs:
        return None
    _spot, role, _exact, _notes = rs
    ip = _is_ip(hp, vp, players)
    if ip is not None and ("ip" if ip else "oop") != role:
        return None
    call = _num(g("call"))
    can_check = g("can_check")
    pot = _num(g("pot"))
    pot_total = _num(g("pot_total"))
    if call is None and can_check is None:
        return None
    facing = (call or 0) > 0 and can_check is not True
    raised = bool(g("villain_raised"))
    if not facing:
        base = "oop_first" if role == "oop" else "ip_vs_check"
        start = pot_total if pot_total else pot   # 앞에 벳이 없으니 화면 POT 가 곧 시작 팟 (입력칸 pot 은 지난 스트리트 값일 때가 있음)
        cr = None
    else:
        if raised:
            base = "ip_vs_raise" if role == "ip" else "oop_vs_raise"
        else:
            base = "oop_vs_bet" if role == "oop" else "ip_vs_bet"
        tot = pot_total if pot_total else ((pot + call) if pot is not None else None)
        cr = call / tot if tot else None
        start = (tot - call) if (tot and not raised) else None
    # SRP 확인: 플랍 시작 팟이 SRP 팟의 0.55~2.2 배 밖이면 (림프 팟 / 3벳 팟) 다루지 않는다 (턴은 0.55~3.6 배)
    exp_pot = spot_info(rs[0], 40)["pot"]
    hi = 2.2 if len(board) == 3 else 3.6
    if start is not None and not (0.55 * exp_pot <= start <= hi * exp_pot):
        return None
    eff = _num(g("stack_bb")) or _num(g("my_stack"))
    growth = None
    if len(board) == 3:
        spr = (eff / start) if (eff and start) else None
    else:
        growth = (start / exp_pot) if start else None
        # 턴 시작 SPR → 플랍 시작 SPR 로 환산 (플랍에서 서로 넣은 칩을 되돌린다)
        spr = ((eff + (start - exp_pot) / 2) / exp_pot) if (eff and start) else None
    res = lookup(hp, vp, None if spr else eff, hero, board, base, players=players, spr=spr, call_ratio=cr,
                 line=g("flop_line"), pot_growth=growth)
    if not res:
        return None
    acts = res["actions"]
    # 액션 고르기: 공격(벳/레이즈/올인) 묶음 vs 체크/콜 vs 폴드 중 합이 큰 쪽, 공격이면 가장 많은 크기
    grp = {"agg": 0.0, "pass": 0.0, "fold": 0.0}
    for a, f in acts.items():
        grp["fold" if a == "fold" else "pass" if a in ("check", "call") else "agg"] += f
    top = max(grp, key=grp.get)
    if top == "agg":
        a = max((k for k in acts if k not in ("fold", "check", "call")), key=lambda k: acts[k])
    elif top == "pass":
        a = "check" if "check" in acts else "call"
    else:
        a = "fold"
    kind = "bet" if a.startswith("bet") else a
    size_bb, size_text = None, ""
    spot_pot = res.get("pot") or exp_pot
    ref = start if start else spot_pot
    if kind == "bet":
        pct = int(a[3:])
        size_bb = round(ref * pct / 100, 1) if ref else None
        size_text = f"팟의 {pct}%" + (f" ≈ {_bb(size_bb)}" if size_bb else "")
    elif kind == "raise":
        amt = res["amounts"].get("raise")
        if amt and ref:
            size_bb = round(amt / spot_pot * ref, 1)
            size_text = f"≈ {_bb(size_bb)} (총액)"
    elif kind == "allin":
        size_bb = eff
        size_text = "올인" + (f" {_bb(eff)}" if eff else "")
    elif kind == "call" and call:
        size_bb = call
        size_text = _bb(call)
    conf = 3
    if not res["exact_spot"]:
        conf -= 1
    if res["distance"] > 6:
        conf -= 1
    if not res["combo_exact"] or "랭크 근사 충돌" in res["notes"]:
        conf -= 1
    if (res.get("exploitability") or 0) > 1.5 or res.get("stale"):
        conf -= 1
    if spr:
        sp = spot_info(res["spot"], res["stack_used"])["spr"]
        if abs(math.log(sp / spr)) > 0.4:
            conf -= 1
    confidence = "high" if conf >= 3 else "medium" if conf == 2 else "low"
    ipos, opos = spot_info(res["spot"], 40)["ip"], spot_info(res["spot"], 40)["oop"]
    flop_txt = res["mapped_flop"] + ("" if res["exact_flop"] else f" (실제 {res['actual_flop']} 근사)")
    if res.get("street") == "turn":
        flop_txt += f" · 턴 {res['mapped_turn']} ({res['line']})"
    detail = [f"솔버 {ipos} vs {opos} SRP {res['stack_used']}BB · 플랍 {flop_txt}"]
    rtxt = _freq_text(res["range_actions"], 0.005)
    if rtxt:
        detail.append(f"레인지 전체: {rtxt}")
    notes = list(res["notes"])
    if spr:
        sp = spot_info(res["spot"], res["stack_used"])["spr"]
        if abs(math.log(sp / spr)) > 0.15:
            notes.append(f"SPR {spr:.1f}→{sp:.1f}")
    if notes:
        detail.append("근사: " + " · ".join(notes))
    return {
        "action": ACTION_KO[kind],
        "size_bb": size_bb,
        "size_text": size_text,
        "reason": "GTO: " + _freq_text(acts),
        "detail": detail[:3],
        "confidence": confidence,
        "color_key": COLOR[kind],
        "plan": [],
        "source": "gto",
        "gto": res,
    }


# --- 서브셋 생성 (개발용) -------------------------------------------------------------------
def _generate_subset(n=150):
    """빈도 가중 욕심쟁이 순서 (numpy 필요). SUBSET 를 다시 만들 때만 쓴다."""
    import numpy as np
    classes = {}
    for combo in itertools.combinations(range(52), 3):
        k = canon_flop(list(combo))[0]
        classes[k] = classes.get(k, 0) + 1
    keys = sorted(classes)
    feats = [flop_features(k) for k in keys]
    D = np.array([[feature_distance(a, b) for b in feats] for a in feats], dtype=np.float32)
    w = np.array([classes[k] for k in keys], dtype=np.float64)
    w /= w.sum()
    cost = np.full(len(keys), 40.0)
    order, stats = [], {}
    for step in range(n):
        gain = (w[:, None] * np.maximum(0.0, cost[:, None] - D)).sum(axis=0)
        gain[order] = -1
        j = int(np.argmax(gain))
        order.append(j)
        cost = np.minimum(cost, D[:, j])
        stats[step + 1] = float((w * cost).sum())
    return [keys[j] for j in order], stats
