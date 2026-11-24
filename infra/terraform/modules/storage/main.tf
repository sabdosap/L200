variable "project_id" {
  type = string
}

variable "region" {
  type = string
}

variable "environment" {
  type = string
}

variable "kms_crypto_key_id" {
  type = string
}

variable "raw_audio_retention_days" {
  type = number
}

variable "transcript_retention_days" {
  type = number
}

variable "packet_retention_days" {
  type = number
}

variable "service_accounts" {
  type = map(string)
}

variable "labels" {
  type = map(string)
}

resource "google_storage_bucket" "raw_audio" {
  name                        = "${var.project_id}-carethread-raw-audio-${var.environment}"
  project                     = var.project_id
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false
  labels                      = var.labels

  versioning {
    enabled = true
  }

  encryption {
    default_kms_key_name = var.kms_crypto_key_id
  }

  lifecycle_rule {
    condition {
      age = var.raw_audio_retention_days
    }
    action {
      type = "Delete"
    }
  }
}

resource "google_storage_bucket" "transcripts" {
  name                        = "${var.project_id}-carethread-transcripts-${var.environment}"
  project                     = var.project_id
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false
  labels                      = var.labels

  versioning {
    enabled = true
  }

  encryption {
    default_kms_key_name = var.kms_crypto_key_id
  }

  lifecycle_rule {
    condition {
      age = var.transcript_retention_days
    }
    action {
      type = "Delete"
    }
  }
}

resource "google_storage_bucket" "sealed_packets" {
  name                        = "${var.project_id}-carethread-packets-${var.environment}"
  project                     = var.project_id
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false
  labels                      = var.labels

  versioning {
    enabled = true
  }

  encryption {
    default_kms_key_name = var.kms_crypto_key_id
  }

  lifecycle_rule {
    condition {
      age = var.packet_retention_days
    }
    action {
      type = "Delete"
    }
  }
}

# Raw audio bucket: API creates objects via signed URLs; Transcription worker reads them.
# sa-adk-runtime has ZERO permissions on the raw-audio bucket.
resource "google_storage_bucket_iam_member" "raw_audio_api_creator" {
  bucket = google_storage_bucket.raw_audio.name
  role   = "roles/storage.objectCreator"
  member = "serviceAccount:${var.service_accounts["api"]}"
}

resource "google_storage_bucket_iam_member" "raw_audio_transcribe_viewer" {
  bucket = google_storage_bucket.raw_audio.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${var.service_accounts["worker_transcribe"]}"
}

# Transcripts bucket: Transcription worker writes verbatim transcripts.
resource "google_storage_bucket_iam_member" "transcripts_transcribe_creator" {
  bucket = google_storage_bucket.transcripts.name
  role   = "roles/storage.objectCreator"
  member = "serviceAccount:${var.service_accounts["worker_transcribe"]}"
}

# Sealed insurer packets bucket: Release worker writes; API reads for authorized download.
resource "google_storage_bucket_iam_member" "packets_release_creator" {
  bucket = google_storage_bucket.sealed_packets.name
  role   = "roles/storage.objectCreator"
  member = "serviceAccount:${var.service_accounts["worker_release"]}"
}

resource "google_storage_bucket_iam_member" "packets_api_viewer" {
  bucket = google_storage_bucket.sealed_packets.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${var.service_accounts["api"]}"
}

output "raw_audio_bucket_name" {
  value = google_storage_bucket.raw_audio.name
}

output "transcripts_bucket_name" {
  value = google_storage_bucket.transcripts.name
}

output "packets_bucket_name" {
  value = google_storage_bucket.sealed_packets.name
}
