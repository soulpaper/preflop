"""결정 시점 기록: 내 차례가 올 때마다 그때까지의 베팅 라인·보드 텍스처·추천을 JSONL 로 쌓고,
핸드가 끝나면(handlog.Tracker.on_hand) 내가 실제로 한 행동과 결과를 별도 줄로 덧붙인다. 나중에 분석용
(보드 텍스처 x 벳 크기별 상대 경향, 추천 대 실제 행동 평가).

    log = decisionlog.DecisionLog(BASE, on_error=log_debug)         # BASE/hands/decisions_YYYY-MM-DD.jsonl
    did = log.record(ctx, advice, reads=reads, state=tracker, profiles=track["profiles"])   # 내 차례 + 추천 계산 뒤
    log.link_hand(hand_record)                                       # Tracker.on_hand 체인 (store.observe 옆)
    log.hero_action(did, "call", 3.0)                                # (선택) 수동 확정
    recs = decisionlog.load(BASE, days=7); decisionlog.summarize(recs)

파일 형식 (줄 단위 JSON, append 전용 → 중간에 죽어도 앞부분은 그대로 읽힘)
  {"type": "decision", "id": ..., ...}          내 차례가 왔을 때 (아래 DECISION_FIELDS)
  {"type": "resolution", "id": <결정 id>, ...}   핸드가 끝난 뒤: hero_actual / followed / outcome / later ...
load() 는 둘을 합쳐 결정 dict 에 'resolution' 키로 붙여 준다 (같은 필드는 나중 줄이 이김).

DECISION_FIELDS
  id, v, t, day, hand_id, street, turn, rev, hero_acts_before, state_street
  hero[2], board[], tex{...board_texture}, hero_pos, villain_pos, situation, hero_ip, aggressor, flop_line, villain_raised
  players, opponents, stack_bb, my_stack, pot, pot_total, call, call_pct_pot, spr, can_check, facing_allin, ante
  bettor{seat,pos,street,act,to,pct} (이번 스트리트 마지막 상대 벳/레이즈), n_aggr (이번 스트리트 벳/레이즈 수)
  seats{키: {pos, name|None, placeholder, hero, folded(street), acts{street:[{act,to,pct,allin}]}, profile{...}}}
      pct = 그 액션으로 낸 칩 / 액션 직전 팟 (BB 단위 팟을 액션 기록에서 다시 계산한 값; 앤티·OCR 팟과 다를 수 있음)
  equity, equity_basis, outs, hand_name, reads{키: {width, summary, classes, line}}
  advice{action(영문 fold/check/call/bet/raise/allin/wait), action_ko, size_bb, size_text, confidence, reason,
         detail[], plan[], color_key, gto_mode(primary/secondary/None), gto_line}
  looseness, tourney{players_left, my_rank, stage, bubble_factor ...}

resolution 필드: src(tracker/manual), hero_actual{action,to,added,pct,allin,size_bb}, followed(True/False/None),
  size_ratio, outcome{final_board, streets, hero_folded, showdown, shown{pos|seat: cards}, hero_won, hero_delta_bb,
  delta_src, confidence, issues[]}, responses[{pos,act}] (내 액션 뒤 같은 스트리트 상대 첫 반응),
  later[{street,pos,act,to,pct}] (내 액션 뒤 상대 액션), bettor_next[], bettor_continued
  hero_delta_bb 는 근사값 (사이드 팟·앤티 무시; 폴드=내가 낸 합계의 음수, 모두 폴드=상대가 낸 합계).

절대 예외를 밖으로 내지 않는다 (실패하면 errors 증가 + on_error(제목) 호출, 빈 값/None 반환). 스레드 안전.
"""
import json
import sys
import threading
import time
from collections import Counter, OrderedDict
from pathlib import Path

BASE = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent
STREETS = ("preflop", "flop", "turn", "river")
SCHEMA_V = 1
RANKS = "23456789TJQKA"
ACTION_EN = {"폴드": "fold", "체크": "check", "콜": "call", "벳": "bet", "레이즈": "raise", "올인": "allin", "대기": "wait"}
CATEGORY = {"fold": "fold", "check": "check", "call": "call", "bet": "aggr", "raise": "aggr", "allin": "aggr"}
MEM_HANDS = 64
MEM_KEYS = 512
PROFILE_KEYS = ("vpip", "pfr", "af", "fold_to_bet", "threebet", "wtsd", "hands", "source")
CTX_DIRECT = ("players", "hero_pos", "villain_pos", "situation", "hero_ip", "aggressor", "flop_line", "villain_raised",
              "stack_bb", "my_stack", "pot", "pot_total", "call", "can_check", "facing_allin", "ante", "outs",
              "hand_name", "looseness", "equity_basis")
TOURNEY_KEYS = ("players_left", "my_rank", "stage", "bubble_factor", "bubble", "m_ratio", "payouts_left", "icm")


# --- 기본 도구 ------------------------------------------------------------------------------------
def _num(v):
    try:
        return None if v is None or isinstance(v, bool) else float(v)
    except (TypeError, ValueError):
        return None


def _r(v, n=2):
    v = _num(v)
    return None if v is None else round(v, n)


