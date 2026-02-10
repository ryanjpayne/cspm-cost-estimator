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
import configparser
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
    "AWS::SSM::Parameter": None,  # Free for standard parameters (up to 10K params, 4KB max, 40 TPS)
    # Networking
    "AWS::EC2::VPC": None,  # Free - VPCs themselves have no charge
    "AWS::EC2::Subnet": None,  # Free - Subnets have no charge
    "AWS::EC2::InternetGateway": None,  # Free - Internet Gateways have no charge
    "AWS::EC2::RouteTable": None,  # Free - Route Tables have no charge
    "AWS::EC2::Route": None,  # Free - Routes have no charge
    "AWS::EC2::SecurityGroup": None,  # Free - Security Groups have no charge
    "AWS::EC2::NetworkAcl": None,  # Free - Network ACLs have no charge
    "AWS::EC2::NetworkAclEntry": None,  # Free - NACL entries have no charge
    "AWS::EC2::VPCGatewayAttachment": None,  # Free - Gateway attachments have no charge
    "AWS::EC2::SubnetRouteTableAssociation": None,  # Free - Route table associations have no charge
    "AWS::EC2::SubnetNetworkAclAssociation": None,  # Free - NACL associations have no charge
    "AWS::EC2::VPCEndpoint": None,  # Free for Gateway endpoints (S3, DynamoDB)
    "AWS::EC2::EIP": None,  # Free when attached, charged when unattached
    "AWS::EC2::NatGateway": "AmazonVPC",  # Charged resource
    "AWS::RDS::DBSubnetGroup": None,  # Free - DB Subnet Groups have no charge
    "AWS::Redshift::ClusterSubnetGroup": None,  # Free - Redshift Subnet Groups have no charge
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
    "AWS::SNS::Subscription": None,  # Free - subscriptions themselves have no charge (delivery costs apply)
    # SQS
    "AWS::SQS::Queue": "AmazonSQS",
    "AWS::SQS::QueuePolicy": None,  # Free - policies have no charge
    # KMS
    "AWS::KMS::Key": "awskms",
    "AWS::KMS::Alias": None,  # Free - aliases have no charge
    # CloudWatch
    "AWS::Logs::LogGroup": "AmazonCloudWatch",
    "AWS::CloudWatch::Alarm": "AmazonCloudWatch",
    # CloudTrail
    "AWS::CloudTrail::Trail": "AWSCloudTrail",
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


def load_config(config_path: str = "aws/config.ini") -> Dict[str, Any]:
    """Load configuration from config.ini file."""
    config = configparser.ConfigParser()
    try:
        config.read(config_path)
        enabled_services = {}
        if config.has_section("enabled_services"):
            for key, value in config.items("enabled_services"):
                enabled_services[key] = value.lower() == "true"
        return enabled_services
    except Exception as e:
        print(f"Warning: Could not load config.ini: {e}")
        return {}


def should_include_template(
    template_name: str, enabled_services: Dict[str, bool]
) -> bool:
    """Determine if a template should be included based on enabled services."""
    # Always include these templates
    if template_name in ["cs_aws_asset_inventory.yaml", "cs_aws_root.yaml"]:
        return True

    # Conditional templates
    template_conditions = {
        "cs_aws_1_click_sensor_management.yaml": enabled_services.get("1click", False),
        "cs_aws_dspm_env.yaml": enabled_services.get("dspm", False),
        "cs_aws_dspm.yaml": enabled_services.get("dspm", False),
        "cs_aws_realtime_visibility_detection_eb.yaml": enabled_services.get(
            "ioa_eventbridge", False
        ),
        "cs_aws_realtime_visibility_detection_s3.yaml": enabled_services.get(
            "ioa_s3", False
        ),
        "cs_aws_realtime_visibility_detection.yaml": enabled_services.get(
            "ioa_eventbridge", False
        )
        or enabled_services.get("ioa_s3", False),
    }

    return template_conditions.get(template_name, False)


