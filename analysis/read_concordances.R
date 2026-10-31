# Read unified fixtures and the versioned team/player references. IDs remain character.
read_concordances <- function(dataset_root, require_integration = TRUE) {
  for (package in c("readr", "jsonlite", "digest")) {
    if (!requireNamespace(package, quietly = TRUE)) stop("Install package: ", package)
  }
  folder <- if (file.exists(file.path(dataset_root, "bundle.json"))) dataset_root else
    file.path(dataset_root, "concordance")
  manifest <- jsonlite::fromJSON(file.path(folder, "bundle.json"), simplifyVector = FALSE)
  if (!identical(manifest$format, "soccer-concordance-bundle-v3")) stop("Rebuild concordances for unified fixtures and availability flags")
  if (require_integration && !isTRUE(manifest$ready_for_R)) {
    stop("MSI integration is unconfirmed. Run the MSI audit and rebuild first.")
  }
  for (relative in names(manifest$auxiliary_checksums)) {
    if (!relative %in% c("id_redirects.csv", "quarantine.csv", "alex_registry.json", "registry_conflicts.csv") ||
        !identical(digest::digest(file = file.path(folder, relative), algo = "sha256", serialize = FALSE),
                   manifest$auxiliary_checksums[[relative]])) stop("Identity sidecar checksum mismatch")
  }
  paths <- c("full/fixtures.csv", "full/entities.csv", "full/teams.csv",
             "scraped/teams.csv", "scraped/players.csv")
  tables <- lapply(paths, function(relative) {
    path <- file.path(folder, relative)
    if (!identical(digest::digest(file = path, algo = "sha256", serialize = FALSE),
                   manifest$files[[relative]]$sha256)) stop("Snapshot checksum mismatch: ", relative)
    data <- readr::read_csv(path, col_types = readr::cols(.default = readr::col_character()),
                            na = "", show_col_types = FALSE, progress = FALSE)
    if (nrow(readr::problems(data))) stop("CSV parsing problems: ", relative)
    prefixes <- if (relative == "full/entities.csv") c("from_", "to_") else ""
    for (prefix in prefixes) {
      key <- data[[paste0(prefix, "entity_id")]]
      peers <- data[paste0(prefix, c("sofascore", "fotmob", "whoscored"), "_id")]
      if (anyNA(key) || any(rowSums(!is.na(peers)) == 0L)) stop("Missing canonical/provider ID: ", relative)
    }
    key <- if (relative == "full/entities.csv") "relationship_id" else "entity_id"
    if (anyDuplicated(data[[key]])) stop("Duplicate lookup key: ", relative)
    if (relative == "full/fixtures.csv") {
      for (column in intersect(c("home_score", "away_score", "winner_code", "matchweek"), names(data))) {
        values <- readr::parse_integer(data[[column]], na = character())
        if (nrow(readr::problems(values))) stop("Invalid numeric field: ", column)
        data[[column]] <- values
      }
      for (column in c("has_player_stats", "is_scheduled_for_scraping")) {
        if (!column %in% names(data) || anyNA(data[[column]]) ||
            any(!tolower(data[[column]]) %in% c("true", "false"))) stop("Missing or invalid fixture flag: ", column)
        data[[column]] <- tolower(data[[column]]) == "true"
      }
      if (any(data$has_player_stats & data$is_scheduled_for_scraping)) stop("Scraped fixture marked pending")
      if ("fixture_metadata_complete" %in% names(data)) data$fixture_metadata_complete <- tolower(data$fixture_metadata_complete) == "true"
      if ("date" %in% names(data)) data$date <- readr::parse_date(data$date)
      if ("kickoff_utc" %in% names(data)) data$kickoff_utc <- readr::parse_datetime(data$kickoff_utc, locale = readr::locale(tz = "UTC"))
    }
    if ("safe_for_cross_provider_joins" %in% names(data)) {
      data$safe_for_cross_provider_joins <- tolower(data$safe_for_cross_provider_joins) == "true"
    }
    data
  })
  names(tables) <- paths
  registry <- jsonlite::fromJSON(file.path(folder, "alex_registry.json"), simplifyVector = FALSE)
  for (field in c("aliases", "published", "redirects")) {
    registry[[field]] <- list2env(registry[[field]], hash = TRUE, parent = emptyenv())
  }
  canonical <- function(key) {
    seen <- character()
    while (!is.null(registry$redirects[[key]])) {
      if (key %in% seen) stop("Cyclic identity redirect")
      seen <- c(seen, key); key <- registry$redirects[[key]]
    }
    if (!is.null(registry$published[[key]])) return(canonical(registry$published[[key]]))
    key
  }
  seen_ids <- new.env(hash = TRUE, parent = emptyenv())
  register <- function(kind, provider, native, entity) {
    if (is.na(native) || !nzchar(native)) return(invisible(NULL))
    token <- paste(kind, provider, native, sep = "|")
    known <- registry$aliases[[token]]
    if ((!is.null(known) && !identical(canonical(known), entity)) ||
        (exists(token, envir = seen_ids, inherits = FALSE) && !identical(seen_ids[[token]], entity))) {
      stop("Provider/canonical ID disagreement: ", token)
    }
    seen_ids[[token]] <- entity
  }
  check_rows <- function(data, kind, prefix = "") {
    for (i in seq_len(nrow(data))) {
      safe <- data[[paste0(prefix, "safe_for_cross_provider_joins")]]
      safe <- length(safe) && !is.na(safe[i]) && tolower(as.character(safe[i])) == "true"
      providers <- c("sofascore", "fotmob", "whoscored")
      present <- providers[vapply(providers, function(p) {
        native <- data[[paste0(prefix, p, "_id")]][i]
        !is.na(native) && nzchar(native)
      }, logical(1))]
      if (!safe) present <- head(present, 1L)
      entity <- data[[paste0(prefix, "entity_id")]][i]
      published <- data[[paste0(prefix, "reep_id")]][i]
      if (safe && length(published) && !is.na(published) && !identical(canonical(published), entity)) {
        stop("Published Reep/canonical ID disagreement")
      }
      for (p in present) register(kind, p, data[[paste0(prefix, p, "_id")]][i], entity)
      if (safe && paste0(prefix, "provider_aliases") %in% names(data)) {
        aliases <- data[[paste0(prefix, "provider_aliases")]][i]
        if (!is.na(aliases)) {
          aliases <- jsonlite::fromJSON(aliases)
          for (p in providers) for (native in aliases[[paste0(p, "_id")]]) register(kind, p, native, entity)
        }
      }
    }
  }
  check_rows(tables[[1]], "match")
  for (i in c(3L, 4L)) check_rows(tables[[i]], "team")
  check_rows(tables[[5]], "player")
  for (prefix in c("from_", "to_")) check_rows(tables[[2]], "team", prefix)
  fixtures <- tables[[1]]; teams <- tables[[3]]
  fixture_source <- if ("fixture_source" %in% names(fixtures)) fixtures$fixture_source else rep("", nrow(fixtures))
  for (side in c("home", "away")) {
    entity <- fixtures[[paste0(side, "_entity_id")]]
    known <- !is.na(entity) & nzchar(entity)
    incomplete <- !known & fixtures$has_player_stats & !fixtures$fixture_metadata_complete &
      !is.na(fixture_source) & fixture_source == "stored_player_stats"
    if (any(!known & !incomplete) || anyNA(match(entity[known], teams$entity_id))) stop("Fixture references a missing full team")
    references <- data.frame(entity_id = entity)
    for (p in c("sofascore", "fotmob", "whoscored")) {
      native <- fixtures[[paste0(p, "_", side, "_team_id")]]
      references[[paste0(p, "_id")]] <- if (is.null(native)) rep(NA_character_, nrow(fixtures)) else native
    }
    references$safe_for_cross_provider_joins <- fixtures[[paste0(side, "_safe_for_cross_provider_joins")]]
    check_rows(references[known, , drop = FALSE], "team")
  }
  list(full = list(fixtures = tables[[1]], entities = tables[[2]], teams = tables[[3]]),
       scraped = list(teams = tables[[4]], players = tables[[5]]),
       manifest = manifest)
}
