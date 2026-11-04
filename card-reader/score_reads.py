"""상대 핸드 리딩 채점 — 쇼다운에서 공개된 상대 실제 카드로 '우리 예상이 맞았나'를 잰다.

    python score_reads.py [핸드기록 폴더] [--ok-only] [--json 경로] [--quiet]

  폴더: 인자 > 환경변수 CARD_READER_DATA > <이 파일 폴더>/dist/hands
        (YYYY-MM-DD.jsonl 핸드 기록 + decisions_YYYY-MM-DD.jsonl 결정 기록. 저장소에는 데이터를 두지 않는다)
  출력: 한국어 요약(표준출력) + JSON 요약(기본: 데이터 폴더/read_scores.json — 저장소 밖)

무엇을 재나 (공개된 상대 좌석 x 그 상대가 실제로 액션한 스트리트마다)
  범위를 다시 만든다: 기록된 액션을 handlog.to_handread → handread.estimate 로 재생 (지금 모델 기준).
    live    앱이 하는 그대로 (to_handread 는 벳 크기를 안 넘기므로 모든 벳/콜이 '보통 크기')
    fixed   입력을 고쳐 본 것: 기록된 금액('to')으로 계산한 벳 크기(팟 대비) + 프리플랍 레이즈 정리(같은 레이즈 두 번 읽음·
            최소 레이즈 미만 올인은 레이즈로 안 셈·올인에 콜 = 올인콜) + 올인 상대는 실제 올인 금액을 스택으로
            → 모델은 그대로 두고 앱이 넘기는 입력만 좋아지면 얼마나 나아지나
    pre     프리플랍 액션만 반영 (포스트플랍 배율표가 도움이 되는지 보는 기준선)
    uniform 남은 카드에서 무작위 (완전 무지 기준선)
  점수
    콤보 로그손실   -ln p(실제 콤보) (p 는 1% 를 균등과 섞어 0 방지). uniform 대비 '정보 이득(bit)' = 얼마나 좁혀 맞췄나
    PIT            범위에서 실제 콤보보다 가능성이 높은 콤보들의 확률 합(+같은 것 절반). 잘 맞으면 평균 0.5 근처,
                   낮으면 실제 패가 범위의 '핵심'에 있었음, 높으면 범위의 꼬리(과소평가)에 있었음
    분류           실제 핸드 분류(셋/투페어/탑페어+/드로우/노페어 …)에 준 확률, 예측 순위(1=최다), Brier(7분류)
    top5           실제 핸드코드가 앱이 보여주는 '가능성 높은 핸드' 5개 안에 있었나
    0확률          실제 콤보가 범위에서 아예 빠졌나 (가중치 컷)
  기록된 리딩(decisions_*.jsonl 의 reads: 결정 순간 사용자가 본 줄)도 따로 채점:
    표시된 상위 3분류 안에 실제 분류가 있었나 / 1순위였나 / 요약 표기(예: 'TT+, AJs+ 외 12%')에 실제 핸드가 들었나
    (표기는 범위의 100-12=88% 를 덮는다고 주장하므로 실제 포함률과 비교)

한계: 쇼다운까지 간 핸드만 보인다(폴드시킨 블러프는 안 보임 → 공격 액션의 '에어'는 실제보다 적게 보인다).
앤티는 금액을 몰라 팟에서 빠짐(벳 크기가 약간 크게 계산될 수 있음). 표본이 작다.
"""
import argparse
import glob
import json
import math
import os
import re
import sys
from collections import Counter, OrderedDict, defaultdict
from math import comb

import evaluator as ev
import handlog
import handread as hr

STREETS = ("preflop", "flop", "turn", "river")
STREET_KO = {"preflop": "프리플랍", "flop": "플랍", "turn": "턴", "river": "리버"}
NEED = {"preflop": 0, "flop": 3, "turn": 4, "river": 5}
EPS = 0.01                     # 범위 확률을 균등과 섞는 비율 (0 확률 → 무한 로그손실 방지)
VARIANTS = ("live", "fixed", "pre", "uniform")
POST_WORDS = ("check", "bet", "call", "raise", "fold")
FAMILY = {"check": "체크", "call": "콜", "aggr": "벳/레이즈"}
OUT_NAME = "read_scores.json"


# --- 입력 ------------------------------------------------------------------------------------
def data_dir(arg=None):
    if arg:
        return arg
    env = os.environ.get("CARD_READER_DATA")
    if env:
        return env
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "dist", "hands")


