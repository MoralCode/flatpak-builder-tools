import os
import subprocess
import xml.etree.ElementTree as ET
import json
from pathlib import Path
import hashlib
import re
import urllib.request
import urllib.error

# --- Configuration ---
MAVEN_EXECUTABLE = "mvn"  # Or the full path to your mvn if not in PATH
FLATPAK_SOURCES_FILE = "flatpak_maven_sources.json"
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
        for repo in root.findall('mvn:repositories/mvn:repository', namespace):
            url = repo.find('mvn:url', namespace)
            if url is not None and url.text:
                repositories.append(url.text.rstrip('/'))

        # Plugin repositories
        for repo in root.findall('mvn:pluginRepositories/mvn:pluginRepository', namespace):
            url = repo.find('mvn:url', namespace)
            if url is not None and url.text:
                repositories.append(url.text.rstrip('/'))

    except ET.ParseError as e:
        print(f"WARNING: Could not parse POM file {pom_file}: {e}")
    
    # Add default Maven Central as a fallback
    if DEFAULT_MAVEN_REPO_URL not in repositories:
        repositories.append(DEFAULT_MAVEN_REPO_URL.rstrip('/'))
        
    return list(set(repositories)) # Return unique URLs

def parse_dependency_list_output(output):
    """Parses the output of 'mvn dependency:list' to extract GAVs."""
    gavs = set()
    # Pattern to match: group:artifact:packaging:version:scope
    # and group:artifact:version:scope (packaging is optional in list)
    # and group:artifact:type:classifier:version:scope (with classifier)
    # Also handle '->' for resolved versions
    pattern = re.compile(
        r'^\s*([a-zA-Z0-9._-]+):([a-zA-Z0-9._-]+):([a-zA-Z0-9._-]+)?(?::([a-zA-Z0-9._-]+))?:([0-9a-zA-Z._-]+)(?::[a-zA-Z]+)?(?: -> ([0-9a-zA-Z._-]+))?.*$'
    )
    for line in output.splitlines():
        match = pattern.match(line)
        if match:
            # Group ID, Artifact ID, Packaging/Type, Classifier, Version, Resolved Version (optional)
            g, a, p_or_c, c_or_v, v, resolved_v = match.groups()

            # Handle cases where packaging is present or not, and classifier
            if resolved_v: # If a resolved version is present, use it
                version = resolved_v
            else:
                version = v

            # Check if p_or_c looks like a classifier or packaging
            packaging = None
            classifier = None
            if p_or_c and not re.match(r'^\d+\.\d+', p_or_c): # If it doesn't look like a version, it might be packaging or classifier
                if c_or_v and not re.match(r'^\d+\.\d+', c_or_v): # If c_or_v also doesn't look like version, then p_or_c is packaging and c_or_v is classifier
                    packaging = p_or_c
                    classifier = c_or_v
                else: # Otherwise p_or_c is packaging, and c_or_v is version
                    packaging = p_or_c
                    # 'v' already holds the actual version in this case
            
            # Simplified logic for direct parsing (less robust but common)
            # This is tricky because the format can vary. Let's aim for a robust GAV extraction
            # The pattern is more for G:A:V than deep parsing of all segments.
            parts = [s for s in line.strip().split(':') if s and ' ' not in s and '->' not in s]
            if len(parts) >= 3:
                group_id = parts[0]
                artifact_id = parts[1]
                # The version is usually the last part before scope, or after a potential classifier
                version_candidate = parts[-1] # Usually the version, if scope is not present
                if version_candidate in ["compile", "test", "provided", "runtime", "system", "import"]: # if it's a scope, ignore it
                    if len(parts) >=4:
                         version_candidate = parts[-2]
                    else:
                        continue # Malformed entry, skip
                
                # Check for packaging and classifier
                actual_version = version_candidate
                actual_classifier = None
                actual_packaging = "jar" # Default
                
                if len(parts) >= 5: # e.g., group:artifact:packaging:classifier:version:scope
                    actual_packaging = parts[2]
                    actual_classifier = parts[3]
                elif len(parts) == 4 and parts[2] not in ["jar", "pom"]: # e.g., group:artifact:classifier:version:scope (no packaging explicitly listed, or packaging is not jar/pom)
                     actual_classifier = parts[2]
                elif len(parts) == 4: # e.g., group:artifact:packaging:version:scope
                    actual_packaging = parts[2]
                
                gavs.add((group_id, artifact_id, actual_version, actual_classifier if actual_classifier != "null" else None, actual_packaging))
            
    # Remove scope markers like ":compile"
    return gavs


