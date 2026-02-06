#!/usr/bin/env python3
"""
AWS CloudFormation Resource Cost Estimator

This script estimates the monthly costs for AWS resources defined in CloudFormation templates.
It uses the AWS Pricing API to fetch current pricing information for each resource type.

Usage:
    python estimate_costs.py <template_file> [--region REGION]
    python estimate_costs.py templates/cs_aws_asset_inventory.yaml
    python estimate_costs.py templates/cs_aws_dspm.yaml --region us-west-2

Requirements:
    pip install pyyaml boto3

AWS Credentials:
    This script requires AWS credentials to access the Pricing API.
    Configure credentials using one of these methods:
    - AWS CLI: aws configure
    - Environment variables: AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY
    - IAM role (if running on EC2)
"""

import sys
import yaml
import json
import argparse
from pathlib import Path
from typing import Dict, List, Any, Optional
from decimal import Decimal

try:
    import boto3
    from botocore.exceptions import ClientError, NoCredentialsError
except ImportError:
    print("Error: boto3 is required. Install it with: pip install boto3")
    sys.exit(1)


# Custom YAML loader to handle CloudFormation intrinsic functions
class CfnYamlLoader(yaml.SafeLoader):
    """Custom YAML loader that handles CloudFormation intrinsic functions."""

    pass


def cfn_constructor(loader, node):
    """Generic constructor for CloudFormation intrinsic functions."""
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    elif isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    return None


def cfn_multi_constructor(loader, tag_suffix, node):
    """Multi-constructor for CloudFormation intrinsic functions."""
    if isinstance(node, yaml.ScalarNode):
        return {tag_suffix: loader.construct_scalar(node)}
    elif isinstance(node, yaml.SequenceNode):
        return {tag_suffix: loader.construct_sequence(node)}
    elif isinstance(node, yaml.MappingNode):
        return {tag_suffix: loader.construct_mapping(node)}
    return {tag_suffix: None}


# Register CloudFormation intrinsic functions
cfn_functions = [
    "Ref",
    "Condition",
    "Equals",
    "Not",
    "And",
    "Or",
    "If",
    "FindInMap",
    "GetAtt",
    "GetAZs",
    "ImportValue",
    "Join",
    "Select",
    "Split",
    "Sub",
    "Transform",
    "Base64",
    "Cidr",
]

for func in cfn_functions:
    CfnYamlLoader.add_constructor(f"!{func}", cfn_constructor)

CfnYamlLoader.add_multi_constructor("!", cfn_multi_constructor)


# Resource type to AWS service code mapping
RESOURCE_SERVICE_MAP = {
    # Compute
    "AWS::Lambda::Function": "AWSLambda",
    "AWS::EC2::Instance": "AmazonEC2",
    # Storage
    "AWS::S3::Bucket": "AmazonS3",
    "AWS::DynamoDB::Table": "AmazonDynamoDB",
    # Secrets & Parameters
    "AWS::SecretsManager::Secret": "AWSSecretsManager",
    "AWS::SSM::Parameter": "AWSSystemsManager",
    # Networking
    "AWS::EC2::VPC": "AmazonVPC",
    "AWS::EC2::Subnet": "AmazonVPC",
    "AWS::EC2::InternetGateway": "AmazonVPC",
    "AWS::EC2::RouteTable": "AmazonVPC",
    "AWS::EC2::SecurityGroup": "AmazonVPC",
    "AWS::EC2::NatGateway": "AmazonVPC",
    # IAM (Free)
    "AWS::IAM::Role": None,  # Free service
    "AWS::IAM::Policy": None,
    "AWS::IAM::InstanceProfile": None,
    "AWS::IAM::ManagedPolicy": None,
    # CloudFormation (Free)
    "AWS::CloudFormation::Stack": None,
    "AWS::CloudFormation::CustomResource": None,
    # EventBridge
    "AWS::Events::Rule": "AmazonEventBridge",
    # SNS
    "AWS::SNS::Topic": "AmazonSNS",
    # SQS
    "AWS::SQS::Queue": "AmazonSQS",
    # CloudWatch
    "AWS::Logs::LogGroup": "AmazonCloudWatch",
    "AWS::CloudWatch::Alarm": "AmazonCloudWatch",
}


class PricingCache:
    """Cache for AWS Pricing API results to minimize API calls."""

    def __init__(self):
        self.cache = {}

    def get(self, key):
        return self.cache.get(key)

    def set(self, key, value):
        self.cache[key] = value


