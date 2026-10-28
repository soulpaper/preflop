"""advisor.recommend 포스트플랍 헤즈업 판단을 GTO 솔버 데이터셋과 비교하는 벤치마크.

데이터: jevonmao/postflop-solver-reasoning-traces-1m (Hugging Face, CC-BY-4.0) — postflop-solver 엔진으로 만든
헤즈업 블라인드 대 블라인드(SB 오픈 / BB 디펜스, 200BB, SB 50 / BB 100 칩) 포스트플랍 결정 467,680개.
전처리본: ../../preflop-tools/gto_dataset/spots.jsonl.gz  (환경변수 GTO_DATASET 로 바꿀 수 있다)

  python bench_gto.py --n 20000                # 검증 분할 앞 20k — 범위/랜덤 에쿼티 두 모드
  python bench_gto.py --n 20000 --split train  # 학습 분할에서 (HU_GTO 튜닝은 train 만 봤다)
  python bench_gto.py --n 20000 --confusion    # 혼동 행렬까지
  python bench_gto.py --n 20000 --minimal      # hero_ip/aggressor/포지션 없이 (라이브 앱 현재 ctx → 'unk' 열)
  python bench_gto.py --n 5000 --mode range    # 한 모드만

랜덤 핸드 에쿼티는 evaluator.equity (플랍/턴 2000회 몬테카를로, 리버 전수, 키별 고정 시드) 로 계산해
데이터셋 옆 eq_random_cache.json 에 캐시한다 (처음 80k 는 12코어로 약 10분).
데이터 주의: 클래스 균형 샘플링(폴드/콜/벳/체크/올인 각 ~87k), 200BB 레이즈 전쟁 때문에 SPR<4 노드가 68%, 리버 노드 일부는
'Your equity' 가 0 인 이상값(강한 패인데 0%) — share 지표가 노드별 최빈 액션 기준이라 샘플링 편향에는 덜 민감하다.

분할: 전체를 고정 시드(--seed, 기본 20261029)로 섞어 앞 80% train / 뒤 20% validation.  --n 은 그 안에서 앞쪽 n 개.
점수 (행동 범주 fold / check / call / bet(벳·레이즈) / allin, 어드바이저 출력은 합법 액션으로 사영)
  agree     : 범주가 솔버 정답과 같으면 1
  freq_w    : action_freq 가중 일치율 (솔버가 자주 하는 정답을 맞힐수록 큰 가중)
  accept    : 일치 또는 정답 빈도 < 0.6 (혼합 전략이라 다른 액션도 허용)
  share     : 솔버가 그 노드에서 추천 범주를 고를 확률 추정 — 일치면 freq, 범주가 2개뿐인 노드면 1-freq (정확),
              3개 이상이면 (1-freq)/(k-1).  노드별 최적(최빈 액션)이 데이터셋의 클래스 균형 샘플링에 흔들리지 않는 지표
"""
import argparse
import gzip
import json
import os
import random
import re
import sys
import time
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import evaluator as ev  # noqa: E402

DATA = os.environ.get("GTO_DATASET", os.path.join(os.path.dirname(os.path.dirname(HERE)), "preflop-tools",
                                                   "gto_dataset", "spots.jsonl.gz"))
if not os.path.exists(DATA):
    DATA = r"C:\Users\soulo\Documents\preflop-tools\gto_dataset\spots.jsonl.gz"
EQ_CACHE = os.path.join(os.path.dirname(DATA), "eq_random_cache.json")
BB = 100.0
SEED = 20261029
TRAIN_FRAC = 0.8
EQ_ITER = 2000          # 랜덤 에쿼티 몬테카를로 반복 (리버는 전수)
CATS = ("fold", "check", "call", "bet", "allin")
NODE_TYPES = ("facing", "checked_to", "first")
SPR_BUCKETS = ((0, 1, "<1"), (1, 2, "1-2"), (2, 4, "2-4"), (4, 8, "4-8"), (8, 16, "8-16"), (16, 1e9, "16+"))
ADV_CAT = {"폴드": "fold", "체크": "check", "콜": "call", "벳": "bet", "레이즈": "bet", "올인": "allin", "대기": "check"}

_ACT = re.compile(r"(SB|BB) (checked|called|bet (\d+) chips|raised to (\d+) chips|went all-in \((\d+) chips\))")


