#!/usr/bin/env python3
"""Back up Spotify playlists and prune entries added before a cutoff date.

The program is intentionally read-only by default. A user must pass
``--confirm`` and then type a matching confirmation phrase before any playlist
items are removed.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import json
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass
from datetime import date, datetime, time as datetime_time, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Iterable


API_BASE = "https://api.spotify.com/v1"
AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
REDIRECT_URI = "http://127.0.0.1:8888/callback"
SCOPES = "playlist-read-private playlist-modify-private playlist-modify-public"
PLAYLIST_ID_RE = re.compile(r"^[A-Za-z0-9]{10,}$")


class PrunerError(RuntimeError):
    pass


@dataclass
class PlaylistEntry:
    """A normalized playlist row used by the CSV and pruning code.

    Spotify may return tracks, episodes, local files, or unavailable items.
    Normalizing them here lets the rest of the program handle one predictable
    shape instead of repeatedly inspecting Spotify's nested JSON response.
    """

    playlist_id: str
    playlist_name: str
    position: int
    added_at: str | None
    uri: str | None
    item_type: str
    name: str
    artists: str
    album: str
    spotify_url: str
    is_local: bool


class CallbackHandler(BaseHTTPRequestHandler):
    """Receive Spotify's one-time OAuth response on this computer."""

    result: dict[str, str] = {}

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/callback":
            self.send_error(404)
            return
        values = urllib.parse.parse_qs(parsed.query)
        CallbackHandler.result = {key: value[0] for key, value in values.items() if value}
        message = (
            "Spotify authorization received. You can close this tab and return "
            "to Playlist Pruner."
        )
        body = f"<!doctype html><title>Playlist Pruner</title><h2>{message}</h2>".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def base64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def load_dotenv(path: Path) -> None:
    """Load simple KEY=VALUE settings without requiring a third-party package."""

    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def parse_playlist_id(value: str) -> str:
    """Accept a Spotify URL, URI, or raw ID and return the playlist ID."""

    value = value.strip()
    if not value:
        raise argparse.ArgumentTypeError("playlist value cannot be empty")
    if value.startswith("spotify:playlist:"):
        candidate = value.rsplit(":", 1)[-1]
    elif "open.spotify.com" in value:
        parsed = urllib.parse.urlparse(value if "://" in value else "https://" + value)
        parts = [part for part in parsed.path.split("/") if part]
        try:
            candidate = parts[parts.index("playlist") + 1]
        except (ValueError, IndexError) as exc:
            raise argparse.ArgumentTypeError(f"not a Spotify playlist URL: {value}") from exc
    else:
        candidate = value
    if not PLAYLIST_ID_RE.fullmatch(candidate):
        raise argparse.ArgumentTypeError(f"invalid Spotify playlist ID: {candidate}")
    return candidate


def parse_cutoff(value: str) -> datetime:
    """Convert YYYY-MM-DD to midnight UTC for consistent comparisons."""

    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD format") from exc
    return datetime.combine(parsed, datetime_time.min, tzinfo=timezone.utc)


