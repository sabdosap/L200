variable "project_id" {
  description = "Target Google Cloud project ID (supplied at plan/apply time; never hardcoded)."
  type        = string
}

variable "region" {
  description = "Primary Google Cloud region for co-located clinical stores, ADK runtime, and SDP."
  type        = string
  default     = "us-central1"
}

variable "environment" {
  description = "Deployment environment label (e.g., mvp, staging)."
  type        = string
  default     = "mvp"
}

variable "spanner_processing_units" {
  description = "Cloud Spanner Enterprise processing units (100 minimum for Spanner Graph)."
  type        = number
  default     = 100
}

variable "enable_argon_reviewer" {
  description = "Feature flag enabling eval-gated gemini-4-argon conflict second-opinion escalation."
  type        = bool
  default     = false
}

variable "raw_audio_retention_days" {
  description = "Retention window in days for raw synthetic visit audio in private CMEK GCS."
  type        = number
  default     = 30
}

variable "transcript_retention_days" {
  description = "Retention window in days for immutable verbatim transcript JSON artifacts."
  type        = number
  default     = 90
}

variable "packet_retention_days" {
  description = "Retention window in days for sealed allowlisted insurer packets."
  type        = number
  default     = 30
}

variable "kms_key_rotation_period" {
  description = "Automatic CMEK rotation period."
  type        = string
  default     = "7776000s"
}

variable "api_image" {
  description = "Container image URI for CareThread API Gateway Cloud Run service."
  type        = string
  default     = "us-docker.pkg.dev/cloudrun/container/hello"
}

variable "adk_runtime_image" {
  description = "Container image URI for CareThread ADK Orchestration Runtime Cloud Run service."
  type        = string
  default     = "us-docker.pkg.dev/cloudrun/container/hello"
}

variable "worker_transcribe_image" {
  description = "Container image URI for Transcription Worker Cloud Run service."
  type        = string
  default     = "us-docker.pkg.dev/cloudrun/container/hello"
}

variable "worker_fhir_image" {
  description = "Container image URI for Deterministic FHIR Writer Worker Cloud Run service."
  type        = string
  default     = "us-docker.pkg.dev/cloudrun/container/hello"
}

variable "worker_graph_image" {
  description = "Container image URI for Spanner Graph Projection Worker Cloud Run service."
  type        = string
  default     = "us-docker.pkg.dev/cloudrun/container/hello"
}

variable "worker_release_image" {
  description = "Container image URI for Insurer Packet Assembler Worker Cloud Run service."
  type        = string
  default     = "us-docker.pkg.dev/cloudrun/container/hello"
}

variable "labels" {
  description = "Standard resource labels applied across CareThread infrastructure."
  type        = map(string)
  default = {
    app                 = "carethread"
    data_classification = "synthetic-mvp"
    managed_by          = "terraform"
  }
}
