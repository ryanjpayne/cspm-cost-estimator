#!/usr/bin/env python3
"""
Download CrowdStrike CloudFormation templates from S3.

This script downloads the root template and all child templates referenced within it,
resolving CloudFormation parameters and intrinsic functions.

Configuration is loaded from config.ini file.
"""

import os
import re
import sys
import yaml
import requests
import configparser
from typing import Dict, Set, Optional, List
from urllib.parse import urlparse


# Add CloudFormation intrinsic function constructors for YAML parser
def cf_constructor(loader, tag_suffix, node):
    """Generic constructor for CloudFormation intrinsic functions."""
    # Convert tag to function name (e.g., !Sub -> Fn::Sub)
    func_name = f"Fn::{tag_suffix}"

    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
        return {func_name: value}
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node)
        return {func_name: value}
    elif isinstance(node, yaml.MappingNode):
        value = loader.construct_mapping(node)
        return {func_name: value}
    return None


# Register CloudFormation intrinsic functions
cf_functions = [
    "Ref",
    "GetAtt",
    "Sub",
    "Join",
    "Select",
    "Split",
    "FindInMap",
    "GetAZs",
    "ImportValue",
    "Base64",
    "Cidr",
    "If",
    "Not",
    "Equals",
    "And",
    "Or",
    "Condition",
]

for func in cf_functions:
    yaml.SafeLoader.add_multi_constructor(
        f"!{func}", lambda loader, suffix, node, f=func: cf_constructor(loader, f, node)
    )


def load_config(config_file: str = "config.ini") -> Dict:
    """Load configuration from config.ini file."""
    config = configparser.ConfigParser()

    # Get the directory where this script is located
    script_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(script_dir, config_file)

    if not os.path.exists(config_path):
        print(f"Error: Configuration file not found: {config_path}")
        print("Please create a config.ini file with the required settings.")
        sys.exit(1)

    config.read(config_path)

    # Parse configuration
    result = {
        "root_template_url": config.get("download", "root_template_url"),
        "region": config.get("download", "region"),
        "output_dir": config.get("download", "output_dir"),
        "additional_templates": [],
        "region_map": {},
    }

    # Parse additional templates (multi-line value)
    if config.has_option("additional_templates", "templates"):
        templates_str = config.get("additional_templates", "templates")
        result["additional_templates"] = [
            line.strip() for line in templates_str.split("\n") if line.strip()
        ]

    # Parse region mappings
    if config.has_section("region_mappings"):
        for region in config.options("region_mappings"):
            values = config.get("region_mappings", region).split(",")
            if len(values) == 2:
                result["region_map"][region] = {
                    "Prefix": values[0].strip(),
                    "BucketRegionId": values[1].strip(),
                }

    return result


def download_file(url: str, output_path: str) -> bool:
    """Download a file from URL to output path."""
    try:
        print(f"Downloading: {url}")
        response = requests.get(url, timeout=30)
        response.raise_for_status()

        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, "w") as f:
            f.write(response.text)

        print(f"  -> Saved to: {output_path}")
        return True
    except Exception as e:
        print(f"  -> Error downloading {url}: {e}")
        return False


def resolve_sub_function(template_str: str, region: str, region_map: Dict) -> str:
    """
    Resolve CloudFormation !Sub function in template URLs.

    Handles patterns like:
    - ${Environment}
    - ${RegionPrefix}
    - ${BucketRegionId}
    - ${AWS::Region}
    - ${VersionVariable}
    """
    # Get region-specific values
    region_prefix = region_map.get(region, {}).get("Prefix", "")
    bucket_region_id = region_map.get(region, {}).get("BucketRegionId", "")

    # Replace CloudFormation pseudo parameters
    template_str = template_str.replace("${AWS::Region}", region)

    # Replace common variables
    template_str = template_str.replace("${Environment}", "prod")
    template_str = template_str.replace("${RegionPrefix}", region_prefix)
    template_str = template_str.replace("${BucketRegionId}", bucket_region_id)

    return template_str


