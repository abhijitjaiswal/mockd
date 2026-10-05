#!/usr/bin/env python3
"""
trackers.py — send a bug report to where the team already tracks work.

A bug report that has to be copied into another window is a bug report that
sometimes is not sent. This posts it — to Jira, GitHub or GitLab issues, a
Slack or Teams channel, or any webhook — and hands back the link, which is
then kept on the test so the next person can see it was already raised.

Nothing is configured by picking from a list. Paste the address of the project
or channel and what it is is worked out from the address; only what that kind
of place needs is then asked for. The shape of the connection is kept in
connections.json; the token is kept in .env, as every other secret here is.
"""
import base64
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
FILE = HERE / "connections.json"

KINDS = {
    "jira": "Jira",
    "github": "GitHub issues",
    "gitlab": "GitLab issues",
    "slack": "a Slack channel",
    "teams": "a Teams channel",
    "webhook": "a webhook",
}
# what each kind still needs from a person, beyond the address
NEEDS = {
    "jira": [("email", "Your Atlassian email", False), ("token", "API token", True)],
    "github": [("token", "Access token (with permission to create issues)", True)],
    "gitlab": [("token", "Access token (api scope)", True)],
    "slack": [], "teams": [], "webhook": [],
}
MAKES_A_TICKET = ("jira", "github", "gitlab")


def needs_for(found):
    """What this particular place needs. A Jira you host yourself signs in with
    a personal access token alone; only Jira Cloud wants the email as well."""
    if found.get("kind") == "jira" and not found.get("cloud"):
        return [("token", "Personal access token", True)]
    return NEEDS.get(found.get("kind"), [])


class Problem(Exception):
    """Something a person can act on."""


# ----------------------------------------------------------------- detection
def detect(address):
    """What a pasted address is, and what can be read out of it."""
    address = (address or "").strip()
    if not re.match(r"^https?://", address, re.I):
        raise Problem("Paste the full address, starting with https://")
    url = urllib.parse.urlparse(address)
    host, path = url.netloc.lower(), url.path
    site = f"{url.scheme}://{url.netloc}"

    if host == "hooks.slack.com":
        return {"kind": "slack", "name": "Slack", "url": address, "sure": True}
    if "webhook.office.com" in host or "logic.azure.com" in host:
        return {"kind": "teams", "name": "Teams", "url": address, "sure": True}

    key = (re.search(r"/browse/([A-Z][A-Z0-9_]+)-\d+", path)
           or re.search(r"/projects/([A-Z][A-Z0-9_]+)", path)
           or re.search(r"[?&]projectKey=([A-Z][A-Z0-9_]+)", address)
           or re.search(r"/browse/([A-Z][A-Z0-9_]+)/?$", path))
    if host.endswith(".atlassian.net") or key or "/jira" in path.lower():
        jira_site = site + ("/jira" if "/jira/" in path.lower() and not host.endswith(".atlassian.net")
                            else "")
        return {"kind": "jira", "site": site if host.endswith(".atlassian.net") else jira_site,
                "project": key.group(1) if key else "",
                "cloud": host.endswith(".atlassian.net"),
                "name": f"Jira{' ' + key.group(1) if key else ''}",
                "sure": host.endswith(".atlassian.net") or bool(key)}

    parts = [p for p in path.split("/") if p]
    if host in ("github.com", "www.github.com") and len(parts) >= 2:
        return {"kind": "github", "api": "https://api.github.com", "repo": f"{parts[0]}/{parts[1]}",
                "name": f"GitHub {parts[0]}/{parts[1]}", "sure": True}
    if host in ("gitlab.com", "www.gitlab.com") and len(parts) >= 2:
        project = "/".join(parts).split("/-/")[0]
        return {"kind": "gitlab", "api": "https://gitlab.com/api/v4", "project": project,
                "name": f"GitLab {project}", "sure": True}
    if "gitlab" in host and len(parts) >= 2:
        project = "/".join(parts).split("/-/")[0]
        return {"kind": "gitlab", "api": f"{site}/api/v4", "project": project,
                "name": f"GitLab {project}", "sure": False}
    if "github" in host and len(parts) >= 2:
        return {"kind": "github", "api": f"{site}/api/v3", "repo": f"{parts[0]}/{parts[1]}",
                "name": f"GitHub {parts[0]}/{parts[1]}", "sure": False}
    return {"kind": "webhook", "name": url.netloc, "url": address, "sure": False}


