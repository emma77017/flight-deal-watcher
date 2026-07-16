# Raspberry Pi Setup — a guided learning path

Goal: your flight watcher running 24/7 on the Pi in a Docker container, surviving
power cuts and reboots, independent of any laptop. Time: ~1 hour, mostly waiting.

You'll learn, in order: what "flashing an OS" means → how to control a headless
Linux machine over SSH → what Docker images/containers/volumes actually are.
Everything below runs from *your Mac's terminal* unless it says "on the Pi."

---

## Step 1 — Flash the operating system (~15 min)

**Concept:** A Pi has no built-in OS. Its "hard drive" is a microSD card, and
"flashing" means writing a bootable OS image onto it. You do this from your Mac.

1. Install the official imager: `brew install --cask raspberry-pi-imager` (or
   download from raspberrypi.com/software).
2. Insert the microSD card into your Mac (adapter needed).
3. In the imager: **Choose Device** → your Pi model. **Choose OS** →
   *Raspberry Pi OS Lite (64-bit)* — "Lite" has no desktop GUI. You won't ever
   plug a monitor into this Pi; that's called running **headless**.
4. Click **Next → Edit Settings** — this is the important part (headless setup):
   - hostname: `flightpi`
   - username: `emma`, and a password you'll remember
   - configure your home Wi-Fi name + password
   - Services tab: **enable SSH** (password authentication)
5. Write it (takes a few minutes), eject the card, put it in the Pi, plug the Pi
   into power. No monitor, no keyboard — that's the point.

## Step 2 — SSH in (~5 min)

**Concept:** SSH ("secure shell") gives you a terminal on another machine over
the network. This is how all servers everywhere are managed.

Wait ~2 minutes after power-on, then from your Mac:

```bash
ssh emma@flightpi.local
```

(`.local` names work via mDNS/Bonjour on your home network. If it can't find it,
check your router's device list for the Pi's IP and `ssh emma@<that-ip>`.)
Type `yes` at the first-connection fingerprint prompt, enter your password —
you're now typing commands *on the Pi*.

## Step 3 — Update the OS and install Docker (on the Pi, ~10 min)

**Concept:** `apt` is Debian/Raspberry Pi OS's package manager (like Homebrew).
Docker packages an app plus everything it needs into an **image**; running an
image gives you a **container** — an isolated, disposable mini-environment.
"Works in the container" = works the same on any machine. That's why it's the
industry standard your husband mentioned.

```bash
sudo apt update && sudo apt full-upgrade -y
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER      # let your user run docker without sudo
exit
```

That `exit` matters — group changes apply on next login. `ssh emma@flightpi.local`
back in, then verify: `docker run hello-world` should print a welcome message.

## Step 4 — Get the watcher and its secrets (on the Pi, ~5 min)

**Concept:** the code is public on GitHub, but secrets (the Gmail app password)
are NOT in git — they live in a local `.env` file that only this Pi has.

```bash
git clone https://github.com/emma77017/flight-deal-watcher.git
cd flight-deal-watcher
cp .env.example .env
nano .env        # fill in the real email + app password, Ctrl-O Enter Ctrl-X to save
```

(The Gmail app password is in `config.toml` on your Mac at `~/FlightWatcher`.)

## Step 5 — Build and launch (on the Pi, ~5 min build)

**Concept:** `docker-compose.yml` describes the whole deployment — image to
build, restart policy, timezone, and **volumes** (folders shared between the Pi
and the container, so the deal-history database survives rebuilds). One command
brings it all up; `-d` means detached (keeps running after you log out).

```bash
docker compose up -d --build
```

## Step 6 — Verify it's alive

```bash
docker compose logs -f          # watch it scan live; Ctrl-C stops watching (not the app)
docker compose exec watcher python watcher.py test-email
docker compose exec watcher python watcher.py status
docker compose exec watcher python watcher.py report
```

The scheduler inside the container runs: full scan 8:00/20:00, pulse every 2h,
healthcheck at noon (Pacific time, set in docker-compose.yml). It starts with a
pulse immediately, so within ~10 minutes `status` should show a completed run.

## Step 6.5 — Make it self-updating (one command)

```bash
cd ~/flight-deal-watcher && sudo bash install_selfupdate.sh
```

From then on the Pi checks GitHub every 3 hours (and after every reboot) and
rebuilds itself whenever routes/thresholds/code change — no SSH needed ever again.

## Step 7 — Living with it

- **Reboots/power cuts:** nothing to do. Docker starts on boot and
  `restart: unless-stopped` revives the container. Test it: `sudo reboot`, wait,
  ssh back in, `docker ps` — the watcher should be running again.
- **Update after code changes:** `git pull && docker compose up -d --build`
- **Change routes/budget:** edit `config.cloud.toml`, then rebuild (or bind-mount
  your own config.toml — see the commented line in docker-compose.yml).
- **Troubleshooting:** `docker ps` (is it up?), `docker compose logs --tail 100`,
  `ls logs/` on the Pi (the same files, via the volume).

## Step 8 — Retire the redundant scanners (optional)

Once the Pi has run for a few days you'll be getting duplicate deal emails
(Pi + GitHub + Mac each alert once per deal). Suggested end state:
- **Pi = primary** (24/7, home IP that Google trusts).
- **GitHub Actions = backup** — keep it; a second opinion from a different
  network is cheap insurance. Or disable: repo → Actions tab → each workflow →
  "Disable workflow".
- **Mac = texts only when it happens to be awake** — or retire it fully with
  `./uninstall_schedule.sh` in `~/FlightWatcher` and rely on Gmail push
  notifications on your phone.
