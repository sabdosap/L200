"""Contract tests for CareThread Terraform IaC, Spanner Graph DDL, Cloud Build CI, and secrets hygiene."""

from __future__ import annotations

from pathlib import Path
import re

REPO_ROOT = Path(__file__).resolve().parents[1]
TF_ROOT = REPO_ROOT / "infra" / "terraform"
MODULES_ROOT = TF_ROOT / "modules"


EXPECTED_SERVICE_ACCOUNTS = (
    "sa-api",
    "sa-adk-runtime",
    "sa-worker-transcribe",
    "sa-worker-fhir",
    "sa-worker-graph",
    "sa-worker-release",
)

EXPECTED_SPANNER_NODE_TABLES = (
    "Patient",
    "Encounter",
    "Practitioner",
    "PractitionerRole",
    "Specialty",
    "Organization",
    "MedicationEvent",
    "MedicationConcept",
    "ProblemEvent",
)


def _read_all_tf_text() -> str:
    tf_files = sorted(TF_ROOT.rglob("*.tf"))
    assert tf_files, "Expected Terraform .tf files under infra/terraform/"
    return "\n".join(path.read_text(encoding="utf-8") for path in tf_files)


def test_required_iac_ci_and_docs_files_exist() -> None:
    """All Task 10 Terraform modules, Cloud Build CI, and agents-cli guide files must exist."""
    expected_files = [
        TF_ROOT / "main.tf",
        TF_ROOT / "variables.tf",
        TF_ROOT / "outputs.tf",
        MODULES_ROOT / "iam" / "main.tf",
        MODULES_ROOT / "storage" / "main.tf",
        MODULES_ROOT / "healthcare_fhir" / "main.tf",
        MODULES_ROOT / "spanner_graph" / "main.tf",
        MODULES_ROOT / "spanner_graph" / "schema.sql",
        MODULES_ROOT / "firestore" / "main.tf",
        MODULES_ROOT / "tasks_and_run" / "main.tf",
        MODULES_ROOT / "security_obs" / "main.tf",
        REPO_ROOT / "cloudbuild.yaml",
        REPO_ROOT / "docs" / "build-with-agents-cli.md",
    ]
    missing = [str(path.relative_to(REPO_ROOT)) for path in expected_files if not path.is_file()]
    assert missing == [], f"Missing expected IaC/CI/docs files: {missing}"


def test_terraform_provider_pinned_and_no_hardcoded_project_ids() -> None:
    """Root Terraform pins hashicorp/google ~> 6.0 and avoids hardcoded project IDs."""
    main_tf = (TF_ROOT / "main.tf").read_text(encoding="utf-8")
    assert "hashicorp/google" in main_tf
    assert "~> 6.0" in main_tf

    all_tf = _read_all_tf_text()
    # project_id must come from var.project_id rather than a hardcoded literal string.
    assert not re.search(r'\bproject\s*=\s*"(?!var\.)[a-z0-9-]{4,}"', all_tf)


def test_iam_defines_six_service_accounts_and_restricts_adk_runtime() -> None:
    """IAM defines all 6 SAs and never grants FHIR write or raw-audio read to sa-adk-runtime."""
    iam_tf = (MODULES_ROOT / "iam" / "main.tf").read_text(encoding="utf-8")

    for sa_id in EXPECTED_SERVICE_ACCOUNTS:
        assert f'"{sa_id}"' in iam_tf, f"Service account '{sa_id}' missing from iam/main.tf"

    # Check iam/main.tf lines mentioning adk_runtime (outside comments) never grant forbidden roles.
    for line in iam_tf.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if "adk_runtime" in stripped or "sa-adk-runtime" in stripped:
            assert "roles/healthcare.fhirResourceEditor" not in stripped, (
                "sa-adk-runtime must NEVER receive roles/healthcare.fhirResourceEditor"
            )
            assert "roles/storage.objectViewer" not in stripped, (
                "sa-adk-runtime must NEVER receive roles/storage.objectViewer"
            )

    all_tf = _read_all_tf_text()

    # Ensure sa-adk-runtime is never granted roles/healthcare.fhirResourceEditor
    # or raw-audio roles/storage.objectViewer in any Terraform resource block.
    resource_blocks = re.findall(r'resource\s+"[^"]+"\s+"[^"]+"\s*\{.*?^\}', all_tf, flags=re.MULTILINE | re.DOTALL)
    for block in resource_blocks:
        if "adk_runtime" in block or "sa-adk-runtime" in block:
            assert "roles/healthcare.fhirResourceEditor" not in block, (
                "sa-adk-runtime must NEVER receive roles/healthcare.fhirResourceEditor"
            )
            assert "roles/storage.objectViewer" not in block, (
                "sa-adk-runtime must NEVER receive roles/storage.objectViewer"
            )


