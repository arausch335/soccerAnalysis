# Combined collection

See [README](../README.md) for the supported API-first, extension-fallback
workflow and [the structure review](WORKFLOW_REVIEW.md) for data organization.
Standalone provider collection and native browser launchers have been retired.

All three providers are required in combined jobs. Existing raw responses,
checkpoints and identity mappings remain usable. Resume fills missing requested
provider and spatial resources. Export is offline and rebuilds tables from saved
artifacts. A partial match keeps successful provider data and stays retryable.

WhoScored supplies the individual event stream. FotMob supplies player/match
statistics; its bundled incident timeline is excluded. Sofascore supplies the
baseline match/lineup identity, detailed statistics and spatial maps.
