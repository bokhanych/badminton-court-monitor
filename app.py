from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import signal
import sys
import tempfile
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

LOG = logging.getLogger("court-monitor")
API_BASE = "https://abws.minskarena.by"
SERVICE_ID = 148
SALE_URL = f"https://saleframe.minskarena.by/service/{SERVICE_ID}"
USER_AGENT = "badminton-court-monitor/1.0"
WEEKDAY_COMMANDS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


@dataclass(frozen=True)
class Slot:
    event_id: int
    start: datetime
    end: datetime
    available: bool
    courts: int
    price_minor: int | None

    @property
    def key(self) -> str:
        return str(self.event_id)


@dataclass(frozen=True)
class Config:
    telegram_token: str
    telegram_chat_id: str
    interval: int
    evening_start: dt_time
    timezone: ZoneInfo
    state_file: Path
    timeout: float
    initial_notify: bool

    @classmethod
    def from_env(cls) -> "Config":
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        if not token or not chat_id:
            raise ValueError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required")
        interval = int(os.getenv("CHECK_INTERVAL_SECONDS", "60"))
        if interval < 10:
            raise ValueError("CHECK_INTERVAL_SECONDS must be at least 10")
        return cls(
            token, chat_id, interval,
            dt_time.fromisoformat(os.getenv("EVENING_START", "18:00")),
            ZoneInfo(os.getenv("TIMEZONE", "Europe/Minsk")),
            Path(os.getenv("STATE_FILE", "/data/state.json")),
            float(os.getenv("REQUEST_TIMEOUT_SECONDS", "20")),
            parse_bool(os.getenv("INITIAL_NOTIFY", "false")),
        )


def parse_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def request_json(url: str, timeout: float, data: dict[str, str] | None = None) -> Any:
    body = urlencode(data).encode() if data is not None else None
    request = Request(url, data=body, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except HTTPError as exc:
        detail = exc.read(500).decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} from {url}: {detail}") from exc
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"request failed for {url}: {exc}") from exc


def fetch_calendar(config: Config) -> list[date]:
    query = urlencode({"target": "saleframe", "lang": "ru"})
    payload = request_json(f"{API_BASE}/api/v1/frame/service/{SERVICE_ID}/calendar?{query}", config.timeout)
    return [date.fromisoformat(item["date"]) for item in payload]


def fetch_range(config: Config, first_day: date, last_day: date) -> list[Slot]:
    start = datetime.combine(first_day, dt_time.min, config.timezone)
    end = datetime.combine(last_day + timedelta(days=1), dt_time.min, config.timezone) - timedelta(seconds=1)
    query = urlencode({
        "sort": "start", "expand": "prices", "fields": "id,start,end,quota",
        "from": int(start.timestamp()), "to": int(end.timestamp()),
        "target": "saleframe", "lang": "ru",
    })
    payload = request_json(f"{API_BASE}/api/v1/frame/service/{SERVICE_ID}/events?{query}", config.timeout)
    return [parse_slot(item, config.timezone) for item in payload.get("data", [])]


def parse_slot(item: dict[str, Any], timezone: ZoneInfo) -> Slot:
    prices = item.get("prices") or []
    purchasable = [p for p in prices if p.get("available") is True and int(p.get("quota", 0)) > 0]
    courts = min(int(item.get("max_services", 0)), sum(int(p.get("quota", 0)) for p in purchasable))
    return Slot(
        int(item["id"]),
        datetime.fromtimestamp(int(item["start"]), timezone),
        datetime.fromtimestamp(int(item["end"]), timezone),
        courts > 0,
        courts,
        min((int(p["price"]) for p in purchasable), default=None),
    )


def relevant_slots(slots: list[Slot], config: Config, now: datetime) -> list[Slot]:
    return [s for s in slots if s.start.timetz().replace(tzinfo=None) >= config.evening_start and s.start > now + timedelta(minutes=30)]


