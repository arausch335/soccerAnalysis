# Dario: finish the remaining 2023/24 collection

Dario is a new computer with worker ID `dario`. “2023–2024” means the football
season `2023/24`, not both the 2023/24 and 2024/25 seasons. Main coordinates;
MSI stores raw data and publishes datasets. Dario fills the unresolved gaps left
by earlier collectors. Keep prior data, identities, provenance and human reviews.

## Assignment

- Men's football only.
- Premier League, Bundesliga, La Liga, Serie A, Ligue 1, Champions League,
  Europa League, Champions League Qualifiers and Europa League Qualifiers.
- Combined collection in Fast mode: Sofascore player/team statistics and
  incidents, FotMob and WhoScored player enrichment, and WhoScored events.
  Momentum is attempted when available but is optional in Fast mode. Additional
  spatial maps are outside this assignment; existing maps stay intact.
- Use only `python main.py --worker-id dario` in the repository's environment.
  Main supplies the actual remaining match IDs and grants season ownership.

## Collection rules

1. Try each provider's API/HTTP path first. The current Sofascore, WhoScored and
   FotMob transports already do this. Reuse validated caches. Prepare and pair
   Chrome as a fallback; pairing alone does not request browser collection.
   A provider can stay on fallback for the rest of a run after its API fails.
2. Respect the configured pacing and provider cooldowns. A rate limit is a reason
   to wait, not a reason to switch to Chrome to evade it. Complete visible
   verification in the connected browser when requested.
3. Before each season, accept Main's current instructions and ownership grant.
   Main merges previous workers' resource evidence and review decisions. Fully
   resolved matches are excluded before checkpoint loading and enrichment.
   Frozen Done seasons stay closed. Unknown schedules still need discovery;
   zero known matches does not establish completion.
4. Resume Dario's saved checkpoints. Completed local providers are reused.
   A partially completed match held elsewhere may need supporting data collected
   locally because inventory evidence is not the original checkpoint. This can
   repeat a supporting request; it must not replace the earlier authoritative
   work. Do not copy another computer's identity or mark remote raw files local.
5. Never use refresh, delete old checkpoints, reset identities, clear ownership
   claims, reopen Done seasons, or withdraw unavailable decisions just to obtain
   a clean run. `--retry` is for unfinished assignments and should only be used
   when needed. Do not run a standalone season command around Main's controls.
6. Upload through the normal storage outbox to MSI. Its verified import merges
   missing data, retains existing populated values, rejects checkpoint identity
   conflicts and archives incoming conflicts/provenance. Do not overwrite season
   folders, copy raw data through Git, or publish datasets directly from Dario.
7. Missing provider coverage and identity conflicts remain review items on Main.
   Never invent event streams, approve identity links or declare unavailable
   resources merely to turn a progress bar green. Report errors and exact gaps.

## Start and stop

Follow [NEW_WORKER_SETUP.md](NEW_WORKER_SETUP.md) first. With Main and MSI online:

```bash
python -m collection.reporting status
python main.py --worker-id dario
```

Keep Dario plugged in, awake, online and Tailscale connected. Leave the paired
Chrome window available for fallback. Use Ctrl+C for a normal stop. To stop all
project collectors on Dario, run `python main.py --terminate --worker-id dario`.
Preserve `data/` and `ensemble_data/` across restarts.

## Handoff note for the person or assistant operating Dario

You operate worker `dario` in `arausch335/soccerAnalysis`. Read this file and
`docs/NEW_WORKER_SETUP.md`. Finish only the unresolved men's 2023/24 jobs assigned
by Main. Start through the worker supervisor, use APIs before browser fallback,
resume saved work and preserve all prior collectors' data and human decisions.
Wait for Main if no grant is available. Send raw packages only to MSI through
the built-in storage service. Surface genuine missing data, identity conflicts,
verification needs and transfer failures for review; do not resolve them by
deleting state or marking the season complete. At handoff, report competitions
finished, exact remaining gaps, API/browser failures and pending uploads.