def _read_jsonl(path):
    out = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
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
    """(핸드 기록 리스트, 결정 기록 리스트)."""
    hands, decs = [], []
    for f in sorted(glob.glob(os.path.join(folder, "*.jsonl"))):
        name = os.path.basename(f)
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}\.jsonl", name):
            hands.extend(r for r in _read_jsonl(f) if r.get("actions") is not None and r.get("seats"))
        elif name.startswith("decisions_"):
            decs.extend(r for r in _read_jsonl(f) if r.get("type") == "decision")
    return hands, decs


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None


# --- 핸드 기록 → 재생 입력 ---------------------------------------------------------------------
def bet_sizes(rec):
    """{(street, 액션 인덱스): 팟 대비 크기} — 벳/레이즈 = 올린 만큼 / (콜한 뒤의 팟), 콜 = 받은 벳의 크기.
    'to' = 그 스트리트에서 그 좌석이 넣은 총액(BB). 금액을 모르면 그 액션은 크기 없음."""
    acts = rec.get("actions") or {}
    pot, out = 0.0, {}
    for st in STREETS:
        put, level, last = defaultdict(float), 0.0, None
        for i, a in enumerate(acts.get(st) or []):
            k, act, to = a.get("seat"), a.get("act"), _num(a.get("to"))
            now = pot + sum(put.values())
            if act in ("bet", "raise"):
                if to is not None and to > level:
                    base = now + max(0.0, level - put[k])
                    last = (to - level) / base if base > 0 else None
                    out[(st, i)] = last
                    put[k], level = to, to
                else:
                    out[(st, i)] = None
                    last = None
            elif act == "call":
                out[(st, i)] = last
                put[k] = max(put[k], to if to is not None else level)
            elif act == "post" and to is not None:
                put[k] = max(put[k], to)
                level = max(level, to)
        pot += sum(put.values())
    return out


def phantom_streets(rec):
    """스트리트 시작 때 액션할 수 있는 사람(폴드·올인 아님)이 2명 미만 → 그 스트리트의 '체크'는 실제 선택이 아님."""
    acts = rec.get("actions") or {}
    seats = rec.get("seats") or {}
    in_hand = {a.get("seat") for a in acts.get("preflop") or []} | {k for k, s in seats.items() if not s.get("folded")}
    folded, allin, out = set(), set(), set()
    for si, st in enumerate(STREETS):
        if si:
            for k, s in seats.items():
                f = s.get("folded")
                if f in STREETS and STREETS.index(f) < si:
                    folded.add(k)
            live = in_hand - folded - allin
            if len(live) < 2 and acts.get(st):
                out.add(st)
        for a in acts.get(st) or []:
            if a.get("act") == "fold":
                folded.add(a.get("seat"))
            if a.get("allin"):
                allin.add(a.get("seat"))
    return out


def seat_positions(rec):
    return {k: s.get("pos") for k, s in (rec.get("seats") or {}).items()}


def replay_seats(rec):
    """앱과 같은 입력 (to_handread)."""
    return handlog.to_handread(rec.get("actions") or {}, seat_positions(rec))


def clean_preflop(actions):
    """프리플랍 액션 정리 사본 + 고친 개수: (1) 같은 좌석이 다른 사람 레이즈 없이 연달아 '레이즈' = 칩 금액을 두 번 읽은 것 → 하나로
    (2) 최소 레이즈에 못 미치는 올인(예: 2.0 오픈에 2.1 올인)은 레이즈 횟수에 안 센다 → 뒤 사람의 raises_before 를 다시 센다

    (3) 올인 레이즈에 콜 = 올인콜 (to_handread 는 콜한 사람이 올인일 때만 call_jam 으로 본다)."""
    out, rb, level, last_inc = [], 0, 1.0, 1.0
    last_raiser, last_idx, fixed = None, None, 0
    for a in actions or []:
        a = dict(a)
        act, to = a.get("act"), _num(a.get("to"))
        if act == "raise":
            if a.get("seat") == last_raiser and last_idx is not None:
                prev = out[last_idx]
                if to is not None and (_num(prev.get("to")) or 0) < to:
                    prev["to"] = to
                prev["allin"] = bool(prev.get("allin") or a.get("allin"))
                level = max(level, to or level)
                fixed += 1
                continue
            inc = (to - level) if (to is not None and to > level) else None
            complete = not (a.get("allin") and inc is not None and inc < last_inc)
            a["raises_before"] = rb
            if complete:
                rb += 1
                if inc is not None:
                    last_inc = max(last_inc, inc)
                last_raiser, last_idx = a.get("seat"), len(out)
            else:
                fixed += 1
            if to is not None:
                level = max(level, to)
        elif act != "post":
            a["raises_before"] = rb
            if act == "call" and last_idx is not None and out[last_idx].get("allin") and not a.get("allin"):
                a["allin"] = True
                fixed += 1
        out.append(a)
    return out, fixed


