# CrowdStrike AWS CSPM Cost Estimator

This directory contains tools for working with CrowdStrike CloudFormation templates.

## Tools

### 1. generate_cost_report.py (Automated Workflow)

Automated workflow that orchestrates the complete process: downloads templates, estimates costs for all templates, and generates a comprehensive cost report.

**Usage:**
```bash
python generate_cost_report.py [--region REGION] [--output REPORT_FILE]
```

**Examples:**
```bash
# Generate report with default settings (us-east-1, cost_report.md)
python generate_cost_report.py

# Generate report for a specific region
python generate_cost_report.py --region us-west-2

# Generate report with custom output filename
python generate_cost_report.py --output my_cost_analysis.md
```

**What it does:**
1. **Step 1:** Downloads all CloudFormation templates from S3 (calls `download_templates.py`)
2. **Step 2:** Estimates costs for each downloaded template (calls `estimate_costs.py` for each)
3. **Step 3:** Generates a comprehensive Markdown report with:
   - Executive summary with total costs across all templates
   - Detailed cost breakdown for each template
   - Cost aggregation by AWS service
   - Cost optimization recommendations
   - Important disclaimers and notes

**Report Contents:**
- **Executive Summary:** Overview of all templates, total resources, and total fixed monthly costs
- **Cost Overview Table:** Quick reference showing costs per template
- **Detailed Breakdown:** Resource-level details for each template
- **Service Aggregation:** Costs grouped by AWS service across all templates
- **Recommendations:** Cost optimization tips and best practices
- **Disclaimers:** Important notes about pricing accuracy and usage-based costs

**Sample Report Output:**
```markdown
# CrowdStrike CloudFormation Cost Estimation Report

**Generated:** 2026-02-06 10:00:00
**Region:** us-east-1

## Executive Summary

- **Total Templates Analyzed:** 8
- **Total Resources:** 45
- **Total Fixed Monthly Cost:** $3.20

### Cost Overview

| Template | Resources | Fixed Monthly Cost |
|----------|-----------|--------------------|
| cs_aws_1_click_sensor_management.yaml | 5 | $0.40 |
| cs_aws_asset_inventory.yaml | 1 | $0.00 |
| cs_aws_dspm.yaml | 10 | $0.40 |
| cs_aws_dspm_env.yaml | 33 | $32.85 |
| cs_aws_realtime_visibility_detection.yaml | 2 | $0.00 |
| cs_aws_realtime_visibility_detection_eb.yaml | 2 | $0.12 |
| cs_aws_realtime_visibility_detection_s3.yaml | 5 | $0.00 |
| cs_aws_root.yaml | 21 | $0.00 |
...
```

**Requirements:**
- AWS credentials configured (for Pricing API access)
- Python packages: `pyyaml`, `boto3`, `requests`
- `config.ini` file properly configured

---

### 2. estimate_costs.py

Estimates monthly costs for AWS resources defined in CloudFormation templates using the AWS Pricing API.

**Usage:**
```bash
python estimate_costs.py <template_file> [--region REGION]
```

**Example:**
```bash
python estimate_costs.py templates/cs_aws_asset_inventory.yaml
python estimate_costs.py templates/cs_aws_dspm.yaml --region us-west-2
```

**AWS Credentials:**
This script requires AWS credentials to access the Pricing API. Configure credentials using:
- AWS CLI: `aws configure`
- Environment variables: `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`
- IAM role (if running on EC2)

**Features:**
- Queries AWS Pricing API for real-time, accurate pricing data
- Supports regional pricing (specify with `--region` flag)
- Analyzes CloudFormation resources and estimates monthly costs
- Displays cost breakdown by resource with pricing units
- Groups costs by AWS service
- Shows detailed pricing notes including free tier information
- Identifies conditional resources
- Caches API results to minimize API calls
- Provides disclaimers about usage-based pricing

**Sample Output:**
```
+--------------------------------+-------------------------------------+-----------------+----------------------+
| Resource Name                  | Resource Type                       | Monthly Cost    | Unit                 |
+--------------------------------+-------------------------------------+-----------------+----------------------+
| ClientSecrets                  | AWS::SecretsManager::Secret         | $0.40           | per secret/month     |
| CreateEnvironmentLambda        | AWS::Lambda::Function               | $0.00           | per invocation       |
| CrowdStrikeAWSIntegrationRole  | AWS::IAM::Role                      | $0.00           | N/A                  |
+--------------------------------+-------------------------------------+-----------------+----------------------+

Total Fixed Monthly Cost: $0.40
```

