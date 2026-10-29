"""팟 오즈, 내재 배당, SPR, 폴드 에쿼티, M-ratio 계산.

금액 단위는 자유 (칩, BB 등) — 입력끼리 단위만 같으면 된다.
"""
import re


def parse_amount(text):
    """'13만', '1.5만', '12,000', '21', '2k' → float. 빈 값/잘못된 값은 None."""
    text = text.strip().replace(",", "").replace(" ", "").lower()
    if not text:
        return None
    # 억·만 단위를 섞어 쓴 금액: '84만 7500', '1억 2000만', '1억2000만 5000', '3억 5000'
    m = re.fullmatch(r"(?:(\d+(?:\.\d+)?)억)?(?:(\d+(?:\.\d+)?)만)?(\d+)?", text)
    if m and sum(g is not None for g in m.groups()) >= 2 and (m.group(1) or m.group(2)) \
            and len(m.group(3) or "") <= (4 if m.group(2) else 8):
        return sum(float(g) * u for g, u in zip(m.groups(), (1e8, 1e4, 1)) if g)
    m = re.fullmatch(r"(\d+(?:\.\d+)?)(만|억|k|bb)?", text)
    if not m:
        return None
    value = float(m.group(1))
    return value * {"만": 1e4, "억": 1e8, "k": 1e3}.get(m.group(2), 1)


def format_amount(value):
    if value >= 1e8:
        return f"{value / 1e8:.2f}".rstrip("0").rstrip(".") + "억"
    if value >= 1e4:  # 소수 둘째 자리까지 ('1.23만') — 한 자리면 입력칸에 되써질 때 12345 → 1.2만 = 12000 으로 2.8% 틀어졌다
        return f"{value / 1e4:.2f}".rstrip("0").rstrip(".") + "만"
    return f"{value:,.0f}" if value == int(value) else f"{value:,.1f}"


def rule_of_4_2(num_outs, cards_to_come):
    """4-2 법칙 근사 (%). 2장 남음 → ×4, 1장 남음 → ×2."""
    return num_outs * (4 if cards_to_come == 2 else 2)


def pot_odds(pot, bet, call=None):
    """필요 승률 = 콜 금액 / (현재 팟 + 상대 벳 + 콜 금액)."""
    call = bet if call is None else call
    total = pot + bet + call
    return call / total if total > 0 else 0.0


def implied_needed(pot, bet, hit_prob, call=None):
    """팟 오즈가 부족할 때, 패 완성 시 추가로 따내야 하는 금액 (0이면 지금 팟만으로 충분).

    본전 조건: p × (pot + bet + 추가) = (1 - p) × call
    """
    call = bet if call is None else call
    if hit_prob <= 0:
        return float("inf")
    return max(0.0, call * (1 - hit_prob) / hit_prob - (pot + bet))


def spr(effective_stack, pot):
    return effective_stack / pot if pot > 0 else float("inf")


def spr_zone(value):
    """(구간 이름, 전략 요약)"""
    if value < 4:
        return "Low SPR", "탑페어 좋은 키커(TPTK)·오버페어로도 올인 승부(커밋)하는 구간"
    if value < 7:
        return "Medium SPR", "원페어는 팟 컨트롤, 투페어·셋 이상일 때 올인을 노리는 구간"
    if value < 10:
        return "Medium~High SPR", "원페어 가치가 떨어지기 시작 — 강한 메이드 핸드 위주로 스택을 건다"
    return "High SPR (딥스택)", "탑페어는 블러프 캐처 수준 — 셋·너트 플러시·너트 스트레이트 위주로 올인"


def breakeven_fold(pot, bet):
    """순수 블러프가 본전이 되는 상대 폴드율 = 벳 / (팟 + 벳)."""
    return bet / (pot + bet) if pot + bet > 0 else 0.0


def bet_ev(pot, bet, fold_prob, equity_when_called):
    """벳 EV = 폴드율 × 팟 + 콜될 확률 × (에쿼티 × (팟 + 2×벳) - 벳)."""
    return fold_prob * pot + (1 - fold_prob) * (equity_when_called * (pot + 2 * bet) - bet)


def m_ratio(stack, sb, bb, ante_total=0.0):
    orbit = sb + bb + ante_total
    return stack / orbit if orbit > 0 else float("inf")


def m_zone(value):
    if value < 5:
        return "푸시/폴드 구간", "오픈 레이즈 불가 — 오픈 올인 또는 폴드"
    if value <= 10:
        return "리스틸 구간", "리스틸 3벳 셔브 타깃 구간"
    if value <= 20:
        return "중간 스택", "운영은 가능하지만 블라인드 압박을 의식해야 하는 구간"
    return "여유 스택", "자유로운 포스트플랍 운영 구간"


def set_mining_ratio(effective_stack, call):
    """셋마이닝: 유효 스택이 콜 금액의 몇 배인지 (15~20배 이상 권장)."""
    return effective_stack / call if call > 0 else float("inf")
