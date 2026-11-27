"""Assistant tools: weather the hub's voice/chat assistant can look up.

Two read-only lookups for every mode: the weather now and a forecast for a day,
part of a day or the next few days. Answers are small dicts of speech-ready
strings (rounded, with units, times in the forecast location's local time like
"3 pm") so the model can answer in a few words.
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import date, datetime, timedelta, timezone

from bdh_sdk import ALL_MODES
from bdh_sdk.tools import Tool, ToolError

from . import forecast as fc
from .alerts import get_alerts
from .views import COMPASS, Units, default_units, utc_now

CURRENT_WEATHER = "current_weather"
WEATHER_FORECAST = "weather_forecast"

UNAVAILABLE = "The weather service isn't answering right now."
NO_LOCATION = "The hub's location isn't set, so there's no weather here."
STALE = "The weather data is out of date right now."
STALE_AFTER = timedelta(hours=3)
# Snow water equivalent: Open-Meteo's precipitation includes snowfall at ~0.7 cm per mm.
SNOW_CM_PER_MM = 0.7
# A precipitating condition that shows up in this many hours of a period gets mentioned.
AT_TIMES_HOURS = 2
DEFAULT_DAYS = 3
WINDS = dict(zip(COMPASS, ("north", "northeast", "east", "southeast", "south", "southwest", "west",
                           "northwest")))
PERIODS = {
    "morning": ("this morning", 6, 12),
    "afternoon": ("this afternoon", 12, 18),
    "evening": ("this evening", 18, 24),
    "night": ("tonight", 18, 30),
    "overnight": ("tonight", 18, 30),
}
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
MONTHS = {m: i + 1 for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep",
                                          "oct", "nov", "dec"))}
_FILLER = re.compile(r"\b(this|the|on|next|coming|for|at|in)\b")


def tools(mode=None):
    if fc.location() is None:
        return []
    return [
        Tool(CURRENT_WEATHER, "Weather now",
             "The weather at the lodge right now: temperature, feels-like, conditions, wind, today's high "
             "and low, sunrise/sunset and any weather warnings. Use for 'how cold is it?', 'is it snowing?', "
             "'what's it like outside?'.",
             modes=ALL_MODES, readonly=True),
        Tool(WEATHER_FORECAST, "Weather forecast",
             "Forecast for a day or part of one (tonight, tomorrow morning, Saturday, the weekend, a date) "
             "or the next few days: conditions, high/low, feels-like, chance and amount of snow or rain, "
             "wind. Use for 'will it snow tonight?', 'what should I wear skiing tomorrow?'.",
             params={"type": "object", "properties": {
                 "day": {"type": "string", "maxLength": 40,
                         "description": "When, in the guest's words: today, tonight, this afternoon, tomorrow, "
                                        "tomorrow night, a weekday (e.g. saturday), weekend, or a date like "
                                        "2026-12-24. Leave out with days for a multi-day outlook."},
                 "days": {"type": "integer", "minimum": 1, "maximum": 7,
                          "description": "How many days from today (1-7) when no day is given; default 3."},
             }},
             modes=ALL_MODES, readonly=True),
    ]


# ── data ──

def _forecast(now):
    """The cached Open-Meteo payload and the location's local time (naive), or ToolError."""
    if fc.location() is None:
        raise ToolError(NO_LOCATION)
    data = fc.get_weather()
    forecast = (data or {}).get("forecast") or {}
    current = forecast.get("current") or {}
    if current.get("temperature_2m") is None:
        raise ToolError(UNAVAILABLE)
    local = (now + timedelta(seconds=forecast.get("utc_offset_seconds", 0))).replace(tzinfo=None)
    if current.get("time") and local - datetime.fromisoformat(current["time"]) > STALE_AFTER:
        raise ToolError(STALE)
    return data, forecast, local


def _col(table, key, i):
    values = table.get(key)
    return values[i] if values and i < len(values) else None


