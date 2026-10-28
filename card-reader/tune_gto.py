"""advisor.HU_GTO 를 GTO 솔버 데이터셋의 학습(train) 분할에서만 좌표 하강(그리드)으로 맞추는 스크립트.

  python tune_gto.py range              # 범위 에쿼티 열 (ip/oop 위치 열 포함)
  python tune_gto.py random             # 랜덤 에쿼티 열
  python tune_gto.py unk_range          # 위치·주도권 모름('unk') 열만, hero_ip/aggressor/포지션 없는 ctx 로
  python tune_gto.py unk_random
  옵션: --n 60000 (학습 표본) --passes 2 --procs 11 --start table.json (이어서) --out tuned_<mode>.json

목표 지표는 bench_gto 의 share (솔버가 그 노드에서 추천 범주를 고를 확률), 한 번에 키 하나씩 후보값을 모두 시험해
+0.0002 이상 오를 때만 바꾼다.  결과 표(JSON)를 advisor.HU_GTO 로 옮기고 bench_gto.py 로 검증(val) 점수를 다시 잰다.
속도: 드로우(outs)를 미리 계산해 ctx 에 넣고 plan 계산을 끈다 (액션은 같다) → 1회 평가 60k 가 수 초.
데이터: jevonmao/postflop-solver-reasoning-traces-1m (CC-BY-4.0).
"""
import argparse
import copy
import json
import os
import pickle
import sys
import time
from multiprocessing import Pool

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import advisor as A  # noqa: E402
import bench_gto as b  # noqa: E402
import evaluator as ev  # noqa: E402

STS = ("flop", "turn", "river")
S = None


def prepare(n, seed=b.SEED):
    """학습 분할 n 개 + 랜덤 에쿼티 + 미리 계산한 outs → 피클 캐시 경로."""
    path = os.path.join(os.path.dirname(b.DATA), f"train_{n}_{seed}.pkl")
    if os.path.exists(path):
        return path
    spots = b.load_split("train", n, seed)
    b.random_equity(spots)

    class _C:
        outs = None

    cache = {}
    for s in spots:
        if len(s["ctx"]["board"]) == 5:
            continue
        if s["key"] not in cache:
            c = _C()
            c.hero = [ev.parse_card(x) for x in s["ctx"]["hero"]]
            c.board = [ev.parse_card(x) for x in s["ctx"]["board"]]
            cache[s["key"]] = A._outs(c)
        s["ctx"]["outs"] = cache[s["key"]]
    with open(path, "wb") as f:
        pickle.dump(spots, f)
    return path


def _init(path):
    global S
    with open(path, "rb") as f:
        S = pickle.load(f)
    A._postflop_plan = lambda c, r, pl: []   # plan 은 액션에 영향 없음 — 속도


def _work(args):
    table, mode, minimal, lo, hi = args
    A.HU_GTO = table
    out = [0.0] * 6
    for s in S[lo:hi]:
        r = A.recommend(b.make_ctx(s, mode, minimal))
        p = b.project(b.ADV_CAT[r["action"]], s["legal"])
        sh = b.share(p, s)
        low = s["spr"] < 4
        out[0] += sh
        out[1] += p == s["gold"]
        out[2] += 1
        out[3] += sh * low
        out[4] += (p == s["gold"]) * low
        out[5] += low
    return out


class Tuner:
    def __init__(self, path, n, procs):
        self.n, self.procs = n, procs
        self.pool = Pool(procs, initializer=_init, initargs=(path,))

    def score(self, table, mode, minimal=False):
        step = (self.n + self.procs * 3 - 1) // (self.procs * 3)
        jobs = [(table, mode, minimal, i, min(self.n, i + step)) for i in range(0, self.n, step)]
        tot = [0.0] * 6
        for o in self.pool.imap_unordered(_work, jobs):
            tot = [x + y for x, y in zip(tot, o)]
        return {"share": tot[0] / tot[2], "agree": tot[1] / tot[2], "low_share": tot[3] / max(1, tot[5]),
                "low_agree": tot[4] / max(1, tot[5])}


def getp(t, path):
    for k in path:
        t = t[k]
    return t


def setp(table, path, v):
    t = copy.deepcopy(table)
    cur = t
    for k in path[:-2]:
        cur = cur[k]
    parent = cur[path[-2]]
    if isinstance(parent, tuple):
        lst = list(parent)
        lst[path[-1]] = v
        cur[path[-2]] = tuple(lst)
    else:
        parent[path[-1]] = v
    return t


