# This file and read_concordances.R travel inside dataset/. No Python is needed.
source(file.path(dirname(normalizePath(sys.frame(1)$ofile, winslash = "/")), "read_concordances.R"), local = TRUE)

verify_dataset <- function(dataset_root = "dataset") {
  for (package in c("jsonlite", "digest")) {
    if (!requireNamespace(package, quietly = TRUE)) stop("Install package: ", package)
  }
  root <- normalizePath(dataset_root, winslash = "/", mustWork = TRUE)
  manifest <- jsonlite::fromJSON(file.path(root, "dataset-manifest.json"), simplifyVector = FALSE)
  if (!identical(manifest$format, "soccer-r-dataset-v1")) stop("Unknown R dataset format")
  paths <- vapply(manifest$files, function(item) {
    path <- item$path
    if (!is.character(path) || length(path) != 1L || is.na(path) ||
        grepl("(^/|:|\\\\|(^|/)\\.\\.?(/|$))", path)) stop("Invalid dataset path")
    full <- normalizePath(file.path(root, path), winslash = "/", mustWork = TRUE)
    if (!startsWith(full, paste0(root, "/"))) stop("Dataset path leaves its root")
    if (isTRUE(file.info(full)$isdir) || file.info(full)$size != item$size ||
        !identical(digest::digest(file = full, algo = "sha256", serialize = FALSE), item$sha256))
      stop("Dataset checksum/size mismatch: ", path)
    path
  }, character(1))
  if (anyDuplicated(paths)) stop("Duplicate dataset manifest path")
  if (!all(c("read_dataset.R", "read_concordances.R", "README.md", "coverage.json",
             "concordance/metadata/manifest.json") %in% paths)) stop("Incomplete R dataset handoff")
  tables <- vapply(manifest$tables, function(item) item$path, character(1))
  if (anyDuplicated(tables) || !setequal(tables, paths[startsWith(paths, "raw_data/")]))
    stop("Dataset table inventory differs from manifest")
  found <- list.files(file.path(root, "raw_data"), pattern = "\\.csv$", recursive = TRUE)
  if (!setequal(if (length(found)) paste0("raw_data/", found) else character(), tables)) stop("Unverified or missing season tables")
  for (item in manifest$tables) {
    expected <- paste0("raw_data/[^/]+/[0-9]{4}-[0-9]{4}/", item$table, "\\.csv$")
    if (!item$table %in% c("fixtures", "player_match_stats", "team_match_stats") ||
        !grepl(paste0("^", expected), item$path)) stop("Invalid season table path")
  }
  invisible(manifest)
}

read_dataset <- function(dataset_root = "dataset", require_integration = FALSE) {
  root <- normalizePath(dataset_root, winslash = "/", mustWork = TRUE)
  manifest <- verify_dataset(root)
  concordance <- read_concordances(file.path(root, "concordance"), require_integration = require_integration)
  if (!identical(isTRUE(manifest$integration_confirmed), isTRUE(concordance$manifest$ready_for_R)))
    stop("Dataset integration status differs from concordance")
  if (!isTRUE(manifest$integration_confirmed))
    warning("MSI integration has not been audited; reading the verified packaged local data. Use require_integration = TRUE to require that certification.", call. = FALSE)
  tables <- data.frame(path = character(), table = character(), competition = character(),
                       season = character(), rows = numeric(), id_namespace = character())
  if (length(manifest$tables)) tables <- do.call(rbind, lapply(manifest$tables, function(item)
    data.frame(path = item$path, table = item$table, competition = item$competition,
               season = item$season, rows = item$rows,
               id_namespace = if (is.null(item$id_namespace)) NA_character_ else item$id_namespace)))
  structure(list(root = root, concordance = concordance, tables = tables, manifest = manifest,
                 coverage = jsonlite::fromJSON(file.path(root, "coverage.json"), simplifyVector = FALSE)),
            class = "soccer_dataset")
}

