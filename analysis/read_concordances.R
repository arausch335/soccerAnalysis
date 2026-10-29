# Read the six versioned identity tables. IDs always remain character.
read_concordances <- function(dataset_root, require_integration = TRUE) {
  for (package in c("readr", "jsonlite", "digest")) {
    if (!requireNamespace(package, quietly = TRUE)) stop("Install package: ", package)
  }
  folder <- if (file.exists(file.path(dataset_root, "bundle.json"))) dataset_root else
    file.path(dataset_root, "concordance")
  manifest <- jsonlite::fromJSON(file.path(folder, "bundle.json"), simplifyVector = FALSE)
  if (!identical(manifest$format, "soccer-concordance-bundle-v2")) stop("Unknown concordance format")
  if (require_integration && !isTRUE(manifest$ready_for_R)) {
    stop("MSI integration is unconfirmed. Run the MSI audit and rebuild first.")
  }
  paths <- c("full/fixtures.csv", "full/entities.csv", "full/teams.csv",
             "scraped/matches.csv", "scraped/teams.csv", "scraped/players.csv")
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
      if ("date" %in% names(data)) data$date <- readr::parse_date(data$date)
      if ("kickoff_utc" %in% names(data)) data$kickoff_utc <- readr::parse_datetime(data$kickoff_utc, locale = readr::locale(tz = "UTC"))
    }
    if ("safe_for_cross_provider_joins" %in% names(data)) {
      data$safe_for_cross_provider_joins <- tolower(data$safe_for_cross_provider_joins) == "true"
    }
    data
  })
  names(tables) <- paths
  list(full = list(fixtures = tables[[1]], entities = tables[[2]], teams = tables[[3]]),
       scraped = list(matches = tables[[4]], teams = tables[[5]], players = tables[[6]]),
       manifest = manifest)
}
