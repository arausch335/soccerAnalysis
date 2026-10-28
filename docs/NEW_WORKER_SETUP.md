# Turn a new computer into a collection worker

Prepared for Dario on 28 October 2026. These instructions cover Windows and
macOS. Use worker ID `dario` throughout. For another computer, Main must first
register its own distinct ID, assignment and token.

## 1. Install the basics

Install from these official pages:

| Software | Windows | macOS | Purpose |
| --- | --- | --- | --- |
| Git | [Installer](https://git-scm.com/install/windows) | [Install options](https://git-scm.com/install/mac); `xcode-select --install` is one option | Download code and shared assignments |
| Python | [Python downloads](https://www.python.org/downloads/windows/) | [Python downloads](https://www.python.org/downloads/macos/) | Run the collector |
| Tailscale | [Download](https://tailscale.com/download) | [Download](https://tailscale.com/download) | Private connection to Main and MSI |
| Google Chrome | [Download](https://www.google.com/chrome/) | [Download](https://www.google.com/chrome/) | Browser fallback through Soccer Collector |

Use a current supported 64-bit CPython installer. This project's Main environment
has been exercised with Python 3.12.10; Python 3.12 or newer is the onboarding
baseline, with package compatibility checked by the install below. Do not
download the old 3.12.10 installer just to match Main. Choose the installer for
the computer's x64/ARM architecture. On Windows, enable Python on PATH if the
installer offers it, and keep the Python launcher. Reopen the terminal after
installation. PyCharm is optional; a terminal is sufficient.

Check the installation:

```powershell
# Windows PowerShell
git --version
py --version
```

```bash
# macOS Terminal
git --version
python3 --version
```

## 2. Join the private network

Sign Tailscale into the same tailnet as Main and MSI, or accept the owner's
tailnet invitation. Approve the new device if that network requires it. Give it
the device name `dario` for clarity; the program's worker ID is configured
separately. Do not sign into an unrelated personal tailnet.

Current project addresses (recheck with Main if they change):

- Main reporting: `http://100.106.146.82:8766`
- MSI raw storage: `http://100.116.229.51:8767`
- Main's dashboard opens locally on Main: `http://127.0.0.1:8765`

Main must run its monitor, and MSI must run its storage/worker service. Allow
these private connections in the host firewall/tailnet policy if required. Do
not expose either receiver through router port forwarding. On Windows:

```powershell
Test-NetConnection 100.106.146.82 -Port 8766
Test-NetConnection 100.116.229.51 -Port 8767
```

On macOS, `nc -vz 100.106.146.82 8766` and
`nc -vz 100.116.229.51 8767` check those listeners. These tests check connection
only; reporting authentication is verified later.

## 3. Download the project and create its environment

Use a local disk folder outside OneDrive, Dropbox and other live sync folders.
Grant this computer read access to the private GitHub repository if prompted;
sign in through Git's browser flow. Worker reporting does not require Git push
access. Do not embed a GitHub token in a clone URL.

Windows PowerShell:

```powershell
New-Item -ItemType Directory -Force "$HOME\Projects" | Out-Null
Set-Location "$HOME\Projects"
git clone https://github.com/arausch335/soccerAnalysis.git
Set-Location soccerAnalysis
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip check
```

macOS Terminal:

```bash
mkdir -p "$HOME/Projects"
cd "$HOME/Projects"
git clone https://github.com/arausch335/soccerAnalysis.git
cd soccerAnalysis
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip check
```

If a folder named `soccerAnalysis` already exists, inspect and reuse the original
checkout; do not clone over it. Check `config/workers.yaml` contains `dario` and
its nine 2023/24 jobs. Do not start if the downloaded plan predates Dario.

In the remaining commands, `python` means the environment's interpreter:
`.\.venv\Scripts\python.exe` on Windows or `.venv/bin/python` on macOS. You can
substitute that full path in every command without activating the environment.
PowerShell activation is optional; no execution-policy change is needed.

## 4. Register Dario's reporting connection

Get only Dario's reporting token privately from Main. On Main it is in the
ignored local `data/_system/config/dario-onboarding.private.txt`; never put it in
Git, a shared guide, a screenshot or an issue. Then run on Dario:

```bash
python -m collection.reporting client --worker-id dario --url http://100.106.146.82:8766
```

Paste Dario's token at the hidden prompt and press Enter. This creates Dario's
local client configuration. Do not run the `server` command on Dario or copy
Main/MSI's `data/`, reporting config, identity, queue or storage secrets. Main
delivers Dario's separate producer credential for MSI automatically after the
first successful synchronization. MSI receives the updated producer allowlist
through its own reporting synchronization.

## 5. Prepare Chrome fallback

```bash
python -m tools.extension_setup
```

In Chrome, open `chrome://extensions`, enable Developer mode, click **Load
unpacked**, and choose this checkout's `build/soccer-collector-extension` folder.
Pin Soccer Collector if useful. Do not choose the source folder containing
Python files. Leave Chrome open with a normal Sofascore, WhoScored or FotMob tab.

## 6. Start the assigned work

Read [DARIO.md](DARIO.md), then run:

```bash
python main.py --worker-id dario
```

Python prints a connection code early in startup. Paste it into Soccer Collector
and choose **Connect this tab**. Pairing prepares fallback; all three providers
still start with their API/HTTP path. The program saves the local pairing for
future runs. If browser verification is requested, complete it in that Chrome
window and let the worker resume.

Use a second terminal in the same checkout to verify:

```bash
python -m collection.reporting status
```

Look for a successful authenticated connection. On Main, verify Dario appears in
Worker plan/logs with the expected season, ownership grant and remaining count.
Check its storage status after a saved match: raw uploads should be acknowledged
by MSI. Main may omit already resolved seasons/matches; this is expected.

Keep all computers plugged in, awake and Tailscale connected during collection.
Do not run another Dario process or a separate manual season collector. Main
uses prior inventories and reviews to skip finished work. A partial match may
need supporting requests on Dario; MSI merges the result without replacing
existing populated checkpoint values and retains provenance.

## 7. Resume and update safely

- Stop normally with Ctrl+C. Run the same worker command to resume. Keep both
  `data/` and `ensemble_data/`, including identity and outbox databases.
- For a manual code update, stop the worker, run `git status --short`, then
  `git pull --ff-only origin main`. Preserve local edits; do not hard reset.
  Reinstall dependencies if they changed.
- After an extension update, run `python -m tools.extension_setup`, then click
  **Reload** for Soccer Collector in Chrome. Restart the worker if needed.
- If Main is unavailable, a new season waits for instructions. Reports/uploads
  already queued survive interruptions. If storage is unavailable, keep the
  local data and check the queue; do not delete or manually overwrite MSI data.
- If authentication fails, use Dario's matching token. If identity/queue
  ownership conflicts, preserve the original files and investigate; never
  change worker IDs or delete queues to hide a conflict.
- Missing data and identity conflicts are reviewed on Main. Do not mark them
  unavailable or reopen completed seasons merely to finish faster.

## Main-side onboarding checklist

1. Add the distinct worker and nonoverlapping assignments in `config/workers.yaml`.
2. Preserve existing credentials; add a reporting token and storage-producer
   credential for the new worker. Keep them only in ignored private state.
3. Commit/push the shared plan and these instructions so all computers see the
   same revision. Main must load its new worker list; MSI must synchronize to
   receive the new storage producer credential before uploads can succeed.
4. Start/restart Main as `python main.py --worker-id main --monitor`. Keep all
   old inventories, review decisions and assignment claims. Inspect any old
   claim before a handoff; never clear a running worker's claim.
5. Deliver only the new worker's reporting token privately. Verify authenticated
   reporting, correct remaining-work instructions, season ownership and one
   acknowledged raw upload after the worker joins.

See [WORKERS.md](WORKERS.md) for reporting, review, storage and recovery details.
