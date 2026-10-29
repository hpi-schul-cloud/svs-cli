"""API-only command line tools for SVS."""

from __future__ import annotations

import argparse
import base64
from email.utils import parsedate_to_datetime
from getpass import getpass
from html.parser import HTMLParser
import json
import math
import mimetypes
import os
import re
import sys
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen


def api_url(base_url: str, path: str, params: dict[str, str]) -> str:
    """Build a v3 SchoolCloud API URL from the configured backend URL."""
    base = base_url.rstrip("/")
    parts = urlsplit(base)
    # BACKEND_URL conventionally ends in /api; append the API version to it.
    if parts.path.rstrip("/").endswith("/api"):
        path_prefix = parts.path.rstrip("/") + "/v3"
    else:
        path_prefix = parts.path.rstrip("/")
    query = urlencode(params)
    return urlunsplit((parts.scheme, parts.netloc, path_prefix + path, query, ""))


def write_progress(message: str, *, end: str = "") -> None:
    """Replace the current terminal line instead of leaving stale characters behind."""
    print(f"\r\033[2K{message}", end=end, file=sys.stderr, flush=True)


def list_schools(
    base_url: str,
    token: str,
    show_progress: bool = False,
    page_size: int = 100,
) -> list[dict]:
    """Fetch all schools from the authenticated v3 school-list endpoint."""
    bearer_token = token.removeprefix("Bearer ").strip()
    schools: list[dict] = []
    skip = 0
    total: int | None = None
    page_number = 0
    while total is None or skip < total:
        page_number += 1
        if show_progress:
            page_count = str(math.ceil(total / page_size)) if total is not None else "?"
            loaded = f" ({len(schools):,} of {total:,} loaded)" if total is not None else ""
            write_progress(f"Fetching schools: page {page_number} of {page_count}{loaded}...")
        url = api_url(base_url, "/school", {"limit": str(page_size), "skip": str(skip)})
        request = Request(url, headers={"Authorization": f"Bearer {bearer_token}", "Accept": "application/json"})
        with urlopen(request, timeout=30) as response:
            payload = json.load(response)
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise ValueError("Unexpected API response: expected an object containing a 'data' list.")
        page = payload["data"]
        schools.extend(page)
        total = payload.get("total", len(page))
        if not page:
            break
        skip += len(page)
    if show_progress:
        write_progress(f"Fetched {len(schools):,} schools across {page_number} page(s).", end="\n")
    return schools


def list_rooms(base_url: str, token: str) -> list[dict]:
    """List rooms available in the authenticated user's room arrangement."""
    bearer_token = token.removeprefix("Bearer ").strip()
    url = api_url(base_url, "/rooms", {})
    request = Request(url, headers={"Authorization": f"Bearer {bearer_token}", "Accept": "application/json"})
    with urlopen(request, timeout=30) as response:
        payload = json.load(response)
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("Unexpected API response: expected an object containing a 'data' list.")
    return payload["data"]


def list_room_boards(base_url: str, token: str, room_id: str) -> list[dict]:
    """List boards visible to the authenticated user in a room."""
    bearer_token = token.removeprefix("Bearer ").strip()
    url = api_url(base_url, f"/rooms/{quote(room_id, safe='')}/boards", {})
    request = Request(url, headers={"Authorization": f"Bearer {bearer_token}", "Accept": "application/json"})
    with urlopen(request, timeout=30) as response:
        payload = json.load(response)
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("Unexpected API response: expected a room board object containing a 'data' list.")
    return payload["data"]


def get_api_json(base_url: str, token: str, path: str, params: dict[str, str] | None = None) -> dict:
    """Fetch a JSON object from the authenticated v3 API."""
    bearer_token = token.removeprefix("Bearer ").strip()
    url = api_url(base_url, path, params or {})
    request = Request(url, headers={"Authorization": f"Bearer {bearer_token}", "Accept": "application/json"})
    with urlopen(request, timeout=30) as response:
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise ValueError(f"Unexpected API response from {path}: expected a JSON object.")
    return payload


def _jwt_is_expired(token: str, *, leeway_seconds: int = 30) -> bool:
    """Check a JWT's exp claim locally so short-lived tokens can be renewed on demand."""
    raw_token = token.removeprefix("Bearer ").strip()
    parts = raw_token.split(".")
    if len(parts) != 3:
        return False
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
        expires_at = claims.get("exp") if isinstance(claims, dict) else None
        return isinstance(expires_at, (int, float)) and expires_at <= time.time() + leeway_seconds
    except (ValueError, UnicodeError, json.JSONDecodeError):
        return False


def _request_schoolcloud_login(
    base_url: str,
    username: str,
    password: str,
    *,
    method: str = "local",
    school_id: str | None = None,
    system_id: str | None = None,
) -> str:
    """Log in through a documented SchoolCloud v3 authentication endpoint."""
    paths = {
        "local": "/authentication/local",
        "local-service-account": "/authentication/local-service-account",
        "ldap": "/authentication/ldap",
    }
    if method not in paths:
        raise ValueError(
            "SCHOOLCLOUD_AUTH_METHOD must be local, local-service-account, or ldap. "
            "OAuth/SSO login requires its provider flow and is not supported by username/password login."
        )
    body: dict[str, str] = {"username": username, "password": password}
    if method == "ldap":
        if not school_id or not system_id:
            raise ValueError("LDAP login requires SCHOOLCLOUD_SCHOOL_ID and SCHOOLCLOUD_SYSTEM_ID.")
        body.update({"schoolId": school_id, "systemId": system_id})
    request = Request(
        api_url(base_url, paths[method], {}),
        data=json.dumps(body).encode("utf-8"),
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=30) as response:
        payload = json.load(response)
    token = payload.get("accessToken") if isinstance(payload, dict) else None
    if not isinstance(token, str) or not token.strip():
        raise ValueError("SchoolCloud login succeeded but returned no accessToken.")
    return token


def get_schoolcloud_token(
    base_url: str,
    *,
    username: str | None = None,
    prompt: bool = False,
) -> str:
    """Reuse a valid token, or obtain one from injected credentials / interactive login."""
    token = os.environ.get("SCHOOLCLOUD_TOKEN", "").strip()
    if token and not _jwt_is_expired(token):
        return token

    auth_method = os.environ.get("SCHOOLCLOUD_AUTH_METHOD", "local").strip().casefold()
    login_username = (username or os.environ.get("SCHOOLCLOUD_USERNAME", "")).strip()
    password = os.environ.get("SCHOOLCLOUD_PASSWORD", "")
    interactive = prompt and sys.stdin.isatty()
    if not login_username and interactive:
        login_username = input("SchoolCloud username: ").strip()
    if not password and interactive:
        password = getpass("SchoolCloud password: ")
    if not login_username or not password:
        if token:
            raise ValueError(
                "SCHOOLCLOUD_TOKEN is expired or near expiry. Set SCHOOLCLOUD_USERNAME and "
                "SCHOOLCLOUD_PASSWORD, or run interactively to log in."
            )
        raise ValueError(
            "Set SCHOOLCLOUD_TOKEN, or provide SCHOOLCLOUD_USERNAME and SCHOOLCLOUD_PASSWORD; "
            "interactive commands can prompt for credentials."
        )
    school_id = os.environ.get("SCHOOLCLOUD_SCHOOL_ID", "").strip()
    system_id = os.environ.get("SCHOOLCLOUD_SYSTEM_ID", "").strip()
    return _request_schoolcloud_login(
        base_url,
        login_username,
        password,
        method=auth_method,
        school_id=school_id,
        system_id=system_id,
    )


def get_me(base_url: str, token: str) -> dict:
    """Fetch the authenticated user's identity and permissions from /me."""
    return get_api_json(base_url, token, "/me")


def get_admin_token(base_url: str, *, prompt: bool = False) -> str:
    """Prefer an explicitly supplied elevated token for school administration."""
    admin_token = os.environ.get("SVS_ADMIN_TOKEN", "").strip()
    if admin_token:
        if _jwt_is_expired(admin_token):
            raise ValueError("SVS_ADMIN_TOKEN is expired. Supply a current admin JWT.")
        return admin_token
    return get_schoolcloud_token(base_url, prompt=prompt)


def get_school_systems(base_url: str, token: str, school_id: str) -> list[dict]:
    """List identity systems attached to a school through the v3 API."""
    request = Request(
        api_url(base_url, f"/school/{quote(school_id, safe='')}/systems", {}),
        headers={
            "Authorization": f"Bearer {token.removeprefix('Bearer ').strip()}",
            "Accept": "application/json",
        },
    )
    with urlopen(request, timeout=30) as response:
        payload = json.load(response)
    if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
        raise ValueError("Unexpected API response: expected a list of school systems.")
    return payload


SCHULCONNEX_OPTION_DEFAULTS = {
    "groupProvisioningClassesEnabled": True,
    "groupProvisioningCoursesEnabled": False,
    "groupProvisioningOtherEnabled": False,
    "schoolExternalToolProvisioningEnabled": False,
}


def get_school_system_options(base_url: str, token: str, school_id: str, system_id: str) -> dict:
    """Read the SchulConneX system options for a school."""
    return get_api_json(
        base_url,
        token,
        f"/schools/{quote(school_id, safe='')}/systems/{quote(system_id, safe='')}/provisioning-options",
    )


def set_school_system_options(
    base_url: str, token: str, school_id: str, system_id: str, options: dict[str, bool]
) -> dict:
    """Replace a school's complete set of system provisioning options."""
    return api_json_request(
        base_url,
        token,
        f"/schools/{quote(school_id, safe='')}/systems/{quote(system_id, safe='')}/provisioning-options",
        method="POST",
        body=options,
    )


def resolve_school_system(
    base_url: str, token: str, school_id: str | None, system_id: str | None
) -> tuple[str, dict]:
    """Resolve a school and its single UI-supported moin.schule system."""
    target_school_id = school_id.strip() if school_id else school_id_from_token(token)
    systems = get_school_systems(base_url, token, target_school_id)
    if system_id:
        matches = [item for item in systems if str(item.get("id", "")) == system_id]
    else:
        matches = [item for item in systems if item.get("alias") == "moin.schule"]
    if not matches:
        if system_id:
            raise ValueError(f"System {system_id} is not attached to school {target_school_id}.")
        raise ValueError(
            f"No moin.schule system was found for school {target_school_id}; pass --systemid to select one."
        )
    if len(matches) != 1:
        raise ValueError("More than one matching system was found; pass --systemid explicitly.")
    system = matches[0]
    if system.get("alias") != "moin.schule":
        raise ValueError(
            "Provisioning options are currently exposed in the SVS UI only for the moin.schule system."
        )
    return target_school_id, system


def print_school_systems(systems: list[dict], output_format: str) -> None:
    if output_format == "json":
        print(json.dumps(systems, ensure_ascii=False, indent=2))
        return
    if not systems:
        print("No systems found for this school.")
        return
    print(f"{'ID':<25}  {'Type':<16}  Alias")
    print(f"{'-' * 25}  {'-' * 16}  {'-' * 30}")
    for system in systems:
        print(f"{system.get('id', ''):<25}  {system.get('type', ''):<16}  {system.get('alias', '')}")


def print_school_system_options(options: dict, output_format: str) -> None:
    if output_format == "json":
        print(json.dumps(options, ensure_ascii=False, indent=2))
        return
    labels = (
        ("groupProvisioningClassesEnabled", "Classes"),
        ("groupProvisioningCoursesEnabled", "Courses"),
        ("groupProvisioningOtherEnabled", "Other groups"),
        ("schoolExternalToolProvisioningEnabled", "School external tools"),
    )
    for key, label in labels:
        value = options.get(key)
        if not isinstance(value, bool):
            raise ValueError(f"SchoolCloud returned an invalid value for {key}.")
        print(f"{label:<24} {'enabled' if value else 'disabled'}")


def _enabled_value(value: str) -> bool:
    return value == "enabled"


def api_json_request(
    base_url: str,
    token: str,
    path: str,
    *,
    method: str = "GET",
    body: dict | None = None,
) -> dict:
    """Send an authenticated JSON request to the v3 server API."""
    headers = {
        "Authorization": f"Bearer {token.removeprefix('Bearer ').strip()}",
        "Accept": "application/json",
    }
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(api_url(base_url, path, {}), data=data, headers=headers, method=method)
    with urlopen(request, timeout=30) as response:
        response_body = response.read()
    if not response_body:
        return {}
    payload = json.loads(response_body)
    if not isinstance(payload, dict):
        raise ValueError(f"Unexpected API response from {path}: expected a JSON object.")
    return payload


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.parts.append(data.strip())


def _html_to_text(value: object) -> str:
    extractor = _TextExtractor()
    extractor.feed(str(value or ""))
    return " ".join(" ".join(extractor.parts).split())