def purchasable_slots(slots: list[Slot], now: datetime) -> list[Slot]:
    return [slot for slot in slots if slot.start > now + timedelta(minutes=30)]


def load_state(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value.get("slots"), dict):
            raise ValueError("missing slots object")
        return value
    except FileNotFoundError:
        return {"initialized": False, "slots": {}}
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        LOG.warning("Cannot read state %s; rebuilding it: %s", path, exc)
        return {"initialized": False, "slots": {}}


def save_state(path: Path, slots: dict[str, bool]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    value = {"initialized": True, "updated_at": datetime.now().astimezone().isoformat(), "slots": slots}
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        temporary = Path(stream.name)
    temporary.replace(path)


def newly_available(slots: list[Slot], state: dict[str, Any], initial_notify: bool) -> list[Slot]:
    if not state.get("initialized") and not initial_notify:
        return []
    old = state.get("slots", {})
    return [slot for slot in slots if slot.available and old.get(slot.key) is not True]


def group_slots_by_date(slots: list[Slot]) -> list[list[Slot]]:
    grouped: dict[date, list[Slot]] = defaultdict(list)
    for slot in sorted(slots, key=lambda item: item.start):
        grouped[slot.start.date()].append(slot)
    return list(grouped.values())


def format_notification(slots: list[Slot]) -> str:
    first = slots[0]
    weekday = ("Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье")[first.start.weekday()]
    lines = [
        f"🏸 <b>{first.start:%d.%m.%Y} — {weekday}</b>",
        "",
        "Доступные слоты:",
    ]
    lines.extend(
        f"• {slot.start:%H:%M}–{slot.end:%H:%M} — свободно: <b>{slot.courts}</b>"
        for slot in slots
    )
    lines.extend(["", f'<a href="{html.escape(SALE_URL)}">Открыть расписание и купить</a>'])
    return "\n".join(lines)


def next_weekday_range(today: date) -> tuple[date, date]:
    monday = today + timedelta(days=7 - today.weekday())
    return monday, monday + timedelta(days=4)


def nearest_sunday(today: date) -> date:
    days_ahead = (6 - today.weekday()) % 7
    return today + timedelta(days=days_ahead)


def format_empty_day(day: date) -> str:
    weekday = ("Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье")[day.weekday()]
    return (
        f"🏸 <b>{day:%d.%m.%Y} — {weekday}</b>\n\n"
        "Доступных вечерних слотов пока нет.\n\n"
        f'<a href="{html.escape(SALE_URL)}">Открыть расписание</a>'
    )


def send_telegram(config: Config, text: str, chat_id: str | None = None) -> None:
    try:
        response = request_json(
            f"https://api.telegram.org/bot{config.telegram_token}/sendMessage", config.timeout,
            {"chat_id": chat_id or config.telegram_chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": "true"},
        )
    except RuntimeError as exc:
        message = str(exc)
        detail = message.split(": ", 1)[-1] if ": " in message else "request failed"
        raise RuntimeError(f"Telegram API request failed: {detail}") from None
    if not response.get("ok"):
        raise RuntimeError(f"Telegram rejected message: {response}")


def poll(config: Config) -> None:
    now = datetime.now(config.timezone)
    days = fetch_calendar(config)
    slots: list[Slot] = []
    future_days = [day for day in days if day >= now.date()]
    if future_days:
        slots = relevant_slots(fetch_range(config, future_days[0], future_days[-1]), config, now)
    state = load_state(config.state_file)
    fresh = newly_available(slots, state, config.initial_notify)
    for daily_slots in group_slots_by_date(fresh):
        send_telegram(config, format_notification(daily_slots))
        LOG.info("Notification sent for %s (%d slot(s))", daily_slots[0].start.date(), len(daily_slots))
    save_state(config.state_file, {slot.key: slot.available for slot in slots})
    LOG.info("Checked %d date(s), %d evening slot(s), %d notification(s)", len(days), len(slots), len(fresh))


def send_next_weekdays(config: Config) -> tuple[date, date, int]:
    now = datetime.now(config.timezone)
    monday, friday = next_weekday_range(now.date())
    slots = relevant_slots(fetch_range(config, monday, friday), config, now)
    available = [slot for slot in slots if slot.available]
    by_date = {group[0].start.date(): group for group in group_slots_by_date(available)}
    for offset in range(5):
        day = monday + timedelta(days=offset)
        daily_slots = by_date.get(day, [])
        send_telegram(config, format_notification(daily_slots) if daily_slots else format_empty_day(day))
    return monday, friday, len(available)


def send_nearest_sunday(config: Config) -> tuple[date, int]:
    now = datetime.now(config.timezone)
    sunday = nearest_sunday(now.date())
    slots = relevant_slots(fetch_range(config, sunday, sunday), config, now)
    available = [slot for slot in slots if slot.available]
    send_telegram(config, format_notification(available) if available else format_empty_day(sunday))
    return sunday, len(available)


def parse_mentioned_date(text: str, bot_username: str, today: date) -> date | None:
    if f"@{bot_username}".casefold() not in text.casefold():
        return None
    match = re.search(r"(?<![\d.])(\d{1,2}\.\d{1,2})(?![\d.])", text)
    if not match:
        return None
    try:
        day, month = (int(part) for part in match.group(1).split("."))
        return date(today.year, month, day)
    except ValueError:
        return None


def parse_weekday_command(text: str, bot_username: str, today: date) -> date | None:
    match = re.match(r"^/(monday|tuesday|wednesday|thursday|friday|saturday|sunday)(?:@(\w+))?(?:\s|$)", text.strip(), re.IGNORECASE)
    if not match:
        return None
    addressed_bot = match.group(2)
    if addressed_bot and addressed_bot.casefold() != bot_username.casefold():
        return None
    week_monday = today - timedelta(days=today.weekday())
    return week_monday + timedelta(days=WEEKDAY_COMMANDS[match.group(1).lower()])


def offset_file(config: Config) -> Path:
    return config.state_file.with_name("telegram-offset.json")


def load_update_offset(config: Config) -> int | None:
    try:
        return int(json.loads(offset_file(config).read_text(encoding="utf-8"))["offset"])
    except (FileNotFoundError, OSError, ValueError, KeyError, json.JSONDecodeError):
        return None


def save_update_offset(config: Config, offset: int) -> None:
    path = offset_file(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"offset": offset}), encoding="utf-8")