def grid(basis, nodes):
    rng = basis == "range"
    G = [((basis, "pen", st), [0.0, 0.03, 0.06, 0.1, 0.15, 0.2] if rng else [0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8]) for st in STS]
    G += [((basis, "call", st), [round(-0.06 + 0.02 * i, 2) for i in range(19)]) for st in STS]
    G += [((basis, "pot_slope", st), [0.0, 0.02, 0.04, 0.06, 0.08, 0.1, 0.13, 0.16, 0.2]) for st in STS]
    G.append(((basis, "pot_ref"), [5.0, 7.0, 10.0, 15.0, 20.0, 30.0]))
    G.append(((basis, "call_slope"), [-0.15, -0.1, -0.05, 0.0, 0.05, 0.1, 0.15]))
    G += [((basis, "allin_call", st), [-0.1, -0.08, -0.05, -0.02, 0.0, 0.02, 0.05, 0.08, 0.12, 0.16, 0.2]) for st in STS]
    G += [((basis, "allin_eq", st), [0.0, 0.3, 0.5, 0.6, 0.7, 0.8, 0.85, 0.9, 0.93, 0.96]) for st in STS]
    G += [((basis, "raise_edge", st), [0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.35, 0.4, 0.45, 0.5, 0.6, 2.0]) for st in STS]
    G.append(((basis, "raise_cls"), [("monster", "strong", "medium", "weak", "air"), ("monster", "strong", "medium"),
                                     ("monster", "strong"), ("monster",)]))
    G.append(((basis, "jam_spr"), [0.0, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0]))
    G += [((basis, "jam_edge", st), [-0.02, 0.0, 0.03, 0.06, 0.1, 0.15, 0.2, 0.3]) for st in STS]
    G += [((basis, "bet", nd, st), [0.4, 0.5, 0.55, 0.6, 0.65, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95, 1.01])
          for nd in nodes for st in STS]
    for st in STS:
        G.append(((basis, "agg", st, 0), [0.0, 0.05, 0.1, 0.15, 0.2, 0.3, 0.4]))
        G.append(((basis, "agg", st, 1), [0.0, 0.05, 0.1, 0.15, 0.2, 0.3]))
    G.append(((basis, "cbet"), [0.0, 0.2, 0.3, 0.4, 0.5, 0.6, 1.01]))
    for nd in nodes:
        G.append(((basis, "semi", nd), [True, False]))
        G.append(((basis, "bluff", nd), [-1.0, 0.03, 0.08, 0.12, 0.18, 0.25]))
        G.append(((basis, "commit_spr", nd), [0.0, 1.0, 2.0, 3.0, 4.0, 6.0]))
    G.append(((basis, "commit_eq"), [0.4, 0.5, 0.6, 0.7, 0.8, 0.9]))
    G.append(((basis, "implied"), [0.0, 0.1, 0.25, 0.5, 0.75, 1.0]))
    G.append(((basis, "implied_outs"), [4, 6, 8, 9, 12, 15]))
    return G


def descend(tuner, table, G, mode, minimal, passes, out):
    best = tuner.score(table, mode, minimal)
    print("start", mode, "minimal" if minimal else "", best, flush=True)
    for p in range(passes):
        changed = False
        for path, vals in G:
            cur = getp(table, path)
            res = {v: tuner.score(setp(table, path, v), mode, minimal) for v in vals if v != cur}
            if not res:
                continue
            v_best = max(res, key=lambda v: res[v]["share"])
            if res[v_best]["share"] > best["share"] + 2e-4:
                table = setp(table, path, v_best)
                print(f"  {path} {cur} -> {v_best}  share {best['share']:.4f} -> {res[v_best]['share']:.4f} "
                      f"(agree {res[v_best]['agree']:.4f}, spr<4 share {res[v_best]['low_share']:.4f})", flush=True)
                best, changed = res[v_best], True
                with open(out, "w", encoding="utf-8") as f:
                    json.dump(table, f, indent=1, default=list)
        print("pass", p, best, flush=True)
        if not changed:
            break
    return table, best


def load_table(path, table):
    with open(path, encoding="utf-8") as f:
        loaded = json.load(f)
    for bk, sub in loaded.items():
        for k, v in sub.items():
            if k == "raise_cls":
                v = tuple(v)
            elif k == "agg":
                v = {s: tuple(x) for s, x in v.items()}
            table[bk][k] = v
    return table


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=("range", "random", "unk_range", "unk_random"))
    ap.add_argument("--n", type=int, default=60000)
    ap.add_argument("--passes", type=int, default=2)
    ap.add_argument("--procs", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    ap.add_argument("--start")
    ap.add_argument("--out")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    table = copy.deepcopy(A.HU_GTO)
    if args.start:
        table = load_table(args.start, table)
    out = args.out or f"tuned_{args.mode}.json"
    tuner = Tuner(prepare(args.n), args.n, args.procs)
    t = time.time()
    if args.mode in ("range", "random"):
        table, best = descend(tuner, table, grid(args.mode, ("ip", "oop")), args.mode, False, args.passes, out)
    else:
        basis = args.mode.split("_")[1]
        G = [g for g in grid(basis, ("unk",)) if "unk" in g[0]]
        table, best = descend(tuner, table, G, basis, True, args.passes, out)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(table, f, indent=1, default=list)
    print(f"done {time.time() - t:.0f}s", best, "→", out)


if __name__ == "__main__":
    main()