def _wikimedia_user_agent() -> str:
    return os.environ.get("WIKIMEDIA_USER_AGENT", "").strip() or "svs/0.1"


def _wikimedia_retry_delay(error: HTTPError, attempt: int) -> float:
    retry_after = error.headers.get("Retry-After", "")
    try:
        delay = max(0.0, float(retry_after))
    except (TypeError, ValueError):
        try:
            retry_at = parsedate_to_datetime(retry_after).timestamp()
            delay = max(0.0, retry_at - time.time())
        except (TypeError, ValueError, OverflowError):
            delay = float(2 ** attempt)
    return min(delay, 60.0)


def _wikimedia_json(request: Request) -> dict:
    """Fetch Wikimedia JSON with bounded retries when the public API rate-limits us."""
    for attempt in range(3):
        try:
            with urlopen(request, timeout=30) as response:
                payload = json.load(response)
            if not isinstance(payload, dict):
                raise ValueError("Wikimedia returned an unexpected JSON response.")
            return payload
        except HTTPError as error:
            if error.code != 429 or attempt == 2:
                raise
            time.sleep(_wikimedia_retry_delay(error, attempt))
    raise AssertionError("Unreachable Wikimedia retry state")


def _preferred_wikimedia_thumbnail_host(image_url: str) -> str:
    """Use Wikimedia's thumbnail CDN for generated thumbnail paths when available."""
    parts = urlsplit(image_url)
    if parts.hostname == "upload.wikimedia.org" and "/thumb/" in parts.path:
        return urlunsplit((parts.scheme, "thumb.wikimedia.org", parts.path, parts.query, parts.fragment))
    return image_url


def search_commons_images(query: str, limit: int = 5) -> list[dict]:
    """Find reusable Wikimedia Commons images and retain attribution metadata."""
    params = {
        "action": "query",
        "generator": "search",
        "gsrnamespace": "6",
        "gsrsearch": query,
        "gsrlimit": str(limit),
        "prop": "imageinfo",
        "iiprop": "url|mime|thumbmime|extmetadata",
        # Request a practical board preview size so Commons returns an actual
        # /thumb/ URL instead of a thumbnail_unscaled URL to the original file.
        "iiurlwidth": "800",
        "format": "json",
        "formatversion": "2",
    }
    request = Request(
        "https://commons.wikimedia.org/w/api.php?" + urlencode(params),
        headers={"Accept": "application/json", "User-Agent": _wikimedia_user_agent()},
    )
    payload = _wikimedia_json(request)

    results: list[dict] = []
    pages = (payload.get("query") or {}).get("pages", []) if isinstance(payload, dict) else []
    for page in (pages if isinstance(pages, list) else []):
        if not isinstance(page, dict):
            continue
        image_infos = page.get("imageinfo")
        image_info = image_infos[0] if isinstance(image_infos, list) and image_infos and isinstance(image_infos[0], dict) else {}
        metadata = image_info.get("extmetadata") or {}
        license_name = _html_to_text((metadata.get("LicenseShortName") or {}).get("value", ""))
        normalized_license = license_name.casefold()
        if not license_name or not any(term in normalized_license for term in ("cc0", "cc by", "public domain")):
            continue
        if any(term in normalized_license for term in ("noncommercial", "no derivatives", "cc by-nc", "cc by-nd")):
            continue
        image_url = image_info.get("thumburl") or image_info.get("url")
        if isinstance(image_url, str):
            image_url = _preferred_wikimedia_thumbnail_host(image_url)
        description_url = image_info.get("descriptionurl")
        mime_type = image_info.get("thumbmime") or image_info.get("mime", "")
        if (
            not isinstance(image_url, str)
            or urlsplit(image_url).hostname not in ("upload.wikimedia.org", "thumb.wikimedia.org")
            or not isinstance(description_url, str)
            or urlsplit(description_url).hostname != "commons.wikimedia.org"
            or mime_type not in ("image/jpeg", "image/png", "image/webp")
        ):
            continue
        results.append(
            {
                "title": page.get("title", ""),
                "url": image_url,
                "originalUrl": image_info.get("url", ""),
                "sourceUrl": description_url,
                "description": _html_to_text((metadata.get("ImageDescription") or {}).get("value", "")),
                "artist": _html_to_text((metadata.get("Artist") or {}).get("value", "")),
                "license": license_name,
                "licenseUrl": _html_to_text((metadata.get("LicenseUrl") or {}).get("value", "")),
                "mimeType": mime_type,
            }
        )
    return results


def search_wikipedia_articles(query: str, limit: int = 3) -> list[dict]:
    """Search German Wikipedia for source links related to the lesson topic."""
    params = {
        "action": "query",
        "list": "search",
        "srsearch": query,
        "srnamespace": "0",
        "srlimit": str(limit),
        "srprop": "snippet",
        "format": "json",
        "formatversion": "2",
    }
    request = Request(
        "https://de.wikipedia.org/w/api.php?" + urlencode(params),
        headers={"Accept": "application/json", "User-Agent": _wikimedia_user_agent()},
    )
    payload = _wikimedia_json(request)
    search_results = (payload.get("query") or {}).get("search", []) if isinstance(payload, dict) else []
    return [
        {
            "title": result.get("title", ""),
            "url": "https://de.wikipedia.org/wiki/" + quote(str(result.get("title", "")).replace(" ", "_"), safe="()"),
        }
        for result in (search_results if isinstance(search_results, list) else [])
        if isinstance(result, dict) and result.get("title")
    ]


def get_wikipedia_page_thumbnail(article_url: str) -> str | None:
    """Get a German Wikipedia lead-image thumbnail when the meta extractor has none."""
    parts = urlsplit(article_url)
    if parts.scheme != "https" or parts.hostname not in ("de.wikipedia.org", "www.de.wikipedia.org"):
        return None
    prefix = "/wiki/"
    if not parts.path.startswith(prefix):
        return None
    page_title = unquote(parts.path[len(prefix):]).replace("_", " ")
    if not page_title:
        return None
    params = {
        "action": "query",
        "prop": "pageimages",
        "piprop": "thumbnail",
        "pithumbsize": "1280",
        "titles": page_title,
        "format": "json",
        "formatversion": "2",
    }
    request = Request(
        "https://de.wikipedia.org/w/api.php?" + urlencode(params),
        headers={"Accept": "application/json", "User-Agent": _wikimedia_user_agent()},
    )
    payload = _wikimedia_json(request)
    pages = (payload.get("query") or {}).get("pages", [])
    for page in pages if isinstance(pages, list) else []:
        thumbnail = page.get("thumbnail") if isinstance(page, dict) else None
        source = thumbnail.get("source") if isinstance(thumbnail, dict) else None
        source_parts = urlsplit(source) if isinstance(source, str) else None
        if source_parts and source_parts.scheme == "https" and source_parts.hostname in (
            "upload.wikimedia.org", "thumb.wikimedia.org"
        ):
            return source
    return None


def _normalize_media_queries(value: object, label: str) -> list[str]:
    """Validate concise search phrases proposed by the lesson-planning model."""
    if not isinstance(value, list) or not 1 <= len(value) <= 3:
        raise ValueError(f"The generated plan must include 1 to 3 {label} search queries.")
    queries: list[str] = []
    for query in value:
        if not isinstance(query, str) or not 3 <= len(query.strip()) <= 120:
            raise ValueError(f"Each {label} search query must contain 3 to 120 characters.")
        normalized = query.strip()
        if normalized.casefold() not in {item.casefold() for item in queries}:
            queries.append(normalized)
    return queries


def _media_terms(value: str) -> set[str]:
    """Return topic terms for checking whether a search result fits the lesson."""
    words = set(re.findall(r"[^\W_]{4,}", value.casefold(), flags=re.UNICODE))
    words.difference_update({"klasse", "stunden", "stunde", "unterricht", "lesson", "grade"})
    words.update(word[:-1] for word in tuple(words) if word.endswith("s") and len(word) > 5)
    aliases = {
        "universum": {"universum", "universe", "cosmos"},
        "wärmetod": {"wärmetod", "waermetod", "heat", "death", "entropy"},
        "waermetod": {"wärmetod", "waermetod", "heat", "death", "entropy"},
        "wärmetodtheorie": {"wärmetod", "waermetod", "heat", "death", "entropy"},
        "heat": {"heat", "wärmetod", "waermetod"},
        "death": {"death", "wärmetod", "waermetod"},
    }
    expanded = set(words)
    for word in words:
        expanded.update(aliases.get(word, ()))
    # Avoid accepting generic pedagogical wording as topical evidence.
    return expanded


def _relevant_media_result(query: str, result: dict) -> bool:
    topic_terms = _media_terms(query)
    result_terms = _media_terms(" ".join(str(result.get(key, "")) for key in ("title", "description")))
    overlap = topic_terms & result_terms
    broad_terms = {"universum", "universums", "universe", "cosmos", "space", "welt", "world"}
    # A generic match like "universe" alone is not enough for a highly specific topic
    # such as heat death; require either two matching terms or one distinctive term.
    return len(overlap) >= 2 or bool(overlap - broad_terms)


def discover_board_resources(media_queries: dict, capabilities: dict) -> dict:
    """Discover one freely reusable image and source articles when board permissions allow them."""
    image_queries = _normalize_media_queries(media_queries.get("imageQueries"), "image")
    link_queries = _normalize_media_queries(media_queries.get("linkQueries"), "Wikipedia link")
    resources = {
        "images": [],
        "links": [],
        "imageSearchQueries": image_queries,
        "linkSearchQueries": link_queries,
        "imageSearchQueriesTried": [],
        "linkSearchQueriesTried": [],
    }
    element_capabilities = {item.get("type"): item for item in capabilities.get("elements", [])}
    file_capability = element_capabilities.get("file", {})
    link_capability = element_capabilities.get("link", {})
    if file_capability.get("available"):
        try:
            for search_query in image_queries:
                resources["imageSearchQueriesTried"].append(search_query)
                candidates = search_commons_images(search_query, limit=12)
                resources["imageCandidatesChecked"] = resources.get("imageCandidatesChecked", 0) + len(candidates)
                resources["images"] = [item for item in candidates if _relevant_media_result(search_query, item)][:1]
                if resources["images"]:
                    resources["imageSearchQueryUsed"] = search_query
                    break
            resources["imageSearchStatus"] = "found" if resources["images"] else (
                "no-relevant-results" if resources.get("imageCandidatesChecked", 0) else "no-results"
            )
        except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError) as error:
            resources["imageSearchStatus"] = "failed"
            resources["imageSearchError"] = str(error)
    else:
        resources["imageSearchStatus"] = "skipped"
        resources["imageSearchReason"] = file_capability.get("reason", "File elements are unavailable.")
    if link_capability.get("available"):
        try:
            for search_query in link_queries:
                resources["linkSearchQueriesTried"].append(search_query)
                candidates = search_wikipedia_articles(search_query, limit=6)
                resources["linkCandidatesChecked"] = resources.get("linkCandidatesChecked", 0) + len(candidates)
                resources["links"] = [item for item in candidates if _relevant_media_result(search_query, item)][:2]
                if resources["links"]:
                    resources["linkSearchQueryUsed"] = search_query
                    break
            resources["linkSearchStatus"] = "found" if resources["links"] else (
                "no-relevant-results" if resources.get("linkCandidatesChecked", 0) else "no-results"
            )
        except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError) as error:
            resources["linkSearchStatus"] = "failed"
            resources["linkSearchError"] = str(error)
    else:
        resources["linkSearchStatus"] = "skipped"
        resources["linkSearchReason"] = link_capability.get("reason", "Link elements are unavailable.")
    return resources


def get_board_school_id(base_url: str, token: str, capabilities: dict) -> str:
    """Resolve the school storage location for a room board, falling back to the user's school."""
    context = capabilities.get("board", {}).get("context", {})
    if context.get("type") == "room" and context.get("id"):
        room = get_api_json(base_url, token, f"/rooms/{quote(context['id'], safe='')}")
        school_id = room.get("schoolId")
        if school_id:
            return str(school_id)
    me = get_me(base_url, token)
    school_id = (me.get("school") or {}).get("id")
    if not school_id:
        raise ValueError("Could not determine the school storage location for board media uploads.")
    return str(school_id)