def gold_cat(a):
    w = a.split()[0]
    return {"bet": "bet", "raise": "bet", "all-in": "allin"}.get(w, w)


def _street_actions(sc, street):
    m = re.search(r"The %s comes [^\n]*" % street, sc)
    if not m:
        return []
    line = m.group(0)
    if ", then " not in line:
        return []
    return _ACT.findall(line.split(", then ", 1)[1])


def _last_aggressor(sc, street):
    """이전 베팅 라운드의 마지막 공격자 ('SB'/'BB') 또는 None (체크로 돌았음)."""
    prev = {"flop": None, "turn": "flop", "river": "turn"}[street]
    if prev is None:
        m = re.search(r"Before the flop, ([^\n]*)", sc)
        raisers = re.findall(r"(SB|BB) raised", m.group(1)) if m else []
        return raisers[-1] if raisers else None
    last = None
    for who, verb, *_ in _street_actions(sc, prev):
        if not verb.startswith(("checked", "called")):
            last = who
    return last


def parse(d):
    """레코드 → spot dict (ctx 는 equity 없이)."""
    sc, m = d["sc"], d["m"]
    hero_pos = re.search(r"your position is (SB|BB)", sc).group(1)
    vill = "BB" if hero_pos == "SB" else "SB"
    pot = int(re.search(r"current pot size is (\d+) chips", sc).group(1))
    stack = int(re.search(r"Your current stack: (\d+) chips", sc).group(1))
    legal = re.search(r"Legal actions: (.*?)\.\n", sc).group(1).split(", ")
    street = m["street"]
    acts = _street_actions(sc, street)
    contrib = {"SB": 0, "BB": 0}
    last_v_inc, v_allin, v_raised = 0, False, False
    for who, verb, b, r, a in acts:
        before = contrib[who]
        if verb == "called":
            contrib[who] = max(contrib.values())
        elif b:
            contrib[who] = int(b)
        elif r:
            contrib[who] = int(r)
        elif a:
            contrib[who] = int(a)
        if who == vill:
            last_v_inc = contrib[who] - before
            v_allin = bool(a)
            v_raised = bool(r or a) and contrib[hero_pos] > 0
    call = max(0, contrib[vill] - contrib[hero_pos])
    legal_cats = sorted({gold_cat(x) for x in legal})
    node = "facing" if "call" in legal_cats else ("checked_to" if acts else "first")
    spr = stack / (pot + call) if pot + call > 0 else 0.0
    board = [m["board"][i:i + 2] for i in range(0, len(m["board"]), 2)]
    hero = [m["hero_hand"][:2], m["hero_hand"][2:]]
    eqm = re.search(r"Your equity: ([\d.]+)%", d["ctx"])
    agg = _last_aggressor(sc, street)
    gold_size = None
    parts = d["a"].split()
    if len(parts) > 1 and parts[0] == "bet":
        gold_size = int(parts[-1]) / pot if pot else None
    pot_before = pot - last_v_inc if call else pot
    ctx = dict(hero=hero, board=board, street=street, opponents_in_hand=1, hero_pos=hero_pos, villain_pos=vill,
               hero_ip=hero_pos == "SB", aggressor=None if agg is None else agg == hero_pos,
               pot=pot_before / BB, pot_total=pot / BB, call=call / BB, can_check="check" in legal_cats,
               to_act=True, stack_bb=(stack + call) / BB, my_stack=(stack + call) / BB, closing_action=True,
               villain_raised=v_raised, facing_allin=v_allin or (call > 0 and stack == 0))
    return {
        "ctx": ctx, "gold": gold_cat(d["a"]), "a": d["a"], "freq": float(m["action_freq"]), "legal": legal_cats,
        "legal_raw": legal, "node": node, "spr": spr, "matchup": m["matchup"], "street": street,
        "eq_range": float(eqm.group(1)) / 100 if eqm else None, "gold_size": gold_size, "key": f"{m['hero_hand']}|{m['board']}",
    }


# --- 로드 / 분할 ------------------------------------------------------------------
def load_split(split="val", n=None, seed=SEED, path=DATA):
    with gzip.open(path, "rt", encoding="utf-8") as f:
        lines = f.readlines()
    idx = list(range(len(lines)))
    random.Random(seed).shuffle(idx)
    cut = int(len(idx) * TRAIN_FRAC)
    sel = idx[:cut] if split == "train" else idx[cut:]
    if n:
        sel = sel[:n]
    return [parse(json.loads(lines[i])) for i in sel]


