import re
import yaml
import argparse
from pathlib import Path
from collections import deque, defaultdict

# --- Helper Functions ---

def _extract_from_regex(content, regex, default_value=None):
    """Generic helper to extract a single match from content using a regex."""
    match = re.search(regex, content, re.DOTALL)
    if match:
        return match.group(1).strip().strip('"').strip("'")
    return default_value

def _extract_all_from_regex(content, regex):
    """Generic helper to extract all non-overlapping matches from content using a regex."""
    return re.findall(regex, content, re.DOTALL)

def _clean_cmake_option(opt: str, package_version: str) -> str | None:
    """Cleans and translates a single CMake option string."""
    opt = opt.strip().strip('"').strip("'")
    if not opt or opt.startswith("#"):
        return None

    # Vcpkg feature variable placeholder
    if "${FEATURE_OPTIONS}" in opt or re.match(r'\$\{\w+_FEATURE_OPTIONS\}', opt):
        return None

    # Replace vcpkg-specific paths with Flatpak-native /app/vendor
    opt = opt.replace('${CURRENT_HOST_INSTALLED_DIR}/tools/protobuf/protoc${VCPKG_HOST_EXECUTABLE_SUFFIX}', '/app/vendor/bin/protoc')
    opt = opt.replace('${CURRENT_INSTALLED_DIR}', '/app/vendor')
    opt = opt.replace('${CURRENT_HOST_INSTALLED_DIR}', '/app/vendor')
    opt = opt.replace('${CURRENT_PACKAGES_DIR}', '/app/vendor')
    # Replace vcpkg's internal VERSION variable with the actual discovered version
    opt = opt.replace('${VERSION}', package_version)

    # Remove vcpkg-specific build flags that Flatpak builder handles or are irrelevant
    if (opt.startswith("-DCMAKE_TOOLCHAIN_FILE") or
        opt.startswith("-DVCPKG_TARGET_TRIPLET") or
        opt.startswith("-DVCPKG_SET_CHARSET_FLAG") or
        opt.startswith("-DDLL_OUT_DIR") or
        opt.startswith("-DARCHIVE_OUT_DIR") or
        opt.startswith("-DCMAKE_DEBUG_POSTFIX") or
        opt.startswith("-D_VCPKG_NO_DEFAULT_PATH_W") or
        opt.startswith("-D_VCPKG_FIND_ROOT_PATH_") or
        opt.startswith("-DCMAKE_MFC_FLAG") or
        "VCPKG_CRT_LINKAGE" in opt or
        "VCPKG_LIBRARY_LINKAGE" in opt
    ):
        return None

    # Convert vcpkg's INSTALL_DIRS to just relying on CMAKE_INSTALL_PREFIX
    if (opt.startswith("-DgRPC_INSTALL_BINDIR") or
        opt.startswith("-DgRPC_INSTALL_LIBDIR") or
        opt.startswith("-DgRPC_INSTALL_INCLUDEDIR") or
        opt.startswith("-DgRPC_INSTALL_CMAKEDIR")):
        return None

    return opt

# --- Source Acquisition Parsers ---