def upload_file_from_url(
    base_url: str,
    token: str,
    school_id: str,
    parent_id: str,
    source_url: str,
    file_name: str,
    user_agent: str | None = None,
) -> dict:
    """Ask SchoolCloud File Storage to fetch a remote asset into a board element."""
    path = f"/file/upload-from-url/school/{quote(school_id, safe='')}/boardnodes/{quote(parent_id, safe='')}"
    try:
        file_record = api_json_request(
            base_url,
            token,
            path,
            method="POST",
            body={
                "url": source_url,
                "fileName": file_name,
                "headers": {"User-Agent": user_agent or _wikimedia_user_agent()},
            },
        )
        if not file_record.get("id"):
            raise ValueError("File Storage returned no file record ID; it did not confirm an attachment.")
        if file_record.get("parentId") and str(file_record["parentId"]) != parent_id:
            raise ValueError("File Storage returned a file record attached to a different parent.")
        file_record["_uploadMethod"] = "upload-from-url"
        return file_record
    except HTTPError as error:
        response_detail = error.read().decode("utf-8", errors="replace").strip()
        request_url = error.url or api_url(base_url, path, {})
        source_host = (urlsplit(source_url).hostname or "").casefold()
        if error.code == 404 and "FILE_NOT_FOUND" in response_detail and source_host in (
            "upload.wikimedia.org", "thumb.wikimedia.org"
        ):
            # Some File Storage deployments cannot fetch Wikimedia's upload host.
            # Download the same public image in the CLI and use the UI's multipart route.
            try:
                file_record = upload_downloaded_image(
                    base_url, token, school_id, parent_id, source_url, file_name,
                    user_agent or _wikimedia_user_agent(),
                )
            except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError) as fallback_error:
                raise ValueError(
                    f"upload-from-url returned FILE_NOT_FOUND for {source_url}; "
                    f"the local download-and-upload fallback also failed: {fallback_error}"
                ) from fallback_error
            file_record["_uploadMethod"] = "local-download-upload"
            return file_record
        detail = f"; response: {response_detail}" if response_detail else ""
        raise ValueError(
            f"File Storage upload-from-url failed with HTTP {error.code} at {request_url}{detail}"
        ) from error


def upload_downloaded_image(
    base_url: str,
    token: str,
    school_id: str,
    parent_id: str,
    source_url: str,
    file_name: str,
    user_agent: str,
) -> dict:
    """Download a Wikimedia image locally and upload it through File Storage's standard multipart API."""
    maximum_bytes = 25 * 1024 * 1024
    for attempt in range(3):
        request = Request(source_url, headers={"Accept": "image/*", "User-Agent": user_agent})
        try:
            with urlopen(request, timeout=60) as response:
                content_type = response.headers.get_content_type()
                if not content_type.startswith("image/"):
                    raise ValueError(f"Wikimedia returned non-image content ({content_type}) for {source_url}.")
                content = response.read(maximum_bytes + 1)
            break
        except HTTPError as error:
            if error.code != 429 or attempt == 2:
                raise
            delay = _wikimedia_retry_delay(error, attempt)
            print(f"Wikimedia rate-limited image download; retrying in {delay:g}s...", file=sys.stderr, flush=True)
            time.sleep(delay)
    if len(content) > maximum_bytes:
        raise ValueError(f"Image exceeds the CLI's 25 MiB fallback upload limit: {source_url}")

    return upload_multipart_file(
        base_url, token, school_id, parent_id, file_name, content_type, content
    )


def upload_multipart_file(
    base_url: str,
    token: str,
    school_id: str,
    parent_id: str,
    file_name: str,
    content_type: str,
    content: bytes,
) -> dict:
    """Upload file bytes through File Storage's standard authenticated multipart API."""
    maximum_bytes = 25 * 1024 * 1024
    if len(content) > maximum_bytes:
        raise ValueError("File exceeds the CLI's 25 MiB upload limit.")

    boundary = "----svs-cli-" + base64.urlsafe_b64encode(os.urandom(18)).decode("ascii").rstrip("=")
    safe_file_name = re.sub(r"[^A-Za-z0-9._-]", "_", file_name).strip("._") or "image"
    multipart = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{safe_file_name}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode("ascii") + content + f"\r\n--{boundary}--\r\n".encode("ascii")
    path = f"/file/upload/school/{quote(school_id, safe='')}/boardnodes/{quote(parent_id, safe='')}"
    request = Request(
        api_url(base_url, path, {}),
        data=multipart,
        headers={
            "Authorization": f"Bearer {token.removeprefix('Bearer ').strip()}",
            "Accept": "application/json",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )
    with urlopen(request, timeout=60) as response:
        payload = json.load(response)
    if not isinstance(payload, dict) or not payload.get("id"):
        raise ValueError("Multipart upload returned no FileRecord ID.")
    if payload.get("parentId") and str(payload["parentId"]) != parent_id:
        raise ValueError("Multipart upload returned a FileRecord attached to a different parent.")
    return payload


def _board_media_error(label: str, title: str, error: Exception) -> str:
    if isinstance(error, HTTPError):
        response_detail = error.read().decode("utf-8", errors="replace").strip()
        request_url = error.url or "unknown endpoint"
        detail = f"HTTP {error.code} at {request_url}"
        if response_detail:
            detail += f": {response_detail}"
    else:
        detail = str(error)
    return f"{label} '{title}' failed: {detail}"


def create_board_link_card(
    base_url: str,
    token: str,
    board_id: str,
    target_url: str,
    column_id: str | None = None,
    card_title: str | None = None,
) -> dict:
    """Create a card containing one link and report each stage of the preview flow."""
    parsed = urlsplit(target_url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("--url must be an absolute http:// or https:// URL.")
    target_url = target_url.strip()

    board = get_api_json(base_url, token, f"/boards/{quote(board_id, safe='')}")
    allowed = board.get("allowedOperations") or {}
    if not allowed.get("createCard") or not allowed.get("createElement"):
        raise ValueError("This token cannot create cards and elements on the selected board.")
    config = get_api_json(base_url, token, "/config/public")
    if not config.get("FEATURE_COLUMN_BOARD_ENABLED") or not config.get("FEATURE_COLUMN_BOARD_LINK_ELEMENT_ENABLED"):
        raise ValueError("Column boards or link elements are disabled in this deployment.")

    columns = board.get("columns") or []
    if column_id:
        column = next((item for item in columns if str(item.get("id")) == column_id), None)
        if not column:
            raise ValueError(f"Column {column_id} is not part of board {board_id}.")
    else:
        column = next((item for item in columns if item.get("id")), None)
        if not column:
            raise ValueError("The board has no columns. Add a column first or choose another board.")
    column_id = str(column["id"])
    host = parsed.hostname
    title = (card_title or f"Link: {host}").strip()
    if not title or len(title) > 100:
        raise ValueError("Card title must contain 1 to 100 characters.")

    print(f"Board: {board.get('title') or board_id} ({board_id})")
    print(f"Using column: {column.get('title') or column_id} ({column_id})")
    print(f"Creating link card for: {target_url}")
    card = api_json_request(base_url, token, f"/columns/{quote(column_id, safe='')}/cards", method="POST", body={})
    card_id = card.get("id")
    if not card_id:
        raise ValueError("The server created a card but returned no ID.")
    api_json_request(base_url, token, f"/cards/{quote(card_id, safe='')}/title", method="PATCH", body={"title": title})
    print(f"Created card: {title} ({card_id})")

    element = api_json_request(base_url, token, f"/cards/{quote(card_id, safe='')}/elements", method="POST", body={"type": "link"})
    element_id = element.get("id")
    if not element_id:
        raise ValueError(f"Card {card_id} was created, but the link element response had no ID.")
    print(f"Created link element: {element_id}")

    metadata = {"url": target_url, "title": "", "description": "", "originalImageUrl": "", "imageUrl": ""}
    try:
        extracted = api_json_request(base_url, token, "/meta-tag-extractor", method="POST", body={"url": target_url})
        metadata.update({key: extracted.get(key) or "" for key in ("url", "title", "description", "originalImageUrl")})
        metadata["url"] = metadata.get("url") or target_url
        print("Metadata extraction: succeeded")
        print(f"  Title: {metadata['title'] or '(none returned; UI normally leaves it blank)'}")
        print(f"  Description: {'present' if metadata['description'] else 'none returned'}")
        print(f"  Preview image: {metadata['originalImageUrl'] or '(none returned)'}")
    except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError) as error:
        print(f"Metadata extraction: failed: {_board_media_error('Metadata extraction', target_url, error)}")
        metadata["url"] = target_url

    original_image = metadata.get("originalImageUrl")
    if original_image:
        image_parts = urlsplit(str(original_image))
        if image_parts.scheme in ("http", "https") and image_parts.hostname:
            image_name = unquote(image_parts.path.rsplit("/", 1)[-1]) or "link-preview-image"
            print("Uploading source preview image to File Storage (same endpoint and User-Agent as the UI)...")
            try:
                context = get_api_json(base_url, token, f"/boards/{quote(board_id, safe='')}/context")
                school_id = get_board_school_id(base_url, token, {"board": {"context": context}})
                record = upload_file_from_url(
                    base_url, token, school_id, str(element_id), str(original_image), image_name,
                    user_agent="Embed Request User Agent",
                )
                print(f"  FileRecord: {record.get('id')} | parentId: {record.get('parentId')} | previewStatus: {record.get('previewStatus')}")
                record_url = str(record.get("url") or "")
                relative_path = urlsplit(record_url).path if record_url else ""
                if record_url.startswith("/") and record.get("previewStatus") == "preview_possible":
                    # Match the UI helper: download -> preview, then request WebP.
                    metadata["imageUrl"] = relative_path.replace("download", "preview", 1) + "?outputFormat=image/webp"
                    print(f"  Preview URL to save: {metadata['imageUrl']}")
                else:
                    print(f"  No preview URL will be saved (FileRecord URL/status: {record_url or '(missing)'} / {record.get('previewStatus')}).")
            except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError) as error:
                print(f"  Image upload failed: {_board_media_error('File Storage upload', str(original_image), error)}")
        else:
            print(f"Preview image URL is invalid or unsupported: {original_image}")
    else:
        print("Preview upload: skipped because metadata extraction returned no source image.")

    api_json_request(
        base_url, token, f"/elements/{quote(str(element_id), safe='')}/content", method="PATCH",
        body={"data": {"type": "link", "content": metadata}},
    )
    print("Saved link content to the element.")
    saved = api_json_request(base_url, token, f"/elements/{quote(str(element_id), safe='')}")
    saved_content = (saved.get("element") or {}).get("content") or {}
    print("Read-back from SchoolCloud:")
    print(f"  URL: {saved_content.get('url') or '(missing)'}")
    print(f"  Title: {saved_content.get('title') or '(blank)'}")
    print(f"  imageUrl: {saved_content.get('imageUrl') or '(blank — no preview saved)'}")
    return {"boardId": board_id, "columnId": column_id, "cardId": card_id, "elementId": element_id, "metadata": metadata, "saved": saved_content}


