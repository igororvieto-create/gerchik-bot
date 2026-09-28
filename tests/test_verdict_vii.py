"""Инструмент вердикта замера VII. Проверяется на синтетике ДО единственного
взгляда на реальные данные: ошибка в нём там означала бы испорченный замер
без права повтора."""
import pytest

from tools import verdict_vii as v


def _row(direction, prior, outcome, i, ts="2026-09-20T00:00:00", rr=2.0):
    return {"symbol": f"S{i}USDT", "direction": direction,
            "mkt_prior_pct": prior, "outcome": outcome, "rr": rr,
            "sl_pct": 2.0, "funding": 0.0, "ts": ts}


def test_groups_follow_the_sign_of_prior_drift():
    assert v.group_of(_row("LONG", 2.0, "WIN", 0)) == "по дрейфу"
    assert v.group_of(_row("SHORT", -2.0, "WIN", 0)) == "по дрейфу"
    assert v.group_of(_row("LONG", -2.0, "WIN", 0)) == "против дрейфа"
    assert v.group_of(_row("SHORT", 2.0, "WIN", 0)) == "против дрейфа"
    assert v.group_of(_row("LONG", 0.5, "WIN", 0)) is None, "боковик вошёл в проверку"
    assert v.group_of(_row("LONG", None, "WIN", 0)) is None


def test_row_r_books_win_loss_and_costs():
    fee = v.ROUND_TRIP_FEE_PCT / 2.0
    assert v.row_r(_row("LONG", 2, "WIN", 0, rr=2.0)) == pytest.approx(2.0 - fee)
    assert v.row_r(_row("LONG", 2, "LOSS", 0)) == pytest.approx(-1.0 - fee)
    assert v.row_r(_row("LONG", 2, "BE", 0)) == pytest.approx(0.0 - fee)
    assert v.row_r(_row("LONG", 2, "EXPIRED", 0)) is None


def test_population_excludes_signals_from_before_the_hypothesis():
    rows = [_row("LONG", 2, "WIN", 0, ts="2026-09-10T00:00:00"),
            _row("LONG", 2, "WIN", 1, ts="2026-09-20T00:00:00")]
    pop = v.population(rows)
    assert len(pop) == 1 and pop[0]["symbol"] == "S1USDT", \
        "в проверку попал исход, увиденный при рождении гипотезы"


def _write(tmp_path, rows):
    import json
    p = tmp_path / "sig.json"
    p.write_text(json.dumps({"signals": rows}), encoding="utf-8")
    return str(p)


def _run(path, capsys, monkeypatch):
    monkeypatch.setattr("sys.argv", ["verdict_vii", path])
    v.main()
    return capsys.readouterr().out


def test_two_losing_groups_do_not_pass_just_because_one_is_less_bad(
        tmp_path, capsys, monkeypatch):
    """Ради этого условия в планке: две убыточные группы тоже различаются
    между собой, а торговать надо прибыльную."""
    rows = []
    for i in range(80):      # по дрейфу: 30% побед при 2R → ev_r ≈ −0.1
        rows.append(_row("LONG", 2.0, "WIN" if i % 10 < 3 else "LOSS", i))
    for i in range(80, 200):  # против: 10% побед → ev_r ≈ −0.7
        rows.append(_row("LONG", -2.0, "WIN" if i % 10 < 1 else "LOSS", i))
    out = _run(_write(tmp_path, rows), capsys, monkeypatch)
    assert "ВЕРДИКТ: НЕ ПРОШЛА" in out, "менее убыточная группа прошла планку"


def test_a_clearly_profitable_group_passes(tmp_path, capsys, monkeypatch):
    rows = []
    for i in range(100):     # по дрейфу: 60% побед при 2R → ev_r ≈ +0.8
        rows.append(_row("SHORT", -3.0, "WIN" if i % 10 < 6 else "LOSS", i))
    for i in range(100, 200):  # против: 20% → ev_r ≈ −0.4
        rows.append(_row("SHORT", 3.0, "WIN" if i % 10 < 2 else "LOSS", i))
    out = _run(_write(tmp_path, rows), capsys, monkeypatch)
    assert "ВЕРДИКТ: ПРОШЛА" in out, out[-600:]


def test_too_few_outcomes_do_not_pass(tmp_path, capsys, monkeypatch):
    rows = [_row("SHORT", -3.0, "WIN" if i % 10 < 6 else "LOSS", i)
            for i in range(50)]
    rows += [_row("SHORT", 3.0, "LOSS", i) for i in range(50, 90)]
    out = _run(_write(tmp_path, rows), capsys, monkeypatch)
    assert "ВЕРДИКТ: НЕ ПРОШЛА" in out, "прошло при 90 исходах из 120"


def test_positive_but_noisy_group_does_not_pass(tmp_path, capsys, monkeypatch):
    """Среднее положительно, разница с «против» значима, выборки хватает —
    но нижняя граница CI95 группы ниже нуля. Обязано НЕ пройти.

    Без этого сценария условие на CI не проверялось вовсе: в остальных
    тестах вместе с ним проваливалось и условие «ev_r > 0», и снятие любого
    одного ничего не меняло (мутация выжила). Условие «ev_r > 0» при этом
    логически следует из «CI > 0» и отдельно не проверяемо — эквивалентный
    мутант, оставлен в коде только потому, что так записана планка."""
    rows = []
    for i in range(60):       # по дрейфу: 36% побед при 2R → ev ≈ +0.03, шумно
        rows.append(_row("LONG", 2.0, "WIN" if (i * 9) % 25 < 9 else "LOSS", i))
    for i in range(60, 160):  # против: одни поражения → разница значима
        rows.append(_row("LONG", -2.0, "LOSS", i))
    out = _run(_write(tmp_path, rows), capsys, monkeypatch)
    assert "✓ ev_r группы «по дрейфу» > 0" in out, "сценарий не тот: среднее не > 0"
    assert "✗ нижняя граница CI95 этой группы > 0" in out, "сценарий не тот"
    assert "✓ разница с «против» > 0" in out, "сценарий не тот: разница незначима"
    assert "ВЕРДИКТ: НЕ ПРОШЛА" in out, "шумная группа прошла, CI не проверяется"