def should_include_resource(
    resource_name: str,
    resource_type: str,
    template_name: str,
    enabled_services: Dict[str, bool],
) -> bool:
    """Determine if a specific resource should be included based on enabled services."""
    # Special case: CloudTrail in cs_aws_realtime_visibility_detection.yaml
    if (
        resource_type == "AWS::CloudTrail::Trail"
        and "realtime_visibility_detection.yaml" in template_name
    ):
        return enabled_services.get("ioa_cloudtrail", False)

    # By default, include all resources if the template is included
    return True


def analyze_resources(
    template: Dict[str, Any],
    template_name: str = "",
    enabled_services: Dict[str, bool] = None,
) -> List[Dict[str, str]]:
    """Extract resource information from the template."""
    if enabled_services is None:
        enabled_services = {}

    resources = template.get("Resources", {})
    resource_list = []

    for resource_name, resource_data in resources.items():
        resource_type = resource_data.get("Type", "N/A")

        # Check if this resource should be included
        if not should_include_resource(
            resource_name, resource_type, template_name, enabled_services
        ):
            continue

        resource_info = {
            "name": resource_name,
            "type": resource_type,
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
    """Estimate NAT Gateway costs.

    NAT Gateway has two cost components:
    1. Hourly charge: $0.045/hour (~$32.85/month)
    2. Data processing: $0.045/GB processed

    Args:
        pricing_client: AWS Pricing API client
        region: AWS region

    Returns:
        Dictionary with cost information
    """
    # Get hourly NAT Gateway pricing
    filters = [
        {"Type": "TERM_MATCH", "Field": "location", "Value": region},
        {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "NAT Gateway"},
    ]

    price_item = pricing_client.get_service_pricing("AmazonVPC", filters)
    hourly_price = (
        pricing_client.extract_price_from_item(price_item) if price_item else 0.045
    )
    monthly_hourly_cost = hourly_price * 730 if hourly_price else 32.85

    # Data processing charge: $0.045 per GB
    data_processing_price_per_gb = 0.045

    # Estimate data processing based on typical usage
    # Conservative estimate for CSPM/security monitoring:
    # - Outbound traffic to CrowdStrike APIs: ~10 GB/month
    # - CloudTrail log forwarding: ~5 GB/month
    # - Other API calls and monitoring: ~5 GB/month
    # Total: ~20 GB/month
    #
    # Note: Actual usage varies significantly:
    # - Light usage: 5-20 GB/month ($0.23-$0.90/month)
    # - Medium usage: 50-200 GB/month ($2.25-$9/month)
    # - Heavy usage: 500+ GB/month ($22.50+/month)
    estimated_data_gb_per_month = 20
    estimated_data_processing_cost = (
        estimated_data_gb_per_month * data_processing_price_per_gb
    )

    # Total monthly cost
    total_monthly_cost = monthly_hourly_cost + estimated_data_processing_cost

    notes = (
        f"NAT Gateway hourly: ${hourly_price if hourly_price else 0.045}/hour = ${monthly_hourly_cost:.2f}/month. "
        f"Data processing: ${data_processing_price_per_gb:.3f}/GB. "
        f"Estimated {estimated_data_gb_per_month} GB/month = ${estimated_data_processing_cost:.2f}/month data processing. "
        f"Total estimated: ${total_monthly_cost:.2f}/month. "
        "Actual data processing costs vary significantly based on traffic volume: "
        "Light usage (5-20 GB/month) adds $0.23-$0.90/month, "
        "medium usage (50-200 GB/month) adds $2.25-$9/month, "
        "heavy usage (500+ GB/month) adds $22.50+/month."
    )

    return {
        "monthly_cost": total_monthly_cost,
        "unit_cost": hourly_price if hourly_price else 0.045,
        "unit": "per hour + $0.045/GB",
        "notes": notes,
        "pricing_available": price_item is not None,
    }


def estimate_cloudtrail_cost(
    pricing_client: AWSPricingClient, region: str
) -> Dict[str, Any]:
    """Estimate CloudTrail costs for additional trail.

    Note: The first copy of management events is free. Additional trails incur charges.
    This function assumes this is an additional trail (not the first free one).
    """
    # CloudTrail pricing: $2.00 per 100,000 management events delivered to S3
    # (after the first free copy)
    price_per_100k_events = 2.00

    # Estimate based on typical production account activity
    # Conservative estimate: 1 million management events per month
    # This can vary significantly based on:
    # - Number of users/services making API calls
    # - Automation and CI/CD pipelines
    # - Application activity levels
    estimated_events_per_month = 1_000_000
    estimated_monthly_cost = (
        estimated_events_per_month / 100_000
    ) * price_per_100k_events

    return {
        "monthly_cost": estimated_monthly_cost,
        "unit_cost": price_per_100k_events,
        "unit": "per 100K events",
        "notes": (
            f"${price_per_100k_events:.2f} per 100,000 management events delivered to S3 (after first free copy). "
            f"Estimated {estimated_events_per_month:,} events/month = ${estimated_monthly_cost:.2f}/month. "
            "Note: First trail copy is free. This pricing applies to additional trails. "
            "Actual costs vary significantly based on account activity: "
            "Low-activity accounts may see <100K events/month ($2/month), "
            "while high-activity accounts with extensive automation can exceed 10M events/month ($200+/month). "
            "Data events (S3/Lambda) incur additional charges if enabled."
        ),
        "pricing_available": True,
    }


def estimate_sqs_cost(
    pricing_client: AWSPricingClient, region: str, properties: Dict[str, Any]
) -> Dict[str, Any]:
    """Estimate SQS Queue costs.

    SQS pricing structure:
    1. Standard Queue: First 1M requests/month free, then $0.40 per million requests
    2. FIFO Queue: First 1M requests/month free, then $0.50 per million requests
    3. Data transfer: Standard AWS data transfer rates apply

    Args:
        pricing_client: AWS Pricing API client
        region: AWS region
        properties: CloudFormation resource properties (to check queue type)

    Returns:
        Dictionary with cost information
    """
    # Determine if this is a FIFO queue
    queue_name = properties.get("QueueName", "")
    is_fifo = (
        queue_name.endswith(".fifo")
        if isinstance(queue_name, str)
        else properties.get("FifoQueue", False)
    )

    # Get SQS pricing from API
    queue_type = "FIFO" if is_fifo else "Standard"
    filters = [
        {"Type": "TERM_MATCH", "Field": "location", "Value": region},
        {"Type": "TERM_MATCH", "Field": "queueType", "Value": queue_type},
    ]

    price_item = pricing_client.get_service_pricing("AmazonSQS", filters)
    price_per_million = (
        pricing_client.extract_price_from_item(price_item)
        if price_item
        else (0.50 if is_fifo else 0.40)
    )

    # Estimate based on typical CloudTrail S3 notification usage
    # Conservative estimate for CloudTrail S3 notifications:
    # - Assume moderate CloudTrail activity: 10,000 S3 objects/month
    # - Each S3 object triggers 1 SQS message
    # - CrowdStrike polls queue: ~1 receive per message
    # - Total: 10,000 sends + 10,000 receives + 10,000 deletes = 30,000 requests/month
    # This is well within the 1M free tier
    estimated_requests_per_month = 30_000

    # Calculate cost (accounting for 1M free tier)
    free_tier_requests = 1_000_000
    billable_requests = max(0, estimated_requests_per_month - free_tier_requests)
    estimated_monthly_cost = (billable_requests / 1_000_000) * price_per_million

    # Build notes
    queue_type_note = "FIFO queue" if is_fifo else "Standard queue"
    free_tier_note = (
        "within free tier"
        if estimated_requests_per_month <= free_tier_requests
        else f"{billable_requests:,} billable requests"
    )

    notes = (
        f"{queue_type_note}: ${price_per_million:.2f} per million requests (after 1M free tier). "
        f"Estimated {estimated_requests_per_month:,} requests/month ({free_tier_note}) "
        f"= ${estimated_monthly_cost:.4f}/month. "
        "Based on ~10K CloudTrail S3 objects/month with 3 operations each (send, receive, delete). "
        "Actual costs vary significantly based on CloudTrail volume and polling frequency. "
        "High-activity accounts can generate 100K-1M+ messages/month."
    )

    return {
        "monthly_cost": estimated_monthly_cost,
        "unit_cost": price_per_million,
        "unit": "per 1M requests",
        "notes": notes,
        "pricing_available": price_item is not None,
    }


def estimate_cloudwatch_logs_cost(
    pricing_client: AWSPricingClient, region: str, properties: Dict[str, Any]
) -> Dict[str, Any]:
    """Estimate CloudWatch Logs costs.

    CloudWatch Logs has two main cost components:
    1. Log Ingestion: $0.50 per GB ingested
    2. Log Storage: $0.03 per GB per month

    Args:
        pricing_client: AWS Pricing API client
        region: AWS region
        properties: CloudFormation resource properties (to check retention settings)

    Returns:
        Dictionary with cost information
    """
    # Get log ingestion pricing
    filters = [
        {"Type": "TERM_MATCH", "Field": "location", "Value": region},
        {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "Data Ingestion"},
        {"Type": "TERM_MATCH", "Field": "groupDescription", "Value": "Log Ingestion"},
    ]

    ingestion_price_item = pricing_client.get_service_pricing(
        "AmazonCloudWatch", filters
    )
    ingestion_price_per_gb = (
        pricing_client.extract_price_from_item(ingestion_price_item)
        if ingestion_price_item
        else 0.50
    )

    # Get log storage pricing
    storage_filters = [
        {"Type": "TERM_MATCH", "Field": "location", "Value": region},
        {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "Log Storage"},
        {"Type": "TERM_MATCH", "Field": "groupDescription", "Value": "Log Storage"},
    ]

    storage_price_item = pricing_client.get_service_pricing(
        "AmazonCloudWatch", storage_filters
    )
    storage_price_per_gb = (
        pricing_client.extract_price_from_item(storage_price_item)
        if storage_price_item
        else 0.03
    )

    # Get retention period from properties (default to 0 = never expire)
    retention_days = properties.get("RetentionInDays", 0)

    # Estimate usage based on typical Lambda function logging
    # Conservative estimate for Lambda logs:
    # - Assume moderate Lambda activity: 10,000 invocations/month
    # - Average log size per invocation: 2 KB
    # - Total ingestion: 10,000 * 2 KB = 20 MB = 0.02 GB/month
    estimated_ingestion_gb_per_month = 0.02

    # Calculate storage based on retention
    # With short retention (e.g., 1 day), storage is minimal
    # Average storage = (ingestion per day * retention days) / 2
    if retention_days > 0:
        daily_ingestion_gb = estimated_ingestion_gb_per_month / 30
        avg_storage_gb = (daily_ingestion_gb * retention_days) / 2
    else:
        # No retention limit - assume 30 days average for estimation
        avg_storage_gb = estimated_ingestion_gb_per_month

    # Calculate costs
    ingestion_cost = estimated_ingestion_gb_per_month * ingestion_price_per_gb
    storage_cost = avg_storage_gb * storage_price_per_gb
    total_monthly_cost = ingestion_cost + storage_cost

    # Build notes
    retention_note = (
        f"{retention_days} days"
        if retention_days > 0
        else "Never expire (indefinite retention)"
    )

    notes = (
        f"Log Ingestion: ${ingestion_price_per_gb:.2f}/GB, "
        f"Log Storage: ${storage_price_per_gb:.2f}/GB/month. "
        f"Retention: {retention_note}. "
        f"Estimated {estimated_ingestion_gb_per_month:.3f} GB ingestion/month "
        f"(~10K Lambda invocations at 2KB/invocation) = ${ingestion_cost:.4f}/month ingestion. "
        f"Estimated {avg_storage_gb:.4f} GB average storage = ${storage_cost:.4f}/month storage. "
        f"Total estimated: ${total_monthly_cost:.4f}/month. "
        "Actual costs vary significantly based on log volume and retention. "
        "High-traffic applications can generate 10-100x more logs."
    )

    return {
        "monthly_cost": total_monthly_cost,
        "unit_cost": ingestion_price_per_gb,
        "unit": "per GB ingested",
        "notes": notes,
        "pricing_available": ingestion_price_item is not None
        or storage_price_item is not None,
    }


def estimate_kms_cost(
    pricing_client: AWSPricingClient, region: str, properties: Dict[str, Any]
) -> Dict[str, Any]:
    """Estimate KMS customer managed key costs.

    KMS Pricing:
    - Customer managed keys: $1.00 per month per key
    - API requests: $0.03 per 10,000 requests (after free tier)
    - Free tier: 20,000 requests per month

    Args:
        pricing_client: AWS Pricing API client
        region: AWS region
        properties: CloudFormation resource properties

    Returns:
        Dictionary with cost information
    """
    # Get KMS pricing from API
    filters = [
        {"Type": "TERM_MATCH", "Field": "location", "Value": region},
        {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "Key Management"},
    ]

    price_item = pricing_client.get_service_pricing("awskms", filters)
    monthly_key_cost = (
        pricing_client.extract_price_from_item(price_item) if price_item else 1.00
    )

    # Note: API request costs are usage-based and not estimated here
    # Free tier: 20,000 requests/month
    # After free tier: $0.03 per 10,000 requests

    notes = (
        f"${monthly_key_cost:.2f}/month per customer managed key. "
        "API requests: $0.03 per 10,000 requests (after 20,000 free tier requests/month). "
        "Actual API costs depend on encryption/decryption volume. "
        "Typical usage: 1,000-10,000 requests/month for secrets/parameter encryption."
    )

    return {
        "monthly_cost": monthly_key_cost,
        "unit_cost": monthly_key_cost,
        "unit": "per key/month",
        "notes": notes,
        "pricing_available": price_item is not None,
    }


def estimate_sns_cost(
    pricing_client: AWSPricingClient, region: str, properties: Dict[str, Any]
) -> Dict[str, Any]:
    """Estimate SNS topic costs.

    SNS Pricing (varies by delivery protocol):
    - HTTP/HTTPS notifications: $0.60 per million requests (after free tier)
    - Email notifications: $2.00 per 100,000 notifications (after free tier)
    - SMS: Varies by destination country
    - Mobile push: $0.50 per million notifications (after free tier)
    - SQS: $0.00 (free)
    - Lambda: $0.00 (free)
    - Free tier: 1,000 email notifications, 1M mobile push, 100K HTTP/HTTPS per month

    Args:
        pricing_client: AWS Pricing API client
        region: AWS region
        properties: CloudFormation resource properties

    Returns:
        Dictionary with cost information
    """
    # For CloudTrail S3 notifications to SQS, SNS is typically used
    # SNS to SQS delivery is FREE
    # However, we'll estimate based on HTTP/HTTPS delivery as a conservative estimate

    # Get SNS pricing from API for HTTP/HTTPS delivery
    filters = [
        {"Type": "TERM_MATCH", "Field": "location", "Value": region},
        {
            "Type": "TERM_MATCH",
            "Field": "productFamily",
            "Value": "API Request",
        },
    ]

    price_item = pricing_client.get_service_pricing("AmazonSNS", filters)
    price_per_million = (
        pricing_client.extract_price_from_item(price_item) if price_item else 0.50
    )

    # Estimate based on typical CloudTrail S3 notification usage
    # Conservative estimate:
    # - 10,000 S3 objects/month (CloudTrail logs)
    # - Each S3 object triggers 1 SNS notification
    # - Total: 10,000 notifications/month
    # This is well within the free tier for most delivery types
    estimated_notifications_per_month = 10_000

    # For SNS to SQS, delivery is free
    # For other protocols, apply pricing after free tier
    free_tier_notifications = 100_000  # HTTP/HTTPS free tier
    billable_notifications = max(
        0, estimated_notifications_per_month - free_tier_notifications
    )
    estimated_monthly_cost = (billable_notifications / 1_000_000) * price_per_million

    notes = (
        f"SNS pricing varies by delivery protocol. "
        f"HTTP/HTTPS: ${price_per_million:.2f} per million (after 100K free tier). "
        "SNS to SQS/Lambda: FREE. "
        "Email: $2.00 per 100K (after 1K free tier). "
        f"Estimated {estimated_notifications_per_month:,} notifications/month "
        f"(within free tier for most protocols) = ${estimated_monthly_cost:.4f}/month. "
        "Based on ~10K CloudTrail S3 objects/month. "
        "Actual costs vary based on notification volume and delivery protocol."
    )

    return {
        "monthly_cost": estimated_monthly_cost,
        "unit_cost": price_per_million,
        "unit": "per 1M requests",
        "notes": notes,
        "pricing_available": price_item is not None,
    }


def estimate_eventbridge_data_transfer_cost(
    pricing_client: AWSPricingClient,
    region: str,
    properties: Dict[str, Any],
    template_path: str = "",
) -> Dict[str, Any]:
    """Estimate EventBridge costs for cross-account event delivery.

    EventBridge Pricing:
    - Event ingestion: $0.00 per million events (free for AWS management events)
    - Cross-account delivery: $1.00 per million events delivered to another account

    Reference: https://aws.amazon.com/eventbridge/pricing/
    """
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
            "notes": "No cross-account event delivery detected. Event ingestion is free for AWS management events.",
            "pricing_available": True,
        }

    # EventBridge cross-account delivery pricing
    # $1.00 per million events delivered to another account
    price_per_million_events = 1.00

    # Estimate based on typical CloudTrail event volume
    # AWS management events are delivered to EventBridge for free (ingestion)
    # But cross-account delivery incurs charges
    #
    # Typical production account activity estimates:
    # - Low activity: 100K-500K events/month
    # - Medium activity: 1M-5M events/month
    # - High activity: 10M+ events/month
    #
    # Conservative estimate for a typical production account: 5 million events/month
    # This aligns with AWS's pricing example
    estimated_events_per_month = 5_000_000

    # Calculate cost: (events / 1M) * $1.00
    estimated_monthly_cost = (
        estimated_events_per_month / 1_000_000
    ) * price_per_million_events

    return {
        "monthly_cost": estimated_monthly_cost,
        "unit_cost": price_per_million_events,
        "unit": "per 1M events",
        "notes": (
            f"EventBridge cross-account delivery: ${price_per_million_events:.2f} per million events. "
            f"Event ingestion is FREE for AWS management events. "
            f"Estimated {estimated_events_per_month:,} events/month delivered cross-account = ${estimated_monthly_cost:.2f}/month. "
            "Actual costs vary significantly based on account activity: "
            "Low-activity accounts may see 100K-500K events/month ($0.10-$0.50/month), "
            "medium-activity accounts 1M-5M events/month ($1-$5/month), "
            "while high-activity accounts with extensive automation can exceed 10M events/month ($10+/month). "
            "Note: Events must be ≤64KB each (larger events count as multiple events)."
        ),
        "pricing_available": True,
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
        # Special note for SSM Parameters
        if resource_type == "AWS::SSM::Parameter":
            notes = "Standard parameters are free: Up to 10,000 parameters per region, 4KB maximum size, and 40 transactions per second (TPS). Advanced parameters incur charges."
        else:
            notes = "No charge for this resource"

        return {
            "resource_name": resource_name,
            "resource_type": resource_type,
            "monthly_cost": 0.0,
            "unit_cost": 0.0,
            "unit": "N/A",
            "notes": notes,
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
    elif resource_type == "AWS::CloudTrail::Trail":
        cost_info = estimate_cloudtrail_cost(pricing_client, region)
    elif resource_type == "AWS::Events::Rule":
        # EventBridge rules may incur data transfer costs for cross-account delivery
        cost_info = estimate_eventbridge_data_transfer_cost(
            pricing_client, region, resource.get("properties", {}), template_path
        )
    elif resource_type == "AWS::Logs::LogGroup":
        # CloudWatch Log Groups have ingestion and storage costs
        cost_info = estimate_cloudwatch_logs_cost(
            pricing_client, region, resource.get("properties", {})
        )
    elif resource_type == "AWS::SQS::Queue":
        # SQS Queues have request-based costs with free tier
        cost_info = estimate_sqs_cost(
            pricing_client, region, resource.get("properties", {})
        )
    elif resource_type == "AWS::KMS::Key":
        # KMS customer managed keys have monthly costs
        cost_info = estimate_kms_cost(
            pricing_client, region, resource.get("properties", {})
        )
    elif resource_type == "AWS::SNS::Topic":
        # SNS topics have usage-based costs
        cost_info = estimate_sns_cost(
            pricing_client, region, resource.get("properties", {})
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
    parser.add_argument(
        "--config",
        default="aws/config.ini",
        help="Path to config.ini file (default: aws/config.ini)",
    )

    args = parser.parse_args()

    # Load enabled services configuration
    enabled_services = load_config(args.config)

    # Extract template filename
    template_name = Path(args.template).name

    # Check if this template should be included based on enabled services
    if enabled_services and not should_include_template(
        template_name, enabled_services
    ):
        print(f"\nTemplate '{template_name}' is disabled in config.ini")
        print(
            "To include this template in cost estimates, enable the corresponding service in [enabled_services] section:"
        )
        print("=" * 100)

        # Provide helpful message about which service to enable
        service_hints = {
            "cs_aws_1_click_sensor_management.yaml": "1click = true",
            "cs_aws_dspm_env.yaml": "dspm = true",
            "cs_aws_dspm.yaml": "dspm = true",
            "cs_aws_realtime_visibility_detection_eb.yaml": "ioa_eventbridge = true",
            "cs_aws_realtime_visibility_detection_s3.yaml": "ioa_s3 = true",
            "cs_aws_realtime_visibility_detection.yaml": "ioa_eventbridge = true or ioa_s3 = true",
        }

        if template_name in service_hints:
            print(f"Set: {service_hints[template_name]}")

        print("=" * 100)
        return

    print(f"\nEstimating costs for CloudFormation template: {args.template}")
    print(f"Region: {args.region}")

    if enabled_services:
        print("\nEnabled services from config.ini:")
        for service, enabled in enabled_services.items():
            status = "✓ enabled" if enabled else "✗ disabled"
            print(f"  {service}: {status}")

    print("=" * 100)

    # Initialize pricing client
    print("\nInitializing AWS Pricing API client...")
    pricing_client = AWSPricingClient(args.region)

    # Load and parse template
    template = load_template(args.template)

    # Analyze resources (with filtering based on enabled services)
    resources = analyze_resources(template, template_name, enabled_services)

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
    print("• Resources filtered based on enabled_services in config.ini")
    print("• Always refer to AWS Pricing Calculator for detailed estimates:")
    print("  https://calculator.aws/")
    print("=" * 100)


if __name__ == "__main__":
    main()