def _prim(v, depth=0):
    """JSON 에 안전한 원시 값만 (numpy 숫자 -> float, 그 밖은 문자열)."""
    if v is None or isinstance(v, (bool, int, str)):
        return v if not isinstance(v, str) else v[:300]
    if isinstance(v, float):
        return v if v == v and abs(v) != float("inf") else None
    if depth >= 4:
        return str(v)[:100]
    if isinstance(v, dict):
        return {str(k): _prim(x, depth + 1) for k, x in list(v.items())[:40]}
    if isinstance(v, (list, tuple, set, frozenset)):
        return [_prim(x, depth + 1) for x in list(v)[:40]]
    f = _num(v)  # numpy 스칼라
    return f if f is not None else str(v)[:100]


def _is_card(c):
    return isinstance(c, str) and len(c) == 2 and c[0] in RANKS and c[1] in "shdc"


def norm_action(a):
    """추천/행동 이름 -> 영문 (fold check call bet raise allin wait). 모르는 건 그대로 소문자."""
    if a is None:
        return None
    a = str(a).strip()
    return ACTION_EN.get(a, a.lower())


def _day(t):
    return time.strftime("%Y-%m-%d", time.localtime(max(float(t), 172800.0)))


# --- 보드 텍스처 ----------------------------------------------------------------------------------
def board_texture(board):
    """['Ks','7d','2c'] -> 텍스처 특징 dict (보드 0장이면 {}).
    suit: monotone / three_flush / four_flush / two_tone / rainbow     paired: 페어 있음, trips: 트립스 이상
    high: 가장 높은 카드 글자, broadway: T 이상 장수, low: 가장 높은 카드가 8 이하
    connected 0..1: 5칸 창(스트레이트 가능 구간) 안에 보드 랭크 3개 이상이면 크게, 2개면 조금 (휴리스틱)
    straight_windows: 보드 랭크 3개 이상 든 5칸 창 수 (= 스트레이트가 보드만으로 둘 만에 만들어질 수 있는 정도)
    wet 0..1 (플러시+연결 위주; 페어면 약간 깎음) / label dry<0.25<semi<0.5<=wet / bucket = label (+'+paired')"""
    cards = [c for c in (board or []) if _is_card(c)]
    n = len(cards)
    if n < 3:
        return {}
    ranks = [RANKS.index(c[0]) for c in cards]           # 0=2 .. 12=A
    suits = Counter(c[1] for c in cards)
    smax = max(suits.values())
    rc = Counter(ranks)
    paired = max(rc.values()) >= 2
    if smax == n:
        suit = "monotone"
    elif smax >= 4:
        suit = "four_flush"
    elif smax == 3:
        suit = "three_flush"
    elif smax == 2:
        suit = "two_tone"
    else:
        suit = "rainbow"
    # 스트레이트 창: 값 1..14 (A 는 1 과 14), 창 = [lo, lo+4] lo=1..10
    vals = set()
    for x in rc:
        vals.add(x + 2)
        if x == 12:
            vals.add(1)
    w3 = w2 = 0
    for lo in range(1, 11):
        k = sum(1 for v in range(lo, lo + 5) if v in vals)
        if k >= 3:
            w3 += 1
        elif k == 2:
            w2 += 1
    conn = min(1.0, (w3 + 0.15 * w2) / 3.0)
    flush_comp = {3: {1: 0.0, 2: 0.5, 3: 1.0},
                  4: {1: 0.0, 2: 0.4, 3: 0.8, 4: 1.0},
                  5: {1: 0.0, 2: 0.0, 3: 0.8, 4: 1.0, 5: 1.0}}[min(n, 5)][min(smax, n)]
    high = max(ranks)
    broadway = sum(1 for x in ranks if x >= 8)
    wet = 0.5 * conn + 0.4 * flush_comp + (0.1 if broadway >= 2 else 0.0) - (0.1 if paired else 0.0)
    wet = max(0.0, min(1.0, wet))
    label = "dry" if wet < 0.25 else "semi" if wet < 0.5 else "wet"
    return {"n": n, "suit": suit, "flush_max": smax, "paired": paired, "trips": max(rc.values()) >= 3,
            "high": RANKS[high], "broadway": broadway, "low": high <= 6,
            "connected": round(conn, 2), "straight_windows": w3, "wet": round(wet, 2), "label": label,
            "bucket": label + ("+paired" if paired else "")}


# --- 액션 기록 정리 ---------------------------------------------------------------------------------
def annotate(actions, pos=None):
    """{street: [{seat,act,to,...}]} -> ({street: [{seat,act,to?,pct?,allin?}]}, {seat: 낸 총액}).
    pct = 그 액션으로 낸 칩 / 액션 직전 팟. 프리플랍 팟은 블라인드(SB .5 / BB 1)로 시작 (포지션을 알 때)."""
    out, pot, total = {}, 0.0, Counter()
    for si, s in enumerate(STREETS):
        inv = Counter()
        if si == 0:
            for k, p in (pos or {}).items():
                if p == "SB":
                    inv[k] = 0.5
                elif p == "BB":
                    inv[k] = 1.0
        rows = []
        for a in (actions or {}).get(s) or []:
            k, act, to = a.get("seat"), a.get("act"), _num(a.get("to"))
            row = {"seat": k, "act": act}
            if to is not None:
                if act != "post":
                    before = pot + sum(inv.values())
                    added = to - inv[k]
                    if added > 0 and before > 0:
                        row["pct"] = round(added / before, 2)
                inv[k] = max(inv[k], to)
                row["to"] = round(to, 2)
            if a.get("allin"):
                row["allin"] = True
            rows.append(row)
        out[s] = rows
        pot += sum(inv.values())
        total.update(inv)
    return out, dict(total)


