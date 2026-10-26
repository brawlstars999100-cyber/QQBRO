"""📅 План дня: Telegram-бот, календарь с напоминаниями.

• Пишешь задачи обычным текстом («завтра в 15:30 врач»), бот сам понимает дату и время.
• В нужный момент напоминает и ждёт отметки «Сделал» (можно отложить).
• Календарь на месяц, утренний план и вечерний итог.
• Многопользовательский: у каждого свои задачи и часовой пояс.
• Данные хранятся в planner_data.json и переживают перезапуск.
"""
import calendar
import copy
import html
import json
import logging
import os
import random
import re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    Defaults,
    MessageHandler,
    filters,
)

TOKEN = os.environ["BOT_TOKEN"]  # токен от @BotFather (для этого бота нужен СВОЙ токен)
DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "planner_data.json")

TZ_CHOICES = [
    ("🇷🇺 Москва", "Europe/Moscow"),
    ("🇺🇦 Киев", "Europe/Kiev"),
    ("🇧🇾 Минск", "Europe/Minsk"),
    ("🇰🇿 Алматы", "Asia/Almaty"),
    ("🇪🇺 Амстердам / Берлин", "Europe/Amsterdam"),
    ("🇬🇧 Лондон", "Europe/London"),
]
MORNINGS = ["06:00", "07:00", "08:00", "09:00", "10:00"]
EVENINGS = ["19:00", "20:00", "21:00", "22:00", "23:00"]
WARNS = [0, 5, 10, 15, 30, 60]
NAGS = [0, 5, 10, 15, 30]
NAG_MAX = 3
HORIZON = 30          # на сколько дней вперёд раскладываем повторяющиеся задачи
LATE_MIN = 120        # если напоминание опоздало больше чем на это, помечаем «пропущено»
DIGEST_WINDOW = 180   # утренний/вечерний дайджест не шлём, если опоздали больше чем на 3 часа
TICK_SECONDS = 30
MAX_TASKS = 400

WD_SHORT = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
MONTHS_G = ["янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]
MONTHS_N = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]
PRAISE = ["💪 Красавчик!", "🔥 Отлично!", "🚀 Так держать!", "👏 Мощно!", "⭐️ Ещё одно дело закрыто!"]

logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)

DEFAULT_USER = {
    "id": 0,
    "onb": 1,              # 1 часовой пояс, 2 утренний план, 0 готово
    "tz": "Europe/Amsterdam",
    "morning": "08:00",    # "" = выключено
    "evening": "21:00",
    "warn": 0,             # за сколько минут предупреждать (0 = не надо)
    "nag": 10,             # через сколько минут повторять, если не ответил (0 = не повторять)
    "tasks": [],
    "rules": [],           # повторяющиеся задачи
    "seq": 0,
    "sent_morn": "",
    "sent_eve": "",
    "mat": "",
}

DB = {"users": {}}
try:
    with open(DATA_FILE, encoding="utf-8") as f:
        DB.update(json.load(f))
except (FileNotFoundError, json.JSONDecodeError):
    pass


def save():
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(DB, f, ensure_ascii=False)
    os.replace(tmp, DATA_FILE)


def get_user(cid):
    u = DB["users"].get(str(cid))
    if u is None:
        u = DB["users"][str(cid)] = copy.deepcopy(DEFAULT_USER)
        u["id"] = cid
    for k, v in DEFAULT_USER.items():
        u.setdefault(k, copy.deepcopy(v))
    return u


# ---------- время и форматирование ----------
def e(text):
    return html.escape(str(text))


def plural(n, forms):
    n = abs(n) % 100
    if 11 <= n <= 14:
        return forms[2]
    n %= 10
    return forms[0] if n == 1 else forms[1] if 2 <= n <= 4 else forms[2]


def tz_of(u):
    name = u["tz"]
    if name.startswith("fixed:"):
        return timezone(timedelta(minutes=int(name[6:])))
    return ZoneInfo(name)


def tz_label(u):
    name = u["tz"]
    for label, z in TZ_CHOICES:
        if z == name:
            return label.split(" ", 1)[1]
    if name.startswith("fixed:"):
        mins = int(name[6:])
        h, m = divmod(abs(mins), 60)
        return f"UTC{'+' if mins >= 0 else '−'}{h}" + (f":{m:02d}" if m else "")
    return name


def parse_tz(text):
    t = text.strip()
    if m := re.fullmatch(r"(?:utc|gmt)?\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?", t.lower()):
        mins = int(m[2]) * 60 + int(m[3] or 0)
        if mins > 14 * 60:
            return None
        return f"fixed:{-mins if m[1] == '-' else mins}"
    try:
        ZoneInfo(t)
        return t
    except Exception:
        return None


def now_of(u):
    """Местное время пользователя без часового пояса, с точностью до минуты."""
    return datetime.now(tz_of(u)).replace(tzinfo=None, second=0, microsecond=0)


def fmt_date(d, today):
    name = {0: "Сегодня", 1: "Завтра", -1: "Вчера"}.get((d - today).days)
    base = f"{WD_SHORT[d.weekday()]} {d.day} {MONTHS_G[d.month - 1]}"
    return f"{name}, {base}" if name else base


def fmt_short(d, today):
    name = {0: "Сегодня", 1: "Завтра"}.get((d - today).days)
    return name or f"{WD_SHORT[d.weekday()]} {d.day} {MONTHS_G[d.month - 1]}"


def bar(done, total, width=10):
    if not total:
        return "▱" * width
    filled = int(width * done / total + 0.5)
    return "▰" * filled + "▱" * (width - filled)