class AWSPricingClient:
    """Client for interacting with AWS Pricing API."""

    def __init__(self, region: str = "us-east-1"):
        """Initialize the pricing client.

        Note: Pricing API is only available in us-east-1 and ap-south-1,
        but it returns pricing for all regions.
        """
        self.pricing_region = region
        self.cache = PricingCache()
        try:
            self.client = boto3.client("pricing", region_name="us-east-1")
        except NoCredentialsError:
            print(
                "Error: AWS credentials not found. Please configure credentials using:"
            )
            print("  - AWS CLI: aws configure")
            print("  - Environment variables: AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY")
            sys.exit(1)

    def get_service_pricing(
        self, service_code: str, filters: List[Dict[str, str]]
    ) -> Optional[Dict]:
        """Get pricing information for a service with specific filters."""
        cache_key = f"{service_code}:{json.dumps(filters, sort_keys=True)}"
        cached = self.cache.get(cache_key)
        if cached:
            return cached

        try:
            response = self.client.get_products(
                ServiceCode=service_code, Filters=filters, MaxResults=1
            )

            if response["PriceList"]:
                price_item = json.loads(response["PriceList"][0])
                self.cache.set(cache_key, price_item)
                return price_item
        except ClientError as e:
            print(f"Warning: Failed to get pricing for {service_code}: {e}")
        except Exception as e:
            print(f"Warning: Unexpected error getting pricing for {service_code}: {e}")

        return None

    def extract_price_from_item(self, price_item: Dict) -> Optional[float]:
        """Extract the actual price value from a pricing API response."""
        try:
            terms = price_item.get("terms", {})
            on_demand = terms.get("OnDemand", {})

            if not on_demand:
                return None

            # Get the first price dimension
            for term_key, term_value in on_demand.items():
                price_dimensions = term_value.get("priceDimensions", {})
                for dim_key, dim_value in price_dimensions.items():
                    price_per_unit = dim_value.get("pricePerUnit", {})
                    usd_price = price_per_unit.get("USD")
                    if usd_price:
                        return float(usd_price)
        except Exception as e:
            print(f"Warning: Failed to extract price: {e}")

        return None


def load_template(template_path: str) -> Dict[str, Any]:
    """Load and parse a CloudFormation YAML template."""
    try:
        with open(template_path, "r") as f:
            return yaml.load(f, Loader=CfnYamlLoader)
    except FileNotFoundError:
        print(f"Error: Template file '{template_path}' not found.")
        sys.exit(1)
    except yaml.YAMLError as e:
        print(f"Error: Failed to parse YAML template: {e}")
        sys.exit(1)


def analyze_resources(template: Dict[str, Any]) -> List[Dict[str, str]]:
    """Extract resource information from the template."""
    resources = template.get("Resources", {})
    resource_list = []

    for resource_name, resource_data in resources.items():
        resource_info = {
            "name": resource_name,
            "type": resource_data.get("Type", "N/A"),
            "condition": resource_data.get("Condition", "None"),
            "properties": resource_data.get("Properties", {}),
        }
        resource_list.append(resource_info)

    return resource_list


def estimate_lambda_cost(
    pricing_client: AWSPricingClient, region: str
) -> Dict[str, Any]:
    """Estimate Lambda function costs."""
    # Lambda pricing has multiple components
    filters = [
        {"Type": "TERM_MATCH", "Field": "location", "Value": region},
        {"Type": "TERM_MATCH", "Field": "group", "Value": "AWS-Lambda-Requests"},
    ]

    price_item = pricing_client.get_service_pricing("AWSLambda", filters)
    unit_price = (
        pricing_client.extract_price_from_item(price_item) if price_item else 0.20
    )

    return {
        "monthly_cost": 0.0,
        "unit_cost": unit_price if unit_price else 0.20,
        "unit": "per 1M requests",
        "notes": "Free tier: 1M requests, 400K GB-seconds/month. Pricing varies by memory and duration.",
        "pricing_available": price_item is not None,
    }


