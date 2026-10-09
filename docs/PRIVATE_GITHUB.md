# Transfer both scrapers through a private GitHub repository

First run `tools/include_player_project.py` as described in the README. Check
that `player_match_information/` contains your player script and its supporting
modules. This package starts without that code because it is stored on your
computer.

## GitHub Desktop

1. Install GitHub Desktop and sign in to your account.
2. Choose **File → New repository**. Name it `sofascore-scrapers` and choose a
   new local location for the repository. Leave the README, license, and Git
   ignore templates unset; this package supplies its own files.
3. Copy the contents of the extracted package, including the newly added
   `player_match_information/` folder, into that repository folder. Include
   `.gitignore` and `.github` too; on macOS, press Command+Shift+Period in Finder
   to show files beginning with a dot. Check Changes and commit with a message
   such as `Add fixture and player match scrapers`.
4. Click **Publish repository**. Keep **Keep this code private** checked, then
   publish to your account.
5. On the other computer, install GitHub Desktop, sign in to the same account,
   and choose **File → Clone repository**. Select `sofascore-scrapers`.
6. Open the cloned folder and follow the README's run instructions.

## Terminal alternative

Create a new repository at https://github.com/new. Name it `sofascore-scrapers`,
select **Private**, and leave the README, `.gitignore`, and license options blank.

In the extracted package folder, run:

```bash
git init -b main
git add .
git commit -m "Add fixture and player match scrapers"
git remote add origin https://github.com/arausch335/sofascore-scrapers.git
git push -u origin main
```

Replace the account/repository in the remote URL if you selected a different
destination. Authenticate with your usual GitHub Desktop, Git credential
manager, or GitHub CLI login.

On the other computer:

```bash
git clone https://github.com/arausch335/sofascore-scrapers.git
cd sofascore-scrapers
```

Sign in to the account with access to the private repository, then follow the
README. Generated scrape outputs stay local because `.gitignore` excludes them.

After later code changes, commit and push on the original computer; run
`git pull` on the other computer to receive them.

GitHub's reference: [Add a local repository to GitHub Desktop](https://docs.github.com/en/desktop/adding-and-cloning-repositories/adding-a-repository-from-your-local-computer-to-github-desktop).
