"""
Job watcher: SRE / DevOps roles in India, posted in the last few days,
matched against Harshal's resume skills. Emails new matches.

Usage:
    python job_watch.py --test      # send a test email
    python job_watch.py --dry-run   # show matches, no email, nothing saved
    python job_watch.py --debug     # (combine with the above) explain every drop
    python job_watch.py             # normal run (schedule this)

companies.txt line format:
    Name | URL                      (platform guessed from the URL)
    Name | type | URL               (type forces the platform)
Types: workday, greenhouse, lever, ashby, radancy, oracle, apple, atlassian

Health check: if a company fails FAIL_ALERT_AFTER runs in a row you get one
email; you get another when it recovers. State lives in health.json (keep it
persisted the same way as seen.json).
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
HEALTH_FILE = os.path.join(HERE, "health.json")
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
MAX_AGE_DAYS = 4          # only jobs posted within this many days
MIN_SKILLS = 3            # job description must match at least this many skill groups
FAIL_ALERT_AFTER = 6      # consecutive failed runs before a "company is failing" email
HEADERS = {"User-Agent": "Mozilla/5.0 (personal job-watch script)"}

# Search words sent to each career site
SEARCH_TERMS = ["site reliability", "sre", "devops", "kubernetes", "platform engineer",
                "infrastructure", "application support", "technology analyst"]

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
    headers = {**HEADERS, **kw.pop("headers", {})}
    r = requests.request(method, url, headers=headers, timeout=30, **kw)
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


def body_of(e):
    """Short error body from a failed HTTP call (helps diagnose 4xx errors)."""
    try:
        return e.response.text[:200].replace("\n", " ")
    except Exception:
        return ""


# ------------------------------------------------------------- sources
# Each source yields dicts: id, company, title, url, location, posted, text, fetch
# "fetch" (optional) is a function returning extra details (date/location/description).
# A source RAISES if it could not get any data at all (that feeds the health check).

# ---- Workday (Nvidia, Walmart, Akamai and many large companies) ----
WORKDAY_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Accept-Language": "en-US",
}


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
    i = http("GET", f"{api}{path}", headers={"Accept": "application/json"}).json()["jobPostingInfo"]
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
    ok, last_err = 0, None
    for term in SEARCH_TERMS:
        for offset in (0, 20, 40):
            body = {"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": term}
            try:
                posts = http("POST", f"{api}/jobs", json=body,
                             headers=WORKDAY_HEADERS).json().get("jobPostings", [])
            except requests.HTTPError as e:
                last_err = e
                print(f"[{name}] '{term}' offset {offset} skipped: {e} | {body_of(e)}")
                break
            ok += 1
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
    if ok == 0 and last_err:  # every request failed -> report as a failure, not "0 listings"
        raise last_err


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


# ---- Oracle HCM "Candidate Experience" sites (JPMorgan and many others) ----
# Untested against the live site: uses Oracle's public recruiting REST API.
def oracle_detail(api, site, rid):
    try:
        params = {"onlyData": "true", "expand": "all",
                  "finder": f'ById;Id="{rid}",siteNumber={site}'}
        data = http("GET", f"{api}/recruitingCEJobRequisitionDetails", params=params,
                    headers={"Accept": "application/json"}).json()
        it = (data.get("items") or [{}])[0]
        text = " ".join(strip_html(it.get(k)) for k in
                        ("ExternalDescriptionStr", "ExternalResponsibilitiesStr",
                         "ExternalQualificationsStr") if it.get(k))
        return clean({"text": text})
    except Exception:
        return {}  # description unavailable -> skill filter is skipped for this job


def oracle(name, url):
    u = urlparse(url)
    base = f"{u.scheme}://{u.netloc}"
    m = re.search(r"/sites/([^/]+)", u.path)
    site = m.group(1) if m else "CX_1"
    api = f"{base}/hcmRestApi/resources/latest"
    seen_ids = set()
    ok, last_err = 0, None
    for term in SEARCH_TERMS:
        kw = f'"{term}"' if " " in term else term
        for off in (0, 25, 50):
            params = {
                "onlyData": "true",
                "expand": "requisitionList.secondaryLocations",
                "finder": f"findReqs;siteNumber={site},keyword={kw},limit=25,offset={off},"
                          f"sortBy=POSTING_DATES_DESC",
            }
            try:
                data = http("GET", f"{api}/recruitingCEJobRequisitions", params=params,
                            headers={"Accept": "application/json"}).json()
            except requests.HTTPError as e:
                last_err = e
                print(f"[{name}] '{term}' offset {off} skipped: {e} | {body_of(e)}")
                break
            ok += 1
            items = data.get("items") or []
            reqs = (items[0].get("requisitionList") if items else None) or []
            if not reqs:
                break
            for j in reqs:
                rid = j.get("Id")
                if not rid or rid in seen_ids:
                    continue
                seen_ids.add(rid)
                locs = [j.get("PrimaryLocation", "")] + [
                    s.get("Name", "") for s in (j.get("secondaryLocations") or []) if isinstance(s, dict)]
                yield {
                    "id": f"{name}:{rid}", "company": name, "title": j.get("Title", ""),
                    "url": f"{base}/hcmUI/CandidateExperience/en/sites/{site}/job/{rid}",
                    "location": " | ".join(x for x in locs if x),
                    "posted": parse_date(j.get("PostedDate")),
                    "fetch": (lambda r=rid: oracle_detail(api, site, r)),
                }
    if ok == 0 and last_err:
        raise last_err


# ---- Apple (jobs.apple.com internal search API; untested, may need tweaks) ----
def apple(name, url):
    s = requests.Session()
    s.headers.update(HEADERS)
    r = s.get("https://jobs.apple.com/api/csrfToken", timeout=30)
    r.raise_for_status()
    csrf = r.headers.get("x-apple-csrf-token", "")
    hdrs = {
        "x-apple-csrf-token": csrf,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Referer": "https://jobs.apple.com/en-in/search",
        "Origin": "https://jobs.apple.com",
    }
    seen_ids = set()
    for term in SEARCH_TERMS:
        for page in (1, 2):
            time.sleep(0.5)
            body = {
                "query": term,
                "filters": {"locations": ["postLocation-INDC"]},  # India
                "page": page,
                "locale": "en-in",
                "sort": "newest",
            }
            r = s.post("https://jobs.apple.com/api/v1/search", json=body, headers=hdrs, timeout=30)
            r.raise_for_status()
            data = r.json()
            data = data.get("res", data) if isinstance(data, dict) else {}
            results = data.get("searchResults") or []
            if not results:
                break
            for j in results:
                pid = j.get("positionId") or j.get("id")
                if not pid or pid in seen_ids:
                    continue
                seen_ids.add(pid)
                locs = [l.get("name", "") for l in (j.get("locations") or []) if isinstance(l, dict)]
                slug = j.get("transformedPostingTitle", "")
                yield {
                    "id": f"{name}:{pid}", "company": name, "title": j.get("postingTitle", ""),
                    "url": f"https://jobs.apple.com/en-in/details/{pid}/{slug}".rstrip("/"),
                    "location": " | ".join(x for x in locs if x) or "India",
                    "posted": parse_date(j.get("postDateInGMT") or j.get("postingDate")),
                    "text": "",  # search results only have a short summary; skill filter is skipped
                }


# ---- Atlassian (careers site JSON listing; untested, written defensively) ----
def atlassian(name, url):
    data = http("GET", "https://www.atlassian.com/endpoint/careers/listings",
                headers={"Accept": "application/json"}).json()
    if isinstance(data, dict):
        data = data.get("listings") or data.get("jobs") or data.get("data") or []
    for j in data:
        if not isinstance(j, dict):
            continue
        post = j.get("portalJobPost") or {}
        jid = j.get("id") or post.get("id")
        if not jid:
            continue
        locs = j.get("locations") or j.get("location") or []
        if isinstance(locs, str):
            locs = [locs]
        locs = [l if isinstance(l, str) else (l.get("name") or "") for l in locs]
        text = " ".join(strip_html(j.get(k)) for k in ("overview", "responsibilities", "qualifications")
                        if j.get(k))
        yield {
            "id": f"{name}:{jid}", "company": name, "title": j.get("title", ""),
            "url": j.get("applyUrl") or j.get("portalUrl") or
                   f"https://www.atlassian.com/company/careers/details/{jid}",
            "location": " | ".join(x for x in locs if x),
            "posted": None,  # listing has no reliable posting date
            "text": text,
        }


SOURCES = {
    "workday": workday, "greenhouse": greenhouse, "lever": lever, "ashby": ashby,
    "radancy": radancy, "oracle": oracle, "apple": apple, "atlassian": atlassian,
}


def pick_source(url):
    """Guess the platform from the URL when companies.txt gives no type."""
    host = urlparse(url).netloc.lower()
    if "myworkdayjobs.com" in host:
        return workday
    if "greenhouse.io" in host:
        return greenhouse
    if "lever.co" in host:
        return lever
    if "ashbyhq.com" in host:
        return ashby
    if "oraclecloud.com" in host:
        return oracle
    if "jobs.apple.com" in host:
        return apple
    return radancy


def get_source(typ, url):
    if typ:
        if typ not in SOURCES:
            raise ValueError(f"unknown type '{typ}' (use one of: {', '.join(SOURCES)})")
        return SOURCES[typ]
    return pick_source(url)


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
    """Lines:  Name | URL   or   Name | type | URL   ->  (name, type or None, url)"""
    out = []
    for line in open(COMPANIES_FILE, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "|" not in line:
            continue
        parts = [x.strip() for x in line.split("|")]
        name, url = parts[0], parts[-1]
        typ = parts[1].lower() if len(parts) >= 3 else None
        out.append((name, typ, url))
    return out


def load_seen():
    if not os.path.exists(SEEN_FILE):
        return set()
    data = json.load(open(SEEN_FILE, encoding="utf-8"))
    return set(data) if isinstance(data, list) else set()


def load_health():
    if not os.path.exists(HEALTH_FILE):
        return {}
    try:
        data = json.load(open(HEALTH_FILE, encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def main():
    if "--test" in sys.argv:
        send_mail("Job watch test", "Email works.")
        print("Test email sent.")
        return

    dry = "--dry-run" in sys.argv
    seen = load_seen()
    health = load_health()
    first_run = not seen and not dry  # dry runs show everything
    matches = []
    newly_failing, recovered = [], []

    for name, typ, url in load_companies():
        try:
            jobs = list(get_source(typ, url)(name, url))
        except Exception as e:
            print(f"[{name}] FAILED to fetch listings: {e} {body_of(e)}")
            health[name] = health.get(name, 0) + 1
            if health[name] == FAIL_ALERT_AFTER:
                newly_failing.append((name, str(e)))
            continue
        if health.get(name, 0) >= FAIL_ALERT_AFTER:
            recovered.append(name)
        health[name] = 0
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
        failing_now = {n: c for n, c in health.items() if c}
        if failing_now:
            print(f"\nConsecutive failures so far: {failing_now}")
        print("\n(dry run: nothing emailed, nothing saved)")
        return

    if matches:
        send_mail(f"{len(matches)} new SRE/DevOps job(s) in India",
                  "\n\n".join(format_job(j) for j in matches))
        print("Email sent.")
    json.dump(sorted(seen), open(SEEN_FILE, "w", encoding="utf-8"))
    json.dump(health, open(HEALTH_FILE, "w", encoding="utf-8"), indent=2)

    # Health emails (never let a mail problem lose the saved state above)
    try:
        if newly_failing:
            send_mail(f"Job watch: {len(newly_failing)} company source(s) failing",
                      f"These have failed {FAIL_ALERT_AFTER} runs in a row, so you may be missing jobs:\n\n"
                      + "\n".join(f"- {n}: {err}" for n, err in newly_failing)
                      + "\n\nCheck the URL in companies.txt or whether the site changed.")
        if recovered:
            send_mail("Job watch: source(s) recovered",
                      "Working again: " + ", ".join(recovered))
    except Exception as e:
        print(f"Could not send health email: {e}")


if __name__ == "__main__":
    main()
