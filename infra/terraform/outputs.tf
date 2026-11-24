output "service_account_emails" {
  description = "Map of dedicated least-privilege service account emails."
  value       = module.iam.service_account_emails
}

output "raw_audio_bucket_name" {
  description = "Private CMEK Cloud Storage bucket for raw synthetic visit audio."
  value       = module.storage.raw_audio_bucket_name
}

output "transcripts_bucket_name" {
  description = "Private CMEK Cloud Storage bucket for verbatim transcripts."
  value       = module.storage.transcripts_bucket_name
}

output "packets_bucket_name" {
  description = "Private CMEK Cloud Storage bucket for sealed allowlisted insurer packets."
  value       = module.storage.packets_bucket_name
}

output "fhir_store_id" {
  description = "Authoritative Cloud Healthcare API FHIR R4 store resource ID."
  value       = module.healthcare_fhir.fhir_store_id
}

output "spanner_instance_name" {
  description = "Spanner Graph Enterprise instance name."
  value       = module.spanner_graph.spanner_instance_name
}

output "spanner_database_name" {
  description = "Spanner Graph continuity database name."
  value       = module.spanner_graph.spanner_database_name
}

output "cloud_run_service_urls" {
  description = "Deployed Cloud Run service URLs."
  value       = module.tasks_and_run.service_urls
}

output "sdp_deidentify_template_name" {
  description = "Sensitive Data Protection de-identification template resource name."
  value       = module.security_obs.sdp_deidentify_template_name
}