def _hours(hourly):
    out = []
    for i, iso in enumerate(hourly.get("time") or ()):
        precip, snow = _col(hourly, "precipitation", i), _col(hourly, "snowfall", i) or 0
        rain = max(0.0, precip - snow / SNOW_CM_PER_MM) if precip is not None else 0
        out.append({
            "t": datetime.fromisoformat(iso), "temp": _col(hourly, "temperature_2m", i),
            "feels": _col(hourly, "apparent_temperature", i), "code": _col(hourly, "weather_code", i),
            "pop": _col(hourly, "precipitation_probability", i), "wind": _col(hourly, "wind_speed_10m", i),
            "gust": _col(hourly, "wind_gusts_10m", i), "dir": _col(hourly, "wind_direction_10m", i),
            "snow": snow, "rain": rain,
        })
    return out


# ── speech ──

def _time(t):
    if t.hour == 0 and t.minute == 0:
        return "midnight"
    if t.hour == 12 and t.minute == 0:
        return "noon"
    minutes = f":{t:%M}" if t.minute else ""
    return f"{t.hour % 12 or 12}{minutes} {'am' if t.hour < 12 else 'pm'}"


def _label(code):
    return fc.describe(code)[1] if code is not None else None


def _temp(u, c):
    value = u.t(c)
    return None if value is None else f"{value}{u.temp}"


def _wind(u, speed, gust=None, direction=None):
    speed = u.s(speed)
    if speed is None:
        return None
    text = f"{speed} {u.speed}"
    if direction is not None and speed:
        text += f" from the {WINDS[COMPASS[round(direction / 45) % 8]]}"
    gust = u.s(gust)
    if gust and gust >= speed + 10:
        text += f", gusts {gust}"
    return text


def _amount(value, unit):
    if unit == "in":
        return f"{value:g} in" if value else "a trace of"
    return f"{round(value)} {unit}" if value >= 1 else f"under 1 {unit}"


def _precip(u, snow_cm, rain_mm):
    """"5 cm snow", "2 mm rain", both, or "none" (trace amounts don't count)."""
    parts = []
    if snow_cm >= 0.2:
        parts.append(f"{_amount(u.snow(snow_cm), u.snow_unit)} snow")
    if rain_mm >= 0.3:
        parts.append(f"{_amount(u.rain(rain_mm), u.rain_unit)} rain")
    return " and ".join(parts) or "none"


def _condition(codes):
    """The usual condition over some hours, plus any rain/snow that comes and goes."""
    codes = [c for c in codes if c is not None]
    if not codes:
        return None
    counts = Counter(codes)
    main = counts.most_common(1)[0][0]
    text = _label(main)
    wet = [(n, c) for c, n in counts.items() if c >= 51 and c != main and n >= AT_TIMES_HOURS]
    if main < 51 and wet:
        text += f", {_label(max(wet)[1]).lower()} at times"
    return text


def _compact(entry):
    return {k: v for k, v in entry.items() if v not in (None, "", [])}


def _summary(u, when, hours, code=None, high=None, low=None, pop=None):
    """One forecast entry for ``hours`` (daily values win where given)."""
    temps = [h["temp"] for h in hours if h["temp"] is not None]
    feels = [h["feels"] for h in hours if h["feels"] is not None]
    winds = [h for h in hours if h["wind"] is not None]
    pops = [h["pop"] for h in hours if h["pop"] is not None]
    snow, rain = sum(h["snow"] for h in hours), sum(h["rain"] for h in hours)
    windy = max(winds, key=lambda h: h["wind"]) if winds else None
    gusts = [h["gust"] for h in hours if h["gust"] is not None]
    snowing = next((h["t"] for h in hours if h["snow"] >= 0.1), None)
    if pop is None and pops:
        pop = max(pops)
    entry = {
        "when": when,
        "condition": _label(code) if code is not None else _condition([h["code"] for h in hours]),
        "high": _temp(u, high if high is not None else (max(temps) if temps else None)),
        "low": _temp(u, low if low is not None else (min(temps) if temps else None)),
        "feels_like_low": _temp(u, min(feels)) if feels else None,
        "precip_chance": f"{pop}%" if pop is not None else None,
        "precip": _precip(u, snow, rain),
        "snow_starts": _time(snowing) if snowing and snow >= 0.2 and snowing > hours[0]["t"] else None,
        "wind": _wind(u, windy["wind"], max(gusts) if gusts else None, windy["dir"]) if windy else None,
    }
    return _compact(entry)