def rule_label(rule):
    rep = rule["rep"]
    if rep == "daily":
        return "каждый день"
    if rep == "weekdays":
        return "по будням"
    return f"каждый {WD_SHORT[int(rep[2:])]}"


def ikb(rows):
    return InlineKeyboardMarkup([[InlineKeyboardButton(t, callback_data=d) for t, d in row] for row in rows])


MAIN_KB = ReplyKeyboardMarkup(
    [["📅 Сегодня", "🗓 Календарь"], ["➕ Добавить", "⚙️ Настройки", "❓ Помощь"]],
    resize_keyboard=True,
    input_field_placeholder="Напиши задачу, например: завтра в 15:30 врач",
)
NAV = {"📅 Сегодня": "today", "🗓 Календарь": "cal", "➕ Добавить": "add", "⚙️ Настройки": "settings", "❓ Помощь": "help"}


# ---------- разбор задач из текста ----------
WD_PATTERN = r"(?:понедельник\w*|вторник\w*|сред[ауеы]\w*|четверг\w*|пятниц\w*|суббот\w*|воскресень\w*|пн|вт|ср|чт|пт|сб|вс)"
WD_NAMES = {"пн": 0, "вт": 1, "ср": 2, "чт": 3, "пт": 4, "сб": 5, "вс": 6}
MONTH_PREFIX = {"янв": 1, "фев": 2, "мар": 3, "апр": 4, "мая": 5, "май": 5, "июн": 6, "июл": 7, "авг": 8, "сен": 9, "окт": 10, "ноя": 11, "дек": 12}


def wd_index(word):
    w = word.lower()
    for key, idx in (("пон", 0), ("вто", 1), ("сре", 2), ("чет", 3), ("пят", 4), ("суб", 5), ("вос", 6)):
        if w.startswith(key):
            return idx
    return WD_NAMES[w[:2]]


def mk_time(h, mi, mod):
    h, mi = int(h), int(mi or 0)
    if mod:
        mod = mod.lower()
        if mod in ("вечера", "дня") and h < 12:
            h += 12
        if mod == "ночи" and h == 12:
            h = 0
    return (h, mi) if 0 <= h <= 23 and 0 <= mi <= 59 else None


def valid_date(y, m, d):
    try:
        return date(y, m, d)
    except ValueError:
        return None


def parse_task(text, now, default_date=None):
    """Строка -> {title, date, time, rep, shifted, found} или None, если не о чём напоминать."""
    s = " " + text.strip() + " "

    def grab(pattern, check=None):
        nonlocal s
        for m in re.finditer(pattern, s, re.I):
            if check is None or check(m):
                s = s[:m.start()] + " " + s[m.end():]
                return m
        return None

    today = now.date()
    rep = None
    if grab(r"(?<!\w)(?:каждый\s+день|ежедневно|каждые\s+сутки)(?!\w)"):
        rep = "daily"
    elif grab(r"(?<!\w)(?:по\s+будн\w+|в\s+будни|каждый\s+будний\s+день)(?!\w)"):
        rep = "weekdays"
    elif m := grab(rf"(?<!\w)(?:кажд\w+|по)\s+({WD_PATTERN})(?!\w)"):
        rep = f"wd{wd_index(m[1])}"

    rel = None
    if m := grab(r"(?<!\w)через\s+(\d+)\s*(мин\w*|м|час\w*|ч)(?!\w)"):
        n = int(m[1])
        rel = timedelta(minutes=n) if m[2].lower().startswith("м") else timedelta(hours=n)
    elif m := grab(r"(?<!\w)через\s+(полчаса|час)(?!\w)"):
        rel = timedelta(minutes=30) if m[1].lower() == "полчаса" else timedelta(hours=1)

    tm = None
    if rel is None:
        if m := grab(r"(?<!\w)(?:в|к)\s+(\d{1,2})(?::(\d{2}))?(?:\s*час\w*)?(?:\s+(утра|вечера|дня|ночи))?(?!\w)",
                     lambda m: mk_time(m[1], m[2], m[3]) is not None):
            tm = mk_time(m[1], m[2], m[3])
        elif m := grab(r"(?<![\w:.])(\d{1,2}):(\d{2})(?![\w:.])", lambda m: mk_time(m[1], m[2], None) is not None):
            tm = mk_time(m[1], m[2], None)

    day = None
    if m := grab(r"(?<!\w)(сегодня|послезавтра|завтра)(?!\w)"):
        day = today + timedelta(days=("сегодня", "завтра", "послезавтра").index(m[1].lower()))

    def dmy(m):
        y = int(m[3]) if m[3] else today.year
        y = y + 2000 if y < 100 else y
        return valid_date(y, int(m[2]), int(m[1]))

    if day is None:
        if m := grab(r"(?<![\w.:])(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?(?![\w.:])", lambda m: dmy(m) is not None):
            day = dmy(m)
            if not m[3] and day < today:
                day = valid_date(today.year + 1, day.month, day.day) or day
        elif m := grab(r"(?<!\w)(\d{1,2})\s+(янв\w*|фев\w*|мар\w*|апр\w*|ма[яй]\w*|июн\w*|июл\w*|авг\w*|сен\w*|окт\w*|ноя\w*|дек\w*)(?!\w)",
                       lambda m: valid_date(today.year, MONTH_PREFIX[m[2][:3].lower()], int(m[1])) is not None):
            day = valid_date(today.year, MONTH_PREFIX[m[2][:3].lower()], int(m[1]))
            if day < today:
                day = valid_date(today.year + 1, day.month, day.day) or day
    if day is None and rep is None:
        if m := grab(rf"(?<!\w)(?:(?:в|во|на)\s+)?({WD_PATTERN})(?!\w)"):
            delta = (wd_index(m[1]) - today.weekday()) % 7
            if delta == 0 and tm and tm <= (now.hour, now.minute):
                delta = 7
            day = today + timedelta(days=delta)

    if tm is None and rel is None:  # «9.30» без двоеточия: время, раз это не дата
        if m := grab(r"(?<![\w.:])(\d{1,2})\.(\d{2})(?![\w.:])", lambda m: mk_time(m[1], m[2], None) is not None):
            tm = mk_time(m[1], m[2], None)

    found = bool(rep or rel or tm or day)
    shifted = False
    if rel is not None:
        dt = now + rel
        d, time_s = dt.date(), dt.strftime("%H:%M")
    else:
        d = day
        time_s = f"{tm[0]:02d}:{tm[1]:02d}" if tm else ""
        if d is None:
            if default_date:
                d = default_date
            elif tm and rep is None:
                d = today if tm > (now.hour, now.minute) else today + timedelta(days=1)
                shifted = d != today
            else:
                d = today

    title = re.sub(r"\s+", " ", s).strip(" \t-–—:,;.")
    title = re.sub(r"^(?:напомни(?:ть)?(?:\s+мне)?|мне)\s+", "", title, flags=re.I).strip(" -–—:,;.")
    if not title:
        return None
    title = title[:120]
    return {"title": title[0].upper() + title[1:], "date": d, "time": time_s, "rep": rep, "shifted": shifted, "found": found}


