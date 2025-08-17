import re
import yaml
import argparse
from pathlib import Path

def parse_vcpkg_portfile(portfile_path: Path, package_name: str) -> dict:
    """
    Parses a vcpkg portfile.cmake and extracts information relevant for a Flatpak manifest.
    """
    content = portfile_path.read_text()
    module_data = {
        "name": package_name,
        "builddir": True,
        "sources": [],
        "buildsystem": "cmake", # Assuming cmake for most vcpkg C++ ports
        "config-opts": [],
        "install-commands": [
            "cmake --build . --target install"
        ]
    }

    # 1. Parse vcpkg_from_github
    # Example: vcpkg_from_github(\n    OUT_SOURCE_PATH SOURCE_PATH\n    REPO grpc/grpc\n    REF "v${VERSION}"\n    SHA512 ...\n    HEAD_REF master\n    PATCHES\n        00001-fix-uwp.patch
    from_github_match = re.search(
        r'vcpkg_from_github\(\s*'
        r'(?:OUT_SOURCE_PATH\s+\S+\s*)?' # Non-capturing group for OUT_SOURCE_PATH
        r'REPO\s+([^\s\)]+)\s*'
        r'REF\s+"([^"]+)"\s*',
        content,
        re.DOTALL
    )

    if from_github_match:
        repo = from_github_match.group(1)
        ref = from_github_match.group(2).replace('${VERSION}', '${FLATPAK_VERSION}') # Replace VCPKG_VERSION with Flatpak's
        source_type = "git" if ref.startswith("v") or ref.startswith("V") else "archive" # Guess based on ref pattern
        if source_type == "git":
            module_data["sources"].append({
                "type": "git",
                "url": f"https://github.com/{repo}.git",
                "tag": ref.lstrip('vV') # Remove 'v' or 'V' prefix for git tag
            })
        else: # Likely means it's a direct hash or specific branch name, we default to git for now.
            module_data["sources"].append({
                "type": "git",
                "url": f"https://github.com/{repo}.git",
                "tag": ref # Use ref as is
            })


        # Extract patches
        patches_match = re.search(r'PATCHES\s*([\s\S]*?)(?=\)|\n\S)', content)
        if patches_match:
            patch_list_str = patches_match.group(1)
            patches = re.findall(r'(\S+\.patch)', patch_list_str)
            for patch_file in patches:
                module_data["sources"].append({
                    "type": "file",
                    "path": f"patches/{patch_file}" # Assumes patches are in a 'patches' subdir relative to manifest
                })
                module_data["build-commands"].append(f"patch -p1 < {patch_file}") # Add patch command


    # 2. Parse vcpkg_cmake_configure OPTIONS
    # This regex is more complex to handle multi-line options
    cmake_configure_match = re.search(
        r'vcpkg_cmake_configure\(\s*'
        r'(?:SOURCE_PATH\s+\S+\s*)?' # Optional SOURCE_PATH
        r'(?:OPTIONS_DEBUG\s+\[?\s*([\s\S]*?)\s*\]\s*)?' # Optional OPTIONS_DEBUG
        r'OPTIONS\s+\[?\s*([\s\S]*?)\s*\]\s*', # Main OPTIONS group
        content,
        re.DOTALL
    )

    if cmake_configure_match:
        # Prioritize main OPTIONS, then debug if present (though debug options usually stripped for release Flatpak)
        options_str = cmake_configure_match.group(2) or cmake_configure_match.group(1)
        if options_str:
            # Split options by newline and then strip quotes and newlines/spaces
            # Filter out empty strings
            raw_opts = [s.strip().strip('"').strip("'") for s in re.split(r'\s*\n\s*', options_str) if s.strip()]

            # Clean and filter options
            clean_opts = []
            for opt in raw_opts:
                if not opt:
                    continue

                # Replace vcpkg-specific paths with Flatpak-native /app/vendor
                # This is a heuristic and might need manual adjustment.
                opt = opt.replace('${CURRENT_HOST_INSTALLED_DIR}/tools/protobuf/protoc${VCPKG_HOST_EXECUTABLE_SUFFIX}', '/app/vendor/bin/protoc')
                opt = opt.replace('${CURRENT_INSTALLED_DIR}', '/app/vendor')
                opt = opt.replace('${CURRENT_HOST_INSTALLED_DIR}', '/app/vendor') # Assuming host tools become app/vendor tools

                # Remove vcpkg-specific build flags that Flatpak builder handles or are irrelevant
                if (opt.startswith("-DCMAKE_TOOLCHAIN_FILE") or
                    opt.startswith("-DVCPKG_TARGET_TRIPLET") or
                    opt.startswith("-DVCPKG_SET_CHARSET_FLAG") or
                    opt.startswith("-DDLL_OUT_DIR") or
                    opt.startswith("-DARCHIVE_OUT_DIR") or
                    opt.startswith("-DCMAKE_DEBUG_POSTFIX") or
                    opt.startswith("-DCMAKE_INSTALL_LIBDIR") or # Handled by /app/vendor structure
                    opt.startswith("-DCMAKE_INSTALL_BINDIR") or
                    opt.startswith("-DCMAKE_INSTALL_INCLUDEDIR") or
                    opt.startswith("-DCMAKE_INSTALL_CMAKEDIR") or # These are often just subdirs of INSTALL_PREFIX
                    opt.startswith("-D_VCPKG_NO_DEFAULT_PATH_W") or # Vcpkg internal
                    opt.startswith("-D_VCPKG_FIND_ROOT_PATH_") or # Vcpkg internal
                    opt.startswith("-DCMAKE_MFC_FLAG") or # Windows-specific
                    "VCPKG_CRT_LINKAGE" in opt or # Windows-specific runtime linkage
                    "VCPKG_LIBRARY_LINKAGE" in opt # Vcpkg library linkage (Flatpak builds shared by default, adjust if static needed)
                ):
                    continue

                clean_opts.append(opt)

            # Add essential Flatpak CMake options
            module_data["config-opts"].append("-DCMAKE_BUILD_TYPE=Release")
            module_data["config-opts"].append("-DCMAKE_INSTALL_PREFIX=/app/vendor")
            module_data["config-opts"].append("-DCMAKE_POSITION_INDEPENDENT_CODE=ON") # Good practice for Flatpak

            module_data["config-opts"].extend(clean_opts)

            # Deduplicate options (useful if FEATURE_OPTIONS are also extracted)
            module_data["config-opts"] = sorted(list(dict.fromkeys(module_data["config-opts"]).keys()))


    # Add common CMake install commands
    module_data["install-commands"] = ["cmake --build . --target install"]


    return module_data

def main():
    parser = argparse.ArgumentParser(
        description="Generate Flatpak manifest module from a vcpkg portfile.cmake."
    )
    parser.add_argument("portfile_path", type=str,
                        help="Path to the vcpkg portfile.cmake (e.g., ports/grpc/portfile.cmake)")
    parser.add_argument("--name", type=str,
                        help="Name of the Flatpak module (defaults to port directory name).")
    parser.add_argument("--output", "-o", type=str,
                        help="Output YAML file path. If not specified, prints to stdout.")

    args = parser.parse_args()

    portfile_path = Path(args.portfile_path)
    if not portfile_path.exists():
        print(f"Error: Portfile not found at {portfile_path}")
        return 1

    package_name = args.name if args.name else portfile_path.parent.name

    module_data = parse_vcpkg_portfile(portfile_path, package_name)

    # Output the YAML
    if args.output:
        output_path = Path(args.output)
        with open(output_path, 'w') as f:
            yaml.dump(module_data, f, sort_keys=False, default_flow_style=False, indent=2)
        print(f"Flatpak module generated and saved to {output_path}")
    else:
        print(yaml.dump(module_data, sort_keys=False, default_flow_style=False, indent=2))

    return 0

if __name__ == "__main__":
    exit(main())