def extract_template_urls(
    template_content: str, region: str, region_map: Dict
) -> Set[str]:
    """
    Extract all child template URLs from the CloudFormation template.

    Looks for TemplateURL properties and resolves their parameters.
    """
    urls = set()

    try:
        # Parse YAML
        template = yaml.safe_load(template_content)

        # Get region mappings from template, or use provided region_map
        template_region_map = template.get("Mappings", {}).get("RegionMap", {})
        if not template_region_map:
            template_region_map = region_map

        # Find all TemplateURL references in Resources
        resources = template.get("Resources", {})

        for resource_name, resource_config in resources.items():
            if not isinstance(resource_config, dict):
                continue

            properties = resource_config.get("Properties", {})
            template_url = properties.get("TemplateURL")

            if template_url:
                # Handle !Sub function
                if isinstance(template_url, dict) and "Fn::Sub" in template_url:
                    # Get the template string
                    sub_content = template_url["Fn::Sub"]

                    if isinstance(sub_content, list):
                        # Format: !Sub [template_string, {variables}]
                        template_str = sub_content[0]
                        variables = sub_content[1] if len(sub_content) > 1 else {}

                        # Apply variable substitutions
                        for var_name, var_value in variables.items():
                            # Handle !FindInMap
                            if (
                                isinstance(var_value, dict)
                                and "Fn::FindInMap" in var_value
                            ):
                                map_ref = var_value["Fn::FindInMap"]
                                if len(map_ref) >= 3:
                                    map_name = map_ref[0]
                                    key1 = map_ref[1]
                                    key2 = map_ref[2]

                                    # Resolve AWS::Region pseudo parameter
                                    if isinstance(key1, dict) and "Fn::Ref" in key1:
                                        if key1["Fn::Ref"] == "AWS::Region":
                                            key1 = region

                                    # Look up in mappings
                                    if map_name in template.get(
                                        "Mappings", {}
                                    ) and isinstance(key1, str):
                                        value = (
                                            template["Mappings"][map_name]
                                            .get(key1, {})
                                            .get(key2, "")
                                        )
                                        template_str = template_str.replace(
                                            f"${{{var_name}}}", str(value)
                                        )
                            # Handle plain string values (like version numbers)
                            elif isinstance(var_value, str):
                                template_str = template_str.replace(
                                    f"${{{var_name}}}", var_value
                                )

                        # Resolve remaining substitutions
                        resolved_url = resolve_sub_function(
                            template_str, region, template_region_map
                        )
                        urls.add(resolved_url)

                    elif isinstance(sub_content, str):
                        # Simple !Sub with just a string
                        resolved_url = resolve_sub_function(
                            sub_content, region, template_region_map
                        )
                        urls.add(resolved_url)

                elif isinstance(template_url, str):
                    # Direct URL string
                    urls.add(template_url)

    except Exception as e:
        print(f"Error parsing template: {e}")
        import traceback

        traceback.print_exc()

    return urls


def get_filename_from_url(url: str) -> str:
    """Extract filename from URL, removing version suffix."""
    parsed = urlparse(url)
    path = parsed.path
    filename = os.path.basename(path)

    # Remove version suffix (e.g., -1.3, -2.0, -6.1) before file extension
    # Pattern: hyphen followed by version number (e.g., -1.3) before .yaml
    filename = re.sub(r"-\d+\.\d+\.yaml$", ".yaml", filename)

    return filename


def main():
    """Main function to download all templates."""
    # Load configuration
    config = load_config()

    root_template_url = config["root_template_url"]
    region = config["region"]
    output_dir = config["output_dir"]
    additional_templates = config["additional_templates"]
    region_map = config["region_map"]

    print("=" * 80)
    print("CrowdStrike CloudFormation Template Downloader")
    print("=" * 80)
    print(f"Region: {region}")
    print(f"Output Directory: {output_dir}")
    print()

    # Create output directory
    os.makedirs(output_dir, exist_ok=True)

    # Download root template
    print("Step 1: Downloading root template...")
    root_filename = get_filename_from_url(root_template_url)
    root_path = os.path.join(output_dir, root_filename)

    if not download_file(root_template_url, root_path):
        print("Failed to download root template. Exiting.")
        sys.exit(1)

    print()

    # Read root template
    with open(root_path, "r") as f:
        root_content = f.read()

    # Extract child template URLs
    print("Step 2: Extracting child template URLs...")
    child_urls = extract_template_urls(root_content, region, region_map)

    print(f"Found {len(child_urls)} child template(s):")
    for url in sorted(child_urls):
        print(f"  - {url}")
    print()

    # Download child templates
    print("Step 3: Downloading child templates...")
    success_count = 0
    for url in sorted(child_urls):
        filename = get_filename_from_url(url)
        output_path = os.path.join(output_dir, filename)

        if download_file(url, output_path):
            success_count += 1

    # Download additional templates
    additional_success = 0
    if additional_templates:
        print()
        print("Step 4: Downloading additional templates...")
        for template_url in additional_templates:
            # Resolve variables in the URL
            resolved_url = resolve_sub_function(template_url, region, region_map)
            filename = get_filename_from_url(resolved_url)
            output_path = os.path.join(output_dir, filename)

            if download_file(resolved_url, output_path):
                additional_success += 1

    print()
    print("=" * 80)
    print(f"Download Summary:")
    print(f"  Root template: {root_filename}")
    print(
        f"  Child templates: {success_count}/{len(child_urls)} downloaded successfully"
    )
    if additional_templates:
        print(
            f"  Additional templates: {additional_success}/{len(additional_templates)} downloaded successfully"
        )
    print(f"  Output directory: {output_dir}")
    print("=" * 80)


if __name__ == "__main__":
    main()
