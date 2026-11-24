terraform {
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

variable "project_id" {
  type = string
}

variable "region" {
  type = string
}

variable "environment" {
  type = string
}

variable "kms_rotation_period" {
  type = string
}

variable "service_accounts" {
  type = map(string)
}

variable "labels" {
  type = map(string)
}

# ---------------------------------------------------------------------------
# CMEK Key Ring & Crypto Key for GCS & Spanner encryption at rest
# ---------------------------------------------------------------------------

resource "google_kms_key_ring" "carethread" {
  name     = "carethread-keyring-${var.environment}"
  project  = var.project_id
  location = var.region
}

resource "google_kms_crypto_key" "cmek" {
  name            = "carethread-cmek-${var.environment}"
  key_ring        = google_kms_key_ring.carethread.id
  rotation_period = var.kms_rotation_period
  purpose         = "ENCRYPT_DECRYPT"
  labels          = var.labels
}

data "google_storage_project_service_account" "gcs_agent" {
  project = var.project_id
}

resource "google_project_service_identity" "spanner_agent" {
  provider = google-beta
  project  = var.project_id
  service  = "spanner.googleapis.com"
}

resource "google_kms_crypto_key_iam_member" "gcs_cmek" {
  crypto_key_id = google_kms_crypto_key.cmek.id
  role          = "roles/cloudkms.cryptoKeyEncrypterDecrypter"
  member        = "serviceAccount:${data.google_storage_project_service_account.gcs_agent.email_address}"
}

resource "google_kms_crypto_key_iam_member" "spanner_cmek" {
  crypto_key_id = google_kms_crypto_key.cmek.id
  role          = "roles/cloudkms.cryptoKeyEncrypterDecrypter"
  member        = "serviceAccount:${google_project_service_identity.spanner_agent.email}"
}

# ---------------------------------------------------------------------------
# Secret Manager Shells (versions injected out-of-band; never hardcoded in TF)
# ---------------------------------------------------------------------------

resource "google_secret_manager_secret" "signing_key" {
  secret_id = "carethread-signing-key-${var.environment}"
  project   = var.project_id
  labels    = var.labels

  replication {
    user_managed {
      replicas {
        location = var.region
      }
    }
  }
}

resource "google_secret_manager_secret" "capability_hmac" {
  secret_id = "carethread-capability-hmac-${var.environment}"
  project   = var.project_id
  labels    = var.labels

  replication {
    user_managed {
      replicas {
        location = var.region
      }
    }
  }
}

resource "google_secret_manager_secret_iam_member" "api_capability_accessor" {
  project   = var.project_id
  secret_id = google_secret_manager_secret.capability_hmac.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${var.service_accounts["api"]}"
}

resource "google_secret_manager_secret_iam_member" "fhir_signing_accessor" {
  project   = var.project_id
  secret_id = google_secret_manager_secret.signing_key.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${var.service_accounts["worker_fhir"]}"
}

# ---------------------------------------------------------------------------
# Sensitive Data Protection (SDP / Cloud DLP) Regional PHI Templates
# ---------------------------------------------------------------------------

resource "google_data_loss_prevention_inspect_template" "phi_inspect" {
  parent       = "projects/${var.project_id}/locations/${var.region}"
  template_id  = "carethread-phi-inspect-${var.environment}"
  display_name = "CareThread PHI Inspect Template"
  description  = "Targeted PHI infoTypes for low-latency telemetry redaction."

  inspect_config {
    min_likelihood = "POSSIBLE"
    include_quote  = false

    info_types {
      name = "PERSON_NAME"
    }
    info_types {
      name = "DATE_OF_BIRTH"
    }
    info_types {
      name = "MEDICAL_RECORD_NUMBER"
    }
    info_types {
      name = "PHONE_NUMBER"
    }
    info_types {
      name = "EMAIL_ADDRESS"
    }
    info_types {
      name = "US_SOCIAL_SECURITY_NUMBER"
    }
  }
}

resource "google_data_loss_prevention_deidentify_template" "phi_deidentify" {
  parent       = "projects/${var.project_id}/locations/${var.region}"
  template_id  = "carethread-phi-deidentify-${var.environment}"
  display_name = "CareThread PHI De-identify Template"
  description  = "Replaces targeted PHI infoTypes in residual telemetry strings."

  deidentify_config {
    info_type_transformations {
      transformations {
        primitive_transformation {
          replace_with_info_type_config {}
        }
      }
    }
  }
}

# ---------------------------------------------------------------------------
# Identity Platform & Zero-PHI Fail-Closed Observability Metric
# ---------------------------------------------------------------------------

resource "google_identity_platform_config" "auth" {
  project                    = var.project_id
  autodelete_anonymous_users = true

  sign_in {
    email {
      enabled           = true
      password_required = true
    }
  }
}

resource "google_logging_metric" "sdp_unavailable_fail_closed" {
  name        = "carethread_sdp_unavailable_fail_closed_${var.environment}"
  project     = var.project_id
  description = "Counts fail-closed telemetry redaction drops when SDP is unavailable."
  filter      = "jsonPayload.event_type=\"SDP_UNAVAILABLE_FAIL_CLOSED\""

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
}

output "cmek_crypto_key_id" {
  value = google_kms_crypto_key.cmek.id
}

output "signing_secret_id" {
  value = google_secret_manager_secret.signing_key.secret_id
}

output "capability_secret_id" {
  value = google_secret_manager_secret.capability_hmac.secret_id
}

output "sdp_inspect_template_name" {
  value = google_data_loss_prevention_inspect_template.phi_inspect.id
}

output "sdp_deidentify_template_name" {
  value = google_data_loss_prevention_deidentify_template.phi_deidentify.id
}