def parse_when(text, now):
    r = parse_task(text + " задача", now)
    return r if r and r["found"] and not r["rep"] else None


# ---------- задачи ----------
def next_id(u):
    u["seq"] += 1
    return u["seq"]


def new_task(u, title, date_iso, time_s, rule=None):
    t = {"id": next_id(u), "title": title, "date": date_iso, "time": time_s, "done": False,
         "rule": rule, "rdate": date_iso, "warned": False, "fired": False, "nags": 0, "nag_at": ""}
    u["tasks"].append(t)
    return t


def find_task(u, tid):
    return next((t for t in u["tasks"] if t["id"] == tid), None)


def rule_matches(rule, d):
    rep = rule["rep"]
    return rep == "daily" or (rep == "weekdays" and d.weekday() < 5) or (rep.startswith("wd") and d.weekday() == int(rep[2:]))


def materialize(u, now):
    """Раскладывает повторяющиеся задачи на ближайшие HORIZON дней."""
    have = {(t["rule"], t["rdate"]) for t in u["tasks"] if t["rule"]}
    for rule in u["rules"]:
        for i in range(HORIZON):
            d = now.date() + timedelta(days=i)
            if not rule_matches(rule, d) or (rule["id"], d.isoformat()) in have:
                continue
            if i == 0 and rule["time"] and rule["time"] <= now.strftime("%H:%M"):
                continue  # сегодняшнее время уже прошло
            new_task(u, rule["title"], d.isoformat(), rule["time"], rule=rule["id"])


def day_tasks(u, iso):
    return sorted((t for t in u["tasks"] if t["date"] == iso), key=lambda t: (t["time"] == "", t["time"], t["id"]))


def is_overdue(t, now):
    if t["done"]:
        return False
    if t["date"] < now.date().isoformat():
        return True
    return t["date"] == now.date().isoformat() and bool(t["time"]) and t["time"] < now.strftime("%H:%M")


def reset_flags(t, warned=False):
    t.update(warned=warned, fired=False, nags=0, nag_at="")


def add_from_text(u, text, now, default_date=None):
    results, errors = [], []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        r = parse_task(line, now, default_date)
        if not r or len(u["tasks"]) >= MAX_TASKS:
            errors.append(line)
            continue
        if r["rep"]:
            rule = {"id": next_id(u), "title": r["title"], "time": r["time"], "rep": r["rep"]}
            u["rules"].append(rule)
            materialize(u, now)
            results.append(("rule", rule, r))
        else:
            results.append(("task", new_task(u, r["title"], r["date"].isoformat(), r["time"]), r))
    save()
    return results, errors


# ---------- экраны ----------
def task_line(t, i, now):
    icon = "✅" if t["done"] else ("⚠️" if is_overdue(t, now) else "⬜")
    body = f"{t['time'] + ' ' if t['time'] else ''}{e(t['title'])}"
    if t["done"]:
        body = f"<s>{body}</s>"
    return f"{i}. {icon} {body}{' 🔁' if t['rule'] else ''}"


def day_nav(d, today):
    prev, nxt = (d - timedelta(days=1)).isoformat(), (d + timedelta(days=1)).isoformat()
    rows = [[("◀️", f"day:{prev}"), ("🗓 Календарь", f"cal:{d.year}-{d.month:02d}"), ("▶️", f"day:{nxt}")],
            [("➕ Добавить", f"add:{d.isoformat()}")]]
    if d != today:
        rows.append([("📅 К сегодняшнему дню", "today")])
    return rows


def day_screen(u, d):
    now = now_of(u)
    tasks = day_tasks(u, d.isoformat())
    head = f"📅 <b>{fmt_date(d, now.date())}</b>"
    if not tasks:
        text = (f"{head}\n\nПока пусто 🌱\nНапиши, что планируешь, например:\n"
                "• <code>в 9:00 зарядка</code>\n• <code>завтра 15:30 врач</code>\n• <code>каждый день в 8 таблетки</code>")
        return text, ikb(day_nav(d, now.date()))
    done = sum(1 for t in tasks if t["done"])
    lines = [head, "", f"{bar(done, len(tasks))}  ✅ {done} из {len(tasks)}", ""]
    lines += [task_line(t, i, now) for i, t in enumerate(tasks, 1)]
    btns = [(str(i), f"task:{t['id']}") for i, t in enumerate(tasks[:36], 1)]
    rows = [btns[i:i + 6] for i in range(0, len(btns), 6)] + day_nav(d, now.date())
    if len(tasks) > 36:
        lines.append(f"\n… и ещё {len(tasks) - 36} (открой через календарь другого дня)")
    return "\n".join(lines), ikb(rows)