def fixed_record(rec):
    """입력을 고친 기록 사본 (프리플랍 레이즈 정리) + 고친 개수."""
    acts = dict(rec.get("actions") or {})
    acts["preflop"], fixed = clean_preflop(acts.get("preflop"))
    r2 = dict(rec)
    r2["actions"] = acts
    return r2, fixed


def eff_stack(rec, seat, stack):
    """올인/올인콜 상대의 프리플랍 범위는 내 스택이 아니라 실제 올인 금액(더 작으면)으로."""
    amt = None
    for a in (rec.get("actions") or {}).get("preflop") or []:
        if a.get("seat") != seat:
            continue
        to = _num(a.get("to"))
        if to and (a.get("allin") or a.get("act") == "call"):
            amt = to if amt is None else max(amt, to)
    words = hr._norm_pre((replay_seats(rec).get(seat) or {}).get("preflop"))
    if amt and words and words[-1] in ("jam", "call_jam"):
        return min(stack, amt) if stack else amt
    return stack


def add_sizes(rec, seats):
    """to_handread 결과에 벳 크기를 붙인 사본 (포스트플랍 bet/call/raise/allin 단어를 {"a", "size"} 로)."""
    sizes = bet_sizes(rec)
    acts = rec.get("actions") or {}
    out = {}
    for k, seat in seats.items():
        s2 = dict(seat)
        for st in STREETS[1:]:
            words = list(seat.get(st) or [])
            idx = [i for i, a in enumerate(acts.get(st) or []) if a.get("seat") == k and a.get("act") in POST_WORDS]
            if len(idx) == len(words):
                new = []
                for w, i in zip(words, idx):
                    sz = sizes.get((st, i))
                    new.append({"a": w, "size": sz} if sz and w in ("bet", "call", "allin", "raise") else w)
                words = new
            s2[st] = words
        out[k] = s2
    return out


def truncate(seat, street):
    """street 까지의 액션만 남긴 좌석 입력."""
    keep = STREETS.index(street)
    s = dict(seat)
    for st in STREETS[keep + 1:]:
        s[st] = []
    if "bet_role" in s:
        s["bet_role"] = {k: v for k, v in s["bet_role"].items() if STREETS.index(k) <= keep}
    return s


def pre_only(seat):
    return truncate(seat, "preflop")


# --- 분류 --------------------------------------------------------------------------------------
def combo_key(c1, c2):
    hi, lo = (c1, c2) if c1 > c2 else (c2, c1)
    return ev.card_str(hi) + ev.card_str(lo), hi, lo


def actual_class(board, hi, lo):
    """(분류 이름 목록, 실제 분류 인덱스)"""
    if len(board) >= 3:
        return hr.CLASSES_POST, hr._classify(hr._board_info(board), hi, lo)[1]
    return hr.CLASSES_PRE, hr._combo_class_pre(hr._combo_code(hi, lo))


_UNI_CACHE = {}


def uniform_classes(board, dead):
    """남은 카드 무작위일 때의 분류 분포 (합 1)."""
    key = (tuple(board), tuple(sorted(dead)))
    got = _UNI_CACHE.get(key)
    if got is not None:
        return got
    blocked = set(board) | set(dead)
    left = [c for c in range(52) if c not in blocked]
    acc = [0.0] * 7
    n = 0
    bi = hr._board_info(board) if len(board) >= 3 else None
    for i, a in enumerate(left):
        for b in left[i + 1:]:
            hi, lo = (a, b) if a > b else (b, a)
            acc[hr._classify(bi, hi, lo)[1] if bi else hr._combo_class_pre(hr._combo_code(hi, lo))] += 1
            n += 1
    out = [x / n for x in acc]
    _UNI_CACHE[key] = out
    return out


def _class_metrics(probs, y):
    """probs: 7분류 확률(합≈1), y: 실제 인덱스."""
    tot = sum(probs) or 1.0
    p = [x / tot for x in probs]
    py = p[y]
    rank = 1 + sum(1 for i, x in enumerate(p) if i != y and x > py + 1e-9)
    brier = sum((x - (1.0 if i == y else 0.0)) ** 2 for i, x in enumerate(p))
    ll = -math.log((1 - EPS) * py + EPS / len(p))
    return {"cls_p": py, "cls_rank": rank, "cls_top1": rank == 1, "brier": brier, "cls_ll": ll, "cls_probs": p}