def create_board_file_card(
    base_url: str,
    token: str,
    board_id: str,
    source_url: str | None = None,
    local_file: str | None = None,
    column_id: str | None = None,
    card_title: str | None = None,
    caption: str | None = None,
    alternative_text: str | None = None,
) -> dict:
    """Create one card with one image file element, sourced from a URL or local file."""
    if bool(source_url) == bool(local_file):
        raise ValueError("Provide exactly one source: --url or --file.")
    content: bytes | None = None
    content_type: str | None = None
    if source_url:
        parsed_source = urlsplit(source_url.strip())
        if parsed_source.scheme not in ("http", "https") or not parsed_source.hostname:
            raise ValueError("--url must be an absolute http:// or https:// URL.")
        source_url = source_url.strip()
        file_name = unquote(parsed_source.path.rsplit("/", 1)[-1]) or "image"
        default_title = f"Image: {parsed_source.hostname}"
    else:
        file_path = Path(str(local_file)).expanduser()
        if not file_path.is_file():
            raise ValueError(f"Local image file does not exist: {file_path}")
        file_name = file_path.name
        content_type = mimetypes.guess_type(file_name)[0] or ""
        if not content_type.startswith("image/"):
            raise ValueError(f"Could not identify {file_name} as an image from its file extension.")
        content = file_path.read_bytes()
        if not content:
            raise ValueError("Local image file is empty.")
        if len(content) > 25 * 1024 * 1024:
            raise ValueError("Local image exceeds the CLI's 25 MiB upload limit.")
        default_title = f"Image: {file_name}"

    board = get_api_json(base_url, token, f"/boards/{quote(board_id, safe='')}")
    allowed = board.get("allowedOperations") or {}
    if not allowed.get("createCard") or not allowed.get("createFileElement"):
        raise ValueError("This token cannot create cards and file elements on the selected board.")
    config = get_api_json(base_url, token, "/config/public")
    if not config.get("FEATURE_COLUMN_BOARD_ENABLED"):
        raise ValueError("Column boards are disabled in this deployment.")
    columns = board.get("columns") or []
    if column_id:
        column = next((item for item in columns if str(item.get("id")) == column_id), None)
        if not column:
            raise ValueError(f"Column {column_id} is not part of board {board_id}.")
    else:
        column = next((item for item in columns if item.get("id")), None)
        if not column:
            raise ValueError("The board has no columns. Add a column first or choose another board.")
    column_id = str(column["id"])
    title = (card_title or default_title).strip()
    if not title or len(title) > 100:
        raise ValueError("Card title must contain 1 to 100 characters.")
    context = get_api_json(base_url, token, f"/boards/{quote(board_id, safe='')}/context")
    school_id = get_board_school_id(base_url, token, {"board": {"context": context}})

    source_description = source_url or str(local_file)
    print(f"Board: {board.get('title') or board_id} ({board_id})")
    print(f"Using column: {column.get('title') or column_id} ({column_id})")
    print(f"Image source: {source_description}")
    card = api_json_request(base_url, token, f"/columns/{quote(column_id, safe='')}/cards", method="POST", body={})
    card_id = card.get("id")
    if not card_id:
        raise ValueError("The server created a card but returned no ID.")
    api_json_request(base_url, token, f"/cards/{quote(card_id, safe='')}/title", method="PATCH", body={"title": title})
    print(f"Created card: {title} ({card_id})")
    element = api_json_request(base_url, token, f"/cards/{quote(card_id, safe='')}/elements", method="POST", body={"type": "file"})
    element_id = element.get("id")
    if not element_id:
        raise ValueError(f"Card {card_id} was created, but the file element response had no ID.")
    print(f"Created file element: {element_id}")
    api_json_request(
        base_url,
        token,
        f"/elements/{quote(element_id, safe='')}/content",
        method="PATCH",
        body={
            "data": {
                "type": "file",
                "content": {
                    "caption": caption if caption is not None else (f"Quelle: {source_url}" if source_url else ""),
                    "alternativeText": alternative_text or title,
                },
            }
        },
    )
    try:
        if source_url:
            record = upload_file_from_url(
                base_url,
                token,
                school_id,
                element_id,
                source_url,
                file_name,
                user_agent="Embed Request User Agent",
            )
        else:
            record = upload_multipart_file(
                base_url, token, school_id, element_id, file_name, str(content_type), content or b""
            )
    except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError) as error:
        message = _board_media_error("Image file upload", source_description, error)
        try:
            api_json_request(base_url, token, f"/elements/{quote(element_id, safe='')}", method="DELETE")
            print(f"Removed empty file element {element_id} after upload failure.")
        except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError) as cleanup_error:
            message += "; cleanup failed: " + _board_media_error("Empty file element cleanup", element_id, cleanup_error)
        raise ValueError(f"{message}. Card {card_id} remains, but no file was attached.") from error

    print("File upload succeeded:")
    print(f"  FileRecord: {record.get('id')}")
    print(f"  Parent element: {record.get('parentId') or element_id}")
    print(f"  File name: {record.get('name') or file_name}")
    print(f"  MIME type: {record.get('mimeType') or content_type or 'reported by File Storage'}")
    print(f"  Preview status: {record.get('previewStatus') or 'unknown'}")
    print(f"  Upload method: {record.get('_uploadMethod') or 'multipart upload'}")
    return {"boardId": board_id, "columnId": column_id, "cardId": card_id, "elementId": element_id, "fileRecord": record}


def get_board_capabilities(base_url: str, token: str, board_id: str) -> dict:
    """Combine live deployment flags, board permissions, and school tool options."""
    board = get_api_json(base_url, token, f"/boards/{quote(board_id, safe='')}")
    context = get_api_json(base_url, token, f"/boards/{quote(board_id, safe='')}/context")
    config = get_api_json(base_url, token, "/config/public")
    allowed = board.get("allowedOperations") or {}
    features = set(board.get("features") or [])
    base_enabled = bool(config.get("FEATURE_COLUMN_BOARD_ENABLED", False))
    create_element = bool(allowed.get("createElement", False))
    definitions = [
        ("text", "richText", base_enabled and create_element, _capability_reason(base_enabled, create_element)),
        ("drawing / whiteboard", "drawing", base_enabled and create_element and bool(config.get("FEATURE_TLDRAW_ENABLED")), _feature_capability_reason(base_enabled and create_element, config, "FEATURE_TLDRAW_ENABLED")),
        ("collaborative Etherpad", "collaborativeTextEditor", base_enabled and create_element and bool(config.get("FEATURE_COLUMN_BOARD_COLLABORATIVE_TEXT_EDITOR_ENABLED")), _feature_capability_reason(base_enabled and create_element, config, "FEATURE_COLUMN_BOARD_COLLABORATIVE_TEXT_EDITOR_ENABLED")),
        ("link", "link", base_enabled and create_element and bool(config.get("FEATURE_COLUMN_BOARD_LINK_ELEMENT_ENABLED")), _feature_capability_reason(base_enabled and create_element, config, "FEATURE_COLUMN_BOARD_LINK_ELEMENT_ENABLED")),
        ("file attachment", "file", base_enabled and bool(allowed.get("createFileElement", False)), _capability_reason(base_enabled, bool(allowed.get("createFileElement", False)))),
        ("file folder", "fileFolder", base_enabled and create_element and bool(config.get("FEATURE_COLUMN_BOARD_FILE_FOLDER_ENABLED")), _feature_capability_reason(base_enabled and create_element, config, "FEATURE_COLUMN_BOARD_FILE_FOLDER_ENABLED")),
        ("H5P activity", "h5p", base_enabled and create_element and bool(config.get("FEATURE_COLUMN_BOARD_H5P_ENABLED")), _feature_capability_reason(base_enabled and create_element, config, "FEATURE_COLUMN_BOARD_H5P_ENABLED")),
        ("external tool", "externalTool", base_enabled and create_element and bool(config.get("FEATURE_COLUMN_BOARD_EXTERNAL_TOOLS_ENABLED")) and bool(allowed.get("createExternalToolElement", False)), _feature_capability_reason(base_enabled and create_element and bool(allowed.get("createExternalToolElement", False)), config, "FEATURE_COLUMN_BOARD_EXTERNAL_TOOLS_ENABLED")),
        ("video conference", "videoConference", base_enabled and create_element and bool(config.get("FEATURE_VIDEOCONFERENCE_ENABLED")) and bool(config.get("FEATURE_COLUMN_BOARD_VIDEOCONFERENCE_ENABLED")) and "videoconference" in features and bool(allowed.get("manageVideoConference", False)), _video_conference_reason(base_enabled and create_element, config, features, allowed)),
        ("Collabora document", "collabora", base_enabled and bool(config.get("FEATURE_COLUMN_BOARD_COLLABORA_ENABLED")) and bool(allowed.get("createFileElement", False)), _feature_capability_reason(base_enabled and bool(allowed.get("createFileElement", False)), config, "FEATURE_COLUMN_BOARD_COLLABORA_ENABLED")),
    ]
    capabilities = [
        {"label": label, "type": kind, "available": bool(available), "reason": reason}
        for label, kind, available, reason in definitions
    ]
    tools: list[dict] = []
    tool_discovery_error = None
    if not config.get("FEATURE_COLUMN_BOARD_EXTERNAL_TOOLS_ENABLED"):
        tool_discovery_status = "External tools are disabled by deployment configuration."
    elif not create_element or not allowed.get("createExternalToolElement"):
        tool_discovery_status = "The current user cannot create external-tool elements on this board."
    elif not config.get("FEATURE_PREFERRED_CTL_TOOLS_ENABLED"):
        tool_discovery_status = "Preferred-tool shortcuts are disabled; other tool choices require the UI configuration flow."
    else:
        try:
            tool_payload = get_api_json(base_url, token, "/tools/preferred-tools", {"contextType": "board-element"})
            tools = tool_payload.get("data", []) if isinstance(tool_payload.get("data"), list) else []
            tool_discovery_status = "Preferred tools loaded for the current user's school."
        except HTTPError as error:
            if error.code != 403:
                raise
            tool_discovery_error = "The server denied preferred-tool discovery (403)."
            tool_discovery_status = tool_discovery_error
    return {
        "board": {
            "id": board.get("id") or board_id,
            "title": board.get("title", ""),
            "layout": board.get("layout", ""),
            "context": context,
            "features": sorted(features),
        },
        "structure": {
            "createColumns": bool(allowed.get("createColumn", False)),
            "createCards": bool(allowed.get("createCard", False)),
            "createElements": create_element,
            "updateCardColor": bool(allowed.get("updateCardColor", False)),
        },
        "elements": capabilities,
        "preferredTools": tools,
        "toolDiscoveryStatus": tool_discovery_status,
        "toolDiscoveryError": tool_discovery_error,
    }