def month_screen(u, y, m):
    today = now_of(u).date()
    prefix = f"{y}-{m:02d}-"
    stats = {}
    for t in u["tasks"]:
        if t["date"].startswith(prefix):
            st = stats.setdefault(int(t["date"][8:10]), [0, 0])
            st[1] += 1
            st[0] += 0 if t["done"] else 1
    py, pm = (y - 1, 12) if m == 1 else (y, m - 1)
    ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
    rows = [[("◀️", f"cal:{py}-{pm:02d}"), (f"{MONTHS_N[m - 1]} {y}", "noop"), ("▶️", f"cal:{ny}-{nm:02d}")],
            [(w.capitalize(), "noop") for w in WD_SHORT]]
    for week in calendar.monthcalendar(y, m):
        row = []
        for dd in week:
            if dd == 0:
                row.append(("·", "noop"))
                continue
            label = str(dd)
            if dd in stats:
                label += "•" if stats[dd][0] else "✓"
            if date(y, m, dd) == today:
                label = "🔹" + label
            row.append((label, f"day:{y}-{m:02d}-{dd:02d}"))
        rows.append(row)
    rows.append([("📅 Сегодня", "today")])
    text = (f"🗓 <b>{MONTHS_N[m - 1]} {y}</b>\n\nНажми на день, чтобы открыть план.\n"
            "🔹 сегодня · • есть дела · ✓ всё сделано")
    return text, ikb(rows)


def task_card(u, t, note=""):
    now = now_of(u)
    d = date.fromisoformat(t["date"])
    when = fmt_date(d, now.date()) + (f" · {t['time']}" if t["time"] else " · без времени")
    lines = ([note, ""] if note else []) + [f"📝 <b>{e(t['title'])}</b>", f"🗓 {when}"]
    rule = next((r for r in u["rules"] if r["id"] == t["rule"]), None)
    if rule:
        lines.append(f"🔁 Повторяется: {rule_label(rule)}")
    lines.append("✅ Сделано" if t["done"] else ("⚠️ Просрочено" if is_overdue(t, now) else "⬜ Не сделано"))
    tid = t["id"]
    rows = [[("↩️ Вернуть", f"t:undo:{tid}")] if t["done"] else [("✅ Сделано", f"t:done:{tid}")]]
    if not t["done"]:
        rows.append([("⏰ +10 мин", f"t:snz:{tid}:10"), ("⏰ +1 час", f"t:snz:{tid}:60"), ("📅 Завтра", f"t:snz:{tid}:tom")])
    rows += [[("✏️ Текст", f"t:edit:{tid}:text"), ("🕒 Перенести", f"t:edit:{tid}:time")],
             [("🗑 Удалить", f"t:del:{tid}")],
             [("⬅️ К дню", f"day:{t['date']}")]]
    return "\n".join(lines), ikb(rows)


def settings_screen(u):
    morning = u["morning"] or "выкл"
    evening = u["evening"] or "выкл"
    warn = f"за {u['warn']} мин" if u["warn"] else "нет"
    nag = f"каждые {u['nag']} мин" if u["nag"] else "нет"
    return "⚙️ <b>Настройки</b>\n\nНажми на пункт, чтобы изменить 👇", ikb([
        [(f"🌅 Утренний план: {morning}", "pick:morning")],
        [(f"🌙 Вечерний итог: {evening}", "pick:evening")],
        [(f"🔔 Предупреждать заранее: {warn}", "pick:warn")],
        [(f"🔁 Повторять, если не ответил: {nag}", "pick:nag")],
        [(f"🌍 Часовой пояс: {tz_label(u)}", "pick:tz")],
        [("♻️ Удалить все данные", "reset_all")],
        [("🏠 К плану на сегодня", "today")],
    ])


def picker_screen(u, key):
    onb = bool(u["onb"])
    steps = {"tz": 1, "morning": 2}
    head = f"<b>Шаг {steps[key]} из 2</b>\n\n" if onb and key in steps else ""

    def opts(values, label, cur, per_row=3):
        btns = [(("✅ " if (not onb and v == cur) else "") + label(v), f"set:{key}:{v}") for v in values]
        return [btns[i:i + per_row] for i in range(0, len(btns), per_row)]

    if key == "tz":
        text = f"{head}🌍 <b>Твой часовой пояс</b>\n\nНужен, чтобы напоминания приходили вовремя."
        btns = [(label, f"set:tz:{i}") for i, (label, _) in enumerate(TZ_CHOICES)]
        rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
        rows.append([("✍️ Другой", "set:tz:other")])
    elif key in ("morning", "evening"):
        what = "утренний план" if key == "morning" else "вечерний итог"
        text = f"{head}{'🌅' if key == 'morning' else '🌙'} <b>Во сколько присылать {what}?</b>\n\n" + (
            "Утром я напишу, что запланировано на день." if key == "morning" else "Вечером подведу итог и предложу перенести невыполненное.")
        rows = opts(MORNINGS if key == "morning" else EVENINGS, str, u[key], per_row=5)
        rows.append([("✍️ Своё время", f"set:{key}:custom"), ("🚫 Не нужно", f"set:{key}:off")])
    elif key == "warn":
        text = "🔔 <b>Предупреждать заранее?</b>\n\nЯ напишу ещё и за несколько минут до дела."
        rows = opts(WARNS, lambda v: "нет" if v == 0 else f"за {v} мин", u["warn"])
    else:  # nag
        text = f"🔁 <b>Повторять, если не ответил?</b>\n\nЕсли не нажмёшь «Сделал», напомню ещё до {NAG_MAX} раз."
        rows = opts(NAGS, lambda v: "нет" if v == 0 else f"{v} мин", u["nag"])
    if not onb:
        rows.append([("⬅️ Назад", "settings")])
    return text, ikb(rows)