def _norm_state(state):
    """Tracker / _Hand / 기록 dict -> {hand_id, street, actions, seats{key:{pos,name,truncated,folded}}} 또는 None."""
    if state is None:
        return None
    h = state
    if hasattr(state, "hand") and not hasattr(state, "seats"):
        h = state.hand
    if h is None:
        return None
    if isinstance(h, dict):
        seats = {k: {"pos": v.get("pos"), "name": v.get("name"), "truncated": bool(v.get("truncated")),
                     "folded": v.get("folded")} for k, v in (h.get("seats") or {}).items()}
        st = h.get("streets")
        return {"hand_id": h.get("id") or h.get("hand_id"), "street": STREETS.index(st) if st in STREETS else None,
                "actions": h.get("actions") or {}, "seats": seats}
    seats = {}
    for k, s in h.seats.items():
        name = s.names.most_common(1)[0][0] if s.names else (None, False)
        seats[k] = {"pos": s.pos.most_common(1)[0][0] if s.pos else None, "name": name[0],
                    "truncated": any(t for (n, t) in s.names if n == name[0]),
                    "folded": STREETS[s.fold_street] if s.folded and s.fold_street is not None else None}
    return {"hand_id": h.id, "street": h.street, "actions": {s: [dict(a) for a in h.actions.get(s, [])] for s in STREETS},
            "seats": seats}


def _profile_summary(p):
    if not isinstance(p, dict):
        return None
    out = {k: _prim(p.get(k)) for k in PROFILE_KEYS if p.get(k) is not None}
    return out or None


def _reads_summary(reads):
    """앱의 reads ({order, lines, width, ...}) 또는 좌석별 handread.estimate 결과 -> {좌석: {width, summary, classes, line}}."""
    if not isinstance(reads, dict) or not reads:
        return None
    out = {}
    if any(k in reads for k in ("order", "lines", "width")):
        lines, width = reads.get("lines") or {}, reads.get("width") or {}
        summ, classes = reads.get("summary") or {}, reads.get("classes") or {}
        keys = list(reads.get("order") or []) + [k for k in list(lines) + list(width) + list(summ) + list(classes)]
        for k in dict.fromkeys(keys):
            e = {}
            if _r(width.get(k), 1) is not None:
                e["width"] = _r(width.get(k), 1)
            if summ.get(k):
                e["summary"] = str(summ[k])[:80]
            if isinstance(classes.get(k), dict):
                e["classes"] = _top_classes(classes[k])
            ln = lines.get(k)
            if ln:
                e["line"] = " | ".join(str(x) for x in (ln if isinstance(ln, (list, tuple)) else [ln])[:2])[:240]
            if e:
                out[str(k)] = e
    else:
        for k, v in reads.items():
            if not isinstance(v, dict):
                continue
            e = {}
            if _r(v.get("width_pct"), 1) is not None:
                e["width"] = _r(v.get("width_pct"), 1)
            if v.get("summary"):
                e["summary"] = str(v["summary"])[:80]
            if isinstance(v.get("classes"), dict):
                e["classes"] = _top_classes(v["classes"])
            if e:
                out[str(k)] = e
    return out or None


def _top_classes(classes, n=3):
    items = sorted(((str(k), _num(v)) for k, v in classes.items() if _num(v) is not None), key=lambda kv: -kv[1])
    return {k: round(v, 1) for k, v in items[:n] if v >= 1}


def _advice_summary(advice):
    if not isinstance(advice, dict):
        return None
    detail = [str(d)[:200] for d in (advice.get("detail") or []) if d][:3]
    plan = _prim(advice.get("plan") or [])
    gto_line = next((d for d in detail if "GTO" in d), None)
    if any("GTO 참고" in d for d in detail):
        gto_mode = "secondary"
    elif str(advice.get("reason") or "").startswith("GTO") or any("규칙 기반 판단" in d for d in detail):
        gto_mode = "primary"
    else:
        gto_mode = None
    out = {"action": norm_action(advice.get("action")), "action_ko": advice.get("action"),
           "size_bb": _r(advice.get("size_bb"), 1), "size_text": advice.get("size_text") or None,
           "confidence": advice.get("confidence"), "reason": str(advice.get("reason") or "")[:300],
           "detail": detail, "plan": plan, "color_key": advice.get("color_key"),
           "gto_mode": gto_mode, "gto_line": gto_line}
    if isinstance(advice.get("gto"), dict):
        out["gto"] = _prim(advice["gto"])
    return out


def _cat(a):
    return CATEGORY.get(norm_action(a))