def _alerts(now, offset):
    try:
        alerts = get_alerts(now)
    except Exception:
        return []
    out = []
    for a in alerts[:3]:
        text = a["title"]
        end = a.get("end")
        if end:
            local = (end.astimezone(timezone.utc) + offset).replace(tzinfo=None)
            text += f" until {_time(local)}" + ("" if local.date() == (now + offset).date() else f" {local:%A}")
        out.append(text)
    return out


# ── days ──

def _day_name(d, today):
    if d == today:
        return "today"
    if d == today + timedelta(days=1):
        return "tomorrow"
    return f"{d:%A}"


def _parse_date(text, today):
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if m:
        return date(*map(int, m.groups()))
    m = (re.fullmatch(r"([a-z]+)\.? (\d{1,2})(?:st|nd|rd|th)?", text)
         or re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th)? (?:of )?([a-z]+)", text))
    if m:
        month, day = m.groups() if m.group(1).isalpha() else m.groups()[::-1]
        month = MONTHS.get(month[:3])
        if month:
            d = date(today.year, month, int(day))
            return d if d >= today - timedelta(days=1) else date(today.year + 1, month, int(day))
    return None


def _weekday(word):
    """0-6 for "saturday", "saturdays" or "sat", else None."""
    word = word.rstrip(".")
    if len(word) < 3:
        return None
    return next((i for i, w in enumerate(WEEKDAYS) if w == word.rstrip("s") or w.startswith(word)), None)


def parse_day(text, local):
    """``text`` → ``(date, period or None)``, or ``("weekend", None)``; raises ToolError.

    ``period`` is a key of :data:`PERIODS`. Today with no period means the rest of today.
    """
    today = local.date()
    s = " ".join(_FILLER.sub(" ", text.lower().strip(" ?.!,")).split())
    if s in ("weekend", "weekend days"):
        return "weekend", None
    if s in ("tonight", "today night"):
        return today, "night"
    if s in ("", "now", "right now", "today", "later", "later today", "rest of today"):
        return today, None
    period = None
    for word in s.split():
        if word in PERIODS:
            period = word
    if period:
        s = " ".join(w for w in s.split() if w != period).strip()
    if s in ("", "today"):
        d = today
    elif s == "tomorrow":
        d = today + timedelta(days=1)
    elif s in ("day after tomorrow", "day after"):
        d = today + timedelta(days=2)
    elif _weekday(s) is not None:
        d = today + timedelta(days=(_weekday(s) - today.weekday()) % 7)
    else:
        try:
            d = _parse_date(s, today)
        except ValueError:
            d = None
        if d is None:
            raise ToolError(f"I can't tell which day {text!r} is; say today, tonight, tomorrow, a weekday "
                            "or a date.")
    if period == "overnight":
        period = "night"
    return d, period


def _day_entry(u, forecast, hours, d, local):
    daily = forecast.get("daily") or {}
    times = daily.get("time") or []
    if d.isoformat() not in times:
        return None
    i = times.index(d.isoformat())
    day_hours = [h for h in hours if h["t"].date() == d]
    when = _day_name(d, local.date())
    if d == local.date():
        # Snow and rain still to come today; the high and low are the whole day's.
        later = [h for h in day_hours if h["t"] >= local.replace(minute=0, second=0, microsecond=0)]
        return _summary(u, when, later or day_hours, None, _col(daily, "temperature_2m_max", i),
                        _col(daily, "temperature_2m_min", i))
    return _summary(u, when, day_hours, _col(daily, "weather_code", i), _col(daily, "temperature_2m_max", i),
                    _col(daily, "temperature_2m_min", i), _col(daily, "precipitation_probability_max", i))


def _last_day(forecast, local):
    times = (forecast.get("daily") or {}).get("time") or []
    return _day_name(date.fromisoformat(times[-1]), local.date()) if times else "today"


