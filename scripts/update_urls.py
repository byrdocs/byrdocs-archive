#!/usr/bin/env python
# -*- coding: utf-8 -*-

import argparse
import glob
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
                parsed_url = urlparse(original_url)
                new_netloc = urlparse(new_domain).netloc
                # Parse the original URL and replace its network location (domain)
                new_url_parts = parsed_url._replace(netloc=new_netloc)
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

def main():
    """Main function to parse arguments and run the script."""
    parser = argparse.ArgumentParser(
        description='Bulk update the domain for the "url" field in YAML files.'
    )
    parser.add_argument(
        'new_domain',
        type=str,
        help='The new domain to set in the "url" field (e.g., byrdocs.cpphusky.xyz).'
    )

    args = parser.parse_args()
    
    metadata_dir = Path(args.dir)
    if not metadata_dir.is_dir():
        print(f"Error: Directory not found at '{metadata_dir}'", file=sys.stderr)
        sys.exit(1)

    update_origin_in_yaml_files(args.new_domain, metadata_dir)
    print("\nProcessing complete.")

if __name__ == '__main__':
    main()
