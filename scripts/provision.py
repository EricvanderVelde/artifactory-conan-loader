#!/usr/bin/env python3
"""
Shared utility library for fetch.py and deploy.py.

Not intended to be run directly — import from fetch.py or deploy.py.
"""

import base64
import hashlib
import json
import platform
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("ERROR: PyYAML is required.  Run: pip install pyyaml")


GITHUB_RAW = "https://raw.githubusercontent.com/conan-io/conan-center-index/master"
GITHUB_API = "https://api.github.com/repos/conan-io/conan-center-index/contents"
CCI_DEFAULT_URL = "https://github.com/conan-io/conan-center-index"


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
    parts = []
    for seg in re.split(r"[.\-+]", v):
        try:
            parts.append(int(seg))
        except ValueError:
            parts.append(seg)
    return parts


def _ver_cmp(a, b):
    at, bt = _ver_tuple(a), _ver_tuple(b)
    length = max(len(at), len(bt))
    at = (at + [0] * length)[:length]
    bt = (bt + [0] * length)[:length]
    for x, y in zip(at, bt):
        if type(x) != type(y):
            return -1 if isinstance(x, int) else 1
        if x < y: return -1
        if x > y: return 1
    return 0


def version_in_range(version, range_spec):
    """Return True if version satisfies a Conan version range or exact match."""
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

_HOST_CONAN_OS = {"Linux": "Linux", "Windows": "Windows", "Darwin": "Macos"}.get(
    platform.system(), "Linux"
)
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
    """Return dict of option_name -> bool/value from default_options."""
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
    """Gather multi-line conditions by looking forward until parens balance."""
    open_p = cond.count("(") - cond.count(")")
    j = if_lineno + 1
    while open_p > 0 and j < len(lines) and j < if_lineno + 15:
        cont = lines[j].strip()
        open_p += cont.count("(") - cont.count(")")
        cond += " " + cont.rstrip("):, \t")
        j += 1
    return cond


_PROPERTY_REF_RE = re.compile(r'self\.(_\w+)\b(?!\s*\()')

# Either form an option can be referenced in: self.options.foo or
# self.options.get_safe("foo"[, default]). Group 1 is the get_safe name,
# group 2 the plain-attribute name — exactly one of the two is set.
_OPTION_REF = r'self\.options\.(?:get_safe\(["\'](\w+)["\'][^)]*\)|(?!get_safe\b)(\w+)\b)'
_OPT_EQ_RE = re.compile(_OPTION_REF + r'\s*==\s*["\']([^"\']+)["\']')
_OPT_VALUE_RE = re.compile(r'(not\s+)?\b' + _OPTION_REF + r'(?!\s*[!=<>])')


def _find_property_expr(lines, name, cache):
    """Return the (possibly multi-line) return-expression of a @property, or None.

    Cached per name since sibling if/elif conditions in the same conanfile
    (e.g. boost's dozen `self._with_*` gates) repeatedly reference the same
    handful of properties.
    """
    if name in cache:
        return cache[name]

    expr = None
    def_re = re.compile(r'^\s*def\s+' + re.escape(name) + r'\s*\(self\)\s*:\s*$')
    for i, line in enumerate(lines):
        if not def_re.match(line):
            continue
        j = i - 1
        while j >= 0 and not lines[j].strip():
            j -= 1
        if j < 0 or not lines[j].strip().startswith('@property'):
            continue
        k = i + 1
        while k < len(lines) and not lines[k].strip():
            k += 1
        if k < len(lines) and lines[k].strip().startswith('return '):
            expr = _gather_cond(lines, k, lines[k].strip()[len('return '):])
        break  # body has logic beyond a single return — too complex to inline

    cache[name] = expr
    return expr


def _expand_property_refs(cond, lines, cache, max_rounds=5):
    """Inline simple `self._foo` @property references into cond, to a fixed point.

    Boost (and similarly-shaped recipes) gate requires() on helper properties
    like `self._with_bzip2` rather than `self.options.bzip2` directly, which
    _cond_skip can't see through on its own.
    """
    for _ in range(max_rounds):
        changed = False

        def repl(m):
            nonlocal changed
            expr = _find_property_expr(lines, m.group(1), cache)
            if expr is None:
                return m.group(0)
            changed = True
            return f"({expr})"

        cond = _PROPERTY_REF_RE.sub(repl, cond)
        if not changed:
            break
    return cond


