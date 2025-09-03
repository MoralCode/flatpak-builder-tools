import os
import subprocess
import xml.etree.ElementTree as ET
import json
from pathlib import Path
import re

# --- Configuration ---
MAVEN_EXECUTABLE = "mvn"  # Or the full path to your mvn if not in PATH
FLATPAK_SOURCES_JSON_FILE = "flatpak_maven_sources_no_hash.json"
DEFAULT_MAVEN_REPO_URL = "https://repo.maven.apache.org/maven2/"

# --- Helper Functions ---

def run_maven_command(project_path, args):
    """Executes a Maven command in the given project path."""
    cmd = [MAVEN_EXECUTABLE] + args
    print(f"\n--- Running Maven command ---")
    print(f"  Command: {' '.join(cmd)}")
    print(f"  In directory: {project_path}")
    print(f"-----------------------------\n")

    try:
        result = subprocess.run(
            cmd,
            cwd=project_path,
            check=True,
            capture_output=True,
            text=True,
            encoding='utf-8',
        )
        print("Maven command successful.")
        return result.stdout
    except FileNotFoundError:
        print(f"\nERROR: Maven executable '{MAVEN_EXECUTABLE}' not found.")
        print("Please ensure Maven is installed and accessible in your system's PATH,")
        print("or update the 'MAVEN_EXECUTABLE' variable in the script to the full path.")
        raise
    except subprocess.CalledProcessError as e:
        print(f"\nERROR: Maven command failed with exit code {e.returncode}.")
        print(f"  Command: {' '.join(cmd)}")
        print(f"  Working Directory: {project_path}")
        print("\n--- Maven STDOUT (partial, for context) ---")
        print('\n'.join(e.stdout.splitlines()[-20:]))
        print("\n--- Maven STDERR (errors typically here) ---")
        print(e.stderr)
        print("-------------------------------------------\n")
        raise
    except Exception as e:
        print(f"\nAN UNEXPECTED ERROR OCCURRED while running Maven: {e}")
        raise

def parse_pom_for_repositories(pom_file):
    """Parses a pom.xml file to extract repository URLs."""
    repositories = []
    try:
        tree = ET.parse(pom_file)
        root = tree.getroot()
        namespace = {'mvn': 'http://maven.apache.org/POM/4.0.0'} # Maven POM namespace

        # Project repositories
        # for repo in root.findall('mvn:repositories/mvn:repository', namespace):
        #     url = repo.find('mvn:url', namespace)
        #     if url is not None and url.text:
        #         repositories.append(url.text.rstrip('/'))

        # Plugin repositories
        # for repo in root.findall('mvn:pluginRepositories/mvn:pluginRepository', namespace):
        #     url = repo.find('mvn:url', namespace)
        #     if url is not None and url.text:
        #         repositories.append(url.text.rstrip('/'))

    except ET.ParseError as e:
        print(f"WARNING: Could not parse POM file {pom_file}: {e}")
    except FileNotFoundError:
        print(f"WARNING: POM file not found at {pom_file}.")

    # Add default Maven Central as a fallback
    if DEFAULT_MAVEN_REPO_URL.rstrip('/') not in repositories:
        repositories.append(DEFAULT_MAVEN_REPO_URL.rstrip('/'))

    return list(set(repositories)) # Return unique URLs

