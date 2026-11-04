"""추천 비교 — 실제 결정 기록을 두 설정으로 재생해 '추천이 바뀐 건수'를 센다.

    python compare_advice.py <데이터 폴더> [--a mult_set=current] [--b mult_set=prev] [--iters 3000] [--json 경로]
    python compare_advice.py <데이터 폴더> --a-rev a6cc113 --b-rev d88aeb4 --opponents voluntary   (커밋 대 커밋)

  데이터 폴더: 위치 인자 또는 환경변수 CARD_READER_DATA (저장소 안 기본 경로는 없다 — 없으면 오류로 끝낸다).
               폴더(또는 그 아래 hands/)에 decisions_YYYY-MM-DD.jsonl 과 YYYY-MM-DD.jsonl(핸드 기록)이 있어야 한다.
  설정 A/B: handread 스위치 (mult_set=current|prev, pre_floor=숫자, style_strength=숫자) 를 쉼표로 이어 쓴다.
            기본은 A = mult_set=current, B = mult_set=prev.
  재생: 결정 기록(decisionlog)에서 결정 순간의 ctx 를 복원하고, 핸드 기록의 액션을 그 순간까지만 잘라
        handlog.to_handread → handread.estimate 로 상대 범위를 설정마다 다시 만든다. 결정 기록의 equity_basis 가 'range' 였으면
        같은 시드로 범위 에쿼티를 다시 재서 advisor.recommend(ctx) 를 부른다 (두 설정의 차이는 오직 스위치).
        복원할 수 없는 결정(카드·핸드 기록·에쿼티 없음 등)은 건너뛰고 이유별로 센다.
  커밋 대 커밋: --a-rev / --b-rev REV 를 주면 그 쪽은 git 의 REV 시점 advisor.py 로 추천한다 (임시 모듈로 불러옴; preflop·rangeq 등
        다른 모듈은 지금 작업 트리 것을 쓴다).  한쪽만 주면 다른 쪽은 지금 advisor.  ctx·에쿼티 시드는 두 쪽이 똑같다.
  상대 수 규칙: --opponents logged(기본) = 기록된 상대 수.  voluntary = 프리플랍 상대 수를 reader.equity_opponents (돈 넣은 사람
        + 안 열린/림프 팟의 BB) 로 결정 기록의 좌석별 액션에서 다시 세고, 그 좌석 레인지를 먼저 써서 에쿼티를 다시 재며
        (모자라면 랜덤 핸드), reader 처럼 opponents_basis='voluntary' 를 넣는다 (예전 규칙으로 기록된 결정을 새 규칙으로 재생).
        폴드 말풍선·차례 고리는 기록에 없어 '못 읽은 폴드' 규칙은 재생하지 않는다.
  출력: 총 결정 수, 재생 성공, 건너뜀(이유별), 바뀐 추천 수, 바뀐 것의 (스트리트, 이전 행동 → 새 행동) 건수 표.
        닉네임·pid·카드·기록 원문은 출력하지 않는다 (--json 도 같은 요약만).
"""
import argparse
import importlib.util
import json
import os
import random
import re
import subprocess
import sys
import tempfile
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))

STREETS = ("preflop", "flop", "turn", "river")
SWITCH_KEYS = {"mult_set": str, "pre_floor": float, "style_strength": float}
CTX_COPY = ("players", "hero_pos", "villain_pos", "situation", "hero_ip", "aggressor", "flop_line", "villain_raised",
            "ref3_villain_pos", "pre_raises", "stack_bb", "my_stack", "pot", "pot_total", "call", "can_check",
            "facing_allin", "ante", "outs", "hand_name", "looseness")
OPP_RULES = ("logged", "voluntary")
SKIP_REASONS = ("no_hero_cards", "no_advice", "no_equity", "no_hand_record", "no_seats", "no_opponents", "replay_error")


def parse_cfg(text):
    """'mult_set=prev,pre_floor=0' -> {'mult_set': 'prev', 'pre_floor': 0.0}. 잘못되면 ValueError."""
    out = {}
    for part in (text or "").split(","):
        part = part.strip()
        if not part:
            continue
        k, _, v = part.partition("=")
        k = k.strip()
        if k not in SWITCH_KEYS or not v.strip():
            raise ValueError(f"알 수 없는 설정: {k!r} (가능: {', '.join(sorted(SWITCH_KEYS))})")
        out[k] = SWITCH_KEYS[k](v.strip())
    return out


