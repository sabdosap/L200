variable "project_id" {
  type = string
}

variable "region" {
  type = string
}

variable "environment" {
  type = string
}

variable "service_accounts" {
  type = map(string)
}

variable "labels" {
  type = map(string)
}

resource "google_healthcare_dataset" "carethread" {
  name      = "carethread-clinical-${var.environment}"
  project   = var.project_id
  location  = var.region
  time_zone = "UTC"
}

resource "google_healthcare_fhir_store" "authoritative_r4" {
  name                                = "carethread-fhir-r4-${var.environment}"
  dataset                             = google_healthcare_dataset.carethread.id
  version                             = "R4"
  enable_update_create                = false
  disable_referential_integrity       = false
  # Resource versioning enabled (enableResourceVersioning = true in Cloud Healthcare API)
  disable_resource_versioning         = false
  complex_data_type_reference_parsing = "ENABLED"
  labels                              = var.labels
}

# Sole FHIR R4 writer identity: sa-worker-fhir.
# sa-adk-runtime is NEVER granted roles/healthcare.fhirResourceEditor.
resource "google_healthcare_fhir_store_iam_member" "fhir_writer_editor" {
  fhir_store_id = google_healthcare_fhir_store.authoritative_r4.id
  role          = "roles/healthcare.fhirResourceEditor"
  member        = "serviceAccount:${var.service_accounts["worker_fhir"]}"
}

# Read-only access for authoritative continuity fallback, graph projection, and allowlisted packet assembly.
resource "google_healthcare_fhir_store_iam_member" "adk_runtime_reader" {
  fhir_store_id = google_healthcare_fhir_store.authoritative_r4.id
  role          = "roles/healthcare.fhirResourceReader"
  member        = "serviceAccount:${var.service_accounts["adk_runtime"]}"
}

resource "google_healthcare_fhir_store_iam_member" "graph_worker_reader" {
  fhir_store_id = google_healthcare_fhir_store.authoritative_r4.id
  role          = "roles/healthcare.fhirResourceReader"
  member        = "serviceAccount:${var.service_accounts["worker_graph"]}"
}

resource "google_healthcare_fhir_store_iam_member" "release_worker_reader" {
  fhir_store_id = google_healthcare_fhir_store.authoritative_r4.id
  role          = "roles/healthcare.fhirResourceReader"
  member        = "serviceAccount:${var.service_accounts["worker_release"]}"
}

output "dataset_id" {
  value = google_healthcare_dataset.carethread.id
}

output "fhir_store_id" {
  value = google_healthcare_fhir_store.authoritative_r4.id
}
