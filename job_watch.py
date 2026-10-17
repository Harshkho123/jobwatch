"""
Job watcher: SRE / DevOps roles in India, posted in the last 2 days,
matched against Harshal's resume skills. Emails new matches.

Usage:
    python job_watch.py --test      # send a test email
    python job_watch.py --dry-run   # show matches, no email, nothing saved
    python job_watch.py             # normal run (schedule this every 5 min)

Add companies in companies.txt (one per line:  Name | career page URL).
"""
import html
import json
import os
import re
import smtplib
import sys
import time
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

HERE = os.path.dirname(os.path.abspath(__file__))
SEEN_FILE = os.path.join(HERE, "seen.json")
COMPANIES_FILE = os.path.join(HERE, "companies.txt")

# ---------------------------------------------------------------- .env
env_path = os.path.join(HERE, ".env")
if os.path.exists(env_path):
    for line in open(env_path, encoding="utf-8"):
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

# ------------------------------------------------------------ settings
MAX_AGE_DAYS = 4         # only jobs posted within this many days
MIN_SKILLS = 3            # job description must match at least this many skill groups
HEADERS = {"User-Agent": "Mozilla/5.0 (personal job-watch script)"}

# Search words sent to each career site
SEARCH_TERMS = ["site reliability", "sre", "devops", "kubernetes", "platform engineer"]

# Job TITLE must match one of these...
TITLE_MATCH = re.compile(
    r"site reliability|\bsre\b|reliability engineer|dev\s?ops|devsecops|platform engineer|"
    r"cloud engineer|infrastructure engineer|cloud infrastructure|release engineer|"
    r"production engineer|kubernetes|observability|automation engineer",
    re.I,
)
# ...and must NOT match any of these (you are senior, hands-on IC)
TITLE_EXCLUDE = re.compile(
    r"\bintern\b|internship|graduate|trainee|junior|entry[- ]level|director|head of|"
    r"vice president|\bvp\b|engineering manager",
    re.I,
)

# Location must look like India
INDIA = re.compile(
    r"\bindia\b|pune|bengaluru|bangalore|hyderabad|mumbai|chennai|gurgaon|gurugram|"
    r"noida|delhi|kolkata|ahmedabad|kochi|coimbatore|thiruvananthapuram|indore",
    re.I,
)
AMBIGUOUS_LOC = re.compile(r"\d+\s+locations?|multiple|various", re.I)

# Skill groups taken from your resume; matched against the job description
SKILLS = {
    "Kubernetes/EKS": r"kubernetes|\bk8s\b|\beks\b",
    "AWS": r"\baws\b|amazon web services",
    "Terraform": r"terraform",
    "Docker/Containers": r"docker|container",
    "Ansible/Chef": r"ansible|\bchef\b",
    "CI/CD": r"ci/cd|jenkins|gitlab|github actions|pipeline",
    "GitOps/Helm": r"argocd|argo cd|gitops|\bhelm\b",
    "Monitoring": r"prometheus|grafana|\belk\b|splunk|datadog|dynatrace|appdynamics|observability|monitoring",
    "Python/Shell": r"python|shell|bash|scripting",
    "Linux": r"linux|unix",
    "SRE practices": r"\bslo\b|\bsli\b|error budget|incident|on-call|post-?mortem|\brca\b|reliability",
    "DR/Release": r"disaster recovery|release management",
}


# ------------------------------------------------------------- helpers
def http(method, url, **kw):
    time.sleep(0.5)  # be gentle with the sites
    r = requests.request(method, url, headers=HEADERS, timeout=30, **kw)
    r.raise_for_status()
    return r


def strip_html(s):
    return BeautifulSoup(html.unescape(s or ""), "html.parser").get_text(" ", strip=True)


def parse_date(s):
    if not s:
        return None
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def recent(dt):
    return (datetime.now(timezone.utc).date() - dt.date()).days <= MAX_AGE_DAYS


def clean(d):
    return {k: v for k, v in d.items() if v}


# ------------------------------------------------------------- sources
# Each source yields dicts: id, company, title, url, location, posted, text, fetch
# "fetch" (optional) is a function returning extra details (date/location/description).

# ---- Workday (Nvidia and many large companies) ----
def workday_age(s):
    s = (s or "").lower()
    if "today" in s:
        return 0
    if "yesterday" in s:
        return 1
    m = re.search(r"(\d+)(\+?)\s*days", s)
    if m:
        return int(m.group(1)) + (100 if m.group(2) else 0)
    return None


def workday_detail(api, path):
    i = http("GET", f"{api}{path}").json()["jobPostingInfo"]
    locs = [i.get("location")] + (i.get("additionalLocations") or [])
    locs.append((i.get("country") or {}).get("descriptor"))
    return clean({
        "location": " | ".join(x for x in locs if x),
        "posted": parse_date(i.get("startDate")),
        "text": strip_html(i.get("jobDescription")),
    })