def _board_authoring_schema() -> dict:
    """Return the board plan shape validated after an LLM response."""
    card_schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "body": {"type": "string"},
        },
        "required": ["title", "body"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "notes": {"type": "array", "items": {"type": "string"}},
            "mediaSearch": {
                "type": "object",
                "properties": {
                    "imageQueries": {"type": "array", "items": {"type": "string"}},
                    "linkQueries": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["imageQueries", "linkQueries"],
                "additionalProperties": False,
            },
            "columns": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "cards": {"type": "array", "items": card_schema},
                    },
                    "required": ["title", "cards"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["summary", "notes", "mediaSearch", "columns"],
        "additionalProperties": False,
    }


def call_llm_for_board_plan(
    endpoint_url: str,
    token: str | None,
    model: str,
    prompt: str,
    board: dict,
    capabilities: dict,
    api_key_header: str = "Authorization",
) -> dict:
    """Ask an OpenAI-compatible Chat Completions endpoint for a board plan."""
    playbook_path = Path(__file__).parent / "skills" / "board-content-authoring" / "SKILL.md"
    try:
        playbook = playbook_path.read_text(encoding="utf-8")
    except OSError as error:
        raise ValueError(f"Could not read the board authoring playbook at {playbook_path}.") from error

    current_columns = [
        {"title": column.get("title", ""), "cardCount": len(column.get("cards", []))}
        for column in board.get("columns", [])
    ]
    live_context = {
        "board": {
            "title": board.get("title"),
            "layout": board.get("layout"),
            "features": board.get("features", []),
            "allowedOperations": board.get("allowedOperations", {}),
            "existingColumns": current_columns,
        },
        "discoveredCapabilities": capabilities.get("elements", []),
        "toolDiscoveryStatus": capabilities.get("toolDiscoveryStatus"),
        "preferredTools": [
            {key: tool[key] for key in ("name", "description") if isinstance(tool.get(key), str)}
            for tool in capabilities.get("preferredTools", [])
            if isinstance(tool, dict)
        ],
    }
    system_prompt = (
        "You design pedagogically useful SchoolCloud lesson-board content. Follow this project playbook:\n\n"
        f"{playbook}\n\n"
        "For this first CLI version, output only new columns, cards, and formatted rich-text element bodies. "
        "Do not emit API calls, IDs, external-tool parameters, or URLs. The CLI may add verified Wikimedia Commons images and German Wikipedia source links automatically after this plan is reviewed. "
        "Also provide separate imageQueries and linkQueries in mediaSearch: 1-3 short, precise search phrases (about 2-6 topical terms each). "
        "Derive them from the core subject matter, not incidental words from the teacher's sentence, lesson instructions, age, or duration. "
        "Use terms likely to find a suitable educational illustration in Commons and relevant reference articles in German Wikipedia. "
        "Queries may be in German or English as appropriate, but must name the actual concept; do not return vague words such as 'einem kurzen'. "
        "H5P is blacklisted. Do not claim specialized capabilities were used. The CLI appends new columns; do not repeat existing columns. "
        "Write each card body as a short HTML fragment for the SchoolCloud CKEditor 5 rich-text field, not Markdown or plain text. "
        "Use only these tags: p, br, strong, b, em, i, u, s, h4, h5, ul, ol, li, blockquote, code, sup, sub, mark. "
        "Put paragraphs in p tags, and represent every list as ul or ol containing li tags; never type dash-prefixed pseudo-lists. "
        "Use bold labels sparingly to make directions scannable. Do not use styles, classes, images, or links. "
        "Use concise, age-appropriate directions, clear student actions, expected artifacts, and accessible language. "
        "If important lesson details are absent, make reasonable assumptions and state them in notes. Keep the plan to 1-6 columns, "
        "1-8 cards per column, titles at most 100 characters, and card bodies at most 5000 characters."
    )
    system_prompt += " Return exactly one JSON object with keys summary, notes, and columns matching this schema:\n"
    system_prompt += json.dumps(_board_authoring_schema(), ensure_ascii=False)
    request_body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": "Live board and feature context:\n"
                + json.dumps(live_context, ensure_ascii=False)
                + "\n\nTeacher request:\n"
                + prompt,
            },
        ],
        "response_format": {"type": "json_object"},
    }
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if token:
        key = token.strip()
        headers[api_key_header] = f"Bearer {key}" if api_key_header.casefold() == "authorization" else key
    request = Request(
        endpoint_url,
        data=json.dumps(request_body, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urlopen(request, timeout=120) as response:
        response_data = json.load(response)
    choices = response_data.get("choices", [])
    output_text = ""
    if choices and isinstance(choices[0], dict):
        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, str):
            output_text = content.strip()
        elif isinstance(content, list):
            output_text = "\n".join(
                item.get("text", "") for item in content if isinstance(item, dict) and isinstance(item.get("text"), str)
            ).strip()
    if not output_text:
        raise ValueError("The LLM provider returned no board plan text (the response may have been refused or incomplete).")
    plan = json.loads(output_text)
    validate_board_plan(plan)
    return plan


def validate_board_plan(plan: dict) -> None:
    """Reject plans that exceed the command's supported board-writing scope."""
    if not isinstance(plan.get("summary"), str) or not isinstance(plan.get("notes"), list) or not all(
        isinstance(note, str) for note in plan["notes"]
    ):
        raise ValueError("The generated plan has invalid summary or notes fields.")
    if not isinstance(plan.get("mediaSearch"), dict):
        raise ValueError("The generated plan has invalid media search queries.")
    _normalize_media_queries(plan["mediaSearch"].get("imageQueries"), "image")
    _normalize_media_queries(plan["mediaSearch"].get("linkQueries"), "Wikipedia link")
    columns = plan.get("columns")
    if not isinstance(columns, list) or not 1 <= len(columns) <= 6:
        raise ValueError("The generated plan must contain between 1 and 6 new columns.")
    for column_index, column in enumerate(columns, start=1):
        if not isinstance(column, dict):
            raise ValueError(f"Generated column {column_index} is invalid.")
        title = column.get("title")
        cards = column.get("cards")
        if not isinstance(title, str) or not title.strip() or len(title) > 100:
            raise ValueError(f"Generated column {column_index} has an invalid title.")
        if not isinstance(cards, list) or not 1 <= len(cards) <= 8:
            raise ValueError(f"Generated column {column_index} must contain between 1 and 8 cards.")
        for card_index, card in enumerate(cards, start=1):
            if not isinstance(card, dict):
                raise ValueError(f"Generated card {card_index} in column {column_index} is invalid.")
            card_title = card.get("title")
            body = card.get("body")
            if not isinstance(card_title, str) or not card_title.strip() or len(card_title) > 100:
                raise ValueError(f"Generated card {card_index} in column {column_index} has an invalid title.")
            if not isinstance(body, str) or not body.strip() or len(body) > 5000:
                raise ValueError(f"Generated card {card_index} in column {column_index} has an invalid body.")


def create_board_content(
    base_url: str,
    token: str,
    board_id: str,
    plan: dict,
    capabilities: dict,
    school_id: str,
) -> list[dict]:
    """Append planned content and automatically attach reviewed sources through v3 APIs."""
    created: list[dict] = []
    try:
        color_palette = ("lightBlue", "amber", "lightGreen", "deepPurple")
        for column_index, column in enumerate(plan["columns"]):
            column_result = api_json_request(base_url, token, f"/boards/{quote(board_id, safe='')}/columns", method="POST")
            column_id = column_result.get("id")
            if not column_id:
                raise ValueError("The server created a column but returned no ID.")
            created_column = {"id": column_id, "title": column["title"].strip(), "cards": []}
            created.append(created_column)
            api_json_request(
                base_url,
                token,
                f"/columns/{quote(column_id, safe='')}/title",
                method="PATCH",
                body={"title": column["title"].strip()},
            )
            for card in column["cards"]:
                card_result = api_json_request(
                    base_url,
                    token,
                    f"/columns/{quote(column_id, safe='')}/cards",
                    method="POST",
                    body={},
                )
                card_id = card_result.get("id")
                if not card_id:
                    raise ValueError("The server created a card but returned no ID.")
                created_card = {"id": card_id, "title": card["title"].strip(), "elementId": None}
                created_column["cards"].append(created_card)
                api_json_request(
                    base_url,
                    token,
                    f"/cards/{quote(card_id, safe='')}/title",
                    method="PATCH",
                    body={"title": card["title"].strip()},
                )
                if capabilities.get("structure", {}).get("updateCardColor"):
                    api_json_request(
                        base_url,
                        token,
                        f"/cards/{quote(card_id, safe='')}/color",
                        method="PATCH",
                        body={"backgroundColor": color_palette[column_index % len(color_palette)]},
                    )
                element = api_json_request(
                    base_url,
                    token,
                    f"/cards/{quote(card_id, safe='')}/elements",
                    method="POST",
                    body={"type": "richText"},
                )
                element_id = element.get("id")
                if not element_id:
                    raise ValueError("The server created a text element but returned no ID.")
                created_card["elementId"] = element_id
                api_json_request(
                    base_url,
                    token,
                    f"/elements/{quote(element_id, safe='')}/content",
                    method="PATCH",
                    body={
                        "data": {
                            "type": "richText",
                            "content": {"text": card["body"].strip(), "inputFormat": "richTextCk5"},
                        }
                    },
                )
        resources = plan.get("resources", {})
        resource_images = resources.get("images", [])
        resource_links = resources.get("links", [])
        summary_words = set(re.findall(r"[^\W_]{4,}", str(plan.get("summary", "")).casefold()))
        all_cards = [(card, column.get("title", "")) for column in created for card in column["cards"]]
        target_card = max(
            all_cards,
            key=lambda item: len(
                summary_words
                & set(re.findall(r"[^\W_]{4,}", f"{item[1]} {item[0]['title']}".casefold()))
            ),
            default=(None, ""),
        )[0]
        if target_card and (resource_images or resource_links):
            card_id = target_card["id"]
            for image in resource_images:
                title = str(image.get("title") or "Wikimedia Commons image")
                element_id = None
                try:
                    element = api_json_request(
                        base_url,
                        token,
                        f"/cards/{quote(card_id, safe='')}/elements",
                        method="POST",
                        body={"type": "file"},
                    )
                    element_id = element.get("id")
                    if not element_id:
                        raise ValueError("The server created an image element but returned no ID.")
                    artist = str(image.get("artist") or "Author not listed")
                    license_name = str(image.get("license") or "License not listed")
                    source_url = str(image["sourceUrl"])
                    license_url = str(image.get("licenseUrl") or "")
                    license_detail = f" ({license_url})" if license_url else ""
                    caption = f"{artist}. Lizenz: {license_name}{license_detail}. Quelle: {source_url}"
                    alt_text = str(image.get("description") or title)
                    api_json_request(
                        base_url,
                        token,
                        f"/elements/{quote(element_id, safe='')}/content",
                        method="PATCH",
                        body={"data": {"type": "file", "content": {"caption": caption, "alternativeText": alt_text}}},
                    )
                    upload_urls = [str(image["url"])]
                    original_url = image.get("originalUrl")
                    if (
                        isinstance(original_url, str)
                        and urlsplit(original_url).hostname == "upload.wikimedia.org"
                        and original_url not in upload_urls
                    ):
                        upload_urls.append(original_url)
                    upload_error = None
                    file_record = None
                    for upload_url in upload_urls:
                        file_name = unquote(urlsplit(upload_url).path.rsplit("/", 1)[-1]) or "wikimedia-image.jpg"
                        try:
                            file_record = upload_file_from_url(
                                base_url,
                                token,
                                school_id,
                                element_id,
                                upload_url,
                                file_name,
                                user_agent="Embed Request User Agent",
                            )
                            break
                        except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError) as error:
                            upload_error = error
                            # Commons occasionally advertises a thumbnail that is not
                            # fetchable yet; retry the original file once on a 404.
                            if "HTTP 404" not in str(error) or upload_url == upload_urls[-1]:
                                attempts = "; ".join(upload_urls[: upload_urls.index(upload_url) + 1])
                                raise ValueError(f"Source image URL(s) tried: {attempts}. Last error: {error}") from error
                    if not file_record:
                        raise ValueError(f"File Storage did not attach the image: {upload_error}")
                    target_card.setdefault("assets", []).append(
                        {
                            "type": "image",
                            "title": title,
                            "sourceUrl": source_url,
                            "license": license_name,
                            "previewStatus": file_record.get("previewStatus", "unknown"),
                        }
                    )
                except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError) as error:
                    target_card.setdefault("mediaErrors", []).append(_board_media_error("Image upload", title, error))
                    if element_id:
                        try:
                            api_json_request(
                                base_url,
                                token,
                                f"/elements/{quote(element_id, safe='')}",
                                method="DELETE",
                            )
                            target_card.setdefault("mediaErrors", []).append(
                                f"Removed empty image element {element_id} after upload failure."
                            )
                        except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError) as cleanup_error:
                            target_card.setdefault("mediaErrors", []).append(
                                _board_media_error("Empty image element cleanup", element_id, cleanup_error)
                            )
            for article in resource_links:
                title = str(article.get("title") or article.get("url") or "Wikipedia source")
                try:
                    element = api_json_request(
                        base_url,
                        token,
                        f"/cards/{quote(card_id, safe='')}/elements",
                        method="POST",
                        body={"type": "link"},
                    )
                    element_id = element.get("id")
                    if not element_id:
                        raise ValueError("The server created a source link but returned no ID.")
                    metadata = {
                        "url": article["url"],
                        "title": title,
                        "description": "",
                        "originalImageUrl": "",
                        "imageUrl": "",
                    }
                    preview_status = "no-source-image"
                    try:
                        extracted = api_json_request(
                            base_url,
                            token,
                            "/meta-tag-extractor",
                            method="POST",
                            body={"url": article["url"]},
                        )
                        metadata.update(
                            {key: extracted[key] for key in ("title", "description", "originalImageUrl") if extracted.get(key)}
                        )
                        preview_status = "source-image-found" if metadata.get("originalImageUrl") else "no-source-image"
                    except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError) as error:
                        preview_status = "metadata-error"
                        target_card.setdefault("mediaErrors", []).append(
                            _board_media_error("Link metadata extraction", title, error)
                        )
                    original_image = metadata.get("originalImageUrl")
                    if not original_image:
                        try:
                            original_image = get_wikipedia_page_thumbnail(str(article["url"]))
                            if original_image:
                                preview_status = "wikipedia-page-image-found"
                        except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError) as error:
                            target_card.setdefault("mediaErrors", []).append(
                                _board_media_error("Wikipedia page image fallback", title, error)
                            )
                    original_image_parts = urlsplit(original_image) if isinstance(original_image, str) else None
                    if (
                        original_image_parts
                        and original_image_parts.scheme in ("http", "https")
                        and original_image_parts.hostname
                    ):
                        try:
                            image_name = unquote(original_image_parts.path.rsplit("/", 1)[-1]) or "link-preview-image"
                            image_record = upload_file_from_url(
                                base_url,
                                token,
                                school_id,
                                element_id,
                                original_image,
                                image_name,
                                user_agent="Embed Request User Agent",
                            )
                            if image_record.get("previewStatus") == "preview_possible" and image_record.get("url"):
                                # The server only persists relative preview URLs (see
                                # ContentElementUpdateService.updateLinkElement). The UI
                                # applies the same download -> preview transformation.
                                record_url = str(image_record["url"])
                                parsed_record_url = urlsplit(record_url)
                                relative_record_url = parsed_record_url.path
                                if parsed_record_url.query:
                                    relative_record_url += "?" + parsed_record_url.query
                                if relative_record_url.startswith("/"):
                                    metadata["imageUrl"] = relative_record_url.replace("download", "preview", 1) + "?outputFormat=image/webp"
                                    preview_status = "available"
                                else:
                                    preview_status = "invalid-file-storage-url"
                            else:
                                preview_status = str(image_record.get("previewStatus") or "preview-unavailable")
                        except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError) as error:
                            preview_status = "upload-failed"
                            target_card.setdefault("mediaErrors", []).append(
                                _board_media_error("Link preview image upload", title, error)
                            )
                    elif metadata.get("originalImageUrl"):
                        preview_status = "invalid-image-url"
                    api_json_request(
                        base_url,
                        token,
                        f"/elements/{quote(element_id, safe='')}/content",
                        method="PATCH",
                        body={"data": {"type": "link", "content": metadata}},
                    )
                    target_card.setdefault("assets", []).append(
                        {
                            "type": "link",
                            "title": metadata.get("title", title),
                            "url": article["url"],
                            "preview": bool(metadata.get("imageUrl")),
                            "previewStatus": preview_status,
                        }
                    )
                except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError) as error:
                    target_card.setdefault("mediaErrors", []).append(_board_media_error("Link creation", title, error))
    except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError) as error:
        if created:
            created_ids = ", ".join(
                f"column {column['id']}" + (f" / cards {', '.join(card['id'] for card in column['cards'])}" if column["cards"] else "")
                for column in created
            )
            raise ValueError(f"Board writes stopped partway through. Created so far: {created_ids}. No rollback was attempted. Cause: {error}") from error
        raise
    return created