def parse_dependency_list_output(output):
    """
    Parses the output of 'mvn dependency:list' to extract GAVs.
    More robust parsing for standard Maven list format.
    """
    gav_pattern = re.compile(
        r'^\s*([a-zA-Z0-9._-]+):([a-zA-Z0-9._-]+):([a-zA-Z0-9._-]+):([a-zA-Z0-9._-]+):([0-9a-zA-Z._-]+)(?::[a-zA-Z]+)?(?: -> ([0-9a-zA-Z._-]+))?.*$'
    )
    # This pattern handles: groupId:artifactId:type:classifier:version:scope -> resolvedVersion
    # where type and classifier can be "null" if not present.
    # We prioritize the "resolvedVersion" if present.

    # Simpler pattern for cases where type/classifier might be missing from list output.
    # Prioritizes version as the last numeric-like segment before scope.
    gavs = set()
    for line in output.splitlines():
        line = line.strip()
        if not line or line == "":
            continue
        try:
            if "Resolved:" in line:
                line = line.split("Resolved: ")[1]
        except Exception as e:
            pass

        # Clean line to only contain GAV-like structure, remove scope, etc.
        # Example: org.springframework.boot:spring-boot:jar:2.7.0:compile
        # Example: org.junit.jupiter:junit-jupiter-api:jar:5.9.3:test
        # Example: org.apache.maven.plugins:maven-resources-plugin:maven-plugin:2.6:default-resources (plugin format)

        # Remove scope and resolved version parts for simpler parsing
        clean_line = re.sub(r':(compile|test|provided|runtime|system|import)(?: -> \S+)?$', '', line)
        clean_line = re.sub(r' -> \S+$', '', clean_line) # Just a resolved version
        
        parts = [p for p in clean_line.split(':') if p and p != "null"]

        if len(parts) >= 3: # Must have at least G:A:V
            group_id = parts[0]
            artifact_id = parts[1]
            version = parts[-1] # Assume version is the last part
            
            # Determine type and classifier based on remaining parts
            packaging = "jar" # Default
            classifier = None

            if len(parts) > 3: # G:A:type:version or G:A:classifier:version or G:A:type:classifier:version
                # Try to distinguish type from classifier heuristically
                candidate_type = parts[2]
                if len(parts) == 4: # G:A:X:V
                    if candidate_type in ["jar", "pom", "war", "ear", "bundle", "maven-plugin"]:
                        packaging = candidate_type
                    else: # Could be a classifier if not a known packaging
                        classifier = candidate_type
                elif len(parts) == 5: # G:A:type:classifier:V
                    packaging = parts[2]
                    classifier = parts[3]

            # Store as tuple for uniqueness in set
            gavs.add((group_id, artifact_id, version, classifier if classifier != "null" else None, packaging))
    return gavs


def construct_maven_url(base_repo_url, group_id, artifact_id, version, extension="jar", classifier=None):
    """Constructs a Maven download URL."""
    group_path = group_id.replace(".", "/")
    filename = f"{artifact_id}-{version}"
    if classifier:
        filename += f"-{classifier}"
    filename += f".{extension}"
    return f"{base_repo_url.rstrip('/')}/{group_path}/{artifact_id}/{version}/{filename}"

# --- New function to output as Flatpak sources JSON ---

def output_flatpak_sources_json(flatpak_urls_list, output_file_path):
    """
    Writes a list of Flatpak source dictionaries to a JSON file.
    The output is a direct JSON array suitable for a Flatpak module's 'sources' field.
    """
    try:
        with open(output_file_path, "w") as f:
            json.dump(flatpak_urls_list, f, indent=2)
        print(f"\nGenerated Flatpak sources JSON (without hashes) at: {output_file_path}")
        print(f"Total {len(flatpak_urls_list)} unique URLs identified.")
        print("\nNOTE: This file does NOT contain SHA256 hashes. You will need to obtain them")
        print("      either manually or by using `flatpak-builder --collect-modules` during build.")
    except Exception as e:
        print(f"ERROR: Could not write Flatpak sources JSON to {output_file_path}: {e}")

# --- Main Script Logic (modified) ---

