# Transfer using the private GitHub repository

The repository already includes both scrapers. On another computer, sign in to
GitHub with the account that has access to `arausch335/sofascore-scrapers`.

## GitHub Desktop

1. Install GitHub Desktop and sign in.
2. Choose **File → Clone repository**, select `sofascore-scrapers`, and choose
   a new local folder.
3. Open the cloned folder in PyCharm or a terminal and follow the
   [README](../README.md) to create a Python environment and install dependencies.
4. After code changes are merged, use **Fetch origin**, then **Pull origin**.

## Terminal

```sh
git clone https://github.com/arausch335/sofascore-scrapers.git
cd sofascore-scrapers
```

Authenticate using GitHub CLI, GitHub Desktop or your credential manager. The
repository must remain private in **Settings → General**.

To review a proposed change on its branch before it is merged:

```sh
git fetch origin
git switch --track origin/simplify-scraper-outputs
```

After the change is merged, switch to `main` and pull:

```sh
git switch main
git pull --ff-only
```

Generated data and virtual environments are excluded from Git. Recreate the
Python environment on each computer. To continue existing player collections,
use [transfer.py](../player_match_information/transfer.py) to transfer caches,
checkpoints and cooldown state separately. Stop collection on the first device
before continuing on the second.

## Creating another private repository

At https://github.com/new, choose **Private** and leave the README, license and
`.gitignore` options blank. In a fresh folder containing this project's source:

```sh
git init -b main
git add .
git commit -m "Add fixture and player statistics scrapers"
git remote add origin https://github.com/YOUR_ACCOUNT/YOUR_PRIVATE_REPOSITORY.git
git push -u origin main
```

Replace the URL with your new repository. If authentication rejects a workflow
file because the token lacks workflow permission, use an appropriately
permissioned credential or omit that workflow. This project does not require
GitHub Actions to run either scraper.
