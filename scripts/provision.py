#!/usr/bin/env python3
"""
Provision Conan packages from conan-center-index into Artifactory.

Fetches recipes from GitHub, mirrors sources to Artifactory, then builds
and uploads Conan packages — driven entirely by the package specs you supply.

With --auto-deps (or when only root packages are given), dependencies are
resolved automatically by analysing each recipe's conanfile.py. You only
need to specify the packages you actually want; transitive deps are resolved
and provisioned in the correct build order.

Usage:
    python3 scripts/provision.py [OPTIONS] PKG_SPEC [PKG_SPEC ...]

PKG_SPEC formats:
    name/version              auto-detect recipe folder from config.yml
    name/version:folder       use the specified recipe folder

Examples:
    # Explicit list (deps must be in order):
    python3 scripts/provision.py \\
        cmake/3.31.12:binary zlib/1.3.2 \\
        abseil/20260107.1 protobuf/6.33.5

    # Auto-resolve deps — just name what you want:
    python3 scripts/provision.py --auto-deps protobuf/6.33.5 protobuf/5.29.6

Environment variables (each overridable via a flag):
    ARTIFACTORY_URL       ARTIFACTORY_USER      ARTIFACTORY_PASSWORD
    CONAN_REMOTE_NAME     CONAN_PROFILE         GITHUB_TOKEN
    CCI_PATH              # path to a local conan-center-index clone

Recommended first-time setup (avoids GitHub API rate limits):
    git clone --depth=1 https://github.com/conan-io/conan-center-index ~/cci
    python3 scripts/provision.py --cci-path ~/cci --packages-file packages.yml
"""

import argparse
import base64
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("ERROR: PyYAML is required.  Run: pip install pyyaml")


GITHUB_RAW = "https://raw.githubusercontent.com/conan-io/conan-center-index/master"
GITHUB_API = "https://api.github.com/repos/conan-io/conan-center-index/contents"


# ---------------------------------------------------------------------------
# GitHub helpers
# ---------------------------------------------------------------------------

def _gh_headers(token=""):
    h = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "conan2-provision-script",
    }
    if token:
        h["Authorization"] = f"token {token}"
    return h


def gh_fetch_text(path, token=""):
    url = f"{GITHUB_RAW}/{path}"
    req = urllib.request.Request(url, headers=_gh_headers(token))
    try:
        with urllib.request.urlopen(req) as r:
            return r.read().decode()
    except urllib.error.HTTPError as e:
        sys.exit(f"ERROR: Could not fetch {url} — HTTP {e.code}")


def gh_list_dir(path, token=""):
    url = f"{GITHUB_API}/{path}?ref=master"
    req = urllib.request.Request(url, headers=_gh_headers(token))
    try:
        with urllib.request.urlopen(req) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        sys.exit(f"ERROR: Could not list {url} — HTTP {e.code}")


def gh_fetch_recipe_dir(github_path, local_dir, token="", skip=None):
    """Recursively download a GitHub directory into local_dir."""
    skip = skip or set()
    local_dir.mkdir(parents=True, exist_ok=True)
    for item in gh_list_dir(github_path, token):
        if item["name"] in skip:
            continue
        dest = local_dir / item["name"]
        if item["type"] == "file":
            if not dest.exists():
                print(f"      {item['name']}")
                dest.write_text(gh_fetch_text(f"{github_path}/{item['name']}", token))
        elif item["type"] == "dir":
            gh_fetch_recipe_dir(f"{github_path}/{item['name']}", dest, token, skip)


# ---------------------------------------------------------------------------
# Version comparison and range matching
# ---------------------------------------------------------------------------

def _ver_tuple(v):
    """Parse a version string into a comparable tuple of integers."""
    parts = []
    for seg in re.split(r"[.\-+]", v):
        try:
            parts.append(int(seg))
        except ValueError:
            parts.append(seg)
    return parts


def _ver_cmp(a, b):
    """Return -1/0/1 comparing two version strings."""
    at, bt = _ver_tuple(a), _ver_tuple(b)
    # Pad shorter tuple so (1, 3) vs (1, 3, 2) gives (1,3,0) < (1,3,2)
    length = max(len(at), len(bt))
    at = (at + [0] * length)[:length]
    bt = (bt + [0] * length)[:length]
    for x, y in zip(at, bt):
        # Mixed int/str: ints sort before strings
        if type(x) != type(y):
            return -1 if isinstance(x, int) else 1
        if x < y:
            return -1
        if x > y:
            return 1
    return 0


