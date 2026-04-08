# halcyon

halcyon is a self-hosted video library built to feel like the old internet again.

The goal is not infinite algorithmic sludge, doomscrolling, or engagement bait. It is a quieter personal media space centered around the creators you actually care about: your subscriptions, your saved channels, your own library, your own pace. The name comes from that feeling on purpose: calm, positive nostalgia, and a return to enjoying media intentionally instead of being dragged around by recommendation engines.

For the best automated setup, pair halcyon with MeTube and point MeTube at the same library folder halcyon watches.

## What halcyon does

- Scans a local media library and builds a YouTube-style interface around it
- Supports multiple local accounts with watch history, queues, playlists, and saved videos
- Matches local files to YouTube metadata when possible
- Auto-organizes matched videos into per-channel folders
- Generates thumbnails and preview clips for hover playback
- Supports retention staging, deletion grace periods, and revert flows
- Provides browser playback plus compatibility/transcode handling when needed

## Who it is for

halcyon is for people who want:

- a personal subscription-first video space
- less algorithmic noise
- more control over their own media
- an interface that feels familiar without the time-wasting parts

## Stack

- FastAPI
- SQLAlchemy
- PostgreSQL
- React
- TypeScript
- Vite
- ffmpeg / ffprobe
- Docker Compose

## Quick start with Docker Compose

halcyon is packaged so the same release folder works on both Windows and Linux with Docker Compose. The included compose file only uses relative bind mounts and container paths, so you can download the package, open the folder, and bring it up directly.

### 1. Get the files in place

1. Download or clone this repository.
2. Open the project folder.
3. Make sure Docker and Docker Compose are installed.

For the easiest update path, clone the repository instead of downloading a one-off zip:

```bash
git clone git@github.com:awpsec/halcyon.git
cd halcyon
```

### 2. Prepare the local folders

halcyon expects these local folders:

- `data/config`
- `data/cache`
- `library`

They are already included in the release package. Put your video library inside `library`, or mount your own media path there in `docker-compose.yml`.

If you use MeTube, point MeTube downloads at that same `library` directory. halcyon will detect new files there, identify the channel, and automatically organize matched videos into per-channel folders.

### 3. Optional: create `.env`

Copy `.env.example` to `.env` if you want to override defaults.

### 4. Start the stack

Run:

```bash
docker compose up --build -d
```

Or use the included helper command:

```bash
./halcyon start
```

On Windows:

```powershell
.\halcyon.ps1 start
```

This starts:

- `halcyon-postgres`
- `halcyon-web`
- `halcyon-worker`

### 5. Open halcyon

Open:

- [http://localhost:11111](http://localhost:11111)

## Helper commands

halcyon ships with small wrapper scripts so common Docker Compose tasks stay simple:

- `halcyon start`
- `halcyon stop`
- `halcyon status`
- `halcyon update`

From Linux/macOS, run `./halcyon ...` or symlink the script into your `PATH`.

From Windows, run `.\halcyon.ps1 ...` in PowerShell or `halcyon.cmd ...` from Command Prompt. If you want bare `halcyon ...`, add the repository folder to your `PATH`.

`halcyon update` keeps your `data/` folders, library bind mount, database volume, and saved Admin settings in place. It pulls the newest git version and rebuilds the stack.

## First boot and admin onboarding

On a fresh install, halcyon creates:

- an `admin` account
- a default `guest` account with password `guest`

The `admin` account starts with a temporary password that is printed in the container logs.

To read it:

```bash
docker compose logs halcyon-web
```

Look for the bootstrap admin credential in the log output, then:

1. Sign in as `admin`
2. Save the generated recovery phrase somewhere safe
3. Confirm that you saved it
4. Set the permanent admin password

That recovery phrase is important. If the admin password is lost later, the recovery flow depends on it.

## Recommended post-install steps

After first login:

1. Open `Settings`
2. Confirm your mounted library path is correct
3. Add your YouTube API key in Admin settings if you want stronger sync enrichment
4. Review retention settings before enabling retention
5. Create any additional user accounts you want
6. Promote trusted accounts to admin only if they should manage server settings

When a newer version is available, Admin settings also shows an update indicator beside the version footer. The popup there will show the current version, newest version, and the host-side update command to run.

## Library workflow

The intended setup is simple:

1. Set MeTube downloads to the same folder halcyon scans
2. halcyon detects the new media
3. halcyon identifies the channel
4. halcyon creates the channel folder if needed
5. halcyon moves the matched video into that channel folder while keeping metadata intact

If a file is already matched but sitting in the wrong place, regular sync can repair that placement automatically.

## Retention behavior

Retention uses a staging model:

1. old items are marked
2. marked items move to the pre-delete retention folder
3. each marked item gets its own grace timer
4. items can be reverted during that grace period
5. once the timer expires, they can be deleted permanently

Retention is designed so that reverting returns the file to the original library location, including the original subfolder path.

## Development

### Backend

```bash
cd backend
pip install -e .[dev]
uvicorn app.main:app --reload
```

### Frontend

```bash
cd frontend
npm install
npm run dev
```

## Default ports

- App: `11111`
- Postgres: `5432`

## Version

Current release package:

- `1.1.26-48`

## Credits

- [MeTube](https://github.com/alexta69/metube) as the recommended downloader companion for feeding the library and automating new downloads into halcyon
- [Return YouTube Dislike](https://www.returnyoutubedislike.com/) for the public API and dislike/vote data used during sync enrichment
