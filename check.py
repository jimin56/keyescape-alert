#!/usr/bin/env python3
"""Scheduled checks that push to my phone via ntfy.

Sources live in config.json (not committed; see config.example.json). Each
adapter returns the currently "open" entries for the dates/times I care about.
When something flips from closed to open, I get a push. Nothing else.

  python3 check.py                                # one pass
  python3 check.py --watch-until 10:40 --every 20 # repeat until HH:MM (local tz)
  python3 check.py --open-watch --until 10:20 --every 10
  python3 check.py --status                       # print current state, no side effects
  python3 check.py --test                         # send a test push
"""
import argparse
import datetime as dt
import http.cookiejar
import json
import logging
import logging.handlers
import os
import pathlib
import re
import sys
import time
import urllib.parse
import urllib.request
import zoneinfo

HERE = pathlib.Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"
STATE_PATH = HERE / "state.json"        # committed: what was open at the last pass
RUNTIME_PATH = HERE / "runtime.json"    # not committed: per-job timing notes
LOG_PATH = HERE / "logs" / "check.log"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/17.0 Safari/605.1.15")
TZ = zoneinfo.ZoneInfo(os.environ.get("TZ_NAME", "Asia/Seoul"))
WD = "월화수목금토일"
FAIL_ALERT_AFTER = 15
REQUEST_GAP = 0.4


def setup_log():
    LOG_PATH.parent.mkdir(exist_ok=True)
    h = logging.handlers.RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    lg = logging.getLogger("check")
    lg.setLevel(logging.INFO)
    lg.addHandler(h)
    lg.addHandler(logging.StreamHandler(sys.stdout))
    return lg


log = setup_log()


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def save_json(path, obj):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def strip_tags(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s)).strip()


def fmt_date(iso):
    d = dt.date.fromisoformat(iso)
    return f"{d.month}/{d.day}({WD[d.weekday()]})"


def fill(template, **kw):
    """Format every string value of a params template."""
    return {k: (v.format(**kw) if isinstance(v, str) else v) for k, v in (template or {}).items()}


def date_kw(d, **extra):
    return dict(date=d.isoformat(), date_compact=d.strftime("%Y%m%d"), month=d.strftime("%Y-%m"), **extra)


# ---------- filters (source overrides global) ----------

def _pick(src, cfg, key, default):
    return src[key] if key in src else cfg.get(key, default)


def wanted_date(src, cfg, date):
    return date.weekday() in set(_pick(src, cfg, "weekdays", [4, 5, 6]))


def time_ok(src, cfg, date, hhmm):
    wd, ko = str(date.weekday()), WD[date.weekday()]
    allow = _pick(src, cfg, "allow_times", {}) or {}
    v = allow.get(wd) or allow.get(ko)
    if v and hhmm not in v:
        return False
    rng = _pick(src, cfg, "allow_range", {}) or {}
    r = rng.get(wd) or rng.get(ko)
    if r and not (r[0] <= hhmm <= r[1]):
        return False
    mh = _pick(src, cfg, "min_hour", {}) or {}
    return int(hhmm[:2]) >= int(mh.get(wd) or mh.get(ko) or 0)


# ---------- adapter "daily-json": one POST per (item, date) returns the day's entries ----------