def test_healthcare_fhir_store_enforces_r4_versioning_and_referential_integrity() -> None:
    """FHIR R4 store disables update-create, keeps referential integrity, and enables versioning."""
    fhir_tf = (MODULES_ROOT / "healthcare_fhir" / "main.tf").read_text(encoding="utf-8")

    assert "google_healthcare_fhir_store" in fhir_tf
    assert re.search(r'\bversion\s*=\s*"R4"', fhir_tf)
    assert re.search(r"enable_update_create\s*=\s*false", fhir_tf)
    assert re.search(r"disable_referential_integrity\s*=\s*false", fhir_tf)
    assert re.search(r"(disable_resource_versioning\s*=\s*false|enable_resource_versioning\s*=\s*true)", fhir_tf)


def test_spanner_graph_ddl_defines_property_graph_and_all_9_tenant_scoped_nodes() -> None:
    """Spanner Graph DDL defines CareThreadContinuityGraph with all 9 tenant-scoped node tables."""
    schema_sql = (MODULES_ROOT / "spanner_graph" / "schema.sql").read_text(encoding="utf-8")

    assert "CREATE PROPERTY GRAPH CareThreadContinuityGraph" in schema_sql
    for table in EXPECTED_SPANNER_NODE_TABLES:
        table_pattern = re.compile(
            rf"CREATE\s+TABLE\s+{table}\s*\((.*?)\)\s*PRIMARY\s+KEY\s*\(\s*tenant_id\b",
            re.IGNORECASE | re.DOTALL,
        )
        assert table_pattern.search(schema_sql), (
            f"Node table '{table}' must be defined with leading PRIMARY KEY (tenant_id, ...)"
        )
        # Also referenced in NODE TABLES inside CREATE PROPERTY GRAPH
        assert re.search(rf"\b{table}\b", schema_sql.split("CREATE PROPERTY GRAPH", 1)[1])


def test_cloudbuild_runs_evals_golden_suite_and_terraform_validate() -> None:
    """cloudbuild.yaml enforces lint, secret scan, evals/test_golden_suite.py, and terraform validate."""
    cloudbuild = (REPO_ROOT / "cloudbuild.yaml").read_text(encoding="utf-8")

    assert "pytest" in cloudbuild
    assert "evals/test_golden_suite.py" in cloudbuild
    assert "terraform validate" in cloudbuild
    assert "secret" in cloudbuild.lower()


def test_zero_hardcoded_secret_patterns_across_repo() -> None:
    """No hardcoded API keys or private keys exist anywhere in the repository."""
    secret_patterns = (
        re.compile(r"AIza[0-9A-Za-z_\-]{10,}"),
        re.compile(r"\bsk-[0-9A-Za-z]{16,}"),
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    )
    skip_dirs = {".git", ".venv", ".pytest_cache", "__pycache__", ".terraform"}
    findings: list[str] = []

    for path in sorted(REPO_ROOT.rglob("*")):
        if not path.is_file():
            continue
        if any(part in skip_dirs for part in path.parts):
            continue
        content = path.read_text(encoding="utf-8", errors="ignore")
        for pattern in secret_patterns:
            if pattern.search(content):
                findings.append(f"{path.relative_to(REPO_ROOT)} matched {pattern.pattern}")

    assert findings == [], f"Found potential hardcoded secrets: {findings}"