def score_result(res, key, code, y, n_live):
    """handread 결과 하나 → 점수 dict."""
    rng = res.get("range") or {}
    p_raw = rng.get(key, 0.0)
    p = (1 - EPS) * p_raw + EPS / n_live
    higher = sum(v for v in rng.values() if v > p_raw + 1e-15)
    equal = sum(v for v in rng.values() if abs(v - p_raw) <= 1e-15)
    names = hr.CLASSES_POST if hr.CLASSES_POST[0] in (res.get("classes") or {}) else hr.CLASSES_PRE
    probs = [(res.get("classes") or {}).get(n, 0.0) for n in names]
    out = {"p": p_raw, "ll": -math.log(p), "pit": higher + 0.5 * equal if p_raw > 0 else 1.0,
           "zero": p_raw <= 0, "top5": code in [c for c, _ in res.get("top_hands") or []],
           "width": res.get("width_pct")}
    out.update(_class_metrics(probs, y))
    return out


def score_uniform(board, dead, y, n_live):
    out = {"p": 1.0 / n_live, "ll": math.log(n_live), "pit": 0.5, "zero": False, "top5": False, "width": None}
    out.update(_class_metrics(uniform_classes(board, dead), y))
    return out


# --- 프로필 ------------------------------------------------------------------------------------
def profile_type(p):
    if not isinstance(p, dict) or not p:
        return "프로필 없음"
    hands = _num(p.get("hands")) or 0
    vpip, pfr = _num(p.get("vpip")), _num(p.get("pfr"))
    if hands < 10 or vpip is None:
        return "표본<10핸드"
    tag = "~" if hands < 30 else ""
    if vpip >= 40:
        style = "루즈패시브" if (pfr or 0) < 15 else "루즈공격"
    elif vpip < 20:
        style = "타이트"
    else:
        style = "보통"
    return tag + style


# --- 결정 기록 (사용자가 본 리딩) --------------------------------------------------------------
_CLS_RE = re.compile(r"^(.*\S)\s+(\d+(?:\.\d+)?)%$")


def _expand_token(tok):
    """'TT+' 'AJs+' 'A9o-A3o' '99-33' 'KQs' → 핸드코드 집합 (handread._tokens 의 역)."""
    R = hr._R
    tok = tok.strip()
    out = set()
    try:
        if tok.endswith("+"):
            t = tok[:-1]
            if len(t) == 2 and t[0] == t[1]:
                for i in range(0, R.index(t[0]) + 1):
                    out.add(R[i] * 2)
            elif len(t) == 3:
                hi, lo = R.index(t[0]), R.index(t[1])
                for i in range(hi + 1, lo + 1):
                    out.add(R[hi] + R[i] + t[2])
        elif "-" in tok:
            a, b = tok.split("-", 1)
            if len(a) == 2 and a[0] == a[1]:
                for i in range(R.index(a[0]), R.index(b[0]) + 1):
                    out.add(R[i] * 2)
            elif len(a) == 3:
                hi = R.index(a[0])
                for i in range(R.index(a[1]), R.index(b[1]) + 1):
                    out.add(R[hi] + R[i] + a[2])
        elif len(tok) == 2 and tok[0] == tok[1] and tok[0] in R:
            out.add(tok)
        elif len(tok) == 3 and tok[0] in R and tok[1] in R and tok[2] in "so":
            out.add(tok)
    except ValueError:
        return set()
    return out


def parse_logged(line):
    """'BTN 오픈: 예상 상위 7% (66, A9s-A7s 외 8%) | 보드 기준: 탑페어+ 40% · 드로우 30%' →
    {codes, covered(%), classes{이름: %}, board(bool), truncated}."""
    out = {"codes": set(), "covered": None, "classes": {}, "board": None, "truncated": False}
    if not line:
        return out
    head, _, tail = line.partition(" | ")
    m = re.search(r"예상 상위 [\d.]+% \((.*)\)\s*$", head)
    if m:
        body = m.group(1)
        rest = re.search(r"\s외 (\d+(?:\.\d+)?)%$", body)
        covered = 100.0 - float(rest.group(1)) if rest else 100.0
        if rest:
            body = body[:rest.start()]
        for t in body.split(","):
            out["codes"] |= _expand_token(t)
        out["covered"] = covered
    elif "예상 상위" in head:
        out["truncated"] = True
    if "기준:" in tail:
        kind, _, cls = tail.partition("기준:")
        out["board"] = kind.strip().startswith("보드")
        for part in cls.split("·"):
            mm = _CLS_RE.match(part.strip())
            if mm:
                out["classes"][mm.group(1)] = float(mm.group(2))
    return out


