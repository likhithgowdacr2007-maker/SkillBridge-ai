#!/usr/bin/env python3
"""Extra daily updates for CareerPath AI (run AFTER your existing hackathon updater).

What it does (it never touches hackathon items or sources.h):
  1. JOBS     - fetches fresh fresher/intern jobs from the Adzuna India API and
                replaces the old auto jobs (k="j", auto=1) in listings.json.
  2. SCHEMES / TRAINING / JOB FAIRS - there is no official API for these, so it
                checks every link (the ones inside index.html and listings.json)
                and records how many work / are gone / are unreachable.
                Nothing is deleted or re-dated: the page still hides items
                after their own date, and the admin fixes broken links.
  3. Writes update-status.json (keys j, s, t, f) and link-report.json.
Standard library only. Needs secrets ADZUNA_APP_ID and ADZUNA_APP_KEY for jobs.
"""
import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(ROOT, "index.html")
LISTINGS = os.path.join(ROOT, "listings.json")
STATUS = os.path.join(ROOT, "update-status.json")
REPORT = os.path.join(ROOT, "link-report.json")
IST = timezone(timedelta(hours=5, minutes=30))
TODAY = datetime.now(IST).date()
UA = "Mozilla/5.0 (compatible; CareerPathAI-updater/1.0; student project)"
KIND_NAMES = {"s": "Schemes", "t": "Training", "f": "Job fairs"}

SENIOR = re.compile(r"senior|\bsr\b|lead|manager|principal|architect|director|head of|\bvp\b", re.I)
QUERIES = ["fresher software engineer", "graduate engineer trainee", "python developer fresher",
           "java developer fresher", "data analyst fresher", "machine learning engineer fresher",
           "web developer fresher", "software engineering intern"]