def as_kind(address, kind):
    """The same address read as a kind the person chose — for a self-hosted
    Jira, GitLab or GitHub whose address gives nothing away."""
    found = detect(address)
    if kind == found["kind"] or kind not in KINDS:
        return found
    url = urllib.parse.urlparse(address.strip())
    site, parts = f"{url.scheme}://{url.netloc}", [p for p in url.path.split("/") if p]
    if kind == "jira":
        key = re.search(r"([A-Z][A-Z0-9_]+)(?:-\d+)?/?$", url.path)
        return {"kind": "jira", "site": site, "project": key.group(1) if key else "",
                "cloud": False, "name": f"Jira{' ' + key.group(1) if key else ''}", "sure": True}
    if kind == "github":
        if len(parts) < 2:
            raise Problem("A GitHub address needs the owner and the repository, like …/owner/repo")
        return {"kind": "github", "api": f"{site}/api/v3", "repo": f"{parts[0]}/{parts[1]}",
                "name": f"GitHub {parts[0]}/{parts[1]}", "sure": True}
    if kind == "gitlab":
        if len(parts) < 2:
            raise Problem("A GitLab address needs the group and the project, like …/group/project")
        project = "/".join(parts).split("/-/")[0]
        return {"kind": "gitlab", "api": f"{site}/api/v4", "project": project,
                "name": f"GitLab {project}", "sure": True}
    return {"kind": kind, "name": KINDS[kind].replace("a ", "").title() if kind != "webhook"
            else url.netloc, "url": address.strip(), "sure": True}


# -------------------------------------------------------------------- storage
def load():
    try:
        found = json.loads(FILE.read_text()).get("connections") or []
        return [c for c in found if isinstance(c, dict)]
    except (OSError, ValueError, AttributeError):
        return []


def current():
    found = load()
    return found[0] if found else None


def describe(connection):
    """A connection as the page may see it: never a credential."""
    if not connection:
        return None
    return {"kind": connection.get("kind"), "name": connection.get("name"),
            "what": KINDS.get(connection.get("kind"), "somewhere"),
            "makes_a_ticket": connection.get("kind") in MAKES_A_TICKET}


def save(found, secrets):
    """Keep the shape here and the secrets in .env. `secrets` is {field: value}."""
    import environments as envmod
    connection = {k: v for k, v in found.items() if k != "sure"}
    values = {}
    for field, value in (secrets or {}).items():
        if value in (None, ""):
            continue
        var = f"TRACKER_{field.upper()}"
        connection[field] = "${%s}" % var
        values[var] = str(value)
    if connection.get("kind") in ("slack", "teams", "webhook") and connection.get("url"):
        values["TRACKER_URL"] = connection["url"]            # the address is the secret
        connection["url"] = "${TRACKER_URL}"
    FILE.write_text(json.dumps({"connections": [connection]}, indent=2) + "\n")
    if values:
        envmod.write_dotenv(values)
    return connection


def forget():
    try:
        FILE.unlink()
    except OSError:
        pass


# -------------------------------------------------------------------- sending
def _call(url, payload, headers):
    data = json.dumps(payload).encode()
    request = urllib.request.Request(url, data=data, method="POST", headers={
        "Content-Type": "application/json", "Accept": "application/json",
        "User-Agent": "mockd", **headers})
    try:
        with urllib.request.urlopen(request, timeout=30) as resp:
            raw = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw, status = exc.read().decode("utf-8", "replace"), exc.code
    except Exception as exc:
        raise Problem(f"It could not be reached: {exc}")
    try:
        body = json.loads(raw) if raw.strip()[:1] in "{[" else {}
    except ValueError:
        body = {}
    if status in (401, 403):
        raise Problem(f"It refused the credential ({status}). Check the token, and that it may "
                      f"create issues there.")
    if status == 404:
        raise Problem("It says that project or address does not exist (404).")
    if status >= 400:
        said = (body.get("message") or body.get("error") or json.dumps(body.get("errors") or "")
                or raw)[:200] if isinstance(body, dict) else raw[:200]
        raise Problem(f"It answered {status}: {said}")
    return body if isinstance(body, dict) else {}


