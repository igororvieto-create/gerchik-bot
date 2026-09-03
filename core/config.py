import logging
import math
import os
from dataclasses import dataclass, fields, field
from typing import List

_log = logging.getLogger("config")

_TRUE  = ("true", "1", "yes", "on", "y")
_FALSE = ("false", "0", "no", "off", "n")


def _env_int(name: str, default: int) -> int:
    """Разбор env с фолбэком. Раньше int(os.getenv(...)) стоял прямо в
    дефолте dataclass: заданная, но ПУСТАЯ переменная в Railway роняла
    импорт core.config, а значит и всё приложение — crash-loop без логов.

    OverflowError ловится наравне с ValueError: 'inf' проходит float(), но
    int(inf) бросает — и это снова был бы crash-loop без внятного лога."""
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        val = float(raw)
        if not math.isfinite(val):
            raise ValueError("не конечное число")
        return int(val)
    except (ValueError, OverflowError):
        _log.error(f"{name}={raw!r} — не целое число, беру {default}")
        return default


def _env_float(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        val = float(raw)
    except ValueError:
        _log.error(f"{name}={raw!r} — не число, беру {default}")
        return default
    if not math.isfinite(val):
        # nan проскакивал мимо _clamp: любое сравнение с nan ложно, поэтому
        # и потолок риска, и связь RISK×MAX_POSITIONS молча пропускали его,
        # а размер позиции дальше считался как nan на КАЖДОМ входе.
        _log.error(f"{name}={raw!r} — не конечное число, беру {default}")
        return default
    return val


def _env_bool(name: str, default: bool) -> bool:
    """Нераспознанное значение раньше превращалось в False, а не в дефолт:
    ABORT_ON_LEVERAGE_FAIL=enabled бесшумно разрешал вход при неудачной
    установке плеча, хотя сайзинг считался под известное плечо."""
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    _log.error(f"{name}={raw!r} — не булево значение, беру {default}")
    return default


@dataclass
class Config:
    BYBIT_API_KEY: str = os.getenv("BYBIT_API_KEY", "").strip()
    BYBIT_SECRET:  str = os.getenv("BYBIT_SECRET",  "").strip()
    NTFY_URL:      str = os.getenv("NTFY_URL",      "").strip()

    # Scanning
    SCAN_INTERVAL_MIN: int   = _env_int("SCAN_INTERVAL_MIN", 4)
    SCAN_BATCH_SIZE:   int   = _env_int("SCAN_BATCH_SIZE", 10)
    SCAN_BATCH_DELAY:  float = _env_float("SCAN_BATCH_DELAY", 0.5)
    TOP_N_PAIRS:       int   = _env_int("TOP_N_PAIRS", 100)

    BLACKLIST: List[str] = field(
        default_factory=lambda: [
            s.strip() for s in os.getenv("BLACKLIST", "LUNAUSDT,FTTUSDT").split(",") if s.strip()
        ]
    )

    # Signal thresholds
    MIN_SCORE:              int   = _env_int("MIN_SCORE", 30)
    OI_CHANGE_THRESHOLD:    float = _env_float("OI_CHANGE_THRESHOLD", 2.0)
    VOL_SPIKE_MULT:         float = _env_float("VOL_SPIKE_MULT", 1.5)
    FUNDING_EXTREME:        float = _env_float("FUNDING_EXTREME", 0.03)
    PRICE_CHANGE_MIN:       float = _env_float("PRICE_CHANGE_MIN", 0.3)
    OB_IMBALANCE_THRESHOLD: float = _env_float("OB_IMBALANCE_THRESHOLD", 0.10)
    MIN_VOL_24H:            float = _env_float("MIN_VOL_24H", 2000000)
    SIGNAL_COOLDOWN_MIN:    int   = _env_int("SIGNAL_COOLDOWN_MIN", 60)

    # Gerchik methodology: key levels, MTF, R:R
    # 1.5: пол риска (0.75 ATR) + буфер делают связку "цена у уровня" и
    # "2R до противоположного уровня" почти неразрешимой — нужно 1.5-2.9 ATR
    # чистого хода до ближайшего пивота, что в окне из 20 свечей редкость.
    # Сетка TP остаётся 1R/2R/3R, торговая цель по-прежнему TP2 = 2R.
    MIN_RR:             float = _env_float("MIN_RR", 1.5)
    # Порог ПОКАЗА (MIN_RR) и порог ТОРГОВЛИ — разные вещи. Торговая цель одна:
    # TP2 = 2R. При запасе до встречного уровня 1.5-2.0R цель стоит ЗА уровнем,
    # от которого сделка и рассчитывает оттолкнуться: тезис отрабатывает на
    # 1.7R, WIN засчитывается только на 2R, а отбой от уровня даёт LOSS или
    # EXPIRED. Такая сделка не может выиграть по построению, поэтому торгуем
    # только при запасе >= 2R, а сигналы 1.5-2.0R остаются информационными.
    MIN_TRADE_HEADROOM_R: float = _env_float("MIN_TRADE_HEADROOM_R", 2.0)
    # Перенос стопа в безубыток после хода в плюс на BREAKEVEN_AT_R.
    # ПО УМОЛЧАНИЮ ВЫКЛЮЧЕН (0.0).
    #
    # Механизм предлагался как способ получить плюс без преимущества сетапа:
    # 14.7% сигналов доходят до +1R и возвращаются в стоп, и перенос делает
    # их нулём вместо убытка. Но этот расчёт учитывает только убытки,
    # ставшие безубытком, и игнорирует победы, которые перенос убивает:
    # путь 0 -> +1R -> откат к БУ -> +2R без переноса был бы WIN, с
    # переносом становится нулём.
    #
    # Поминутная симуляция (20 000 путей, честные хай/лоу 15м-баров):
    #   без сноса:      EV +0.023R без переноса / +0.009R с переносом
    #   снос +0.3R/48ч: EV +0.146R без переноса / +0.126R с переносом
    # То есть преимущества перенос не создаёт, а при наличии преимущества
    # его уменьшает. Это согласуется с теоремой об остановке: для процесса
    # без сноса НИКАКОЕ правило выхода не создаёт матожидания.
    #
    # Механизм оставлен рабочим и протестированным: он снижает разброс и
    # просадку (реальная польза для управления риском, но не для EV), и
    # включается одной переменной, когда накопленные mfe_r это оправдают.
    BREAKEVEN_AT_R: float = _env_float("BREAKEVEN_AT_R", 0.0)
    # Стоп переносится не ровно на вход, а на вход + издержки круга:
    # тейкер 0.055% × 2 входа/выхода. Стоп ровно на входе давал бы минус
    # на комиссиях при каждом срабатывании.
    BREAKEVEN_FEE_PCT: float = _env_float("BREAKEVEN_FEE_PCT", 0.12)
    KEY_LEVEL_LOOKBACK: int   = _env_int("KEY_LEVEL_LOOKBACK", 20)
    KEY_LEVEL_WING:     int   = _env_int("KEY_LEVEL_WING", 2)
    KEY_LEVEL_ATR_MULT: float = _env_float("KEY_LEVEL_ATR_MULT", 1.2)
    # Пивоты ближе этого расстояния к цене — рыночный шум, а не уровень
    LEVEL_NOISE_ATR:    float = _env_float("LEVEL_NOISE_ATR", 0.5)
    # Потолок ширины стопа в ATR — защита от абсурдно широких стопов
    MAX_SL_ATR:         float = _env_float("MAX_SL_ATR", 3.5)
    # Разворот должен входить рядом с сетапом, а не через несколько ATR
    REVERSAL_MAX_DRIFT_ATR: float = _env_float("REVERSAL_MAX_DRIFT_ATR", 1.0)
    REQUIRE_MTF_ALIGN:  bool  = _env_bool("REQUIRE_MTF_ALIGN", True)
    MTF_TREND_LOOKBACK: int   = _env_int("MTF_TREND_LOOKBACK", 6)
    MIN_LISTING_AGE_DAYS: int = _env_int("MIN_LISTING_AGE_DAYS", 14)
    # Глубина ленты сделок. 0 отключает запрос целиком.
    #
    # ГЛУБИНА 1000 ДОПУСТИМА ТОЛЬКО ПОТОМУ, что лента запрашивается лишь
    # для символов, где сигнал УЖЕ состоялся (strategy/scanner.py, перед
    # построением Signal), а не для всех ста на каждом скане.
    #
    # Прежний расчёт остаётся в силе и объясняет, почему это важно: ответ
    # recent-trade весит ~141 байт на сделку. Запрос на каждый символ
    # каждые 4 минуты давал 0.62 ГБ/сутки при глубине 100 и дал бы ~5
    # ГБ/сутки при 1000. Прокси Webshare тарифицируются по трафику, и
    # дальше цепочка механическая: 402/403 -> перебор прокси -> прямое
    # соединение с IP Railway -> гео-блок Bybit -> get_positions() = None
    # -> НЕПРЕРЫВНАЯ проверка наличия стопа прекращается. То есть
    # рецидивирующий баг №1, вызванный квотой на трафик.
    #
    # Сигналов около 50 в сутки, поэтому запрос под них — ~7 МБ/сутки,
    # в сто раз МЕНЬШЕ прежнего. Если поток когда-нибудь войдёт в решение
    # об отборе, запрос придётся вернуть в общий сбор — и вместе с ним
    # вернуть эту арифметику.
    #
    # Само значение 1000 выбрано замером, а не с запасом: лента покрывала
    # минимум минуту у 46% сигналов при 100, у 80% при 500, у 89% при 1000.
    # Отбраковка «короткая лента» оказалась систематической: сто сделок
    # покрывают тем меньше времени, чем сильнее всплеск, то есть ровно на
    # климаксе. У отброшенных ATR 9.9% против 4.1%, score 46.8 против 35.8.
    TRADE_FLOW_LIMIT: int = _env_int("TRADE_FLOW_LIMIT", 1000)
    MAX_LAST_CANDLE_ATR:  float = _env_float("MAX_LAST_CANDLE_ATR", 2.0)
    # Два переключателя структуры стратегии. Значения по умолчанию РАВНЫ
    # сегодняшнему поведению — боевой бот не меняется. Нужны для того,
    # чтобы tools/replay.py мог измерить варианты, а не чтобы их крутить.
    #
    # FUNDING_VOTE: фандинг участвует в голосовании за направление. Он
    # задаёт сторону примерно 2/3 сделок (фандинг чаще положителен, голос
    # контрарный -> постоянный SHORT), а литературной опоры под ним нет:
    # это переменная базиса, не прогноз (docs/LITERATURE.md §4).
    FUNDING_VOTE:         bool  = _env_bool("FUNDING_VOTE", True)
    # VSA_SPIKE_EXEMPT: VSA-развороты освобождены от анти-спайк гейта.
    # Из-за этого бот входит против вертикальных свечей — при объёме 36x
    # среднего и OI +37% за 4 часа, то есть ровно в новостной спайк,
    # который запрещает инвариант в CLAUDE.md.
    VSA_SPIKE_EXEMPT:     bool  = _env_bool("VSA_SPIKE_EXEMPT", True)
    # Освобождений у VSA-разворотов ТРИ, и вводились они вместе: анти-спайк,
    # MTF-фильтр и требование близости к уровню. Проверять их поодиночке
    # бессмысленно — гипотеза «освобождения и есть проблема» цельная.
    VSA_MTF_EXEMPT:       bool  = _env_bool("VSA_MTF_EXEMPT", True)
    VSA_LEVEL_EXEMPT:     bool  = _env_bool("VSA_LEVEL_EXEMPT", True)

    # Signal history
    # 5000 ≈ 8 суток потока сигналов: лимит не должен обрезать семидневную
    # выборку винрейта (500 выбирались за ~5 дней и статистика теряла хвост)
    # Ярлык стратегии. Статистика считается ТОЛЬКО по сигналам с текущим
    # ярлыком, поэтому при переходе на другую стратегию старые исходы не
    # смешиваются с новыми — и при этом не стираются, а остаются для
    # сравнения. Менять базу вручную не нужно: достаточно задать новое
    # значение переменной.
    STRATEGY_ID: str = (os.getenv("STRATEGY_ID", "vsa-v1") or "vsa-v1").strip()
    MAX_SIGNALS_DB:  int = _env_int("MAX_SIGNALS_DB", 5000)
    SIGNAL_TTL_HOURS: int = _env_int("SIGNAL_TTL_HOURS", 24)

    # ── Auto-trading ──────────────────────────────────────────────────────────
    AUTO_TRADE:      bool  = _env_bool("AUTO_TRADE", False)
    RISK_PER_TRADE:  float = _env_float("RISK_PER_TRADE", 1.0)
    MAX_MARGIN_PCT:  float = _env_float("MAX_MARGIN_PCT", 10.0)
    MAX_POSITIONS:   int   = _env_int("MAX_POSITIONS", 3)
    LEVERAGE:        int   = _env_int("LEVERAGE", 5)
    # 45, а не 60: шкала score дискретна, и при ΔOI<5% потолок = 59 даже при
    # идеальных объёме/фандинге/стакане. Порог 60 отбирал ИСКЛЮЧИТЕЛЬНО
    # импульсные разгоны с ΔOI≥6% — ровно тот вход вдогонку, что дал 1W/14L.
    TRADE_MIN_SCORE: int   = _env_int("TRADE_MIN_SCORE", 45)

    # Risk guards
    MAX_SAME_DIRECTION:     int   = _env_int("MAX_SAME_DIRECTION", 2)
    # 6% допускает 2 стопа при риске 3% или 6 стопов при риске 1%
    DAILY_LOSS_LIMIT_PCT:   float = _env_float("DAILY_LOSS_LIMIT_PCT", 6.0)
    ABORT_ON_LEVERAGE_FAIL: bool  = _env_bool("ABORT_ON_LEVERAGE_FAIL", True)


def _clamp(value, lo, hi, name: str):
    """Жёсткое ограничение инвариантов проекта на уровне конфига.
    Раньше потолки (риск 1-3%, плечо ≤5x) проверялись ТОЛЬКО в /api/settings —
    опечатка в переменной окружения Railway (RISK_PER_TRADE=10) проходила
    насквозь до расчёта размера позиции без единой проверки."""
    if value < lo or value > hi:
        clamped = max(lo, min(hi, value))
        _log.error(
            f"{name}={value} вне допустимого диапазона [{lo}, {hi}] — принудительно {clamped}"
        )
        return clamped
    return value


cfg = Config()

# Инварианты CLAUDE.md — нарушить нельзя ни через env, ни через API
cfg.RISK_PER_TRADE       = _clamp(cfg.RISK_PER_TRADE,       0.1, 3.0,  "RISK_PER_TRADE")
cfg.LEVERAGE             = int(_clamp(cfg.LEVERAGE,           1,   5,  "LEVERAGE"))
cfg.MAX_POSITIONS        = int(_clamp(cfg.MAX_POSITIONS,      1,  20,  "MAX_POSITIONS"))
cfg.MAX_MARGIN_PCT       = _clamp(cfg.MAX_MARGIN_PCT,       1.0, 50.0, "MAX_MARGIN_PCT")
cfg.DAILY_LOSS_LIMIT_PCT = _clamp(cfg.DAILY_LOSS_LIMIT_PCT, 1.0, 20.0, "DAILY_LOSS_LIMIT_PCT")

# Параметры нагрузки на API. SCAN_INTERVAL_MIN=0 (например, из "0.5", которое
# _env_int усекает до нуля) APScheduler молча превращает в интервал 1 СЕКУНДА,
# а это TOP_N_PAIRS×4 ≈ 400 запросов к Bybit в секунду — гарантированный бан.
cfg.SCAN_INTERVAL_MIN   = int(_clamp(cfg.SCAN_INTERVAL_MIN,   1,    60, "SCAN_INTERVAL_MIN"))
cfg.TOP_N_PAIRS         = int(_clamp(cfg.TOP_N_PAIRS,         5,   500, "TOP_N_PAIRS"))
cfg.SCAN_BATCH_SIZE     = int(_clamp(cfg.SCAN_BATCH_SIZE,     1,    50, "SCAN_BATCH_SIZE"))
cfg.SCAN_BATCH_DELAY    = _clamp(cfg.SCAN_BATCH_DELAY,      0.0,  10.0, "SCAN_BATCH_DELAY")
cfg.SIGNAL_COOLDOWN_MIN = int(_clamp(cfg.SIGNAL_COOLDOWN_MIN, 0,  1440, "SIGNAL_COOLDOWN_MIN"))
# MAX_SIGNALS_DB — единственный параметр, который НАПРЯМУЮ УДАЛЯЕТ данные:
# cleanup_old_signals режет решённые сигналы до этого числа, не глядя на
# возраст, и ходит по крону каждые 6 часов. Клампа у него не было, и
# опечатка вроде MAX_SIGNALS_DB=3 уничтожает всю статистику форвард-теста
# за один прогон, оставляя в логе только «Cleanup: removed 10 old signals».
# Нижняя граница 1000 — это больше 7-суточной витрины при любом потоке.
cfg.MAX_SIGNALS_DB      = int(_clamp(cfg.MAX_SIGNALS_DB, 1000, 200_000, "MAX_SIGNALS_DB"))
cfg.TRADE_FLOW_LIMIT    = int(_clamp(cfg.TRADE_FLOW_LIMIT,    0,  1000, "TRADE_FLOW_LIMIT"))

# Связь из docs/REVIEW.md §2, которую поштучные клампы не ловят: порог показа
# выше торгового делает торговый порог фиктивным (сигналов ниже него просто не
# существует, и enter_trade не отсекает ничего). В /api/settings это проверяется,
# а через env проходило молча.
if cfg.MIN_SCORE > cfg.TRADE_MIN_SCORE:
    _log.error(
        f"MIN_SCORE({cfg.MIN_SCORE}) > TRADE_MIN_SCORE({cfg.TRADE_MIN_SCORE}) — "
        f"торговый порог стал бы фиктивным, поднимаю его до {cfg.MIN_SCORE}"
    )
    cfg.TRADE_MIN_SCORE = cfg.MIN_SCORE

# Параметры, охраняющие инвариант «не торговать листинги/новостные спайки»
# и геометрию сделки. Клампов у них не было вовсе: одна env-переменная молча
# отключала инвариант целиком, без единой строки в логе. Проверено:
# MIN_LISTING_AGE_DAYS=0 -> age_days >= 0 истинно всегда, листинг первого дня
# торгуется; MAX_LAST_CANDLE_ATR=1e9 -> анти-спайк не срабатывает никогда;
# MIN_RR=0 -> гейт R:R отключён, и через связь ниже обнуляется торговый порог
# запаса. RISK_PER_TRADE=10 этот же путь ловит и логирует, а эти — нет.
cfg.MIN_LISTING_AGE_DAYS = int(_clamp(cfg.MIN_LISTING_AGE_DAYS, 1, 365,
                                      "MIN_LISTING_AGE_DAYS"))
cfg.MAX_LAST_CANDLE_ATR  = _clamp(cfg.MAX_LAST_CANDLE_ATR, 1.0, 10.0,
                                  "MAX_LAST_CANDLE_ATR")
cfg.MIN_RR               = _clamp(cfg.MIN_RR, 1.0, 10.0, "MIN_RR")
cfg.MAX_SL_ATR           = _clamp(cfg.MAX_SL_ATR, 1.0, 10.0, "MAX_SL_ATR")

# Безубыток должен наступать РАНЬШЕ цели, иначе механизм недостижим
cfg.BREAKEVEN_AT_R    = _clamp(cfg.BREAKEVEN_AT_R,    0.0, 1.9, "BREAKEVEN_AT_R")
cfg.BREAKEVEN_FEE_PCT = _clamp(cfg.BREAKEVEN_FEE_PCT, 0.0, 1.0, "BREAKEVEN_FEE_PCT")
cfg.MIN_TRADE_HEADROOM_R = _clamp(cfg.MIN_TRADE_HEADROOM_R, cfg.MIN_RR, 10.0,
                                  "MIN_TRADE_HEADROOM_R")

# Связь из docs/REVIEW.md §2: полный набор позиций не должен пробивать
# дневной лимит. Поштучные клампы это не ловили: MAX_POSITIONS=10 при риске
# 1% даёт 10% одновременного риска при лимите 6% — предохранитель сработал
# бы уже после того, как убыток его превысил.
_worst = cfg.RISK_PER_TRADE * cfg.MAX_POSITIONS
if _worst > cfg.DAILY_LOSS_LIMIT_PCT:
    import logging as _lg
    _new_max = max(1, int(cfg.DAILY_LOSS_LIMIT_PCT // cfg.RISK_PER_TRADE))
    _lg.getLogger("config").error(
        f"RISK_PER_TRADE({cfg.RISK_PER_TRADE}%) × MAX_POSITIONS({cfg.MAX_POSITIONS}) "
        f"= {_worst}% превышает DAILY_LOSS_LIMIT_PCT({cfg.DAILY_LOSS_LIMIT_PCT}%) — "
        f"MAX_POSITIONS снижен до {_new_max}"
    )
    cfg.MAX_POSITIONS = _new_max


# ── Опечатки в ИМЕНАХ переменных окружения ──────────────────────────────────
#
# Реальный случай: переменная была названа " DASHBOARD_TOKEN" — с пробелом в
# начале. os.getenv("DASHBOARD_TOKEN") вернул пустоту, защита дашборда молча
# выключилась, и мутирующие эндпоинты боевого бота с ключами биржи оказались
# открыты всем, у кого есть ссылка. Ни одной ошибки при этом не появилось:
# для кода переменной просто не существовало.
#
# Значение мы не проверяем — оно может быть любым. Проверяем ИМЯ: пробелы по
# краям не бывают намеренными, а расхождение только в регистре означает, что
# человек хотел задать известный параметр и промахнулся.
_KNOWN_ENV_NAMES = frozenset(
    [f.name for f in fields(Config)] + [
        # читаются напрямую, минуя Config
        "DASHBOARD_TOKEN", "DB_PATH", "PORT", "AUTO_TRADE", "BOT_MODE",
        "STRATEGY_ID",
        "PROXY_URL", "PROXY_LIST",
    ]
)


def env_name_typos() -> List[str]:
    """Переменные, чьё ИМЯ почти совпадает с известным параметром.

    Возвращает готовые к показу строки. Пусто — значит совпадений нет.
    """
    out: List[str] = []
    for raw in os.environ:
        cleaned = raw.strip()
        if cleaned == raw and raw not in _KNOWN_ENV_NAMES:
            # имя без пробелов и не наше — чужая переменная, не наше дело
            upper = raw.upper()
            if upper in _KNOWN_ENV_NAMES and upper != raw:
                out.append(f"{raw!r} — код читает {upper}, регистр не совпадает")
            continue
        if cleaned != raw and cleaned.upper() in _KNOWN_ENV_NAMES:
            out.append(f"{raw!r} — лишние пробелы в имени, "
                       f"код читает {cleaned.upper()} и НЕ ВИДИТ эту переменную")
        elif cleaned != raw:
            out.append(f"{raw!r} — лишние пробелы в имени переменной")
    return sorted(out)


_TYPOS = env_name_typos()
if _TYPOS:
    import logging as _lg2
    _tl = _lg2.getLogger("config")
    for _t in _TYPOS:
        _tl.error(f"ПЕРЕМЕННАЯ ОКРУЖЕНИЯ НЕ ПРИМЕНЕНА: {_t}")