def json_post(url, params, referer):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(params).encode(), headers={
        "User-Agent": UA, "Referer": referer, "X-Requested-With": "XMLHttpRequest"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def scan_daily(src, cfg, today, state, fetch=None):
    fetch = fetch or json_post
    f = {"ok": "status", "list": "data", "hh": "hh", "mm": "mm", "closed_key": "enable", "closed_val": "N",
         **src.get("fields", {})}
    out, fetched, errors = {}, set(), []
    days = int(src.get("window_days", 7))
    dates = [today + dt.timedelta(days=i) for i in range(days) if wanted_date(src, cfg, today + dt.timedelta(days=i))]
    for it in src["items"]:
        sid = f"{src['id']}:{it['item']}"
        kw = dict(group=src.get("group", ""), item=it["item"], info=it.get("info", ""))
        page = src["page"].format(**kw)
        for d in dates:
            try:
                j = fetch(src["api"], fill(src["params"], **date_kw(d, **kw)), page)
            except Exception as e:
                errors.append(f"{sid} {d}: {type(e).__name__}")
                continue
            fetched.add((sid, d.isoformat()))
            if not j.get(f["ok"]):
                continue
            for s in j.get(f["list"]) or []:
                hhmm = f"{s[f['hh']]}:{s[f['mm']]}"
                if s.get(f["closed_key"]) == f["closed_val"] or not time_ok(src, cfg, d, hhmm):
                    continue
                out[f"{sid}|{d.isoformat()}|{hhmm}"] = {"src": sid, "label": it["name"], "date": d.isoformat(),
                                                         "time": hhmm, "click": page}
            time.sleep(REQUEST_GAP)
    return out, fetched, errors


# ---------- adapter "tiered-html": days -> times -> remaining, HTML fragments ----------

def html_session(page, base):
    """Cookie-keeping session seeded by loading the page once."""
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    opener.open(urllib.request.Request(page, headers={"User-Agent": UA}), timeout=15).read()

    def post(path, params):
        req = urllib.request.Request(base + path, data=urllib.parse.urlencode(params).encode(), headers={
            "User-Agent": UA, "Referer": page, "X-Requested-With": "XMLHttpRequest"})
        with opener.open(req, timeout=15) as r:
            return r.read().decode("utf-8", "replace")
    return post


def tiered_due(sid, every, now, force=False):
    """Heavy source: within one job use runtime.json; across jobs use a time bucket aligned to the 5-min cron."""
    if force:
        return True
    last = load_json(RUNTIME_PATH, {}).get("last", {}).get(sid)
    if last:
        return now - dt.datetime.fromisoformat(last) >= dt.timedelta(minutes=every, seconds=-20)
    return now.minute % every < 5


def tiered_mark(sid, now):
    rt = load_json(RUNTIME_PATH, {})
    rt.setdefault("last", {})[sid] = now.isoformat(timespec="seconds")
    save_json(RUNTIME_PATH, rt)


def tiered_dates(post, src, today):
    st = src["steps"]["days"]
    months = sorted({(today + dt.timedelta(days=i)).strftime("%Y-%m") for i in range(0, 32)})
    dates = set()
    for m in months:
        txt = post(st["path"], fill(st["params"], item=src["item"], month=m))
        dates |= {x.strip() for x in txt.split(",") if re.fullmatch(r"\d{4}-\d{2}-\d{2}", x.strip())}
        time.sleep(REQUEST_GAP)
    return dates


def scan_tiered(src, cfg, today, state, post=None, force=False):
    sid, item, steps = src["id"], src["item"], src["steps"]
    out, fetched, errors = {}, set(), []
    now = dt.datetime.now(TZ)
    if not tiered_due(sid, int(src.get("every_minutes", 15)), now, force):
        return out, fetched, errors
    try:
        post = post or html_session(src["page"].format(item=item), src["base"])
        dates = tiered_dates(post, src, today)
    except Exception as e:
        errors.append(f"{sid} days: {type(e).__name__}")
        return out, fetched, errors

    maxd = max(dates) if dates else None
    prev = state.setdefault("maxdate", {}).get(sid)
    if maxd and prev and maxd > prev:
        log.info("%s: range extended to %s", sid, maxd)
    if maxd:
        state["maxdate"][sid] = maxd

    cache = state.setdefault("times", {}).setdefault(sid, {})
    for d in list(cache):
        if d < today.isoformat():
            cache.pop(d)
    targets = sorted(d for d in dates if d >= today.isoformat() and wanted_date(src, cfg, dt.date.fromisoformat(d)))
    tstep, rstep = steps["times"], steps["remain"]
    for d in targets:
        dd = dt.date.fromisoformat(d)
        try:
            if not cache.get(d):
                html = post(tstep["path"], fill(tstep["params"], item=item, **date_kw(dd)))
                cache[d] = re.findall(tstep["regex"], html)
                time.sleep(REQUEST_GAP)
            for tid, hhmm in cache[d]:
                if not time_ok(src, cfg, dd, hhmm):
                    continue
                txt = strip_tags(post(rstep["path"], fill(rstep["params"], item=item, time_id=tid)))
                time.sleep(REQUEST_GAP)
                m = re.search(rstep["regex"], txt)
                val = m.group(1).strip() if m else ""
                closed = (not val or any(w in txt for w in rstep.get("closed", []))
                          or (val.isdigit() and int(val) == 0))
                if closed:
                    continue
                out[f"{sid}|{d}|{hhmm}"] = {"src": sid, "label": src["name"], "date": d, "time": hhmm,
                                            "info": val, "click": src["page"].format(item=item)}
            fetched.add((sid, d))
        except Exception as e:
            errors.append(f"{sid} {d}: {type(e).__name__}")
    tiered_mark(sid, now)
    return out, fetched, errors


ADAPTERS = {"daily-json": scan_daily, "tiered-html": scan_tiered}


# ---------- ntfy ----------

def ntfy(cfg, title, message, click=None, priority=4, tags=None):
    payload = {"topic": cfg["ntfy_topic"], "title": title, "message": message, "priority": priority}
    if click:
        payload["click"] = click
    if tags:
        payload["tags"] = tags
    req = urllib.request.Request(cfg.get("ntfy_server", "https://ntfy.sh"),
                                 data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                 headers={"Content-Type": "application/json", "User-Agent": "check"})
    with urllib.request.urlopen(req, timeout=15) as r:
        r.read()


def notify_new(cfg, new):
    by_src = {}
    for s in new.values():
        by_src.setdefault(s["src"], []).append(s)
    tmpl = cfg.get("alert_title", "{label} {date} {time}")
    for slots in by_src.values():
        slots.sort(key=lambda s: (s["date"], s["time"]))
        first = slots[0]
        by_date = {}
        for s in slots:
            by_date.setdefault(s["date"], []).append(s["time"] + (f" ({s['info']})" if s.get("info") else ""))
        lines = [f"{fmt_date(d)} {', '.join(ts)}" for d, ts in sorted(by_date.items())]
        title = tmpl.format(label=first["label"], date=fmt_date(first["date"]), time=first["time"])
        if len(slots) > 1:
            title += f" +{len(slots) - 1}"
        ntfy(cfg, title, "\n".join(lines) + "\n" + cfg.get("alert_footer", ""), click=first["click"], priority=5, tags=["tada"])
    log.info("notified %d source(s), %d entr%s", len(by_src), len(new), "y" if len(new) == 1 else "ies")


# ---------- main ----------

def sources_of(cfg):
    defaults = cfg.get("type_defaults", {})
    return [{**defaults.get(s["type"], {}), **s} for s in cfg["sources"]]


def run(cfg, today=None, notify=True, force_heavy=False):
    today = today or dt.datetime.now(TZ).date()
    state = load_json(STATE_PATH, {"open": {}, "fails": 0, "fail_notified": False})
    open_now, fetched, errors, new, failed_push = {}, set(), [], {}, set()
    skip_heavy = os.environ.get("SKIP_HEAVY") == "1"   # e.g. a local runner that leaves heavy sources to CI
    for src in sources_of(cfg):
        if skip_heavy and src["type"] == "tiered-html":
            continue
        kw = {"force": True} if (force_heavy and src["type"] == "tiered-html") else {}
        o, f, e = ADAPTERS[src["type"]](src, cfg, today, state, **kw)
        open_now.update(o)
        fetched |= f
        errors += e
        # notify per source as soon as it finishes (don't wait for the slower ones)
        n = {k: v for k, v in o.items() if k not in state["open"]}
        if n and notify:
            try:
                notify_new(cfg, n)
            except Exception as ex:
                log.error("push failed: %s", type(ex).__name__)
                failed_push |= set(n)
        new.update(n)

    kept = {k: v for k, v in state["open"].items()
            if (v["src"], v["date"]) not in fetched and v["date"] >= today.isoformat()}
    merged = {**kept, **open_now}
    for k in failed_push:  # retry next pass
        merged.pop(k, None)

    if errors and not fetched:
        state["fails"] = state.get("fails", 0) + 1
        if state["fails"] >= FAIL_ALERT_AFTER and not state.get("fail_notified") and notify:
            ntfy(cfg, cfg.get("fail_title", "check failing"), f"{state['fails']} consecutive failures.", priority=3, tags=["warning"])
            state["fail_notified"] = True
    else:
        state["fails"], state["fail_notified"] = 0, False

    for e in errors:
        log.warning("fetch failed: %s", e)
    log.info("pass %s | pairs %d | open %d | new %d", today, len(fetched), len(merged), len(new))

    state["open"] = merged
    save_json(STATE_PATH, state)
    return new, merged, errors


def watch(cfg, until_hhmm, every, max_minutes=30):
    h, m = map(int, until_hhmm.split(":"))
    start = dt.datetime.now(TZ)
    deadline = min(start.replace(hour=h, minute=m, second=0, microsecond=0), start + dt.timedelta(minutes=max_minutes))
    n = 0
    while True:
        run(cfg)
        n += 1
        if dt.datetime.now(TZ) + dt.timedelta(seconds=every) > deadline:
            break
        time.sleep(every)
    log.info("watch done: %d passes", n)


def open_watch(cfg, until_hhmm, every, post_factory=None, max_minutes=40):
    """Poll only the cheap 'days' step until the sale range extends; then alert and do a full pass."""
    srcs = [s for s in sources_of(cfg) if s["type"] == "tiered-html"]
    if not srcs:
        return
    today = dt.datetime.now(TZ).date()
    state = load_json(STATE_PATH, {})
    baseline = {s["id"]: state.get("maxdate", {}).get(s["id"]) for s in srcs}
    posts = {}
    h, m = map(int, until_hhmm.split(":"))
    start = dt.datetime.now(TZ)
    deadline = min(start.replace(hour=h, minute=m, second=0, microsecond=0), start + dt.timedelta(minutes=max_minutes))
    tmpl = cfg.get("open_title", "{label}: {first}~{last}")
    n = 0
    while True:
        n += 1
        for src in srcs:
            sid = src["id"]
            try:
                if sid not in posts:
                    posts[sid] = (post_factory or html_session)(src["page"].format(item=src["item"]), src["base"])
                dates = tiered_dates(posts[sid], src, today)
            except Exception as e:
                log.warning("open-watch fetch failed %s: %s", sid, type(e).__name__)
                continue
            maxd = max(dates) if dates else None
            if not maxd:
                continue
            if baseline[sid] is None:
                baseline[sid] = maxd
                continue
            if maxd > baseline[sid]:
                new_dates = sorted(d for d in dates if d > baseline[sid])
                log.info("%s: range extended (%d new dates)", sid, len(new_dates))
                ntfy(cfg, tmpl.format(label=src["name"], first=fmt_date(new_dates[0]), last=fmt_date(new_dates[-1])),
                     cfg.get("open_body", ""), click=cfg.get("open_click") or src["page"].format(item=src["item"]),
                     priority=5, tags=["rotating_light"])
                baseline[sid] = maxd
                run(cfg, force_heavy=True)
                time.sleep(60)
                run(cfg, force_heavy=True)
                return
        if dt.datetime.now(TZ) + dt.timedelta(seconds=every) > deadline:
            break
        time.sleep(every)
    log.info("open-watch done: %d polls, no change", n)


def status(cfg):
    today = dt.datetime.now(TZ).date()
    tmp = {"open": {}}
    print(f"today {today}")
    for src in sources_of(cfg):
        kw = {"force": True} if src["type"] == "tiered-html" else {}
        o, f, e = ADAPTERS[src["type"]](src, cfg, today, tmp, **kw)
        labels = {}
        for v in o.values():
            labels.setdefault(v["label"], []).append(f"{fmt_date(v['date'])} {v['time']}" + (f" ({v['info']})" if v.get("info") else ""))
        names = [t["name"] for t in src.get("items", [])] or [src.get("name", src["id"])]
        for n in names:
            print(f"- {n}: " + (", ".join(sorted(labels.get(n, []))) or "-"))
        dates = sorted({d for _, d in f})
        print(f"  (dates: {', '.join(fmt_date(d) for d in dates) or 'none'})")
        for x in e:
            print("  ! fetch failed:", x)


def load_config():
    cfg = load_json(CONFIG_PATH, None)
    if not cfg:
        sys.exit("config.json missing (see config.example.json)")
    if os.environ.get("NTFY_TOPIC"):
        cfg["ntfy_topic"] = os.environ["NTFY_TOPIC"]
    if not cfg.get("ntfy_topic"):
        sys.exit("ntfy_topic missing (config.json or NTFY_TOPIC)")
    return cfg


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--watch-until", metavar="HH:MM")
    ap.add_argument("--open-watch", action="store_true")
    ap.add_argument("--until", metavar="HH:MM")
    ap.add_argument("--every", type=int, default=20)
    args = ap.parse_args()
    cfg = load_config()
    if args.test:
        ntfy(cfg, "test", "check.py is connected.", tags=["bell"])
        print("sent")
    elif args.status:
        status(cfg)
    elif args.watch_until:
        watch(cfg, args.watch_until, args.every)
    elif args.open_watch:
        open_watch(cfg, args.until or "10:20", args.every)
    else:
        run(cfg)