def workday(name, url):
    u = urlparse(url)
    base = f"{u.scheme}://{u.netloc}"
    tenant = u.netloc.split(".")[0]
    parts = [p for p in u.path.split("/") if p and not re.fullmatch(r"[a-z]{2}-[A-Z]{2}", p)]
    site = parts[0] if parts else ""
    api = f"{base}/wday/cxs/{tenant}/{site}"
    seen_paths = set()
    for term in SEARCH_TERMS:
        for offset in (0, 20, 40):
            body = {"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": term}
            posts = http("POST", f"{api}/jobs", json=body).json().get("jobPostings", [])
            if not posts:
                break
            for j in posts:
                path = j.get("externalPath")
                if not path or path in seen_paths:
                    continue
                seen_paths.add(path)
                age = workday_age(j.get("postedOn"))
                if age is not None and age > MAX_AGE_DAYS:
                    continue  # too old; skip without extra requests
                posted = datetime.now(timezone.utc) - timedelta(days=age) if age is not None else None
                yield {
                    "id": f"{name}:{path}", "company": name, "title": j.get("title", ""),
                    "url": f"{base}/{site}{path}", "location": j.get("locationsText", ""),
                    "posted": posted,
                    "fetch": (lambda p=path: workday_detail(api, p)),
                }