def _cond_skip(cond, options):
    """Return True if an if/elif condition means the block should be skipped."""
    if _OS_EQ_RE.search(cond) or _OS_IN_RE.search(cond):
        return True

    if not options:
        return False

    om = _OPT_EQ_RE.search(cond)
    if om:
        opt, expected = om.group(1) or om.group(2), om.group(3)
        if opt in options and options[opt] != expected:
            return True

    def _negate_if_bool(negated, value):
        return not value if negated and isinstance(value, bool) else value

    opt_values = [
        _negate_if_bool(bool(om.group(1)), options.get(om.group(2) or om.group(3)))
        for om in _OPT_VALUE_RE.finditer(cond)
    ]

    if not opt_values:
        return False

    has_or = bool(re.search(r'\bor\b', cond))
    if has_or:
        return all(v is False for v in opt_values) and all(v is not None for v in opt_values)
    return any(v is False for v in opt_values)


def _resolve_cond_skip(cond, lines, options, cache):
    """_cond_skip, falling back to property expansion only if the raw condition is inconclusive.

    Some helper properties (e.g. `self._settings_build`) already contain an
    `os ==` substring that the raw regex matches directly — expanding those
    can strip the substring and produce a false negative. Only expand when
    the unexpanded condition didn't already resolve, so we add coverage for
    option-gating properties (e.g. boost's `self._with_bzip2`) without
    disturbing conditions that already match as-is.
    """
    if _cond_skip(cond, options):
        return True
    expanded = _expand_property_refs(cond, lines, cache)
    return expanded != cond and _cond_skip(expanded, options)


def extract_raw_requires(conanfile_text, options=None):
    """Return list of (ref, is_tool_require) from conanfile.py, filtered by conditions."""
    lines = conanfile_text.splitlines()
    results = []
    prop_cache: dict = {}  # @property name -> return-expression, shared across all conditions below

    for m in re.finditer(
        r"self\.(tool_requires|requires)\s*\(\s*[fF]?[\"']([^\"']+)[\"']",
        conanfile_text,
    ):
        is_tool = m.group(1) == "tool_requires"
        ref = m.group(2)
        if "/" not in ref:
            continue

        req_lineno = conanfile_text[:m.start()].count("\n")
        req_line = lines[req_lineno] if req_lineno < len(lines) else ""
        req_indent = len(req_line) - len(req_line.lstrip())

        skip = False
        for i in range(req_lineno - 1, max(-1, req_lineno - 15), -1):
            if i < 0:
                break
            line = lines[i]
            stripped = line.lstrip()
            if not stripped:
                continue
            indent = len(line) - len(stripped)
            if indent >= req_indent:
                continue
            is_if = stripped.startswith("if ")
            is_elif = stripped.startswith("elif ")
            if not is_if and not is_elif:
                continue
            cond = stripped[3 if is_if else 5:].rstrip(": \t")
            cond = _gather_cond(lines, i, cond)
            if _resolve_cond_skip(cond, lines, options, prop_cache):
                skip = True
                break
            req_indent = indent

        if not skip:
            results.append((ref, is_tool))

    return results


def parse_dep_ref(ref):
    """Split 'name/[>=1.0 <2]' into (name, version_range)."""
    slash = ref.index("/")
    return ref[:slash], ref[slash + 1:]


# ---------------------------------------------------------------------------
# Source URL helpers
# ---------------------------------------------------------------------------

def source_filename(url):
    """Derive a descriptive local filename for an upstream source URL."""
    m = re.search(r"github\.com/[^/]+/([^/]+)/archive/(.+)", url)
    if m:
        repo = m.group(1)
        basename = m.group(2).split("/")[-1]
        return f"{repo}-{basename}"
    return url.rstrip("/").split("/")[-1]


def walk_sources(node):
    """Yield (url, sha256) from file-based conandata source entries (skip git sources).

    Some recipes (e.g. cmake's `binary` variant) key their sources by Conan OS
    name to ship a separate binary per platform. When a dict's keys are
    exactly the set of Conan OS names, only the host OS's branch is walked —
    mirrors the settings.os filtering already applied to conditional
    requires() in _cond_skip.
    """
    if isinstance(node, dict):
        if "commit" in node:
            return
        if "url" in node:
            url = node["url"]
            sha256 = node.get("sha256", "")
            if isinstance(url, list):
                for u in url:
                    yield u, sha256
            else:
                yield url, sha256
        elif node and set(node) <= _ALL_CONAN_OS:
            yield from walk_sources(node.get(_HOST_CONAN_OS, {}))
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
    """Return set of version strings from conandata.yml text, or None on failure."""
    try:
        data = yaml.safe_load(text) or {}
        return {str(k) for k in data.get("sources", {}).keys()}
    except Exception:
        return None