def data_dir(arg=None):
    d = arg or os.environ.get("CARD_READER_DATA")
    if not d:
        raise SystemExit("오류: 데이터 폴더를 위치 인자 또는 환경변수 CARD_READER_DATA 로 주세요 (저장소 안 기본 경로는 없습니다).")
    if not os.path.isdir(d):
        raise SystemExit("오류: 데이터 폴더가 없습니다.")
    return d


def _read_jsonl(path):
    out = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for ln in f:
                ln = ln.strip()
                if ln:
                    try:
                        x = json.loads(ln)
                    except ValueError:
                        continue
                    if isinstance(x, dict):
                        out.append(x)
    except OSError:
        pass
    return out


def load(folder):
    """(핸드 기록 {id: rec}, 결정 기록 리스트). folder 와 folder/hands 를 모두 본다."""
    hands, decs = {}, []
    dirs = [folder] + ([os.path.join(folder, "hands")] if os.path.isdir(os.path.join(folder, "hands")) else [])
    for d in dirs:
        for name in sorted(os.listdir(d)):
            p = os.path.join(d, name)
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}\.jsonl", name):
                for r in _read_jsonl(p):
                    if r.get("id") is not None and r.get("actions") is not None and r.get("seats"):
                        hands[r["id"]] = r
            elif re.fullmatch(r"decisions_\d{4}-\d{2}-\d{2}\.jsonl", name):
                decs.extend(r for r in _read_jsonl(p) if r.get("type") == "decision")
    decs.sort(key=lambda d: (d.get("t") or 0, str(d.get("id"))))
    return hands, decs


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None


def truncate_actions(actions, dec_seats):
    """핸드 기록의 액션을 결정 순간까지만: 좌석·스트리트마다 결정 기록(seats[k].acts)에 있는 개수만큼 앞에서부터."""
    allow = {}
    for k, s in (dec_seats or {}).items():
        for st, rows in ((s or {}).get("acts") or {}).items():
            allow[(k, st)] = len(rows or [])
    used, out = Counter(), {}
    for st in STREETS:
        rows = []
        for a in (actions or {}).get(st) or []:
            key = (a.get("seat"), st)
            if used[key] < allow.get(key, 0):
                used[key] += 1
                rows.append(a)
        out[st] = rows
    return out


def live_opponents(actions, hero_key="hero"):
    """잘린 액션에서 폴드하지 않고 행동한 상대 좌석, 공격 횟수 많은 순 (reader.make_reads 의 주 상대 먼저와 비슷)."""
    folded, aggr, seen = set(), Counter(), set()
    for st in STREETS:
        for a in actions.get(st) or []:
            k = a.get("seat")
            if k is None or k == hero_key:
                continue
            seen.add(k)
            if a.get("act") == "fold":
                folded.add(k)
            elif a.get("act") in ("bet", "raise"):
                aggr[k] += 1
    return sorted((k for k in seen if k not in folded), key=lambda k: (-aggr[k], str(k)))


def restore_ctx(d):
    """결정 기록 -> advisor ctx (equity 는 기록값; 범위 에쿼티는 replay 에서 다시 잼). 복원 불가면 (None, 이유)."""
    hero = [c for c in (d.get("hero") or []) if isinstance(c, str)]
    if len(hero) != 2:
        return None, "no_hero_cards"
    if not (d.get("advice") or {}).get("action"):
        return None, "no_advice"
    if _num(d.get("equity")) is None:
        return None, "no_equity"
    board = [c for c in (d.get("board") or []) if isinstance(c, str)]
    ctx = {"hero": hero, "board": board, "street": d.get("street") or "preflop",
           "opponents_in_hand": d.get("opponents"), "equity": float(d["equity"])}
    for k in CTX_COPY:
        if d.get(k) is not None:
            ctx[k] = d[k]
    if d.get("equity_basis"):
        ctx["equity_basis"] = d["equity_basis"]
    if not board:   # reader 처럼 프리플랍엔 이미 낸 내 칩 (블라인드·림프) — 내 좌석 기록의 이번 스트리트 금액
        hero_seat = (d.get("seats") or {}).get("hero")
        if isinstance(hero_seat, dict):
            ctx["hero_bet"] = _street_bet(hero_seat, ctx["street"])
    return ctx, None