def version_in_range(version, range_spec):
    """Return True if version satisfies a Conan version range or exact match.

    Examples of range_spec: "1.3.2", "[>=1.2.11 <2]", "[>=20230802.1 <=20260107.1]"
    """
    range_spec = range_spec.strip()
    if not range_spec.startswith("["):
        return version == range_spec

    for token in range_spec.strip("[]").split():
        m = re.fullmatch(r"(>=|<=|>|<|==|!=)(.*)", token)
        if not m:
            continue
        op, bound = m.group(1), m.group(2)
        cmp = _ver_cmp(version, bound)
        if op == ">=" and cmp < 0:  return False
        if op == "<=" and cmp > 0:  return False
        if op == ">"  and cmp <= 0: return False
        if op == "<"  and cmp >= 0: return False
        if op == "==" and cmp != 0: return False
        if op == "!=" and cmp == 0: return False

    return True


# ---------------------------------------------------------------------------
# Dependency extraction from conanfile.py
# ---------------------------------------------------------------------------

# Conan OS name for the host running this script (used to skip foreign-OS deps)
_HOST_CONAN_OS = {"Linux": "Linux", "Windows": "Windows", "Darwin": "Macos"}.get(
    platform.system(), "Linux"
)
# All known Conan OS names — anything that isn't the host OS gets filtered out
_ALL_CONAN_OS = {
    "Linux", "Windows", "Macos", "Android", "iOS", "tvOS",
    "watchOS", "FreeBSD", "SunOS", "AIX", "Neutrino", "Arduino",
    "Emscripten", "VxWorks",
}
_SKIP_OS = _ALL_CONAN_OS - {_HOST_CONAN_OS}
_OS_EQ_RE = re.compile(
    r'settings(?:_build)?\.os\s*==\s*["\'](' + '|'.join(re.escape(o) for o in _SKIP_OS) + r')["\']'
)
_OS_IN_RE = re.compile(
    r'settings(?:_build)?\.os\s+in\s+\[.*["\'](' + '|'.join(re.escape(o) for o in _SKIP_OS) + r')["\']'
)


def extract_default_options(conanfile_text):
    """Return a dict of option_name -> bool/value from a conanfile.py default_options.

    Only bool False values meaningfully suppress conditional requires, but all
    values are returned so equality checks (e.g. with_ssl == "openssl") work.
    Falls back to an empty dict if the dict can't be parsed.
    """
    opts = {}
    m = re.search(r'\bdefault_options\s*=\s*\{([^}]*)\}', conanfile_text, re.DOTALL)
    if not m:
        return opts
    for entry in re.finditer(r'["\'](\w+)["\']\s*:\s*([^,\n]+)', m.group(1)):
        key = entry.group(1)
        val = entry.group(2).strip().rstrip(',').strip()
        if val == 'True':
            opts[key] = True
        elif val == 'False':
            opts[key] = False
        else:
            opts[key] = val.strip('"\'')
    return opts


def _gather_cond(lines, if_lineno, cond):
    """If *cond* has unbalanced open parens (multi-line condition), gather continuation
    lines forward until the parens balance.  Returns the joined condition string."""
    open_p = cond.count("(") - cond.count(")")
    j = if_lineno + 1
    while open_p > 0 and j < len(lines) and j < if_lineno + 15:
        cont = lines[j].strip()
        open_p += cont.count("(") - cont.count(")")
        cond += " " + cont.rstrip("):, \t")
        j += 1
    return cond


def _cond_skip(cond, options):
    """Return True if an if/elif condition means the block should be skipped.

    Handles:
    * OS filter — settings.os / settings_build.os that is not the host OS
    * Equality — ``options.X == "val"`` where X has a different value
    * Truthy — ``options.X`` is False; uses OR/AND-aware logic so that
      ``if (A or B):`` is only skipped when ALL mentioned options are False
    """
    if _OS_EQ_RE.search(cond) or _OS_IN_RE.search(cond):
        return True

    if not options:
        return False

    # Equality check: options.X == "value"  (covers elif SSL-variant chains)
    om = re.search(r'self\.options\.(\w+)\s*==\s*["\']([^"\']+)["\']', cond)
    if om:
        opt, expected = om.group(1), om.group(2)
        if opt in options and options[opt] != expected:
            return True

    # Truthy checks — collect all directly-mentioned options (not equality, not get_safe)
    opt_values = []
    for om in re.finditer(r'\bself\.options\.(?!get_safe\b)(\w+)\b(?!\s*[!=<>])', cond):
        opt = om.group(1)
        opt_values.append(options.get(opt))  # None = unspecified (unknown)
    for om in re.finditer(r'self\.options\.get_safe\(["\'](\w+)["\']', cond):
        opt = om.group(1)
        opt_values.append(options.get(opt))

    if not opt_values:
        return False

    has_or = bool(re.search(r'\bor\b', cond))
    if has_or:
        # OR chain: skip only when ALL options are explicitly False (no unknowns)
        return all(v is False for v in opt_values) and all(v is not None for v in opt_values)
    else:
        # AND chain / simple check: skip if any option is False
        return any(v is False for v in opt_values)