# --- 랜덤 핸드 에쿼티 (캐시) --------------------------------------------------------
def _rand_eq(key):
    hh, bb = key.split("|")
    hero = [ev.parse_card(hh[:2]), ev.parse_card(hh[2:])]
    board = [ev.parse_card(bb[i:i + 2]) for i in range(0, len(bb), 2)]
    rng = random.Random(sum(ord(ch) * (i + 1) for i, ch in enumerate(key)))   # 키별 고정 시드 → 재현 가능
    if len(board) == 5:
        return key, ev.equity(hero, board, 1, rng=rng)["equity"]
    return key, ev.equity(hero, board, 1, iterations=EQ_ITER, exact_limit=0, rng=rng)["equity"]


def random_equity(spots, procs=None):
    cache = {}
    if os.path.exists(EQ_CACHE):
        with open(EQ_CACHE, encoding="utf-8") as f:
            cache = json.load(f)
    todo = sorted({s["key"] for s in spots} - set(cache))
    if todo:
        from multiprocessing import Pool
        t = time.time()
        with Pool(procs or max(1, (os.cpu_count() or 2) - 1)) as pool:
            for k, e in pool.imap_unordered(_rand_eq, todo, chunksize=64):
                cache[k] = e
        print(f"[랜덤 에쿼티 {len(todo)}개 계산 {time.time() - t:.0f}s]", file=sys.stderr)
        tmp = EQ_CACHE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f)
        os.replace(tmp, EQ_CACHE)
    for s in spots:
        s["eq_random"] = cache[s["key"]]
    return spots


# --- 예측 / 점수 ------------------------------------------------------------------
def make_ctx(s, mode, minimal=False):
    ctx = dict(s["ctx"])
    if mode == "range":
        ctx["equity"], ctx["equity_basis"] = s["eq_range"], "range"
    else:
        ctx["equity"] = s["eq_random"]
    if minimal:  # 라이브 앱이 아직 안 넘기는 키를 빼고 (위치/주도권/레이즈 여부 모름 → HU_GTO 'unk' 열).
        # hero_pos/villain_pos 도 뺀다: 이 데이터는 헤즈업(SB = 버튼 = IP)이라 9인 기준 추정(SB 가 먼저)과 반대
        for k in ("hero_ip", "aggressor", "villain_raised", "closing_action", "hero_pos", "villain_pos"):
            ctx.pop(k, None)
    return ctx


def project(cat, legal):
    """어드바이저 범주를 그 노드의 합법 액션으로 사영 (벳 의도인데 올인만 있으면 올인 등)."""
    if cat in legal:
        return cat
    alt = {"bet": "allin", "allin": "bet", "fold": "check", "call": "check", "check": "call"}.get(cat)
    if alt in legal:
        return alt
    return cat


def share(pred, s):
    if pred == s["gold"]:
        return s["freq"]
    k = len(s["legal"])
    if pred not in s["legal"]:
        return 0.0
    return (1 - s["freq"]) / max(1, k - 1)


def predict(spots, mode, recommend=None, minimal=False):
    if recommend is None:
        from advisor import recommend
    out = []
    for s in spots:
        r = recommend(make_ctx(s, mode, minimal))
        cat = project(ADV_CAT[r["action"]], s["legal"])
        size = None
        if cat == "bet" and r.get("size_bb") and s["ctx"]["call"] == 0:
            size = r["size_bb"] / s["ctx"]["pot_total"]
        out.append((cat, size))
    return out


def spr_bucket(x):
    for lo, hi, name in SPR_BUCKETS:
        if lo <= x < hi:
            return name
    return SPR_BUCKETS[-1][2]


class Score:
    __slots__ = ("n", "agree", "fw", "fsum", "accept", "share")

    def __init__(self):
        self.n = self.agree = self.fw = self.fsum = self.accept = self.share = 0.0

    def add(self, pred, s):
        ok = pred == s["gold"]
        self.n += 1
        self.agree += ok
        self.fw += s["freq"] * ok
        self.fsum += s["freq"]
        self.accept += ok or s["freq"] < 0.6
        self.share += share(pred, s)

    def row(self):
        n = max(1, self.n)
        return {"n": int(self.n), "agree": self.agree / n, "freq_w": self.fw / max(1e-9, self.fsum),
                "accept": self.accept / n, "share": self.share / n}


