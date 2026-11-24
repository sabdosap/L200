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

variable "raw_audio_bucket_name" {
  type = string
}

variable "transcripts_bucket_name" {
  type = string
}

variable "packets_bucket_name" {
  type = string
}

variable "fhir_store_id" {
  type = string
}

variable "spanner_instance_name" {
  type = string
}

variable "spanner_database_name" {
  type = string
}

variable "firestore_database_name" {
  type = string
}

variable "sdp_deidentify_template" {
  type = string
}

variable "signing_secret_id" {
  type = string
}

variable "capability_secret_id" {
  type = string
}

variable "enable_argon_reviewer" {
  type = bool
}

variable "api_image" {
  type = string
}

variable "adk_runtime_image" {
  type = string
}

variable "worker_transcribe_image" {
  type = string
}

variable "worker_fhir_image" {
  type = string
}

variable "worker_graph_image" {
  type = string
}

variable "worker_release_image" {
  type = string
}

variable "labels" {
  type = map(string)
}

# ---------------------------------------------------------------------------
# Cloud Tasks Queues (bounded retries + Stackdriver logging)
# ---------------------------------------------------------------------------

locals {
  queues = {
    transcribe_and_draft      = "carethread-transcribe-and-draft-${var.environment}"
    commit_fhir_record        = "carethread-commit-fhir-record-${var.environment}"
    refresh_continuity_graph  = "carethread-refresh-continuity-graph-${var.environment}"
    assemble_insurer_packet   = "carethread-assemble-insurer-packet-${var.environment}"
  }
}

resource "google_cloud_tasks_queue" "queues" {
  for_each = local.queues
  name     = each.value
  location = var.region
  project  = var.project_id

  rate_limits {
    max_dispatches_per_second = 10
    max_concurrent_dispatches = 20
  }

  retry_config {
    max_attempts  = 5
    min_backoff   = "5s"
    max_backoff   = "120s"
    max_doublings = 4
  }

  stackdriver_logging_config {
    sampling_ratio = 1.0
  }
}

# ---------------------------------------------------------------------------
# Cloud Run v2 Services (dedicated service account per component)
# ---------------------------------------------------------------------------

resource "google_cloud_run_v2_service" "api" {
  name     = "carethread-api-${var.environment}"
  location = var.region
  project  = var.project_id
  ingress  = "INGRESS_TRAFFIC_ALL"
  labels   = var.labels

  template {
    service_account = var.service_accounts["api"]

    containers {
      image = var.api_image

      env {
        name  = "CARETHREAD_PROJECT_ID"
        value = var.project_id
      }
      env {
        name  = "CARETHREAD_REGION"
        value = var.region
      }
      env {
        name  = "CARETHREAD_RAW_AUDIO_BUCKET"
        value = var.raw_audio_bucket_name
      }
      env {
        name  = "CARETHREAD_PACKETS_BUCKET"
        value = var.packets_bucket_name
      }
      env {
        name  = "CARETHREAD_FIRESTORE_DB"
        value = var.firestore_database_name
      }
      env {
        name  = "CARETHREAD_SDP_TEMPLATE"
        value = var.sdp_deidentify_template
      }
      env {
        name = "CARETHREAD_CAPABILITY_HMAC_SECRET"
        value_source {
          secret_key_ref {
            secret  = var.capability_secret_id
            version = "latest"
          }
        }
      }
    }
  }
}

resource "google_cloud_run_v2_service" "adk_runtime" {
  name     = "carethread-adk-runtime-${var.environment}"
  location = var.region
  project  = var.project_id
  ingress  = "INGRESS_TRAFFIC_INTERNAL_ONLY"
  labels   = var.labels

  template {
    service_account = var.service_accounts["adk_runtime"]

    containers {
      image = var.adk_runtime_image

      env {
        name  = "CARETHREAD_PROJECT_ID"
        value = var.project_id
      }
      env {
        name  = "CARETHREAD_REGION"
        value = var.region
      }
      env {
        name  = "CARETHREAD_FHIR_STORE_ID"
        value = var.fhir_store_id
      }
      env {
        name  = "CARETHREAD_SPANNER_INSTANCE"
        value = var.spanner_instance_name
      }
      env {
        name  = "CARETHREAD_SPANNER_DATABASE"
        value = var.spanner_database_name
      }
      env {
        name  = "CARETHREAD_FIRESTORE_DB"
        value = var.firestore_database_name
      }
      env {
        name  = "CARETHREAD_SDP_TEMPLATE"
        value = var.sdp_deidentify_template
      }
      env {
        name  = "CARETHREAD_ENABLE_ARGON_REVIEWER"
        value = tostring(var.enable_argon_reviewer)
      }
    }
  }
}