HELP_TEXT = (
    "❓ <b>Как пользоваться</b>\n\n"
    "✍️ Просто пиши задачи в чат, по одной в строке:\n"
    "• <code>в 9:00 зарядка</code>\n"
    "• <code>завтра 15:30 врач</code>\n"
    "• <code>через 30 минут позвонить маме</code>\n"
    "• <code>27.10 18:00 вебинар</code>\n"
    "• <code>в пятницу в 19:00 кино</code>\n"
    "• <code>каждый день в 8 таблетки</code>\n"
    "• <code>по будням в 10:00 планёрка</code>\n"
    "• <code>купить хлеб</code> (без времени)\n\n"
    "⏰ В нужное время я напишу и подожду, пока нажмёшь «Сделал». Можно отложить на 10 минут, час или на завтра.\n"
    "🗓 В «Календаре» виден месяц: нажми на день, чтобы открыть его план.\n"
    "🌅 Утром пришлю план дня, 🌙 вечером подведу итог.\n"
    "⚙️ Время дайджестов, предупреждения заранее и часовой пояс меняются в настройках.\n\n"
    "💡 Время пиши через двоеточие: <code>9:30</code>. Задачу можно открыть по номеру в списке дня и изменить."
)

ADD_PROMPT = (
    "✍️ <b>Напиши задачи</b>, по одной в строке:\n\n"
    "• <code>в 9:00 зарядка</code>\n• <code>завтра 15:30 врач</code>\n• <code>каждый день в 8 таблетки</code>\n\n"
    "Я сам пойму дату и время 🙂"
)


async def show(q, screen):
    text, markup = screen
    try:
        await q.edit_message_text(text, reply_markup=markup)
    except BadRequest as ex:
        if "not modified" not in str(ex).lower():
            raise


async def send(context, u, text, markup=None):
    await context.bot.send_message(u["id"], text, reply_markup=markup)


def remind_kb(tid):
    return ikb([
        [("✅ Сделал", f"t:done:{tid}")],
        [("⏰ +10 мин", f"t:snz:{tid}:10"), ("⏰ +1 час", f"t:snz:{tid}:60")],
        [("📅 На завтра", f"t:snz:{tid}:tom")],
    ])


# ---------- фоновая проверка напоминаний ----------
async def tick_user(context, u):
    now = now_of(u)
    today = now.date().isoformat()
    dirty = False

    if u["mat"] != today:
        materialize(u, now)
        cutoff = (now.date() - timedelta(days=60)).isoformat()
        u["tasks"] = [t for t in u["tasks"] if t["date"] >= cutoff]
        u["mat"] = today
        dirty = True

    for t in list(u["tasks"]):
        if t["done"] or not t["time"]:
            continue
        due = datetime.fromisoformat(f"{t['date']}T{t['time']}")
        if u["warn"] and not t["warned"] and not t["fired"] and due - timedelta(minutes=u["warn"]) <= now < due:
            t["warned"] = True
            dirty = True
            await send(context, u, f"🔔 <b>Через {max(1, int((due - now).total_seconds() // 60))} мин</b>\n\n📝 {e(t['title'])} в {t['time']}",
                       ikb([[("✅ Уже сделал", f"t:done:{t['id']}")]]))
        if not t["fired"] and now >= due:
            late = (now - due).total_seconds() / 60
            t["fired"] = True
            t["nags"] = 0
            t["nag_at"] = (now + timedelta(minutes=u["nag"])).isoformat() if u["nag"] and late <= LATE_MIN else ""
            dirty = True
            head = "⏰ <b>Пора!</b>" if late <= LATE_MIN else f"⏰ <b>Пропущено, пока бот был выключен</b> (было в {t['time']})"
            await send(context, u, f"{head}\n\n📝 {e(t['title'])}{' 🔁' if t['rule'] else ''}", remind_kb(t["id"]))
        elif t["fired"] and t["nag_at"] and now >= datetime.fromisoformat(t["nag_at"]):
            dirty = True
            if t["nags"] >= NAG_MAX:
                t["nag_at"] = ""
            else:
                t["nags"] += 1
                t["nag_at"] = (now + timedelta(minutes=u["nag"])).isoformat()
                await send(context, u, f"👀 Напоминаю: <b>{e(t['title'])}</b>. Сделал?", remind_kb(t["id"]))

    hm = now.strftime("%H:%M")

    def in_window(target):
        mins = (now.hour * 60 + now.minute) - (int(target[:2]) * 60 + int(target[3:]))
        return 0 <= mins <= DIGEST_WINDOW

    if u["morning"] and u["sent_morn"] != today and hm >= u["morning"]:
        u["sent_morn"] = today
        dirty = True
        if in_window(u["morning"]):
            text, markup = day_screen(u, now.date())
            await send(context, u, "🌅 <b>Доброе утро!</b> Вот твой план.\n\n" + text, markup)

    if u["evening"] and u["sent_eve"] != today and hm >= u["evening"]:
        u["sent_eve"] = today
        dirty = True
        tasks = day_tasks(u, today)
        if tasks and in_window(u["evening"]):
            done = sum(1 for t in tasks if t["done"])
            left = [t for t in tasks if not t["done"]]
            if not left:
                await send(context, u, f"🌙 <b>Итог дня</b>\n\n{bar(done, len(tasks))}  ✅ {done} из {len(tasks)}\n\nВсё выполнено, ты молодец 🎉")
            else:
                lst = "\n".join(f"• {e(t['title'])}" for t in left[:15])
                await send(context, u, f"🌙 <b>Итог дня</b>\n\n{bar(done, len(tasks))}  ✅ {done} из {len(tasks)}\n\nНе сделано:\n{lst}\n\nПеренести на завтра?",
                           ikb([[("➡️ Перенести на завтра", "ev:move")], [("👌 Оставить как есть", "ev:keep")]]))
    if dirty:
        save()