def logged_reads(decs):
    """{(hand_id, street, seat): 마지막 결정의 read line}."""
    out = {}
    for d in sorted(decs, key=lambda d: (d.get("t") or 0)):
        for k, r in (d.get("reads") or {}).items():
            if isinstance(r, dict) and r.get("line"):
                out[(d.get("hand_id"), d.get("street"), k)] = r["line"]
    return out


# --- 채점 본체 ---------------------------------------------------------------------------------
def _kind_label(sized_seat, street):
    """그 스트리트 마지막 액션의 종류 (크기·주도권 포함; 크기 모르면 '_?')."""
    if street == "preflop":
        return "pre_" + str(hr._pre_action(hr._norm_pre(sized_seat.get("preflop"))) or "?")
    kinds = hr._norm_post(sized_seat.get(street), (sized_seat.get("bet_role") or {}).get(street))
    if not kinds:
        return None
    kind, size = kinds[-1]
    if size is None and kind.rsplit("_", 1)[-1] in ("small", "med", "big"):
        kind = kind.rsplit("_", 1)[0] + "_?"
    return kind


def _family(kind):
    if kind is None or kind.startswith("pre_"):
        return None
    if kind == "check":
        return "check"
    if kind.startswith("call"):
        return "call"
    return "aggr"


def _context(rec, decs_by_hand, sess_defaults):
    ds = decs_by_hand.get(rec.get("id")) or []
    last = ds[-1] if ds else None
    sess = str(rec.get("id") or "").rsplit("-", 1)[0]
    dflt = sess_defaults.get(sess) or {}
    stack = _num((last or {}).get("stack_bb")) or dflt.get("stack_bb") or 100
    ante = bool((_num((last or {}).get("ante")) or 0) > 0) if last else bool(dflt.get("ante"))
    profiles = {}
    for d in ds:
        for k, s in (d.get("seats") or {}).items():
            if isinstance(s, dict) and isinstance(s.get("profile"), dict):
                profiles[k] = s["profile"]
    return stack, ante, profiles