def estimate_secrets_manager_cost(
    pricing_client: AWSPricingClient, region: str
) -> Dict[str, Any]:
    """Estimate Secrets Manager costs."""
    filters = [
        {"Type": "TERM_MATCH", "Field": "location", "Value": region},
        {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "Secret"},
    ]

    price_item = pricing_client.get_service_pricing("AWSSecretsManager", filters)
    price = pricing_client.extract_price_from_item(price_item) if price_item else 0.40

    return {
        "monthly_cost": price if price else 0.40,
        "unit_cost": price if price else 0.40,
        "unit": "per secret/month",
        "notes": f"${price if price else 0.40}/secret/month + API call charges",
        "pricing_available": price_item is not None,
    }


def estimate_nat_gateway_cost(
    pricing_client: AWSPricingClient, region: str
) -> Dict[str, Any]:
    """Estimate NAT Gateway costs."""
    filters = [
        {"Type": "TERM_MATCH", "Field": "location", "Value": region},
        {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "NAT Gateway"},
    ]

    price_item = pricing_client.get_service_pricing("AmazonVPC", filters)
    hourly_price = (
        pricing_client.extract_price_from_item(price_item) if price_item else 0.045
    )
    monthly_cost = hourly_price * 730 if hourly_price else 32.85

    return {
        "monthly_cost": monthly_cost,
        "unit_cost": hourly_price if hourly_price else 0.045,
        "unit": "per hour",
        "notes": f"${hourly_price if hourly_price else 0.045}/hour + data processing charges (~${monthly_cost:.2f}/month)",
        "pricing_available": price_item is not None,
    }


def estimate_eventbridge_data_transfer_cost(
    pricing_client: AWSPricingClient,
    region: str,
    properties: Dict[str, Any],
    template_path: str = "",
) -> Dict[str, Any]:
    """Estimate EventBridge data transfer costs for cross-account event delivery."""
    # For CrowdStrike EventBridge templates, assume cross-account delivery
    # These templates are specifically designed to forward events to CrowdStrike's account
    is_cross_account = "realtime_visibility_detection_eb" in template_path.lower()

    if not is_cross_account:
        # For other templates, check if targets suggest cross-account delivery
        targets = properties.get("Targets", [])
        for target in targets:
            target_arn = target.get("Arn", "")
            # Check if target ARN is a string (not a CloudFormation function)
            if isinstance(target_arn, str):
                # Cross-account if ARN contains a different account ID
                if (
                    "arn:aws:events:" in target_arn
                    or "arn:aws-us-gov:events:" in target_arn
                ):
                    is_cross_account = True
                    break
            elif isinstance(target_arn, dict):
                # Handle CloudFormation intrinsic functions that reference external event buses
                if "Ref" in target_arn:
                    ref_param = target_arn.get("Ref", "")
                    if "EventBridge" in ref_param or "EventBus" in ref_param:
                        is_cross_account = True
                        break

    if not is_cross_account:
        return {
            "monthly_cost": 0.0,
            "unit_cost": 0.0,
            "unit": "N/A",
            "notes": "No cross-account data transfer detected",
            "pricing_available": True,
        }

    # Get data transfer out pricing
    # Data transfer pricing uses AmazonEC2 service code with specific filters
    filters = [
        {"Type": "TERM_MATCH", "Field": "location", "Value": region},
        {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "Data Transfer"},
        {"Type": "TERM_MATCH", "Field": "transferType", "Value": "AWS Outbound"},
    ]

    price_item = pricing_client.get_service_pricing("AmazonEC2", filters)

    # Data transfer pricing is tiered, but we'll use first tier (0-10TB) as baseline
    # Typical pricing: $0.01-$0.02 per GB for first 10TB
    price_per_gb = (
        pricing_client.extract_price_from_item(price_item) if price_item else 0.01
    )

    # Estimate based on typical CloudTrail event volume
    # Average CloudTrail event size: ~1-5 KB
    # Typical production account: 10,000 - 1,000,000 events/day
    # Conservative estimate: 100,000 events/day * 2 KB = 200 MB/day = 6 GB/month
    estimated_gb_per_month = 6.0
    estimated_monthly_cost = estimated_gb_per_month * (
        price_per_gb if price_per_gb else 0.01
    )

    return {
        "monthly_cost": estimated_monthly_cost,
        "unit_cost": price_per_gb if price_per_gb else 0.01,
        "unit": "per GB",
        "notes": (
            f"Cross-account data transfer: ${price_per_gb if price_per_gb else 0.01:.4f}/GB. "
            f"Estimated {estimated_gb_per_month} GB/month based on ~100K events/day at 2KB/event. "
            "Actual costs vary significantly based on CloudTrail event volume and event size. "
            "High-activity accounts may see 10-100x higher costs."
        ),
        "pricing_available": price_item is not None,
    }


