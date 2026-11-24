CREATE TABLE Patient (
  tenant_id STRING(64) NOT NULL,
  patient_id STRING(128) NOT NULL,
  fhir_patient_id STRING(128) NOT NULL,
  fhir_version_id STRING(64) NOT NULL,
  is_current BOOL NOT NULL DEFAULT (true),
  updated_at TIMESTAMP NOT NULL OPTIONS (allow_commit_timestamp = true)
) PRIMARY KEY (tenant_id, patient_id);

CREATE TABLE Encounter (
  tenant_id STRING(64) NOT NULL,
  encounter_id STRING(128) NOT NULL,
  patient_id STRING(128) NOT NULL,
  fhir_encounter_id STRING(128) NOT NULL,
  fhir_version_id STRING(64) NOT NULL,
  encounter_date DATE NOT NULL,
  specialty STRING(128) NOT NULL,
  composition_fhir_ref STRING(256),
  composition_version_id STRING(64),
  status STRING(32) NOT NULL,
  is_current BOOL NOT NULL DEFAULT (true),
  updated_at TIMESTAMP NOT NULL OPTIONS (allow_commit_timestamp = true)
) PRIMARY KEY (tenant_id, encounter_id);

CREATE INDEX EncounterByPatientDate
  ON Encounter (tenant_id, patient_id, encounter_date DESC)
  STORING (status, specialty, fhir_version_id, composition_fhir_ref, is_current);

CREATE TABLE Practitioner (
  tenant_id STRING(64) NOT NULL,
  practitioner_id STRING(128) NOT NULL,
  fhir_practitioner_id STRING(128) NOT NULL,
  display_name STRING(256) NOT NULL,
  fhir_version_id STRING(64),
  is_current BOOL NOT NULL DEFAULT (true)
) PRIMARY KEY (tenant_id, practitioner_id);

CREATE TABLE Specialty (
  tenant_id STRING(64) NOT NULL,
  specialty_code STRING(64) NOT NULL,
  display_name STRING(256) NOT NULL,
  is_current BOOL NOT NULL DEFAULT (true)
) PRIMARY KEY (tenant_id, specialty_code);

CREATE TABLE Organization (
  tenant_id STRING(64) NOT NULL,
  org_id STRING(128) NOT NULL,
  fhir_org_id STRING(128) NOT NULL,
  display_name STRING(256) NOT NULL,
  fhir_version_id STRING(64),
  is_current BOOL NOT NULL DEFAULT (true)
) PRIMARY KEY (tenant_id, org_id);

CREATE TABLE PractitionerRole (
  tenant_id STRING(64) NOT NULL,
  role_id STRING(128) NOT NULL,
  fhir_role_id STRING(128) NOT NULL,
  practitioner_id STRING(128) NOT NULL,
  specialty_code STRING(64) NOT NULL,
  org_id STRING(128),
  fhir_version_id STRING(64),
  is_current BOOL NOT NULL DEFAULT (true)
) PRIMARY KEY (tenant_id, role_id);

CREATE TABLE MedicationConcept (
  tenant_id STRING(64) NOT NULL,
  rxnorm_code STRING(64) NOT NULL,
  canonical_name STRING(256) NOT NULL,
  is_current BOOL NOT NULL DEFAULT (true)
) PRIMARY KEY (tenant_id, rxnorm_code);

CREATE TABLE MedicationEvent (
  tenant_id STRING(64) NOT NULL,
  med_event_id STRING(128) NOT NULL,
  patient_id STRING(128) NOT NULL,
  encounter_id STRING(128) NOT NULL,
  rxnorm_code STRING(64) NOT NULL,
  medication_name STRING(256) NOT NULL,
  fhir_resource_type STRING(64) NOT NULL,
  fhir_resource_id STRING(128) NOT NULL,
  fhir_version_id STRING(64) NOT NULL,
  medication_status STRING(32) NOT NULL,
  event_date DATE NOT NULL,
  evidence_ref STRING(512) NOT NULL,
  is_current BOOL NOT NULL DEFAULT (true)
) PRIMARY KEY (tenant_id, med_event_id);

CREATE TABLE ProblemEvent (
  tenant_id STRING(64) NOT NULL,
  problem_event_id STRING(128) NOT NULL,
  patient_id STRING(128) NOT NULL,
  encounter_id STRING(128) NOT NULL,
  fhir_resource_id STRING(128) NOT NULL,
  fhir_version_id STRING(64) NOT NULL,
  statement STRING(1024) NOT NULL,
  polarity STRING(32) NOT NULL,
  certainty STRING(64),
  event_date DATE NOT NULL,
  evidence_ref STRING(512) NOT NULL,
  is_current BOOL NOT NULL DEFAULT (true)
) PRIMARY KEY (tenant_id, problem_event_id);

CREATE TABLE HadEncounter (
  tenant_id STRING(64) NOT NULL,
  patient_id STRING(128) NOT NULL,
  encounter_id STRING(128) NOT NULL,
  edge_version_id STRING(64) NOT NULL,
  event_date DATE NOT NULL,
  is_current BOOL NOT NULL DEFAULT (true),
  recorded_at TIMESTAMP NOT NULL OPTIONS (allow_commit_timestamp = true)
) PRIMARY KEY (tenant_id, patient_id, encounter_id, edge_version_id);