def _session_defaults(decs):
    acc = defaultdict(list)
    for d in decs:
        acc[str(d.get("hand_id") or "").rsplit("-", 1)[0]].append(d)
    out = {}
    for s, ds in acc.items():
        stacks = [_num(d.get("stack_bb")) for d in ds if _num(d.get("stack_bb"))]
        antes = [(_num(d.get("ante")) or 0) > 0 for d in ds]
        out[s] = {"stack_bb": sorted(stacks)[len(stacks) // 2] if stacks else None,
                  "ante": antes.count(True) > antes.count(False)}
    return out


def score_hands(hands, decs, ok_only=False):
    """→ (rows, logged_rows, skipped Counter)."""
    skipped = Counter()
    decs_by_hand = defaultdict(list)
    for d in sorted(decs, key=lambda d: d.get("t") or 0):
        decs_by_hand[d.get("hand_id")].append(d)
    sess_defaults = _session_defaults(decs)
    logged = logged_reads(decs)
    rows, lrows, seen = [], [], set()
    order = sorted(hands, key=lambda r: (r.get("confidence") != "ok", r.get("start") or 0))
    for rec in order:
        seats_rec = rec.get("seats") or {}
        shown = {k: s.get("shown") for k, s in seats_rec.items() if s.get("shown") and not s.get("hero")}
        if not shown:
            continue
        if ok_only and rec.get("confidence") != "ok":
            skipped["low_confidence"] += len(shown)
            continue
        if not (rec.get("actions") or {}).get("preflop"):
            skipped["no_preflop(mid_hand_start)"] += len(shown)
            continue
        board = hr._board_cards(rec.get("board") or [])
        dead = [c for c in hr._cards(rec.get("hero_cards") or []) if c not in board]
        live_seats = replay_seats(rec)
        frec, nfix = fixed_record(rec)
        fixed_seats = add_sizes(frec, replay_seats(frec))
        phantom = phantom_streets(rec)
        stack, ante, profiles = _context(rec, decs_by_hand, sess_defaults)
        players = int(_num(rec.get("pos_players")) or _num(rec.get("players")) or 9)
        for k, cards in shown.items():
            cs = hr._cards(cards)
            if len(cs) != 2 or set(cs) & (set(board) | set(dead)):
                skipped["bad_shown_cards"] += 1
                continue
            dkey = (tuple(board), tuple(sorted(cs)))
            if dkey in seen:
                skipped["duplicate"] += 1
                continue
            seen.add(dkey)
            if k not in live_seats or not live_seats[k].get("preflop"):
                skipped["no_actions"] += 1
                continue
            key, hi, lo = combo_key(*cs)
            code = hr._combo_code(hi, lo)
            prof = profiles.get(k)
            ptype = profile_type(prof)
            prof_arg = {k: prof} if prof else None
            fstack = eff_stack(frec, k, stack)
            for st in STREETS:
                n = NEED[st]
                if st != "preflop":
                    if len(board) < n or not live_seats[k].get(st):
                        continue
                    if st in phantom:
                        skipped["phantom_street(올인 뒤 체크)"] += 1
                        continue
                    if all(w == "fold" for w in live_seats[k].get(st)):
                        continue
                b = board[:n]
                n_live = comb(52 - len(b) - len(dead), 2)
                names, y = actual_class(b, hi, lo)
                ests = {}
                for var, seat, stk in (("live", truncate(live_seats[k], st), stack),
                                       ("fixed", truncate(fixed_seats.get(k) or live_seats[k], st), fstack),
                                       ("pre", pre_only(live_seats[k]), stack)):
                    ests[var] = hr.estimate({k: seat}, b, players, dead=dead, profiles=prof_arg, stack_bb=stk,
                                            ante=ante)[k]
                kind = _kind_label(live_seats[k], st) if st == "preflop" else _kind_label(fixed_seats.get(k) or {}, st)
                row = OrderedDict(hand=rec.get("id"), seat=k, street=st, actual=code, actual_class=names[y],
                                  kind=kind, kind_fixed=_kind_label(fixed_seats.get(k) or {}, st),
                                  family=_family(kind), profile=ptype, conf=rec.get("confidence"),
                                  label=ests["live"].get("label"), label_fixed=ests["fixed"].get("label"),
                                  stack=stack, stack_fixed=fstack, input_fixes=nfix)
                for var in ("live", "fixed", "pre"):
                    row[var] = score_result(ests[var], key, code, y, n_live)
                row["uniform"] = score_uniform(b, dead, y, n_live)
                row["class_names"] = names
                rows.append(row)
                line = logged.get((rec.get("id"), st, k))
                if line:
                    lrows.append(score_logged(line, rec.get("id"), k, st, code, names[y], row))
    return rows, lrows, skipped


def score_logged(line, hand, seat, street, code, cls, row):
    p = parse_logged(line)
    shown = p["classes"]
    top = max(shown.items(), key=lambda kv: kv[1])[0] if shown else None
    return OrderedDict(hand=hand, seat=seat, street=street, actual=code, actual_class=cls, kind=row["kind"],
                       family=row["family"], in_top3=cls in shown, top1=(top == cls), shown_p=shown.get(cls, 0.0) / 100,
                       in_summary=(code in p["codes"]) if p["covered"] is not None else None,
                       covered=p["covered"], replay_cls_p=row["live"]["cls_p"])


# --- 집계 --------------------------------------------------------------------------------------
def _mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def agg(rows):
    """행 묶음 → 변형별 평균 점수."""
    out = {"n": len(rows)}
    for var in VARIANTS:
        rs = [r[var] for r in rows]
        out[var] = {
            "ll": _mean([r["ll"] for r in rs]),
            "pit": _mean([r["pit"] for r in rs]),
            "cls_p": _mean([r["cls_p"] for r in rs]),
            "cls_top1": _mean([float(r["cls_top1"]) for r in rs]),
            "cls_rank": _mean([r["cls_rank"] for r in rs]),
            "brier": _mean([r["brier"] for r in rs]),
            "cls_ll": _mean([r["cls_ll"] for r in rs]),
            "top5": _mean([float(r["top5"]) for r in rs]),
            "zero": _mean([float(r["zero"]) for r in rs]),
        }
    if rows:
        u = out["uniform"]["ll"]
        for var in ("live", "fixed", "pre"):
            out[var]["bits_vs_uniform"] = (u - out[var]["ll"]) / math.log(2)
    return out


def calibration(rows, var="live"):
    """분류별 평균 예측 확률 vs 실제 빈도."""
    if not rows:
        return {}
    names = rows[0]["class_names"]
    pred = [_mean([r[var]["cls_probs"][i] for r in rows]) for i in range(7)]
    act = [sum(1 for r in rows if r["actual_class"] == n) / len(rows) for n in names]
    return OrderedDict((n, {"pred": round(p, 3), "actual": round(a, 3)}) for n, p, a in zip(names, pred, act))


def group(rows, keyfn):
    g = OrderedDict()
    for r in rows:
        k = keyfn(r)
        if k is None:
            continue
        g.setdefault(k, []).append(r)
    return g


def summarize(rows, lrows, skipped):
    post = [r for r in rows if r["street"] != "preflop"]
    final = {}
    for r in rows:
        final[(r["hand"], r["seat"])] = r
    s = OrderedDict()
    s["n_seats"] = len({(r["hand"], r["seat"]) for r in rows})
    s["n_rows"] = len(rows)
    s["skipped"] = dict(skipped)
    s["by_street"] = OrderedDict((k, agg(v)) for k, v in group(rows, lambda r: r["street"]).items())
    s["final_street"] = agg(list(final.values()))
    s["by_kind"] = OrderedDict((k, agg(v)) for k, v in sorted(group(post, lambda r: r["kind"]).items()))
    s["by_family"] = OrderedDict((f"{r0}/{k}", agg(v)) for (r0, k), v in sorted(
        group(post, lambda r: (r["street"], r["family"])).items()))
    s["by_preflop_action"] = OrderedDict((k, agg(v)) for k, v in sorted(
        group([r for r in rows if r["street"] == "preflop"], lambda r: r["kind"]).items()))
    s["by_preflop_action_fixed"] = OrderedDict((k, agg(v)) for k, v in sorted(
        group([r for r in rows if r["street"] == "preflop"], lambda r: r["kind_fixed"]).items()))
    s["by_profile"] = OrderedDict((k, agg(v)) for k, v in sorted(group(rows, lambda r: r["profile"]).items()))
    s["by_confidence"] = OrderedDict((k, agg(v)) for k, v in sorted(group(rows, lambda r: r["conf"]).items()))
    s["calibration_by_family"] = OrderedDict(
        (f"{st}/{fam}", {"n": len(v), "classes": calibration(v)})
        for (st, fam), v in sorted(group(post, lambda r: (r["street"], r["family"])).items()))
    s["calibration_postflop_all"] = {"n": len(post), "classes": calibration(post)}
    s["calibration_preflop"] = {"n": sum(1 for r in rows if r["street"] == "preflop"),
                                "classes": calibration([r for r in rows if r["street"] == "preflop"])}
    if lrows:
        lg = OrderedDict()
        for k, v in group(lrows, lambda r: r["street"]).items():
            summ = [x for x in v if x["in_summary"] is not None]
            lg[k] = {"n": len(v), "in_top3": _mean([float(x["in_top3"]) for x in v]),
                     "top1": _mean([float(x["top1"]) for x in v]), "shown_p": _mean([x["shown_p"] for x in v]),
                     "replay_cls_p": _mean([x["replay_cls_p"] for x in v]),
                     "summary_n": len(summ), "in_summary": _mean([float(x["in_summary"]) for x in summ]),
                     "summary_claims": _mean([x["covered"] / 100 for x in summ])}
        s["logged"] = lg
    return s


# --- 보고 --------------------------------------------------------------------------------------
def _pct(x, d=0):
    return "-" if x is None else f"{x * 100:.{d}f}%"


def _f(x, d=2):
    return "-" if x is None else f"{x:.{d}f}"


def _line(name, a):
    lv, fx, pr, un = a["live"], a["fixed"], a["pre"], a["uniform"]
    return (f"  {name:<16} n={a['n']:>3} | 정보이득 live {_f(lv.get('bits_vs_uniform'), 1)}bit"
            f" (fixed {_f(fx.get('bits_vs_uniform'), 1)}, pre {_f(pr.get('bits_vs_uniform'), 1)})"
            f" | PIT {_f(lv['pit'])} | 실제분류 확률 {_pct(lv['cls_p'])} (pre {_pct(pr['cls_p'])}, 무작위 {_pct(un['cls_p'])})"
            f" 1순위 {_pct(lv['cls_top1'])} | Brier {_f(lv['brier'])}/{_f(pr['brier'])}/{_f(un['brier'])}"
            f" | top5 {_pct(lv['top5'])} | 0확률 {_pct(lv['zero'])}")


def report(s, out=None):
    w = (out or sys.stdout).write
    w(f"== 상대 리딩 채점: 공개된 상대 {s['n_seats']}명, 채점 지점 {s['n_rows']}개 ==\n")
    if s["skipped"]:
        w("제외: " + ", ".join(f"{k} {v}" for k, v in s["skipped"].items()) + "\n")
    w("지표: 정보이득 = 무작위 대비 실제 콤보를 몇 bit 더 잘 맞혔나(높을수록 좋음) · PIT 0.5 근처가 보정 양호"
      "(낮음=실제 패가 범위 중심, 높음=범위 꼬리) · Brier live/pre/무작위 (낮을수록 좋음)\n")
    w("\n[스트리트별]\n")
    for k, a in s["by_street"].items():
        w(_line(STREET_KO.get(k, k), a) + "\n")
    w(_line("마지막 스트리트", s["final_street"]) + "\n")
    w("\n[프리플랍 액션별]\n")
    for k, a in s["by_preflop_action"].items():
        w(_line(k, a) + "\n")
    w("\n[프리플랍 액션별 — 입력 정리 후(fixed) 분류 기준]\n")
    for k, a in s["by_preflop_action_fixed"].items():
        w(_line(k, a) + "\n")
    w("\n[포스트플랍 마지막 액션별 (크기=기록 금액 기준, _?=금액 모름)]\n")
    for k, a in s["by_kind"].items():
        w(_line(str(k), a) + "\n")
    w("\n[프로필별]\n")
    for k, a in s["by_profile"].items():
        w(_line(k, a) + "\n")
    w("\n[기록 신뢰도별]\n")
    for k, a in s["by_confidence"].items():
        w(_line(str(k), a) + "\n")
    w("\n[분류 보정: 예측 평균 → 실제 빈도 (live)]\n")
    for name, c in [("프리플랍", s["calibration_preflop"]), ("포스트플랍 전체", s["calibration_postflop_all"])] + \
            [(k, v) for k, v in s["calibration_by_family"].items()]:
        if not c["n"]:
            continue
        parts = [f"{n} {_pct(v['pred'])}→{_pct(v['actual'])}" for n, v in c["classes"].items()
                 if v["pred"] >= 0.03 or v["actual"] > 0]
        w(f"  {name} (n={c['n']}): " + " · ".join(parts) + "\n")
    if s.get("logged"):
        w("\n[결정 순간 화면에 보인 리딩 (decisions 기록)]\n")
        for k, v in s["logged"].items():
            w(f"  {STREET_KO.get(k, k):<6} n={v['n']:>3} | 실제 분류가 표시 상위3 안 {_pct(v['in_top3'])}, 1순위 {_pct(v['top1'])},"
              f" 표시 확률 {_pct(v['shown_p'])} (재생 {_pct(v['replay_cls_p'])})"
              f" | 요약 표기에 실제 핸드 포함 {_pct(v['in_summary'])} (표기가 주장한 비중 {_pct(v['summary_claims'])},"
              f" n={v['summary_n']})\n")
    w("\n주의: 쇼다운까지 간 핸드만 보임 → 폴드시킨 블러프가 빠져 공격 액션의 에어 비중은 실제보다 낮게 관측된다. 표본이 작다.\n")


def _jsonable(rows):
    out = []
    for r in rows:
        r2 = OrderedDict((k, v) for k, v in r.items() if k != "class_names")
        for var in VARIANTS:
            if var in r2:
                r2[var] = {k: (round(v, 4) if isinstance(v, float) else
                               [round(x, 4) for x in v] if isinstance(v, list) else v) for k, v in r2[var].items()}
        out.append(r2)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="쇼다운 공개 카드로 상대 리딩 채점")
    ap.add_argument("folder", nargs="?", help="핸드 기록 폴더 (기본: CARD_READER_DATA 또는 dist/hands)")
    ap.add_argument("--ok-only", action="store_true", help="confidence=ok 기록만")
    ap.add_argument("--json", help=f"JSON 요약 경로 (기본: 데이터 폴더/{OUT_NAME})")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    folder = data_dir(args.folder)
    hands, decs = load(folder)
    rows, lrows, skipped = score_hands(hands, decs, ok_only=args.ok_only)
    s = summarize(rows, lrows, skipped)
    if not args.quiet:
        report(s)
    path = args.json or os.path.join(folder, OUT_NAME)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"summary": s, "rows": _jsonable(rows), "logged": lrows}, f, ensure_ascii=False, indent=1,
                      default=lambda o: sorted(o) if isinstance(o, set) else str(o))
        if not args.quiet:
            print(f"\nJSON: {path}")
    except OSError as e:
        print(f"JSON 저장 실패: {e}", file=sys.stderr)
    return s


if __name__ == "__main__":
    main()
