# Parquet (default) or CSV fixtures and IDs under dataset/concordance; supporting tables stay in metadata.
read_concordances <- function(concordance_root = "dataset/concordance", require_integration = TRUE) {
  for (package in c("jsonlite", "digest")) {
    if (!requireNamespace(package, quietly = TRUE)) stop("Install package: ", package)
  }
  folder <- concordance_root
  if (!file.exists(file.path(folder, "metadata", "manifest.json"))) folder <- file.path(folder, "concordance")
  manifest <- jsonlite::fromJSON(file.path(folder, "metadata", "manifest.json"), simplifyVector = FALSE)
  encoding <- switch(manifest$format, "soccer-concordance-csv-v1" = "csv",
    "soccer-concordance-parquet-v1" = "parquet", "soccer-concordance-parquet-v2" = "parquet", stop("Unknown concordance format"))
  if (!is.null(manifest$data_format) && !identical(manifest$data_format, encoding)) stop("Concordance format fields disagree")
  if (encoding == "parquet" && !requireNamespace("arrow", quietly = TRUE)) stop("Install package: arrow")
  if (require_integration && !isTRUE(manifest$ready_for_R)) stop("MSI integration is unconfirmed. Audit and rebuild first.")
  expected <- character()
  read_table <- function(name) {
    info <- manifest$tables[[name]]
    if (is.null(info) || !length(info$files)) stop("Missing table: ", name)
    paths <- vapply(seq_along(info$files), function(index) {
      item <- info$files[[index]]
      relative <- item$path
      stem <- switch(name, "fixtures/fixtures" = "fixtures", "fixtures/extra" = "metadata/fixture_extra",
        "fixtures/metadata" = "metadata/fixtures", "entities" = "metadata/entities", name)
      wanted <- if (identical(manifest$format, "soccer-concordance-parquet-v2")) paste0(stem, sprintf("/part-%05d.parquet", index - 1L)) else paste0(stem, ".", encoding)
      if (!identical(relative, wanted)) stop("Invalid concordance path")
      path <- file.path(folder, relative)
      if (!identical(digest::digest(file = path, algo = "sha256", serialize = FALSE), item$sha256)) stop("Concordance checksum mismatch: ", relative)
      expected <<- c(expected, relative)
      path
    }, character(1))
    data <- do.call(rbind, lapply(paths, function(path) {
      if (encoding == "parquet") as.data.frame(arrow::read_parquet(path))
      else read.csv(path, colClasses = "character", na.strings = "", check.names = FALSE)
    }))
    if (encoding == "csv" && name %in% c("fixtures/fixtures", "fixtures/extra")) {
      for (column in intersect(c("has_player_stats", "is_scheduled_for_scraping", "awarded"), names(data)))
        data[[column]] <- as.logical(data[[column]])
      for (column in intersect(c("matchweek", "home_score", "away_score", "winner_code"), names(data)))
        data[[column]] <- as.integer(as.numeric(data[[column]]))
      if ("date" %in% names(data)) data$date <- as.Date(data$date)
      if ("kickoff_utc" %in% names(data)) data$kickoff_utc <- as.POSIXct(data$kickoff_utc, tz = "UTC")
    }
    if (nrow(data) != info$rows) stop("Row count mismatch: ", name)
    data
  }
  fixtures <- read_table("fixtures/fixtures")
  extra <- read_table("fixtures/extra")
  metadata <- read_table("fixtures/metadata")
  ids <- lapply(c("match", "team", "player"), function(kind) {
    data <- read_table(paste0(kind, "_ids"))
    columns <- c("entity_id", "alex_id", "reep_id", "sofascore_id", "fotmob_id", "whoscored_id",
                 "entity_name", "reep_name", "sofascore_name", "fotmob_name", "whoscored_name")
    if (!identical(names(data), columns) || !all(vapply(data, is.character, logical(1)))) stop("ID/name columns must be character")
    if (anyNA(data$entity_id) || anyDuplicated(data$entity_id)) stop("Missing or duplicate entity ID")
    if (any(rowSums(!is.na(data[c("sofascore_id", "fotmob_id", "whoscored_id")])) == 0L)) stop("Identity has no provider ID")
    for (p in c("sofascore", "fotmob", "whoscored")) {
      native <- data[[paste0(p, "_id")]]
      if (anyDuplicated(native[!is.na(native)])) stop("Provider ID maps to multiple entities")
    }
    chosen <- rep(NA_character_, nrow(data))
    for (p in c("reep", "sofascore", "fotmob", "whoscored")) {
      name <- data[[paste0(p, "_name")]]
      use <- is.na(chosen) & !is.na(name)
      chosen[use] <- name[use]
    }
    if (!identical(data$entity_name, chosen)) stop("Entity name does not follow provider priority")
    canonical <- ifelse(!is.na(data$reep_id), data$reep_id, data$alex_id)
    if (anyNA(canonical) || any(canonical != data$entity_id)) stop("Canonical ID differs from Reep/Alex identity")
    data
  })
  names(ids) <- c("match_ids", "team_ids", "player_ids")
  details <- lapply(c("match_ids", "team_ids", "player_ids"), function(name) read_table(paste0("metadata/", name)))
  names(details) <- names(ids)
  entities <- read_table("entities")
  registry_rows <- read_table("metadata/registry")
  if (!identical(manifest$registry_format, "soccer-alex-ids-v1")) stop("Unknown identity registry format")
  registry <- lapply(c("aliases", "published", "redirects"), function(section) {
    rows <- registry_rows[registry_rows$section == section, , drop = FALSE]
    values <- lapply(rows$value, jsonlite::fromJSON)
    names(values) <- rows$key
    list2env(values, hash = TRUE, parent = emptyenv())
  })
  names(registry) <- c("aliases", "published", "redirects")
  canonical <- function(key) {
    seen <- character()
    repeat {
      if (key %in% seen) stop("Cyclic identity redirect")
      seen <- c(seen, key)
      next_key <- registry$redirects[[key]]
      if (is.null(next_key)) next_key <- registry$published[[key]]
      if (is.null(next_key)) return(key)
      key <- next_key
    }
  }
  for (kind in c("match", "team", "player")) {
    data <- ids[[paste0(kind, "_ids")]]
    for (i in seq_len(nrow(data))) for (provider in c("sofascore", "fotmob", "whoscored")) {
      native <- data[[paste0(provider, "_id")]][i]
      if (is.na(native)) next
      alias <- registry$aliases[[paste(kind, provider, native, sep = "|")]]
      if (!is.null(alias) && canonical(alias) != data$entity_id[i]) stop("Provider/canonical ID disagreement")
    }
  }
  found <- list.files(folder, pattern = "\\.(csv|parquet)$", recursive = TRUE)
  found <- found[!grepl("^metadata/(compiled|legacy|fixture_baselines)/", found)]
  if (!setequal(found, expected)) stop("Unverified or stale concordance tables")
  key <- fixtures$match_entity_id
  if (anyNA(key) || anyDuplicated(key) || anyNA(match(key, ids$match_ids$entity_id))) stop("Invalid fixture match key")
  for (data in list(extra, metadata)) {
    if (anyNA(data$match_entity_id) || anyDuplicated(data$match_entity_id) || !setequal(key, data$match_entity_id)) stop("Fixture tables have different match keys")
  }
  for (side in c("home", "away")) {
    team <- fixtures[[paste0(side, "_team_entity_id")]]
    source <- if ("fixture_source" %in% names(metadata)) metadata$fixture_source[match(key, metadata$match_entity_id)] else rep("", length(key))
    allowed_missing <- fixtures$has_player_stats & !is.na(source) & source == "stored_player_stats"
    if (any(is.na(team) & !allowed_missing)) stop("Fixture is missing a team entity ID")
    if (anyNA(match(team[!is.na(team)], ids$team_ids$entity_id))) stop("Fixture team is absent from team_ids")
  }
  if (any(fixtures$has_player_stats & extra$is_scheduled_for_scraping[match(key, extra$match_entity_id)])) stop("Scraped fixture marked pending")
  c(list(fixtures = fixtures, extra = extra, metadata = metadata), ids,
    list(identity_metadata = details, registry = registry_rows, entities = entities, manifest = manifest))
}
