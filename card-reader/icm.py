"""토너먼트 ICM (Malmuth-Harville) — 상금 구조, $에쿼티, 버블 팩터, ICM 필요 승률, 토너먼트 단계.

순수 함수, I/O 없음, 난수 없음 (결정적).  칩 단위는 무엇이든 (advisor 는 BB).  상금은 비율(합 1) 또는 금액.

상금 구조
  payouts(n_paid, prize_pool=None, structure='standard')
      w_i ∝ 1 / (i + b)^a  (i = 1..n_paid) 를 합 1 로 정규화 (prize_pool 을 주면 금액).
      'standard'  a=1.00 b=0.5 : 흔한 온라인 MTT (15명 지급 시 1등 24% / 2등 15% / 3등 10% / 최소 2.4%), 3명 지급 49/30/21
      'flat'      a=0.70 b=1.0 : 평평한 구조 (15명 1등 15%)
      'top_heavy' a=1.35 b=0.3 : 상위 집중 (15명 1등 37%)
      리스트/튜플을 주면 그 비율(또는 금액)을 정규화해서 쓴다 (custom).
  paid_places(entrants, frac=0.15) : 기본 참가자의 ~15% 지급 (작은 필드는 최소 3명, 2인 이하는 1명)

ICM 에쿼티 — icm_equities(stacks, payouts)
  Malmuth-Harville: 남은 칩 비율대로 1등을 뽑고, 나머지에서 같은 방식으로 2등 … (= Plackett-Luce).
  · 살아 있는 인원 <= EXACT_MAX(11): 정확한 재귀 (부분집합 비트마스크 DP + 메모: '상위 k 자리를 차지한 집합' 확률, O(2^n·n), 10명 ≈ 3ms)
  · 그보다 많으면 '지수 경주' 적분 (같은 Harville 모델의 1차원 적분 표현, 근사는 수치 적분뿐):
      Harville 순위 = T_j = E_j / s_j (E_j ~ Exp(1) 독립) 의 오름차순.  내 T 를 x 로 고정하면 다른 사람 j 가 나보다 앞설
      확률은 독립적으로 p_j = 1 - exp(-(s_j/s_h)·x) → 내 순위 분포 = ∫ e^-x · PoissonBinomial(p_j(x)) dx.
      x = e^y 로 바꿔 y ∈ [-26, 4.5] 사다리꼴 340점 (매끄러운 이중지수 꼴이라 스펙트럼 정확도) + 같은 스택은 이항 분포로 묶는다.
      정확도 (test_icm.py 로 재현): 10명 무작위 스택 × 3 구조에서 정확 재귀와의 최대 차이 ≈ 5e-12 (상금 비율 기준).
      32명 필드 한 명 계산 ≈ 1.5ms, 310명 필드 버블 팩터(3상태) ≈ 6ms.
  · 필드 축소 모델 (advisor): 내 테이블 스택은 그대로, 나머지 (남은 인원 - 테이블 인원) 은 '나머지 평균 스택' 동일 스택으로 본다
      (평균 스택 × 남은 인원 = 총 칩).  실제 나머지 스택이 고르지 않으면 오차가 생긴다 — test_icm.py 의 예
      (10명 중 4명만 알고 6명을 평균으로 대체, 15회) 에서 내 에쿼티 차이 최대 0.84%p (상금 비율), 버블 팩터 차이 최대 0.13.

버블 팩터 / ICM 필요 승률
  bubble_factor(hero, villain, others, payouts)  : 올인 대결 (서로 min(hero, villain) 만큼 걸림) 에서
                                                   ($ 잃는 양) / ($ 따는 양).  칩EV 면 1, 버블에서 덮인(covered) 스택은 1.5~3.
  required_equity_icm(risk, reward, hero, villain, others, payouts)
      risk  : 내가 더 넣는 칩 (콜 금액, 내 스택 이하)
      reward: 이기면 내가 가져오는 남의 칩 = 콜 전 팟 전체 (상대 벳 + 블라인드·앤티·다른 사람 돈 = 데드 머니)
      villain: 상대의 남은 스택 (벳 뒤, 올인이면 0)
      상태: 폴드 → 나 hero, 상대 villain + reward (팟을 가져감) / 이김 → hero + reward / 짐 → hero - risk, 상대 villain + reward + risk
      필요 승률 = ($폴드 - $짐) / ($이김 - $짐).  칩EV 로는 risk / (risk + reward) 와 같다.
  위험 프리미엄 (risk premium) = 50:50 올인 대결(데드 머니 없음)의 ICM 필요 승률 - 50% = BF/(1+BF) - 0.5

토너먼트 단계 — tournament_state(players_left, entrants, my_rank, table_stacks, avg_stack=None, payouts=None, ...)
  지급 인원 P, 남은 인원 N, over = N - P
  final (파이널 테이블: N <= P 이고 N <= 9) / itm (인 더 머니) / bubble (over <= max(1, ceil(0.1P)))
  / near_bubble (over <= max(3, 0.5P)) / mid (over <= 2P) / early (그 밖) / unknown (지급 인원을 모름)

속도: 상태별 내 에쿼티는 (스택 0.1% 버킷, 상금) 으로 lru_cache — advisor 한 번에 < 10ms (32명 필드).
"""
import math
from collections import Counter
from functools import lru_cache