class _RichTextPreviewParser(HTMLParser):
    """Render generated CKEditor HTML as readable terminal text for confirmation."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "li":
            self._newline()
            self.parts.append("• ")
        elif tag in {"p", "h4", "h5", "blockquote", "ul", "ol", "br", "hr"}:
            self._newline()

    def handle_endtag(self, tag: str) -> None:
        if tag in {"p", "h4", "h5", "li", "blockquote", "ul", "ol", "hr"}:
            self._newline()

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def _newline(self) -> None:
        if self.parts and not self.parts[-1].endswith("\n"):
            self.parts.append("\n")

    def text(self) -> str:
        return "".join(self.parts).strip()


def rich_text_preview(markup: str) -> str:
    parser = _RichTextPreviewParser()
    parser.feed(markup)
    return parser.text()


def print_board_plan(plan: dict) -> None:
    print(f"Plan: {plan['summary']}\n")
    for column_index, column in enumerate(plan["columns"], start=1):
        print(f"{column_index}. {column['title']}")
        for card_index, card in enumerate(column["cards"], start=1):
            print(f"   {column_index}.{card_index} {card['title']}")
            preview = rich_text_preview(card["body"])
            print("      " + preview.replace("\n", "\n      "))
    resources = plan.get("resources", {})
    if plan.get("automaticCardColors"):
        print("\nAutomatic card colors by column: " + " → ".join(plan["automaticCardColors"]))
    if resources.get("imageSearchQueries") or resources.get("linkSearchQueries"):
        print("\nModel-proposed media searches:")
        for query in resources.get("imageSearchQueries", []):
            print(f"  Image: {query}")
        for query in resources.get("linkSearchQueries", []):
            print(f"  Wikipedia: {query}")
    if resources.get("images") or resources.get("links"):
        print("\nAutomatically selected media (attached after confirmation):")
        for image in resources.get("images", []):
            print(f"  Image: {image.get('title', '')} — {image.get('license', '')}")
            print(f"    Author: {image.get('artist') or 'Not listed'}")
            print(f"    Source: {image.get('sourceUrl', '')}")
        for article in resources.get("links", []):
            print(f"  Link: {article.get('title', '')} — {article.get('url', '')}")
    elif not resources.get("images") and not resources.get("links"):
        print("\nNo media was selected.")
    print("Media discovery status:")
    for label, status_key, error_key, reason_key in (
        ("Image", "imageSearchStatus", "imageSearchError", "imageSearchReason"),
        ("Links", "linkSearchStatus", "linkSearchError", "linkSearchReason"),
    ):
        status = resources.get(status_key, "not-run")
        detail = resources.get(error_key) or resources.get(reason_key)
        if status in ("no-results", "no-relevant-results"):
            query_key = "imageSearchQueriesTried" if label == "Image" else "linkSearchQueriesTried"
            attempted = resources.get(query_key, [])
            if status == "no-relevant-results":
                count_key = "imageCandidatesChecked" if label == "Image" else "linkCandidatesChecked"
                detail = f"Checked {resources.get(count_key, 0)} results; none matched the lesson topic"
            else:
                detail = "No eligible results found"
            detail += (f"; tried: {' | '.join(attempted)}." if attempted else ".")
        elif status == "found":
            query_key = "imageSearchQueryUsed" if label == "Image" else "linkSearchQueryUsed"
            used_query = resources.get(query_key)
            detail = "Selected for the plan" + (f" (query: {used_query})." if used_query else ".")
        elif not detail and status == "not-run":
            detail = "Discovery did not run."
        print(f"  {label}: {status}" + (f" — {detail}" if detail else ""))
    if plan["notes"]:
        print("\nNotes:")
        for note in plan["notes"]:
            print(f"  - {note}")


def _capability_reason(base_enabled: bool, permitted: bool) -> str:
    if not base_enabled:
        return "Column boards are disabled by deployment configuration."
    if not permitted:
        return "The current user does not have the required board permission."
    return "Enabled by deployment and board permissions."


def _feature_capability_reason(base_and_permission: bool, config: dict, flag: str) -> str:
    if not base_and_permission:
        return "Column board content creation is disabled or not permitted for this user."
    if not config.get(flag):
        return f"Disabled by deployment flag {flag}."
    return "Enabled by deployment and board permissions."


def _video_conference_reason(base_and_permission: bool, config: dict, features: set, allowed: dict) -> str:
    if not base_and_permission:
        return "Column board content creation is disabled or not permitted for this user."
    if not config.get("FEATURE_VIDEOCONFERENCE_ENABLED"):
        return "Disabled by deployment flag FEATURE_VIDEOCONFERENCE_ENABLED."
    if not config.get("FEATURE_COLUMN_BOARD_VIDEOCONFERENCE_ENABLED"):
        return "Disabled by deployment flag FEATURE_COLUMN_BOARD_VIDEOCONFERENCE_ENABLED."
    if "videoconference" not in features:
        return "The board does not have the videoconference feature enabled."
    if not allowed.get("manageVideoConference"):
        return "The current user does not have manageVideoConference permission."
    return "Enabled by deployment, board feature, and board permission."


def create_room_board(base_url: str, token: str, room_id: str, title: str) -> dict:
    """Create a columns board in a room through the authenticated v3 API."""
    clean_title = title.strip()
    if not clean_title:
        raise ValueError("Board name must contain at least one non-space character.")
    if len(clean_title) > 100:
        raise ValueError("Board name must be 100 characters or fewer.")
    bearer_token = token.removeprefix("Bearer ").strip()
    url = api_url(base_url, "/boards", {})
    request = Request(
        url,
        data=json.dumps({
            "title": clean_title,
            "parentId": room_id,
            "parentType": "room",
            "layout": "columns",
        }).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {bearer_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urlopen(request, timeout=30) as response:
        payload = response.read()
    result = json.loads(payload) if payload else {}
    if not isinstance(result, dict):
        raise ValueError("Unexpected API response from board creation: expected an object.")
    return result


def school_id_from_token(token: str) -> str:
    """Read the authenticated user's school ID claim from their JWT payload."""
    jwt = token.removeprefix("Bearer ").strip()
    parts = jwt.split(".")
    if len(parts) != 3:
        raise ValueError("SCHOOLCLOUD_TOKEN is not a JWT with a schoolId claim.")
    try:
        payload_segment = parts[1]
        payload_bytes = base64.urlsafe_b64decode(payload_segment + "=" * (-len(payload_segment) % 4))
        payload = json.loads(payload_bytes)
    except (ValueError, json.JSONDecodeError) as error:
        raise ValueError("Could not read schoolId from SCHOOLCLOUD_TOKEN.") from error
    school_id = payload.get("schoolId") if isinstance(payload, dict) else None
    if not isinstance(school_id, str) or not school_id:
        raise ValueError("SCHOOLCLOUD_TOKEN does not contain a schoolId claim; pass --schoolid.")
    return school_id


def fetch_school_users(
    base_url: str,
    token: str,
    school_id: str,
    user_role: str,
) -> list[dict]:
    """Fetch one role list from the same v3 school endpoint used by the room UI."""
    bearer_token = token.removeprefix("Bearer ").strip()
    headers = {"Authorization": f"Bearer {bearer_token}", "Accept": "application/json"}
    url = api_url(base_url, f"/school/{quote(school_id, safe='')}/{user_role}s", {})
    request = Request(url, headers=headers)
    with urlopen(request, timeout=30) as response:
        payload = json.load(response)
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError(f"Unexpected API response from school {user_role} list: expected a 'data' list.")
    return [{**user, "role": user_role} for user in payload["data"] if isinstance(user, dict)]


def search_school_users(
    base_url: str,
    token: str,
    first_name: str,
    last_name: str,
    school_id: str | None,
    school_name: str | None,
    role: str,
    show_progress: bool = False,
) -> list[dict]:
    """Search room-UI-visible users, across schools when no school ID is supplied."""
    if not first_name.strip() or not last_name.strip():
        raise ValueError("Both first name and last name must contain at least one non-space character.")
    if school_name is not None and not school_name.strip():
        raise ValueError("School name must contain at least one non-space character.")
    own_school_id = school_id_from_token(token)
    if school_id:
        schools = [{"id": school_id}]
        roles_by_school = {school_id: [role] if role != "all" else ["student", "teacher"]}
    elif school_name:
        schools = list_schools(base_url, token, show_progress=show_progress, page_size=1000)
        school_query = school_name.strip().casefold()
        schools = [
            school
            for school in schools
            if school_query in str(school.get("name", "")).casefold()
        ]
        if not schools:
            raise ValueError(f"No schools matched the name {school_name!r}.")
        roles_by_school = {}
        for school in schools:
            matched_school_id = school.get("id") or school.get("_id")
            if role == "all":
                # Match the room UI visibility: teachers can be selected across
                # schools, while students are listed only for the caller's school.
                visible_roles = ["teacher"]
                if matched_school_id == own_school_id:
                    visible_roles.insert(0, "student")
            elif role == "student" and matched_school_id != own_school_id:
                visible_roles = []
            else:
                visible_roles = [role]
            roles_by_school[matched_school_id] = visible_roles
    elif role == "student":
        schools = [{"id": own_school_id}]
        roles_by_school = {own_school_id: ["student"]}
    else:
        schools = list_schools(base_url, token, show_progress=show_progress, page_size=1000)
        school_ids = {school.get("id") or school.get("_id") for school in schools}
        school_ids.discard(None)
        school_ids.add(own_school_id)
        school_by_id = {school.get("id") or school.get("_id"): school for school in schools}
        schools = [school_by_id.get(sid, {"id": sid}) for sid in sorted(school_ids)]
        # The room UI only offers students from the caller's own school. Teachers can
        # be selected from any school returned by the school's teacher endpoint.
        roles_by_school = {
            school.get("id") or school.get("_id"): (["teacher"] + (["student"] if (school.get("id") or school.get("_id")) == own_school_id and role == "all" else []))
            for school in schools
        }

    tasks = [
        (school, user_role)
        for school in schools
        for user_role in roles_by_school.get(school.get("id") or school.get("_id"), [])
    ]
    matches: list[dict] = []
    completed = 0
    if show_progress:
        write_progress(f"Searching users across {len(tasks)} school list(s)...")
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {
            executor.submit(
                fetch_school_users,
                base_url,
                token,
                school.get("id") or school.get("_id"),
                user_role,
            ): school
            for school, user_role in tasks
        }
        for future in as_completed(futures):
            try:
                matches.extend(future.result())
            except Exception:
                for pending in futures:
                    pending.cancel()
                raise
            completed += 1
            if show_progress:
                write_progress(f"Searching users: completed {completed} of {len(tasks)} school list(s)...")

    first_query = first_name.strip().casefold()
    last_query = last_name.strip().casefold()
    results = [
        user
        for user in matches
        if first_query in str(user.get("firstName", "")).casefold()
        and last_query in str(user.get("lastName", "")).casefold()
    ]
    results.sort(key=lambda user: (user.get("lastName", "").casefold(), user.get("firstName", "").casefold(), user.get("role", "")))
    if show_progress:
        write_progress(f"Found {len(results)} matching user(s).", end="\n")
    return results