def parse_added_at(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def chunks(values: list[str], size: int) -> Iterable[list[str]]:
    for index in range(0, len(values), size):
        yield values[index : index + size]


class SpotifyClient:
    """Small Spotify Web API client with authentication and retry handling."""

    def __init__(self, client_id: str):
        self.client_id = client_id
        self.access_token = self.authorize()

    def authorize(self) -> str:
        """Authorize with PKCE and return a short-lived Spotify access token."""

        # PKCE proves that the program exchanging the authorization code is the
        # same program that started login. It avoids storing a Client Secret.
        verifier = base64url(secrets.token_bytes(64))
        challenge = base64url(hashlib.sha256(verifier.encode()).digest())

        # Spotify sends this value back unchanged. Comparing it below protects
        # the local callback from accepting a response started by someone else.
        state = secrets.token_urlsafe(24)
        params = {
            "client_id": self.client_id,
            "response_type": "code",
            "redirect_uri": REDIRECT_URI,
            "scope": SCOPES,
            "state": state,
            "code_challenge_method": "S256",
            "code_challenge": challenge,
        }
        url = AUTHORIZE_URL + "?" + urllib.parse.urlencode(params)
        CallbackHandler.result = {}
        # The local server receives exactly one browser redirect from Spotify.
        # 127.0.0.1 never leaves this computer.
        try:
            server = HTTPServer(("127.0.0.1", 8888), CallbackHandler)
        except OSError as exc:
            raise PrunerError("cannot listen on 127.0.0.1:8888; another program may use it") from exc
        server.timeout = 180
        print("Opening Spotify authorization in your browser...")
        if not webbrowser.open(url):
            print(f"Open this URL manually:\n{url}\n")
        server.handle_request()
        server.server_close()
        result = CallbackHandler.result
        if not result:
            raise PrunerError("authorization timed out after 3 minutes")
        if result.get("state") != state:
            raise PrunerError("authorization state did not match; please try again")
        if "error" in result:
            raise PrunerError(f"Spotify authorization failed: {result['error']}")
        code = result.get("code")
        if not code:
            raise PrunerError("Spotify did not return an authorization code")
        # Exchange the temporary authorization code for the access token used
        # in subsequent API requests.
        payload = urllib.parse.urlencode(
            {
                "client_id": self.client_id,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT_URI,
                "code_verifier": verifier,
            }
        ).encode()
        request = urllib.request.Request(
            TOKEN_URL,
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                token = json.load(response)
        except urllib.error.HTTPError as exc:
            raise PrunerError(f"token exchange failed: {read_http_error(exc)}") from exc
        return token["access_token"]

    def request(
        self,
        method: str,
        path_or_url: str,
        *,
        query: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Make an authenticated API request and retry temporary failures."""

        url = path_or_url if path_or_url.startswith("http") else API_BASE + path_or_url
        if query:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(query)
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Authorization": f"Bearer {self.access_token}"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        for attempt in range(5):
            request = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    raw = response.read()
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and attempt < 4:
                    # Spotify tells clients how long to wait after rate limits.
                    delay = max(1, int(exc.headers.get("Retry-After", "1")))
                    print(f"Spotify rate limit reached; retrying in {delay}s...")
                    time.sleep(delay)
                    continue
                if exc.code >= 500 and attempt < 4:
                    # Server errors are often temporary, so wait progressively
                    # longer before each retry (1, 2, 4, then 8 seconds).
                    time.sleep(2**attempt)
                    continue
                raise PrunerError(f"Spotify API {method} {url} failed: {read_http_error(exc)}") from exc
            except urllib.error.URLError as exc:
                if attempt < 4:
                    time.sleep(2**attempt)
                    continue
                raise PrunerError(f"network request failed: {exc.reason}") from exc
        raise PrunerError("Spotify request failed after retries")


def read_http_error(exc: urllib.error.HTTPError) -> str:
    try:
        detail = exc.read().decode("utf-8", errors="replace")
    except Exception:
        detail = ""
    return f"HTTP {exc.code} {detail}".strip()


def get_playlist(client: SpotifyClient, playlist_id: str) -> tuple[str, str, list[PlaylistEntry]]:
    """Download and normalize every item in one playlist."""

    metadata = client.request("GET", f"/playlists/{playlist_id}")
    name = metadata.get("name") or playlist_id
    snapshot_id = metadata.get("snapshot_id") or ""
    entries: list[PlaylistEntry] = []
    url: str | None = f"{API_BASE}/playlists/{playlist_id}/items?limit=50&additional_types=track,episode"
    position = 0
    # Spotify returns no more than 50 playlist items per page. Its ``next`` URL
    # points to the following page and becomes null after the final page.
    while url:
        page = client.request("GET", url)
        for wrapped in page.get("items", []):
            item = wrapped.get("item") or wrapped.get("track") or {}
            artists = "; ".join(a.get("name", "") for a in item.get("artists", []))
            album = (item.get("album") or {}).get("name", "")
            entries.append(
                PlaylistEntry(
                    playlist_id=playlist_id,
                    playlist_name=name,
                    position=position,
                    added_at=wrapped.get("added_at"),
                    uri=item.get("uri"),
                    item_type=item.get("type", "unknown"),
                    name=item.get("name", "Unavailable item"),
                    artists=artists,
                    album=album,
                    spotify_url=(item.get("external_urls") or {}).get("spotify", ""),
                    is_local=bool(wrapped.get("is_local") or item.get("is_local")),
                )
            )
            position += 1
        url = page.get("next")
    return name, snapshot_id, entries


CSV_FIELDS = [
    "playlist_name",
    "playlist_id",
    "position",
    "added_at",
    "item_type",
    "name",
    "artists",
    "album",
    "uri",
    "spotify_url",
    "is_local",
]


def write_csv(path: Path, entries: Iterable[PlaylistEntry]) -> None:
    """Write playlist rows as a UTF-8 CSV that also opens cleanly in Excel."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for entry in entries:
            writer.writerow({field: getattr(entry, field) for field in CSV_FIELDS})


def classify_entries(
    entries: list[PlaylistEntry], cutoff: datetime
) -> tuple[list[PlaylistEntry], list[PlaylistEntry], list[PlaylistEntry]]:
    """Separate removable, ambiguous duplicate, and unsafe-to-remove entries.

    Spotify's removal endpoint works by URI rather than by exact playlist row.
    Removing a URI can therefore remove duplicate occurrences. If a URI exists
    both before and after the cutoff, the older occurrence is marked ambiguous
    and skipped so the newer occurrence is not removed accidentally.
    """

    by_uri: dict[str, list[PlaylistEntry]] = {}
    unknown: list[PlaylistEntry] = []
    for entry in entries:
        # Local files have no removable Spotify URI. Missing or malformed dates
        # cannot be compared safely, so those entries are also left untouched.
        if entry.is_local or not entry.uri or parse_added_at(entry.added_at) is None:
            unknown.append(entry)
        else:
            by_uri.setdefault(entry.uri, []).append(entry)

    removable: list[PlaylistEntry] = []
    ambiguous: list[PlaylistEntry] = []
    for occurrences in by_uri.values():
        older = [entry for entry in occurrences if parse_added_at(entry.added_at) < cutoff]  # type: ignore[operator]
        newer = [entry for entry in occurrences if parse_added_at(entry.added_at) >= cutoff]  # type: ignore[operator]
        if older and newer:
            ambiguous.extend(older)
        elif older:
            removable.extend(older)
    return removable, ambiguous, unknown


def collect_playlist_values(args: argparse.Namespace) -> list[str]:
    """Collect, validate, and de-duplicate playlist inputs from both sources."""

    values = list(args.playlist or [])
    if args.playlist_file:
        try:
            lines = Path(args.playlist_file).read_text(encoding="utf-8-sig").splitlines()
        except OSError as exc:
            raise PrunerError(f"cannot read playlist file: {exc}") from exc
        values.extend(line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#"))
    if not values:
        raise PrunerError("provide at least one --playlist or --playlist-file")
    result: list[str] = []
    for value in values:
        try:
            playlist_id = parse_playlist_id(value)
        except argparse.ArgumentTypeError as exc:
            raise PrunerError(str(exc)) from exc
        if playlist_id not in result:
            result.append(playlist_id)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Back up Spotify playlists and remove entries added before a cutoff date."
    )
    parser.add_argument("--playlist", action="append", help="playlist URL, URI, or ID; repeatable")
    parser.add_argument("--playlist-file", help="text file containing one playlist URL/URI/ID per line")
    parser.add_argument("--before", required=True, type=parse_cutoff, metavar="YYYY-MM-DD")
    parser.add_argument("--client-id", help="Spotify client ID (or set SPOTIFY_CLIENT_ID)")
    parser.add_argument("--output-dir", default="backups", help="CSV output directory (default: backups)")
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="enable removal after a typed confirmation; without this flag the run is read-only",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run backup, preview, and optional removal in that safety-first order."""

    load_dotenv(Path(".env"))
    args = build_parser().parse_args(argv)
    client_id = args.client_id or os.environ.get("SPOTIFY_CLIENT_ID")
    if not client_id:
        raise PrunerError("set SPOTIFY_CLIENT_ID in .env or provide --client-id")
    playlist_ids = collect_playlist_values(args)
    client = SpotifyClient(client_id)
    cutoff: datetime = args.before
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output_dir = Path(args.output_dir)
    all_entries: list[PlaylistEntry] = []
    plans: list[tuple[str, str, str, list[PlaylistEntry]]] = []

    for playlist_id in playlist_ids:
        name, snapshot_id, entries = get_playlist(client, playlist_id)
        all_entries.extend(entries)
        removable, ambiguous, unknown = classify_entries(entries, cutoff)
        plans.append((playlist_id, name, snapshot_id, removable))
        print(f"\n{name}: {len(entries)} total, {len(removable)} eligible for removal")
        if ambiguous:
            print(f"  Skipped {len(ambiguous)} older duplicate occurrence(s) also present after cutoff")
        if unknown:
            print(f"  Skipped {len(unknown)} local, unavailable, or undated item(s)")
        for entry in removable[:20]:
            print(f"  {entry.added_at[:10] if entry.added_at else 'unknown'}  {entry.artists} — {entry.name}")
        if len(removable) > 20:
            print(f"  ...and {len(removable) - 20} more (see CSV)")

    # Backups are always written before the program considers changing Spotify.
    backup_path = output_dir / f"playlist-backup-{timestamp}.csv"
    candidate_path = output_dir / f"prune-candidates-{timestamp}.csv"
    write_csv(backup_path, all_entries)
    candidates = [entry for *_rest, entries in plans for entry in entries]
    write_csv(candidate_path, candidates)
    print(f"\nFull backup: {backup_path.resolve()}")
    print(f"Candidates:  {candidate_path.resolve()}")
    print(f"Cutoff: items added before {cutoff.date().isoformat()} (UTC)")
    print(f"Total eligible occurrences: {len(candidates)}")

    # A normal invocation stops here. Merely running the program can never
    # remove playlist items unless --confirm was supplied explicitly.
    if not args.confirm:
        print("\nDRY RUN ONLY — no Spotify playlists were changed.")
        print("Review the CSV files, then rerun with --confirm to enable removal.")
        return 0
    if not candidates:
        print("\nNothing to remove.")
        return 0
    # The typed count makes an accidental Enter or stale command insufficient
    # to authorize a destructive operation.
    answer = input(f'\nType DELETE {len(candidates)} to permanently remove these entries: ').strip()
    if answer != f"DELETE {len(candidates)}":
        print("Confirmation did not match. No Spotify playlists were changed.")
        return 0

    removed: list[PlaylistEntry] = []
    for playlist_id, name, snapshot_id, entries in plans:
        # De-duplicate URIs because Spotify removes every matching occurrence.
        uris = list(dict.fromkeys(entry.uri for entry in entries if entry.uri))
        current_snapshot = snapshot_id
        # Spotify accepts at most 100 playlist items in one removal request.
        for batch in chunks(uris, 100):
            body: dict[str, Any] = {"items": [{"uri": uri} for uri in batch]}
            if current_snapshot:
                # A snapshot anchors the request to the playlist version that
                # we inspected, even if another edit happened afterward.
                body["snapshot_id"] = current_snapshot
            response = client.request("DELETE", f"/playlists/{playlist_id}/items", body=body)
            current_snapshot = response.get("snapshot_id", current_snapshot)
        removed.extend(entries)
        print(f"Removed {len(entries)} occurrence(s) from {name}")

    removal_path = output_dir / f"removed-{timestamp}.csv"
    write_csv(removal_path, removed)
    print(f"Removal record: {removal_path.resolve()}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nCancelled. No further changes were made.", file=sys.stderr)
        raise SystemExit(130)
    except PrunerError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)

