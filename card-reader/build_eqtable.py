"""프리플랍 에쿼티 표 만들기: 169 핸드 x 상대 1~9명 (랜덤 핸드 기준) -> eqtable/preflop_random.json

  python build_eqtable.py [--workers N] [--scale X] [--status]

- 작업 단위 = (핸드, 상대 수, 조각). 조각마다 시드가 고정이라 다시 돌려도 같은 결과고, 끝난 조각은
  eqtable/preflop_random.partial.jsonl 에 한 줄씩 저장 -> 중단했다가 다시 실행하면 이어서 한다 (resumable).
- 기본 workers = max(1, CPU - 4) 이고 프로세스 우선순위는 BELOW_NORMAL (게임/다른 작업을 방해하지 않게).
- 셀당 표본: 상대 1명 1,000,000 / 2명 600,000 / 3~9명 400,000  (표준오차 <= 0.05% / 0.06% / 0.08%).
  --scale 로 전체 배수 조정 (예: 0.2 로 빠른 시험).
- 모든 조각이 끝나면 JSON 을 쓴다: {"v":1,"hands":{"AA":[[equity,win,tie,lose,samples] x 9], ...}}
"""
import argparse
import json
import multiprocessing as mp
import os
import random
import sys
import time
import zlib
from pathlib import Path

import eqtable
import evaluator as ev

OUT_DIR = eqtable.DIR
TABLE_PATH = OUT_DIR / eqtable.TABLE_NAME
PARTIAL_PATH = OUT_DIR / "preflop_random.partial.jsonl"
CHUNK = 100_000
CELL_SAMPLES = {1: 1_000_000, 2: 600_000}   # 나머지(3~9명)는 400,000


def cell_chunks(k, scale=1.0):
    total = max(CHUNK // 10, int(CELL_SAMPLES.get(k, 400_000) * scale))
    full, rest = divmod(total, CHUNK)
    sizes = [CHUNK] * full + ([rest] if rest else [])
    return sizes


def set_low_priority():
    """BELOW_NORMAL_PRIORITY_CLASS (Windows) / nice (그 외)."""
    try:
        if os.name == "nt":
            import ctypes
            ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x00004000)
        else:
            os.nice(10)
    except Exception:
        pass


def simulate_chunk(task):
    """task = (code, k, chunk_idx, n) -> (code, k, chunk_idx, n, win, tie, lose, share)"""
    code, k, idx, n = task
    hero = eqtable.code_cards(code)
    rng = random.Random(zlib.crc32(f"{code}-{k}-{idx}".encode()))
    deck = [c for c in range(52) if c not in hero]
    sample = rng.sample
    evalf = ev.evaluate_fast
    need = 5 + 2 * k
    win = tie = lose = 0
    share = 0.0
    for _ in range(n):
        d = sample(deck, need)
        board = d[:5]
        hs = evalf(hero + board)
        best = -1
        cnt = 0
        beaten = False
        # 상대 점수 중 최고와 그 개수
        bs = None
        for i in range(k):
            o = d[5 + 2 * i], d[6 + 2 * i]
            s = evalf([o[0], o[1]] + board)
            if bs is None or s > bs:
                bs, cnt = s, 1
            elif s == bs:
                cnt += 1
        if hs > bs:
            win += 1
            share += 1.0
        elif hs < bs:
            lose += 1
        else:
            tie += 1
            share += 1.0 / (1 + cnt)
    return code, k, idx, n, win, tie, lose, share


def load_partial():
    done = {}
    if PARTIAL_PATH.exists():
        for line in PARTIAL_PATH.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
                done[(r[0], r[1], r[2])] = r
            except (ValueError, IndexError):
                continue   # 잘린 마지막 줄
    return done


def write_table(done, scale):
    hands = {}
    for code in eqtable.all_codes():
        rows = []
        for k in range(1, eqtable.MAX_OPP + 1):
            tot = win = tie = lose = 0
            share = 0.0
            for idx, n in enumerate(cell_chunks(k, scale)):
                r = done[(code, k, idx)]
                tot += r[3]
                win += r[4]
                tie += r[5]
                lose += r[6]
                share += r[7]
            rows.append([round(share / tot, 6), round(win / tot, 6), round(tie / tot, 6), round(lose / tot, 6), tot])
        hands[code] = rows
    tmp = TABLE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps({"v": 1, "hands": hands}, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, TABLE_PATH)
    return TABLE_PATH.stat().st_size


def fmt(sec):
    sec = int(sec)
    return f"{sec // 3600}h{sec % 3600 // 60:02d}m{sec % 60:02d}s" if sec >= 3600 else f"{sec // 60}m{sec % 60:02d}s"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 4) - 4))
    ap.add_argument("--scale", type=float, default=1.0)
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    set_low_priority()

    tasks = [(code, k, idx, n) for k in range(1, eqtable.MAX_OPP + 1) for code in eqtable.all_codes()
             for idx, n in enumerate(cell_chunks(k, args.scale))]
    cost = lambda t: t[3] * (t[1] + 1.5)
    done = load_partial()
    todo = [t for t in tasks if (t[0], t[1], t[2]) not in done]
    total_cost = sum(cost(t) for t in tasks)
    left_cost = sum(cost(t) for t in todo)
    print(f"조각 {len(tasks)}개 중 {len(tasks) - len(todo)}개 완료, 남은 {len(todo)}개, workers={args.workers}", flush=True)
    if args.status or not todo:
        if not todo:
            size = write_table(done, args.scale)
            print(f"완료: {TABLE_PATH} ({size} bytes)")
        return
    # 비싼 것(상대 많은 셀)부터 -> 마지막에 꼬리가 짧다
    todo.sort(key=cost, reverse=True)
    t0 = time.time()
    done_cost = 0.0
    last_print = 0.0
    with mp.Pool(args.workers, initializer=set_low_priority) as pool, open(PARTIAL_PATH, "a", encoding="utf-8") as fh:
        for r in pool.imap_unordered(simulate_chunk, todo, chunksize=1):
            fh.write(json.dumps(list(r)) + "\n")
            fh.flush()
            done[(r[0], r[1], r[2])] = list(r)
            done_cost += r[3] * (r[1] + 1.5)
            now = time.time()
            if now - last_print >= 20:
                last_print = now
                el = now - t0
                eta = el * (left_cost - done_cost) / done_cost if done_cost else 0
                pct = (total_cost - left_cost + done_cost) / total_cost * 100
                print(f"{pct:5.1f}%  경과 {fmt(el)}  ETA {fmt(eta)}", flush=True)
    size = write_table(done, args.scale)
    print(f"완료: {TABLE_PATH} ({size} bytes), 총 {fmt(time.time() - t0)}", flush=True)


if __name__ == "__main__":
    mp.freeze_support()
    main()