async def tick(context: ContextTypes.DEFAULT_TYPE):
    for u in list(DB["users"].values()):
        if u["onb"]:
            continue
        try:
            await tick_user(context, u)
        except Exception:
            logging.exception("Ошибка при обработке пользователя %s", u.get("id"))


# ---------- команды и текст ----------
async def onboarding_prompt(reply, u):
    text, markup = picker_screen(u, "tz" if u["onb"] == 1 else "morning")
    await reply(text, reply_markup=markup)


async def finish_onboarding(reply_or_show, u, via_edit=None):
    u["onb"] = 0
    now = now_of(u)
    u["sent_morn"] = now.date().isoformat()  # утренний план начнём слать со следующего дня
    materialize(u, now)
    save()
    text = ("🎉 <b>Всё готово!</b>\n\nТеперь просто пиши задачи в чат, я сам пойму дату и время:\n\n"
            "• <code>в 9:00 зарядка</code>\n• <code>завтра 15:30 врач</code>\n• <code>каждый день в 8 таблетки</code>\n\n"
            "Кнопки внизу: план на сегодня, календарь, настройки.")
    markup = ikb([[("📅 План на сегодня", "today")]])
    if via_edit is not None:
        await show(via_edit, (text, markup))
    else:
        await reply_or_show(text, reply_markup=markup)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = get_user(update.effective_chat.id)
    reply = update.message.reply_text
    if u["onb"]:
        await reply(
            "👋 <b>Привет! Я «План дня» 📅</b>\n\n"
            "Записывай задачи обычным текстом, а я напомню о них вовремя и покажу всё в календаре.\n\n"
            "⚡️ Настроим за 30 секунд.",
            reply_markup=MAIN_KB,
        )
        await onboarding_prompt(reply, u)
    else:
        await reply("👋 С возвращением!", reply_markup=MAIN_KB)
        text, markup = day_screen(u, now_of(u).date())
        await reply(text, reply_markup=markup)


async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await handle_nav(update, context, "today")


async def cmd_calendar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await handle_nav(update, context, "cal")


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await handle_nav(update, context, "add")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await handle_nav(update, context, "help")


async def handle_nav(update, context, where):
    u = get_user(update.effective_chat.id)
    context.user_data.clear()
    reply = update.message.reply_text
    if u["onb"]:
        return await onboarding_prompt(reply, u)
    now = now_of(u)
    if where == "today":
        text, markup = day_screen(u, now.date())
    elif where == "cal":
        text, markup = month_screen(u, now.year, now.month)
    elif where == "settings":
        text, markup = settings_screen(u)
    elif where == "add":
        text, markup = ADD_PROMPT, None
    else:
        text, markup = HELP_TEXT, ikb([[("📅 План на сегодня", "today")]])
    await reply(text, reply_markup=markup)