def estimate_resource_cost(
    resource: Dict[str, str],
    pricing_client: AWSPricingClient,
    region: str,
    template_path: str = "",
) -> Dict[str, Any]:
    """Estimate the monthly cost for a single resource."""
    resource_type = resource["type"]
    resource_name = resource["name"]

    # Get service code for this resource type
    service_code = RESOURCE_SERVICE_MAP.get(resource_type)

    # Handle free services
    if service_code is None:
        return {
            "resource_name": resource_name,
            "resource_type": resource_type,
            "monthly_cost": 0.0,
            "unit_cost": 0.0,
            "unit": "N/A",
            "notes": "No charge for this resource",
            "condition": resource.get("condition", "None"),
            "service": resource_type.split("::")[1] if "::" in resource_type else "AWS",
            "pricing_available": True,
        }

    # Handle special cases with custom pricing logic
    if resource_type == "AWS::Lambda::Function":
        cost_info = estimate_lambda_cost(pricing_client, region)
    elif resource_type == "AWS::SecretsManager::Secret":
        cost_info = estimate_secrets_manager_cost(pricing_client, region)
    elif resource_type == "AWS::EC2::NatGateway":
        cost_info = estimate_nat_gateway_cost(pricing_client, region)
    elif resource_type == "AWS::Events::Rule":
        # EventBridge rules may incur data transfer costs for cross-account delivery
        cost_info = estimate_eventbridge_data_transfer_cost(
            pricing_client, region, resource.get("properties", {}), template_path
        )
    else:
        # Generic pricing lookup
        cost_info = {
            "monthly_cost": 0.0,
            "unit_cost": 0.0,
            "unit": "usage-based",
            "notes": f"Pricing varies by usage. Service: {service_code}",
            "pricing_available": False,
        }

    return {
        "resource_name": resource_name,
        "resource_type": resource_type,
        "monthly_cost": cost_info.get("monthly_cost", 0.0),
        "unit_cost": cost_info.get("unit_cost", 0.0),
        "unit": cost_info.get("unit", "N/A"),
        "notes": cost_info.get("notes", ""),
        "condition": resource.get("condition", "None"),
        "service": service_code,
        "pricing_available": cost_info.get("pricing_available", False),
    }


def print_cost_table(cost_estimates: List[Dict[str, Any]]):
    """Print cost estimates in a formatted table."""
    if not cost_estimates:
        print("No resources found in the template.")
        return

    # Calculate column widths
    name_width = max(len(r["resource_name"]) for r in cost_estimates)
    name_width = max(name_width, len("Resource Name"))
    name_width = min(name_width, 35)

    type_width = max(len(r["resource_type"]) for r in cost_estimates)
    type_width = max(type_width, len("Resource Type"))
    type_width = min(type_width, 40)

    condition_width = max(len(r.get("condition", "None")) for r in cost_estimates)
    condition_width = max(condition_width, len("Condition"))
    condition_width = min(condition_width, 25)

    unit_cost_width = 12
    monthly_cost_width = 15
    unit_width = 20

    # Print header
    separator = f"+{'-' * (name_width + 2)}+{'-' * (type_width + 2)}+{'-' * (condition_width + 2)}+{'-' * (unit_cost_width + 2)}+{'-' * (monthly_cost_width + 2)}+{'-' * (unit_width + 2)}+"
    header = f"| {'Resource Name':<{name_width}} | {'Resource Type':<{type_width}} | {'Condition':<{condition_width}} | {'Unit Cost':<{unit_cost_width}} | {'Monthly Cost':<{monthly_cost_width}} | {'Unit':<{unit_width}} |"

    print(separator)
    print(header)
    print(separator)

    # Print resources
    total_fixed_cost = 0.0
    for estimate in cost_estimates:
        name = estimate["resource_name"][:name_width]
        rtype = estimate["resource_type"][:type_width]
        condition = estimate.get("condition", "None")[:condition_width]

        # Format unit cost
        unit_cost = estimate.get("unit_cost", 0.0)
        if isinstance(unit_cost, (int, float)):
            if unit_cost > 0:
                unit_cost_str = f"${unit_cost:.4f}"
            else:
                unit_cost_str = "Free"
        else:
            unit_cost_str = "N/A"

        # Format monthly cost
        if isinstance(estimate["monthly_cost"], (int, float)):
            monthly_cost_str = f"${estimate['monthly_cost']:.2f}"
            if estimate["monthly_cost"] > 0:
                total_fixed_cost += estimate["monthly_cost"]
        else:
            monthly_cost_str = str(estimate["monthly_cost"])

        unit = estimate["unit"][:unit_width]

        row = f"| {name:<{name_width}} | {rtype:<{type_width}} | {condition:<{condition_width}} | {unit_cost_str:<{unit_cost_width}} | {monthly_cost_str:<{monthly_cost_width}} | {unit:<{unit_width}} |"
        print(row)

    print(separator)
    print(f"\nTotal Fixed Monthly Cost: ${total_fixed_cost:.2f}")
    print("\nNote: Many resources have usage-based pricing. Actual costs depend on:")
    print("  - Request/invocation volume")
    print("  - Data transfer and storage amounts")
    print("  - Free tier eligibility")
    print("  - Regional pricing variations")

    # Print detailed notes
    print("\n" + "=" * 100)
    print("DETAILED COST NOTES:")
    print("=" * 100)

    for estimate in cost_estimates:
        if estimate.get("notes"):
            print(f"\n{estimate['resource_name']} ({estimate['resource_type']}):")
            print(f"  {estimate['notes']}")
            if estimate.get("condition") != "None":
                print(f"  Condition: {estimate['condition']}")
            if not estimate.get("pricing_available", True):
                print(f"  ⚠️  Live pricing data not available - using estimates")