def size_bucket(pct):
    """팟 대비 벳 크기 -> 버킷 이름 (분석용 묶음)."""
    p = _num(pct)
    if p is None:
        return "nobet"
    if p < 0.42:
        return "<=1/3"
    if p < 0.62:
        return "1/2"
    if p < 0.9:
        return "2/3-3/4"
    if p < 1.2:
        return "pot"
    return "overbet"


# --- 로그 ------------------------------------------------------------------------------------------
class DecisionLog:
    def __init__(self, base_dir=None, on_error=None, enabled=True):
        self.dir = Path(base_dir if base_dir is not None else BASE) / "hands"
        self.on_error = on_error
        self.enabled = enabled
        self.lock = threading.RLock()
        self.errors = 0
        self.last_error = None
        self.count = 0
        self.last_ms = 0.0
        self._keys = OrderedDict()      # 중복 키 -> 결정 id
        self._mem = OrderedDict()       # hand_id -> [결정 dict]
        self._by_id = {}                # 결정 id -> 결정 dict (_mem 에 든 것)
        self._turns = {}                # (hand_id, street) -> [{"turn":.., "call":.., "n_rev":..}] (state 없을 때)
        self._seq = Counter()
        self._checked = set()           # 줄바꿈 끝 확인한 파일
        self._linked = set()
        self._mkdir = False

    # --- 내부 -------------------------------------------------------------------------------------
    def _fail(self, title):
        self.errors += 1
        try:
            import traceback
            self.last_error = traceback.format_exc().strip().splitlines()[-1][:300]
        except Exception:
            self.last_error = title
        if self.on_error is not None:
            try:
                self.on_error(title)
            except Exception:
                pass

    def _append(self, day, obj):
        if not self._mkdir:
            self.dir.mkdir(parents=True, exist_ok=True)
            self._mkdir = True
        path = self.dir / f"decisions_{day}.jsonl"
        line = json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n"
        if path not in self._checked:   # 이전에 죽어서 줄바꿈 없이 끝난 파일이면 새 줄부터
            self._checked.add(path)
            try:
                with open(path, "rb") as f:
                    f.seek(0, 2)
                    if f.tell():
                        f.seek(-1, 2)
                        if f.read(1) != b"\n":
                            line = "\n" + line
            except OSError:
                pass
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)

    def _remember(self, d):
        hid = d.get("hand_id")
        self._mem.setdefault(hid, []).append(d)
        self._mem.move_to_end(hid)
        self._by_id[d["id"]] = d
        while len(self._mem) > MEM_HANDS:
            _, old = self._mem.popitem(last=False)
            for x in old:
                self._by_id.pop(x["id"], None)

    # --- 기록 -------------------------------------------------------------------------------------
    def record(self, ctx, advice, reads=None, state=None, hand_id=None, t=None, profiles=None):
        """내 차례 + 추천 계산 뒤에 부름. 같은 결정(키 동일)이면 새로 쓰지 않고 기존 id 를 돌려줌. 실패하면 None.
        state: handlog.Tracker (또는 Tracker.hand / 기록 dict) -> 지금까지의 좌석별 액션. profiles: {좌석키: stats}."""
        if not self.enabled:
            return None
        try:
            t0 = time.perf_counter()
            with self.lock:
                out = self._record(ctx, advice, reads, state, hand_id, t, profiles)
            self.last_ms = (time.perf_counter() - t0) * 1000
            return out
        except Exception:
            self._fail("decisionlog.record")
            return None

    def _record(self, ctx, advice, reads, state, hand_id, t, profiles):
        if not isinstance(ctx, dict):
            return None
        t = time.time() if t is None else float(t)
        board = [c for c in (ctx.get("board") or []) if _is_card(c)]
        hero = [c for c in (ctx.get("hero") or []) if _is_card(c)][:2]
        street = ctx.get("street") if ctx.get("street") in STREETS else \
            {0: "preflop", 3: "flop", 4: "turn", 5: "river"}.get(len(board), "preflop")
        stn = _norm_state(state)
        hid = hand_id if hand_id is not None else (stn or {}).get("hand_id")
        adv = _advice_summary(advice)
        # 상태에서: 좌석별 액션, 내 이번 스트리트 액션 수, 마지막 상대 벳
        seats, bettor, n_aggr, hab, lag = None, None, 0, None, None
        if stn:
            acts, inv_total = annotate(stn["actions"], {k: s["pos"] for k, s in stn["seats"].items()})
            seats = {}
            for k, s in stn["seats"].items():
                e = {"pos": s["pos"], "hero": k == "hero", "folded": s["folded"]}
                if k != "hero":
                    if s["truncated"] or not s["name"]:
                        e["name"], e["placeholder"] = None, f"seat-local:{k}"
                    else:
                        e["name"] = s["name"]
                e["acts"] = {st: [{x: y for x, y in r.items() if x != "seat"} for r in rows if r["seat"] == k]
                             for st, rows in acts.items() if any(r["seat"] == k for r in rows)}
                pr = _profile_summary((profiles or {}).get(k))
                if pr:
                    e["profile"] = pr
                seats[k] = e
            cur = acts.get(street) or []
            hab = sum(1 for r in cur if r["seat"] == "hero" and r["act"] != "post")
            aggr = [r for r in cur if r["act"] in ("bet", "raise") and r["seat"] != "hero"]
            n_aggr = sum(1 for r in cur if r["act"] in ("bet", "raise"))
            if aggr:
                b = aggr[-1]
                bettor = {"seat": b["seat"], "pos": stn["seats"].get(b["seat"], {}).get("pos"), "street": street,
                          "act": b["act"], "to": b.get("to"), "pct": b.get("pct")}
            if stn["street"] is not None and STREETS[min(stn["street"], 3)] != street:
                lag = STREETS[min(stn["street"], 3)]
        call = _r(ctx.get("call"))
        pot, pot_total = _r(ctx.get("pot")), _r(ctx.get("pot_total"))
        pot_before = pot if pot else (round(pot_total - (call or 0), 2) if pot_total else None)
        call_pct = round(call / pot_before, 3) if call and pot_before and pot_before > 0 else None
        stack = _num(ctx.get("my_stack")) if _num(ctx.get("my_stack")) is not None else _num(ctx.get("stack_bb"))
        pot_now = pot_total or pot
        spr = round(stack / pot_now, 2) if stack is not None and pot_now and pot_now > 0 else None
        # 같은 차례 / 중복 판단
        n_acts = tuple((st, len(rows)) for st, rows in sorted((stn or {}).get("actions", {}).items()))
        sig = (hid, street, tuple(hero), tuple(board), call, ctx.get("opponents_in_hand"), pot_total,
               adv and (adv["action"], adv["size_bb"]), n_acts)
        if sig in self._keys:
            self._keys.move_to_end(sig)
            return self._keys[sig]
        if hab is not None:
            turn = hab
        else:
            tl = self._turns.setdefault((hid, street), [])
            if tl and tl[-1]["call"] == call:
                turn = tl[-1]["turn"]
            else:
                turn = (tl[-1]["turn"] + 1) if tl else 0
                tl.append({"turn": turn, "call": call})
        rev = sum(1 for d in self._mem.get(hid, []) if d["street"] == street and d["turn"] == turn)
        self._seq[hid] += 1
        did = f"{hid}/{self._seq[hid]}" if hid else f"t{int(t * 1000)}-{self._seq[hid]}"
        tourney = {k: _prim(ctx.get(k)) for k in TOURNEY_KEYS if ctx.get(k) is not None}
        if isinstance(ctx.get("tournament"), dict):
            tourney.update({k: _prim(v) for k, v in ctx["tournament"].items() if _prim(v) is not None})
        d = {"type": "decision", "id": did, "v": SCHEMA_V, "t": round(t, 3), "day": _day(t), "hand_id": hid,
             "street": street, "turn": turn, "rev": rev, "hero_acts_before": hab, "state_street": lag,
             "hero": hero, "board": board, "tex": board_texture(board)}
        for k in CTX_DIRECT:
            if ctx.get(k) is not None and k not in ("pot", "pot_total", "call"):
                d[k] = _prim(ctx[k])
        d.update({"opponents": ctx.get("opponents_in_hand"), "pot": pot, "pot_total": pot_total, "call": call,
                  "call_pct_pot": call_pct, "spr": spr, "bettor": bettor, "n_aggr": n_aggr, "seats": seats,
                  "equity": _r(ctx.get("equity"), 4), "reads": _reads_summary(reads), "advice": adv,
                  "tourney": tourney or None})
        d = {k: v for k, v in d.items() if v is not None}
        self._append(d["day"], d)
        self._keys[sig] = did
        while len(self._keys) > MEM_KEYS:
            self._keys.popitem(last=False)
        self._remember(d)
        self.count += 1
        return did

    # --- 결과 연결 --------------------------------------------------------------------------------
    def hero_action(self, decision_id, action, size_bb=None):
        """내가 실제로 한 행동을 직접 기록 (수동/보조). 실패하면 False."""
        try:
            with self.lock:
                d = self._by_id.get(decision_id)
                act = norm_action(action)
                line = {"type": "resolution", "id": decision_id, "t": round(time.time(), 3), "src": "manual",
                        "hero_actual": {"action": act, **({"size_bb": _r(size_bb)} if size_bb is not None else {})}}
                if d and d.get("advice"):
                    line["followed"] = self._followed(d["advice"].get("action"), act)
                self._append((d or {}).get("day") or _day(time.time()), line)
                return True
        except Exception:
            self._fail("decisionlog.hero_action")
            return False

    @staticmethod
    def _followed(advised, actual):
        a, b = _cat(advised), _cat(actual)
        return None if a is None or b is None else a == b

    def link_hand(self, rec):
        """Tracker.on_hand 로 온 핸드 기록 -> 그 핸드의 결정마다 실제 행동·결과를 resolution 줄로 덧붙임. 연결한 결정 수."""
        try:
            with self.lock:
                return self._link(rec)
        except Exception:
            self._fail("decisionlog.link_hand")
            return 0

    def _decisions_for(self, rec):
        hid = rec.get("id")
        ds = list(self._mem.get(hid, [])) if hid else []
        if not ds and hid:                                   # 앱을 다시 켠 뒤 등: 파일에서 찾는다
            ds = [d for d in self._scan_days(rec) if d.get("hand_id") == hid]
        if not ds:                                           # 핸드 id 없이 기록된 결정: 카드 + 시간으로
            hc = set(rec.get("hero_cards") or [])
            t0, t1 = float(rec.get("start") or 0) - 30, float(rec.get("end") or 0) + 30
            ds = [d for d in self._mem.get(None, []) if hc and set(d.get("hero") or []) == hc and t0 <= d["t"] <= t1]
        return ds

    def _scan_days(self, rec):
        out = []
        t = float(rec.get("start") or time.time())
        now = time.time()
        for day in dict.fromkeys((_day(t - 86400), _day(t), _day(float(rec.get("end") or t)), _day(now - 86400), _day(now))):
            out += [x for x in _read_lines(self.dir / f"decisions_{day}.jsonl") if x.get("type") == "decision"]
        return out

    def _link(self, rec):
        if not isinstance(rec, dict):
            return 0
        hid = rec.get("id")
        if hid in self._linked:
            return 0
        ds = self._decisions_for(rec)
        if not ds:
            return 0
        self._linked.add(hid)
        acts, inv_total = annotate(rec.get("actions") or {}, {k: s.get("pos") for k, s in (rec.get("seats") or {}).items()})
        seats = rec.get("seats") or {}
        hero_key = next((k for k, s in seats.items() if s.get("hero")), "hero")
        outcome = self._outcome(rec, acts, inv_total, hero_key)
        n = 0
        for d in sorted(ds, key=lambda x: (x["t"], x["id"])):
            line = self._resolve_one(d, rec, acts, seats, hero_key, outcome)
            self._append(d.get("day") or _day(d["t"]), line)
            n += 1
        return n

    def _resolve_one(self, d, rec, acts, seats, hero_key, outcome):
        street = d["street"]
        rows = acts.get(street) or []
        mine = [i for i, r in enumerate(rows) if r["seat"] == hero_key and r["act"] != "post"]
        # 어느 행동이 이 결정에 대한 것인가: 그 시점의 벳/레이즈 수(n_aggr)가 같은 내 첫 행동.
        # (내 체크가 추적기에 안 보이다가 스트리트가 끝나 추정으로 들어가는 경우에도 맞음) 못 찾으면 내 n 번째 행동
        nag = d.get("n_aggr")
        cands = [i for i in mine if sum(1 for x in rows[:i] if x["act"] in ("bet", "raise")) == nag] if nag is not None else []
        idx = d.get("hero_acts_before") if d.get("hero_acts_before") is not None else d["turn"]
        line = {"type": "resolution", "id": d["id"], "t": round(time.time(), 3), "src": "tracker",
                "outcome": outcome}
        pos_of = lambda k: (seats.get(k) or {}).get("pos") or k
        if cands or idx < len(mine):
            i = cands[0] if cands else mine[idx]
            r = rows[i]
            prev_to = max([x.get("to", 0) for x in rows[:i] if x["seat"] == hero_key] + [0.0])
            if street == "preflop":
                p = (seats.get(hero_key) or {}).get("pos")
                prev_to = max(prev_to, {"SB": 0.5, "BB": 1.0}.get(p, 0.0))
            ha = {"action": r["act"], "allin": bool(r.get("allin")) or None}
            if r.get("to") is not None:
                ha.update({"to": r["to"], "size_bb": r["to"], "added": round(r["to"] - prev_to, 2)})
            if r.get("pct") is not None:
                ha["pct"] = r["pct"]
            line["hero_actual"] = {k: v for k, v in ha.items() if v is not None}
            adv = d.get("advice") or {}
            line["followed"] = self._followed(adv.get("action"), r["act"])
            if _cat(adv.get("action")) == "aggr" and _cat(r["act"]) == "aggr" and adv.get("size_bb") and r.get("to"):
                line["size_ratio"] = round(r["to"] / adv["size_bb"], 2)
            # 내 액션 뒤 같은 스트리트: 상대 반응. 이후 스트리트: 상대 액션
            later, resp, seen = [], [], set()
            for x in rows[i + 1:]:
                if x["seat"] == hero_key:
                    continue
                item = {"street": street, "pos": pos_of(x["seat"]), "act": x["act"]}
                for f in ("to", "pct"):
                    if x.get(f) is not None:
                        item[f] = x[f]
                later.append(item)
                if x["seat"] not in seen:
                    seen.add(x["seat"])
                    resp.append({"pos": item["pos"], "act": x["act"]})
            line["responses"] = resp
        else:
            line["link"] = "no_hero_action_seen"
            later = []
        si = STREETS.index(street)
        for s in STREETS[si + 1:]:
            for x in acts.get(s) or []:
                if x["seat"] == hero_key:
                    continue
                item = {"street": s, "pos": pos_of(x["seat"]), "act": x["act"]}
                for f in ("to", "pct"):
                    if x.get(f) is not None:
                        item[f] = x[f]
                later.append(item)
        line["later"] = later[:30]
        b = d.get("bettor")
        if b and si < 3:                                    # 벳한 상대가 다음 스트리트에도 이어 갔나
            nxt = [x["act"] for x in acts.get(STREETS[si + 1]) or [] if x["seat"] == b["seat"]]
            reached = bool(acts.get(STREETS[si + 1])) or STREETS.index(rec.get("streets") or "preflop") > si
            line["bettor_next"] = nxt
            if nxt:
                line["bettor_continued"] = any(a in ("bet", "raise") for a in nxt)
            elif reached:
                line["bettor_continued"] = False     # 다음 스트리트에 왔는데 액션 없음 = 이미 폴드
        return line

    def _outcome(self, rec, acts, inv_total, hero_key):
        seats = rec.get("seats") or {}
        hero = seats.get(hero_key) or {}
        issues = [i.get("code") for i in rec.get("issues") or []]
        sd = rec.get("showdown")
        shown = {}
        for k, s in seats.items():
            if k != hero_key and s.get("shown"):
                shown[s.get("pos") or k] = s["shown"]
        out = {"final_board": rec.get("board") or [], "streets": rec.get("streets"), "hero_folded": hero.get("folded"),
               "showdown": bool(sd), "confidence": rec.get("confidence"), "issues": issues}
        if shown:
            out["shown"] = shown
        out["hero_won"] = out["hero_delta_bb"] = None
        if rec.get("confidence") != "ok":
            return {k: v for k, v in out.items() if v is not None}
        inv_h = inv_total.get(hero_key, 0.0)
        others = sum(v for k, v in inv_total.items() if k != hero_key)
        live = [k for k, s in seats.items() if not s.get("folded")]
        if hero.get("folded"):
            out.update(hero_won=False, hero_delta_bb=round(-inv_h, 2), delta_src="folded")
        elif live == [hero_key]:
            out.update(hero_won=True, hero_delta_bb=round(others, 2), delta_src="uncontested")
        elif sd and len(out["final_board"]) == 5:
            res = self._showdown(rec, seats, hero_key, live)
            if res is not None:
                dl = round(others if res > 0 else -inv_h if res < 0 else 0.0, 2)
                out.update(hero_won=True if res > 0 else False if res < 0 else "split", hero_delta_bb=dl,
                           delta_src="showdown_approx")
        return {k: v for k, v in out.items() if v is not None}

    @staticmethod
    def _showdown(rec, seats, hero_key, live):
        """남은 모두의 패를 알면 1 (내가 이김) / -1 (짐) / 0 (비김). 모르면 None."""
        try:
            import evaluator as ev
            board = [ev.parse_card(c) for c in rec["board"]]
            hands = {}
            for k in live:
                cards = rec.get("hero_cards") if k == hero_key else (seats.get(k) or {}).get("shown")
                if not cards or len(cards) != 2:
                    return None
                hands[k] = ev.evaluate([ev.parse_card(c) for c in cards] + board)
            mine = hands[hero_key]
            best_other = max(v for k, v in hands.items() if k != hero_key)
            return 1 if mine > best_other else -1 if mine < best_other else 0
        except Exception:
            return None

    # --- 읽기 -------------------------------------------------------------------------------------
    def load(self, days=None):
        try:
            return load(self.dir.parent, days)
        except Exception:
            self._fail("decisionlog.load")
            return []


