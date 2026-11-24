variable "project_id" {
  type = string
}

locals {
  service_accounts = {
    api = {
      account_id   = "sa-api"
      display_name = "CareThread API Gateway"
      description  = "Deterministic AuthZ, consent verification, signed upload URLs, and Cloud Tasks dispatch."
    }
    adk_runtime = {
      account_id   = "sa-adk-runtime"
      display_name = "CareThread ADK Orchestration Runtime"
      description  = "ADK Coordinator & sequential pipeline. Zero FHIR write and zero raw-audio read access."
    }
    worker_transcribe = {
      account_id   = "sa-worker-transcribe"
      display_name = "CareThread Transcription Worker"
      description  = "Reads raw audio, invokes gemini-3.5-transcribe / chirp_3, writes immutable transcript."
    }
    worker_fhir = {
      account_id   = "sa-worker-fhir"
      display_name = "CareThread Deterministic FHIR Writer"
      description  = "Sole service identity permitted to commit signed resources to FHIR R4."
    }
    worker_graph = {
      account_id   = "sa-worker-graph"
      display_name = "CareThread Spanner Graph Projection Worker"
      description  = "Reads signed FHIR R4 resources and upserts derived Spanner Graph continuity nodes/edges."
    }
    worker_release = {
      account_id   = "sa-worker-release"
      display_name = "CareThread Insurer Packet Assembler Worker"
      description  = "Reads allowlisted signed FHIR R4 resources and writes sealed insurer packets."
    }
  }
}

resource "google_service_account" "carethread" {
  for_each     = local.service_accounts
  project      = var.project_id
  account_id   = each.value.account_id
  display_name = each.value.display_name
  description  = each.value.description
}

# ---------------------------------------------------------------------------
# Project-level least-privilege bindings (explicitly excluding FHIR write and
# raw-audio read from sa-adk-runtime)
# ---------------------------------------------------------------------------

locals {
  project_roles = {
    api_datastore = { sa = "api", role = "roles/datastore.user" }
    api_tasks     = { sa = "api", role = "roles/cloudtasks.enqueuer" }
    api_dlp       = { sa = "api", role = "roles/dlp.user" }
    api_logging   = { sa = "api", role = "roles/logging.logWriter" }
    api_trace     = { sa = "api", role = "roles/cloudtrace.agent" }

    # sa-adk-runtime: Vertex AI inference, FHIR read-only fallback, Spanner read-only,
    # filtered Firestore sessions, SDP redaction, and telemetry.
    # NEVER granted roles/healthcare.fhirResourceEditor or raw-audio roles/storage.objectViewer.
    adk_runtime_ai      = { sa = "adk_runtime", role = "roles/aiplatform.user" }
    adk_runtime_fhir_ro = { sa = "adk_runtime", role = "roles/healthcare.fhirResourceReader" }
    adk_runtime_spanner = { sa = "adk_runtime", role = "roles/spanner.databaseReader" }
    adk_runtime_fs      = { sa = "adk_runtime", role = "roles/datastore.user" }
    adk_runtime_dlp     = { sa = "adk_runtime", role = "roles/dlp.user" }
    adk_runtime_logging = { sa = "adk_runtime", role = "roles/logging.logWriter" }
    adk_runtime_trace   = { sa = "adk_runtime", role = "roles/cloudtrace.agent" }

    transcribe_ai      = { sa = "worker_transcribe", role = "roles/aiplatform.user" }
    transcribe_speech  = { sa = "worker_transcribe", role = "roles/speech.client" }
    transcribe_fs      = { sa = "worker_transcribe", role = "roles/datastore.user" }
    transcribe_dlp     = { sa = "worker_transcribe", role = "roles/dlp.user" }
    transcribe_logging = { sa = "worker_transcribe", role = "roles/logging.logWriter" }
    transcribe_trace   = { sa = "worker_transcribe", role = "roles/cloudtrace.agent" }

    fhir_fs      = { sa = "worker_fhir", role = "roles/datastore.user" }
    fhir_tasks   = { sa = "worker_fhir", role = "roles/cloudtasks.enqueuer" }
    fhir_dlp     = { sa = "worker_fhir", role = "roles/dlp.user" }
    fhir_logging = { sa = "worker_fhir", role = "roles/logging.logWriter" }
    fhir_trace   = { sa = "worker_fhir", role = "roles/cloudtrace.agent" }

    graph_fs      = { sa = "worker_graph", role = "roles/datastore.user" }
    graph_dlp     = { sa = "worker_graph", role = "roles/dlp.user" }
    graph_logging = { sa = "worker_graph", role = "roles/logging.logWriter" }
    graph_trace   = { sa = "worker_graph", role = "roles/cloudtrace.agent" }

    release_fs      = { sa = "worker_release", role = "roles/datastore.user" }
    release_dlp     = { sa = "worker_release", role = "roles/dlp.user" }
    release_logging = { sa = "worker_release", role = "roles/logging.logWriter" }
    release_trace   = { sa = "worker_release", role = "roles/cloudtrace.agent" }
  }
}

resource "google_project_iam_member" "least_privilege" {
  for_each = local.project_roles
  project  = var.project_id
  role     = each.value.role
  member   = "serviceAccount:${google_service_account.carethread[each.value.sa].email}"
}

output "service_account_emails" {
  value = { for k, sa in google_service_account.carethread : k => sa.email }
}