def _sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _file_checksums(path, algorithms=("sha256", "sha1", "md5")):
    """Compute multiple checksums of a file in a single read pass."""
    hashers = {name: hashlib.new(name) for name in algorithms}
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            for h in hashers.values():
                h.update(chunk)
    return {name: h.hexdigest() for name, h in hashers.items()}


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


def art_upload(local_path, target_url, user, password, sha256=None, sha1=None, md5=None):
    """Upload local_path to target_url.

    Any of sha256/sha1/md5 given are sent as X-Checksum-* deploy headers.
    Artifactory verifies the uploaded content against them server-side and
    rejects the upload (409) on a mismatch.
    """
    checksum_headers = []
    for name, value in (("Sha256", sha256), ("Sha1", sha1), ("Md5", md5)):
        if value:
            checksum_headers += ["-H", f"X-Checksum-{name}: {value}"]
    subprocess.run(
        ["curl", "--silent", "--show-error", "--fail",
         "-u", f"{user}:{password}", *checksum_headers, "-T", str(local_path), target_url],
        check=True,
    )


# ---------------------------------------------------------------------------
# Conan CLI helpers
# ---------------------------------------------------------------------------

def conan_json(cmd):
    """Run a conan subcommand and parse its --format=json output, or None on any failure.

    A non-zero exit or unparseable output means the check itself is broken
    (bad conan install, malformed args, remote unreachable at the transport
    level) rather than a normal "not found", so it's worth a warning — unlike
    a valid JSON response reporting "not found", which conan itself uses as
    its routine way of saying a recipe/package doesn't exist yet.
    """
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  WARNING: `{' '.join(cmd)}` failed (exit {result.returncode}): "
              f"{result.stderr.strip()[:200]}")
        return None
    try:
        return json.loads(result.stdout)
    except ValueError:
        print(f"  WARNING: `{' '.join(cmd)}` produced unparseable output — treating as unknown.")
        return None


def conan_option_args(name, options):
    """Build -o name/*:opt=value args for `conan create`/`conan graph info`."""
    args = []
    for opt_name, opt_val in (options or {}).items():
        conan_val = "True" if opt_val is True else "False" if opt_val is False else str(opt_val)
        args += ["-o", f"{name}/*:{opt_name}={conan_val}"]
    return args


def conan_package_id(recipe_dir, name, version, profile_path, cppstd, options):
    """Return the package_id conan would build for this recipe+profile+options.

    None if it can't be determined (e.g. a dependency isn't resolvable yet) —
    callers should fall through to building in that case.
    """
    cmd = [
        "conan", "graph", "info", str(recipe_dir),
        "--version", version,
        f"--profile:build={profile_path}", f"--profile:host={profile_path}",
        "-s", f"compiler.cppstd={cppstd}",
        *conan_option_args(name, options),
        "--format=json",
    ]
    data = conan_json(cmd)
    if data is None:
        return None
    graph = data.get("graph", {})
    root_id = next(iter(graph.get("root", {})), None)
    return graph.get("nodes", {}).get(root_id, {}).get("package_id")


def conan_package_exists(name, version, package_id, remote_name):
    """Return True if this exact package_id is already uploaded to the given remote."""
    cmd = ["conan", "list", f"{name}/{version}:{package_id}",
           "-r", remote_name, "--format=json"]
    data = conan_json(cmd)
    if data is None:
        return False
    info = next(iter(data.values()), {})
    if not isinstance(info, dict) or "error" in info:
        return False  # not found, or the query itself failed — either way, don't skip the build
    for ref_data in info.values():
        for rrev_data in ref_data.get("revisions", {}).values():
            if package_id in rrev_data.get("packages", {}):
                return True
    return False


# ---------------------------------------------------------------------------
# Packages YAML
# ---------------------------------------------------------------------------

def load_packages_file(path):
    """Parse packages.yml and return list of (name, version, folder, options)."""
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
        options = {str(k): v for k, v in raw_opts.items()}
        specs.append((name, version, folder, options))
    return specs