def construct_maven_url(base_repo_url, group_id, artifact_id, version, extension="jar", classifier=None):
    """Constructs a Maven download URL."""
    group_path = group_id.replace(".", "/")
    filename = f"{artifact_id}-{version}"
    if classifier:
        filename += f"-{classifier}"
    filename += f".{extension}"
    return f"{base_repo_url.rstrip('/')}/{group_path}/{artifact_id}/{version}/{filename}"

def calculate_sha256_from_url(url, temp_dir):
    """
    Downloads a file temporarily to calculate its SHA256 hash.
    Returns (sha256_hash, dest_filename)
    """
    # Use the filename from the URL
    dest_filename = url.split('/')[-1]
    temp_filepath = Path(temp_dir) / dest_filename

    max_retries = 3
    for attempt in range(max_retries):
        try:
            print(f"  Downloading temporarily: {url}...")
            urllib.request.urlretrieve(url, temp_filepath)
            sha256_hash = hashlib.sha256(temp_filepath.read_bytes()).hexdigest()
            temp_filepath.unlink() # Delete the temporary file
            return sha256_hash, dest_filename
        except urllib.error.HTTPError as e:
            print(f"  HTTP Error {e.code} for {url} (Attempt {attempt + 1}/{max_retries}): {e.reason}")
            if e.code == 404 and "pom" not in url: # POM files might legitimately not have source/javadoc
                 print(f"  Likely missing artifact at URL: {url}. Skipping.")
                 temp_filepath.unlink(missing_ok=True)
                 return None, None # Indicate not found
            if attempt < max_retries - 1:
                import time
                time.sleep(2 ** attempt) # Exponential backoff
            else:
                print(f"  Failed to download {url} after {max_retries} attempts.")
                temp_filepath.unlink(missing_ok=True)
                return None, None
        except Exception as e:
            print(f"  Error downloading {url} (Attempt {attempt + 1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                import time
                time.sleep(2 ** attempt)
            else:
                print(f"  Failed to download {url} after {max_retries} attempts.")
                temp_filepath.unlink(missing_ok=True)
                return None, None

def get_project_artifact_id(project_path):
    """Extracts the artifactId from the project's pom.xml."""
    pom_file = Path(project_path) / "pom.xml"
    if not pom_file.exists():
        return None
    try:
        tree = ET.parse(pom_file)
        root = tree.getroot()
        namespace = {'mvn': 'http://maven.apache.org/POM/4.0.0'}
        artifact_id_elem = root.find('mvn:artifactId', namespace)
        if artifact_id_elem is not None and artifact_id_elem.text:
            return artifact_id_elem.text
    except ET.ParseError:
        print(f"WARNING: Could not parse POM file {pom_file} for project artifactId.")
    return None


# --- Main Script Logic ---

def generate_flatpak_maven_sources_urls(project_path):
    """
    Generates a Flatpak sources JSON file for a given Maven project
    by resolving URLs and calculating hashes.
    """
    project_path = Path(project_path).resolve()
    pom_file = project_path / "pom.xml"

    if not pom_file.exists():
        print(f"ERROR: pom.xml not found at {pom_file}. Please provide the root path of a Maven project.")
        return

    # Create a temporary directory for hash calculation downloads
    temp_hash_dir = project_path / ".flatpak_hash_temp"
    temp_hash_dir.mkdir(parents=True, exist_ok=True)

    print(f"Analyzing Maven project: {project_path}")

    try:
        # 1. Get remote repositories from pom.xml
        maven_repositories = parse_pom_for_repositories(pom_file)
        print(f"Discovered Maven repositories: {maven_repositories}")

        # 2. Get project dependencies
        print("Resolving project dependencies...")
        dependency_list_output = run_maven_command(
            project_path,
            ["dependency:list", "-DoutputAbsoluteArtifactFilename=true", "-DoutputType=text", "-DincludeTypes=jar,pom", "-DoutputFile=dependencies.tmp"]
        )
        project_dependencies = parse_dependency_list_output(dependency_list_output)

        # 3. Get plugin dependencies (plugins often have their own deps)
        print("Resolving plugin dependencies...")
        plugin_list_output = run_maven_command(
            project_path,
            ["dependency:resolve-plugins", "-DoutputAbsoluteArtifactFilename=true", "-DoutputType=text", "-DincludeTypes=jar,pom", "-DoutputFile=plugins.tmp"]
        )
        plugin_dependencies = parse_dependency_list_output(plugin_list_output)

        all_gavs = project_dependencies.union(plugin_dependencies)
        
        # Add the project's own POM if it's a module
        project_artifact_id = get_project_artifact_id(project_path)
        if project_artifact_id:
            # You might need to parse the project's group and version from its own pom.xml
            # For simplicity, we assume this is just about external dependencies for now.
            # If the project itself is built and installed, its POM would also be needed by other modules.
            pass


        if not all_gavs:
            print("No external Maven dependencies (project or plugin) found.")
            return

        print(f"\nFound {len(all_gavs)} unique Maven GAVs to process.")

        flatpak_sources = []
        processed_urls = set() # To avoid duplicate URLs (same artifact from different repos)

        for group_id, artifact_id, version, classifier, packaging in all_gavs:
            # Construct URLs for main JAR/POM, sources, and javadoc
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
            # Javadoc JAR (optional, but useful)
            javadoc_url = construct_maven_url(
                DEFAULT_MAVEN_REPO_URL, group_id, artifact_id, version, extension="jar", classifier="javadoc"
            )

            potential_urls = [main_url, pom_url]
            if classifier != "sources": # Avoid adding "sources" classifier when it's already the primary
                potential_urls.append(source_url)
            if classifier != "javadoc": # Avoid adding "javadoc" classifier when it's already the primary
                potential_urls.append(javadoc_url)


            print(f"\nProcessing GAV: {group_id}:{artifact_id}:{version}")
            for url in potential_urls:
                if url in processed_urls:
                    continue # Skip if already processed

                # Attempt to find the artifact in known repositories
                found_url = None
                for repo_url in maven_repositories:
                    current_url = url.replace(DEFAULT_MAVEN_REPO_URL, repo_url) # Try with specific repo
                    try:
                        # Test if URL is reachable without full download
                        with urllib.request.urlopen(current_url, timeout=5) as response:
                            if response.getcode() == 200:
                                found_url = current_url
                                break
                    except (urllib.error.URLError, urllib.error.HTTPError):
                        pass # Try next repo

                if not found_url:
                    found_url = url # Fallback to default if not found in specific repos
                    # Final check on default repo if not found in project repos
                    try:
                        with urllib.request.urlopen(found_url, timeout=5) as response:
                            if response.getcode() != 200:
                                print(f"  WARNING: Could not find artifact for {url.split('/')[-1]} in any configured repository. Skipping.")
                                continue
                    except (urllib.error.URLError, urllib.error.HTTPError):
                        print(f"  WARNING: Could not find artifact for {url.split('/')[-1]} in any configured repository. Skipping.")
                        continue


                sha256_hash, dest_filename = calculate_sha256_from_url(found_url, temp_hash_dir)

                if sha256_hash and dest_filename:
                    source_entry = {
                        "type": "archive",
                        "url": found_url,
                        "sha256": sha256_hash,
                        "dest-filename": dest_filename
                    }
                    flatpak_sources.append(source_entry)
                    processed_urls.add(found_url)
                    print(f"  Added: {dest_filename}")
                else:
                    print(f"  Skipping {url.split('/')[-1]} due to download/hash failure.")

        # 4. Write the Flatpak sources JSON
        output_file_path = project_path / FLATPAK_SOURCES_FILE
        with open(output_file_path, "w") as f:
            json.dump(flatpak_sources, f, indent=2)

        print(f"\nSuccessfully generated Flatpak sources JSON at: {output_file_path}")
        print(f"Total {len(flatpak_sources)} sources added.")

    except Exception as e:
        print(f"\nAn error occurred: {e}")
        print(f"Please ensure Maven is installed and accessible, and that the project builds successfully.")
    finally:
        # Clean up the temporary hash directory
        import shutil
        if temp_hash_dir.exists():
            print(f"Cleaning up temporary directory: {temp_hash_dir}")
            shutil.rmtree(temp_hash_dir)

# --- Script Entry Point ---
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Generate a Flatpak sources JSON for a Maven project "
                    "by resolving URLs and calculating hashes for offline builds."
    )
    parser.add_argument(
        "project_path",
        help="Path to the root directory of the Maven project (where pom.xml is located)."
    )
    args = parser.parse_args()

    generate_flatpak_maven_sources_urls(args.project_path)