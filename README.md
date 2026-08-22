# Playlist Pruner

[![Tests](https://github.com/jtdelosh-ops/playlist-pruner/actions/workflows/tests.yml/badge.svg)](https://github.com/jtdelosh-ops/playlist-pruner/actions/workflows/tests.yml)

Playlist Pruner backs up one or more Spotify playlists to CSV and finds entries
added before a cutoff date. It is read-only unless you explicitly pass
`--confirm` and type a matching confirmation phrase.

It downloads playlist metadata only—not music or audio.

> [!NOTE]
> Playlist Pruner is an independent project and is not affiliated with,
> endorsed by, or sponsored by Spotify. Use of the Spotify Web API is subject
> to Spotify's [Developer Terms](https://developer.spotify.com/terms) and
> [Developer Policy](https://developer.spotify.com/policy).

## Requirements

- Python 3.10 or newer
- Spotify Premium
- A Spotify developer app with this exact redirect URI:
  `http://127.0.0.1:8888/callback`

The app requests these Spotify permissions:

- `playlist-read-private`
- `playlist-modify-private`
- `playlist-modify-public`

No Client Secret is needed; authentication uses Authorization Code with PKCE.

## Features

- Backs up full playlists and removal candidates as Excel-friendly CSV files
- Defaults to a read-only dry run
- Requires both `--confirm` and a typed phrase before modifying Spotify
- Accepts playlist URLs, Spotify URIs, IDs, or a text file of playlist links
- Protects newer duplicate occurrences from URI-based deletion
- Uses only the Python standard library

## Setup

Open PowerShell in this folder and make a local environment file:

```powershell
Copy-Item .env.example .env
notepad .env
```

Replace the placeholder with the Client ID shown in your Spotify app settings.

## Dry run

For one playlist:

```powershell
python playlist_pruner.py --playlist "SPOTIFY_PLAYLIST_LINK" --before 2020-01-01
```

For several playlists, repeat `--playlist`:

```powershell
python playlist_pruner.py `
  --playlist "FIRST_PLAYLIST_LINK" `
  --playlist "SECOND_PLAYLIST_LINK" `
  --before 2020-01-01
```

Alternatively, put one playlist link per line in `playlists.txt`:

```powershell
python playlist_pruner.py --playlist-file playlists.txt --before 2020-01-01
```

The browser opens for Spotify authorization. The program then creates two CSVs
under `backups/`: a complete backup and a list of removal candidates. No playlist
is changed during a dry run.

## Remove the reviewed candidates

Run the same command with `--confirm`:

```powershell
python playlist_pruner.py --playlist "SPOTIFY_PLAYLIST_LINK" --before 2020-01-01 --confirm
```

The program creates fresh backup and candidate CSV files before asking you to
type a confirmation phrase. After removal, it writes an additional removal log.

## Safety behavior

- Entries whose `added_at` date is missing are skipped.
- Local or unavailable items are skipped.
- Spotify removes playlist items by URI. If a URI occurs both before and after
  the cutoff, its older occurrences are skipped to protect the newer ones.
- Spotify playlist folders are not visible through the API, so supply every
  playlist link from a folder individually.
- Playlist entries are selected by when they were added to that playlist, not by
  the track or album release date.

## Troubleshooting

- **INVALID_CLIENT or invalid redirect URI:** confirm the Client ID and ensure
  `http://127.0.0.1:8888/callback` is saved exactly in the Spotify dashboard.
- **Port 8888 is already in use:** close the other local program and retry.
- **403 Forbidden:** the playlist must be owned by you or list you as a
  collaborator under Spotify's current Development Mode API restrictions.
- **Browser does not open:** copy the authorization URL printed by the program.

## Development

Run the test suite with:

```powershell
python -m unittest -v
```

## License

MIT