**Important Notes:**
- Pricing data is retrieved in real-time from AWS Pricing API
- Prices reflect the specified region (default: us-east-1)
- Many resources show $0.00 because they're free or usage-based
- Actual costs depend on usage patterns, data transfer, and regional pricing
- Free tier benefits apply to eligible AWS accounts
- Resources marked with ⚠️ use fallback estimates when API data is unavailable
- Always refer to AWS Pricing Calculator for detailed estimates


## Requirements

Install required Python packages:

```bash
pip install pyyaml requests boto3
```

Or if using a virtual environment:

```bash
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install pyyaml requests boto3
```

**Note:** `boto3` is required for the cost estimation tool to access the AWS Pricing API.

## Directory Structure

```
crowdstrike-infra/aws/
├── README.md                    # This file
├── config.ini                   # Configuration file for download_templates.py
├── estimate_costs.py            # Cost estimation and analysis tool
├── download_templates.py        # Template download tool
└── templates/                   # Downloaded templates directory
    ├── cs_aws_root.yaml
    ├── cs_aws_asset_inventory.yaml
    ├── cs_aws_dspm.yaml
    └── ...
```

### 3. download_templates.py

Downloads CrowdStrike CloudFormation templates from S3.

**Usage:**
```bash
python download_templates.py
```

**Configuration:**

The script uses a `config.ini` file for configuration. Create or modify this file to customize the download behavior:

```ini
[download]
# Root template URL
root_template_url = https://cs-prod-cloudconnect-templates.s3-us-west-1.amazonaws.com/modular/cs_aws_root.yaml

# AWS Region to use for template downloads
region = us-east-1

# Output directory for downloaded templates
output_dir = templates

[additional_templates]
# Additional templates to download (not referenced in root template)
# These use the same URL pattern with variable substitution
# Format: one URL per line
templates = 
    https://cs-${Environment}-cloudconnect-templates-${RegionPrefix}-${BucketRegionId}.s3.${AWS::Region}.amazonaws.com/modular/cs_aws_dspm_env.yaml

[region_mappings]
# Region mappings for template URL resolution
# Format: region = prefix,bucket_region_id
us-east-1 = use1,4721cdb0
us-east-2 = use2,0c19f3dd
us-west-1 = usw1,810f8878
us-west-2 = usw2,bce32e5c
```

**Configuration Options:**

- `root_template_url`: The URL of the root CloudFormation template
- `region`: AWS region to use for downloading templates
- `output_dir`: Directory where templates will be saved
- `additional_templates`: List of additional template URLs to download (supports CloudFormation variable substitution)
- `region_mappings`: Mapping of AWS regions to their prefix and bucket region ID

**Features:**
- Downloads root template and all child templates
- Supports additional templates not referenced in root
- Configurable via config.ini file

## Workflow Examples

### Automated Workflow (Recommended)

The easiest way to analyze all CrowdStrike CloudFormation templates:

```bash
# Generate complete cost report for all templates
python generate_cost_report.py

# Generate report for a specific region
python generate_cost_report.py --region us-west-2 --output cost_report_usw2.md
```

This single command will:
1. Download all templates from S3
2. Estimate costs for each template
3. Generate a comprehensive Markdown report

### Manual Workflow

For analyzing individual templates or custom workflows:

1. **Download templates:**
   ```bash
   python download_templates.py
   ```

2. **Analyze a specific template:**
   ```bash
   python estimate_costs.py templates/cs_aws_dspm.yaml
   ```

3. **Analyze multiple templates:**
   ```bash
   python estimate_costs.py templates/cs_aws_asset_inventory.yaml
   python estimate_costs.py templates/cs_aws_dspm.yaml
   python estimate_costs.py templates/cs_aws_realtime_visibility_detection.yaml
   ```

The estimate_costs.py tool provides comprehensive analysis including:
- Resource listing with types and conditions
- Cost estimates with unit pricing
- Service-level cost summaries
- Detailed pricing notes

These workflows help you understand what resources will be deployed and their estimated monthly costs before deploying the CloudFormation stack.