def _street_bet(seat, street):
    """좌석 기록(acts)에서 그 스트리트에 낸 칩 (BB) = 블라인드·콜·레이즈·벳 금액 중 가장 큰 값 (없으면 0)."""
    bet = 0.0
    for r in ((seat.get("acts") or {}).get(street) or []):
        to = _num(r.get("to"))
        if to is not None and r.get("act") in ("post", "call", "raise", "bet"):
            bet = max(bet, to)
    return round(bet, 2)


def table_state_from(d):
    """결정 기록의 좌석별 액션 -> reader.equity_opponents 가 읽는 테이블 상태 모양 (바닥 칩 = 이번 스트리트에 낸 금액)."""
    street = d.get("street") or "preflop"
    seats, blinds, raises = [], {}, 0
    for k, s in (d.get("seats") or {}).items():
        if not isinstance(s, dict):
            continue
        rows = (s.get("acts") or {}).get(street) or []
        raises += sum(1 for r in rows if r.get("act") == "raise")
        key = "hero" if s.get("hero") else k
        if s.get("pos") in ("SB", "BB"):
            blinds[s["pos"].lower()] = key
        seats.append({"key": key, "pos": s.get("pos"), "in_hand": not s.get("folded"), "bet": _street_bet(s, street),
                      "allin": any(r.get("allin") for r in rows)})
    return {"seats": seats, "blind_seats": blinds, "preflop": {"raises": raises}, "acting_seat": "hero"}


def voluntary_opponents(d, equity_opponents=None):
    """프리플랍 결정의 에쿼티 상대 (reader.equity_opponents) -> (n, keys, basis) 또는 None (포스트플랍 / 정할 수 없음 → 기록값).
    n 은 reader 처럼 1~9 와 '핸드에 남은 상대 수' 로 자르고, basis 는 자르지 않은 값과 같을 때만 'voluntary' (reader 와 같은 조건)."""
    if (d.get("street") or "preflop") != "preflop" or d.get("board"):
        return None
    if equity_opponents is None:
        from reader import equity_opponents
    ts = table_state_from(d)
    info = equity_opponents(ts, 0)
    if not info:
        return None
    n = max(1, min(9, int(info["n"])))
    cap = sum(1 for s in ts["seats"] if s["in_hand"] and s["key"] != "hero")   # reader.max_opponents
    if cap:
        n = min(n, cap)
    return n, list(info["keys"]), ("voluntary" if n == info["n"] else None)