def print_cost_summary_by_service(cost_estimates: List[Dict[str, Any]]):
    """Print cost summary grouped by AWS service."""
    service_costs = {}
    service_counts = {}

    for estimate in cost_estimates:
        service = estimate.get("service", "Unknown")
        cost = estimate.get("monthly_cost", 0)

        if service not in service_costs:
            service_costs[service] = 0.0
            service_counts[service] = 0

        service_counts[service] += 1
        if isinstance(cost, (int, float)):
            service_costs[service] += cost

    print("\n" + "=" * 100)
    print("COST SUMMARY BY SERVICE:")
    print("=" * 100)

    # Sort by cost (descending)
    sorted_services = sorted(service_costs.items(), key=lambda x: x[1], reverse=True)

    print(f"\n{'Service':<30} | {'Resource Count':<15} | {'Monthly Cost':<15}")
    print("-" * 65)

    for service, cost in sorted_services:
        count = service_counts[service]
        cost_str = f"${cost:.2f}" if cost > 0 else "Free/Usage-based"
        print(f"{service:<30} | {count:<15} | {cost_str:<15}")


def main():
    """Main function to estimate CloudFormation resource costs."""
    parser = argparse.ArgumentParser(
        description="Estimate AWS CloudFormation resource costs using AWS Pricing API"
    )
    parser.add_argument("template", help="Path to CloudFormation template file")
    parser.add_argument(
        "--region",
        default="us-east-1",
        help="AWS region for pricing (default: us-east-1)",
    )

    args = parser.parse_args()

    print(f"\nEstimating costs for CloudFormation template: {args.template}")
    print(f"Region: {args.region}")
    print("=" * 100)

    # Initialize pricing client
    print("\nInitializing AWS Pricing API client...")
    pricing_client = AWSPricingClient(args.region)

    # Load and parse template
    template = load_template(args.template)

    # Analyze resources
    resources = analyze_resources(template)

    print(f"\nFound {len(resources)} resources in template")
    print()

    # Estimate costs for each resource
    cost_estimates = []
    for resource in resources:
        estimate = estimate_resource_cost(
            resource, pricing_client, args.region, args.template
        )
        cost_estimates.append(estimate)

    # Print results
    print_cost_table(cost_estimates)
    print_cost_summary_by_service(cost_estimates)

    print("\n" + "=" * 100)
    print("IMPORTANT DISCLAIMERS:")
    print("=" * 100)
    print(f"• Prices shown are based on {args.region} region pricing")
    print("• Actual costs vary by usage patterns and AWS pricing changes")
    print("• Free tier benefits apply to eligible accounts (first 12 months)")
    print(
        "• Usage-based resources show $0.00 but will incur costs based on actual usage"
    )
    print("• Pricing data retrieved from AWS Pricing API")
    print("• Always refer to AWS Pricing Calculator for detailed estimates:")
    print("  https://calculator.aws/")
    print("=" * 100)


if __name__ == "__main__":
    main()
