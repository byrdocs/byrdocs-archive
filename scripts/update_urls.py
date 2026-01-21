#!/usr/bin/env python
# -*- coding: utf-8 -*-

import argparse
import glob
import re
import sys
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from ruamel.yaml import YAML


def update_origin_in_yaml_files(new_domain: str, metadata_dir: Path):
    """
    Updates the domain of the 'url' field in all YAML files within a directory.

    Args:
        new_domain (str): The new domain (e.g., 'byrdocs.cpphusky.xyz').
        metadata_dir (Path): The path to the directory containing YAML files.
    """
    if not new_domain:
        print("Error: new_domain cannot be empty.", file=sys.stderr)
        return

    yaml = YAML()
    yaml.preserve_quotes = True
    
    file_pattern = str(metadata_dir / '*.yml')
    yaml_files = list(glob.glob(file_pattern))

    if not yaml_files:
        print(f"No .yml files found in '{metadata_dir}'.", file=sys.stderr)
        return

    print(f"Found {len(yaml_files)} YAML files to process...")

    for filepath in yaml_files:
        path = Path(filepath)
        try:
            with path.open('r', encoding='utf-8') as f:
                data = yaml.load(f)

            if data and 'url' in data and data['url']:
                original_url = data['url']
                
                # Parse the original URL and replace its network location (domain)
                parsed_url = urlparse(original_url)
                new_url_parts = parsed_url._replace(netloc=new_domain)
                new_url = urlunparse(new_url_parts)

                if original_url != new_url:
                    data['url'] = new_url
                    with path.open('w', encoding='utf-8') as f:
                        yaml.dump(data, f)
                    print(f"Updated URL in {path.name} to {new_url}")
                else:
                    print(f"URL in {path.name} already uses the new domain. Skipping.")
            else:
                print(f"Skipping {path.name}: 'url' key not found or is empty.")

        except Exception as e:
            print(f"Error processing file {filepath}: {e}", file=sys.stderr)


def update_schema_url_patterns(new_domain: str, schema_dir: Path):
    """
    Updates the domain of the 'url' pattern in all YAML schema files within a directory.

    Args:
        new_domain (str): The new domain (e.g., 'byrdocs.cpphusky.xyz').
        schema_dir (Path): The path to the directory containing schema files.
    """
    if not new_domain:
        print("Error: new_domain cannot be empty.", file=sys.stderr)
        return

    yaml = YAML()
    yaml.preserve_quotes = True
    
    file_pattern = str(schema_dir / '**/*.yaml')
    yaml_files = list(glob.glob(file_pattern, recursive=True))

    if not yaml_files:
        print(f"No .yaml files found in '{schema_dir}'.", file=sys.stderr)
        return

    print(f"Found {len(yaml_files)} schema files to process in '{schema_dir}'...")

    # Escape the new domain for regex
    escaped_new_domain = re.escape(new_domain)

    for filepath in yaml_files:
        path = Path(filepath)
        try:
            with path.open('r', encoding='utf-8') as f:
                data = yaml.load(f)

            if (data and 'properties' in data and 
                    'url' in data.get('properties', {}) and 
                    'pattern' in data.get('properties', {}).get('url', {})):

                original_pattern = data['properties']['url']['pattern']

                # Replace domain in regex pattern
                new_pattern, num_subs = re.subn(r'(https://)[^/]+', r'\1' + escaped_new_domain, original_pattern)

                if num_subs > 0 and original_pattern != new_pattern:
                    data['properties']['url']['pattern'] = new_pattern
                    with path.open('w', encoding='utf-8') as f:
                        yaml.dump(data, f)
                    print(f"Updated URL pattern in {path.name} to {new_pattern}")
                else:
                    print(f"URL pattern in {path.name} already uses the new domain or did not match. Skipping.")
            else:
                print(f"Skipping {path.name}: 'properties/url/pattern' not found.")

        except Exception as e:
            print(f"Error processing file {filepath}: {e}", file=sys.stderr)


def main():
    """Main function to parse arguments and run the script."""
    parser = argparse.ArgumentParser(
        description='Bulk update the domain for URLs in YAML metadata and schema files.'
    )
    parser.add_argument(
        'new_domain',
        type=str,
        help='The new domain to set for URLs (e.g., byrdocs.cpphusky.xyz).'
    )
    parser.add_argument(
        '--metadata-dir',
        type=str,
        default='metadata',
        help='The directory containing the metadata YAML files (default: "metadata").'
    )
    parser.add_argument(
        '--schema-dir',
        type=str,
        default='schema',
        help='The directory containing the schema YAML files (default: "schema").'
    )

    args = parser.parse_args()
    
    # Update metadata files
    metadata_dir = Path(args.metadata_dir)
    if metadata_dir.is_dir():
        print(f"Processing metadata files in '{metadata_dir}'...")
        update_origin_in_yaml_files(args.new_domain, metadata_dir)
    else:
        print(f"Info: Metadata directory '{metadata_dir}' not found, skipping.", file=sys.stdout)

    # Update schema files
    schema_dir = Path(args.schema_dir)
    if schema_dir.is_dir():
        print(f"Processing schema files in '{schema_dir}'...")
        update_schema_url_patterns(args.new_domain, schema_dir)
    else:
        print(f"Info: Schema directory '{schema_dir}' not found, skipping.", file=sys.stdout)

    print("\nProcessing complete.")


if __name__ == '__main__':
    main()