# --- 파일 읽기 / 분석 -------------------------------------------------------------------------------
def _read_lines(path):
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
                    continue              # 죽으면서 잘린 줄은 건너뜀
                if isinstance(x, dict):
                    out.append(x)
    except OSError:
        pass
    return out


def load(base_dir=None, days=None):
    """BASE/hands/decisions_*.jsonl -> 결정 dict 리스트 (시간순). 결정마다 'resolution' (resolution 줄들을 합친 dict) 이 붙음.
    days: 최근 N 일 (오늘 포함) 또는 날짜 문자열 리스트 ['2026-10-29'], None 이면 전부."""
    hands = Path(base_dir if base_dir is not None else BASE) / "hands"
    files = sorted(hands.glob("decisions_*.jsonl"))
    if isinstance(days, int):
        keep = {_day(time.time() - 86400 * i) for i in range(days)}
        files = [f for f in files if f.stem[len("decisions_"):] in keep]
    elif days:
        keep = set(days)
        files = [f for f in files if f.stem[len("decisions_"):] in keep]
    decisions, res = OrderedDict(), {}
    for f in files:
        for x in _read_lines(f):
            if x.get("type") == "decision" and x.get("id"):
                decisions[x["id"]] = x
            elif x.get("type") == "resolution" and x.get("id"):
                m = res.setdefault(x["id"], {"srcs": []})
                m["srcs"].append(x.get("src"))
                for k, v in x.items():
                    if k not in ("type", "id", "t", "src") and v is not None:
                        m[k] = v
    out = []
    for did, d in decisions.items():
        d = dict(d)
        if did in res:
            d["resolution"] = res[did]
        out.append(d)
    out.sort(key=lambda d: (d.get("t") or 0, d["id"]))
    return out