def get_maven_flatpak_sources(project_path):
    """
    Generates a Flatpak-compatible JSON file containing source URLs for a given
    Maven project's dependencies. SHA256 hashes are NOT included.
    """
    project_path = Path(project_path).resolve()
    pom_file = project_path / "pom.xml"

    if not pom_file.exists():
        print(f"ERROR: pom.xml not found at {pom_file}. Please provide the root path of a Maven project.")
        return

    print(f"Analyzing Maven project: {project_path} for Flatpak sources (no hashing)...")

    try:
        # 1. Get remote repositories from pom.xml
        maven_repositories = parse_pom_for_repositories(pom_file)
        print(f"Discovered Maven repositories: {maven_repositories}")

        # 2. Get project dependencies GAVs
        print("Resolving project dependencies GAVs...")
        dependency_list_output = run_maven_command(
            project_path,
            ["dependency:list", "-DoutputType=text", "-DincludeTypes=jar,pom,maven-plugin,bundle,war,ear"]
        )
        project_dependencies_gavs = parse_dependency_list_output(dependency_list_output)

        # 3. Get plugin dependencies GAVs
        print("Resolving plugin dependencies GAVs...")
        plugin_list_output = run_maven_command(
            project_path,
            ["dependency:resolve-plugins", "-DoutputType=text", "-DincludeTypes=jar,pom,maven-plugin"]
        )
        plugin_dependencies_gavs = parse_dependency_list_output(plugin_list_output)

        all_gavs = project_dependencies_gavs.union(plugin_dependencies_gavs)

        if not all_gavs:
            print("No external Maven dependencies (project or plugin) found.")
            return

        print(f"\nFound {len(all_gavs)} unique Maven GAVs to process.")

        flatpak_sources_list = []
        added_urls = set() # To prevent duplicate URLs in the output

        for group_id, artifact_id, version, classifier, packaging in sorted(list(all_gavs)):
            # Construct URLs for main artifact, POM, sources, and javadoc
            # Main artifact
            main_url = construct_maven_url(
                DEFAULT_MAVEN_REPO_URL, group_id, artifact_id, version, extension=packaging, classifier=classifier
            )
            # POM file
            pom_url = construct_maven_url(
                DEFAULT_MAVEN_REPO_URL, group_id, artifact_id, version, extension="pom"
            )
            # Source JAR
            source_url = construct_maven_url(
                DEFAULT_MAVEN_REPO_URL, group_id, artifact_id, version, extension="jar", classifier="sources"
            )
            # Javadoc JAR (optional, but sometimes useful)
            javadoc_url = construct_maven_url(
                DEFAULT_MAVEN_REPO_URL, group_id, artifact_id, version, extension="jar", classifier="javadoc"
            )

            potential_urls_for_gav = [main_url, pom_url]
            if classifier != "sources": # Avoid adding "sources" classifier when it's already the primary artifact
                potential_urls_for_gav.append(source_url)
            if classifier != "javadoc": # Avoid adding "javadoc" classifier when it's already the primary artifact
                potential_urls_for_gav.append(javadoc_url)
            
            # For each potential URL, try to map it to the correct repository
            print(f"\nProcessing GAV: {group_id}:{artifact_id}:{version} (Packaging: {packaging}, Classifier: {classifier})")
            for base_url in potential_urls_for_gav:
                dest_filename = base_url.split('/')[-1]

                # First, check configured repositories
                found_in_config_repo = False
                for repo_url in maven_repositories:
                    # Replace the default URL's base with the current repo's base
                    candidate_url = base_url.replace(DEFAULT_MAVEN_REPO_URL.rstrip('/'), repo_url)
                    if candidate_url not in added_urls:
                        flatpak_sources_list.append({
                            "type": "archive",
                            # SHA256 is intentionally omitted here
                            "url": candidate_url,
                            "dest-filename": dest_filename
                        })
                        added_urls.add(candidate_url)
                        print(f"  Identified URL from config repo: {candidate_url}")
                        found_in_config_repo = True
                
                # Also include the default Maven Central URL if it hasn't been added already
                # (e.g., if it's not explicitly in maven_repositories, or was a different artifact path)
                if base_url not in added_urls:
                     flatpak_sources_list.append({
                        "type": "archive",
                        "url": base_url,
                        "dest-filename": dest_filename
                     })
                     added_urls.add(base_url)
                     print(f"  Identified URL (fallback to Maven Central): {base_url}")


        # Output the collected URLs as a Flatpak sources JSON file
        output_file_path = project_path / FLATPAK_SOURCES_JSON_FILE
        output_flatpak_sources_json(flatpak_sources_list, output_file_path)

    except Exception as e:
        print(f"\nAn error occurred: {e}")
        print(f"Please ensure Maven is installed and accessible, and that the project builds successfully.")

# --- Script Entry Point ---
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Generate a Flatpak-compatible JSON file with source URLs for a project's Maven dependencies."
                    "SHA256 hashes are NOT included, and will need to be added manually or by Flatpak Builder."
    )
    parser.add_argument(
        "project_path",
        help="Path to the root directory of the Maven project (where pom.xml is located)."
    )
    args = parser.parse_args()

    get_maven_flatpak_sources(args.project_path)