def load_advisor(rev, path="advisor.py"):
    """git REV 시점의 advisor.py 를 임시 모듈로 불러 recommend 를 돌려준다 (다른 모듈은 지금 작업 트리 것)."""
    try:
        src = subprocess.run(["git", "show", f"{rev}:./{path}"], cwd=HERE, capture_output=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError) as e:
        raise SystemExit(f"오류: {rev} 의 {path} 를 꺼낼 수 없습니다 ({e}).")
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    name = "advisor_rev_" + re.sub(r"\W", "_", rev)
    p = os.path.join(tempfile.mkdtemp(prefix="cmp-adv-rev-"), name + ".py")
    with open(p, "wb") as f:
        f.write(src)
    spec = importlib.util.spec_from_file_location(name, p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.recommend


def _profiles(d, keys):
    out = {}
    for k in keys:
        p = ((d.get("seats") or {}).get(k) or {}).get("profile")
        if isinstance(p, dict):
            out[k] = dict(p)
            t = ((d.get("seats") or {}).get(k) or {}).get("type")
            if t and not out[k].get("style"):
                out[k]["style"] = t
    return out


def build_inputs(d, rec, opp=None):
    """결정 d + 핸드 기록 rec -> (seats(estimate 입력), order, hero, board, profiles). 복원 불가면 (None, 이유).
    opp: (n, keys) 이면 상대 수 n, keys 좌석의 레인지를 먼저 (reader.range_analyze 순서)."""
    import handlog
    if not rec:
        return None, "no_hand_record"
    if not d.get("seats"):
        return None, "no_seats"
    acts = truncate_actions(rec.get("actions"), d["seats"])
    pos = {k: s.get("pos") for k, s in (rec.get("seats") or {}).items()}
    live = handlog.to_handread(acts, pos)
    order = [k for k in live_opponents(acts) if k in live]
    n = d.get("opponents")
    if opp is not None:
        n, keys = opp
        order = [k for k in order if k in keys] + [k for k in order if k not in keys]
    if isinstance(n, (int, float)) and n >= 1:
        order = order[:int(n)]
    if not order:
        return None, "no_opponents"
    return ({k: live[k] for k in order}, order, _profiles(d, order)), None


def estimate_ranges(d, seats, order, profiles, cfg, hr):
    ante = (_num(d.get("ante")) or 0) > 0
    players = int(_num(d.get("players")) or 9)
    stack = _num(d.get("stack_bb")) or 100
    seats = {k: (dict(v, profile=profiles[k]) if k in profiles else v) for k, v in seats.items()}
    with hr.switches(**cfg):
        est = hr.estimate(seats, d.get("board") or [], players, dead=(d.get("hero") or []) + (d.get("board") or []),
                          profiles=profiles, stack_bb=stack, ante=ante)
    return {k: est[k]["range"] for k in order if k in est}


def advise_with(d, ctx0, seats, order, profiles, cfg, recommend, hr, rq, iters, seed, pad=None):
    """한 설정으로 ctx 를 완성(상대 범위 + 범위 에쿼티)하고 추천 action 을 돌려준다.
    pad: 상대 수를 다시 셌을 때 그 수 — 레인지가 모자라면 랜덤 핸드로 채우고 (reader.range_analyze), 랜덤 기준 결정도 이 수로 다시 잰다."""
    ctx = dict(ctx0)
    if d.get("equity_basis") == "range":
        ranges = estimate_ranges(d, seats, order, profiles, cfg, hr)
        if ranges:
            rl = [ranges[k] for k in order if k in ranges]
            if pad:
                rl = rl[:pad] + [rq.random_range() for _ in range(pad - len(rl))]
            eq = rq.equity_vs_ranges(ctx["hero"], ctx["board"], rl, iters, random.Random(seed), cache=False)
            ctx["equity"] = eq["equity"]
            ctx["villain_ranges"] = rl
            ctx["villain_profiles"] = [profiles[k] for k in order if k in profiles]
    elif pad:
        eq = rq.equity_vs_ranges(ctx["hero"], ctx["board"], [rq.random_range() for _ in range(pad)], iters,
                                 random.Random(seed), cache=False)
        ctx["equity"] = eq["equity"]
    adv = recommend(ctx)
    a = adv.get("action") if isinstance(adv, dict) else None
    return a


def replay(decs, hands, cfg_a, cfg_b, recommend, iters=3000, seed=7, hr=None, rq=None, recommend_b=None,
           opponents="logged", equity_opponents=None):
    """-> [{i, street, a, b}] (재생 성공한 결정마다 하나, i = decs 안 번호) 와 건너뜀 Counter. 기록 원문은 돌려주지 않는다.
    recommend_b: B 쪽 recommend (없으면 recommend).  opponents: 'logged' / 'voluntary' (모듈 설명) — 두 쪽에 똑같이."""
    if hr is None:
        import handread as hr
    if rq is None:
        import rangeq as rq
    recommend_b = recommend_b or recommend
    out, skipped = [], Counter()
    for i, d in enumerate(decs):
        ctx0, why = restore_ctx(d)
        if ctx0 is None:
            skipped[why] += 1
            continue
        try:
            vol = voluntary_opponents(d, equity_opponents) if opponents == "voluntary" else None
            pad, opp = None, None
            if vol is not None:
                pad, keys, basis = vol
                opp = (pad, keys)
                ctx0["opponents_in_hand"] = pad
                if basis:
                    ctx0["opponents_basis"] = basis
            inp, why = build_inputs(d, hands.get(d.get("hand_id")), opp)
            if inp is None:
                skipped[why] += 1
                continue
            seats, order, profiles = inp
            a = advise_with(d, ctx0, seats, order, profiles, cfg_a, recommend, hr, rq, iters, seed, pad)
            b = advise_with(d, ctx0, seats, order, profiles, cfg_b, recommend_b, hr, rq, iters, seed, pad)
        except Exception:   # 한 건 때문에 전체가 죽지 않게 (내용은 남기지 않고 건수만)
            skipped["replay_error"] += 1
            continue
        if a is None or b is None:
            skipped["no_advice"] += 1
            continue
        out.append({"i": i, "street": d.get("street") or "preflop", "a": a, "b": b})
    return out, skipped


def summarize(results, total, skipped):
    """집계만: 총 결정 수, 재생 성공, 건너뜀(이유별), 바뀐 수, (스트리트, 이전 → 새) 건수."""
    changes = Counter((r["street"], r["a"], r["b"]) for r in results if r["a"] != r["b"])
    sk = {k: v for k, v in skipped.items() if v}
    return {"total": total, "replayed": len(results), "skipped": sum(sk.values()), "skipped_by_reason": dict(sorted(sk.items())),
            "changed": sum(changes.values()),
            "changes": [{"street": s, "from": a, "to": b, "count": n}
                        for (s, a, b), n in sorted(changes.items(), key=lambda x: (STREETS.index(x[0][0]) if x[0][0] in STREETS else 9,
                                                                                     -x[1], x[0][1], x[0][2]))]}


def format_report(s, cfg_a, cfg_b):
    lines = [f"추천 비교  A={cfg_a or '(기본)'}  B={cfg_b or '(기본)'}",
             f"총 결정 {s['total']}  재생 성공 {s['replayed']}  건너뜀 {s['skipped']}  추천이 바뀐 것 {s['changed']}"]
    if s["skipped_by_reason"]:
        lines.append("건너뜀 이유: " + ", ".join(f"{k} {v}" for k, v in s["skipped_by_reason"].items()))
    if s["changes"]:
        lines.append("")
        lines.append(f"{'스트리트':<10}{'A 행동':<10}{'':<4}{'B 행동':<10}{'건수':>6}")
        for c in s["changes"]:
            lines.append(f"{c['street']:<10}{c['from']:<10}{'->':<4}{c['to']:<10}{c['count']:>6}")
    else:
        lines.append("바뀐 추천 없음")
    return "\n".join(lines)


def main(argv=None, recommend=None):
    ap = argparse.ArgumentParser(description="두 설정으로 추천 재생 비교 (요약만 출력)")
    ap.add_argument("folder", nargs="?", help="데이터 폴더 (또는 환경변수 CARD_READER_DATA)")
    ap.add_argument("--a", help="설정 A (기본 mult_set=current)")
    ap.add_argument("--b", help="설정 B (기본 mult_set=prev, --a-rev/--b-rev 를 주면 A 와 같은 설정)")
    ap.add_argument("--a-rev", help="A 쪽 advisor.py 를 이 git 커밋 시점 것으로 (예: a6cc113)")
    ap.add_argument("--b-rev", help="B 쪽 advisor.py 를 이 git 커밋 시점 것으로 (예: d88aeb4)")
    ap.add_argument("--opponents", choices=OPP_RULES, default="logged",
                    help="프리플랍 상대 수: logged(기록값, 기본) / voluntary(reader.equity_opponents 로 다시 셈, 두 쪽 같게)")
    ap.add_argument("--iters", type=int, default=3000, help="범위 에쿼티 표본 수 (기본 3000)")
    ap.add_argument("--json", help="요약 JSON 저장 경로")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    folder = data_dir(args.folder)
    if args.a is None:
        args.a = "mult_set=current"
    if args.b is None:   # 커밋 비교면 스위치는 두 쪽 같게 (차이는 advisor 커밋만)
        args.b = args.a if (args.a_rev or args.b_rev) else "mult_set=prev"
    try:
        cfg_a, cfg_b = parse_cfg(args.a), parse_cfg(args.b)
    except ValueError as e:
        raise SystemExit(f"오류: {e}")
    try:
        rec_a = load_advisor(args.a_rev) if args.a_rev else recommend
        rec_b = load_advisor(args.b_rev) if args.b_rev else recommend
        if rec_a is None or rec_b is None:
            import advisor
            rec_a, rec_b = rec_a or advisor.recommend, rec_b or advisor.recommend
    except ImportError as e:
        raise SystemExit(f"오류: advisor 를 불러올 수 없습니다 ({e.name or '의존 모듈'} 없음) — 앱 실행 환경(Windows)에서 돌리세요.")
    hands, decs = load(folder)
    results, skipped = replay(decs, hands, cfg_a, cfg_b, rec_a, iters=args.iters, recommend_b=rec_b, opponents=args.opponents)
    s = summarize(results, len(decs), skipped)
    label_a = " ".join(x for x in (args.a, args.a_rev and f"advisor@{args.a_rev}") if x)
    label_b = " ".join(x for x in (args.b, args.b_rev and f"advisor@{args.b_rev}") if x)
    if args.opponents != "logged":
        label_b += f"  (프리플랍 상대 수: {args.opponents}, 두 쪽 같음)"
    print(format_report(s, label_a, label_b))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(dict(s, a=cfg_a, b=cfg_b, a_rev=args.a_rev, b_rev=args.b_rev, opponents=args.opponents), f, ensure_ascii=False, indent=1)
        print(f"\nJSON: {args.json}")
    return s


if __name__ == "__main__":
    main()
