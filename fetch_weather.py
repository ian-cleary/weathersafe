#!/usr/bin/env python3
"""Fetch METAR + TAF for Wick (EGPC, proxy for Thurso) and Humberside
(EGNJ, proxy for Manton) from the free aviation weather API and write
data.json in the WeatherSafe contract format.

No API key needed. Run by GitHub Actions every 10 minutes, and by hand
whenever a fresh file is wanted. Never invents numbers: any field the
source does not provide is written as null and the page shows it as
"unavailable".
"""
import json
import math
import re
import sys
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

API = "https://aviationweather.gov/api/data"
LONDON = ZoneInfo("Europe/London")
KT_TO_MPH = 1.15078

STATIONS = {
    "thurso": {"icao": "EGPC", "source": "Wick Airport"},
    "manton": {"icao": "EGNJ", "source": "Humberside Airport"},
}

COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
           "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]

COMPASS_WORDS = {
    "N": "north", "NNE": "north-north-east", "NE": "north-east",
    "ENE": "east-north-east", "E": "east", "ESE": "east-south-east",
    "SE": "south-east", "SSE": "south-south-east", "S": "south",
    "SSW": "south-south-west", "SW": "south-west", "WSW": "west-south-west",
    "W": "west", "WNW": "west-north-west", "NW": "north-west",
    "NNW": "north-north-west",
}

WX_WORDS = {
    "TS": "thunderstorms", "TSRA": "thundery rain", "TSGR": "thundery hail",
    "+RA": "heavy rain", "RA": "rain", "-RA": "light rain",
    "SHRA": "showers", "SHSN": "snow showers",
    "+SN": "heavy snow", "SN": "snow", "-SN": "light snow",
    "DZ": "drizzle", "FG": "fog", "BR": "mist", "FU": "smoke",
    "GR": "hail", "GS": "small hail", "SQ": "squalls", "FC": "funnel cloud",
}


