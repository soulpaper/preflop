"""상대 레인지 기반 에쿼티 + 상대 성향(프로필) 반영 레인지 생성.

공개 API
  villain_range(action, pos, players, stack_bb, profile=None, *, opener=None) -> {핸드코드: 가중치(0~1)}
  equity_vs_ranges(hero, board, ranges, iterations=6000, rng=None) -> {equity, win, tie, lose, samples, exact}
  narrow(range, board, action='bet', keep=None, dead=()) -> {콤보('AhKd'): 가중치}   포스트플랍 좁히기
  width_pct(range) -> 레인지 폭 (%, 콤보 기준)      random_range() -> 169핸드 균등
  profile_reliable(profile) -> bool                 profile_weight(profile) -> hands/(hands+50)

레인지 표현
  키가 핸드코드('AKs', 'T9o', '77')면 그 핸드의 모든 콤보가 같은 가중치.
  키가 4글자 콤보('AhKd')면 그 콤보 하나만 (narrow 결과 / 보드에 맞춘 좁히기용). 둘을 섞어 써도 된다.
  (c1, c2, w) 튜플 리스트도 받는다 (카드는 evaluator 정수).

villain_range 의 action
  'open' / 'call_open' / '3bet' / 'call_3bet' / '4bet' / 'limp' / 'jam' / 'call_jam' / 'blind' / None(랜덤)
  기본은 preflop.ranges 차트(포지션/인원 기준). 차트가 없는 경우(UTG 의 콜 등) top_pct 폭으로 대체한다.
  profile = {vpip, pfr, threebet, fold_to_bet, af, hands, reliable} (퍼센트는 0~100).
  reliable(hands>=30) 일 때만 쓰고, 차트 폭과 상대 본인 빈도를 가중치 w = hands/(hands+50) 로 섞는다.
    open/raise  → 폭 = (1-w)*차트 + w*PFR
    3bet        → 폭 = (1-w)*차트 + w*3bet%
    call_open / limp → 폭 = (1-w)*차트 + w*(VPIP-PFR)   (PFR 위의 밴드)
  차트의 모양(어떤 핸드를 포함하는지)은 유지하고, 폭만 강한 핸드부터 줄이거나 다음으로 강한 핸드를 더한다.

속도: 레인지당 콤보 목록과 누적 가중치를 한 번만 만들고, 가중 샘플링은 random.choices(cum_weights=...)
  로 한꺼번에(C 구현) 뽑는다. 겹침은 비트마스크로 거절 샘플링한다.
"""
import random
from itertools import accumulate

import evaluator as ev
import preflop as pf

G = pf.DATA["G"]
RANK_IDX = {h: i for i, h in enumerate(G)}
BIT = [1 << i for i in range(52)]
MIN_HANDS = 30        # 이 핸드 수 이상이어야 프로필을 믿는다
PRIOR_HANDS = 50      # 블렌드 가중치 = hands / (hands + PRIOR_HANDS)
MIN_WIDTH, MAX_WIDTH = 0.6, 100.0

# narrow: 포스트플랍 액션별로 남기는 상위 비율 (현재 핸드 강도 기준)
NARROW_KEEP = {"bet": 0.60, "raise": 0.35, "allin": 0.25, "call": 0.75}


# --- 콤보 테이블 -------------------------------------------------------------------
def _build_combos():
    table = {}
    for h in G:
        r1, r2 = ev.RANKS.index(h[0]), ev.RANKS.index(h[1])
        cs = []
        if len(h) == 2:
            for s1 in range(4):
                for s2 in range(s1 + 1, 4):
                    cs.append((r1 * 4 + s1, r1 * 4 + s2))
        elif h[2] == "s":
            for s in range(4):
                cs.append((r1 * 4 + s, r2 * 4 + s))
        else:
            for s1 in range(4):
                for s2 in range(4):
                    if s1 != s2:
                        cs.append((r1 * 4 + s1, r2 * 4 + s2))
        table[h] = cs
    return table