def _adf(markdown):
    """Markdown as the document format Jira Cloud wants — paragraphs and code
    blocks, which is all a bug report is."""
    content, lines, i = [], markdown.split("\n"), 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("```"):
            block = []
            i += 1
            while i < len(lines) and not lines[i].startswith("```"):
                block.append(lines[i])
                i += 1
            content.append({"type": "codeBlock", "content": [{"type": "text", "text": "\n".join(block) or " "}]})
        elif line.startswith("## "):
            content.append({"type": "heading", "attrs": {"level": 3},
                            "content": [{"type": "text", "text": line[3:].strip() or " "}]})
        elif line.strip():
            text = re.sub(r"\*\*(.+?)\*\*", r"\1", line)
            content.append({"type": "paragraph", "content": [{"type": "text", "text": text}]})
        i += 1
    return {"type": "doc", "version": 1, "content": content or [
        {"type": "paragraph", "content": [{"type": "text", "text": " "}]}]}


def send(connection, title, markdown, extra=None):
    """Post one bug report. Returns {"url": ..., "key": ...}; either may be None
    for a chat channel, which has nothing to link to."""
    import environments as envmod
    missing = []
    c = envmod.resolve(connection, missing)
    if missing:
        raise Problem("Its credential is no longer set (" + ", ".join(sorted(set(missing))) +
                      "). Connect it again.")
    kind = c.get("kind")
    title = title[:240]

    if kind == "jira":
        if not c.get("project"):
            raise Problem("Which Jira project? Paste an address that has the project in it, "
                          "like …/browse/ABC-1 or …/projects/ABC.")
        fields = {"project": {"key": c["project"]}, "summary": title,
                  "issuetype": {"name": c.get("issue_type") or "Bug"}}
        if c.get("cloud"):
            token = base64.b64encode(f"{c.get('email', '')}:{c.get('token', '')}".encode()).decode()
            fields["description"] = _adf(markdown)
            body = _call(f"{c['site']}/rest/api/3/issue", {"fields": fields},
                         {"Authorization": f"Basic {token}"})
        else:
            fields["description"] = markdown
            auth = ({"Authorization": f"Bearer {c['token']}"} if not c.get("email") else
                    {"Authorization": "Basic " + base64.b64encode(
                        f"{c['email']}:{c.get('token', '')}".encode()).decode()})
            body = _call(f"{c['site']}/rest/api/2/issue", {"fields": fields}, auth)
        key = body.get("key")
        return {"key": key, "url": f"{c['site']}/browse/{key}" if key else None}

    if kind == "github":
        body = _call(f"{c['api']}/repos/{c['repo']}/issues", {"title": title, "body": markdown},
                     {"Authorization": f"Bearer {c.get('token', '')}",
                      "X-GitHub-Api-Version": "2022-11-28"})
        number = body.get("number")
        return {"key": f"#{number}" if number else None, "url": body.get("html_url")}

    if kind == "gitlab":
        project = urllib.parse.quote(c["project"], safe="")
        body = _call(f"{c['api']}/projects/{project}/issues",
                     {"title": title, "description": markdown},
                     {"PRIVATE-TOKEN": c.get("token", "")})
        number = body.get("iid")
        return {"key": f"#{number}" if number else None, "url": body.get("web_url")}

    if kind in ("slack", "teams"):
        text = f"*{title}*\n{markdown}" if kind == "slack" else f"**{title}**\n\n{markdown}"
        _call(c["url"], {"text": text[:3500]}, {})
        return {"key": None, "url": None}

    body = _call(c["url"], {"title": title, "markdown": markdown, **(extra or {})}, {})
    return {"key": body.get("key") or body.get("id"), "url": body.get("url")}
