variable "project_id" {
  type = string
}

variable "region" {
  type = string
}

variable "environment" {
  type = string
}

variable "processing_units" {
  type = number
}

variable "kms_key_name" {
  type = string
}

variable "service_accounts" {
  type = map(string)
}

variable "labels" {
  type = map(string)
}

locals {
  ddl_statements = [
    for stmt in split(";", file("${path.module}/schema.sql")) :
    trimspace(stmt)
    if trimspace(stmt) != ""
  ]
}

resource "google_spanner_instance" "continuity" {
  name             = "carethread-graph-${var.environment}"
  project          = var.project_id
  config           = "regional-${var.region}"
  display_name     = "CareThread Continuity Graph (${var.environment})"
  edition          = "ENTERPRISE"
  processing_units = var.processing_units
  labels           = var.labels
}

resource "google_spanner_database" "continuity" {
  instance               = google_spanner_instance.continuity.name
  name                   = "carethread-continuity"
  project                = var.project_id
  database_dialect       = "GOOGLE_STANDARD_SQL"
  deletion_protection    = true
  enable_drop_protection = true
  ddl                    = local.ddl_statements

  encryption_config {
    kms_key_name = var.kms_key_name
  }
}

# Read-only Spanner Graph access for ADK continuity retrieval.
resource "google_spanner_database_iam_member" "adk_runtime_reader" {
  instance = google_spanner_instance.continuity.name
  database = google_spanner_database.continuity.name
  project  = var.project_id
  role     = "roles/spanner.databaseReader"
  member   = "serviceAccount:${var.service_accounts["adk_runtime"]}"
}

# Read/write Spanner Graph access strictly limited to async graph projection worker.
resource "google_spanner_database_iam_member" "graph_worker_user" {
  instance = google_spanner_instance.continuity.name
  database = google_spanner_database.continuity.name
  project  = var.project_id
  role     = "roles/spanner.databaseUser"
  member   = "serviceAccount:${var.service_accounts["worker_graph"]}"
}

output "spanner_instance_name" {
  value = google_spanner_instance.continuity.name
}

output "spanner_database_name" {
  value = google_spanner_database.continuity.name
}