COMBOS = _build_combos()


def random_range():
    """169 핸드 균등 = 랜덤 핸드."""
    return {h: 1.0 for h in G}


def width_pct(rng):
    """레인지 폭 (%, 가중치 적용 콤보 / 1326)."""
    return sum(w * (1 if len(k) == 4 else pf.combos(k)) for k, w in rng.items()) / 1326 * 100


# --- 프로필 ------------------------------------------------------------------------
def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None


def profile_reliable(profile):
    """reliable 키가 있으면 그것을, 없으면 hands >= 30."""
    if not profile:
        return False
    flag = profile.get("reliable")
    if flag is not None:
        return bool(flag)
    hands = _num(profile.get("hands"))
    return hands is not None and hands >= MIN_HANDS


def profile_weight(profile):
    """프로필 블렌드 가중치. 신뢰할 수 없으면 0."""
    if not profile_reliable(profile):
        return 0.0
    hands = _num(profile.get("hands"))
    hands = MIN_HANDS if hands is None else max(0.0, hands)
    return hands / (hands + PRIOR_HANDS)


def _pct_stat(profile, key):
    v = _num((profile or {}).get(key))
    return None if v is None else min(100.0, max(0.0, v))


# --- 레인지 만들기 -------------------------------------------------------------------
def _resize(base, target_pct, exclude=()):
    """차트 모양을 유지한 채 폭을 target_pct 로. 줄일 땐 강한 핸드부터 남기고, 늘릴 땐 다음으로 강한 핸드를 더한다.
    경계 핸드는 부분 가중치."""
    target_pct = min(MAX_WIDTH, max(MIN_WIDTH, target_pct))
    limit = target_pct / 100 * 1326
    cur = sum(pf.combos(h) for h in base)
    weights = {}
    acc = 0.0
    if limit <= cur:
        order = sorted(base, key=RANK_IDX.__getitem__)
    else:
        weights = {h: 1.0 for h in base}
        acc = float(cur)
        order = [h for h in G if h not in base and h not in exclude]
    for h in order:
        if acc >= limit - 1e-9:
            break
        c = pf.combos(h)
        if acc + c > limit:
            w = (limit - acc) / c
            if w >= 0.15:
                weights[h] = w
            break
        weights[h] = 1.0
        acc += c
    return weights


def _norm_players(pos, players):
    """(players, pos) 보정: 포지션 이름이 맞는 인원 수를 찾는다. 못 찾으면 pos=None."""
    try:
        n = int(players) if players else 0
    except (TypeError, ValueError):
        n = 0
    n = min(10, max(6, n)) if n else 9
    if not pos:
        return n, None
    for k in [n] + [x for x in (6, 7, 8, 9, 10) if x != n]:
        if pos in pf.seats(k):
            return k, pos
    return n, None


def _chart(situation, hero, villain, players, stack_bb, ante=False):
    try:
        return pf.ranges(situation, hero, villain, players, stack_bb or 100.0, ante)
    except (KeyError, ValueError, IndexError):
        return None


def _jam_pct(stack_bb, pos, players):
    stack = stack_bb or 20.0
    b = 2
    if pos:
        b = min(9, max(1, pf.behind(pos, players)))
    pct = pf._interp(stack, pf.DATA["ts"], pf.DATA["hs"][str(b)])
    if stack > pf.SHORT_STACK_BB:
        pct *= (pf.SHORT_STACK_BB / stack) ** 1.3
    return min(90.0, max(3.0, pct))


def _call_jam_pct(stack_bb):
    stack = stack_bb or 20.0
    pct = pf._interp(stack, pf.DATA["ts"], pf.DATA["xs"]) * 0.8
    if stack > pf.SHORT_STACK_BB:
        pct *= pf.SHORT_STACK_BB / stack
    return min(90.0, max(3.5, pct))