def final_turns(records):
    """같은 차례의 여러 개정 중 마지막 것만 (hand_id, street, turn 별)."""
    last = OrderedDict()
    for d in records:
        last[(d.get("hand_id"), d.get("street"), d.get("turn"))] = d
    return list(last.values())


def _rate(a, b):
    return round(a / b, 3) if b else None


def summarize(records):
    """결정 리스트(load 결과) -> 분석용 요약 dict.
    n_turns / n_resolved / follow_rate(전체·추천 행동별·신뢰도별) / avg_equity_by_advice /
    facing[street|벳 크기|텍스처]: 상대 벳을 마주한 결정들 (벳한 상대가 다음 스트리트에도 이어갔나, 쇼다운·내 승률)
    hero_bets[street|내 크기|텍스처]: 내가 벳/레이즈했을 때 상대 반응 (fold/call/raise 비율)
    called_realized: 콜한 결정의 평균 에쿼티 대 실제 승률(결과를 아는 것만)"""
    turns = final_turns(records)
    out = {"n_decisions": len(records), "n_turns": len(turns), "n_resolved": 0, "follow_rate": None,
           "follow_by_advice": {}, "follow_by_confidence": {}, "avg_equity_by_advice": {}, "facing": {},
           "hero_bets": {}, "called_realized": None}
    fol = [0, 0]
    by_adv, by_conf, eq = {}, {}, {}
    facing, hbets = {}, {}
    cr = {"n": 0, "known": 0, "won": 0, "eq": 0.0}
    for d in turns:
        res = d.get("resolution") or {}
        adv = d.get("advice") or {}
        a = adv.get("action")
        if res.get("hero_actual"):
            out["n_resolved"] += 1
        if d.get("equity") is not None and a:
            e = eq.setdefault(a, [0, 0.0])
            e[0] += 1
            e[1] += d["equity"]
        fw = res.get("followed")
        if fw is not None:
            fol[0] += int(fw)
            fol[1] += 1
            for tbl, key in ((by_adv, a), (by_conf, adv.get("confidence"))):
                c = tbl.setdefault(key, {"n": 0, "followed": 0, "actual": Counter()})
                c["n"] += 1
                c["followed"] += int(fw)
                c["actual"][(res.get("hero_actual") or {}).get("action")] += 1
        tex = (d.get("tex") or {}).get("bucket") or "preflop"
        won = (res.get("outcome") or {}).get("hero_won")
        ha = res.get("hero_actual") or {}
        if d.get("street") != "preflop" or d.get("bettor"):
            pct = d.get("call_pct_pot") if d.get("call_pct_pot") is not None else (d.get("bettor") or {}).get("pct")
            if d.get("bettor") or d.get("call"):
                k = f"{d.get('street')}|{size_bucket(pct)}|{tex}"
                c = facing.setdefault(k, {"n": 0, "bettor_known": 0, "bettor_continued": 0, "hero_fold": 0,
                                          "showdown": 0, "won_known": 0, "won": 0})
                c["n"] += 1
                if res.get("bettor_continued") is not None:
                    c["bettor_known"] += 1
                    c["bettor_continued"] += int(bool(res["bettor_continued"]))
                c["hero_fold"] += int(ha.get("action") == "fold")
                oc = res.get("outcome") or {}
                c["showdown"] += int(bool(oc.get("showdown")))
                if won in (True, False) and ha.get("action") != "fold":
                    c["won_known"] += 1
                    c["won"] += int(won is True)
        if _cat(ha.get("action")) == "aggr" and res.get("responses") is not None:
            k = f"{d.get('street')}|{size_bucket(ha.get('pct'))}|{tex}"
            c = hbets.setdefault(k, {"n": 0, "fold": 0, "call": 0, "raise": 0, "no_response": 0})
            c["n"] += 1
            first = [r["act"] for r in res["responses"]]
            if not first:
                c["no_response"] += 1
            for act in first:
                if act == "fold":
                    c["fold"] += 1
                elif act in ("call", "check"):
                    c["call"] += 1
                elif act in ("raise", "bet"):
                    c["raise"] += 1
        if a == "call" and ha.get("action") == "call" and d.get("equity") is not None:
            cr["n"] += 1
            cr["eq"] += d["equity"]
            if won in (True, False):
                cr["known"] += 1
                cr["won"] += int(won is True)
    out["follow_rate"] = _rate(fol[0], fol[1])
    for name, tbl in (("follow_by_advice", by_adv), ("follow_by_confidence", by_conf)):
        out[name] = {str(k): {"n": c["n"], "rate": _rate(c["followed"], c["n"]), "actual": dict(c["actual"])}
                     for k, c in tbl.items()}
    out["avg_equity_by_advice"] = {k: {"n": n, "equity": round(s / n, 3)} for k, (n, s) in eq.items()}
    for k, c in facing.items():
        c["bettor_continue_rate"] = _rate(c["bettor_continued"], c["bettor_known"])
        c["win_rate_known"] = _rate(c["won"], c["won_known"])
    for k, c in hbets.items():
        c["fold_rate"] = _rate(c["fold"], c["n"])
    out["facing"], out["hero_bets"] = dict(sorted(facing.items())), dict(sorted(hbets.items()))
    if cr["n"]:
        out["called_realized"] = {"n": cr["n"], "avg_equity": round(cr["eq"] / cr["n"], 3), "known": cr["known"],
                                  "win_rate": _rate(cr["won"], cr["known"])}
    return out


if __name__ == "__main__":  # python decisionlog.py [일수]  -> 요약 JSON
    recs = load(BASE, int(sys.argv[1]) if len(sys.argv) > 1 else None)
    print(json.dumps(summarize(recs), ensure_ascii=False, indent=1))