def _parse_vcpkg_download_distfile(content: str, package_name: str, package_version: str) -> tuple[list, list] | None:
    """Parses vcpkg_download_distfile and vcpkg_extract_source_archive calls."""
    download_distfile_match = re.search(
        r'vcpkg_download_distfile\(\s*'
        r'ARCHIVE\s+([^\s\)]+)\s*'
        r'(?:URLS\s+("(?P<urls_quoted>.*?)"|(?P<urls_raw>\S+))\s*)?'
        r'(?:FILENAME\s+("(?P<filename_quoted>.*?)"|(?P<filename_raw>\S+))\s*)?'
        r'(?:SHA512\s+(?P<sha512>\S+)\s*)?',
        content,
        re.DOTALL
    )

    if not download_distfile_match:
        return None

    sources = []
    archive_name_var = download_distfile_match.group(1) # e.g., ARCHIVE
    urls_str = download_distfile_match.group('urls_quoted') or download_distfile_match.group('urls_raw')
    sha512 = download_distfile_match.group('sha512')

    if urls_str:
        urls_str = urls_str.replace('${VERSION}', package_version)
        urls = re.findall(r'https?://[^\s,"]+', urls_str)
        
        source_entry = {
            "type": "archive",
            "url": urls[0] if urls else "",
            "sha512": sha512.lower() if sha512 else "UNKNOWN_SHA512",
            "x-checker-data": {} # Will be populated more precisely below
        }

        # Auto-generate x-checker-data for archives
        if urls and "github.com" in urls[0]:
            repo_match = re.match(r'https://github.com/([^/]+/[^/]+)/releases/download', urls[0])
            if repo_match:
                source_entry["x-checker-data"] = {
                    "type": "github",
                    "url": repo_match.group(1),
                    "tag-pattern": r'^v?(\d+(?:\.\d+){1,2})$' # Common tag pattern
                }
        elif urls: # Fallback for non-github archives
            source_entry["x-checker-data"] = {
                "type": "html",
                "url": urls[0].rsplit('/', 1)[0], # Get base path for updates
                "version-pattern": r'(\d+(?:\.\d+){1,2})',
                "url-pattern": re.escape(urls[0]).replace(re.escape(package_version), r'(\d+(?:\.\d+){1,2})') # Generic pattern for URL
            }


        # Check for NO_REMOVE_ONE_LEVEL from vcpkg_extract_source_archive
        extract_archive_match = re.search(
            r'vcpkg_extract_source_archive\(\s*'
            r'(?:SOURCE_PATH\s+\S+\s*)?'
            rf'ARCHIVE\s+"?\$?\{{{archive_name_var}\}}?"?\s*'
            r'(?P<no_remove_one_level>NO_REMOVE_ONE_LEVEL)?',
            content,
            re.DOTALL
        )
        if extract_archive_match and extract_archive_match.group('no_remove_one_level'):
            source_entry["extract-strip"] = 0
        else:
            source_entry["extract-strip"] = 1 # Default to 1 for standard archives

        sources.append(source_entry)

    # Extract patches from vcpkg_extract_source_archive call
    patches_extract_match = re.search(
        r'vcpkg_extract_source_archive\([\s\S]*?ARCHIVE\s+"?\$?\{{.*?\s*}(?:[\s\S]*?PATCHES\s*([\s\S]*?)(?=\)|\n\S))?',
        content,
        re.DOTALL
    )
    patches = []
    if patches_extract_match and patches_extract_match.group(1):
        patch_list_str = patches_extract_match.group(1)
        patches.extend(re.findall(r'(\S+\.patch)', patch_list_str))
    
    return sources, patches

def _parse_vcpkg_from_github(content: str, package_name: str, package_version: str) -> tuple[list, list] | None:
    """Parses vcpkg_from_github calls."""
    from_github_match = re.search(
        r'vcpkg_from_github\(\s*'
        r'(?:OUT_SOURCE_PATH\s+\S+\s*)?'
        r'REPO\s+([^\s\)]+)\s*'
        r'REF\s+"?\$?\{?VERSION\}?"?\s*(?:#.*)?\s*',
        content,
        re.DOTALL
    )

    if not from_github_match:
        return None

    sources = []
    repo = from_github_match.group(1)
    
    source_tag = package_version # Use discovered version for tag

    source_entry = {
        "type": "git",
        "url": f"https://github.com/{repo}.git",
        "tag": source_tag.lstrip('vV') if source_tag.startswith('v') or source_tag.startswith('V') else source_tag,
        "x-checker-data": {
            "type": "git",
            "url": f"https://github.com/{repo}.git",
            "tag-pattern": r'^v?(\d+(?:\.\d+){1,2})$' # Common tag pattern
        }
    }
    sources.append(source_entry)

    # Extract patches specific to vcpkg_from_github
    patches_github_match = re.search(r'PATCHES\s*([\s\S]*?)(?=\)|\n\S)', content)
    patches = []
    if patches_github_match:
        patch_list_str = patches_github_match.group(1)
        patches.extend(re.findall(r'(\S+\.patch)', patch_list_str))
    
    return sources, patches