def evaluate(spots, preds):
    groups = defaultdict(Score)
    conf = defaultdict(Counter)
    size_ok = Counter()
    for s, (p, size) in zip(spots, preds):
        keys = ["all", f"street={s['street']}", f"node={s['node']}", f"{s['street']}×{s['node']}",
                f"spr={spr_bucket(s['spr'])}", f"matchup={s['matchup']}", "lowspr" if s["spr"] < 4 else "highspr"]
        for k in keys:
            groups[k].add(p, s)
        conf[s["node"]][(s["gold"], p)] += 1
        conf["all"][(s["gold"], p)] += 1
        if p == "bet" and s["gold"] == "bet" and size is not None and s["gold_size"] is not None:
            size_ok["n"] += 1
            size_ok["ok"] += (size <= 0.5 + 1e-6) == (s["gold_size"] <= 0.5 + 1e-6)
    return {k: v.row() for k, v in groups.items()}, conf, size_ok


def print_report(title, rows, conf=None, size_ok=None, file=sys.stdout):
    print(f"\n=== {title} ===", file=file)
    order = ["all", "lowspr", "highspr"] + [f"street={x}" for x in ("flop", "turn", "river")] + \
        [f"node={x}" for x in NODE_TYPES] + [f"{s}×{n}" for s in ("flop", "turn", "river") for n in NODE_TYPES] + \
        [f"spr={b[2]}" for b in SPR_BUCKETS] + [f"matchup={x}" for x in ("SRP", "3BP", "4BP")]
    print(f"{'group':24s} {'n':>6s} {'agree':>6s} {'freq_w':>6s} {'accept':>6s} {'share':>6s}", file=file)
    for k in order:
        if k in rows:
            r = rows[k]
            print(f"{k:24s} {r['n']:6d} {r['agree']:6.3f} {r['freq_w']:6.3f} {r['accept']:6.3f} {r['share']:6.3f}", file=file)
    if size_ok and size_ok["n"]:
        print(f"bet size class (<=50% pot vs bigger) agreement on matched bets: {size_ok['ok'] / size_ok['n']:.3f} "
              f"(n={size_ok['n']})", file=file)
    if conf:
        for node in ("all",) + NODE_TYPES:
            c = conf.get(node)
            if not c:
                continue
            print(f"\nconfusion [{node}] rows=solver gold, cols=advisor", file=file)
            print("        " + "".join(f"{x:>8s}" for x in CATS), file=file)
            for g in CATS:
                if any(c[(g, p)] for p in CATS):
                    print(f"{g:8s}" + "".join(f"{c[(g, p)]:8d}" for p in CATS), file=file)


def run(spots, modes=("range", "random"), confusion=False, minimal=False, recommend=None, quiet=False):
    res = {}
    for mode in modes:
        t = time.perf_counter()
        preds = predict(spots, mode, recommend, minimal)
        dt = time.perf_counter() - t
        rows, conf, size_ok = evaluate(spots, preds)
        res[mode] = rows
        if not quiet:
            print_report(f"mode={mode}{' (minimal ctx)' if minimal else ''}  n={len(spots)}  "
                         f"{dt / max(1, len(spots)) * 1000:.2f} ms/call", rows, conf if confusion else None, size_ok)
    return res


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--n", type=int, default=20000, help="표본 수 (분할 안에서 앞쪽 n 개)")
    ap.add_argument("--split", choices=("train", "val"), default="val")
    ap.add_argument("--mode", choices=("range", "random", "both"), default="both")
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--confusion", action="store_true")
    ap.add_argument("--minimal", action="store_true", help="hero_ip/aggressor/villain_raised 없이 (라이브 앱 ctx 근사)")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    t = time.time()
    spots = load_split(args.split, args.n, args.seed)
    print(f"[{args.split} {len(spots)}개 로드 {time.time() - t:.1f}s]", file=sys.stderr)
    modes = ("range", "random") if args.mode == "both" else (args.mode,)
    if "random" in modes:
        random_equity(spots)
    run(spots, modes, args.confusion, args.minimal)


if __name__ == "__main__":
    main()