def added_summary(u, results, errors, now):
    today = now.date()
    lines = ["✅ <b>Добавил:</b>"]
    first_day = None
    for kind, obj, r in results:
        if kind == "rule":
            lines.append(f"• 🔁 {rule_label(obj)}{' ' + obj['time'] if obj['time'] else ''} · {e(obj['title'])}")
        else:
            d = date.fromisoformat(obj["date"])
            first_day = first_day or d
            when = fmt_short(d, today) + (f" {obj['time']}" if obj["time"] else ", без времени")
            note = " <i>(время уже прошло, поставил на завтра)</i>" if r["shifted"] else ""
            lines.append(f"• 🕒 {when} · {e(obj['title'])}{note}")
    if errors:
        lines.append("")
        lines += [f"🤔 Не понял: <code>{e(x)}</code> (напиши, о чём напомнить)" for x in errors]
    return "\n".join(lines), first_day or today


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = get_user(update.effective_chat.id)
    text = update.message.text.strip()
    reply = update.message.reply_text
    if text in NAV:
        return await handle_nav(update, context, NAV[text])
    mode = context.user_data.get("await")
    now = now_of(u)

    if mode == "tz":
        z = parse_tz(text)
        if z is None:
            return await reply("🤔 Не понял. Пришли сдвиг от UTC, например <code>+3</code> или <code>+5:30</code>, либо название вроде <code>Europe/Warsaw</code>.")
        context.user_data.pop("await", None)
        u["tz"] = z
        save()
        if u["onb"]:
            u["onb"] = 2
            save()
            return await onboarding_prompt(reply, u)
        t, markup = settings_screen(u)
        return await reply(t, reply_markup=markup)

    if u["onb"]:
        if mode in ("morning",) and (m := re.fullmatch(r"(\d{1,2})[:.](\d{2})", text)) and int(m[1]) < 24 and int(m[2]) < 60:
            context.user_data.pop("await", None)
            u["morning"] = f"{int(m[1]):02d}:{m[2]}"
            return await finish_onboarding(reply, u)
        return await onboarding_prompt(reply, u)

    if mode in ("morning", "evening"):
        m = re.fullmatch(r"(\d{1,2})[:.](\d{2})", text)
        if not m or int(m[1]) > 23 or int(m[2]) > 59:
            return await reply("🤔 Пришли время в формате ЧЧ:ММ, например <code>08:30</code>")
        context.user_data.pop("await", None)
        u[mode] = f"{int(m[1]):02d}:{m[2]}"
        save()
        t, markup = settings_screen(u)
        return await reply(t, reply_markup=markup)

    if mode == "edit_text":
        t = find_task(u, context.user_data.get("id"))
        context.user_data.pop("await", None)
        if not t:
            return await reply("🤔 Не нашёл эту задачу.")
        new = re.sub(r"\s+", " ", text).strip()[:120]
        if not new:
            return await reply("🤔 Пришли новый текст задачи.")
        t["title"] = new[0].upper() + new[1:]
        if t["rule"]:
            rule = next((r for r in u["rules"] if r["id"] == t["rule"]), None)
            if rule:
                rule["title"] = t["title"]
                for other in u["tasks"]:
                    if other["rule"] == rule["id"] and not other["done"]:
                        other["title"] = t["title"]
        save()
        card, markup = task_card(u, t, "✏️ Текст обновлён")
        return await reply(card, reply_markup=markup)

    if mode == "edit_time":
        t = find_task(u, context.user_data.get("id"))
        if not t:
            context.user_data.pop("await", None)
            return await reply("🤔 Не нашёл эту задачу.")
        r = parse_when(text, now)
        if not r:
            return await reply("🤔 Не понял. Пришли, например: <code>15:30</code>, <code>завтра 10:00</code> или <code>28.10 9:00</code>")
        context.user_data.pop("await", None)
        t["date"] = r["date"].isoformat()
        if r["time"]:
            t["time"] = r["time"]
        t["done"] = False
        reset_flags(t, warned=True)
        save()
        card, markup = task_card(u, t, "🕒 Перенёс")
        return await reply(card, reply_markup=markup)

    default_date = None
    if mode == "add" and context.user_data.get("add_date"):
        default_date = date.fromisoformat(context.user_data["add_date"])
    results, errors = add_from_text(u, text, now, default_date)
    if not results:
        return await reply("🤔 Не понял, о чём напомнить. Напиши, например: <code>завтра в 15:30 врач</code>\n\nПримеры — в «❓ Помощь».")
    context.user_data.pop("await", None)
    context.user_data.pop("add_date", None)
    summary, first_day = added_summary(u, results, errors, now)
    await reply(summary, reply_markup=ikb([[("📅 План на день", f"day:{first_day.isoformat()}")], [("🗓 Календарь", f"cal:{first_day.year}-{first_day.month:02d}")]]))