def _parse_cmake_options(content: str, package_version: str) -> list:
    """Parses CMake configure options."""
    cmake_options_match = re.search(
        r'vcpkg_cmake_configure\([\s\S]*?OPTIONS\s+((?:[^\n]|\n\s*)*?)(?=\n\s*\S|$)',
        content,
        re.DOTALL
    )
    
    raw_opts = []
    if cmake_options_match:
        options_str = cmake_options_match.group(1)
        raw_opts = [s.strip() for s in re.split(r'\s*\n\s*', options_str) if s.strip()]

    clean_opts = []
    for opt in raw_opts:
        cleaned_opt = _clean_cmake_option(opt, package_version)
        if cleaned_opt:
            clean_opts.append(cleaned_opt)

    # Add essential Flatpak CMake options
    essential_opts = [
        "-DCMAKE_BUILD_TYPE=Release",
        "-DCMAKE_INSTALL_PREFIX=/app/vendor",
        "-DCMAKE_POSITION_INDEPENDENT_CODE=ON"
    ]
    
    # Prepend essential options and deduplicate
    all_opts = essential_opts + clean_opts
    return sorted(list(dict.fromkeys(all_opts).keys()))

def _parse_dependencies(content: str, package_name: str) -> tuple[list, list]:
    """Extracts direct and host dependencies."""
    direct_dependencies = []
    host_dependencies = []

    # vcpkg_find_package(NAME <pkg> ...)
    for match in re.finditer(r'vcpkg_find_package\(\s*NAME\s+([^\s\)]+)', content):
        dep_name = match.group(1)
        direct_dependencies.append(dep_name)

    # vcpkg_check_features (simple case, assumes features map to dependencies)
    feature_deps_match = re.search(r'vcpkg_check_features\([\s\S]*?FEATURES\s*([\s\S]*?)(?:OUT_FEATURE_OPTIONS|$)', content)
    if feature_deps_match:
        feature_list_str = feature_deps_match.group(1)
        feature_names = re.findall(r'(\w+)', feature_list_str)
        for feature in feature_names:
            if feature.lower() not in ["core", "dbg", "tools", "doc", "test", "examples", "opengl", "debug", "private"]:
                direct_dependencies.append(feature)

    # vcpkg_copy_tools (indicates a host dependency)
    copy_tools_match = re.search(r'vcpkg_copy_tools\([\s\S]*?TOOL_NAMES\s*([\s\S]*?)(?=\)|\n\S)', content)
    if copy_tools_match:
        tool_names_str = copy_tools_match.group(1)
        tool_names = re.findall(r'(\w+)', tool_names_str)
        for tool in tool_names:
            if "grpc" in tool and "grpc" not in host_dependencies: # Heuristic for specific tools
                host_dependencies.append("grpc")
            elif "protoc" in tool and "protobuf" not in host_dependencies:
                host_dependencies.append("protobuf")
            else:
                host_dependencies.append(tool) # Potential host dependency, manual review needed.

    # Filter out self-dependencies and deduplicate
    direct_dependencies = list(dict.fromkeys([dep for dep in direct_dependencies if dep != package_name]))
    host_dependencies = list(dict.fromkeys([dep for dep in host_dependencies if dep != package_name]))

    return direct_dependencies, host_dependencies

# --- Main Parsing Logic ---