def extract_raw_requires(conanfile_text, options=None):
    """Return list of (ref, is_tool_require) extracted from conanfile.py.

    Conditional requires are filtered in three ways:

    * **OS filter** — blocks for a non-host OS (``settings.os`` or
      ``settings_build.os``) are skipped, e.g. Windows-only tool_requires
      like msys2 are never pulled in when provisioning for Linux.

    * **Equality filter** — ``elif self.options.with_ssl == "libressl":``
      blocks are skipped when ``with_ssl`` has a different value, preventing
      alternative-implementation deps from being provisioned.

    * **Truthy filter** — ``if self.options.with_gsl:`` blocks are skipped
      when the option is False.  Multi-line ``if (A or B or C):`` conditions
      are gathered completely; OR chains are only skipped when *all* options
      are False.

    When *options* is ``None``, no option filtering is applied — pass
    ``extract_default_options()`` output if recipe defaults should be respected.
    """
    lines = conanfile_text.splitlines()
    results = []

    for m in re.finditer(
        r"self\.(tool_requires|requires)\s*\(\s*[fF]?[\"']([^\"']+)[\"']",
        conanfile_text,
    ):
        is_tool = m.group(1) == "tool_requires"
        ref = m.group(2)
        if "/" not in ref:
            continue

        # Determine indentation of this requires line
        req_lineno = conanfile_text[:m.start()].count("\n")
        req_line = lines[req_lineno] if req_lineno < len(lines) else ""
        req_indent = len(req_line) - len(req_line.lstrip())

        skip = False
        # Walk backwards looking for enclosing if/elif-blocks at lower indentation
        for i in range(req_lineno - 1, max(-1, req_lineno - 15), -1):
            if i < 0:
                break
            line = lines[i]
            stripped = line.lstrip()
            if not stripped:
                continue
            indent = len(line) - len(stripped)
            if indent >= req_indent:
                continue  # same or deeper level — not an enclosing block

            is_if = stripped.startswith("if ")
            is_elif = stripped.startswith("elif ")
            if not is_if and not is_elif:
                continue  # enclosing block but not a conditional

            cond = stripped[3 if is_if else 5:].rstrip(": \t")
            cond = _gather_cond(lines, i, cond)  # collect multi-line conditions

            if _cond_skip(cond, options):
                skip = True
                break
            req_indent = indent  # narrow scope: only look for blocks enclosing this one

        if not skip:
            results.append((ref, is_tool))

    return results


def parse_dep_ref(ref):
    """Split 'name/[>=1.0 <2]' or 'name/1.3' into (name, version_range)."""
    slash = ref.index("/")
    return ref[:slash], ref[slash + 1:]


# ---------------------------------------------------------------------------
# Source URL helpers
# ---------------------------------------------------------------------------

def source_filename(url):
    """Derive a descriptive local filename for an upstream source URL.

    GitHub archive URLs (including .../archive/refs/tags/vX.Y.Z.tar.gz style)
    get the repo name prepended so the stored name is unique and readable,
    e.g. abseil-cpp-20260107.1.tar.gz or protobuf-v3.21.12.tar.gz.
    Only the final path component is used so slashes never end up in the name.
    """
    m = re.search(r"github\.com/[^/]+/([^/]+)/archive/(.+)", url)
    if m:
        repo = m.group(1)
        basename = m.group(2).split("/")[-1]   # drop refs/tags/ prefix if present
        return f"{repo}-{basename}"
    return url.rstrip("/").split("/")[-1]


def walk_sources(node):
    """Yield (url, sha256) from file-based conandata source entries (skip git sources)."""
    if isinstance(node, dict):
        if "commit" in node:
            return  # git-based source — handled by _iter_git_sources
        if "url" in node:
            url = node["url"]
            sha256 = node.get("sha256", "")
            if isinstance(url, list):
                for u in url:
                    yield u, sha256
            else:
                yield url, sha256
        else:
            for v in node.values():
                yield from walk_sources(v)
    elif isinstance(node, list):
        for item in node:
            yield from walk_sources(item)


def _iter_git_sources(node):
    """Yield (url, commit) pairs from git-based conandata source entries."""
    if isinstance(node, dict):
        if "commit" in node:
            yield node.get("url", ""), node["commit"]
        else:
            for v in node.values():
                yield from _iter_git_sources(v)


def _parse_conandata_sources(text):
    """Return set of version strings from conandata.yml text, or None on parse failure."""
    try:
        data = yaml.safe_load(text) or {}
        return {str(k) for k in data.get("sources", {}).keys()}
    except Exception:
        return None


def _sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# Artifactory helpers
# ---------------------------------------------------------------------------

def _basic_auth(user, password):
    return base64.b64encode(f"{user}:{password}".encode()).decode()


def art_exists(url, user, password):
    req = urllib.request.Request(url, method="HEAD")
    req.add_header("Authorization", f"Basic {_basic_auth(user, password)}")
    try:
        urllib.request.urlopen(req)
        return True
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False
        raise