def add_room_member(base_url: str, token: str, room_id: str, user_ids: list[str]) -> dict:
    """Add one or more users to a room through the authenticated v3 API."""
    bearer_token = token.removeprefix("Bearer ").strip()
    url = api_url(base_url, f"/rooms/{quote(room_id, safe='')}/members/add", {})
    request = Request(
        url,
        data=json.dumps({"userIds": user_ids}).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {bearer_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        method="PATCH",
    )
    with urlopen(request, timeout=30) as response:
        payload = response.read()
    return json.loads(payload) if payload else {}


def print_schools(schools: list[dict], output_format: str) -> None:
    if output_format == "json":
        print(json.dumps(schools, ensure_ascii=False, indent=2))
        return

    if not schools:
        print("No schools found.")
        return
    print(f"{'ID':<25}  Name")
    print(f"{'-' * 25}  {'-' * 40}")
    for school in schools:
        school_id = school.get("_id") or school.get("id") or ""
        print(f"{school_id:<25}  {school.get('name', '')}")


def print_rooms(rooms: list[dict], output_format: str) -> None:
    if output_format == "json":
        print(json.dumps(rooms, ensure_ascii=False, indent=2))
        return

    if not rooms:
        print("No rooms found.")
        return
    print(f"{'ID':<25}  {'Members':>7}  {'Locked':<6}  Name")
    print(f"{'-' * 25}  {'-' * 7}  {'-' * 6}  {'-' * 40}")
    for room in rooms:
        room_id = room.get("id") or room.get("_id") or ""
        members = room.get("totalMembers", "")
        locked = "yes" if room.get("isLocked") else "no"
        print(f"{room_id:<25}  {str(members):>7}  {locked:<6}  {room.get('name', '')}")


def print_boards(boards: list[dict], output_format: str) -> None:
    if output_format == "json":
        print(json.dumps(boards, ensure_ascii=False, indent=2))
        return

    if not boards:
        print("No boards found.")
        return
    print(f"{'ID':<25}  {'Layout':<12}  {'Visible':<7}  Title")
    print(f"{'-' * 25}  {'-' * 12}  {'-' * 7}  {'-' * 40}")
    for board in boards:
        board_id = board.get("id") or board.get("_id") or ""
        visible = "yes" if board.get("isVisible") else "no"
        print(f"{board_id:<25}  {str(board.get('layout', '')):<12}  {visible:<7}  {board.get('title', '')}")


def print_me(info: dict, output_format: str) -> None:
    if output_format == "json":
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return

    user = info.get("user") or {}
    school = info.get("school") or {}
    account = info.get("account") or {}
    roles = info.get("roles") or []
    name = f"{user.get('firstName', '')} {user.get('lastName', '')}".strip() or "(unknown)"
    role_names = ", ".join(str(role.get("name", role.get("id", ""))) for role in roles) or "(none)"
    print(f"User:    {name}")
    print(f"User ID: {user.get('id') or '(unknown)'}")
    print(f"School:  {school.get('name') or '(unknown)'}")
    print(f"School ID: {school.get('id') or '(unknown)'}")
    print(f"Roles:   {role_names}")
    print(f"Language: {info.get('language') or '(unknown)'}")
    print(f"Account ID: {account.get('id') or '(unknown)'}")


def print_board_capabilities(capabilities: dict, output_format: str) -> None:
    if output_format == "json":
        print(json.dumps(capabilities, ensure_ascii=False, indent=2))
        return

    board = capabilities["board"]
    print(f"Board: {board.get('title', '')} ({board.get('id', '')})")
    print(f"Layout: {board.get('layout', '')}; context: {board.get('context', {})}")
    structure = capabilities["structure"]
    print("\nBoard actions:")
    for label, key in (
        ("Create columns", "createColumns"),
        ("Create cards", "createCards"),
        ("Create elements", "createElements"),
        ("Set card colors", "updateCardColor"),
    ):
        print(f"  {label:<22} {'available' if structure.get(key) else 'unavailable'}")
    print("\nContent options:")
    print(f"  {'Option':<27} {'Status':<13} Details")
    print(f"  {'-' * 27} {'-' * 13} {'-' * 64}")
    for element in capabilities["elements"]:
        print(f"  {element['label']:<27} {('available' if element['available'] else 'unavailable'):<13} {element['reason']}")
    print(f"\nPreferred tools: {capabilities['toolDiscoveryStatus']}")
    tools = capabilities.get("preferredTools", [])
    if tools:
        for tool in tools:
            print(f"  - {tool.get('name', '(unnamed)')} (school tool ID: {tool.get('schoolExternalToolId', '')})")
    elif capabilities.get("elements") and any(item["type"] == "externalTool" and item["available"] for item in capabilities["elements"]):
        print("  No preferred tool shortcuts were returned.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="svs", description="Work with SVS through its HTTP APIs.")
    parser.add_argument(
        "--api-url",
        default=os.environ.get("SCHOOLCLOUD_API_URL", "http://localhost:3030/api"),
        help="Backend API base URL (default: SCHOOLCLOUD_API_URL or %(default)s)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    auth_parser = subparsers.add_parser("auth", help="Log in or show the current account")
    auth_parser.add_argument("--username", "--user", help="Username (password is prompted unless injected via environment)")
    auth_parser.add_argument(
        "--print-token",
        action="store_true",
        help="Print the access token to stdout for explicit scripting use",
    )
    me_parser = subparsers.add_parser("me", help="Show the authenticated user's basic account information")
    me_parser.add_argument("--json", action="store_true", help="Print results as JSON")

    schools_parser = subparsers.add_parser("schools", help="List schools")
    schools_parser.add_argument("--json", action="store_true", help="Print results as JSON")

    rooms_parser = subparsers.add_parser("rooms", help="List your rooms")
    rooms_parser.add_argument("--name", help="Filter rooms by a case-insensitive name substring")
    rooms_parser.add_argument("--json", action="store_true", help="Print results as JSON")

    boards_parser = subparsers.add_parser("boards", help="List or search boards in a room")
    boards_parser.add_argument("--roomid", required=True, help="Room ID")
    boards_parser.add_argument("--name", help="Filter boards by a case-insensitive title substring")
    boards_parser.add_argument("--json", action="store_true", help="Print results as JSON")

    board_parser = subparsers.add_parser("board", help="Manage boards")
    board_commands = board_parser.add_subparsers(dest="board_command", required=True)
    board_create_parser = board_commands.add_parser("create", help="Create a columns board in a room")
    board_create_parser.add_argument("--roomid", required=True, help="Room ID")
    board_create_parser.add_argument("--name", required=True, help="Name of the new board")
    board_create_parser.add_argument("--json", action="store_true", help="Print the created board as JSON")
    board_capabilities_parser = board_commands.add_parser("capabilities", help="Show content options available on a board")
    board_capabilities_parser.add_argument("--boardid", required=True, help="Board ID")
    board_capabilities_parser.add_argument("--json", action="store_true", help="Print capabilities as JSON")
    board_link_test_parser = board_commands.add_parser(
        "create-link-card", help="Create a card containing a link element"
    )
    board_link_test_parser.add_argument("--boardid", required=True, help="Board ID")
    board_link_test_parser.add_argument("--url", required=True, help="HTTP or HTTPS URL for the link element")
    board_link_test_parser.add_argument("--columnid", help="Existing column ID (defaults to the board's first column)")
    board_link_test_parser.add_argument("--title", help="Card title (defaults to the URL host)")
    board_file_parser = board_commands.add_parser(
        "create-file-card", help="Create a card with one image file from a URL or local file"
    )
    board_file_parser.add_argument("--boardid", required=True, help="Board ID")
    file_source = board_file_parser.add_mutually_exclusive_group(required=True)
    file_source.add_argument("--url", help="HTTP or HTTPS image URL")
    file_source.add_argument("--file", help="Path to a local image file")
    board_file_parser.add_argument("--columnid", help="Existing column ID (defaults to the board's first column)")
    board_file_parser.add_argument("--title", help="Card title (defaults to the image name or host)")
    board_file_parser.add_argument("--caption", help="File caption; URL sources are used as the default caption")
    board_file_parser.add_argument("--alt-text", help="Accessible alternative text (defaults to the card title)")
    board_content_parser = board_commands.add_parser("create-content", help="Plan and create lesson content with an LLM provider")
    target = board_content_parser.add_mutually_exclusive_group()
    target.add_argument("--boardid", help="Add content to an existing board")
    board_content_parser.add_argument("--roomid", help="Create a new board in this room")
    board_content_parser.add_argument("--name", "--board-name", dest="board_name", help="Name for the new board (requires --roomid)")
    board_content_parser.add_argument("--prompt", required=True, help="Lesson request for the board content")
    board_content_parser.add_argument("--yes", action="store_true", help="Apply the generated plan without interactive confirmation")
    board_content_parser.add_argument("--json", action="store_true", help="Print the generated plan as JSON without applying it")

    room_parser = subparsers.add_parser("room", help="Manage rooms")
    room_commands = room_parser.add_subparsers(dest="room_command", required=True)
    add_member_parser = room_commands.add_parser("add-member", help="Add users to a room")
    add_member_parser.add_argument("--roomid", required=True, help="Room ID")
    add_member_parser.add_argument("--userid", required=True, action="append", help="User ID; repeat for multiple users")

    user_parser = subparsers.add_parser("user", help="Search for users")
    user_commands = user_parser.add_subparsers(dest="user_command", required=True)
    user_search_parser = user_commands.add_parser("search", help="Search school users by first and last name")
    user_search_parser.add_argument("--first-name", required=True, help="First name or part of a first name")
    user_search_parser.add_argument("--last-name", required=True, help="Last name or part of a last name")
    school_scope = user_search_parser.add_mutually_exclusive_group()
    school_scope.add_argument("--schoolid", help="Limit search to a school ID")
    school_scope.add_argument("--school-name", help="Find schools by name first, then search matching schools")
    user_search_parser.add_argument("--role", choices=("student", "teacher", "all"), default="all")
    user_search_parser.add_argument("--json", action="store_true", help="Print results as JSON")

    school_parser = subparsers.add_parser("school", help="Inspect and administer a school")
    school_commands = school_parser.add_subparsers(dest="school_command", required=True)
    school_systems_parser = school_commands.add_parser("systems", help="List identity systems attached to a school")
    school_systems_parser.add_argument("--schoolid", help="School ID (defaults to the JWT's school)")
    school_systems_parser.add_argument("--json", action="store_true", help="Print results as JSON")
    school_options_parser = school_commands.add_parser(
        "system-options", help="Read or update moin.schule provisioning settings"
    )
    school_options_commands = school_options_parser.add_subparsers(dest="school_options_command", required=True)
    school_options_get_parser = school_options_commands.add_parser("get", help="Show current provisioning settings")
    school_options_get_parser.add_argument("--schoolid", help="School ID (defaults to the JWT's school)")
    school_options_get_parser.add_argument("--systemid", help="System ID (auto-detect moin.schule if unique)")
    school_options_get_parser.add_argument("--json", action="store_true", help="Print results as JSON")
    school_options_set_parser = school_options_commands.add_parser("set", help="Update provisioning settings")
    school_options_set_parser.add_argument("--schoolid", help="School ID (defaults to the JWT's school)")
    school_options_set_parser.add_argument("--systemid", help="System ID (auto-detect moin.schule if unique)")
    school_options_set_parser.add_argument(
        "--classes", choices=("enabled", "disabled"), help="Enable or disable class provisioning"
    )
    school_options_set_parser.add_argument(
        "--courses", choices=("enabled", "disabled"), help="Enable or disable course provisioning"
    )
    school_options_set_parser.add_argument(
        "--other-groups", choices=("enabled", "disabled"), help="Enable or disable other-group provisioning"
    )
    school_options_set_parser.add_argument(
        "--external-tools", choices=("enabled", "disabled"), help="Enable or disable external-tool provisioning"
    )
    school_options_set_parser.add_argument("--yes", action="store_true", help="Apply without interactive confirmation")
    school_options_set_parser.add_argument("--json", action="store_true", help="Print updated settings as JSON")

    args = parser.parse_args(argv)
    try:
        token = (
            get_admin_token(args.api_url, prompt=True)
            if args.command == "school"
            else get_schoolcloud_token(
                args.api_url,
                username=getattr(args, "username", None),
                prompt=True,
            )
        )
        if args.command == "auth":
            info = get_me(args.api_url, token)
            if args.print_token:
                print(token.removeprefix("Bearer ").strip())
            else:
                print("Authenticated successfully.")
                print_me(info, "table")
        elif args.command == "school" and args.school_command == "systems":
            school_id = args.schoolid or school_id_from_token(token)
            systems = get_school_systems(args.api_url, token, school_id)
            print_school_systems(systems, "json" if args.json else "table")
        elif args.command == "school" and args.school_command == "system-options":
            school_id, system = resolve_school_system(args.api_url, token, args.schoolid, args.systemid)
            system_id = str(system.get("id", ""))
            if args.school_options_command == "get":
                options = get_school_system_options(args.api_url, token, school_id, system_id)
                print_school_system_options(options, "json" if args.json else "table")
            else:
                requested = {
                    "groupProvisioningClassesEnabled": args.classes,
                    "groupProvisioningCoursesEnabled": args.courses,
                    "groupProvisioningOtherEnabled": args.other_groups,
                    "schoolExternalToolProvisioningEnabled": args.external_tools,
                }
                supplied = {key: value for key, value in requested.items() if value is not None}
                if not supplied:
                    raise ValueError("Set at least one option with --classes, --courses, --other-groups, or --external-tools.")
                try:
                    current = get_school_system_options(args.api_url, token, school_id, system_id)
                except HTTPError as error:
                    if error.code != 404:
                        raise
                    error.close()
                    # These are the defaults used by the UI and the server's SchulConneX options class.
                    current = dict(SCHULCONNEX_OPTION_DEFAULTS)
                for key, value in current.items():
                    if key not in SCHULCONNEX_OPTION_DEFAULTS or not isinstance(value, bool):
                        raise ValueError(f"Unexpected provisioning option returned by the server: {key}")
                target = dict(SCHULCONNEX_OPTION_DEFAULTS)
                target.update(current)
                target.update({key: _enabled_value(value) for key, value in supplied.items()})
                if target == current:
                    if args.json:
                        print_school_system_options(current, "json")
                    else:
                        print("No changes requested; provisioning settings are already at those values.")
                else:
                    deleted_groups = [
                        label
                        for key, label in (
                            ("groupProvisioningClassesEnabled", "classes"),
                            ("groupProvisioningCoursesEnabled", "courses"),
                            ("groupProvisioningOtherEnabled", "other groups"),
                        )
                        if current.get(key, SCHULCONNEX_OPTION_DEFAULTS[key]) and not target[key]
                    ]
                    if deleted_groups:
                        print(
                            "Warning: disabling provisioning will delete existing "
                            + ", ".join(deleted_groups)
                            + " groups for this school and system.",
                            file=sys.stderr,
                        )
                    if not args.yes:
                        print(f"School {school_id}, system {system_id} ({system.get('alias', '')})", file=sys.stderr)
                        print("Current settings:", file=sys.stderr)
                        print_school_system_options(current, "table")
                        print("Requested settings:", file=sys.stderr)
                        print_school_system_options(target, "table")
                        if not sys.stdin.isatty():
                            raise ValueError("No settings were changed. Rerun interactively or pass --yes to apply.")
                        try:
                            confirmed = input("Apply these school system settings? [y/N] ").strip().casefold() in ("y", "yes")
                        except EOFError:
                            confirmed = False
                        if not confirmed:
                            print("Cancelled; no settings were changed.")
                            return 0
                    set_school_system_options(args.api_url, token, school_id, system_id, target)
                    verified = get_school_system_options(args.api_url, token, school_id, system_id)
                    if any(verified.get(key) != value for key, value in target.items()):
                        raise ValueError("Settings were submitted, but read-back verification did not match the requested values.")
                    if args.json:
                        print_school_system_options(verified, "json")
                    else:
                        print(f"Updated system options for school {school_id}, system {system_id}.")
                        print_school_system_options(verified, "table")
        elif args.command == "me":
            info = get_me(args.api_url, token)
            print_me(info, "json" if args.json else "table")
        elif args.command == "schools":
            schools = list_schools(args.api_url, token, show_progress=not args.json)
            print_schools(schools, "json" if args.json else "table")
        elif args.command == "rooms":
            name_query = None
            if args.name is not None:
                name_query = args.name.strip().casefold()
                if not name_query:
                    raise ValueError("Room name filter must contain at least one non-space character.")
            rooms = list_rooms(args.api_url, token)
            if name_query is not None:
                rooms = [
                    room for room in rooms
                    if name_query in str(room.get("name", "")).casefold()
                ]
            print_rooms(rooms, "json" if args.json else "table")
        elif args.command == "boards":
            name_query = None
            if args.name is not None:
                name_query = args.name.strip().casefold()
                if not name_query:
                    raise ValueError("Board name filter must contain at least one non-space character.")
            boards = list_room_boards(args.api_url, token, args.roomid)
            if name_query is not None:
                boards = [
                    board for board in boards
                    if name_query in str(board.get("title", "")).casefold()
                ]
            print_boards(boards, "json" if args.json else "table")
        elif args.command == "board" and args.board_command == "create":
            result = create_room_board(args.api_url, token, args.roomid, args.name)
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                board_id = result.get("id") or result.get("_id") or ""
                print(f"Created board {args.name.strip()} in room {args.roomid} (ID: {board_id}).")
        elif args.command == "board" and args.board_command == "capabilities":
            capabilities = get_board_capabilities(args.api_url, token, args.boardid)
            print_board_capabilities(capabilities, "json" if args.json else "table")
        elif args.command == "board" and args.board_command == "create-link-card":
            create_board_link_card(
                args.api_url, token, args.boardid, args.url, args.columnid, args.title
            )
        elif args.command == "board" and args.board_command == "create-file-card":
            create_board_file_card(
                args.api_url,
                token,
                args.boardid,
                source_url=args.url,
                local_file=args.file,
                column_id=args.columnid,
                card_title=args.title,
                caption=args.caption,
                alternative_text=args.alt_text,
            )
        elif args.command == "board" and args.board_command == "create-content":
            creating_board = not args.boardid
            if creating_board and (not args.roomid or not args.board_name):
                raise ValueError("Provide either --boardid for an existing board or both --roomid and --name for a new board.")
            if args.boardid and (args.roomid or args.board_name):
                raise ValueError("Use --boardid alone, or use --roomid with --name to create a board.")
            if creating_board and args.json:
                raise ValueError("--json is plan-only and requires --boardid; use a named board to create and apply content.")
            llm_url = os.environ.get("LLM_API_URL", "").strip()
            llm_token = (os.environ.get("LLM_API_TOKEN") or os.environ.get("LLM_API_KEY") or "").strip()
            llm_key_header = os.environ.get("LLM_API_KEY_HEADER", "Authorization").strip()
            llm_model = os.environ.get("LLM_MODEL", "").strip()
            if not llm_url:
                raise ValueError("Set LLM_API_URL to a Chat Completions-compatible endpoint.")
            if not llm_model:
                raise ValueError("Set LLM_MODEL to the model name required by your provider.")
            if not llm_key_header or any(char in llm_key_header for char in "\r\n:"):
                raise ValueError("LLM_API_KEY_HEADER must be a valid HTTP header name.")
            endpoint = urlsplit(llm_url)
            if endpoint.scheme not in ("http", "https") or not endpoint.netloc:
                raise ValueError("LLM endpoint URL must be an absolute http:// or https:// URL.")
            if endpoint.scheme != "https" and endpoint.hostname not in ("localhost", "127.0.0.1", "::1"):
                raise ValueError("Use HTTPS for remote LLM providers; plain HTTP is allowed only for localhost.")
            if args.json and args.yes:
                raise ValueError("--json prints a plan only; omit --yes and rerun without --json to apply interactively.")
            if not args.json:
                print("Checking board permissions and available features, then generating a plan...", file=sys.stderr)
            board_id = args.boardid
            if creating_board:
                new_board = create_room_board(args.api_url, token, args.roomid, args.board_name)
                board_id = str(new_board.get("id") or new_board.get("_id") or "")
                if not board_id:
                    raise ValueError("The server created the board but returned no board ID.")
                print(f"Created board '{args.board_name.strip()}' (ID: {board_id}).", file=sys.stderr)
            capabilities = get_board_capabilities(args.api_url, token, board_id)
            board = get_api_json(args.api_url, token, f"/boards/{quote(board_id, safe='')}")
            structure = capabilities.get("structure", {})
            if board.get("layout") != "columns":
                raise ValueError("Prompt-driven content currently supports columns boards only.")
            for operation, label in (("createColumns", "create columns"), ("createCards", "create cards"), ("createElements", "create text elements")):
                if not structure.get(operation):
                    raise ValueError(f"The current user cannot {label} on this board.")
            rich_text = next((item for item in capabilities.get("elements", []) if item.get("type") == "richText"), None)
            if not rich_text or not rich_text.get("available"):
                raise ValueError("Rich-text elements are not available for this board and user.")
            plan = call_llm_for_board_plan(
                llm_url,
                llm_token or None,
                llm_model,
                args.prompt,
                board,
                capabilities,
                llm_key_header,
            )
            plan["resources"] = discover_board_resources(plan["mediaSearch"], capabilities)
            if structure.get("updateCardColor"):
                palette = ("lightBlue", "amber", "lightGreen", "deepPurple")
                plan["automaticCardColors"] = [palette[index % len(palette)] for index in range(len(plan["columns"]))]
            if args.json:
                print(json.dumps(plan, ensure_ascii=False, indent=2))
            else:
                print_board_plan(plan)
                should_apply = args.yes
                if not should_apply and sys.stdin.isatty():
                    try:
                        should_apply = input("\nApply this plan to the board? [y/N] ").strip().casefold() in ("y", "yes")
                    except EOFError:
                        should_apply = False
                if not should_apply:
                    print("\nPlan only; no SchoolCloud content was created. Use --yes to apply non-interactively.")
                else:
                    has_media = bool(plan["resources"].get("images") or plan["resources"].get("links"))
                    school_id = get_board_school_id(args.api_url, token, capabilities) if has_media else ""
                    created = create_board_content(args.api_url, token, board_id, plan, capabilities, school_id)
                    updated_board = get_api_json(args.api_url, token, f"/boards/{quote(board_id, safe='')}")
                    existing_column_ids = {column.get("id") for column in updated_board.get("columns", [])}
                    missing_columns = [item["id"] for item in created if item["id"] not in existing_column_ids]
                    if missing_columns:
                        raise ValueError(f"Writes completed, but verification did not find created columns: {', '.join(missing_columns)}")
                    existing_card_ids = {
                        card.get("cardId") or card.get("id")
                        for column in updated_board.get("columns", [])
                        for card in column.get("cards", [])
                    }
                    expected_card_ids = [card["id"] for column in created for card in column["cards"]]
                    missing_cards = [card_id for card_id in expected_card_ids if card_id not in existing_card_ids]
                    if missing_cards:
                        raise ValueError(f"Writes completed, but verification did not find created cards: {', '.join(missing_cards)}")
                    assets = [asset for column in created for card in column["cards"] for asset in card.get("assets", [])]
                    image_count = sum(asset.get("type") == "image" for asset in assets)
                    link_count = sum(asset.get("type") == "link" for asset in assets)
                    color_count = sum(len(column["cards"]) for column in created) if structure.get("updateCardColor") else 0
                    print(
                        f"\nCreated {len(created)} column(s), {len(expected_card_ids)} card(s), "
                        f"and {len(expected_card_ids)} rich-text element(s); uploaded {image_count} image(s), "
                        f"added {link_count} link(s), "
                        f"and colored {color_count} card(s)."
                    )
                    for column in created:
                        print(f"  {column['title']} (column {column['id']})")
                        for card in column["cards"]:
                            for asset in card.get("assets", []):
                                extra = f" ({asset['license']})" if asset.get("license") else ""
                                if asset.get("type") == "link":
                                    extra += f" (preview: {asset.get('previewStatus', 'unknown')})"
                                print(f"    {asset['type']}: {asset['title']}{extra}")
                            for media_error in card.get("mediaErrors", []):
                                print(f"    Media warning: {media_error}", file=sys.stderr)
        elif args.command == "room" and args.room_command == "add-member":
            result = add_room_member(args.api_url, token, args.roomid, args.userid)
            if result:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                print(f"Added {len(args.userid)} member(s) to room {args.roomid}.")
        elif args.command == "user" and args.user_command == "search":
            users = search_school_users(
                args.api_url,
                token,
                args.first_name,
                args.last_name,
                args.schoolid,
                args.school_name,
                args.role,
                show_progress=not args.json,
            )
            if args.json:
                print(json.dumps(users, ensure_ascii=False, indent=2))
            elif not users:
                print("No matching users found.")
            else:
                print(f"{'ID':<25}  {'Role':<8}  {'Name':<32}  School")
                print(f"{'-' * 25}  {'-' * 8}  {'-' * 32}  {'-' * 40}")
                for user in users:
                    user_id = user.get("id") or user.get("_id") or ""
                    name = f"{user.get('firstName', '')} {user.get('lastName', '')}".strip()
                    print(f"{user_id:<25}  {user.get('role', ''):<8}  {name:<32}  {user.get('schoolName', '')}")
    except HTTPError as error:
        if args.command in ("schools", "user") and not args.json:
            write_progress("")
        detail = error.read().decode("utf-8", errors="replace").strip()
        if args.command == "board" and getattr(args, "board_command", None) == "create-content":
            print(f"Remote API request failed ({error.code}): {detail or error.reason}", file=sys.stderr)
        else:
            print(f"API request failed ({error.code}): {detail or error.reason}", file=sys.stderr)
        return 1
    except (URLError, TimeoutError, ValueError, json.JSONDecodeError) as error:
        if args.command in ("schools", "user") and not args.json:
            write_progress("")
        if args.command == "board" and getattr(args, "board_command", None) == "create-content":
            print(f"Could not generate or apply board content: {error}", file=sys.stderr)
        else:
            print(f"Could not fetch or search SchoolCloud data: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
