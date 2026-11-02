"""GTO 결과 폴더를 믿지 않고 검사한다. 파일을 고치지 않고 JSON 으로만 읽는다.

    python3 -I review_app.py check-gto <gto-data 폴더>

보는 것: 압축 폭탄(압축 해제 크기 제한), 구조·값 범위, 전략 문자열 길이·글자, 빈도 합,
레인지 합계(agg)와 콤보별 전략의 일치, 압축 원본(.compact.json)과 라이브러리의 일치, 실행 기록과 개수·수치의 일치.
"""
import glob
import gzip
import json
import math
import os
import re
import sys

import evaluator as ev
import solverlib as sl

MAX_UNPACKED = 256 * 1024 * 1024
FLOP_RE = re.compile(r"^([2-9TJQKA][shdc]){3}$")
ACT_RE = re.compile(r"^(check|call|fold|raise|allin|bet\d{1,3})$")
NODE_RE = re.compile(r"^(oop_first|ip_vs_check|(oop|ip)_vs_(bet|raise):(bet\d{1,3}|allin))$")
LINES = {"x_x", "x_bet33_c", "x_bet75_c", "bet33_c", "bet75_c"}


def read_gz(path):
    """압축을 풀면서 크기를 재고, 한도 안일 때만 JSON 으로 읽는다."""
    buf, total = [], 0
    with gzip.open(path, "rb") as f:
        while True:
            chunk = f.read(1 << 20)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_UNPACKED:
                raise ValueError("unpacked size over limit")
            buf.append(chunk)
    return json.loads(b"".join(buf)), total


def check_node(name, nd, board, errs, where):
    if not isinstance(nd, dict) or not NODE_RE.match(name):
        errs.append(f"{where}: bad node {str(name)[:40]}")
        return False
    acts, s = nd.get("a"), nd.get("s")
    if not (isinstance(acts, list) and acts and all(isinstance(a, str) and ACT_RE.match(a) for a in acts)):
        errs.append(f"{where}/{name}: bad actions")
        return False
    if not isinstance(s, str) or (s.strip(sl.ALPH + sl.MISSING)):
        errs.append(f"{where}/{name}: bad strategy chars")
        return False
    n = len(sl.combo_order(board)[1])
    w = len(acts) - 1 if len(acts) > 1 else 0
    if len(s) != n * w:
        errs.append(f"{where}/{name}: strategy length {len(s)} != {n}*{w}")
        return False
    agg, amt = nd.get("agg"), nd.get("amt")
    if not (isinstance(agg, list) and len(agg) == len(acts)
            and all(isinstance(x, (int, float)) and not isinstance(x, bool) and -1e-6 <= x <= 1 + 1e-6 for x in agg)):
        errs.append(f"{where}/{name}: bad agg")
        return False
    if abs(sum(agg) - 1) > 0.02:
        errs.append(f"{where}/{name}: agg sum {sum(agg):.3f}")
        return False
    if not (isinstance(amt, list) and len(amt) == len(acts)
            and all(isinstance(x, (int, float)) and not isinstance(x, bool) and x >= 0 and math.isfinite(x) for x in amt)):
        errs.append(f"{where}/{name}: bad amounts")
        return False
    return True


def first_node_gap(lib, flop, nd, role):
    """그 노드의 첫 결정(oop_first / ip_vs_check 는 레인지가 바로 온다): 범위 가중 콤보 빈도와 agg 의 최대 차이."""
    board = sl.parse_board(flop)
    weights = lib.get("ranges", {}).get(role)
    if isinstance(weights, str):
        weights = sl.parse_ts_range(weights)
    if not isinstance(weights, dict):
        return None
    tot = [0.0] * len(nd["a"])
    wsum = 0.0
    idx, lst = sl.combo_order(board)
    for c in lst:
        w = weights.get(sl.hand_code(*c))
        if not w:
            continue
        f = sl.decode(nd, c, board)
        if f is None:
            continue
        wsum += w
        for i, x in enumerate(f):
            tot[i] += w * x
    if not wsum:
        return None
    return max(abs(t / wsum - a) for t, a in zip(tot, nd["agg"]))