import numpy as np

STRUCTURES = {"standard": (1.0, 0.5), "flat": (0.70, 1.0), "top_heavy": (1.35, 0.3)}
PAID_FRAC = 0.15
EXACT_MAX = 11
TABLE_SIZE = 9
STAGE_LABEL = {"early": "초반", "mid": "중반", "near_bubble": "버블 근처", "bubble": "버블", "itm": "인 더 머니",
               "final": "파이널 테이블", "unknown": "상금 구조 모름"}

# 지수 경주 적분 격자: x = e^y, ∫ e^-x g(x) dx = ∫ x e^-x g(x) dy
_Y = np.linspace(-26.0, 4.5, 340)
_X = np.exp(_Y)
_W = _X * np.exp(-_X) * (_Y[1] - _Y[0])
_W[0] *= 0.5
_W[-1] *= 0.5


# --- 상금 구조 -------------------------------------------------------------------------
def paid_places(entrants, frac=PAID_FRAC):
    """참가자 수 → 지급 인원 (기본 ~15%, 6명 이상이면 최소 3명)."""
    n = int(entrants or 0)
    if n <= 0:
        return 0
    if n <= 2:
        return 1
    return max(1, min(n - 1, max(int(round(n * frac)), min(3, n // 2))))


def _bands(n):
    """온라인 MTT 식 지급 구간: 1~9 등 개별, 10~18 등 3명씩, 19~54 등 9명씩, 그 뒤 18명씩."""
    out, i = [], 1
    while i <= n:
        size = 1 if i <= 9 else 3 if i <= 18 else 9 if i <= 54 else 18
        out.append((i, min(n, i + size - 1)))
        i += size
    return out


def payouts(n_paid, prize_pool=None, structure="standard", bands=True):
    """지급 순위별 상금. 비율(합 1) — prize_pool 을 주면 금액.  structure: 'standard' / 'flat' / 'top_heavy' / 리스트.
    bands: 10등 아래는 같은 구간이 같은 상금 (10~12, 13~15, 16~18, 19~27 …; 실제 온라인 MTT 처럼 — 버블 직후 ICM 압박이 줄어든다)."""
    if isinstance(structure, (list, tuple)):   # custom: 순위 순서로 (큰 상금이 위) 정렬
        w = sorted((max(0.0, float(x)) for x in structure), reverse=True)
    else:
        n = int(n_paid or 0)
        if n <= 0:
            return []
        a, b = STRUCTURES.get(structure, STRUCTURES["standard"])
        w = [1.0 / (i + b) ** a for i in range(1, n + 1)]
        if bands:
            for lo, hi in _bands(n):
                avg = sum(w[lo - 1:hi]) / (hi - lo + 1)
                w[lo - 1:hi] = [avg] * (hi - lo + 1)
    s = sum(w)
    if s <= 0:
        return []
    scale = (prize_pool if prize_pool else 1.0) / s
    return [x * scale for x in w]


def as_payouts(spec, paid=None, prize_pool=None):
    """payouts 인자 정규화: 리스트(비율/금액) 또는 프리셋 이름 → 비율 리스트(합 1, prize_pool 이면 금액)."""
    if isinstance(spec, (list, tuple)) and spec:
        return payouts(None, prize_pool, list(spec))
    if paid:
        return payouts(paid, prize_pool, spec if isinstance(spec, str) else "standard")
    return []


# --- ICM 에쿼티 -----------------------------------------------------------------------
def _exact(st, pays):
    """정확한 Malmuth-Harville (모든 st > 0).  prob[mask] = mask 집합이 상위 popcount(mask) 자리를 (순서 무관) 차지할 확률."""
    m = len(st)
    K = min(m, len(pays))
    eq = [0.0] * m
    if K == 0:
        return eq
    total = float(sum(st))
    full = 1 << m
    msum = [0.0] * full
    pc = [0] * full
    for mask in range(1, full):
        low = mask & -mask
        i = low.bit_length() - 1
        msum[mask] = msum[mask ^ low] + st[i]
        pc[mask] = pc[mask ^ low] + 1
    prob = [0.0] * full
    prob[0] = 1.0
    bits = [1 << i for i in range(m)]
    for mask in range(full):       # mask | bit > mask 라서 숫자 순서로 처리하면 선행 상태가 이미 확정돼 있다
        p = prob[mask]
        k = pc[mask]
        if p == 0.0 or k >= K:
            continue
        rem = total - msum[mask]
        if rem <= 0:
            continue
        pay = pays[k]
        nxt = k + 1 < K
        f = p / rem
        for i in range(m):
            b = bits[i]
            if mask & b:
                continue
            q = f * st[i]
            eq[i] += q * pay
            if nxt:
                prob[mask | b] += q
    return eq


def _place_dist(hero, groups, K):
    """내 순위 분포 P(내 앞에 k 명) k=0..K-1 (지수 경주 적분).  groups: [(stack, count)] (stack > 0)."""
    dist = np.zeros((_X.size, K))
    dist[:, 0] = 1.0
    for s, cnt in groups:
        p = -np.expm1(-(s / hero) * _X)          # 나보다 앞설 확률 (노드별)
        if cnt <= 3 or K <= 2:
            for _ in range(cnt):
                new = dist * (1.0 - p)[:, None]
                new[:, 1:] += dist[:, :-1] * p[:, None]
                dist = new
        else:                                     # 같은 스택 cnt 명 = 이항 분포로 한 번에 합성
            kmax = min(cnt, K - 1)
            ks = np.arange(kmax + 1)
            lc = np.array([math.lgamma(cnt + 1) - math.lgamma(k + 1) - math.lgamma(cnt - k + 1) for k in ks])
            with np.errstate(divide="ignore"):
                lp = np.log(np.clip(p, 1e-300, 1.0))[:, None]
                lq = np.log1p(-np.clip(p, 0.0, 1.0 - 1e-16))[:, None]
            pmf = np.exp(lc[None, :] + ks[None, :] * lp + (cnt - ks)[None, :] * lq)
            new = np.zeros_like(dist)
            for k in range(kmax + 1):
                new[:, k:] += dist[:, :K - k] * pmf[:, k:k + 1]
            dist = new
    return _W @ dist


def _groups(others):
    """others: 스택 리스트 또는 [(stack, count)] → (살아 있는 그룹, 탈락자 수)."""
    cnt = Counter()
    dead = 0
    for o in others:
        s, n = (o if isinstance(o, tuple) else (o, 1))
        n = int(n)
        if n <= 0:
            continue
        s = float(s)
        if s <= 0:
            dead += n
        else:
            cnt[s] += n
    return sorted(cnt.items()), dead


def hero_equity(hero, others, pays):
    """한 명(hero)의 ICM 에쿼티.  others: 다른 사람 스택 리스트 또는 [(stack, count)].  pays: 순위별 상금 리스트."""
    groups, dead = _groups(others)
    n_alive = sum(c for _, c in groups) + (1 if hero > 0 else 0)
    n = n_alive + dead
    pays = list(pays)[:n]
    if hero <= 0:   # 이미 탈락: 살아 있는 사람들 아래 순위 (탈락자끼리는 같은 순위로 나눔)
        lo, hi = n_alive, n
        k = dead + 1
        return sum(pays[lo:hi]) / k if hi > lo else 0.0
    if not groups:
        return float(sum(pays[:1]))
    K = min(n_alive, len(pays))
    if K <= 0:
        return 0.0
    if n_alive <= EXACT_MAX:
        st = [float(hero)] + [s for s, c in groups for _ in range(c)]
        return _exact(st, pays[:n_alive])[0]
    d = _place_dist(float(hero), groups, K)
    return float(np.dot(d, pays[:K]))


def icm_equities(stacks, payouts_):
    """모든 사람의 ICM 에쿼티 (payouts_ 단위; 합 = 남은 인원 만큼의 상금 합).  스택 0 은 탈락 (아래 순위를 나눠 가짐)."""
    st = [max(0.0, float(s)) for s in stacks]
    n = len(st)
    pays = list(payouts_)[:n]
    pays += [0.0] * (n - len(pays))
    alive = [i for i in range(n) if st[i] > 0]
    out = [0.0] * n
    dead = [i for i in range(n) if st[i] <= 0]
    if dead:
        share = sum(pays[len(alive):]) / len(dead)
        for i in dead:
            out[i] = share
    if not alive:
        return out
    if len(alive) <= EXACT_MAX:
        eq = _exact([st[i] for i in alive], pays[:len(alive)])
        for j, i in enumerate(alive):
            out[i] = eq[j]
        return out
    K = len(alive)
    for i in alive:
        others = [st[j] for j in alive if j != i]
        groups, _ = _groups(others)
        out[i] = float(np.dot(_place_dist(st[i], groups, K), pays[:K]))
    return out


# --- 캐시된 내 에쿼티 (스택 버킷) ---------------------------------------------------------
def bucket(s):
    """스택 0.1% 버킷 (유효숫자 4자리: 캐시 적중 + 결과 오차는 버블 팩터 ~0.5% 이내)."""
    s = float(s)
    if s <= 0:
        return 0.0
    e = math.floor(math.log10(s)) - 3
    step = 10.0 ** e
    return round(round(s / step) * step, 10)


@lru_cache(maxsize=4096)
def _hero_eq_cached(hero, groups, pays):
    return hero_equity(hero, list(groups), pays)


def hero_equity_cached(hero, others, pays):
    groups, dead = _groups(others)
    key = Counter()
    for s, c in groups:
        key[bucket(s)] += c
    g = tuple(sorted(key.items())) + ((0.0, dead),) if dead else tuple(sorted(key.items()))
    return _hero_eq_cached(bucket(hero), g, tuple(round(p, 12) for p in pays))


def bubble_factor(hero_stack, villain_stack, others, payouts_):
    """올인 대결의 버블 팩터 = ($ 잃는 양) / ($ 따는 양).  걸리는 칩 = min(hero, villain).  others: 나머지 스택."""
    h, v = float(hero_stack), float(villain_stack)
    r = min(h, v)
    if r <= 0:
        return 1.0
    others = list(others)
    e0 = hero_equity_cached(h, [(v, 1)] + others, payouts_)
    ew = hero_equity_cached(h + r, [(v - r, 1)] + others, payouts_)
    el = hero_equity_cached(h - r, [(v + r, 1)] + others, payouts_)
    gain, loss = ew - e0, e0 - el
    if gain <= 1e-15:
        return float("inf") if loss > 0 else 1.0
    return loss / gain


def required_equity_icm(risk, reward, hero_stack, villain_stack, others, payouts_, detail=False):
    """콜/잼의 ICM 필요 승률 (0~1).  risk: 내가 더 넣는 칩, reward: 콜 전 팟 (상대 벳 + 데드 머니).
    villain_stack: 상대의 남은 스택 (벳 뒤).  detail=True 면 dict(icm, chip, eq_fold, eq_win, eq_lose)."""
    h = float(hero_stack)
    risk = max(0.0, min(float(risk), h))
    reward = max(0.0, float(reward))
    v = max(0.0, float(villain_stack))
    chip = risk / (risk + reward) if risk + reward > 0 else 0.0
    others = list(others)
    ef = hero_equity_cached(h, [(v + reward, 1)] + others, payouts_)
    ew = hero_equity_cached(h + reward, [(v, 1)] + others, payouts_)
    el = hero_equity_cached(h - risk, [(v + reward + risk, 1)] + others, payouts_)
    span = ew - el
    req = (ef - el) / span if span > 1e-15 else chip
    req = min(0.999, max(0.0, req))
    if detail:
        return {"icm": req, "chip": chip, "eq_fold": ef, "eq_win": ew, "eq_lose": el}
    return req


def risk_premium(bf):
    """50:50 올인(데드 머니 없음)의 필요 승률 증가분 = BF/(1+BF) - 0.5 (0 이상)."""
    if bf is None or bf != bf:
        return 0.0
    if math.isinf(bf):
        return 0.5
    return max(0.0, bf / (1.0 + bf) - 0.5)


# --- 필드 모델 / 토너먼트 단계 ------------------------------------------------------------
def _stack_list(table_stacks):
    if not table_stacks:
        return []
    vals = table_stacks.values() if isinstance(table_stacks, dict) else table_stacks
    out = []
    for v in vals:
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f > 0:
            out.append(f)
    return out


def rest_of_field(players_left, known_stacks, avg_stack=None, extra_chips=0.0):
    """테이블 밖 나머지 인원을 평균 스택 그룹으로: [(stack, count)].
    known_stacks: 이미 모델에 넣은 스택 (나·상대·테이블), avg_stack 이 없으면 아는 스택의 평균, extra_chips: 팟 등 테이블 위 칩."""
    known = [s for s in known_stacks if s and s > 0]
    n_rest = int(players_left or 0) - len(known)
    if n_rest <= 0 or not known:
        return []
    avg = avg_stack if avg_stack and avg_stack > 0 else sum(known) / len(known)
    if avg_stack and avg_stack > 0:
        rest_total = avg * int(players_left) - sum(known) - (extra_chips or 0.0)
        r = max(0.3 * avg, rest_total / n_rest)
    else:
        r = avg
    return [(r, n_rest)]


def stage_of(players_left, paid, table_size=TABLE_SIZE):
    n = int(players_left or 0)
    p = int(paid or 0)
    if n <= 0 or p <= 0:
        return "unknown"
    over = n - p
    if over <= 0:
        return "final" if n <= table_size else "itm"
    if over <= max(1, math.ceil(0.1 * p)):
        return "bubble"
    if over <= max(3, 0.5 * p):
        return "near_bubble"
    if over <= 2 * p:
        return "mid"
    return "early"


def tournament_state(players_left, entrants, my_rank, table_stacks, avg_stack=None, payouts=None, paid=None,
                     hero_stack=None, prize_pool=None):
    """단계와 위험 프리미엄.  payouts: 리스트 또는 프리셋 이름 (없으면 'standard'), paid: 지급 인원 (없으면 entrants 의 ~15%).
    table_stacks: 내 테이블 스택 (dict 또는 리스트).  avg_stack 이 없으면 테이블 스택 평균.  hero_stack 이 없으면 평균 스택 기준.
    반환 dict: stage, label, players_left, entrants, paid, my_rank, avg_stack, payouts(비율), bubble_factor, risk_premium(0~0.5)
    (bubble_factor / risk_premium 은 내 스택 vs 평균 스택 (서로 같으면 덮는 쪽) 의 올인 기준)."""
    spec = payouts if payouts is not None else "standard"
    if isinstance(spec, (list, tuple)) and spec:
        paid = len(spec)
    if not paid and entrants:
        paid = paid_places(entrants)
    pays = as_payouts(spec, paid) if paid else []
    stacks = _stack_list(table_stacks)
    avg = avg_stack if avg_stack and avg_stack > 0 else (sum(stacks) / len(stacks) if stacks else None)
    stage = stage_of(players_left, paid)
    out = {"stage": stage, "label": STAGE_LABEL[stage], "players_left": players_left, "entrants": entrants,
           "paid": paid, "my_rank": my_rank, "avg_stack": avg, "payouts": pays, "bubble_factor": 1.0,
           "risk_premium": 0.0}
    if stage == "unknown" or not avg or not players_left or players_left < 2:
        return out
    hero = float(hero_stack) if hero_stack and hero_stack > 0 else avg
    vil = avg
    others = rest_of_field(players_left, [hero, vil], avg)
    bf = bubble_factor(hero, vil, others, pays)
    out["bubble_factor"] = bf
    out["risk_premium"] = risk_premium(bf)
    return out