def compass(deg):
    return COMPASS[int((deg + 11.25) // 22.5) % 16]


def humidity(temp, dew):
    def sat(t):
        return math.exp(17.625 * t / (243.04 + t))
    return round(100 * sat(dew) / sat(temp))


def feels_like(temp, wind_mph):
    """Wind chill (Steadman) when cold and breezy, else the temperature."""
    v_kmh = wind_mph / 0.621371
    if temp <= 10 and v_kmh >= 4.8:
        wc = (13.12 + 0.6215 * temp - 11.37 * v_kmh ** 0.16
              + 0.3965 * temp * v_kmh ** 0.16)
        return round(wc)
    return temp


def parse_metar(raw):
    """Parse a METAR rawOb string into contract fields (nulls where absent)."""
    out = {"temp": None, "feels_like": None, "wind_mph": None, "gust_mph": None,
           "wind_dir": None, "wind_deg": None, "rain_mm": None,
           "rain_now": None, "pressure_hpa": None, "humidity": None,
           "condition": None, "glyph": None}
    tokens = raw.split()

    m = re.search(r"\b(VRB|\d{3})(\d{2,3})(G(\d{2,3}))?KT\b", raw)
    if m:
        if m.group(1) != "VRB":
            out["wind_deg"] = int(m.group(1))
            out["wind_dir"] = compass(int(m.group(1)))
        else:
            out["wind_dir"] = "Variable"
        out["wind_mph"] = round(int(m.group(2)) * KT_TO_MPH)
        out["gust_mph"] = round(int(m.group(4)) * KT_TO_MPH) if m.group(4) else None

    m = re.search(r"\b(M?\d{2})/(M?\d{2})\b", raw)
    if m:
        def num(s):
            return -int(s[1:]) if s.startswith("M") else int(s)
        out["temp"] = num(m.group(1))
        dew = num(m.group(2))
        out["humidity"] = humidity(out["temp"], dew)

    m = re.search(r"\bQ(\d{4})\b", raw)
    if m:
        out["pressure_hpa"] = int(m.group(1))

    if out["temp"] is not None and out["wind_mph"] is not None:
        out["feels_like"] = feels_like(out["temp"], out["wind_mph"])

    # Present weather: groups like -RA, SHRA, TSGR, FG, BR, SN.
    wx = []
    for tok in tokens:
        if re.fullmatch(r"[+-]?(TS|SH|FZ)?(RA|DZ|SN|SG|GR|GS|FG|BR|FU|SQ|FC)", tok):
            wx.append(tok)
    out["rain_now"] = any(w in ("RA", "DZ", "SHRA", "+RA", "TSRA") for w in wx)

    clouds = re.findall(r"\b(FEW|SCT|BKN|OVC)(\d{3})\b", raw)
    cover = clouds[-1][0] if clouds else None
    if "NCD" in tokens or "CLR" in tokens or "SKC" in tokens or "NSC" in tokens:
        cover = "CLR"

    vis = None
    if "CAVOK" in tokens:
        vis = 10000
    else:
        vis_tokens = [t for t in tokens if re.fullmatch(r"\d{4}", t)]
        if vis_tokens:
            vis = int(vis_tokens[0])

    if any(w.startswith("TS") for w in wx):
        out["condition"], out["glyph"] = "Thunderstorms", "storm"
    elif any(w in ("SN", "+SN", "-SN", "SHSN") for w in wx):
        out["condition"], out["glyph"] = "Snow", "snow"
    elif any(w in ("RA", "+RA", "-RA", "SHRA", "DZ", "TSRA") for w in wx):
        heavy = any(w in ("+RA", "TSRA") for w in wx)
        out["condition"] = "Heavy rain" if heavy else ("Light rain" if "-RA" in wx or "DZ" in wx else "Rain")
        out["glyph"] = "rain"
    elif any(w in ("FG", "BR") for w in wx) or (vis is not None and vis < 1000):
        out["condition"], out["glyph"] = "Fog", "fog"
    elif cover in ("OVC", "BKN"):
        out["condition"], out["glyph"] = "Cloudy", "cloud"
    elif cover == "SCT":
        out["condition"], out["glyph"] = "Partly cloudy", "partly"
    else:
        out["condition"], out["glyph"] = "Clear", "clear"
    return out


def describe_section(text):
    """One plain-language sentence for a TAF section (wind, vis, weather, cloud).

    Token-based, so the validity period (2409/2418) can never be mistaken
    for a visibility figure.
    """
    tokens = text.split()
    bits = []
    m = re.search(r"\b(VRB|\d{3})(\d{2,3})(G(\d{2,3}))?KT\b", text)
    if m:
        if m.group(1) == "VRB":
            bits.append("variable wind %d mph" % round(int(m.group(2)) * KT_TO_MPH))
        else:
            bits.append("%s wind %d mph" % (COMPASS_WORDS[compass(int(m.group(1)))],
                                             round(int(m.group(2)) * KT_TO_MPH)))
        if m.group(4):
            bits[-1] += ", gusting %d mph" % round(int(m.group(4)) * KT_TO_MPH)
    if "CAVOK" in tokens:
        bits.append("visibility good")
    else:
        vis_tokens = [t for t in tokens if re.fullmatch(r"\d{4}", t)]
        if vis_tokens:
            v = int(vis_tokens[0])
            # 9999 is the code for 10 km or more.
            bits.append("visibility %s" % ("good" if v >= 9999 else
                                           ("moderate, %d m" % v if v >= 3000 else
                                            "poor, only %d m" % v)))
    wx = [t for t in tokens
          if re.fullmatch(r"[+-]?(TS|SH|FZ)?(RA|DZ|SN|SG|GR|GS|FG|BR|FU|SQ|FC)", t)]
    if wx:
        bits.append(", ".join(WX_WORDS.get(w, w.lower()) for w in wx))
    clouds = re.findall(r"\b(FEW|SCT|BKN|OVC)(\d{3})\b", text)
    if clouds:
        amt, h = clouds[-1]
        words = {"FEW": "a little cloud", "SCT": "scattered cloud",
                 "BKN": "broken cloud", "OVC": "overcast"}[amt]
        bits.append("%s at %s ft" % (words, int(h) * 100))
    if not bits:
        return None
    s = "; ".join(bits)
    return s[0].upper() + s[1:] + "."


def taf_outlook(raw):
    """Plain-language near-term outlook from a raw TAF string."""
    if not raw:
        return None
    # Strip the TAF header (station, issue time, validity period) so its
    # numbers can never leak into the prevailing-section description.
    raw = re.sub(r"^TAF \w+ \d{6}Z \d{4}/\d{4}\s*", "", raw)
    # Split into change groups, keeping their headers.
    parts = re.split(r"\b(BECMG \d{4}/\d{4}|TEMPO \d{4}/\d{4}|PROB\d{2} TEMPO \d{4}/\d{4}|FM\d{6})\b", raw)
    head, rest = parts[0], parts[1:]
    sentences = []
    first = describe_section(head)
    if first:
        sentences.append("Now: " + first)
    for i in range(0, len(rest) - 1, 2):
        header, body = rest[i], rest[i + 1]
        desc = describe_section(body)
        if not desc:
            continue
        if header.startswith("BECMG") or header.startswith("FM"):
            sentences.append("Becoming: " + desc)
        elif header.startswith("PROB"):
            sentences.append("Possibly, at times: " + desc)
        else:
            sentences.append("At times: " + desc)
        if len(sentences) >= 3:
            break
    return " ".join(sentences) if sentences else None


def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "WeatherSafe/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def main():
    data = {"updated_at": datetime.now(LONDON).isoformat()}
    for key, st in STATIONS.items():
        entry = {"source": st["source"]}
        try:
            metars = fetch_json("%s/metar?ids=%s&format=json" % (API, st["icao"]))
            if metars:
                m = metars[0]
                entry.update(parse_metar(m["rawOb"]))
                rt = datetime.fromisoformat(m["reportTime"].replace("Z", "+00:00"))
                entry["observed_at"] = rt.astimezone(LONDON).isoformat()
        except Exception as exc:
            print("METAR %s failed: %s" % (st["icao"], exc), file=sys.stderr)
        try:
            tafs = fetch_json("%s/taf?ids=%s&format=json" % (API, st["icao"]))
            if tafs:
                entry["outlook"] = taf_outlook(tafs[0]["rawTAF"])
        except Exception as exc:
            print("TAF %s failed: %s" % (st["icao"], exc), file=sys.stderr)
            entry["outlook"] = None
        entry.setdefault("outlook", None)
        data[key] = entry
    with open("data.json", "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    print(json.dumps(data, indent=2))


if __name__ == "__main__":
    main()
