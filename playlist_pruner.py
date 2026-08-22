#!/usr/bin/env python3
"""Back up Spotify playlists and prune entries added before a cutoff date."""

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
    def __init__(self, client_id: str):
        self.client_id = client_id
        self.access_token = self.authorize()

    def authorize(self) -> str:
        verifier = base64url(secrets.token_bytes(64))
        challenge = base64url(hashlib.sha256(verifier.encode()).digest())
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
                    delay = max(1, int(exc.headers.get("Retry-After", "1")))
                    print(f"Spotify rate limit reached; retrying in {delay}s...")
                    time.sleep(delay)
                    continue
                if exc.code >= 500 and attempt < 4:
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
    metadata = client.request("GET", f"/playlists/{playlist_id}")
    name = metadata.get("name") or playlist_id
    snapshot_id = metadata.get("snapshot_id") or ""
    entries: list[PlaylistEntry] = []
    url: str | None = f"{API_BASE}/playlists/{playlist_id}/items?limit=50&additional_types=track,episode"
    position = 0
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
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for entry in entries:
            writer.writerow({field: getattr(entry, field) for field in CSV_FIELDS})


def classify_entries(
    entries: list[PlaylistEntry], cutoff: datetime
) -> tuple[list[PlaylistEntry], list[PlaylistEntry], list[PlaylistEntry]]:
    by_uri: dict[str, list[PlaylistEntry]] = {}
    unknown: list[PlaylistEntry] = []
    for entry in entries:
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

    backup_path = output_dir / f"playlist-backup-{timestamp}.csv"
    candidate_path = output_dir / f"prune-candidates-{timestamp}.csv"
    write_csv(backup_path, all_entries)
    candidates = [entry for *_rest, entries in plans for entry in entries]
    write_csv(candidate_path, candidates)
    print(f"\nFull backup: {backup_path.resolve()}")
    print(f"Candidates:  {candidate_path.resolve()}")
    print(f"Cutoff: items added before {cutoff.date().isoformat()} (UTC)")
    print(f"Total eligible occurrences: {len(candidates)}")

    if not args.confirm:
        print("\nDRY RUN ONLY — no Spotify playlists were changed.")
        print("Review the CSV files, then rerun with --confirm to enable removal.")
        return 0
    if not candidates:
        print("\nNothing to remove.")
        return 0
    answer = input(f'\nType DELETE {len(candidates)} to permanently remove these entries: ').strip()
    if answer != f"DELETE {len(candidates)}":
        print("Confirmation did not match. No Spotify playlists were changed.")
        return 0

    removed: list[PlaylistEntry] = []
    for playlist_id, name, snapshot_id, entries in plans:
        uris = list(dict.fromkeys(entry.uri for entry in entries if entry.uri))
        current_snapshot = snapshot_id
        for batch in chunks(uris, 100):
            body: dict[str, Any] = {"items": [{"uri": uri} for uri in batch]}
            if current_snapshot:
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