# ---------- кнопки ----------
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    u = get_user(update.effective_chat.id)
    data = q.data
    now = now_of(u)
    keep_state = data.startswith(("set:", "pick:"))
    if not keep_state:
        context.user_data.clear()

    if u["onb"] and not keep_state:
        await onboarding_prompt(q.message.reply_text, u)
        return

    if data == "noop":
        return

    if data == "today":
        await show(q, day_screen(u, now.date()))

    elif data.startswith("day:"):
        await show(q, day_screen(u, date.fromisoformat(data[4:])))

    elif data == "cal":
        await show(q, month_screen(u, now.year, now.month))

    elif data.startswith("cal:"):
        y, m = map(int, data[4:].split("-"))
        await show(q, month_screen(u, y, m))

    elif data.startswith("add:"):
        context.user_data["await"] = "add"
        context.user_data["add_date"] = data[4:]
        d = date.fromisoformat(data[4:])
        await show(q, (f"✍️ <b>Добавь задачи на {fmt_date(d, now.date()).lower()}</b>\n\nПиши по одной в строке, например:\n"
                       "• <code>в 9:00 зарядка</code>\n• <code>15:30 врач</code>\n• <code>купить хлеб</code>",
                       ikb([[("❌ Отмена", f"day:{data[4:]}")]])))

    elif data.startswith("task:"):
        t = find_task(u, int(data[5:]))
        await show(q, task_card(u, t) if t else day_screen(u, now.date()))

    elif data.startswith("t:"):
        parts = data.split(":")
        act, tid = parts[1], int(parts[2])
        t = find_task(u, tid)
        if not t:
            await show(q, ("🤔 Эта задача уже удалена.", ikb([[("📅 План на сегодня", "today")]])))
            return
        if act == "done":
            t.update(done=True, fired=True, nag_at="")
            save()
            today_tasks = day_tasks(u, t["date"])
            left = sum(1 for x in today_tasks if not x["done"])
            tail = f"Осталось на день: {left}" if left else "На этот день всё сделано 🎉"
            await show(q, task_card(u, t, f"{random.choice(PRAISE)} {tail}"))
        elif act == "undo":
            t["done"] = False
            due_passed = bool(t["time"]) and datetime.fromisoformat(f"{t['date']}T{t['time']}") <= now
            reset_flags(t, warned=True)
            t["fired"] = due_passed
            save()
            await show(q, task_card(u, t, "↩️ Вернул в список"))
        elif act == "snz":
            if parts[3] == "tom":
                base = max(date.fromisoformat(t["date"]), now.date())
                t["date"] = (base + timedelta(days=1)).isoformat()
                note = "📅 Перенёс на завтра"
            else:
                new = now + timedelta(minutes=int(parts[3]))
                t["date"], t["time"] = new.date().isoformat(), new.strftime("%H:%M")
                note = f"⏰ Отложил до {t['time']}"
            t["done"] = False
            reset_flags(t, warned=True)
            save()
            await show(q, task_card(u, t, note))
        elif act == "edit":
            context.user_data["await"] = "edit_text" if parts[3] == "text" else "edit_time"
            context.user_data["id"] = tid
            prompt = ("✏️ Пришли новый текст задачи." if parts[3] == "text" else
                      "🕒 Пришли новое время или дату, например: <code>15:30</code>, <code>завтра 10:00</code>, <code>28.10 9:00</code>")
            await show(q, (prompt, ikb([[("❌ Отмена", f"task:{tid}")]])))
        elif act == "del":
            if t["rule"]:
                await show(q, (f"🗑 <b>{e(t['title'])}</b> повторяется. Что удалить?",
                               ikb([[("Только это", f"t:del1:{tid}")], [("Всю серию", f"t:delall:{tid}")], [("❌ Отмена", f"task:{tid}")]])))
            else:
                u["tasks"].remove(t)
                save()
                await show(q, day_screen(u, date.fromisoformat(t["date"])))
        elif act == "del1":
            u["tasks"].remove(t)
            save()
            await show(q, day_screen(u, date.fromisoformat(t["date"])))
        elif act == "delall":
            rid = t["rule"]
            u["rules"] = [r for r in u["rules"] if r["id"] != rid]
            u["tasks"] = [x for x in u["tasks"] if not (x["rule"] == rid and (not x["done"]))]
            save()
            await show(q, day_screen(u, date.fromisoformat(t["date"])))

    elif data in ("ev:move", "ev:keep"):
        if data == "ev:keep":
            await show(q, ("👌 Оставил как есть. Хорошего вечера 🌙", None))
            return
        tomorrow = (now.date() + timedelta(days=1)).isoformat()
        moved = 0
        for t in day_tasks(u, now.date().isoformat()):
            if not t["done"]:
                t["date"] = tomorrow
                reset_flags(t, warned=False)
                moved += 1
        save()
        await show(q, (f"➡️ Перенёс на завтра: {moved} {plural(moved, ('задачу', 'задачи', 'задач'))}. Спокойной ночи 🌙",
                       ikb([[("📅 Завтра", f"day:{tomorrow}")]])))

    elif data == "help":
        await show(q, (HELP_TEXT, ikb([[("📅 План на сегодня", "today")]])))

    elif data == "settings":
        await show(q, settings_screen(u))

    elif data == "reset_all":
        await show(q, ("♻️ <b>Удалить все данные?</b>\n\nПропадут все задачи, повторы и настройки.",
                       ikb([[("Да, удалить", "reset_all_yes"), ("Нет", "settings")]])))

    elif data == "reset_all_yes":
        DB["users"].pop(str(u["id"]), None)
        u = get_user(update.effective_chat.id)
        save()
        await show(q, ("♻️ Всё удалено. Настроим заново 👇", None))
        await onboarding_prompt(q.message.reply_text, u)

    elif data.startswith("pick:"):
        await show(q, picker_screen(u, data[5:]))

    elif data.startswith("set:"):
        _, key, val = data.split(":", 2)
        if (key, val) == ("tz", "other") or (key in ("morning", "evening") and val == "custom"):
            context.user_data["await"] = key
            prompt = ("🌍 Пришли сдвиг от UTC, например <code>+3</code> или <code>+5:30</code>, либо название вроде <code>Europe/Warsaw</code>."
                      if key == "tz" else "🕒 Пришли время в формате ЧЧ:ММ, например <code>08:30</code>")
            back = "settings" if not u["onb"] else "pick:tz" if key == "tz" else "pick:morning"
            await show(q, (prompt, ikb([[("❌ Отмена", back)]])))
            return
        if key == "tz":
            u["tz"] = TZ_CHOICES[int(val)][1]
        elif key in ("morning", "evening"):
            u[key] = "" if val == "off" else val
        elif key == "warn":
            u["warn"] = int(val)
        elif key == "nag":
            u["nag"] = int(val)
        save()
        if u["onb"]:
            if key == "tz":
                u["onb"] = 2
                save()
                await show(q, picker_screen(u, "morning"))
            elif key == "morning":
                await finish_onboarding(None, u, via_edit=q)
        else:
            await show(q, settings_screen(u))


async def on_error(update, context):
    logging.error("Ошибка в обработчике", exc_info=context.error)


async def post_init(app: Application):
    await app.bot.set_my_commands([
        ("today", "📅 План на сегодня"),
        ("calendar", "🗓 Календарь"),
        ("add", "➕ Добавить задачи"),
        ("help", "❓ Как пользоваться"),
    ])
    try:
        print("✅ Бот «План дня» успешно запущен 🚀", flush=True)
    except UnicodeEncodeError:
        print("Бот План дня успешно запущен", flush=True)


def main():
    app = Application.builder().token(TOKEN).defaults(Defaults(parse_mode=ParseMode.HTML)).post_init(post_init).build()
    if app.job_queue is None:
        raise SystemExit('Установи зависимости: pip install "python-telegram-bot[job-queue]" tzdata')
    private = filters.ChatType.PRIVATE
    app.add_handler(CommandHandler("start", cmd_start, filters=private))
    app.add_handler(CommandHandler("today", cmd_today, filters=private))
    app.add_handler(CommandHandler("calendar", cmd_calendar, filters=private))
    app.add_handler(CommandHandler("add", cmd_add, filters=private))
    app.add_handler(CommandHandler("help", cmd_help, filters=private))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & private, on_text))
    app.add_error_handler(on_error)
    app.job_queue.run_repeating(tick, interval=TICK_SECONDS, first=5)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