def art_upload(local_path, target_url, user, password):
    subprocess.run(
        ["curl", "--silent", "--show-error", "--fail",
         "-u", f"{user}:{password}", "-T", str(local_path), target_url],
        check=True,
    )


# ---------------------------------------------------------------------------
# Packages YAML file
# ---------------------------------------------------------------------------

def load_packages_file(path):
    """Parse a packages YAML file and return list of (name, version, folder, options).

    YAML format::

        packages:
          - ref: cmake/3.31.12:binary
          - ref: zlib/1.3.2
          - ref: opentelemetry-cpp/1.17.0
            options:
              with_gsl: true
              with_prometheus: false
              with_otlp_grpc: true
    """
    data = yaml.safe_load(Path(path).read_text()) or {}
    specs = []
    for entry in data.get("packages", []):
        ref = entry.get("ref", "")
        raw_opts = entry.get("options") or {}
        folder = None
        if ":" in ref:
            ref, folder = ref.rsplit(":", 1)
        if "/" not in ref:
            sys.exit(f"ERROR: Invalid ref '{ref}' in {path}. Expected name/version.")
        name, version = ref.split("/", 1)
        # Normalise option values: YAML bools → Python bools, everything else → str
        options = {}
        for k, v in raw_opts.items():
            options[str(k)] = v  # keep Python bool / int / str as-is
        specs.append((name, version, folder, options))
    return specs


# ---------------------------------------------------------------------------
# Provisioner
# ---------------------------------------------------------------------------

