#!/usr/bin/env python3
"""
Phase 2 — Upload bundle to Artifactory and build Conan packages.

Run on the air-gapped machine after copying the bundle produced by fetch.py.
Reads the manifest, uploads source tarballs to Artifactory, patches
conandata.yml files to use Artifactory URLs, then builds and uploads every
Conan package.  Optionally builds and runs the test project.

Usage:
    python3 scripts/deploy.py \\
        --bundle-dir bundle/ \\
        --sources-url http://artifactory:8082/artifactory

    # Skip conan build (sources-only upload):
    python3 scripts/deploy.py --bundle-dir bundle/ --no-build

    # Also build and run the test project after provisioning:
    python3 scripts/deploy.py --bundle-dir bundle/ --run-tests

Environment variables:
    ARTIFACTORY_URL       ARTIFACTORY_USER      ARTIFACTORY_PASSWORD
    CONAN_REMOTE_NAME     CONAN_PROFILE
"""

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("ERROR: PyYAML is required.  Run: pip install pyyaml")

sys.path.insert(0, str(Path(__file__).parent))
from provision import (
    art_exists, art_upload,
    _sha256_file,
    walk_sources, _iter_git_sources, source_filename,
)


class Deployer:
    def __init__(self, args):
        self.a = args
        self.bundle_dir = Path(args.bundle_dir)
        self.recipes_dir = self.bundle_dir / "recipes"
        self.sources_dir = self.bundle_dir / "sources"

        manifest_path = self.bundle_dir / "manifest.yml"
        if not manifest_path.exists():
            sys.exit(
                f"ERROR: No manifest.yml found in {self.bundle_dir}\n"
                f"  Run fetch.py first to populate the bundle."
            )
        data = yaml.safe_load(manifest_path.read_text()) or {}
        self.pkg_list = [
            (p["name"], p["version"], p["folder"], p.get("options") or {})
            for p in data.get("packages", [])
        ]

        profile = Path(args.profile)
        if not profile.exists():
            candidate = Path(__file__).resolve().parent.parent / "profiles" / args.profile
            if candidate.exists():
                profile = candidate
        self.profile_path = profile

        self.url_map: dict = {}  # upstream URL -> Artifactory URL

    # -- source upload -------------------------------------------------------

    def _upload_file(self, src, filename):
        art_target = f"{self.a.sources_url}/{self.a.sources_repo}/{filename}"
        if art_exists(art_target, self.a.sources_user, self.a.sources_pass):
            print(f"    {filename}: already in Artifactory.")
        else:
            print(f"    Uploading {filename} ...")
            art_upload(src, art_target, self.a.sources_user, self.a.sources_pass)
            print(f"    {filename}: uploaded.")
        return art_target

    def upload_sources(self, name, version, recipe_dir):
        conandata_path = recipe_dir / "conandata.yml"
        if not conandata_path.exists():
            print("  No conandata.yml, skipping.")
            return

        conandata = yaml.safe_load(conandata_path.read_text())
        sources = conandata.get("sources", {})
        if version not in sources:
            print(f"  No sources entry for {version}.")
            return

        # --- git-based sources (tarball was archived by fetch.py) ---
        git_refs = list(_iter_git_sources(sources[version]))
        if git_refs:
            print("  Uploading git-based source ...")
            for _git_url, git_commit in git_refs:
                filename = f"{name}-{version}.tar.gz"
                tarball = self.sources_dir / filename
                if not tarball.exists():
                    sys.exit(f"ERROR: Source tarball not in bundle: {tarball}\n  Re-run fetch.py.")
                sha256 = _sha256_file(tarball)
                art_target = self._upload_file(tarball, filename)
                conandata["sources"][version] = {"url": art_target, "sha256": sha256}
            conandata_path.write_text(
                yaml.dump(conandata, default_flow_style=False, allow_unicode=True, sort_keys=False)
            )
            print("  Patched conandata.yml: git source → tarball.")
            self._patch_conanfile_git_to_tarball(recipe_dir)
            return

        # --- file-based sources ---
        print("  Uploading sources ...")
        for url, _sha256 in walk_sources(sources[version]):
            if url in self.url_map:
                continue
            filename = source_filename(url)
            cached = self.sources_dir / filename
            if not cached.exists():
                sys.exit(
                    f"ERROR: Source not found in bundle: {cached}\n"
                    f"  Re-run fetch.py to download missing sources."
                )
            self.url_map[url] = self._upload_file(cached, filename)

        # Patch conandata.yml with Artifactory URLs (single-pass)
        text = conandata_path.read_text()
        if self.url_map:
            url_re = re.compile("|".join(re.escape(u) for u in self.url_map))
            patched = url_re.sub(lambda m: self.url_map[m.group(0)], text)
        else:
            patched = text
        if patched != text:
            conandata_path.write_text(patched)
            print("  Patched conandata.yml with Artifactory URLs.")

    def _patch_conanfile_git_to_tarball(self, recipe_dir):
        conanfile = recipe_dir / "conanfile.py"
        text = conanfile.read_text()
        if "git.fetch_commit" not in text:
            return
        orig = text
        text = re.sub(
            r"(from conan\.tools\.files import\b)([^\n]+)",
            lambda m: m.group(0) if re.search(r"\bget\b", m.group(2))
                      else m.group(1) + m.group(2).rstrip() + ", get",
            text,
        )
        text = re.sub(r"\nfrom conan\.tools\.scm import Git\b[^\n]*", "", text)
        text = re.sub(
            r'git\s*=\s*Git\(self\)\s*\n(\s*)git\.fetch_commit\('
            r'\*\*self\.conan_data\["sources"\]\[self\.version\]\)',
            r'get(self, **self.conan_data["sources"][self.version], strip_root=True)',
            text,
        )
        if text != orig:
            conanfile.write_text(text)
            print("  Patched conanfile.py: git.fetch_commit() → get().")

    # -- conan build & upload ------------------------------------------------

    def build_and_upload(self, name, version, recipe_dir, options=None):
        ref = f"{name}/{version}"
        print(f"  Exporting {ref} ...")
        subprocess.run(
            ["conan", "export", str(recipe_dir), "--version", version],
            check=True,
        )
        cmd = [
            "conan", "create", str(recipe_dir),
            "--version", version,
            f"--profile:build={self.profile_path}",
            f"--profile:host={self.profile_path}",
            "--build", "missing",
            "-s", f"compiler.cppstd={self.a.cppstd}",
            "--test-folder", "",
        ]
        for opt_name, opt_val in (options or {}).items():
            if isinstance(opt_val, bool):
                conan_val = "True" if opt_val else "False"
            else:
                conan_val = str(opt_val)
            cmd += ["-o", f"{name}/*:{opt_name}={conan_val}"]
        if options:
            print(f"  Options: {options}")
        print(f"  Building {ref} ...")
        result = subprocess.run(cmd)
        if result.returncode == 6:
            print(f"  WARNING: {ref} skipped — invalid configuration for this platform.")
            return
        if result.returncode != 0:
            raise subprocess.CalledProcessError(result.returncode, result.args)
        print(f"  Uploading {ref} ...")
        subprocess.run(
            ["conan", "upload", ref, "--remote", self.a.remote_name, "--confirm"],
            check=True,
        )

    # -- test project --------------------------------------------------------

    def run_tests(self):
        repo_root = Path(__file__).resolve().parent.parent
        test_dir = repo_root / "test_project"
        if not test_dir.exists():
            print("WARNING: test_project/ not found, skipping tests.")
            return

        print("\n=== Building test project ===")
        build_dir = test_dir / "build"
        build_dir.mkdir(exist_ok=True)

        print("  conan install ...")
        subprocess.run(
            [
                "conan", "install", str(test_dir),
                f"--output-folder={build_dir}",
                f"--profile:build={self.profile_path}",
                f"--profile:host={self.profile_path}",
                "--build=missing",
                "--remote", self.a.remote_name,
            ],
            check=True,
        )

        # Locate protoc from the local Conan cache and put it on PATH
        result = subprocess.run(
            ["conan", "list", "protobuf/*:*", "--format=json"],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            import json
            try:
                data = json.loads(result.stdout)
                revs = next(iter(next(iter(data["Local Cache"].values()))["revisions"].values()))
                pkg_id = next(iter(revs["packages"].keys()))
                pkg_ref = next(iter(next(iter(data["Local Cache"].keys())).split("/")))
                protoc_result = subprocess.run(
                    ["conan", "cache", "path", f"protobuf/{pkg_id}"],
                    capture_output=True, text=True,
                )
                if protoc_result.returncode == 0:
                    import os as _os
                    protoc_bin = Path(protoc_result.stdout.strip()) / "bin"
                    _os.environ["PATH"] = str(protoc_bin) + _os.pathsep + _os.environ["PATH"]
                    print(f"  Using protoc from: {protoc_bin}")
            except Exception:
                pass  # non-fatal; CMake may find protoc another way

        # Clear stale CMake cache so protoc is re-detected
        (build_dir / "CMakeCache.txt").unlink(missing_ok=True)
        import shutil as _shutil
        proto_out = build_dir / "proto"
        if proto_out.exists():
            _shutil.rmtree(proto_out)

        print("  cmake configure ...")
        subprocess.run(
            [
                "cmake", str(test_dir),
                "-B", str(build_dir),
                f"-DCMAKE_TOOLCHAIN_FILE={build_dir}/conan_toolchain.cmake",
                "-DCMAKE_BUILD_TYPE=Release",
            ],
            check=True,
        )

        print("  cmake build ...")
        import multiprocessing
        subprocess.run(
            ["cmake", "--build", str(build_dir),
             "--parallel", str(multiprocessing.cpu_count())],
            check=True,
        )

        binary = build_dir / "protobuf_demo"
        if not binary.exists():
            print("  WARNING: protobuf_demo binary not found.")
            return
        print("  Running protobuf_demo ...")
        subprocess.run([str(binary)], check=True)
        print("  Test passed.")

    # -- orchestration -------------------------------------------------------

    def run(self):
        if not self.pkg_list:
            sys.exit("ERROR: Manifest is empty — run fetch.py first.")

        print(f"=== Deploying {len(self.pkg_list)} packages ===")
        for name, version, folder, options in self.pkg_list:
            print(f"\n=== {name}/{version} ===")
            recipe_dir = self.recipes_dir / name / folder
            if not recipe_dir.exists():
                sys.exit(
                    f"ERROR: Recipe not found in bundle: {recipe_dir}\n"
                    f"  Re-run fetch.py."
                )
            if not self.a.no_mirror:
                self.upload_sources(name, version, recipe_dir)
            if not self.a.no_build:
                self.build_and_upload(name, version, recipe_dir, options or None)

        if self.a.run_tests:
            self.run_tests()

        print("\n=== All packages deployed ===")


def main():
    repo_root = Path(__file__).resolve().parent.parent

    p = argparse.ArgumentParser(
        description="Phase 2: Upload bundle to Artifactory and build Conan packages",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--bundle-dir", metavar="DIR",
                   default=str(repo_root / "bundle"),
                   help="Bundle directory produced by fetch.py (default: ./bundle)")

    g = p.add_argument_group("Conan")
    g.add_argument("--profile", metavar="NAME_OR_PATH",
                   default=os.environ.get("CONAN_PROFILE", "linux-x86_64-gcc-cxx17"),
                   help="Conan profile name (looked up under profiles/) or absolute path")
    g.add_argument("--remote-name", metavar="NAME",
                   default=os.environ.get("CONAN_REMOTE_NAME", "artifactory"))
    g.add_argument("--cppstd", default="17", metavar="STD",
                   help="C++ standard passed to conan create (default: 17)")

    g = p.add_argument_group("Source upload")
    g.add_argument("--sources-url", metavar="URL",
                   default=os.environ.get("ARTIFACTORY_URL", "http://localhost:8082/artifactory"),
                   help="Base URL of the Artifactory instance to mirror source tarballs into")
    g.add_argument("--sources-user", metavar="USER",
                   default=os.environ.get("ARTIFACTORY_USER", "admin"),
                   help="HTTP Basic Auth user for uploading to the generic sources repo")
    g.add_argument("--sources-pass", metavar="PASS",
                   default=os.environ.get("ARTIFACTORY_PASSWORD", "password"),
                   help="HTTP Basic Auth password for uploading to the generic sources repo")
    g.add_argument("--sources-repo", default="conan-sources", metavar="REPO",
                   help="Artifactory generic repo for source tarballs (default: conan-sources)")

    g = p.add_argument_group("Skip steps")
    g.add_argument("--no-mirror", action="store_true",
                   help="Skip source upload to Artifactory")
    g.add_argument("--no-build", action="store_true",
                   help="Skip conan create and upload")

    g = p.add_argument_group("Testing")
    g.add_argument("--run-tests", action="store_true",
                   help="Build and run test_project after provisioning")

    args = p.parse_args()
    Deployer(args).run()


if __name__ == "__main__":
    main()