def get_json(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def clean(s):
    s = re.sub(r"<[^>]+>", "", s or "")
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def fmt(d):
    return f"{d.day} {d.strftime('%b %Y')}"


def still_active(x):
    e = x.get("e")
    if not e:
        return True
    try:
        return datetime.strptime(e, "%Y-%m-%d").date() >= TODAY
    except ValueError:
        return False


# ---------- 1. jobs ----------
def fetch_adzuna():
    app_id, key = os.environ.get("ADZUNA_APP_ID"), os.environ.get("ADZUNA_APP_KEY")
    if not (app_id and key):
        raise RuntimeError("ADZUNA_APP_ID / ADZUNA_APP_KEY secrets are not set")
    out = []
    for q in QUERIES:
        qs = urllib.parse.urlencode({"app_id": app_id, "app_key": key, "results_per_page": 20,
                                     "what": q, "sort_by": "date", "max_days_old": 14,
                                     "content-type": "application/json"})
        data = get_json("https://api.adzuna.com/v1/api/jobs/in/search/1?" + qs)
        for j in data.get("results") or []:
            title = clean(j.get("title"))
            url = j.get("redirect_url") or ""
            if not title or SENIOR.search(title) or not url.startswith("https://"):
                continue
            try:
                posted = datetime.strptime((j.get("created") or "")[:10], "%Y-%m-%d").date()
            except ValueError:
                continue
            lo, hi = j.get("salary_min"), j.get("salary_max")
            if lo:
                est = " (estimated)" if str(j.get("salary_is_predicted")) == "1" else ""
                pay = (f"Rs {lo/1e5:.1f} to {hi/1e5:.1f} lakh per year{est}" if hi
                       else f"From Rs {lo/1e5:.1f} lakh per year{est}")
            else:
                pay = "Not stated. See posting."
            cat = "Data & AI" if re.search(r"data|machine|\bai\b|ml", title.lower()) else "IT & Software"
            item = {
                "k": "j", "ty": "Job", "t": title,
                "o": clean((j.get("company") or {}).get("display_name")) or "Company not stated",
                "d": "Posted " + fmt(posted) + ". Deadline not stated; check the posting.",
                "v": clean((j.get("location") or {}).get("display_name")) or "India",
                "f": pay, "el": "Fresher / entry level (confirm on posting)",
                "l": url, "c": TODAY.isoformat(), "cat": cat, "auto": 1, "src": "Adzuna",
            }
            if lo:
                item["sl"] = round(lo / 1e5, 2)
            out.append(item)
    seen, uniq = set(), []
    for x in out:
        key2 = (norm(x["t"]), norm(x["o"]))
        if key2 not in seen:
            seen.add(key2)
            uniq.append(x)
    return uniq


# ---------- 2. link checks ----------
def read_page_items():
    """Pull the RL arrays (hard-coded listings) out of index.html."""
    try:
        s = open(INDEX, encoding="utf-8").read()
    except OSError:
        return []
    dec, items = json.JSONDecoder(), []
    i = s.find("const RL=")
    if i >= 0:
        try:
            items += dec.raw_decode(s[i + len("const RL="):])[0]
        except ValueError:
            pass
    for m in re.finditer(r"RL\.push\(\.\.\.", s):
        try:
            items += dec.raw_decode(s[m.end():])[0]
        except ValueError:
            pass
    return [x for x in items if isinstance(x, dict)]


def check(url):
    """'ok' | 'gone' (404/410) | 'unreachable' (blocked, slow, DNS, 5xx...)."""
    for method in ("HEAD", "GET"):
        try:
            req = urllib.request.Request(url, method=method, headers={"User-Agent": UA})
            urllib.request.urlopen(req, timeout=12).close()
            return "ok"
        except urllib.error.HTTPError as e:
            if e.code in (404, 410):
                return "gone"
            if method == "HEAD" and e.code in (400, 403, 405, 501):
                continue  # some servers refuse HEAD; try GET
            return "unreachable"
        except Exception:
            return "unreachable"
    return "unreachable"


def verify_links(items):
    todo = [x for x in items if x.get("k") in KIND_NAMES and str(x.get("l", "")).startswith("https://")
            and still_active(x)]
    urls = sorted({x["l"] for x in todo})
    with ThreadPoolExecutor(max_workers=8) as ex:
        res = dict(zip(urls, ex.map(check, urls)))
    summary, report = {}, []
    for k in KIND_NAMES:
        mine = [x for x in todo if x["k"] == k]
        cnt = {"ok": 0, "gone": 0, "unreachable": 0}
        for x in mine:
            r = res[x["l"]]
            cnt[r] += 1
            if r != "ok":
                report.append({"kind": KIND_NAMES[k], "title": x.get("t"), "link": x["l"], "result": r})
        summary[k] = (len(mine), cnt)
    return summary, report


# ---------- main ----------
def main():
    listings = load(LISTINGS, [])
    if not isinstance(listings, list):
        listings = []
    status = load(STATUS, {})
    if not isinstance(status, dict):
        status = {}
    sources = status.get("sources") if isinstance(status.get("sources"), dict) else {}

    # jobs
    prev_jobs = [x for x in listings if isinstance(x, dict) and x.get("auto") and x.get("k") == "j"]
    rest = [x for x in listings if isinstance(x, dict) and not (x.get("auto") and x.get("k") == "j")]
    manual_keys = {(norm(x.get("t")), norm(x.get("o"))) for x in rest if x.get("k") == "j"}
    try:
        jobs = [x for x in fetch_adzuna() if (norm(x["t"]), norm(x["o"])) not in manual_keys]
        if not jobs:
            raise RuntimeError("source returned no usable items")
        sources["j"] = [{"name": "Adzuna India", "ok": True, "count": len(jobs)}]
        print(f"Adzuna: {len(jobs)} jobs")
    except Exception as ex:
        jobs = [x for x in prev_jobs if still_active(x)]
        sources["j"] = [{"name": "Adzuna India", "ok": False, "count": len(jobs), "error": str(ex)[:200]}]
        print(f"Adzuna FAILED: {ex} (kept {len(jobs)} earlier jobs)", file=sys.stderr)
    out = rest + jobs

    # schemes / training / fairs link verification
    summary, report = verify_links(read_page_items() + out)
    for k, (total, c) in summary.items():
        note = f"Links checked today: {c['ok']} working"
        if c["gone"]:
            note += f", {c['gone']} gone (fix in admin)"
        if c["unreachable"]:
            note += f", {c['unreachable']} could not be reached"
        sources[k] = [{"name": note, "ok": True, "count": total}]
        print(f"{KIND_NAMES[k]}: {note}")

    status["updated"] = TODAY.isoformat()
    status["sources"] = sources
    with open(LISTINGS, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1, ensure_ascii=False)
    with open(STATUS, "w", encoding="utf-8") as f:
        json.dump(status, f, indent=1, ensure_ascii=False)
    with open(REPORT, "w", encoding="utf-8") as f:
        json.dump({"checked": TODAY.isoformat(), "needs_attention": report}, f, indent=1, ensure_ascii=False)


if __name__ == "__main__":
    main()