class Provisioner:
    def __init__(self, args):
        self.a = args
        self.recipes_dir = Path(args.recipes_dir)
        self.cache_dir = Path(args.cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        # upstream URL -> Artifactory URL; built up as sources are mirrored
        self.url_map: dict = {}
        # Cache: name -> full config.yml dict
        self._config_cache: dict = {}
        # Cache: (name, folder) -> set of version strings that have sources entries
        self._sources_cache: dict = {}
        # Explicit version pins from --pin flags
        self.pins: dict = {}
        for pin in (args.pin or []):
            if "=" in pin:
                n, v = pin.split("=", 1)
                self.pins[n.strip()] = v.strip()

        # Resolve profile path
        profile = Path(args.profile)
        if not profile.exists():
            candidate = Path(__file__).resolve().parent.parent / "profiles" / args.profile
            if candidate.exists():
                profile = candidate
        self.profile_path = profile

        # Local conan-center-index clone (replaces GitHub API when set)
        self.cci_path: Path | None = None
        if args.cci_path:
            self.cci_path = Path(args.cci_path)
            if not self.cci_path.exists():
                print(f"  Cloning conan-center-index into {self.cci_path} ...")
                subprocess.run(
                    ["git", "clone", "--depth=1",
                     "https://github.com/conan-io/conan-center-index",
                     str(self.cci_path)],
                    check=True,
                )

    # -- index helpers (local clone or GitHub API) ---------------------------

    def _index_read_text(self, rel_path):
        """Read a file from the CCI index — local clone or GitHub API."""
        if self.cci_path:
            p = self.cci_path / rel_path
            if not p.exists():
                sys.exit(f"ERROR: {p} not found in local conan-center-index clone.")
            return p.read_text()
        return gh_fetch_text(rel_path, self.a.github_token)

    def _index_read_text_optional(self, rel_path):
        """Like _index_read_text but returns None instead of exiting on missing."""
        if self.cci_path:
            p = self.cci_path / rel_path
            return p.read_text() if p.exists() else None
        try:
            return gh_fetch_text(rel_path, self.a.github_token)
        except SystemExit:
            return None

    # -- recipe fetch --------------------------------------------------------

    def _fetch_config(self, name):
        """Fetch and cache config.yml for a package from conan-center-index."""
        if name not in self._config_cache:
            text = self._index_read_text_optional(f"recipes/{name}/config.yml")
            self._config_cache[name] = yaml.safe_load(text) or {} if text else {}
        return self._config_cache[name]

    def _versions_with_sources(self, name, folder):
        """Return set of version strings that have a sources entry in conandata.yml.

        Prefers the local recipes dir (already copied/trimmed) over the index,
        so the version picker never selects a version we can't actually build.
        Returns None when conandata.yml is unavailable (treated as unconstrained).
        """
        key = (name, folder)
        if key not in self._sources_cache:
            local = self.recipes_dir / name / folder / "conandata.yml"
            if local.exists():
                self._sources_cache[key] = _parse_conandata_sources(local.read_text())
            else:
                text = self._index_read_text_optional(
                    f"recipes/{name}/{folder}/conandata.yml"
                )
                self._sources_cache[key] = _parse_conandata_sources(text) if text else None
        return self._sources_cache[key]

    def _auto_folder(self, name, version):
        """Look up the recipe folder for name/version from the index config.yml."""
        versions = self._fetch_config(name).get("versions", {})
        if version not in versions:
            sys.exit(
                f"ERROR: {name}/{version} not found in conan-center-index config.yml.\n"
                f"  Available versions: {sorted(versions.keys())}"
            )
        return versions[version]["folder"]

    def fetch_recipe(self, name, version, folder):
        if folder is None:
            folder = self._auto_folder(name, version)

        dest = self.recipes_dir / name / folder
        if dest.exists() and not self.a.refetch:
            print(f"  Recipe already at {dest}  (use --refetch to re-download)")
            return dest, folder

        if dest.exists():
            shutil.rmtree(dest)

        if self.cci_path:
            src = self.cci_path / "recipes" / name / folder
            if not src.exists():
                sys.exit(f"ERROR: Recipe not found in local CCI clone: {src}")
            print(f"  Copying recipe from local clone (folder: {folder}) ...")
            shutil.copytree(
                src, dest,
                ignore=shutil.ignore_patterns("test_package", "test_v1_package"),
            )
        else:
            print(f"  Fetching recipe from conan-center-index (folder: {folder}) ...")
            gh_fetch_recipe_dir(
                f"recipes/{name}/{folder}", dest,
                token=self.a.github_token,
                skip={"test_package"},
            )
        return dest, folder

    # -- dependency resolution -----------------------------------------------

    def fetch_available_versions(self, name):
        """Return all versions available for a package in conan-center-index."""
        return list(self._fetch_config(name).get("versions", {}).keys())

    def pick_version(self, dep_name, range_spec):
        """Return (version, folder) — latest version satisfying range_spec that also
        has a real sources entry in conandata.yml.  Falls back through older candidates
        if the newest version isn't in conandata.yml yet (e.g. a very new cmake release).
        Returns (None, None) if nothing satisfies the range.

        Respects --pin overrides: if dep_name is pinned, that version is used as long
        as it satisfies the range.
        """
        all_versions = self._fetch_config(dep_name).get("versions", {})

        # --pin override
        if dep_name in self.pins:
            pinned = self.pins[dep_name]
            if version_in_range(pinned, range_spec) and pinned in all_versions:
                return pinned, all_versions[pinned]["folder"]
            print(
                f"  WARNING: Pinned {dep_name}={pinned} does not satisfy "
                f"{range_spec} or is unknown — ignoring pin."
            )

        candidates = sorted(
            [v for v in all_versions if version_in_range(v, range_spec)],
            key=_ver_tuple,
            reverse=True,  # latest first so we exit early on the common case
        )

        for v in candidates:
            folder = all_versions[v]["folder"]
            with_sources = self._versions_with_sources(dep_name, folder)
            # None means we couldn't fetch conandata.yml — assume it's fine
            if with_sources is None or v in with_sources:
                return v, folder

        return None, None

    def collect_packages(self, specs):
        """Resolve full dependency tree for all specs; return list in build order.

        *specs* is a list of ``(name, version, folder, options)`` tuples.

        Uses DFS topological sort so every dependency appears before the package
        that requires it.  Duplicate packages (diamond deps) are deduplicated.

        Options are used in two ways for root packages:
        * Option-gated ``requires`` whose option is explicitly disabled are not
          followed (so disabled deps are not provisioned).
        * The options dict is carried to ``build_and_upload`` so the package is
          built with the requested flags; unspecified options use the recipe's
          own ``default_options`` — they are never overridden.

        Automatically-resolved transitive dependencies use no option overrides;
        their recipe defaults are always honoured.
        """
        order = []
        resolved = set()
        visiting = set()  # cycle detection

        def visit(name, version, folder, options=None):
            ref = f"{name}/{version}"
            if ref in resolved:
                return
            if ref in visiting:
                print(f"  WARNING: Circular dependency at {ref}, skipping.")
                return

            visiting.add(ref)
            print(f"\n  Resolving deps for {ref} ...")

            # Fetch recipe so we can parse it (idempotent if already present)
            recipe_dir, actual_folder = self.fetch_recipe(name, version, folder)

            conanfile_path = recipe_dir / "conanfile.py"
            if conanfile_path.exists():
                conanfile_text = conanfile_path.read_text()
                # Root packages use explicit options; transitive deps fall back to
                # the recipe's own default_options so features disabled by default
                # (e.g. libcurl's with_ldap=False, with_zstd=False) are not provisioned.
                filter_opts = options if options is not None else extract_default_options(conanfile_text)
                for dep_ref, _ in extract_raw_requires(conanfile_text, options=filter_opts):
                    dep_name, dep_range = parse_dep_ref(dep_ref)
                    best, best_folder = self.pick_version(dep_name, dep_range)
                    if best is None:
                        print(f"  WARNING: No version of {dep_name} satisfies {dep_range} — skipping.")
                        continue
                    print(f"    {dep_name}: {dep_range}  →  {best}")
                    visit(dep_name, best, best_folder, options=None)

            visiting.discard(ref)
            resolved.add(ref)
            order.append((name, version, actual_folder, options))

        for name, version, folder, options in specs:
            visit(name, version, folder, options)

        return order

    # -- source mirroring ----------------------------------------------------

    def _clone_and_archive(self, name, version, git_url, git_commit):
        """Clone a git repo at a specific commit and return a tar.gz archive path.

        The archive is cached in self.cache_dir so repeated runs are fast.
        """
        filename = f"{name}-{version}.tar.gz"
        cached = self.cache_dir / filename
        if cached.exists():
            print(f"    {filename}: cache hit.")
            return cached

        print(f"    Cloning {git_url} at {git_commit[:8]} ...")
        with tempfile.TemporaryDirectory() as tmpdir:
            subprocess.run(["git", "init", tmpdir],
                           check=True, capture_output=True)
            subprocess.run(["git", "-C", tmpdir, "remote", "add", "origin", git_url],
                           check=True, capture_output=True)
            subprocess.run(["git", "-C", tmpdir, "fetch", "--depth=1", "origin", git_commit],
                           check=True)
            with open(cached, "wb") as fh:
                subprocess.run(
                    ["git", "-C", tmpdir, "archive",
                     f"--prefix={name}-{version}/", "--format=tar.gz", "FETCH_HEAD"],
                    stdout=fh, check=True,
                )
        print(f"    {filename}: archived.")
        return cached

    def _patch_conanfile_git_to_tarball(self, recipe_dir):
        """Replace git.fetch_commit() with get() in a conanfile.py.

        Patches both the import line and the source() method body so the recipe
        fetches the pre-archived tarball from Artifactory instead of cloning.
        """
        conanfile = recipe_dir / "conanfile.py"
        text = conanfile.read_text()
        if "git.fetch_commit" not in text:
            return

        orig = text
        # Ensure 'get' is in the conan.tools.files import
        text = re.sub(
            r'(from conan\.tools\.files import\b)([^\n]+)',
            lambda m: m.group(0) if re.search(r'\bget\b', m.group(2))
                      else m.group(1) + m.group(2).rstrip() + ', get',
            text,
        )
        # Remove the now-unused Git import
        text = re.sub(r'\nfrom conan\.tools\.scm import Git\b[^\n]*', '', text)
        # Replace  git = Git(self)\n    git.fetch_commit(...)
        text = re.sub(
            r'git\s*=\s*Git\(self\)\s*\n(\s*)git\.fetch_commit\('
            r'\*\*self\.conan_data\["sources"\]\[self\.version\]\)',
            r'get(self, **self.conan_data["sources"][self.version], strip_root=True)',
            text,
        )
        if text != orig:
            conanfile.write_text(text)
            print("  Patched conanfile.py: git.fetch_commit() → get().")

    def mirror_sources(self, name, version, recipe_dir):
        conandata_path = recipe_dir / "conandata.yml"
        if not conandata_path.exists():
            print("  No conandata.yml found; skipping source mirror.")
            return

        conandata = yaml.safe_load(conandata_path.read_text())
        sources = conandata.get("sources", {})
        if version not in sources:
            print(f"  No sources entry for version {version}; skipping mirror.")
            return

        # --- git-based sources (have 'commit' key instead of 'sha256') ---
        git_refs = list(_iter_git_sources(sources[version]))
        if git_refs:
            print("  Mirroring git source ...")
            for git_url, git_commit in git_refs:
                # If the URL was already patched to Artifactory in a prior run,
                # recover the original remote URL from the CCI index.
                if git_url.startswith(self.a.artifactory_url):
                    folder = recipe_dir.name
                    orig_text = self._index_read_text_optional(
                        f"recipes/{name}/{folder}/conandata.yml"
                    )
                    if orig_text:
                        orig_data = yaml.safe_load(orig_text) or {}
                        orig_src = orig_data.get("sources", {}).get(version, {})
                        for o_url, o_commit in _iter_git_sources(orig_src):
                            if o_commit == git_commit:
                                git_url = o_url
                                break
                    else:
                        sys.exit(
                            f"ERROR: {name}/{version} source URL was already rewritten "
                            f"to Artifactory and the original URL cannot be recovered.\n"
                            f"  Delete {recipe_dir} and re-run, or pass --cci-path."
                        )

                tarball = self._clone_and_archive(name, version, git_url, git_commit)
                sha256 = _sha256_file(tarball)
                art_target = f"{self.a.artifactory_url}/{self.a.sources_repo}/{tarball.name}"

                if art_exists(art_target, self.a.artifactory_user, self.a.artifactory_pass):
                    print(f"    {tarball.name}: already in Artifactory.")
                else:
                    print(f"    Uploading {tarball.name} ...")
                    art_upload(tarball, art_target,
                               self.a.artifactory_user, self.a.artifactory_pass)
                    print(f"    {tarball.name}: uploaded.")

                # Rewrite the sources entry: {url, commit} → {url, sha256}
                conandata["sources"][version] = {"url": art_target, "sha256": sha256}

            conandata_path.write_text(
                yaml.dump(conandata, default_flow_style=False,
                          allow_unicode=True, sort_keys=False)
            )
            print("  Patched conandata.yml: git source → tarball.")
            self._patch_conanfile_git_to_tarball(recipe_dir)
            return

        # --- regular file-based sources ---
        print("  Mirroring sources ...")
        for url, sha256 in walk_sources(sources[version]):
            if url in self.url_map:
                continue

            filename = source_filename(url)
            art_target = f"{self.a.artifactory_url}/{self.a.sources_repo}/{filename}"

            # --- download ---
            cached = self.cache_dir / filename
            if cached.exists():
                actual = _sha256_file(cached)
                if sha256 and actual != sha256:
                    print(f"    {filename}: cached checksum mismatch, re-downloading.")
                    cached.unlink()
                else:
                    print(f"    {filename}: cache hit.")

            if not cached.exists():
                print(f"    Downloading {filename} ...")
                req = urllib.request.Request(
                    url, headers={"User-Agent": "conan2-provision-script"}
                )
                try:
                    with urllib.request.urlopen(req) as resp, open(cached, "wb") as fh:
                        shutil.copyfileobj(resp, fh)
                except urllib.error.HTTPError as e:
                    sys.exit(f"ERROR: Download failed for {url} — HTTP {e.code}")

                if sha256:
                    actual = _sha256_file(cached)
                    if actual != sha256:
                        cached.unlink()
                        sys.exit(
                            f"ERROR: SHA-256 mismatch for {filename}.\n"
                            f"  expected: {sha256}\n"
                            f"  actual  : {actual}"
                        )
                print(f"    {filename}: downloaded OK.")

            # --- upload ---
            if art_exists(art_target, self.a.artifactory_user, self.a.artifactory_pass):
                print(f"    {filename}: already in Artifactory.")
            else:
                print(f"    Uploading {filename} ...")
                art_upload(cached, art_target, self.a.artifactory_user, self.a.artifactory_pass)
                print(f"    {filename}: uploaded.")

            self.url_map[url] = art_target

        # --- patch conandata.yml ---
        text = conandata_path.read_text()
        if self.url_map:
            url_re = re.compile('|'.join(re.escape(u) for u in self.url_map))
            patched = url_re.sub(lambda m: self.url_map[m.group(0)], text)
        else:
            patched = text
        if patched != text:
            conandata_path.write_text(patched)
            print("  Patched conandata.yml with Artifactory URLs.")

    # -- build & upload ------------------------------------------------------

    def build_and_upload(self, name, version, recipe_dir, options=None):
        ref = f"{name}/{version}"

        print(f"  Exporting {ref} ...")
        subprocess.run(
            ["conan", "export", str(recipe_dir), "--version", version],
            check=True,
        )

        # Build the conan create command, appending any user-specified options.
        # Options not listed here use the recipe's own default_options — we do
        # not override them so the recipe author's defaults are always honoured.
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
            print(f"  Options: { {k: v for k, v in options.items()} }")

        print(f"  Building {ref} ...")
        result = subprocess.run(cmd)
        # Conan exit code 6 = ConanInvalidConfiguration (e.g. Windows-only package
        # built on Linux).  Treat as a warning rather than a hard failure so the
        # rest of the provisioning run continues.
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

    # -- orchestration -------------------------------------------------------

    def provision(self, name, version, folder, options=None):
        print(f"\n=== {name}/{version} ===")

        if not self.a.no_fetch:
            recipe_dir, folder = self.fetch_recipe(name, version, folder)
        else:
            base = self.recipes_dir / name
            if folder:
                recipe_dir = base / folder
            else:
                subdirs = [d for d in base.iterdir() if d.is_dir()] if base.exists() else []
                if not subdirs:
                    sys.exit(f"ERROR: No recipe directory found at {base}")
                recipe_dir = subdirs[0]
                folder = subdirs[0].name

        if not self.a.no_mirror:
            self.mirror_sources(name, version, recipe_dir)

        if not self.a.no_build:
            self.build_and_upload(name, version, recipe_dir, options)

    def run(self, cli_specs):
        # Build the canonical list of (name, version, folder, options) tuples
        if self.a.packages_file:
            specs = load_packages_file(self.a.packages_file)
            if cli_specs:
                print("WARNING: --packages-file was given; ignoring positional PKG_SPEC args.")
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

        # --packages-file implies --auto-deps unless --no-auto-deps is given
        use_auto_deps = (self.a.auto_deps or bool(self.a.packages_file)) and not self.a.no_auto_deps
        if use_auto_deps:
            print("=== Resolving dependency tree ===")
            pkg_list = self.collect_packages(specs)
            print(f"\n=== Build order ({len(pkg_list)} packages) ===")
            for name, version, folder, options in pkg_list:
                suffix = f"  [{folder}]" if folder else ""
                opt_hint = f"  {options}" if options else ""
                print(f"  {name}/{version}{suffix}{opt_hint}")
        else:
            pkg_list = specs

        for name, version, folder, options in pkg_list:
            self.provision(name, version, folder, options)

        print("\n=== All packages provisioned ===")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    repo_root = Path(__file__).resolve().parent.parent

    p = argparse.ArgumentParser(
        description="Provision Conan packages from conan-center-index into Artifactory",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("packages", nargs="*", metavar="PKG_SPEC",
                   help="name/version or name/version:folder  "
                        "(omit when using --packages-file)")

    p.add_argument("--packages-file", metavar="FILE",
                   help="YAML file listing packages and options (see below). "
                        "Takes precedence over positional PKG_SPEC args.")

    g = p.add_argument_group("Dependency resolution")
    g.add_argument("--auto-deps", action="store_true",
                   help="Automatically resolve and provision transitive dependencies. "
                        "Enabled by default when --packages-file is used. "
                        "Specify only the packages you want; deps are discovered by "
                        "analysing each recipe's conanfile.py and resolved to the "
                        "latest satisfying version from conan-center-index.")
    g.add_argument("--no-auto-deps", action="store_true",
                   help="Disable automatic dependency resolution even when --packages-file is used.")
    g.add_argument("--pin", metavar="name=version", action="append",
                   help="Pin a dependency to a specific version when using --auto-deps "
                        "(e.g. --pin cmake=3.31.12). May be repeated.")

    g = p.add_argument_group("Conan")
    g.add_argument("--profile", metavar="NAME_OR_PATH",
                   default=os.environ.get("CONAN_PROFILE", "linux-x86_64-gcc-cxx17"),
                   help="Profile name (looked up under profiles/) or absolute path")
    g.add_argument("--remote-name", metavar="NAME",
                   default=os.environ.get("CONAN_REMOTE_NAME", "artifactory"))
    g.add_argument("--cppstd", default="17", metavar="STD",
                   help="C++ standard passed to conan create (default: 17)")

    g = p.add_argument_group("Artifactory")
    g.add_argument("--artifactory-url", metavar="URL",
                   default=os.environ.get("ARTIFACTORY_URL", "http://localhost:8082/artifactory"))
    g.add_argument("--artifactory-user", metavar="USER",
                   default=os.environ.get("ARTIFACTORY_USER", "admin"))
    g.add_argument("--artifactory-pass", metavar="PASS",
                   default=os.environ.get("ARTIFACTORY_PASSWORD", "password"))
    g.add_argument("--sources-repo", default="conan-sources", metavar="REPO",
                   help="Artifactory generic repo for source tarballs (default: conan-sources)")

    g = p.add_argument_group("Paths")
    g.add_argument("--recipes-dir", default=str(repo_root / "recipes"),
                   help="Local directory for fetched recipes (default: ./recipes)")
    g.add_argument("--cache-dir", default=str(repo_root / ".source-cache"),
                   help="Download cache directory (default: ./.source-cache)")
    g.add_argument("--cci-path", metavar="PATH",
                   default=os.environ.get("CCI_PATH", ""),
                   help="Path to a local clone of conan-center-index. "
                        "When set, all recipe lookups use the local clone instead of "
                        "the GitHub API — no rate limits, works offline after the "
                        "initial clone.  If PATH does not exist it is cloned "
                        "automatically from GitHub.  "
                        "Example: --cci-path ~/conan-center-index")

    g = p.add_argument_group("GitHub")
    g.add_argument("--github-token", default=os.environ.get("GITHUB_TOKEN", ""),
                   metavar="TOKEN",
                   help="GitHub personal access token (avoids 60 req/h rate limit; "
                        "also used when auto-cloning conan-center-index)")
    g.add_argument("--refetch", action="store_true",
                   help="Re-copy/re-download recipe files even if already present locally")

    g = p.add_argument_group("Skip steps")
    g.add_argument("--no-fetch", action="store_true",
                   help="Skip recipe fetch/copy (use existing local recipes as-is)")
    g.add_argument("--no-mirror", action="store_true",
                   help="Skip source download and Artifactory upload")
    g.add_argument("--no-build", action="store_true",
                   help="Skip conan create and conan upload")

    args = p.parse_args()
    if not args.packages and not args.packages_file:
        p.error("Provide at least one PKG_SPEC or --packages-file.")
    Provisioner(args).run(args.packages)


if __name__ == "__main__":
    main()
