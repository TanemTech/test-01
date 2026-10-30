"""Provision TANEM client repositories from vetted GitHub issue requests.

Runs only in the owner-controlled GitHub Actions workflow. No user-provided
issue content is ever evaluated as shell, Python or an arbitrary API URL.
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


TITLE = "TANEM: CREATE REPOSITORY"
SLUG_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
PREFIX_RE = re.compile(r"[A-Za-z][A-Za-z0-9-]{0,24}\Z")
API = "https://api.github.com"
OWNER = "TanemTech"  # Owner is a personal GitHub account, not an organization.


class InvalidRequest(ValueError):
    pass


class GithubFailure(RuntimeError):
    pass


def validate_yandex_url(raw):
    if not isinstance(raw, str) or len(raw) > 300:
        raise InvalidRequest("Provide a Yandex Maps link no longer than 300 characters.")
    parsed = urlsplit(raw)
    host = (parsed.hostname or "").lower()
    allowed = {"yandex.ru", "yandex.com", "yandex.by", "yandex.kz", "yandex.com.tr"}
    try:
        port = parsed.port
    except ValueError:
        raise InvalidRequest("Invalid port in the Yandex Maps link.") from None
    if (parsed.scheme != "https" or parsed.username or parsed.password or
            port not in (None, 443) or host not in allowed or
            not (parsed.path == "/maps" or parsed.path.startswith("/maps/"))):
        raise InvalidRequest("A direct https://yandex.../maps link is required.")
    if any(ord(c) < 32 for c in raw):
        raise InvalidRequest("Invalid characters in the Yandex Maps link.")
    return raw


def parse_request(issue, config):
    if issue.get("title") != TITLE:
        raise InvalidRequest("Not a TANEM repository request.")
    # All ChatGPT specialist projects use the same connected TanemTech account.
    # The project label helps sort repositories; it is not identity verification.
    author = issue.get("user", {}).get("login", "")
    if not isinstance(author, str) or author.lower() != OWNER.lower():
        raise InvalidRequest("Only the connected TanemTech GitHub account can request creation.")
    try:
        details = json.loads(issue.get("body") or "")
    except (ValueError, TypeError):
        raise InvalidRequest("The request body must be a JSON object.") from None
    if not isinstance(details, dict) or set(details) != {"project", "client_slug", "yandex_url"}:
        raise InvalidRequest("Use exactly project, client_slug and yandex_url in the JSON body.")
    project = details["project"]
    projects = config.get("projects", {})
    if not isinstance(projects, dict) or not isinstance(project, str) or project not in projects:
        raise InvalidRequest("Unknown TANEM ChatGPT project. Use an approved project key.")
    prefix = projects[project].get("prefix", "") if isinstance(projects[project], dict) else ""
    if not isinstance(prefix, str) or not PREFIX_RE.fullmatch(prefix) or prefix.endswith("-"):
        raise InvalidRequest("Invalid configured project prefix.")
    slug = details["client_slug"]
    if not isinstance(slug, str) or len(slug) > 55 or not SLUG_RE.fullmatch(slug):
        raise InvalidRequest("client_slug must be lowercase Latin letters, numbers and hyphens.")
    url = validate_yandex_url(details["yandex_url"])
    repository = f"{prefix}-{slug}"
    if len(repository) > 85:
        raise InvalidRequest("Repository name is too long.")
    return {"name": repository, "url": url, "project": project}


def github_api(method, path, token, payload=None):
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "tanem-repository-creator",
    }
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = Request(API + path, headers=headers, method=method, data=body)
    try:
        with urlopen(req, timeout=25) as response:
            return json.load(response) if response.status != 204 else {}
    except HTTPError as exc:
        # Never echo response bodies: an upstream error could contain sensitive data.
        raise GithubFailure(f"GitHub API failed ({method} {path.split('?')[0]}): HTTP {exc.code}") from None
    except URLError as exc:
        raise GithubFailure("Unable to reach the GitHub API.") from exc


def issue_comment(repo, issue_number, token, message):
    return github_api("POST", f"/repos/{repo}/issues/{issue_number}/comments", token, {"body": message})


def create_repository(details, token):
    # No overwrite or automatic rename; an existing name must be reviewed.
    name = details["name"]
    result = github_api("POST", "/user/repos", token, {
        "name": name,
        "description": "Яндекс.Карты: " + details["url"],
        "private": False,
        "auto_init": True,
        "has_issues": True,
    })
    if result.get("full_name", "").lower() != f"{OWNER}/{name}".lower():
        raise GithubFailure("Repository owner/name did not match the expected account.")
    return result["html_url"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--event", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()

    event = json.loads(args.event.read_text(encoding="utf-8"))
    issue = event.get("issue", {})
    if issue.get("title") != TITLE or event.get("action") != "opened":
        return 0

    if event.get("repository", {}).get("full_name", "").lower() != f"{OWNER}/test-01".lower():
        raise SystemExit("Unexpected request repository")
    number = issue.get("number")
    if not isinstance(number, int) or number < 1:
        raise SystemExit("Invalid issue number")
    issue_token = os.environ.get("GITHUB_TOKEN", "")
    if not issue_token:
        raise SystemExit("GITHUB_TOKEN unavailable")

    config = json.loads(args.config.read_text(encoding="utf-8"))
    try:
        details = parse_request(issue, config)
    except InvalidRequest as exc:
        issue_comment("TanemTech/test-01", number, issue_token, "⚠️ Заявка отклонена: " + str(exc))
        return 0

    creator_token = os.environ.get("TANEM_REPO_CREATOR_TOKEN", "")
    if not creator_token:
        issue_comment("TanemTech/test-01", number, issue_token,
                      "⏸️ Создание не настроено: отсутствует отдельный токен владельца. Репозиторий не создан.")
        return 1

    try:
        url = create_repository(details, creator_token)
    except GithubFailure as exc:
        issue_comment("TanemTech/test-01", number, issue_token, "❌ Репозиторий не создан: " + str(exc))
        return 1

    issue_comment("TanemTech/test-01", number, issue_token,
                  f"✅ Публичный репозиторий создан: {url}\n\n"
                  "Откройте ссылку и загрузите материалы клиента. "
                  "Публикация Pages и Selectel не запускалась.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