ALIASES = {"raise": "open", "call": "call_open", "cold_call": "call_open", "limp_call": "limp",
           "3-bet": "3bet", "threebet": "3bet", "4-bet": "4bet", "fourbet": "4bet", "5bet": "jam",
           "allin": "jam", "all_in": "jam", "call_allin": "call_jam", "bb": "blind", "check": "blind"}


def villain_range(action, pos, players, stack_bb, profile=None, *, opener=None, ante=False):
    """상대의 프리플랍 액션 + 포지션 → {핸드코드: 가중치}.

    opener: call_open / 3bet 의 '오픈한 사람' 포지션 (모르면 오픈 가능한 앞 포지션들의 중간),
            call_3bet / 4bet 의 '3벳한 사람' 포지션 (모르면 내 뒤 첫 포지션).
    ante:   토너먼트 앤티가 있으면 True — 오픈 폭과 vs 오픈(콜/3벳) 폭을 preflop 의 앤티 보정대로 넓힌다 (기본 False).
    """
    act = (action or "").lower().strip() or None
    act = ALIASES.get(act, act)
    players, pos = _norm_players(pos, players)
    w = profile_weight(profile)
    vpip, pfr, tb = (_pct_stat(profile, k) for k in ("vpip", "pfr", "threebet"))

    base, target, exclude, extras = None, None, (), {}
    chart_pct = None

    if act == "open":
        if pos and pos != "BB":
            base = pf.open_range(pos, players, ante)   # 앤티가 있으면 오픈 폭도 넓어진다 (preflop.ANTE_OPEN_MULT)
        else:
            base = pf.top_pct(14 if pos == "BB" else 18)
        chart_pct = pf.range_pct(base)
        if w and pfr is not None:
            target = (1 - w) * chart_pct + w * pfr
    elif act in ("call_open", "3bet"):
        chart = None
        o = opener
        if pos:
            cands = pf.valid_villains("vsopen", pos, players)
            if o not in cands:
                o = cands[len(cands) // 2] if cands else None
            if o:
                chart = _chart("vsopen", pos, o, players, stack_bb, ante)
        if act == "3bet":
            base = chart[0][1] if chart else pf.top_pct(6)
            chart_pct = pf.range_pct(base)
            if w and tb is not None:
                target = (1 - w) * chart_pct + w * tb
        else:
            base = chart[1][1] if chart else pf.top_pct(24) - pf.top_pct(6)
            exclude = chart[0][1] if chart else pf.top_pct(6)
            chart_pct = pf.range_pct(base)
            if w and vpip is not None and pfr is not None:
                target = (1 - w) * chart_pct + w * max(2.0, vpip - pfr)
    elif act in ("call_3bet", "4bet"):
        chart = None
        o = opener
        if pos and pos != "BB":
            cands = pf.valid_villains("vs3bet", pos, players)
            if o not in cands:
                o = cands[0] if cands else None
            if o:
                chart = _chart("vs3bet", pos, o, players, stack_bb, ante)
        if act == "4bet":
            base = chart[0][1] if chart else pf.top_pct(3)
        else:
            base = chart[1][1] if chart else pf.top_pct(10) - pf.top_pct(2)
    elif act == "limp":
        base = pf.top_pct(45) - pf.top_pct(8)
        chart_pct = pf.range_pct(base)
        extras = {h: 0.2 for h in pf.top_pct(8)}   # 프리미엄 림프(트랩)는 드물게
        if w and vpip is not None and pfr is not None:
            target = (1 - w) * chart_pct + w * max(2.0, vpip - pfr)
    elif act == "jam":
        base = pf.top_pct(_jam_pct(stack_bb, pos, players))
    elif act == "call_jam":
        base = pf.top_pct(_call_jam_pct(stack_bb))
    elif act == "blind":
        n = len(G)
        return {h: 1.0 - 0.6 * i / n for i, h in enumerate(G)}
    else:
        return random_range()

    out = _resize(base, target, exclude) if target is not None else {h: 1.0 for h in base}
    for h, x in extras.items():
        out.setdefault(h, x)
    # 집합 순서(해시 시드에 따라 실행마다 다름) 대신 강도 순서로 고정 — 같은 시드면 같은 표본이 나오게 (결정적)
    return {h: out[h] for h in sorted(out, key=RANK_IDX.__getitem__)}


# --- 레인지 펼치기 ------------------------------------------------------------------
def _cards(v):
    out = []
    for c in v or []:
        out.append(c if isinstance(c, int) else ev.parse_card(str(c)))
    return out


def expand(rng):
    """레인지 → [(c1, c2, w), ...] (중복 콤보는 호출자 책임)."""
    if isinstance(rng, dict):
        out = []
        for key, w in rng.items():
            if not w or w <= 0:
                continue
            if len(key) == 4:
                out.append((ev.parse_card(key[:2]), ev.parse_card(key[2:]), float(w)))
            else:
                for c1, c2 in COMBOS[key]:
                    out.append((c1, c2, float(w)))
        return out
    return [(t[0], t[1], float(t[2]) if len(t) > 2 else 1.0) for t in rng]


# --- 포스트플랍 좁히기 ---------------------------------------------------------------
_DRAW_FLOOR = (1, 6, 0, 0, 0)  # 드로우는 최소한 약한 원페어급으로 본다


def _has_draw(c1, c2, board):
    cards = [c1, c2] + board
    counts = [0] * 4
    for c in cards:
        counts[c & 3] += 1
    for s in range(4):
        if counts[s] == 4 and ((c1 & 3) == s or (c2 & 3) == s):
            return True
    br = {c >> 2 for c in board}
    allr = br | {c1 >> 2, c2 >> 2}
    if ev._straight_high(allr) is not None:
        return False
    done = 0
    for r in range(13):
        if r not in allr and ev._straight_high(allr | {r}) is not None and ev._straight_high(br | {r}) is None:
            done += 1
    return done >= 2


def narrow(rng, board, action="bet", keep=None, dead=()):
    """포스트플랍 베팅/레이즈한 상대의 레인지에서 약한 부분을 제거한다.

    현재 보드 기준 핸드 강도(evaluator.evaluate; 플랍/턴은 플러시·양차 드로우를 약한 원페어급으로 상향)로
    콤보를 정렬해 상위 keep 비율(가중치 기준)만 남긴다. 기본 keep: bet 0.60, raise 0.35, allin 0.25, call 0.75.
    보드/dead 카드를 가진 콤보는 제거. 반환은 콤보 키('AhKd') 레인지 -> equity_vs_ranges 에 그대로 쓴다.
    경계에서 동률인 콤보는 통째로 포함. 보드가 3장 미만이면 보드 카드만 제거하고 가중치는 그대로.
    """
    b = _cards(board)
    blocked = set(b) | set(_cards(dead))
    items = [(c1, c2, w) for c1, c2, w in expand(rng) if c1 not in blocked and c2 not in blocked]
    if keep is None:
        keep = NARROW_KEEP.get(action, 1.0)
    if len(b) < 3 or keep >= 1.0:
        return {ev.card_str(c1) + ev.card_str(c2): w for c1, c2, w in items}
    draws_possible = len(b) < 5
    scored = []
    for c1, c2, w in items:
        key = ev.evaluate([c1, c2] + b)
        if draws_possible and key < _DRAW_FLOOR and _has_draw(c1, c2, b):
            key = _DRAW_FLOOR
        scored.append((key, c1, c2, w))
    scored.sort(key=lambda t: t[0], reverse=True)
    total = sum(t[3] for t in scored)
    target = keep * total
    out, acc, i = {}, 0.0, 0
    while i < len(scored) and acc < target - 1e-9:
        j = i
        while j < len(scored) and scored[j][0] == scored[i][0]:
            j += 1
        for _, c1, c2, w in scored[i:j]:
            out[ev.card_str(c1) + ev.card_str(c2)] = w
            acc += w
        i = j
    return out


# --- 에쿼티 -------------------------------------------------------------------------
def _compile(rng, dead_mask, deck_cards):
    pop = []
    for c1, c2, w in expand(rng):
        m = BIT[c1] | BIT[c2]
        if not m & dead_mask and w > 0:
            pop.append((c1, c2, m, w))
    if not pop:  # 레인지가 전부 죽은 카드와 겹침 → 랜덤으로 대체
        pop = []
        for i, a in enumerate(deck_cards):
            for b in deck_cards[i + 1:]:
                pop.append((a, b, BIT[a] | BIT[b], 1.0))
    cw = list(accumulate(p[3] for p in pop))
    return [p[:3] for p in pop], cw


def equity_vs_ranges(hero, board, ranges, iterations=6000, rng=None):
    """hero(2장)의 에쿼티: 상대 각자가 자기 레인지에서 가중 샘플링한 두 장을 들고 있다고 가정.

    ranges: 상대별 레인지 리스트 (상대 수 = len(ranges)). 죽은 카드(내 패/보드)는 제외하고,
    상대끼리 카드가 겹치면 그 표본을 버리고 다시 뽑는다(거절 샘플링). 남은 보드는 균등 랜덤.
    표본이 하나도 안 나오는 불가능한 조합이면 ValueError.
    """
    rng = rng or random
    hero, board = _cards(hero), _cards(board)
    if len(hero) != 2 or len(set(hero + board)) != len(hero) + len(board):
        raise ValueError("잘못된 카드")
    if isinstance(ranges, dict):
        ranges = [ranges]
    if not ranges:
        raise ValueError("상대 레인지 없음")
    need = 5 - len(board)
    if need < 0:
        raise ValueError("보드가 5장 초과")
    base_mask = 0
    for c in hero + board:
        base_mask |= BIT[c]
    deck = [c for c in range(52) if not base_mask & BIT[c]]
    comp = [_compile(r, base_mask, deck) for r in ranges]
    evaluate = ev.evaluate
    sample, choices = rng.sample, rng.choices
    bl = list(board)
    hl = list(hero)
    hero_fixed = evaluate(hl + bl) if need == 0 else None
    single = len(comp) == 1
    win = tie = lose = done = 0
    share = 0.0
    attempts, cap = 0, max(iterations * 40, 2000)
    while done < iterations and attempts < cap:
        n = iterations - done
        attempts += n
        draws = [choices(pop, cum_weights=cw, k=n) for pop, cw in comp]
        for vh in zip(*draws):
            mask = base_mask
            for v in vh:
                m = v[2]
                if mask & m:
                    break
                mask |= m
            else:
                if need:
                    while True:
                        run = sample(deck, need)
                        rm = 0
                        for c in run:
                            rm |= BIT[c]
                        if not rm & mask:
                            break
                    full = bl + run
                    hs = evaluate(hl + full)
                else:
                    full = bl
                    hs = hero_fixed
                if single:
                    v = vh[0]
                    os_ = evaluate([v[0], v[1]] + full)
                    if hs > os_:
                        win += 1
                        share += 1.0
                    elif hs < os_:
                        lose += 1
                    else:
                        tie += 1
                        share += 0.5
                else:
                    scores = [evaluate([v[0], v[1]] + full) for v in vh]
                    best = max(scores)
                    if hs > best:
                        win += 1
                        share += 1.0
                    elif hs < best:
                        lose += 1
                    else:
                        tie += 1
                        share += 1.0 / (1 + scores.count(hs))
                done += 1
                if done >= iterations:
                    break
    if not done:
        raise ValueError("가능한 조합이 없음 (레인지가 서로/보드와 겹침)")
    return {"equity": share / done, "win": win / done, "tie": tie / done, "lose": lose / done,
            "samples": done, "exact": False}