read_dataset_table <- function(dataset, table, competition = NULL, season = NULL) {
  if (!inherits(dataset, "soccer_dataset")) stop("Load the folder with read_dataset first")
  if (length(table) != 1L || !table %in% c("fixtures", "player_match_stats", "team_match_stats"))
    stop("Choose fixtures, player_match_stats or team_match_stats")
  selected <- dataset$tables$table == table
  if (!is.null(competition)) selected <- selected & dataset$tables$competition %in% competition
  if (!is.null(season)) selected <- selected & dataset$tables$season %in% season
  entries <- dataset$manifest$tables[which(selected)]
  if (!length(entries)) stop("No packaged table matches this selection: ", table)
  lookup <- function(native, kind, providers) {
    result <- rep(NA_character_, length(native))
    reference <- dataset$concordance[[paste0(kind, "_ids")]]
    for (provider in c("sofascore", "fotmob", "whoscored")) {
      use <- which(!is.na(providers) & providers == provider & !is.na(native) & native != "")
      if (length(use)) {
        result[use] <- reference$entity_id[match(native[use], reference[[paste0(provider, "_id")]])]
        missing <- use[is.na(result[use])]
        if (length(missing)) {
          numbers <- unique(native[missing])
          resolved <- dataset$concordance$resolve_provider_ids(kind, provider, numbers)
          result[missing] <- resolved[match(native[missing], numbers)]
        }
      }
    }
    result
  }
  store_id <- function(data, column, resolved, kind) {
    if (column %in% names(data)) {
      supplied <- as.character(data[[column]])
      supplied[supplied == ""] <- NA_character_
      if (any(!is.na(supplied) & !is.na(resolved) & supplied != resolved))
        stop("Packaged canonical ID disagrees with provider mapping: ", column)
      missing <- is.na(supplied)
      if ("identity_resolution_status" %in% names(data)) missing <- missing & data$identity_resolution_status != "conflict"
      supplied[missing] <- resolved[missing]; resolved <- supplied
    }
    allowed <- dataset$concordance[[paste0(kind, "_ids")]]$entity_id
    if (any(!is.na(resolved) & !resolved %in% allowed)) stop("Unknown packaged canonical ID: ", column)
    data[[column]] <- resolved
    data
  }
  frames <- lapply(entries, function(item) {
    path <- file.path(dataset$root, item$path)
    info <- dataset$manifest$files[[match(item$path, vapply(dataset$manifest$files, function(f) f$path, character(1)))]]
    if (!file.exists(path) || file.info(path)$size != info$size ||
        !identical(digest::digest(file = path, algo = "sha256", serialize = FALSE), info$sha256))
      stop("Dataset changed since loading: ", item$path)
    columns <- unlist(item$columns, use.names = FALSE)
    classes <- rep(NA_character_, length(columns)); names(classes) <- columns
    classes[grepl("(^id$|_id$)", columns) | columns == "season"] <- "character"
    data <- read.csv(path, colClasses = classes, na.strings = c("", "NA"),
                     check.names = FALSE, stringsAsFactors = FALSE, fileEncoding = "UTF-8")
    if (!identical(names(data), columns) || nrow(data) != item$rows) stop("Season schema/row count mismatch: ", item$path)
    if ("date" %in% columns) data$date <- as.Date(data$date)
    if ("kickoff_utc" %in% columns) data$kickoff_utc <- .soccer_parse_utc(data$kickoff_utc)
    providers <- rep(if (is.null(item$id_namespace)) NA_character_ else item$id_namespace, nrow(data))
    data$id_namespace <- providers
    data <- store_id(data,"match_entity_id",lookup(data$match_id,"match",providers),"match")
    if (all(c("provider", "provider_match_id") %in% columns)) {
      alternate <- lookup(data$provider_match_id, "match", data$provider)
      use <- is.na(data$match_entity_id)
      if ("identity_resolution_status" %in% names(data)) use <- use & data$identity_resolution_status != "conflict"
      data$match_entity_id[use] <- alternate[use]
      if (any(!use & !is.na(alternate) & data$match_entity_id != alternate, na.rm = TRUE))
        stop("Baseline/provider match identities disagree: ", item$path)
    }
    for (spec in list(c("player_id", "player", "player_entity_id"), c("team_id", "team", "team_entity_id"),
                      c("opponent_id", "team", "opponent_entity_id"), c("home_team_id", "team", "home_team_entity_id"),
                      c("away_team_id", "team", "away_team_entity_id"))) {
      if (spec[1] %in% columns) data <- store_id(data,spec[3],lookup(data[[spec[1]]],spec[2],providers),spec[2])
    }
    if (all(c("provider", "provider_team_id") %in% columns))
      data <- store_id(data,"team_entity_id",lookup(data$provider_team_id,"team",data$provider),"team")
    for (column in intersect(c("team_entity_id","opponent_entity_id","home_team_entity_id","away_team_entity_id"),names(data)))
      data <- store_id(data,column,rep(NA_character_,nrow(data)),"team")
    if ("player_entity_id" %in% names(data)) data <- store_id(data,"player_entity_id",rep(NA_character_,nrow(data)),"player")
    missing <- sum(is.na(data$match_entity_id) & !is.na(data$match_id))
    if (missing) warning(missing, " rows have an unlinked match ID in ", item$path, call. = FALSE)
    for (name in intersect(c("team_entity_id", "player_entity_id", "opponent_entity_id"), names(data))) {
      if (anyNA(data[[name]])) warning("Some ", name, " values could not be linked in ", item$path, call. = FALSE)
    }
    data
  })
  columns <- unique(unlist(lapply(frames, names), use.names = FALSE))
  frames <- lapply(frames, function(data) {
    for (missing in setdiff(columns, names(data))) data[[missing]] <- rep(NA, nrow(data))
    data[columns]
  })
  result <- do.call(rbind, frames); rownames(result) <- NULL
  result
}
