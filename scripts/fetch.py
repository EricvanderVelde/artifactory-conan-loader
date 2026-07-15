#!/usr/bin/env python3
"""
Phase 1 — Analyse packages.yml and generate a download script.

Run this on the disconnected machine, which only needs access to a local
conan-center-index clone.  The script resolves the full dependency tree,
copies all required recipe files into the bundle, and writes a data file
(bundle/download_sources.json) listing every file that needs to be
downloaded, alongside the generic downloader script that reads it.

Workflow
--------
1. On the disconnected machine — run this script:

     python3 scripts/fetch.py \\
         --packages-file packages.yml \\
         --cci-path /path/to/local/conan-center-index \\
         --output-dir bundle/

   Output:
     bundle/recipes/             — recipe files (conanfile.py, conandata.yml, patches)
     bundle/manifest.yml         — resolved build order with versions and options
     bundle/download_sources.json — generated data: every URL/checksum to fetch
     bundle/download_sources.py  — generic downloader script (copied verbatim)

2. Take bundle/download_sources.json and bundle/download_sources.py to a
   connected machine (same directory) and run:

     python3 bundle/download_sources.py

   Downloads are saved to bundle/sources/ next to the JSON file.

3. Copy bundle/ back to the disconnected machine.

4. On the disconnected machine — run deploy.py:

     python3 scripts/deploy.py --bundle-dir bundle/

Environment variables:
    CCI_PATH   Path to an existing local CCI clone
    CCI_URL    URL to clone conan-center-index from (only if CCI_PATH is absent)
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("ERROR: PyYAML is required.  Run: pip install pyyaml")

sys.path.insert(0, str(Path(__file__).parent))
from provision import (
    CCI_DEFAULT_URL,
    _ver_tuple, version_in_range,
    extract_default_options, extract_raw_requires, parse_dep_ref,
    walk_sources, _iter_git_sources, source_filename,
    _parse_conandata_sources,
    load_packages_file,
)

class Fetcher:
    def __init__(self, args):
        self.a = args
        self.output_dir = Path(args.output_dir)
        self.recipes_dir = self.output_dir / "recipes"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.recipes_dir.mkdir(parents=True, exist_ok=True)

        # CCI: use --cci-path if given, else clone from --cci-url
        if args.cci_path:
            self.cci_path = Path(args.cci_path)
        else:
            self.cci_path = self.output_dir / "conan-center-index"

        if not self.cci_path.exists():
            cci_url = args.cci_url or CCI_DEFAULT_URL
            print(f"Cloning conan-center-index from {cci_url} ...")
            subprocess.run(
                ["git", "clone", "--depth=1", cci_url, str(self.cci_path)],
                check=True,
            )

        self._config_cache: dict = {}
        self._sources_cache: dict = {}
        self.pins: dict = {}
        for pin in (args.pin or []):
            if "=" in pin:
                n, v = pin.split("=", 1)
                self.pins[n.strip()] = v.strip()

    # -- CCI helpers ---------------------------------------------------------

    def _cci_read_optional(self, rel_path):
        p = self.cci_path / rel_path
        return p.read_text() if p.exists() else None

    def _fetch_config(self, name):
        if name not in self._config_cache:
            text = self._cci_read_optional(f"recipes/{name}/config.yml")
            self._config_cache[name] = yaml.safe_load(text) or {} if text else {}
        return self._config_cache[name]

    def _versions_with_sources(self, name, folder):
        key = (name, folder)
        if key not in self._sources_cache:
            local = self.recipes_dir / name / folder / "conandata.yml"
            if local.exists():
                self._sources_cache[key] = _parse_conandata_sources(local.read_text())
            else:
                text = self._cci_read_optional(f"recipes/{name}/{folder}/conandata.yml")
                self._sources_cache[key] = _parse_conandata_sources(text) if text else None
        return self._sources_cache[key]

    # -- version picking -----------------------------------------------------

    def _auto_folder(self, name, version):
        versions = self._fetch_config(name).get("versions", {})
        if version not in versions:
            sys.exit(
                f"ERROR: {name}/{version} not found in CCI config.yml.\n"
                f"  Available: {sorted(versions.keys())}"
            )
        return versions[version]["folder"]

    def pick_version(self, dep_name, range_spec):
        all_versions = self._fetch_config(dep_name).get("versions", {})
        if dep_name in self.pins:
            pinned = self.pins[dep_name]
            if version_in_range(pinned, range_spec) and pinned in all_versions:
                return pinned, all_versions[pinned]["folder"]
            print(f"  WARNING: Pin {dep_name}={pinned} ignored — doesn't satisfy {range_spec}.")
        candidates = sorted(
            [v for v in all_versions if version_in_range(v, range_spec)],
            key=_ver_tuple,
            reverse=True,
        )
        for v in candidates:
            folder = all_versions[v]["folder"]
            with_sources = self._versions_with_sources(dep_name, folder)
            if with_sources is None or v in with_sources:
                return v, folder
        return None, None

    # -- recipe copying ------------------------------------------------------

    def copy_recipe(self, name, version, folder):
        if folder is None:
            folder = self._auto_folder(name, version)
        dest = self.recipes_dir / name / folder
        if not dest.exists():
            src = self.cci_path / "recipes" / name / folder
            if not src.exists():
                sys.exit(f"ERROR: Recipe not found in CCI clone: {src}")
            shutil.copytree(
                src, dest,
                ignore=shutil.ignore_patterns("test_package", "test_v1_package"),
            )
        return dest, folder

    # -- dependency resolution -----------------------------------------------

    def resolve_packages(self, specs):
        order = []
        resolved = set()
        visiting = set()  # cycle detection

        def visit(name, version, folder, options=None, depth=0, label=None):
            indent = "  " * depth
            ref = f"{name}/{version}"
            line = label or ref
            if ref in resolved:
                return  # already printed and fully resolved elsewhere — no need to repeat it
            if ref in visiting:
                print(f"{indent}{line}  (circular — already resolving, skipped)")
                return
            visiting.add(ref)
            print(f"{indent}{line}")
            recipe_dir, actual_folder = self.copy_recipe(name, version, folder)
            conanfile_path = recipe_dir / "conanfile.py"
            if conanfile_path.exists():
                text = conanfile_path.read_text()
                filter_opts = {**extract_default_options(text), **(options or {})}
                for dep_ref, _ in extract_raw_requires(text, options=filter_opts):
                    dep_name, dep_range = parse_dep_ref(dep_ref)
                    best, best_folder = self.pick_version(dep_name, dep_range)
                    if best is None:
                        print(f"{indent}  WARNING: No version of {dep_name} satisfies {dep_range} — skipping.")
                        continue
                    visit(dep_name, best, best_folder, options=None, depth=depth + 1,
                          label=f"{dep_name}: {dep_range}  →  {best}")
            visiting.discard(ref)
            resolved.add(ref)
            order.append((name, version, actual_folder, options))

        for name, version, folder, options in specs:
            visit(name, version, folder, options)
        return order

    # -- download data generation ---------------------------------------------

    def generate_download_data(self, pkg_list):
        """Write bundle/download_sources.json and copy the downloader script next to it."""
        seen_urls: set = set()
        packages: list[dict] = []

        for name, version, folder, _options in pkg_list:
            conandata_path = self.recipes_dir / name / folder / "conandata.yml"
            if not conandata_path.exists():
                continue
            conandata = yaml.safe_load(conandata_path.read_text())
            sources = conandata.get("sources", {})
            if version not in sources:
                continue

            entries: list[dict] = []

            git_refs = list(_iter_git_sources(sources[version]))
            if git_refs:
                for git_url, git_commit in git_refs:
                    entries.append({"type": "git", "url": git_url, "commit": git_commit})
            else:
                for url, sha256 in walk_sources(sources[version]):
                    if url in seen_urls:
                        continue
                    seen_urls.add(url)
                    entry = {"type": "download", "url": url, "file": source_filename(url)}
                    if sha256:
                        entry["sha256"] = sha256
                    entries.append(entry)

            if entries:
                packages.append({"name": name, "version": version, "sources": entries})

        data_path = self.output_dir / "download_sources.json"
        data_path.write_text(json.dumps({"packages": packages}, indent=2) + "\n")

        script_src = Path(__file__).resolve().parent / "download_sources.py"
        script_path = self.output_dir / "download_sources.py"
        shutil.copyfile(script_src, script_path)
        script_path.chmod(0o755)

        return data_path, script_path

    # -- pinning ---------------------------------------------------------------

    def _auto_pin_top_level(self, specs):
        """Pin every top-level ref's version so transitive deps reuse it instead
        of picking the newest CCI version that satisfies their range.

        --pin on the CLI always wins (self.pins is pre-populated from it before
        this runs). A name listed at the top level with two different versions
        is ambiguous, so it's left alone rather than guessing.
        """
        versions_by_name: dict = {}
        for name, version, _folder, _options in specs:
            versions_by_name.setdefault(name, set()).add(version)

        for name, versions in versions_by_name.items():
            if name in self.pins:
                continue
            if len(versions) > 1:
                print(f"  NOTE: {name} listed at top level with multiple versions "
                      f"{sorted(versions)} — not auto-pinning.")
                continue
            self.pins[name] = next(iter(versions))

    # -- orchestration -------------------------------------------------------

    def run(self, cli_specs):
        if self.a.packages_file:
            specs = load_packages_file(self.a.packages_file)
        else:
            specs = []
            for spec in cli_specs:
                folder = None
                if ":" in spec:
                    spec, folder = spec.rsplit(":", 1)
                if "/" not in spec:
                    sys.exit(f"ERROR: Invalid spec '{spec}'. Expected name/version[:folder].")
                name, version = spec.split("/", 1)
                specs.append((name, version, folder, {}))

        self._auto_pin_top_level(specs)

        print("=== Resolving dependency tree ===")
        pkg_list = self.resolve_packages(specs)

        print(f"\n=== Build order ({len(pkg_list)} packages) ===")
        for name, version, folder, options in pkg_list:
            opt_hint = f"  {options}" if options else ""
            print(f"  {name}/{version}  [{folder}]{opt_hint}")

        manifest = {
            "packages": [
                {"name": n, "version": v, "folder": f, "options": dict(opts) if opts else {}}
                for n, v, f, opts in pkg_list
            ]
        }
        manifest_path = self.output_dir / "manifest.yml"
        manifest_path.write_text(
            yaml.dump(manifest, default_flow_style=False, allow_unicode=True, sort_keys=False)
        )

        print("\n=== Generating download data ===")
        data_path, script_path = self.generate_download_data(pkg_list)

        print(f"\n=== Done ===")
        print(f"  Recipes  : {self.recipes_dir}/")
        print(f"  Manifest : {manifest_path}")
        print(f"  Data     : {data_path}")
        print(f"  Script   : {script_path}")
        print(f"\nNext steps:")
        print(f"  1. Take {data_path} and {script_path} to a connected machine")
        print(f"     (same directory) and run:")
        print(f"       python3 {script_path.name}")
        print(f"     Sources will be saved to {self.output_dir}/sources/")
        print(f"  2. Copy {self.output_dir}/ back to this machine.")
        print(f"  3. Run deploy.py:")
        print(f"       python3 scripts/deploy.py --bundle-dir {self.output_dir}")


def main():
    repo_root = Path(__file__).resolve().parent.parent

    p = argparse.ArgumentParser(
        description="Phase 1: Resolve packages and generate a source download script",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("packages", nargs="*", metavar="PKG_SPEC",
                   help="name/version or name/version:folder  "
                        "(omit when using --packages-file)")
    p.add_argument("--packages-file", metavar="FILE",
                   help="YAML file listing packages and options")
    p.add_argument("--output-dir", metavar="DIR",
                   default=str(repo_root / "bundle"),
                   help="Directory for the output bundle (default: ./bundle)")

    g = p.add_argument_group("conan-center-index")
    g.add_argument("--cci-url", metavar="URL",
                   default=os.environ.get("CCI_URL", ""),
                   help=f"URL to clone conan-center-index from "
                        f"(default: {CCI_DEFAULT_URL}). "
                        f"Point this at a local git mirror.")
    g.add_argument("--cci-path", metavar="PATH",
                   default=os.environ.get("CCI_PATH", ""),
                   help="Path to an existing local CCI clone "
                        "(skips cloning; takes precedence over --cci-url)")

    g = p.add_argument_group("Dependency resolution")
    g.add_argument("--pin", metavar="name=version", action="append",
                   help="Pin a dependency to a specific version (may be repeated). "
                        "Every top-level PKG_SPEC/--packages-file ref is pinned "
                        "automatically; use --pin to override or to pin a "
                        "transitive dependency that isn't listed directly.")

    args = p.parse_args()
    if not args.packages and not args.packages_file:
        p.error("Provide at least one PKG_SPEC or --packages-file.")

    Fetcher(args).run(args.packages)


if __name__ == "__main__":
    main()
