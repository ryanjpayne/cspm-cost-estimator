#!/usr/bin/env python3
"""
CrowdStrike CloudFormation Cost Report Generator

This script orchestrates the complete workflow:
1. Downloads CloudFormation templates from S3
2. Estimates costs for each template
3. Generates a comprehensive cost report

Usage:
    python generate_cost_report.py [--region REGION] [--output REPORT_FILE]
    python generate_cost_report.py --region us-east-1 --output cost_report.md

Requirements:
    pip install pyyaml boto3
"""

import os
import sys
import json
import argparse
import subprocess
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Any
import configparser

# Import functions from existing scripts
try:
    import download_templates
    import estimate_costs
except ImportError as e:
    print(f"Error: Failed to import required modules: {e}")
    print(
        "Make sure download_templates.py and estimate_costs.py are in the same directory."
    )
    sys.exit(1)


class CostReportGenerator:
    """Orchestrates template download, cost estimation, and report generation."""

    def __init__(self, region: str = "us-east-1", output_file: str = "cost_report.md"):
        self.region = region
        self.output_file = output_file
        self.script_dir = os.path.dirname(os.path.abspath(__file__))
        self.config = self._load_config()
        self.templates_dir = os.path.join(
            self.script_dir, self.config.get("output_dir", "templates")
        )
        self.cost_estimates = {}
        self.errors = []

    def _load_config(self) -> Dict[str, Any]:
        """Load configuration from config.ini."""
        config_path = os.path.join(self.script_dir, "config.ini")
        if not os.path.exists(config_path):
            print(f"Warning: config.ini not found at {config_path}")
            return {"output_dir": "templates", "enabled_services": {}}

        config = configparser.ConfigParser()
        config.read(config_path)

        # Load enabled services
        enabled_services = {}
        if config.has_section("enabled_services"):
            for key, value in config.items("enabled_services"):
                enabled_services[key] = value.lower() == "true"

        return {
            "output_dir": config.get("download", "output_dir", fallback="templates"),
            "region": config.get("download", "region", fallback="us-east-1"),
            "enabled_services": enabled_services,
        }

    def step1_download_templates(self) -> bool:
        """Step 1: Download CloudFormation templates."""
        print("\n" + "=" * 80)
        print("STEP 1: DOWNLOADING CLOUDFORMATION TEMPLATES")
        print("=" * 80)

        try:
            # Run download_templates.py
            download_templates.main()
            print("\n✓ Templates downloaded successfully")
            return True
        except Exception as e:
            error_msg = f"Failed to download templates: {e}"
            print(f"\n✗ {error_msg}")
            self.errors.append(error_msg)
            return False

    def step2_estimate_costs(self) -> bool:
        """Step 2: Estimate costs for each template."""
        print("\n" + "=" * 80)
        print("STEP 2: ESTIMATING COSTS FOR EACH TEMPLATE")
        print("=" * 80)

        # Get list of template files
        if not os.path.exists(self.templates_dir):
            error_msg = f"Templates directory not found: {self.templates_dir}"
            print(f"\n✗ {error_msg}")
            self.errors.append(error_msg)
            return False

        template_files = [
            f
            for f in os.listdir(self.templates_dir)
            if f.endswith((".yaml", ".yml", ".json"))
        ]

        if not template_files:
            error_msg = "No template files found in templates directory"
            print(f"\n✗ {error_msg}")
            self.errors.append(error_msg)
            return False

        print(f"\nFound {len(template_files)} template(s) to analyze:")
        for template_file in sorted(template_files):
            print(f"  - {template_file}")

        # Initialize pricing client once for all templates
        pricing_client = estimate_costs.AWSPricingClient(self.region)

        # Estimate costs for each template
        success_count = 0
        for template_file in sorted(template_files):
            template_path = os.path.join(self.templates_dir, template_file)
            print(f"\n{'-' * 80}")
            print(f"Analyzing: {template_file}")
            print(f"{'-' * 80}")

            try:
                # Load template
                template = estimate_costs.load_template(template_path)

                # Extract template metadata
                metadata = template.get("Metadata", {})
                template_info = metadata.get("TemplateInfo", {})
                template_name = template_info.get("Name", "N/A")
                template_description = template_info.get("Description", "N/A")
                template_version = template_info.get("Version", "N/A")

                # Also check top-level Description field
                if template_description == "N/A":
                    template_description = template.get("Description", "N/A")

                # Analyze resources
                resources = estimate_costs.analyze_resources(template)

                # Estimate costs
                cost_estimates = []
                for resource in resources:
                    estimate = estimate_costs.estimate_resource_cost(
                        resource, pricing_client, self.region, template_path
                    )
                    cost_estimates.append(estimate)

                # Store results
                self.cost_estimates[template_file] = {
                    "template_path": template_path,
                    "template_name": template_name,
                    "template_description": template_description,
                    "template_version": template_version,
                    "resource_count": len(resources),
                    "cost_estimates": cost_estimates,
                    "total_fixed_cost": sum(
                        e.get("monthly_cost", 0)
                        for e in cost_estimates
                        if isinstance(e.get("monthly_cost"), (int, float))
                    ),
                }

                print(f"✓ Analyzed {len(resources)} resources")
                success_count += 1

            except Exception as e:
                error_msg = f"Failed to estimate costs for {template_file}: {e}"
                print(f"✗ {error_msg}")
                self.errors.append(error_msg)
                self.cost_estimates[template_file] = {
                    "error": str(e),
                    "resource_count": 0,
                    "cost_estimates": [],
                    "total_fixed_cost": 0.0,
                }

        print(f"\n{'=' * 80}")
        print(
            f"Cost estimation complete: {success_count}/{len(template_files)} templates analyzed successfully"
        )
        print(f"{'=' * 80}")

        return success_count > 0

    def step3_generate_report(self) -> bool:
        """Step 3: Generate comprehensive cost report."""
        print("\n" + "=" * 80)
        print("STEP 3: GENERATING COST REPORT")
        print("=" * 80)

        try:
            report_path = os.path.join(self.script_dir, self.output_file)

            with open(report_path, "w") as f:
                self._write_report_header(f)
                self._write_executive_summary(f)
                self._write_template_details(f)
                self._write_cost_breakdown_by_service(f)
                self._write_recommendations(f)
                self._write_errors_section(f)
                self._write_report_footer(f)

            print(f"\n✓ Report generated successfully: {report_path}")
            return True

        except Exception as e:
            error_msg = f"Failed to generate report: {e}"
            print(f"\n✗ {error_msg}")
            self.errors.append(error_msg)
            return False

    def _write_report_header(self, f):
        """Write report header."""
        f.write("# CrowdStrike AWS CSPM Cost Estimate\n\n")
        f.write(f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        f.write(f"**Region:** {self.region}\n\n")
        f.write(f"**Templates Directory:** {self.templates_dir}\n\n")
        f.write("---\n\n")

    def _write_executive_summary(self, f):
        """Write executive summary section."""
        f.write("## Executive Summary\n\n")

        f.write("- **Scope:** Per Account\n")

        total_templates = len(self.cost_estimates)
        total_resources = sum(
            est.get("resource_count", 0) for est in self.cost_estimates.values()
        )
        total_fixed_cost = sum(
            est.get("total_fixed_cost", 0) for est in self.cost_estimates.values()
        )

        f.write(f"- **Total Templates Analyzed:** {total_templates}\n")
        f.write(f"- **Total Resources:** {total_resources}\n")
        f.write(f"- **Total Fixed Monthly Cost:** ${total_fixed_cost:.2f}\n")

        if self.errors:
            f.write(f"- **Errors Encountered:** {len(self.errors)}\n")

        # Write enabled services section
        enabled_services = self.config.get("enabled_services", {})
        if enabled_services:
            f.write("\n## Enabled Services\n\n")

            # Map config keys to friendly names
            service_names = {
                "ioa_eventbridge": "Realtime Visibility with EventBridge",
                "ioa_s3": "Realtime Visibility with S3",
                "ioa_cloudtrail": "Realtime Visibility with non-mutating events (additional CloudTrail)",
                "1click": "1Click",
                "dspm": "DSPM",
            }

            for key, enabled in enabled_services.items():
                if enabled:
                    friendly_name = service_names.get(key, key)
                    f.write(f"- {friendly_name}\n")

            f.write("\n")

        f.write("## Cost Overview\n\n")
        f.write("| Template | Resources | Fixed Monthly Cost |\n")
        f.write("|----------|-----------|--------------------|\n")

        for template_name in sorted(self.cost_estimates.keys()):
            est = self.cost_estimates[template_name]
            resource_count = est.get("resource_count", 0)
            fixed_cost = est.get("total_fixed_cost", 0.0)

            if "error" in est:
                f.write(f"| {template_name} | Error | Error |\n")
            else:
                f.write(f"| {template_name} | {resource_count} | ${fixed_cost:.2f} |\n")

        f.write("\n")
        f.write(
            "> **Note:** Many resources have usage-based pricing. Actual costs will vary based on:\n"
        )
        f.write("> - Request/invocation volume\n")
        f.write("> - Data transfer and storage amounts\n")
        f.write("> - Free tier eligibility\n")
        f.write("> - Regional pricing variations\n\n")
        f.write("---\n\n")

    def _write_template_details(self, f):
        """Write detailed cost breakdown for each template."""
        f.write("## Detailed Cost Breakdown by Template\n\n")

        for template_name in sorted(self.cost_estimates.keys()):
            est = self.cost_estimates[template_name]

            f.write(f"### {template_name}\n\n")

            if "error" in est:
                f.write(f"**Error:** {est['error']}\n\n")
                continue

            # Write template metadata
            tmpl_name = est.get("template_name", "N/A")
            tmpl_description = est.get("template_description", "N/A")
            tmpl_version = est.get("template_version", "N/A")

            f.write(f"**Name:** {tmpl_name}\n\n")
            f.write(f"**Description:** {tmpl_description}\n\n")
            f.write(f"**Version:** {tmpl_version}\n\n")

            resource_count = est.get("resource_count", 0)
            fixed_cost = est.get("total_fixed_cost", 0.0)
            cost_estimates = est.get("cost_estimates", [])

            f.write(f"**Resources:** {resource_count}\n\n")
            f.write(f"**Fixed Monthly Cost:** ${fixed_cost:.2f}\n\n")

            if cost_estimates:
                f.write("#### Resource Details\n\n")
                f.write(
                    "| Resource Name | Resource Type | Monthly Cost | Unit Cost | Unit | Notes |\n"
                )
                f.write(
                    "|---------------|---------------|--------------|-----------|------|-------|\n"
                )

                for estimate in cost_estimates:
                    name = estimate.get("resource_name", "N/A")
                    rtype = estimate.get("resource_type", "N/A")
                    monthly_cost = estimate.get("monthly_cost", 0.0)
                    unit_cost = estimate.get("unit_cost", 0.0)
                    unit = estimate.get("unit", "N/A")
                    notes = estimate.get("notes", "")

                    monthly_cost_str = (
                        f"${monthly_cost:.2f}"
                        if isinstance(monthly_cost, (int, float))
                        else "N/A"
                    )
                    unit_cost_str = (
                        f"${unit_cost:.4f}"
                        if isinstance(unit_cost, (int, float)) and unit_cost > 0
                        else "Free"
                    )

                    # Escape pipe characters in notes
                    notes = notes.replace("|", "\\|")

                    f.write(
                        f"| {name} | {rtype} | {monthly_cost_str} | {unit_cost_str} | {unit} | {notes} |\n"
                    )

                f.write("\n")

            f.write("---\n\n")

    def _write_cost_breakdown_by_service(self, f):
        """Write cost breakdown grouped by AWS service."""
        f.write("## Cost Breakdown by AWS Service\n\n")

        # Aggregate costs by service across all templates
        service_costs = {}
        service_counts = {}

        for template_name, est in self.cost_estimates.items():
            if "error" in est:
                continue

            for estimate in est.get("cost_estimates", []):
                service = estimate.get("service", "Unknown")
                cost = estimate.get("monthly_cost", 0)

                if service not in service_costs:
                    service_costs[service] = 0.0
                    service_counts[service] = 0

                service_counts[service] += 1
                if isinstance(cost, (int, float)):
                    service_costs[service] += cost

        # Sort by cost (descending)
        sorted_services = sorted(
            service_costs.items(), key=lambda x: x[1], reverse=True
        )

        f.write("| Service | Resource Count | Total Monthly Cost |\n")
        f.write("|---------|----------------|--------------------|\n")

        for service, cost in sorted_services:
            count = service_counts[service]
            cost_str = f"${cost:.2f}" if cost > 0 else "Free/Usage-based"
            f.write(f"| {service} | {count} | {cost_str} |\n")

        f.write("\n---\n\n")

    def _write_recommendations(self, f):
        """Write conditions disclaimer."""
        f.write("## Disclaimers\n\n")

        f.write("### Organizations \n\n")
        f.write("  - This estimate is generated for a single account environment\n")
        f.write(
            "  - In an Organization deployment, all templates except cs_aws_root.yaml will be deployed to each account\n"
        )

        f.write("---\n\n")

        f.write("### Conditional Resources \n\n")
        f.write("  - Some resources are deployed conditionally\n")
        f.write("  - Some Lambda functions are only invoked once during onboarding\n")
        f.write(
            "  - Review CloudFormation parameters and templates before deployment\n\n"
        )

        f.write("---\n\n")

        f.write("### Regional Resources \n\n")
        f.write(
            "  - cs_aws_realtime_visibility_detection_eb.yaml resources are deployed to each region\n"
        )
        f.write(
            "  - EventBridge rules must exist in each region to enable IOAs for each region\n"
        )
        f.write(
            "  - Cost estimate for this template should be assumed per region & per account\n\n"
        )

        f.write("---\n\n")

    def _write_errors_section(self, f):
        """Write errors section if any errors occurred."""
        if not self.errors:
            return

        f.write("## Errors and Warnings\n\n")
        f.write("The following errors were encountered during report generation:\n\n")

        for i, error in enumerate(self.errors, 1):
            f.write(f"{i}. {error}\n")

        f.write("\n---\n\n")

    def _write_report_footer(self, f):
        """Write report footer."""
        f.write("## Important Disclaimers\n\n")
        f.write(f"- Prices shown are based on **{self.region}** region pricing\n")
        f.write("- Actual costs vary by usage patterns and AWS pricing changes\n")
        f.write("- Free tier benefits apply to eligible accounts (first 12 months)\n")
        f.write(
            "- Usage-based resources show $0.00 but will incur costs based on actual usage\n"
        )
        f.write("- Pricing data retrieved from AWS Pricing API\n")
        f.write(
            "- Cross-account data transfer costs are estimated based on typical CloudTrail volumes\n"
        )
        f.write(
            "- Always refer to [AWS Pricing Calculator](https://calculator.aws/) for detailed estimates\n\n"
        )

        f.write("---\n\n")
        f.write(
            f"*Report generated by CrowdStrike CloudFormation Cost Report Generator*\n"
        )

    def run(self) -> bool:
        """Run the complete workflow."""
        print("\n" + "=" * 80)
        print("CROWDSTRIKE CLOUDFORMATION COST REPORT GENERATOR")
        print("=" * 80)
        print(f"Region: {self.region}")
        print(f"Output: {self.output_file}")
        print("=" * 80)

        # Step 1: Download templates
        if not self.step1_download_templates():
            print("\n✗ Workflow failed at Step 1: Template download")
            return False

        # Step 2: Estimate costs
        if not self.step2_estimate_costs():
            print("\n✗ Workflow failed at Step 2: Cost estimation")
            return False

        # Step 3: Generate report
        if not self.step3_generate_report():
            print("\n✗ Workflow failed at Step 3: Report generation")
            return False

        # Success
        print("\n" + "=" * 80)
        print("✓ WORKFLOW COMPLETED SUCCESSFULLY")
        print("=" * 80)
        print(
            f"\nCost report generated: {os.path.join(self.script_dir, self.output_file)}"
        )
        print("\nNext steps:")
        print("  1. Review the cost report")
        print("  2. Validate estimates against your expected usage")
        print("  3. Use AWS Pricing Calculator for detailed scenarios")
        print("=" * 80)

        return True


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Generate comprehensive cost report for CrowdStrike CloudFormation templates"
    )
    parser.add_argument(
        "--region",
        default="us-east-1",
        help="AWS region for pricing (default: us-east-1)",
    )
    parser.add_argument(
        "--output",
        default="cost_report.md",
        help="Output report filename (default: cost_report.md)",
    )

    args = parser.parse_args()

    # Create and run report generator
    generator = CostReportGenerator(region=args.region, output_file=args.output)
    success = generator.run()

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
