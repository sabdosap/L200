terraform {
  required_version = ">= 1.6.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
    google-beta = {
      source  = "hashicorp/google-beta"
      version = "~> 6.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

provider "google-beta" {
  project = var.project_id
  region  = var.region
}

locals {
  enabled_apis = toset([
    "aiplatform.googleapis.com",
    "artifactregistry.googleapis.com",
    "cloudkms.googleapis.com",
    "cloudtasks.googleapis.com",
    "cloudtrace.googleapis.com",
    "dlp.googleapis.com",
    "firestore.googleapis.com",
    "healthcare.googleapis.com",
    "identitytoolkit.googleapis.com",
    "logging.googleapis.com",
    "monitoring.googleapis.com",
    "run.googleapis.com",
    "secretmanager.googleapis.com",
    "spanner.googleapis.com",
    "speech.googleapis.com",
    "storage.googleapis.com",
  ])
}

resource "google_project_service" "carethread_apis" {
  for_each           = local.enabled_apis
  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

module "iam" {
  source     = "./modules/iam"
  project_id = var.project_id

  depends_on = [google_project_service.carethread_apis]
}

module "security_obs" {
  source = "./modules/security_obs"
  providers = {
    google      = google
    google-beta = google-beta
  }
  project_id          = var.project_id
  region              = var.region
  environment         = var.environment
  kms_rotation_period = var.kms_key_rotation_period
  service_accounts    = module.iam.service_account_emails
  labels              = var.labels

  depends_on = [module.iam]
}

module "storage" {
  source                    = "./modules/storage"
  project_id                = var.project_id
  region                    = var.region
  environment               = var.environment
  kms_crypto_key_id         = module.security_obs.cmek_crypto_key_id
  raw_audio_retention_days  = var.raw_audio_retention_days
  transcript_retention_days = var.transcript_retention_days
  packet_retention_days     = var.packet_retention_days
  service_accounts          = module.iam.service_account_emails
  labels                    = var.labels

  depends_on = [module.security_obs]
}

module "healthcare_fhir" {
  source           = "./modules/healthcare_fhir"
  project_id       = var.project_id
  region           = var.region
  environment      = var.environment
  service_accounts = module.iam.service_account_emails
  labels           = var.labels

  depends_on = [module.iam]
}

module "spanner_graph" {
  source           = "./modules/spanner_graph"
  project_id       = var.project_id
  region           = var.region
  environment      = var.environment
  processing_units = var.spanner_processing_units
  kms_key_name     = module.security_obs.cmek_crypto_key_id
  service_accounts = module.iam.service_account_emails
  labels           = var.labels

  depends_on = [module.security_obs]
}

module "firestore" {
  source      = "./modules/firestore"
  project_id  = var.project_id
  region      = var.region
  environment = var.environment

  depends_on = [google_project_service.carethread_apis]
}

module "tasks_and_run" {
  source                  = "./modules/tasks_and_run"
  project_id              = var.project_id
  region                  = var.region
  environment             = var.environment
  service_accounts        = module.iam.service_account_emails
  raw_audio_bucket_name   = module.storage.raw_audio_bucket_name
  transcripts_bucket_name = module.storage.transcripts_bucket_name
  packets_bucket_name     = module.storage.packets_bucket_name
  fhir_store_id           = module.healthcare_fhir.fhir_store_id
  spanner_instance_name   = module.spanner_graph.spanner_instance_name
  spanner_database_name   = module.spanner_graph.spanner_database_name
  firestore_database_name = module.firestore.database_name
  sdp_deidentify_template = module.security_obs.sdp_deidentify_template_name
  signing_secret_id       = module.security_obs.signing_secret_id
  capability_secret_id    = module.security_obs.capability_secret_id
  enable_argon_reviewer   = var.enable_argon_reviewer
  api_image               = var.api_image
  adk_runtime_image       = var.adk_runtime_image
  worker_transcribe_image = var.worker_transcribe_image
  worker_fhir_image       = var.worker_fhir_image
  worker_graph_image      = var.worker_graph_image
  worker_release_image    = var.worker_release_image
  labels                  = var.labels

  depends_on = [
    module.storage,
    module.healthcare_fhir,
    module.spanner_graph,
    module.firestore,
    module.security_obs,
  ]
}