def parse_vcpkg_portfile(portfile_path: Path) -> tuple[dict, list, list]:
    """
    Main function to parse a single vcpkg portfile and extract all relevant data.
    Chooses the appropriate source acquisition method.
    """
    content = portfile_path.read_text()
    package_name = portfile_path.parent.name
    
    # Try to get the version from vcpkg.json first
    vcpkg_json_path = portfile_path.parent / "vcpkg.json"
    package_version = "UNKNOWN_VERSION"
    if vcpkg_json_path.exists():
        try:
            vcpkg_json = yaml.safe_load(vcpkg_json_path.read_text())
            package_version = vcpkg_json.get("version") or vcpkg_json.get("version-string")
            if package_version and package_version.startswith("v"):
                package_version = package_version[1:]
        except Exception as e:
            print(f"Warning: Could not parse {vcpkg_json_path} for {package_name} version: {e}")

    module_data = {
        "name": package_name,
        "builddir": True,
        "sources": [],
        "buildsystem": "cmake", # Default, but could be "simple", "autotools" if detected
        "config-opts": [],
        "install-commands": []
    }
    
    all_patches = []

    # Attempt to parse source acquisition methods in order of precedence
    sources_and_patches = _parse_vcpkg_download_distfile(content, package_name, package_version)
    if sources_and_patches:
        module_data["sources"], all_patches = sources_and_patches
    else:
        sources_and_patches = _parse_vcpkg_from_github(content, package_name, package_version)
        if sources_and_patches:
            module_data["sources"], all_patches = sources_and_patches
        else:
            print(f"Warning: No explicit vcpkg_from_github or vcpkg_download_distfile found for {package_name}. "
                  "Assuming source is handled externally or it's a header-only library/simple build. "
                  "Manual source setup may be required for this module.")
            # For this case, we might add a placeholder source or assume source is copied
            # For simplicity, we'll leave sources empty.

    # Add patch files to sources and install-commands
    for patch_file in sorted(list(set(all_patches))):
        module_data["sources"].append({
            "type": "file",
            "path": f"patches/{patch_file}"
        })
        module_data["install-commands"].append(f"patch -p1 < {patch_file}")

    # Add common CMake install command if not already added by patches
    if not module_data["install-commands"] or "cmake --build . --target install" not in module_data["install-commands"]:
        module_data["install-commands"].append("cmake --build . --target install")


    module_data["config-opts"] = _parse_cmake_options(content, package_version)
    direct_dependencies, host_dependencies = _parse_dependencies(content, package_name)

    return module_data, direct_dependencies, host_dependencies

# --- Topological Sort (unchanged from previous version) ---

def topological_sort(graph):
    """Performs a topological sort on a directed graph."""
    in_degree = {u: 0 for u in graph}
    for u in graph:
        for v in graph[u]:
            in_degree[v] += 1

    queue = deque([u for u in graph if in_degree[u] == 0])
    sorted_nodes = []

    while queue:
        u = queue.popleft()
        sorted_nodes.append(u)
        for v in graph[u]:
            in_degree[v] -= 1
            if in_degree[v] == 0:
                queue.append(v)

    if len(sorted_nodes) != len(graph):
        remaining_nodes = set(graph.keys()) - set(sorted_nodes)
        if remaining_nodes:
            cycle_nodes = list(remaining_nodes)
            error_msg = f"Circular dependency detected involving: {cycle_nodes}. "
            error_msg += "Check the portfiles for these packages."
            raise ValueError(error_msg)
        else:
            raise ValueError("Topological sort failed for an unknown reason (graph size mismatch).")
    return sorted_nodes


# --- Main Execution (largely unchanged, but calls refactored parsers) ---