def strong_hand_stat(nd, board):
    """가장 강한 5% 콤보의 평균 폴드 빈도, 가장 약한 20%의 평균 폴드 빈도 (상식 점검: 앞이 뒤보다 훨씬 낮아야 한다)."""
    if "fold" not in nd["a"]:
        return None
    i = nd["a"].index("fold")
    idx, lst = sl.combo_order(board)
    rows = []
    for c in lst:
        f = sl.decode(nd, c, board)
        if f is not None:
            rows.append((ev.evaluate(list(c) + list(board)), f[i]))
    if len(rows) < 40:
        return None
    rows.sort(key=lambda r: r[0])
    n = len(rows)
    weak = rows[:n // 5]
    strong = rows[-max(1, n // 20):]
    return sum(x for _s, x in strong) / len(strong), sum(x for _s, x in weak) / len(weak)


def run(root):
    errs, out = [], {}
    libdir = os.path.join(root, "card-reader", "gto_lib")
    solves = os.path.join(root, "preflop-tools", "solves")
    libs = sorted(glob.glob(os.path.join(libdir, "*_*bb.json.gz")))
    out["lib_files"] = len(libs)
    flops, exploits, per, gaps, sanity = 0, [], {}, [], []
    stale, range_diff, unpacked_max, compact_diff, compact_seen = 0, [], 0, 0, 0
    keyset = set()
    for p in libs:
        m = re.fullmatch(r"(.+)_(\d+)bb\.json\.gz", os.path.basename(p))
        if not m or m.group(1) not in sl.SPOTS:
            errs.append(f"unknown lib file {os.path.basename(p)[:60]}")
            continue
        spot, stack = m.group(1), int(m.group(2))
        try:
            lib, size = read_gz(p)
        except Exception as exc:
            errs.append(f"{spot}_{stack}: unreadable {type(exc).__name__}")
            continue
        unpacked_max = max(unpacked_max, size)
        if lib.get("spot") != spot or lib.get("stack") != stack:
            errs.append(f"{spot}_{stack}: header mismatch")
        info = sl.spot_info(spot, stack)
        if abs((lib.get("pot") or 0) - info["pot"]) > 1e-3 or abs((lib.get("behind") or 0) - info["behind"]) > 1e-3:
            errs.append(f"{spot}_{stack}: pot/behind differ from code")
        if lib.get("tree_hash") != sl.tree_hash():
            errs.append(f"{spot}_{stack}: tree hash {lib.get('tree_hash')} != {sl.tree_hash()}")
        try:
            cur = sl.range_hash(sl.spot_ranges(spot, stack))
            if cur != lib.get("range_hash"):
                range_diff.append(f"{spot}_{stack}")
        except Exception as exc:
            errs.append(f"{spot}_{stack}: range hash {type(exc).__name__}")
        per[f"{spot}_{stack}"] = len(lib.get("flops") or {})
        for flop, fl in (lib.get("flops") or {}).items():
            where = f"{spot}_{stack}/{str(flop)[:12]}"
            if not FLOP_RE.match(str(flop)):
                errs.append(f"{where}: bad flop name")
                continue
            board = sl.parse_board(flop)
            if len(set(board)) != 3:
                errs.append(f"{where}: duplicate cards")
                continue
            flops += 1
            keyset.add((spot, stack, flop))
            x = fl.get("exploit")
            if isinstance(x, (int, float)):
                exploits.append(x)
            if fl.get("rh") != lib.get("range_hash"):
                stale += 1
            nodes = fl.get("nodes") or {}
            if not {"oop_first", "ip_vs_check"} <= set(nodes):
                errs.append(f"{where}: first nodes missing")
            good = {n: nd for n, nd in nodes.items() if check_node(n, nd, board, errs, where)}
            if "oop_first" in good:
                g = first_node_gap(lib, flop, good["oop_first"], "oop")
                if g is not None:
                    gaps.append(g)
            for n, nd in good.items():
                if "_vs_bet" in n:
                    st = strong_hand_stat(nd, board)
                    if st:
                        sanity.append(st)
            cp = os.path.join(solves, f"{spot}_{stack}bb", f"{flop}.compact.json")
            if os.path.exists(cp) and os.path.getsize(cp) < MAX_UNPACKED:
                compact_seen += 1
                try:
                    with open(cp, encoding="utf-8") as f:
                        cj = json.load(f)
                    if {k: v.get("s") for k, v in (cj.get("nodes") or {}).items()} != {k: v.get("s") for k, v in nodes.items()}:
                        compact_diff += 1
                except Exception:
                    compact_diff += 1
    # 턴 파일
    tfiles = sorted(glob.glob(os.path.join(libdir, "turn", "*", "*.json.gz")))
    tok, tmiss, tline = 0, 0, {}
    for p in tfiles:
        d = os.path.basename(os.path.dirname(p))
        m = re.fullmatch(r"(.+)_(\d+)bb", d)
        flop = os.path.basename(p)[:-len(".json.gz")]
        where = f"turn/{d}/{flop[:12]}"
        if not m or not FLOP_RE.match(flop):
            errs.append(f"{where}: bad name")
            continue
        if (m.group(1), int(m.group(2)), flop) not in keyset:
            tmiss += 1
        try:
            td, size = read_gz(p)
        except Exception as exc:
            errs.append(f"{where}: unreadable {type(exc).__name__}")
            continue
        unpacked_max = max(unpacked_max, size)
        board = sl.parse_board(flop)
        for line, L in (td.get("lines") or {}).items():
            if line not in LINES:
                errs.append(f"{where}: bad line {str(line)[:20]}")
                continue
            tline[line] = tline.get(line, 0) + 1
            for c, nodes in (L.get("cards") or {}).items():
                try:
                    tc = sl.card(c)
                except (ValueError, TypeError):
                    errs.append(f"{where}: bad turn card")
                    continue
                if tc in board:
                    errs.append(f"{where}: turn card on board")
                    continue
                for n, nd in nodes.items():
                    if n == "oop_first" and not nd.get("s") and len(nd.get("a") or []) == 1:
                        continue
                    check_node(n, nd, board + [tc], errs, f"{where}/{line}/{c}")
        tok += 1
    out.update(flops=flops, turn_files=tok, turn_without_flop=tmiss, turn_lines=tline, per_spot=per,
               exploit_mean=round(sum(exploits) / len(exploits), 3) if exploits else None,
               exploit_max=round(max(exploits), 3) if exploits else None,
               over_target=sum(1 for x in exploits if x > sl.ACCURACY),
               flops_older_ranges=stale, libs_range_differs_from_code=range_diff, unpacked_max_mb=round(unpacked_max / 1048576, 1),
               compact_compared=compact_seen, compact_differs=compact_diff)
    if gaps:
        out["agg_gap_oop_first_max"] = round(max(gaps), 4)
        out["agg_gap_oop_first_mean"] = round(sum(gaps) / len(gaps), 4)
    if sanity:
        out["fold_strong5pct_mean"] = round(sum(a for a, _b in sanity) / len(sanity), 3)
        out["fold_weak20pct_mean"] = round(sum(b for _a, b in sanity) / len(sanity), 3)
        out["sanity_nodes"] = len(sanity)
    pj = os.path.join(solves, "progress.json")
    try:
        with open(pj, encoding="utf-8") as f:
            pr = json.load(f)
        out["progress_done_total"] = pr.get("done_total")
        if pr.get("done_total") != flops:
            errs.append(f"progress done_total {pr.get('done_total')} != flops {flops}")
    except Exception as exc:
        errs.append(f"progress.json: {type(exc).__name__}")
    out["errors"] = len(errs)
    out["error_samples"] = errs[:15]
    return out


def main(root):
    res = run(root)
    print(json.dumps(res, ensure_ascii=False, indent=1))
    return 1 if res["errors"] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