resource "google_cloud_run_v2_service" "worker_transcribe" {
  name     = "carethread-worker-transcribe-${var.environment}"
  location = var.region
  project  = var.project_id
  ingress  = "INGRESS_TRAFFIC_INTERNAL_ONLY"
  labels   = var.labels

  template {
    service_account = var.service_accounts["worker_transcribe"]

    containers {
      image = var.worker_transcribe_image

      env {
        name  = "CARETHREAD_RAW_AUDIO_BUCKET"
        value = var.raw_audio_bucket_name
      }
      env {
        name  = "CARETHREAD_TRANSCRIPTS_BUCKET"
        value = var.transcripts_bucket_name
      }
      env {
        name  = "CARETHREAD_SDP_TEMPLATE"
        value = var.sdp_deidentify_template
      }
    }
  }
}

resource "google_cloud_run_v2_service" "worker_fhir" {
  name     = "carethread-worker-fhir-${var.environment}"
  location = var.region
  project  = var.project_id
  ingress  = "INGRESS_TRAFFIC_INTERNAL_ONLY"
  labels   = var.labels

  template {
    service_account = var.service_accounts["worker_fhir"]

    containers {
      image = var.worker_fhir_image

      env {
        name  = "CARETHREAD_FHIR_STORE_ID"
        value = var.fhir_store_id
      }
      env {
        name  = "CARETHREAD_FIRESTORE_DB"
        value = var.firestore_database_name
      }
      env {
        name  = "CARETHREAD_SDP_TEMPLATE"
        value = var.sdp_deidentify_template
      }
      env {
        name = "CARETHREAD_SIGNING_SECRET"
        value_source {
          secret_key_ref {
            secret  = var.signing_secret_id
            version = "latest"
          }
        }
      }
    }
  }
}

resource "google_cloud_run_v2_service" "worker_graph" {
  name     = "carethread-worker-graph-${var.environment}"
  location = var.region
  project  = var.project_id
  ingress  = "INGRESS_TRAFFIC_INTERNAL_ONLY"
  labels   = var.labels

  template {
    service_account = var.service_accounts["worker_graph"]

    containers {
      image = var.worker_graph_image

      env {
        name  = "CARETHREAD_FHIR_STORE_ID"
        value = var.fhir_store_id
      }
      env {
        name  = "CARETHREAD_SPANNER_INSTANCE"
        value = var.spanner_instance_name
      }
      env {
        name  = "CARETHREAD_SPANNER_DATABASE"
        value = var.spanner_database_name
      }
      env {
        name  = "CARETHREAD_SDP_TEMPLATE"
        value = var.sdp_deidentify_template
      }
    }
  }
}

resource "google_cloud_run_v2_service" "worker_release" {
  name     = "carethread-worker-release-${var.environment}"
  location = var.region
  project  = var.project_id
  ingress  = "INGRESS_TRAFFIC_INTERNAL_ONLY"
  labels   = var.labels

  template {
    service_account = var.service_accounts["worker_release"]

    containers {
      image = var.worker_release_image

      env {
        name  = "CARETHREAD_FHIR_STORE_ID"
        value = var.fhir_store_id
      }
      env {
        name  = "CARETHREAD_PACKETS_BUCKET"
        value = var.packets_bucket_name
      }
      env {
        name  = "CARETHREAD_SDP_TEMPLATE"
        value = var.sdp_deidentify_template
      }
    }
  }
}

# Least-privilege Cloud Run service-to-service invoker bindings.
resource "google_cloud_run_v2_service_iam_member" "api_invokes_adk" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.adk_runtime.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${var.service_accounts["api"]}"
}

resource "google_cloud_run_v2_service_iam_member" "transcribe_invokes_adk" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.adk_runtime.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${var.service_accounts["worker_transcribe"]}"
}

resource "google_cloud_run_v2_service_iam_member" "api_invokes_transcribe" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.worker_transcribe.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${var.service_accounts["api"]}"
}

resource "google_cloud_run_v2_service_iam_member" "api_invokes_fhir_worker" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.worker_fhir.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${var.service_accounts["api"]}"
}

resource "google_cloud_run_v2_service_iam_member" "fhir_invokes_graph_worker" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.worker_graph.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${var.service_accounts["worker_fhir"]}"
}

resource "google_cloud_run_v2_service_iam_member" "api_invokes_release_worker" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.worker_release.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${var.service_accounts["api"]}"
}

output "service_urls" {
  value = {
    api               = google_cloud_run_v2_service.api.uri
    adk_runtime       = google_cloud_run_v2_service.adk_runtime.uri
    worker_transcribe = google_cloud_run_v2_service.worker_transcribe.uri
    worker_fhir       = google_cloud_run_v2_service.worker_fhir.uri
    worker_graph      = google_cloud_run_v2_service.worker_graph.uri
    worker_release    = google_cloud_run_v2_service.worker_release.uri
  }
}