def telegram_updates(config: Config, offset: int | None) -> list[dict[str, Any]]:
    params = {"timeout": 0, "allowed_updates": json.dumps(["message"])}
    if offset is not None:
        params["offset"] = offset
    try:
        response = request_json(
            f"https://api.telegram.org/bot{config.telegram_token}/getUpdates?{urlencode(params)}",
            config.timeout,
        )
    except RuntimeError as exc:
        message = str(exc)
        detail = message.split(": ", 1)[-1] if ": " in message else "request failed"
        raise RuntimeError(f"Telegram update request failed: {detail}") from None
    return response.get("result", [])


def register_bot_commands(config: Config) -> None:
    descriptions = {
        "monday": "Свободные корты в понедельник",
        "tuesday": "Свободные корты во вторник",
        "wednesday": "Свободные корты в среду",
        "thursday": "Свободные корты в четверг",
        "friday": "Свободные корты в пятницу",
        "saturday": "Свободные корты в субботу",
        "sunday": "Свободные корты в воскресенье",
    }
    try:
        response = request_json(
            f"https://api.telegram.org/bot{config.telegram_token}/setMyCommands",
            config.timeout,
            {"commands": json.dumps([{"command": command, "description": description} for command, description in descriptions.items()], ensure_ascii=False)},
        )
    except RuntimeError as exc:
        message = str(exc)
        detail = message.split(": ", 1)[-1] if ": " in message else "request failed"
        raise RuntimeError(f"Telegram command registration failed: {detail}") from None
    if not response.get("ok"):
        raise RuntimeError("Telegram rejected command registration")