CREATE TABLE AttendedBy (
  tenant_id STRING(64) NOT NULL,
  encounter_id STRING(128) NOT NULL,
  role_id STRING(128) NOT NULL,
  edge_version_id STRING(64) NOT NULL,
  is_current BOOL NOT NULL DEFAULT (true)
) PRIMARY KEY (tenant_id, encounter_id, role_id, edge_version_id);

CREATE TABLE FulfilledBy (
  tenant_id STRING(64) NOT NULL,
  role_id STRING(128) NOT NULL,
  practitioner_id STRING(128) NOT NULL,
  edge_version_id STRING(64) NOT NULL,
  is_current BOOL NOT NULL DEFAULT (true)
) PRIMARY KEY (tenant_id, role_id, practitioner_id, edge_version_id);

CREATE TABLE HasSpecialty (
  tenant_id STRING(64) NOT NULL,
  role_id STRING(128) NOT NULL,
  specialty_code STRING(64) NOT NULL,
  edge_version_id STRING(64) NOT NULL,
  is_current BOOL NOT NULL DEFAULT (true)
) PRIMARY KEY (tenant_id, role_id, specialty_code, edge_version_id);

CREATE TABLE BelongsTo (
  tenant_id STRING(64) NOT NULL,
  role_id STRING(128) NOT NULL,
  org_id STRING(128) NOT NULL,
  edge_version_id STRING(64) NOT NULL,
  is_current BOOL NOT NULL DEFAULT (true)
) PRIMARY KEY (tenant_id, role_id, org_id, edge_version_id);

CREATE TABLE RecordedMedEvent (
  tenant_id STRING(64) NOT NULL,
  encounter_id STRING(128) NOT NULL,
  med_event_id STRING(128) NOT NULL,
  edge_version_id STRING(64) NOT NULL,
  event_date DATE NOT NULL,
  is_current BOOL NOT NULL DEFAULT (true)
) PRIMARY KEY (tenant_id, encounter_id, med_event_id, edge_version_id);

CREATE TABLE RefersToConcept (
  tenant_id STRING(64) NOT NULL,
  med_event_id STRING(128) NOT NULL,
  rxnorm_code STRING(64) NOT NULL,
  edge_version_id STRING(64) NOT NULL,
  is_current BOOL NOT NULL DEFAULT (true)
) PRIMARY KEY (tenant_id, med_event_id, rxnorm_code, edge_version_id);

CREATE TABLE RecordedProblem (
  tenant_id STRING(64) NOT NULL,
  encounter_id STRING(128) NOT NULL,
  problem_event_id STRING(128) NOT NULL,
  edge_version_id STRING(64) NOT NULL,
  event_date DATE NOT NULL,
  is_current BOOL NOT NULL DEFAULT (true)
) PRIMARY KEY (tenant_id, encounter_id, problem_event_id, edge_version_id);

CREATE PROPERTY GRAPH CareThreadContinuityGraph
  NODE TABLES (
    Patient,
    Encounter,
    Practitioner,
    PractitionerRole,
    Specialty,
    Organization,
    MedicationEvent,
    MedicationConcept,
    ProblemEvent
  )
  EDGE TABLES (
    HadEncounter
      SOURCE KEY (tenant_id, patient_id) REFERENCES Patient (tenant_id, patient_id)
      DESTINATION KEY (tenant_id, encounter_id) REFERENCES Encounter (tenant_id, encounter_id)
      LABEL HAD_ENCOUNTER,
    AttendedBy
      SOURCE KEY (tenant_id, encounter_id) REFERENCES Encounter (tenant_id, encounter_id)
      DESTINATION KEY (tenant_id, role_id) REFERENCES PractitionerRole (tenant_id, role_id)
      LABEL ATTENDED_BY,
    FulfilledBy
      SOURCE KEY (tenant_id, role_id) REFERENCES PractitionerRole (tenant_id, role_id)
      DESTINATION KEY (tenant_id, practitioner_id) REFERENCES Practitioner (tenant_id, practitioner_id)
      LABEL FULFILLED_BY,
    HasSpecialty
      SOURCE KEY (tenant_id, role_id) REFERENCES PractitionerRole (tenant_id, role_id)
      DESTINATION KEY (tenant_id, specialty_code) REFERENCES Specialty (tenant_id, specialty_code)
      LABEL HAS_SPECIALTY,
    BelongsTo
      SOURCE KEY (tenant_id, role_id) REFERENCES PractitionerRole (tenant_id, role_id)
      DESTINATION KEY (tenant_id, org_id) REFERENCES Organization (tenant_id, org_id)
      LABEL BELONGS_TO,
    RecordedMedEvent
      SOURCE KEY (tenant_id, encounter_id) REFERENCES Encounter (tenant_id, encounter_id)
      DESTINATION KEY (tenant_id, med_event_id) REFERENCES MedicationEvent (tenant_id, med_event_id)
      LABEL RECORDED_MED_EVENT,
    RefersToConcept
      SOURCE KEY (tenant_id, med_event_id) REFERENCES MedicationEvent (tenant_id, med_event_id)
      DESTINATION KEY (tenant_id, rxnorm_code) REFERENCES MedicationConcept (tenant_id, rxnorm_code)
      LABEL REFERS_TO_CONCEPT,
    RecordedProblem
      SOURCE KEY (tenant_id, encounter_id) REFERENCES Encounter (tenant_id, encounter_id)
      DESTINATION KEY (tenant_id, problem_event_id) REFERENCES ProblemEvent (tenant_id, problem_event_id)
      LABEL RECORDED_PROBLEM
  );
