import os
import subprocess
import xml.etree.ElementTree as ET
import json
from pathlib import Path
import hashlib
import re

# --- Configuration ---
MAVEN_EXECUTABLE = "mvn"  # Or the full path to your mvn if not in PATH
TEMP_DEPENDENCY_DIR = "maven_offline_dependencies_temp"
FLATPAK_SOURCES_FILE = "flatpak_maven_sources.json"
DEFAULT_MAVEN_REPO_URL = "https://repo.maven.apache.org/maven2/" # Used as a fallback

# --- Helper Functions ---

def run_maven_command(project_path, args):
    """Executes a Maven command in the given project path."""
    cmd = [MAVEN_EXECUTABLE] + args
    print(f"Running Maven command: {' '.join(cmd)} in {project_path}")
    try:
        result = subprocess.run(
            cmd,
            cwd=project_path,
            check=True,
            capture_output=True,
            text=True
        )
        print("Maven command successful.")
        return result.stdout
    except subprocess.CalledProcessError as e:
        print(f"Error running Maven command: {e}")
        print(f"STDOUT:\n{e.stdout}")
        print(f"STDERR:\n{e.stderr}")
        raise

def calculate_sha256(filepath):
    """Calculates the SHA256 hash of a file."""
    hasher = hashlib.sha256()
    with open(filepath, "rb") as f:
        while True:
            chunk = f.read(4096)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()

def artifact_to_path(group_id, artifact_id, version, classifier=None, extension="jar"):
    """
    Converts Maven coordinates to a path in a typical Maven repository structure.
    e.g., org.springframework.boot:spring-boot:2.7.0 -> org/springframework/boot/spring-boot/2.7.0/spring-boot-2.7.0.jar
    """
    group_path = group_id.replace(".", "/")
    filename = f"{artifact_id}-{version}"
    if classifier:
        filename += f"-{classifier}"
    filename += f".{extension}"
    return f"{group_path}/{artifact_id}/{version}/{filename}"

def extract_artifact_info_from_filepath(filepath: Path):
    """
    Attempts to extract Maven GAVCE (Group, Artifact, Version, Classifier, Extension)
    from a file path downloaded by Maven Dependency Plugin.

    Example path: .../org/springframework/boot/spring-boot/2.7.0/spring-boot-2.7.0.jar
    """
    parts = filepath.parts
    # Look for the version number pattern to identify the start of artifact segment
    for i, part in enumerate(parts):
        # A common pattern for versions is X.Y.Z or X-SNAPSHOT
        if re.match(r'^\d+\.\d+(\.\d+)?(-SNAPSHOT)?$', part):
            version = part
            artifact_id = parts[i-1]
            group_id_parts = parts[0:i-2]
            group_id = ".".join(group_id_parts)

            # Extract extension and potential classifier
            filename = filepath.name
            base_filename, extension = os.path.splitext(filename)
            extension = extension.lstrip('.') # Remove leading dot

            classifier = None
            # Check for classifier: spring-boot-2.7.0-classifier.jar
            # Needs to be careful not to confuse it with version
            # This is a bit heuristic and might need refinement
            if base_filename.startswith(f"{artifact_id}-{version}-"):
                classifier_segment = base_filename[len(f"{artifact_id}-{version}-"):]
                # Simple check for common classifiers. This is a weak point.
                if not re.match(r'^\d', classifier_segment): # Classifier typically doesn't start with a digit
                    classifier = classifier_segment

            return {
                "group_id": group_id,
                "artifact_id": artifact_id,
                "version": version,
                "classifier": classifier,
                "extension": extension
            }
    return None # Could not parse

# --- Main Script Logic ---

def generate_flatpak_maven_sources(project_path):
    """
    Generates a Flatpak sources JSON file for a given Maven project
    to enable fully offline builds.
    """
    project_path = Path(project_path).resolve()
    temp_dir = project_path / TEMP_DEPENDENCY_DIR
    temp_dir.mkdir(parents=True, exist_ok=True)

    print(f"Collecting all Maven dependencies for project: {project_path}")
    print(f"Temporary download directory: {temp_dir}")

    try:
        # 1. Resolve and copy all dependencies (main, test, plugin)
        # This is the most crucial step. We use the dependency:copy-dependencies
        # and dependency:resolve-plugins to get all necessary artifacts.
        # It's important to do a full resolution to get transitive dependencies.
        run_maven_command(
            project_path,
            [
                "dependency:copy-dependencies",
                "dependency:resolve-plugins", # Resolve plugin dependencies
                f"-DoutputDirectory={temp_dir.as_posix()}",
                "-DskipTests",
                "-Dmdep.copyPom=true", # Copy associated POM files
                "-Dmdep.copySources=true", # Copy source JARs if available
                "-Dmdep.copyTransitive=true" # Ensure transitive dependencies are copied
            ]
        )

        # 2. Collect information about downloaded artifacts
        flatpak_sources = []
        downloaded_files = list(temp_dir.rglob("*.*"))
        print(f"Found {len(downloaded_files)} potential artifacts in {temp_dir}")

        for filepath in downloaded_files:
            if filepath.is_file():
                artifact_info = extract_artifact_info_from_filepath(filepath)
                if not artifact_info:
                    print(f"WARNING: Could not parse artifact info for: {filepath.relative_to(temp_dir)}")
                    # For unparseable files, we can just include them as a generic file source
                    # but it's less ideal as we don't have the original URL for robust fetching
                    continue

                group_id = artifact_info["group_id"]
                artifact_id = artifact_info["artifact_id"]
                version = artifact_info["version"]
                classifier = artifact_info["classifier"]
                extension = artifact_info["extension"]

                # Construct the Maven-style path for the URL
                maven_repo_path = artifact_to_path(
                    group_id, artifact_id, version, classifier, extension
                )
                source_url = f"{DEFAULT_MAVEN_REPO_URL}{maven_repo_path}"

                # Calculate SHA256
                sha256_hash = calculate_sha256(filepath)

                # Flatpak source entry
                source_entry = {
                    "type": "archive",
                    "url": source_url,
                    "sha256": sha256_hash,
                    "dest-filename": filepath.name # Use original filename as downloaded by Maven
                }
                flatpak_sources.append(source_entry)
                print(f"  Added: {filepath.name}")

        # 3. Write the Flatpak sources JSON
        output_file_path = project_path / FLATPAK_SOURCES_FILE
        with open(output_file_path, "w") as f:
            json.dump(flatpak_sources, f, indent=2)

        print(f"\nSuccessfully generated Flatpak sources JSON at: {output_file_path}")
        print(f"Total {len(flatpak_sources)} sources added.")
        print(f"You can now clean up the temporary directory: {temp_dir}")

    except Exception as e:
        print(f"\nAn error occurred: {e}")
        print(f"Please ensure Maven is installed and accessible, and that the project builds successfully.")
    finally:
        # It's generally good to keep the temp_dir for inspection, but you can add a cleanup step.
        # import shutil
        # if temp_dir.exists():
        #     shutil.rmtree(temp_dir)
        pass

# --- Script Entry Point ---
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Generate a Flatpak sources JSON for a Maven project "
                    "to enable fully offline builds."
    )
    parser.add_argument(
        "project_path",
        help="Path to the root directory of the Maven project (where pom.xml is located)."
    )
    args = parser.parse_args()

    generate_flatpak_maven_sources(args.project_path) 
