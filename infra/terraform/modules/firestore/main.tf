variable "project_id" {
  type = string
}

variable "region" {
  type = string
}

variable "environment" {
  type = string
}

resource "google_firestore_database" "operational" {
  project                     = var.project_id
  name                        = "(default)"
  location_id                 = var.region
  type                        = "FIRESTORE_NATIVE"
  concurrency_mode            = "OPTIMISTIC"
  app_engine_integration_mode = "DISABLED"
  delete_protection_state     = "DELETE_PROTECTION_ENABLED"
}

# 14-day TTL policy on filtered ADK session state (zero raw PHI stored).
resource "google_firestore_field" "adk_sessions_ttl" {
  project    = var.project_id
  database   = google_firestore_database.operational.name
  collection = "adk_sessions"
  field      = "ttl_expire_at"

  ttl_config {}
}

# TTL policy on completed job state and transactional outbox entries.
resource "google_firestore_field" "jobs_ttl" {
  project    = var.project_id
  database   = google_firestore_database.operational.name
  collection = "jobs"
  field      = "ttl_expire_at"

  ttl_config {}
}

resource "google_firestore_field" "outbox_ttl" {
  project    = var.project_id
  database   = google_firestore_database.operational.name
  collection = "outbox_events"
  field      = "ttl_expire_at"

  ttl_config {}
}

output "database_name" {
  value = google_firestore_database.operational.name
}