def process_bot_mentions(config: Config, bot_username: str) -> int:
    current_offset = load_update_offset(config)
    updates = telegram_updates(config, current_offset)
    if not updates:
        return 0
    next_offset = max(int(update["update_id"]) for update in updates) + 1
    if current_offset is None:
        save_update_offset(config, next_offset)
        LOG.info("Telegram command listener initialized")
        return 0
    handled = 0
    today = datetime.now(config.timezone).date()
    for update in updates:
        message = update.get("message") or {}
        text = message.get("text") or ""
        command_date = parse_weekday_command(text, bot_username, today)
        has_mention = f"@{bot_username}".casefold() in text.casefold()
        if not command_date and not has_mention:
            continue
        chat_id = str((message.get("chat") or {}).get("id", ""))
        requested = command_date or parse_mentioned_date(text, bot_username, today)
        if not requested:
            send_telegram(
                config,
                f"Укажите дату без года после упоминания бота, например: <code>@{html.escape(bot_username)} 07.09</code>",
                chat_id,
            )
        else:
            now = datetime.now(config.timezone)
            slots = purchasable_slots(fetch_range(config, requested, requested), now)
            available = [slot for slot in slots if slot.available]
            send_telegram(config, format_notification(available) if available else format_empty_day(requested), chat_id)
        handled += 1
    save_update_offset(config, next_offset)
    return handled


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--test-notification", action="store_true")
    parser.add_argument("--weekdays-next-week", action="store_true")
    parser.add_argument("--next-sunday", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), format="%(asctime)s %(levelname)s %(message)s")
    try:
        config = Config.from_env()
    except (ValueError, ZoneInfoNotFoundError) as exc:
        LOG.error("Invalid configuration: %s", exc)
        return 2
    if args.test_notification:
        send_telegram(config, "✅ Тест: монитор свободных кортов подключён.")
        LOG.info("Test notification sent")
        return 0
    if args.weekdays_next_week:
        monday, friday, count = send_next_weekdays(config)
        LOG.info("Sent weekdays %s–%s: %d available slot(s)", monday, friday, count)
        return 0
    if args.next_sunday:
        sunday, count = send_nearest_sunday(config)
        LOG.info("Sent Sunday %s: %d available slot(s)", sunday, count)
        return 0
    stopping = False

    def stop(_signum: int, _frame: Any) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        bot_username = request_json(
            f"https://api.telegram.org/bot{config.telegram_token}/getMe", config.timeout
        )["result"]["username"]
        register_bot_commands(config)
    except (RuntimeError, KeyError):
        LOG.error("Cannot initialize Telegram command listener")
        return 1
    LOG.info(
        "Monitoring service %d, slots from %s; commands: @%s",
        SERVICE_ID,
        config.evening_start.strftime("%H:%M"),
        bot_username,
    )
    next_schedule_check = 0.0
    while not stopping:
        now_monotonic = time.monotonic()
        if now_monotonic >= next_schedule_check:
            try:
                poll(config)
            except Exception:
                LOG.exception("Poll failed; will retry")
            next_schedule_check = time.monotonic() + config.interval
        try:
            handled = process_bot_mentions(config, bot_username)
            if handled:
                LOG.info("Handled %d Telegram request(s)", handled)
        except Exception:
            LOG.exception("Telegram command check failed; will retry")
        sleep_for = min(5.0, max(0.0, next_schedule_check - time.monotonic()))
        deadline = time.monotonic() + sleep_for
        while not stopping and time.monotonic() < deadline:
            time.sleep(min(1.0, deadline - time.monotonic()))
    LOG.info("Stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