def _period_entry(u, forecast, hours, d, period, local):
    name, start_h, end_h = PERIODS[period]
    day = datetime.combine(d, datetime.min.time())
    start, end = day + timedelta(hours=start_h), day + timedelta(hours=end_h)
    if period == "night" and d == local.date() and local.hour < 6:  # it's already tonight
        start, end = day, day + timedelta(hours=6)
    if end <= local:
        raise ToolError(f"{name.capitalize()} is already over.")
    start = max(start, local.replace(minute=0, second=0, microsecond=0))
    span = [h for h in hours if start <= h["t"] < end]
    if not span:
        raise ToolError(f"The forecast only goes as far as {_last_day(forecast, local)}.")
    if d != local.date():
        when = _day_name(d, local.date())
        name = f"{when} {'night' if period == 'night' else period}"
    return _summary(u, f"{name} ({_time(start)} to {_time(end)})", span)


# ── running ──

def _current(args, now):
    data, forecast, local = _forecast(now)
    u = Units(default_units())
    c, daily = forecast["current"], forecast.get("daily") or {}
    offset = timedelta(seconds=forecast.get("utc_offset_seconds", 0))
    today = local.date().isoformat()
    i = (daily.get("time") or []).index(today) if today in (daily.get("time") or []) else None
    sun = []
    for key in ("sunrise", "sunset"):
        nxt = next((t for t in map(datetime.fromisoformat, daily.get(key) or ()) if t > local), None)
        if nxt:
            sun.append((nxt, f"{key} {_time(nxt)}" + ("" if nxt.date() == local.date() else " tomorrow")))
    aqi = _air_quality((data or {}).get("aqi"))
    return _compact({
        "place": fc.place_name(),
        "as_of": _time(datetime.fromisoformat(c["time"])) if c.get("time") else None,
        "condition": _label(c.get("weather_code")),
        "temp": _temp(u, c.get("temperature_2m")),
        "feels_like": _temp(u, c.get("apparent_temperature")),
        "wind": _wind(u, c.get("wind_speed_10m"), c.get("wind_gusts_10m"), c.get("wind_direction_10m")),
        "humidity": f"{c['relative_humidity_2m']}%" if c.get("relative_humidity_2m") is not None else None,
        "today_high": _temp(u, _col(daily, "temperature_2m_max", i)) if i is not None else None,
        "today_low": _temp(u, _col(daily, "temperature_2m_min", i)) if i is not None else None,
        "next_sun": min(sun)[1] if sun else None,
        "air_quality": aqi,
        "alerts": _alerts(now, offset),
    })


def _air_quality(value):
    """Only worth saying when it isn't good."""
    if value is None or value <= 50:
        return None
    for limit, label in ((100, "moderate"), (150, "unhealthy for sensitive groups")):
        if value <= limit:
            return f"{label} (AQI {round(value)})"
    return f"unhealthy (AQI {round(value)})"


def _forecast_tool(args, now):
    _, forecast, local = _forecast(now)
    u = Units(default_units())
    hours = _hours(forecast.get("hourly") or {})
    offset = timedelta(seconds=forecast.get("utc_offset_seconds", 0))
    entries = []
    if args.get("day"):
        d, period = parse_day(args["day"], local)
        if d == "weekend":
            today = local.date()
            dates = [today + timedelta(days=n) for n in range(7)]
            dates = [x for x in dates if x.weekday() >= 5][:2]
        elif period:
            entries = [_period_entry(u, forecast, hours, d, period, local)]
            dates = []
        else:
            if d < local.date():
                raise ToolError("That day has passed; I only have the forecast ahead.")
            dates = [d]
    else:
        dates = [local.date() + timedelta(days=n) for n in range(args.get("days") or DEFAULT_DAYS)]
    for d in dates:
        entry = _day_entry(u, forecast, hours, d, local)
        if entry is None:
            if not entries:
                raise ToolError(f"The forecast only goes as far as {_last_day(forecast, local)}.")
            break
        entries.append(entry)
    return _compact({"place": fc.place_name(), "forecast": entries, "alerts": _alerts(now, offset)})


_RUNNERS = {CURRENT_WEATHER: _current, WEATHER_FORECAST: _forecast_tool}


def run_tool(tool_id, args, mode=None):
    runner = _RUNNERS.get(tool_id)
    if runner is None:
        raise ToolError(f"Unknown tool {tool_id!r}")
    return runner(args or {}, utc_now())
