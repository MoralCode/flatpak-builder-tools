import re
import yaml
import argparse
from pathlib import Path
from collections import deque, defaultdict

def extract_cmake_variable(content, var_name):
    """Extracts a CMake variable value (simple case)."""
    match = re.search(rf'{var_name}\s*=\s*([^\n\r]+)', content)
    if match:
        return match.group(1).strip().strip('"').strip("'")
    return None

def parse_vcpkg_portfile(portfile_path: Path, vcpkg_root: Path) -> tuple[dict, list, list]:
    """
    Parses a vcpkg portfile.cmake and extracts information for a Flatpak module,
    along with its direct vcpkg dependencies.

    Returns:
        tuple[dict, list, list]: (module_data, direct_dependencies, host_dependencies)
    """
    content = portfile_path.read_text()
    package_name = portfile_path.parent.name
    
    # Try to get the version from vcpkg.json first
    vcpkg_json_path = portfile_path.parent / "vcpkg.json"
    version = None
    if vcpkg_json_path.exists():
        try:
            vcpkg_json = yaml.safe_load(vcpkg_json_path.read_text())
            version = vcpkg_json.get("version") or vcpkg_json.get("version-string")
            if version and version.startswith("v"): # remove v prefix for flatpak tag
                version = version[1:]
        except Exception as e:
            print(f"Warning: Could not parse {vcpkg_json_path}: {e}")

    module_data = {
        "name": package_name,
        "builddir": True,
        "sources": [],
        "buildsystem": "cmake", # Assuming cmake for most vcpkg C++ ports
        "config-opts": [],
        "install-commands": []
    }

    # 1. Parse vcpkg_from_github
    # Adjusted regex to handle more variations and capture more
    from_github_match = re.search(
        r'vcpkg_from_github\(\s*'
        r'(?:OUT_SOURCE_PATH\s+\S+\s*)?'
        r'REPO\s+([^\s\)]+)\s*'
        r'REF\s+"?\$?\{?VERSION\}?"?\s*(?:#.*)?\s*', # Matches "REF \"${VERSION}\"" or "REF ${VERSION}" etc.
        content,
        re.DOTALL
    )

    if from_github_match:
        repo = from_github_match.group(1)
        # Use extracted version if found, otherwise placeholder.
        source_tag = version if version else "UNKNOWN_VERSION"
        
        module_data["sources"].append({
            "type": "git",
            "url": f"https://github.com/{repo}.git",
            "tag": source_tag # Use a placeholder or found version
        })

        # Extract patches
        # Regex to find PATCHES block, handling comments and different line endings
        patches_block_match = re.search(r'PATCHES\s*([\s\S]*?)(?=\)|\n\S)', content)
        if patches_block_match:
            patch_list_str = patches_block_match.group(1)
            # Find words ending in .patch, filter out commented lines
            patches = [
                p for p in re.findall(r'(\S+\.patch)', patch_list_str)
                if not re.match(r'^\s*#', p, re.MULTILINE) # Filter lines that start with a comment
            ]
            for patch_file in patches:
                module_data["sources"].append({
                    "type": "file",
                    "path": f"patches/{patch_file}" # Assumes patches are in a 'patches' subdir relative to manifest
                })
                module_data["install-commands"].append(f"patch -p1 < {patch_file}") # Add patch command


    # 2. Parse vcpkg_cmake_configure OPTIONS
    # Main OPTIONS group, and also look for FEATURE_OPTIONS
    cmake_options_match = re.search(
        r'vcpkg_cmake_configure\([\s\S]*?OPTIONS\s+((?:[^\n]|\n\s*)*?)(?=\n\s*\S|$)',
        content,
        re.DOTALL
    )
    feature_options_var_match = re.search(
        r'vcpkg_check_features\([\s\S]*?OUT_FEATURE_OPTIONS\s+(\S+)',
        content
    )
    feature_options_var = feature_options_var_match.group(1) if feature_options_var_match else None

    raw_opts = []
    if cmake_options_match:
        options_str = cmake_options_match.group(1)
        # Split by whitespace, handling newlines and comments
        raw_opts = [s.strip().strip('"').strip("'") for s in re.split(r'\s*\n\s*', options_str) if s.strip()]

    clean_opts = []
    for opt in raw_opts:
        if not opt or opt.startswith("#") or opt == "${FEATURE_OPTIONS}" or (feature_options_var and opt == f"${{{feature_options_var}}}"):
            continue

        # Replace vcpkg-specific paths with Flatpak-native /app/vendor
        # This is a heuristic and might need manual adjustment.
        opt = opt.replace('${CURRENT_HOST_INSTALLED_DIR}/tools/protobuf/protoc${VCPKG_HOST_EXECUTABLE_SUFFIX}', '/app/vendor/bin/protoc')
        opt = opt.replace('${CURRENT_INSTALLED_DIR}', '/app/vendor')
        opt = opt.replace('${CURRENT_HOST_INSTALLED_DIR}', '/app/vendor')
        opt = opt.replace('${CURRENT_PACKAGES_DIR}', '/app/vendor') # For things like INSTALL_CMAKEDIR

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
            continue

        # Convert vcpkg's INSTALL_DIRS to just relying on CMAKE_INSTALL_PREFIX
        if (opt.startswith("-DgRPC_INSTALL_BINDIR") or
            opt.startswith("-DgRPC_INSTALL_LIBDIR") or
            opt.startswith("-DgRPC_INSTALL_INCLUDEDIR") or
            opt.startswith("-DgRPC_INSTALL_CMAKEDIR")):
            continue

        clean_opts.append(opt)

    # Add essential Flatpak CMake options
    module_data["config-opts"].append("-DCMAKE_BUILD_TYPE=Release")
    module_data["config-opts"].append("-DCMAKE_INSTALL_PREFIX=/app/vendor")
    module_data["config-opts"].append("-DCMAKE_POSITION_INDEPENDENT_CODE=ON") # Good practice for Flatpak

    module_data["config-opts"].extend(clean_opts)

    # Deduplicate options
    module_data["config-opts"] = sorted(list(dict.fromkeys(module_data["config-opts"]).keys()))


    # 3. Extract Dependencies
    direct_dependencies = []
    host_dependencies = []

    # vcpkg_find_package(NAME <pkg> ...)
    for match in re.finditer(r'vcpkg_find_package\(\s*NAME\s+(\S+)', content):
        dep_name = match.group(1)
        direct_dependencies.append(dep_name)

    # vcpkg_check_features (simple case, assumes features map to dependencies)
    # This is a heuristic: it assumes feature names often correspond to package names
    # e.g., 'zlib' feature -> 'zlib' package
    feature_deps_match = re.search(r'vcpkg_check_features\([\s\S]*?FEATURES\s*([\s\S]*?)(?:OUT_FEATURE_OPTIONS|$)', content)
    if feature_deps_match:
        feature_list_str = feature_deps_match.group(1)
        feature_names = re.findall(r'(\w+)', feature_list_str)
        for feature in feature_names:
            # Common dependencies usually named after feature, e.g., 'zlib' feature uses 'zlib' package
            # This is a heuristic and might need manual adjustment.
            if feature.lower() not in ["core", "dbg", "tools", "doc", "test", "examples", "opengl", "debug"]: # Common non-package features
                direct_dependencies.append(feature)

    # vcpkg_copy_tools (indicates a host dependency)
    # This is rough, as tool names don't always map directly to package names
    copy_tools_match = re.search(r'vcpkg_copy_tools\([\s\S]*?TOOL_NAMES\s*([\s\S]*?)(?=\)|\n\S)', content)
    if copy_tools_match:
        tool_names_str = copy_tools_match.group(1)
        tool_names = re.findall(r'(\w+)', tool_names_str)
        for tool in tool_names:
            # Heuristic: map tool names like 'grpc_cpp_plugin' to 'grpc' itself, or 'protoc' to 'protobuf'
            if "grpc" in tool and "grpc" not in host_dependencies:
                host_dependencies.append("grpc")
            elif "protoc" in tool and "protobuf" not in host_dependencies:
                host_dependencies.append("protobuf")
            else:
                # Add the tool name as a potential host dependency. Manual review needed.
                host_dependencies.append(tool)


    # Filter out self-dependencies
    direct_dependencies = [dep for dep in direct_dependencies if dep != package_name]
    host_dependencies = [dep for dep in host_dependencies if dep != package_name]

    # Deduplicate dependencies
    direct_dependencies = list(dict.fromkeys(direct_dependencies))
    host_dependencies = list(dict.fromkeys(host_dependencies))

    return module_data, direct_dependencies, host_dependencies

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
        raise ValueError("Circular dependency detected in the graph!")
    return sorted_nodes

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

    package_graph = defaultdict(set) # {package: {dependencies}}
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
        module_data, direct_deps, host_deps = parse_vcpkg_portfile(portfile_path, vcpkg_root)
        all_modules_data[current_pkg] = module_data
        processed_packages.add(current_pkg)

        for dep in direct_deps + host_deps: # Treat all discovered deps as needed for ordering
            if dep not in processed_packages and dep not in queue:
                queue.append(dep)
            package_graph[current_pkg].add(dep) # This is actually saying current_pkg depends on dep
                                                # For topological sort, we want dep -> current_pkg

    # Invert the graph for topological sort (dependencies point to dependents)
    # A -> B means A is a dependency of B, so A must come before B.
    # We want a graph where edges go from dependency to dependent.
    dependency_graph = defaultdict(set)
    all_involved_packages = set(all_modules_data.keys())
    for pkg, deps in package_graph.items():
        all_involved_packages.add(pkg) # Ensure all packages are in the graph nodes
        for dep in deps:
            if dep in all_modules_data: # Only add if we actually parsed the dependency
                dependency_graph[dep].add(pkg)
            else:
                # If a dependency was listed but we didn't process its portfile
                # (e.g., it was skipped due to missing portfile, or it's a very common system lib)
                # Ensure it's in the graph to avoid errors during sort.
                dependency_graph[dep] # Add node if not exists (no outgoing edges)


    # Add packages that have no outgoing edges (no dependencies listed for them within the graph)
    # but are part of the set of all packages to be built.
    for pkg in all_involved_packages:
        if pkg not in dependency_graph:
            dependency_graph[pkg] = set()

    # Perform topological sort
    try:
        # We need the nodes in topological order. The graph `dependency_graph` has edges
        # A -> B if A must be built before B. So we sort the keys of this graph.
        # However, `topological_sort` expects edges from parent to child (what it provides).
        # We need to compute in-degrees on our `dependency_graph` to get correct order.

        # Create a graph where A -> B means B depends on A.
        # The `package_graph` we built is actually more like this already: {dependent: {dependencies}}
        # Let's rebuild the graph structure to be `dep -> dependent` for sorting.

        graph_for_sort = defaultdict(set)
        for pkg, deps_of_pkg in package_graph.items():
            # pkg depends on deps_of_pkg. So deps_of_pkg must come before pkg.
            # Add reverse edges: dep -> pkg
            for dep in deps_of_pkg:
                # Ensure all nodes are present in graph_for_sort, even if they have no dependencies
                if dep not in all_involved_packages and dep not in all_modules_data:
                     # This might be a system lib not handled by vcpkg. Add a dummy node.
                     graph_for_sort[dep] = set()
                graph_for_sort[dep].add(pkg)
            # Ensure the current package is a node in the graph, even if it has no explicit dependencies
            if pkg not in graph_for_sort:
                graph_for_sort[pkg] = set()

        # Filter out nodes from graph_for_sort that aren't in all_modules_data,
        # as we only want to generate modules for the ones we parsed.
        final_graph_nodes = sorted([node for node in graph_for_sort if node in all_modules_data])
        # Rebuild graph to only contain relevant nodes and edges between them
        cleaned_graph_for_sort = defaultdict(set)
        for u in final_graph_nodes:
            for v in graph_for_sort[u]:
                if v in final_graph_nodes:
                    cleaned_graph_for_sort[u].add(v)
            if u not in cleaned_graph_for_sort: # Ensure nodes with no outgoing edges are present
                cleaned_graph_for_sort[u] = set()

        # Perform the sort
        sorted_package_names = topological_sort(cleaned_graph_for_sort)

        # Reverse the order if it's dependency -> dependent
        # The topological sort typically orders nodes such that if there is an edge
        # from A to B, then A appears before B in the ordering.
        # So, if graph_for_sort is `dependency -> dependent`, the result is correct.
        final_modules_list = [all_modules_data[name] for name in sorted_package_names]

    except ValueError as e:
        print(f"Error during topological sort: {e}")
        print("Dependency graph (dependency -> dependent):")
        for pkg, deps in graph_for_sort.items():
            print(f"  {pkg} -> {deps}")
        return 1

    # Create the full Flatpak manifest
    flatpak_manifest = {
        "app-id": args.app_id,
        "runtime": args.runtime,
        "runtime-version": args.runtime.split('/')[-1], # Extract version from runtime string
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
    print("  - Manually check package versions. '${FLATPAK_VERSION}' is a placeholder.")
    print("  - Add `x-checker-data` for automatic updates.")
    print("  - Adjust CMake flags, especially paths, and feature-related options.")
    print("  - Ensure patches are correctly copied to your Flatpak project's 'patches/' directory.")
    print("  - You may need to add `--env` variables for `PATH` or `LD_LIBRARY_PATH` within modules.")
    print("  - System dependencies (like zlib, openssl) may exist in runtime; check if explicit module is needed.")

    return 0

if __name__ == "__main__":
    exit(main())