# ---- Greenhouse ----
def greenhouse(name, url):
    token = urlparse(url).path.strip("/").split("/")[0]
    data = http("GET", f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true").json()
    for j in data.get("jobs", []):
        yield {
            "id": f"{name}:{j['id']}", "company": name, "title": j["title"],
            "url": j["absolute_url"], "location": (j.get("location") or {}).get("name", ""),
            "posted": parse_date(j.get("first_published") or j.get("updated_at")),
            "text": strip_html(j.get("content")),
        }


# ---- Lever ----
def lever(name, url):
    slug = urlparse(url).path.strip("/").split("/")[0]
    for j in http("GET", f"https://api.lever.co/v0/postings/{slug}?mode=json").json():
        cat = j.get("categories") or {}
        locs = [cat.get("location", "")] + (cat.get("allLocations") or [])
        created = j.get("createdAt")
        yield {
            "id": f"{name}:{j['id']}", "company": name, "title": j["text"],
            "url": j["hostedUrl"], "location": " | ".join(x for x in locs if x),
            "posted": datetime.fromtimestamp(created / 1000, timezone.utc) if created else None,
            "text": j.get("descriptionPlain", ""),
        }


# ---- Ashby ----
def ashby(name, url):
    slug = urlparse(url).path.strip("/").split("/")[0]
    data = http("GET", f"https://api.ashbyhq.com/posting-api/job-board/{slug}").json()
    for j in data.get("jobs", []):
        locs = [j.get("location", "")] + [s.get("location", "") for s in j.get("secondaryLocations") or []]
        yield {
            "id": f"{name}:{j['id']}", "company": name, "title": j["title"],
            "url": j["jobUrl"], "location": " | ".join(x for x in locs if x),
            "posted": parse_date(j.get("publishedAt")),
            "text": j.get("descriptionPlain", ""),
        }


# ---- Radancy / TalentBrew style sites (Citi, Barclays, many banks) ----
def jsonld_detail(href):
    soup = BeautifulSoup(http("GET", href).text, "html.parser")
    for s in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(s.string or "")
        except ValueError:
            continue
        for d in data if isinstance(data, list) else [data]:
            if not isinstance(d, dict) or d.get("@type") != "JobPosting":
                continue
            locs = d.get("jobLocation") or []
            locs = [locs] if isinstance(locs, dict) else locs
            parts = []
            for l in locs:
                a = l.get("address") or {}
                c = a.get("addressCountry")
                c = c.get("name") if isinstance(c, dict) else c
                parts.append(f"{a.get('addressLocality', '')} {a.get('addressRegion', '')} {c or ''}".strip())
            return clean({
                "location": " | ".join(p for p in parts if p),
                "posted": parse_date(d.get("datePosted")),
                "text": strip_html(d.get("description")),
            })
    return clean({"text": soup.get_text(" ", strip=True)[:20000]})


def radancy(name, url):
    u = urlparse(url)
    base = f"{u.scheme}://{u.netloc}"
    seen_urls = set()
    for term in SEARCH_TERMS:
        for page in (1, 2):
            r = http("GET", f"{base}/search-jobs", params={"k": term, "l": "India", "p": page})
            soup = BeautifulSoup(r.text, "html.parser")
            for a in soup.select('a[href*="/job/"]'):
                href = urljoin(base, a["href"]).split("?")[0]
                if href in seen_urls:
                    continue
                seen_urls.add(href)
                h = a.find(["h2", "h3"])
                loc = a.select_one(".job-location")
                yield {
                    "id": f"{name}:{href}", "company": name,
                    "title": (h or a).get_text(" ", strip=True), "url": href,
                    "location": loc.get_text(" ", strip=True) if loc else "",
                    "fetch": (lambda h=href: jsonld_detail(h)),
                }


def pick_source(url):
    host = urlparse(url).netloc.lower()
    if "myworkdayjobs.com" in host:
        return workday
    if "greenhouse.io" in host:
        return greenhouse
    if "lever.co" in host:
        return lever
    if "ashbyhq.com" in host:
        return ashby
    return radancy


# ------------------------------------------------------------ filtering
DEBUG = "--debug" in sys.argv


def note(job, msg):
    if DEBUG:
        print(f"   - [{job['company']}] {job['title']} | {msg}")


def fmt_date(dt):
    return dt.strftime("%d %b %Y") if dt else "date unknown"


def evaluate(job, seen, first_run):
    """Return job if it should trigger an alert, else None. Adds decided jobs to `seen`."""
    title = job["title"]
    if not title or not TITLE_MATCH.search(title):
        return None
    if TITLE_EXCLUDE.search(title):
        note(job, "dropped: title excluded (intern/junior/manager etc.)")
        return None
    if job["id"] in seen:
        note(job, "dropped: already seen")
        return None

    if job.get("posted") and not recent(job["posted"]):
        seen.add(job["id"])
        note(job, f"dropped: too old (posted {fmt_date(job['posted'])})")
        return None

    loc = job.get("location", "")
    listing_loc_ok = bool(loc and INDIA.search(loc))
    if loc and not AMBIGUOUS_LOC.search(loc) and not listing_loc_ok:
        seen.add(job["id"])
        note(job, f"dropped: location not India ({loc})")
        return None

    if job.get("fetch"):  # may raise -> job is retried on the next run
        job.update(job["fetch"]())
        if job.get("posted") and not recent(job["posted"]):
            seen.add(job["id"])
            note(job, f"dropped: too old (posted {fmt_date(job['posted'])})")
            return None
        loc = job.get("location", "")
        if loc and not listing_loc_ok and not INDIA.search(loc):
            seen.add(job["id"])
            note(job, f"dropped: location not India ({loc})")
            return None

    text = job.get("text", "")
    matched = [k for k, p in SKILLS.items() if re.search(p, text, re.I)]
    if text and len(matched) < MIN_SKILLS:
        seen.add(job["id"])
        note(job, f"dropped: only {len(matched)} skill groups matched {matched} (need {MIN_SKILLS})")
        return None

    job["matched"] = matched
    seen.add(job["id"])
    if not job.get("posted") and first_run:
        note(job, "dropped: posting date unknown on first run")
        return None
    note(job, f"ACCEPTED (posted {fmt_date(job.get('posted'))}, {len(matched)} skill groups)")
    return job


# ----------------------------------------------------------- email/main
def send_mail(subject, body):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.environ["MAIL_USER"]
    msg["To"] = os.environ["MAIL_TO"]
    msg.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(os.environ["MAIL_USER"], os.environ["MAIL_APP_PASSWORD"])
        s.send_message(msg)


def format_job(j):
    posted = j["posted"].strftime("%d %b %Y") if j.get("posted") else "date not shown"
    skills = ", ".join(j.get("matched", [])) or "n/a"
    return (f"[{j['company']}] {j['title']}\n"
            f"Location: {j.get('location') or 'n/a'}\n"
            f"Posted: {posted}\n"
            f"Resume match ({len(j.get('matched', []))}/{len(SKILLS)}): {skills}\n"
            f"{j['url']}")


def load_companies():
    out = []
    for line in open(COMPANIES_FILE, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "|" not in line:
            continue
        n, u = [x.strip() for x in line.split("|", 1)]
        out.append((n, u))
    return out


def load_seen():
    if not os.path.exists(SEEN_FILE):
        return set()
    data = json.load(open(SEEN_FILE, encoding="utf-8"))
    return set(data) if isinstance(data, list) else set()


def main():
    if "--test" in sys.argv:
        send_mail("Job watch test", "Email works.")
        print("Test email sent.")
        return

    dry = "--dry-run" in sys.argv
    seen = load_seen()
    first_run = not seen and not dry  # dry runs show everything
    matches = []

    for name, url in load_companies():
        try:
            jobs = list(pick_source(url)(name, url))
        except Exception as e:
            print(f"[{name}] FAILED to fetch listings: {e}")
            continue
        print(f"[{name}] {len(jobs)} listings fetched")
        for job in jobs:
            try:
                hit = evaluate(job, seen, first_run)
            except Exception as e:
                print(f"[{name}] could not check '{job['title']}': {e}")
                continue
            if hit:
                matches.append(hit)

    matches.sort(key=lambda j: len(j.get("matched", [])), reverse=True)
    print(f"New matching jobs: {len(matches)}")

    if dry:
        for j in matches:
            print("\n" + format_job(j))
        print("\n(dry run: nothing emailed, nothing saved)")
        return

    if matches:
        send_mail(f"{len(matches)} new SRE/DevOps job(s) in India",
                  "\n\n".join(format_job(j) for j in matches))
        print("Email sent.")
    json.dump(sorted(seen), open(SEEN_FILE, "w", encoding="utf-8"))


if __name__ == "__main__":
    main()