def main():
    parser = argparse.ArgumentParser(
        description="Generate Flatpak manifest modules from vcpkg portfiles, including dependencies."
    )
    parser.add_argument("vcpkg_root", type=str,
                        help="Path to your vcpkg installation directory (e.g., /path/to/vcpkg)")
    parser.add_argument("start_packages", nargs='+',
                        help="Space-separated list of root package names to generate (e.g., grpc sentry-native)")
    parser.add_argument("--output", "-o", type=str, default="flatpak_app_manifest.yaml",
                        help="Output YAML file path for the full Flatpak manifest.")
    parser.add_argument("--runtime", type=str, default="org.freedesktop.Platform/x86_64/23.08",
                        help="Flatpak runtime to use.")
    parser.add_argument("--sdk", type=str, default="org.freedesktop.Sdk/x86_64/23.08",
                        help="Flatpak SDK to use.")
    parser.add_argument("--app-id", type=str, default="org.yourdomain.YourApp",
                        help="Flatpak application ID.")
    parser.add_argument("--app-command", type=str, default="your_app",
                        help="Main command for the Flatpak application.")


    args = parser.parse_args()

    vcpkg_root = Path(args.vcpkg_root)
    if not vcpkg_root.is_dir():
        print(f"Error: VCPKG_ROOT directory not found at {vcpkg_root}")
        return 1

    ports_dir = vcpkg_root / "ports"
    if not ports_dir.is_dir():
        print(f"Error: VCPKG_ROOT does not contain a 'ports' directory: {ports_dir}")
        return 1

    package_deps_map = defaultdict(set) # {dependent: {dependencies}}
    all_modules_data = {}
    processed_packages = set()
    queue = deque(args.start_packages)

    while queue:
        current_pkg = queue.popleft()
        if current_pkg in processed_packages:
            continue

        portfile_path = ports_dir / current_pkg / "portfile.cmake"
        if not portfile_path.is_file():
            print(f"Warning: Portfile not found for {current_pkg} at {portfile_path}. Skipping.")
            continue

        print(f"Processing: {current_pkg}")
        # Call the refactored parse_vcpkg_portfile
        module_data, direct_deps, host_deps = parse_vcpkg_portfile(portfile_path)
        all_modules_data[current_pkg] = module_data
        processed_packages.add(current_pkg)

        for dep in direct_deps + host_deps:
            if dep not in processed_packages and dep not in queue:
                queue.append(dep)
            package_deps_map[current_pkg].add(dep)


    # Invert the graph for topological sort (dependencies point to dependents)
    graph_for_sort = defaultdict(set)
    
    # Build the graph_for_sort where edges go from dependency to dependent
    for pkg, deps_of_pkg in package_deps_map.items():
        for dep in deps_of_pkg:
            if dep in all_modules_data: # Only add edges if the dependency is also a module we intend to build
                graph_for_sort[dep].add(pkg)
            else: # Ensure any discovered dependencies are nodes, even if we don't build them (e.g., system libs)
                if dep not in graph_for_sort:
                    graph_for_sort[dep] = set()
        # Ensure the current package (pkg) is also a node in the graph, even if it has no explicit dependencies
        if pkg not in graph_for_sort:
            graph_for_sort[pkg] = set()

    # Filter `graph_for_sort` to only include nodes that are in `all_modules_data`
    # (i.e., only packages for which we have actually generated a Flatpak module).
    filtered_graph_for_sort = defaultdict(set)
    for u in graph_for_sort:
        if u in all_modules_data:
            for v in graph_for_sort[u]:
                if v in all_modules_data:
                    filtered_graph_for_sort[u].add(v)
            if u not in filtered_graph_for_sort:
                filtered_graph_for_sort[u] = set()


    try:
        sorted_package_names = topological_sort(filtered_graph_for_sort)
        final_modules_list = [all_modules_data[name] for name in sorted_package_names]

    except ValueError as e:
        print(f"Error during topological sort: {e}")
        print("\nReconstructed Dependency Graph (Dependency -> Dependent):")
        for pkg, deps in sorted(filtered_graph_for_sort.items()):
            print(f"  {pkg}: {sorted(list(deps))}")
        print("\nNote: Dependencies not listed might be system dependencies or unparsed ports.")
        return 1

    # Create the full Flatpak manifest
    flatpak_manifest = {
        "app-id": args.app_id,
        "runtime": args.runtime,
        "runtime-version": args.runtime.split('/')[-1],
        "sdk": args.sdk,
        "command": args.app_command,
        "modules": final_modules_list
    }

    # Output the YAML
    output_path = Path(args.output)
    with open(output_path, 'w') as f:
        yaml.dump(flatpak_manifest, f, sort_keys=False, default_flow_style=False, indent=2)
    print(f"\nFlatpak manifest generated and saved to {output_path}")
    print("\nIMPORTANT: Review the generated manifest carefully!")
    print("  - Manually verify all package versions. 'UNKNOWN_VERSION' or auto-guessed versions may be incorrect.")
    print("  - Add `x-checker-data` for automatic updates where missing or incorrect, especially for archives.")
    print("  - Adjust CMake flags, especially paths, and feature-related options (e.g., `-DgRPC_BUILD_CODEGEN=ON`).")
    print("  - Ensure patches are correctly copied to your Flatpak project's 'patches/' directory (if any).")
    print("  - You may need to add `--env` variables for `PATH` or `LD_LIBRARY_PATH` within modules for some tools (e.g., protoc).")
    print("  - Common system dependencies (like zlib, openssl, curl) may exist in the runtime; consider removing explicit modules if they are not patched.")
    print("  - Transitive dependency flags (e.g., `-DProtobuf_DIR=/app/vendor/lib/cmake/protobuf`) might still need manual adjustment for absolute `/app/vendor/` paths, or using `CMAKE_PREFIX_PATH` in your final application module.")

    return 0

if __name__ == "__main__":
    exit(main())