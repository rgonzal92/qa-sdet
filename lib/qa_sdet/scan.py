"""Source classifier, `codemap-scan` and the shell menu."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import codemap, core, scrub

TYPE_CHECKING = False  # mypy reads it as True (pyproject always_true): typing stays out of the import path
if TYPE_CHECKING:
    from typing import Any


SCAN_SKIP_DIRS = {
    "node_modules",
    ".git",
    "dist",
    "build",
    "out",
    ".next",
    ".nuxt",
    ".svelte-kit",
    ".angular",
    "coverage",
    "vendor",
    "__pycache__",
    ".venv",
    "venv",
    "target",
    "bin",
    "obj",
    ".cache",
    ".turbo",
    "storybook-static",
    "qa-out",
    ".playwright-cli",
    ".playwright-mcp",
    ".idea",
    ".vscode",
}
FRONTEND_SUFFIXES = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue", ".svelte", ".html"}
TEMPLATE_SUFFIXES = {
    ".cshtml",
    ".razor",
    ".erb",
    ".jsp",
    ".twig",
    ".hbs",
    ".njk",
}  # server-rendered UI (.blade.php: name)
BACKEND_SUFFIXES = {".py", ".java", ".kt", ".cs", ".go", ".rb", ".php"}
SPEC_SUFFIXES = {".yaml", ".yml", ".json"}
# Never read: secret stores and keys (source files are judged on content; literal secrets are scrubbed at ingest).
SECRET_FILE_RE = re.compile(
    r"(?i)^\.env|\.(pem|key|p12|pfx|jks|keystore|crt|cer|kdbx)$|^secrets?\.(json|ya?ml|env|"
    r"properties|ini|toml|txt|xml)$|^credentials?\.(json|xml|ya?ml|ini|toml|txt)$|"
    r"^id_(rsa|ed25519|ecdsa|dsa)|^\.(npmrc|netrc|pgpass|pypirc|dockercfg)$|^appsettings\.\w+\.json$|"
    r"^secrets?\.(py|rb|php|go|java|kt|cs)$|^credentials?\.(py|rb|php|go)$|^wp-config\.php$"
)
GENERATED_FILE_RE = re.compile(
    r"(?i)\.min\.(js|css)$|\.map$|^(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|"
    r"composer\.lock|Gemfile\.lock|poetry\.lock|Cargo\.lock)$|\.(bundle|chunk)\.js$|\.d\.ts$|"
    r"\.generated\.|\.g\.(ts|cs)$"
)
TEST_WORDS = {"test", "tests", "spec", "specs", "cypress", "playwright", "selenium", "pageobject", "pageobjects", "pom"}
TEST_NAME_RE = re.compile(r"(?i)\.(spec|test|cy|e2e|po)\.\w+$")
UNIT_TEST_RE = re.compile(
    r"(?i)\.(spec|test)\.\w+$|(^|/)test_\w+\.py$|\b(describe|it|test)\(\s*['\"`]|import\s+pytest|"
    r"@Test\b|\[Fact\]|\[Test\]"
)
TESTS_RE = re.compile(
    r"get_by_(test_id|role|label|text|placeholder|alt_text|title)\(|GetBy(TestId|Role|Label|Text|Placeholder|"
    r"AltText|Title)\(|\.Locator\(|By\.(ID|CSS_SELECTOR|XPATH|NAME|CLASS_NAME|LINK_TEXT)\b|"
    r"By\.(Id|CssSelector|XPath|Name|ClassName|LinkText)\(|\[FindsBy|"
    r"getBy(TestId|Role|Label|Text|Placeholder|AltText|Title)\(|page\.(locator|fill|click|check|type|"
    r"selectOption)\(|\.locator\(|cy\.(get|findBy\w+|contains)\(|By\.(id|css|cssSelector|xpath|name|"
    r"className|linkText|tagName)\(|@FindBy\(|\$\$?\(['\"`]|data-testid|data-test|data-cy"
)
TESTS_IMPORT_RE = re.compile(
    r"""from\s+['"]@playwright/test['"]|require\(\s*['"]@playwright/test|from\s+['"]cypress|"""
    r"""\bcy\.(visit|get)\(|^from\s+selenium|^import\s+org\.openqa\.selenium|webdriverio|"""
    r"""from\s+['"]@testing-library|\bbrowser\.\$\(|^from\s+playwright\.(sync_api|async_api)|"""
    r"""^using\s+(Microsoft\.Playwright|OpenQA\.Selenium)|^import\s+com\.microsoft\.playwright|"""
    r"""^(from|import)\s+behave\b|pytest_bdd""",
    re.M,
)
PO_CLASS_RE = re.compile(r"\bclass\s+\w*(Page|Screen|Po|PO)(Object)?\b")
PO_CALL_RE = re.compile(
    r"\bpage\.getBy\w+\(|\.locator\(|\bGetBy(Role|TestId|Label|Text|Placeholder|AltText|Title)\(|"
    r"\.Locator\(|FindElement\(By\.|@FindBy\(|\bget_by_(role|test_id|label|text|placeholder|alt_text|"
    r"title)\(|\bcy\.get\("
)  # not repository calls (GetByIdAsync, get_by_email)
BACKEND_RE = re.compile(
    r"@(Get|Post|Put|Patch|Delete|RequestMapping|GetMapping|PostMapping|PutMapping|DeleteMapping|PatchMapping|Controller|"
    r"RestController|Path|GET|POST|PUT|DELETE|PATCH)\b|"  # Spring, Nest, JAX-RS
    r"\b(router|app|server|fastify|v1|api_router|blueprint)\.(get|post|put|patch|delete|route)\(\s*['\"`]/|"  # express-ish
    r"express\.Router\(|from\s+['\"](express|fastify|koa|@nestjs/\w+|hapi)['\"]|require\(\s*['\"](express|fastify|koa)['\"]|"
    r"\[Http(Get|Post|Put|Patch|Delete)|\[Route\(|\.Map(Get|Post|Put|Patch|Delete)\(|"  # ASP.NET
    r"\[(Required|StringLength|MaxLength|MinLength|Range|RegularExpression|EmailAddress)\b|AbstractValidator<|RuleFor\(|"
    r"@\w+\.(get|post|put|patch|delete|route)\(|\burlpatterns\b|viewsets\.|APIView\b|generics\.\w+View|"  # Flask, FastAPI, Django
    r"\.(GET|POST|PUT|PATCH|DELETE|Handle|HandleFunc|Get|Post|Put|Patch|Delete|Route|Group|Mount)\(\s*\"|"  # Go routers
    r"\b(validate|binding):\"|@(field:|get:|param:)\w+|\[property:\s*\w+|@api_view\(|\bserializers\.\w+(Field|Serializer)\b|"
    r"\bforms\.\w+Field\(|from\s+['\"]hono['\"]|"
    r"@(Min|Max|Size|NotNull|NotBlank|NotEmpty|Length|Pattern|Range|Positive|Email|Valid)\b|"
    r"@(Is\w+|MaxLength|MinLength|Matches)\(|\bField\([^)]*\b(ge|le|max_length)=|"
    r"Rails\.application\.routes|\bresources\s+:|\bvalidates\s+:|params\.require\(|"  # Rails
    r"Route::(get|post|put|patch|delete|resource|apiResource)\(|extends\s+FormRequest|->validate\(\[|"  # Laravel
    r"\bmodels\.\w+Field\(|forms\.ModelForm|\bmarshmallow\b|\brouting\s*\{|"  # Django, Ktor
    r"\b(body|query|param|check)\(\s*['\"][\w.]+['\"]\s*\)\s*\.(is\w+|not\w*|matches|exists)"
)
FRONTEND_RE = re.compile(
    r"\bpath\s*:\s*['\"`]|<Route\b|createBrowserRouter|createHashRouter|RouterModule|\bRoutes\b|"
    r"createRouter\(|vue-router|data-testid|data-test=|data-cy|aria-label|formControlName|"
    r"Validators\.|\bv-model|\bng-model|\byup\.|\bz\.(object|string|number)|useForm\(|register\(|"
    r"\b(required|maxLength|minLength|pattern)\b|\b(min|max)=|\bfetch\(|\baxios\b|HttpClient|\$http|"
    r"\buseQuery\(|\bapiClient\b|['\"`]/api/|<template\b|@Component\(|export\s+default\s+defineComponent|"
    r"\b\w{0,40}(api|http|client|request)\w{0,40}\.(get|post|put|patch|delete)\s*(<[^()]{0,200}?>)?\s*\("
)  # api.post<Z>('/zones')
PAGE_DIR_RE = re.compile(
    r"(^|/)(app|pages|routes|views|screens)/"
)  # file-system routing (Next, Nuxt, SvelteKit, Remix)
UI_SIGNAL_RE = re.compile(
    r"export\s+((async\s+)?function|const|let)\s+(load|loader|action|generateMetadata|"
    r"getServerSideProps|getStaticProps)\b|['\"]use client['\"]|@Component\(|defineComponent|<template\b"
)
UI_TAG_RE = re.compile(r"<[A-Za-z][\w.-]*[\s/>]")  # a tag; in .ts it would be a generic (Promise<Zone>)
NOISE_FILE_RE = re.compile(
    r"(?i)\.(stories|story|fixtures?|mocks?)\.\w+$|(^|/)(__mocks__|mocks|__fixtures__|test-utils|"
    r"testing)/|(^|/)setupTests\.\w+$"
)
BACKEND_WEAK_RE = re.compile(r"\b(openapi|swagger)\b|\bJoi\.|\bcelebrate\(|\bpydantic\b")  # backend languages only
SERVER_SIGNAL_RE = re.compile(
    r"\bmongoose\.(Schema|model)\b|new\s+Schema\(|sequelize\.define|\bDataTypes\.|@(Column|Entity|"
    r"PrimaryGeneratedColumn)\b|@prisma/client|\bknex\.schema|\(\s*req\s*,\s*res\b|\breq\.(body|params|query)\b|"
    r"module\.exports\s*=\s*\{?\s*\w*(Controller|Service|Model|Repository)"
)
SCHEMA_RE = re.compile(
    r"from\s+['\"](zod|yup|joi|class-validator|valibot|superstruct)['\"]|\bz\.object\(|\byup\.object\(|"
    r"\bJoi\.object\(|\bmarshmallow\b|validate:\""
)
PAGE_SIGNAL_RE = re.compile(
    r"\.goto\(|GotoAsync\(|GoToUrl\(|cy\.visit\(|\bnavigate(To)?\(|driver\.get\(|browser\.url\(|"
    r"\bclass\s+\w*(Page|Screen|Po|PO|Component|Dialog|Modal|Form|Panel|Section|Widget)(Object)?\b|"
    r"(?i:(^|/)(pages|pom|po|steps?|page-?objects?|e2e|cypress|playwright)/)"
)
SPEC_VERSION_RE = re.compile(
    r"(?m)^\s*['\"]?(openapi|swagger)['\"]?\s*:\s*['\"]?\d|[{,]\s*\"(openapi|swagger)\"\s*:\s*\"\d"
)
CONFIG_FILE_RE = re.compile(
    r"(?i)(^|/)(application|bootstrap)[\w.-]*\.(ya?ml|properties)$|(^|/)docker-compose[\w.-]*\.ya?ml$|"
    r"(^|/)values[\w.-]*\.ya?ml$|"
    r"\.tfvars$|(^|/)config\.(json|ya?ml)$|(^|/)(next|nuxt|vite|vue|svelte|astro|remix|webpack)\.config\.\w+$|"
    r"(^|/)environments?/environment[\w.-]*\.ts$|(^|/)(config|configuration|settings|secrets?|"
    r"credentials?)\.(js|ts|mjs|cjs)$|(^|/)config/(default|production|development|staging|local|test|"
    r"custom-environment-variables)[\w.-]*\.(js|ts|mjs|cjs)$"
)
SECRET_CONTENT_RE = re.compile(
    r"(?m)^\s*(SECRET_KEY|INSTALLED_APPS|DATABASES)\s*=|class\s+\w+\(\s*BaseSettings\s*\)|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|\b[a-z][\w+.-]*://[^\s:/?#'\"@]+:[^\s@'\"/?#]+@"
)
# Django settings, pydantic settings, keys, connection URIs with a password (mongodb://user:pass@host)
CONFIG_SECRET_RE = re.compile(
    r"(?i)\b(secret\w*|pass(word|wd|phrase)?|pwd|private_?key\w*|api_?key\w*|\w*token)['\"]?\s*[:=]\s*"
    r"['\"][^'\"\s]{6,}['\"]"
)  # in a config/ folder: a literal credential
SERVER_ROUTE_PATH_RE = re.compile(
    r"(^|/)(pages/api/|app/(.*/)?route\.(ts|js)$|[^/]*\+server\.(ts|js)$|server/(api|routes|"
    r"middleware)/)"
)
SERVER_ROUTE_RE = re.compile(
    r"export\s+(async\s+)?(function|const)\s+(GET|POST|PUT|PATCH|DELETE)\b|defineEventHandler\(|"
    r"NextApiRequest|\(\s*req\b[^,)]*,\s*res\b"
)
JSX_RE = re.compile(r"<[A-Z][\w.]*[\s/>]|return\s*\(?\s*<(div|form|input|button|table|section|main|ul|span|a)[\s>]")
STRONG_UI_RE = re.compile(
    r"<[A-Z][\w.]*[\s/>]|<(div|form|input|button|table|select|textarea)\b|<template\b|@Component\(|"
    r"['\"]use client['\"]"
)
STRONG_UI_TS_RE = re.compile(
    r"<template\b|@Component\(|['\"]use client['\"]"
)  # .ts can't hold JSX: <Zone> is a generic
ROUTER_ENTRY_RE = re.compile(r"\bpath\s*:\s*['\"`]|<Route\b")  # two or more in a file: it is a router
PKG_CONTAINERS = {"packages", "apps", "services", "libs", "modules", "projects"}
SCAN_MAX_BYTES = 400_000  # bigger files are generated or data, not hand-written source


def _rel_files(root):
    """Relative paths of regular files under root. In a git repo: tracked and untracked-but-not-ignored files, the
    same set incremental scans see (so ignored generated code is never mapped); otherwise a walk of the tree."""
    listed = core._git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", ".")
    rels = [rel.rstrip("/") for rel in (listed or "").split("\0") if rel and os.path.normpath(rel) != "."]
    if rels:  # (nothing listed: not a repo, or a folder a parent repo ignores: walk it)
        for rel in rels:
            if not (rel.isprintable() and rel.encode("utf-8", "ignore").decode() == rel):
                continue  # a name the DB can't hold: skipped (including submodules under such a dir)
            if os.path.isdir(os.path.join(root, rel)) and not os.path.islink(os.path.join(root, rel)):
                # (an empty, uninitialised submodule lists nothing of its own: git answers for the parent there)
                if not any(part in SCAN_SKIP_DIRS for part in rel.split("/")) and os.path.exists(
                    os.path.join(root, rel, ".git")
                ):
                    yield from (
                        f"{rel}/{sub}" for sub in _rel_files(os.path.join(root, rel))
                    )  # a submodule: its own files
            elif not any(part in SCAN_SKIP_DIRS for part in rel.split("/")[:-1]):
                yield rel
        return
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SCAN_SKIP_DIRS and not d.startswith(".")]
        for name in files:
            rel = os.path.relpath(os.path.join(dirpath, name), root).replace(os.sep, "/")
            if rel.isprintable() and rel.encode("utf-8", "ignore").decode() == rel:
                yield rel


def _decode_source(data):
    """Source text: UTF-16/32 when a BOM says so (Visual Studio can save .cs that way), else UTF-8 (BOM dropped); bytes
    that don't decode are dropped (a Latin-1 file still classifies by its ASCII)."""
    for bom, enc in (
        (b"\xff\xfe\0\0", "utf-32"),
        (b"\0\0\xfe\xff", "utf-32"),
        (b"\xff\xfe", "utf-16"),
        (b"\xfe\xff", "utf-16"),
    ):
        if data.startswith(bom):
            return data.decode(enc, "ignore")
    return data.decode("utf-8-sig", "ignore")


def _strong_backend(text):
    """A real server signal: a route, a server import, request validation; not just a word like 'swagger'."""
    return bool(BACKEND_RE.search(text) or SERVER_SIGNAL_RE.search(text))


def _tests_path(path):
    """A test folder or file name: a whole word of a path segment (e2e, ui-tests, UiTests, __tests__, page-objects,
    qa-automation), never letters inside a word (inspections, LatestIncidents, speedtest, signal-automation-ui)."""
    for seg in path.split("/"):
        words = {w.lower() for w in re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])", seg)}
        if (
            "e2e" in seg.lower()
            or words & TEST_WORDS
            or {"page", "objects"} <= words
            or {"page", "object"} <= words
            or ("automation" in words and (words & {"qa", "ui"} or seg.lower() == "automation"))
        ):
            return True
    return False


def _shell_menu(rel, text):
    """The app shell's menu config (a config.json with a nav of module urls and keys). Not credentials: no secret
    words. Not an API spec that merely has module and key properties (openapi-config.json)."""
    return (
        rel.endswith("config.json")
        and '"module"' in text
        and '"key"' in text
        and '"nav"' in text
        and not CONFIG_SECRET_RE.search(text)
        and not SPEC_VERSION_RE.search(text[:4000])
    )


def _shell_nav_routes(nav, parent, lines, rel):
    """(page, label, module id, url, line) for every menu item with a module, children included."""
    out = []
    for it in nav if isinstance(nav, list) else []:
        if not isinstance(it, dict):
            continue
        key = str(it.get("key") or "")
        path = f"{parent}/{key}" if parent else f"/{key}"
        mod = it.get("module")
        if isinstance(mod, dict) and isinstance(mod.get("url"), str):
            ln = next((i + 1 for i, line in enumerate(lines) if f'"url": "{mod["url"]}"' in line), 1)
            out.append((path, str(it.get("title") or key)[:120], str(mod.get("id") or "")[:60], mod["url"], ln))
        out += _shell_nav_routes(it.get("children"), path, lines, rel)
    return out


def _apply_shell_menu(con, repos):
    """The app shell's menu maps shell page paths to module page paths. Code writes its route rows, in memory and
    straight into the DB, not a mapper: a mapper can fail to write them, and a full re-map deletes its rows. Never raises."""
    for repo, _head, candidates, _routers in repos:
        for rel, (area, _size, h) in candidates.items():
            if area != "shell":
                continue
            try:
                text = _decode_source((Path(repo) / rel).read_bytes())
            except OSError:
                continue
            try:
                found = _shell_nav_routes(json.loads(text).get("nav"), "", text.splitlines(), rel)
            except (
                ValueError,
                AttributeError,
                RecursionError,
            ):  # broken, or too deep (json and the walk both recurse):
                found = []  # no routes, so its old ones go and its hash lands (else every scan re-maps in full)
            if (
                not found
                and not con.execute(
                    "SELECT 1 FROM codemap WHERE repo=? AND area='frontend' AND src=? LIMIT 1", (repo, rel)
                ).fetchone()
            ):
                continue  # (a menu that lost all its modules still ingests: its old rows go; the frontend keeps them)
            cm = scrub.redact_json(
                {
                    "area": "frontend",
                    "rows": [  # as the hook does for a mapper's
                        {
                            "repo": repo,
                            "page": page,
                            "kind": "route",
                            "label": label,
                            "value": f"module {mid} at {url}"[:300],
                            "file": f"{rel}:{ln}",
                        }
                        for page, label, mid, url, ln in found
                    ],
                }
            )
            scan = {
                "area": "frontend",
                "repos": [
                    # its hash: without it the next scan sees a changed multi-route file and re-maps frontend in full;
                    # leave commit empty so unmapped frontend files don't look up-to-date before a mapper runs
                    {"path": repo, "commit": "", "full": False, "files": [rel], "deleted": [], "hashes": {rel: h}}
                ],
            }
            try:
                codemap.apply_codemap(con, cm, scan, core.now())
            except (ValueError, sqlite3.Error) as e:
                print(f"qa-mem: shell menu not applied: {e}", file=sys.stderr)


def _ui_file(name):
    """A UI component file by type: .jsx/.tsx/.vue/.svelte, Angular .component.ts/.html."""
    return os.path.splitext(name)[1].lower() in (".jsx", ".tsx", ".vue", ".svelte") or bool(
        re.search(r"\.component\.(ts|html)$", name)
    )


def _strong_ui(rel, text):
    return bool((STRONG_UI_TS_RE if rel.endswith((".ts", ".mts", ".cts")) else STRONG_UI_RE).search(text))


def _pkg(rel):
    """The top folder a file belongs to ('' at the root; packages/<x> in a monorepo)."""
    parts = rel.split("/")[:-1]
    return "/".join(parts[:2] if parts and parts[0] in PKG_CONTAINERS else parts[:1])


def classify_source(rel, text, folder=""):
    """The code-map area a source file feeds, or None. folder = the scanned folder's own name (a folder given as
    `e2e/` still counts as tests). Tests are page objects and specs with locators; backend needs a server-side
    signal (and backend languages never go to the frontend); frontend components are taken by file type."""
    name = rel.rsplit("/", 1)[-1]
    if (
        SECRET_FILE_RE.search(name)
        or GENERATED_FILE_RE.search(name)
        or (CONFIG_FILE_RE.search(rel) and not _shell_menu(rel, text))
        or SECRET_CONTENT_RE.search(text[:20000])
    ):
        return None  # secret stores, generated code, config that holds credentials
    if _shell_menu(rel, text):
        return "frontend"  # the app shell's menu: maps shell page paths to module page paths
    suffix = os.path.splitext(name)[1].lower()
    if suffix in SPEC_SUFFIXES:  # only real API specs among data files (a version key), never config that holds secrets
        return "backend" if SPEC_VERSION_RE.search(text[:4000]) else None
    if suffix in TEMPLATE_SUFFIXES or name.endswith(".blade.php"):
        return "frontend"  # server-rendered pages: the UI lives in the template
    if suffix not in FRONTEND_SUFFIXES | BACKEND_SUFFIXES:
        return None
    if re.search(r"(^|/)config/", rel) and CONFIG_SECRET_RE.search(text):
        return None  # a config module holding credentials (config/index.js with a jwt secret)
    ui_file = _ui_file(name)
    # a component is a component wherever it sits (tests/, inspections/): only a test import or name makes it a test
    testish = (
        TESTS_IMPORT_RE.search(text) or TEST_NAME_RE.search(name) or (not ui_file and _tests_path(f"{folder}/{rel}"))
    )
    if testish and TESTS_RE.search(text) and PAGE_SIGNAL_RE.search(f"{folder}/{rel}\n{text}"):
        return "tests"  # e2e specs and page objects (they open pages); component unit tests don't
    if testish and (UNIT_TEST_RE.search(f"{folder}/{rel}") or UNIT_TEST_RE.search(text)):
        return None  # unit tests without locators teach nothing about pages
    if NOISE_FILE_RE.search(f"/{rel}"):
        return None  # stories, mocks, fixtures, test helpers: not what users see
    if SERVER_ROUTE_PATH_RE.search(f"/{rel}") and (SERVER_ROUTE_RE.search(text) or BACKEND_RE.search(text)):
        return (
            "backend"  # API routes inside UI frameworks (Next pages/api and route.ts, SvelteKit +server, Nuxt server/)
        )
    if ui_file:
        return "frontend"  # UI components, whatever words they contain
    if PO_CLASS_RE.search(text) and PO_CALL_RE.search(text) and not _strong_backend(text):
        return "tests"  # a page object outside a test folder (this.page.getBy…, FindElement(By.…))
    if _strong_backend(text) or (suffix in BACKEND_SUFFIXES and BACKEND_WEAK_RE.search(text)):
        return "backend"
    if suffix in BACKEND_SUFFIXES:
        return None  # server code with no routes or validation
    if (
        PAGE_DIR_RE.search(f"/{rel}")
        and suffix in (".ts", ".js")
        and (UI_SIGNAL_RE.search(text) or (suffix == ".js" and UI_TAG_RE.search(text)))
    ) or (suffix in (".js", ".mjs") and JSX_RE.search(text)):
        return "frontend"  # file-routed pages, and React components in .js files (no TS generics to confuse JSX)
    if FRONTEND_RE.search(text) or SCHEMA_RE.search(text):
        return "frontend"  # (a schema file beside server code is moved to backend by the scan)
    return None


CHUNK_FILES, CHUNK_TOKENS = 40, 60_000  # what one qa-mapper can read well; bigger areas are split across mappers


def codemap_scan(con, paths, out_dir, full=False, stamp=None, chunk_files=CHUNK_FILES, chunk_tokens=CHUNK_TOKENS):
    """List the source files each qa-mapper must read and write qa-out/init/scan-<area>[-<n>].json (one per mapper).
    Incremental by content: per area and repo, a file is listed when its content differs from what was last mapped
    (new, edited, uncommitted), which also lists files whose map never landed. Files with
    rows that are gone or no longer this area's are listed as deleted. When a router (a file defining several routes)
    changed or a route file is gone, frontend and tests are mapped in full (rows elsewhere sit on routes that may have
    moved). A page renamed inside its own one-route file isn't noticed: its old rows stay until `--full`. Areas with
    nothing to read are settled here. Returns a summary dict for the skill."""
    stamp = stamp or datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    own = re.compile(
        rf"(scan-({'|'.join(codemap.CODEMAP_AREAS)})(-\d+)?\.json|code-map-({'|'.join(codemap.CODEMAP_AREAS)})(-\d+)?\.json(\..+)?)"
    )
    for old in [*out_dir.glob("scan-*.json"), *out_dir.glob("code-map-*.json*")]:
        if own.fullmatch(old.name):  # --out may be a folder holding the user's own files
            old.unlink(
                missing_ok=True
            )  # a new scan: maps of older scans are stale (and the Write tool won't overwrite them)
    summary: dict[str, Any] = {"incremental": False, "total_files": 0, "tokens": 0, "areas": {}, "skipped": []}
    repos = []
    for raw in paths:
        repo = os.path.realpath(os.path.expanduser(str(raw)))
        if not os.path.isdir(repo):
            summary["skipped"].append(f"{raw}: not a folder")
            continue
        if os.path.dirname(repo) == repo or repo == os.path.realpath(os.path.expanduser("~")):
            # walking the whole disk or home folder takes minutes and maps unrelated code
            summary["skipped"].append(f"{raw}: too broad (the whole disk or home folder); give the app's repo folders")
            continue
        if any(r[0] == repo for r in repos):
            continue  # the same folder given twice (. and ./): listed once
        head = (core._git(repo, "rev-parse", "HEAD") or "").strip()
        head = head if core.COMMIT_RE.fullmatch(head) else ""
        scanned = {}  # rel -> [area, bytes, text]
        for rel in _rel_files(repo):
            full_path = os.path.join(repo, rel)
            try:
                if (
                    os.path.islink(full_path)
                    or not os.path.isfile(full_path)
                    or not os.path.realpath(full_path).startswith(repo.rstrip(os.sep) + os.sep)
                ):
                    continue  # links (also a linked parent folder) can point out of the repo or at /dev/zero; FIFOs hang
                size = os.path.getsize(full_path)
                if size > SCAN_MAX_BYTES:
                    continue
                with open(full_path, "rb") as fh:
                    raw_bytes = fh.read()
            except OSError:
                continue
            text = _decode_source(raw_bytes)
            area = classify_source(rel, text, os.path.basename(repo))
            if area == "frontend" and _shell_menu(rel, text):
                area = "shell"  # written by code (_write_shell_menu), not by a mapper
            if area:
                scanned[rel] = [area, size, text, hashlib.sha1(raw_bytes, usedforsecurity=False).hexdigest()]
        # a server folder (a real route or server import, not a stray word): its schema files are request rules
        # (in a repo with UI components too, only schemas in the server's own top folder: client/ form schemas stay)
        be_pkgs = {_pkg(rel) for rel, c in scanned.items() if c[0] == "backend" and _strong_backend(c[2])}
        if be_pkgs:
            ui_pkgs = {
                _pkg(rel) for rel, c in scanned.items() if c[0] == "frontend" and _ui_file(rel.rsplit("/", 1)[-1])
            }
            for rel, c in scanned.items():
                if (
                    c[0] == "frontend"
                    and SCHEMA_RE.search(c[2])
                    and not _strong_ui(rel, c[2])
                    and (not ui_pkgs or (_pkg(rel) in be_pkgs and _pkg(rel) not in ui_pkgs))
                ):
                    c[0] = "backend"
        # hashed by bytes: an edit to a non-UTF-8 character is still an edit
        routers = {rel for rel, c in scanned.items() if c[0] == "frontend" and len(ROUTER_ENTRY_RE.findall(c[2])) > 1}
        repos.append((repo, head, {rel: (c[0], c[1], c[3]) for rel, c in scanned.items()}, routers))
    routes_moved = False
    for (
        repo,
        _head,
        candidates,
        routers,
    ) in repos:  # did a router change, or a route file go, since frontend last mapped?
        stored = codemap._hashes(con, "frontend", repo)
        by_file = codemap._route_pages_by_file(con, repo)
        for src, pages in by_file.items():
            cur = candidates.get(src)
            if cur is None or cur[0] not in ("frontend", "shell") or (len(pages) > 1 and stored.get(src) != cur[2]):
                routes_moved = True
        # a new router file, or a one-route file that now holds several: its new pages render components whose
        # files didn't change, so only a full pass puts their locators there
        if stored and any(
            rel not in stored or (len(by_file.get(rel, ())) <= 1 and stored[rel] != candidates[rel][2])
            for rel in routers
        ):
            routes_moved = True
    areas = {a: [] for a in codemap.CODEMAP_AREAS}
    for repo, head, candidates, _ in repos:
        for a in codemap.CODEMAP_AREAS:
            mine = {rel: (size, h) for rel, (area, size, h) in candidates.items() if area == a}
            keep = set(mine) | (
                {rel for rel, (ar, _, _) in candidates.items() if ar == "shell"} if a == "frontend" else set()
            )
            stored = codemap._hashes(con, a, repo)
            had = {
                r[0] for r in con.execute("SELECT DISTINCT src FROM codemap WHERE repo=? AND area=?", (repo, a))
            } | set(stored)
            if full or not had or (routes_moved and a in ("frontend", "tests")):
                # with a shell menu, replaced file by file (files + deleted cover the rest): its code-written rows stay
                entry: dict[str, Any] = {
                    "path": repo,
                    "commit": head,
                    "full": keep == set(mine),
                    "files": sorted(mine),
                    "deleted": sorted(rel for rel in had if rel not in keep),
                }  # used when the area is chunked
            else:
                summary["incremental"] = True
                entry = {
                    "path": repo,
                    "commit": head,
                    "full": False,
                    "files": sorted(rel for rel, (_, h) in mine.items() if stored.get(rel) != h),
                    # renamed away, deleted, reverted out, or no longer this area's: their rows must go
                    "deleted": sorted(rel for rel in had if rel not in keep),
                }
            entry["hashes"] = {rel: mine[rel][1] for rel in entry["files"]}
            entry["sizes"] = {rel: mine[rel][0] for rel in entry["files"]}
            entry["bytes"] = sum(entry["sizes"].values())
            if entry["files"]:  # listed, not yet mapped: no stored hash, so a map that never lands lists them again
                with con:
                    codemap._set_hashes(con, a, repo, {k: v for k, v in stored.items() if k not in entry["files"]})
            else:  # nothing for a mapper: settle it now so cards don't say "behind" forever
                with con:
                    if entry["full"]:
                        con.execute("DELETE FROM codemap WHERE repo=? AND area=?", (repo, a))
                    for rel in entry["deleted"]:
                        con.execute("DELETE FROM codemap WHERE repo=? AND area=? AND src=?", (repo, a, rel))
                    if head:
                        core.set_meta(con, f"codemap_commit:{a}:{repo}", head)
                    codemap._set_hashes(
                        con,
                        a,
                        repo,
                        {} if entry["full"] else {k: v for k, v in stored.items() if k not in entry["deleted"]},
                    )
                entry["settled"] = True
            areas[a].append(entry)
    _apply_shell_menu(con, repos)
    with con:
        codemap._drop_code_only_pages(con)
    for a, entries in areas.items():
        chunks = _chunk(entries, chunk_files, chunk_tokens)
        info = []
        for n, chunk in enumerate(chunks, 1):
            suffix = "" if len(chunks) == 1 else f"-{n}"
            data = {
                "scan_id": f"{a}{suffix}-{stamp}",
                "area": a,
                "repos": [{k: v for k, v in r.items() if k != "sizes"} for r in chunk],
            }
            scan_file = out_dir / f"scan-{a}{suffix}.json"
            core._write(scan_file, json.dumps(data, indent=1))
            files = sum(len(r["files"]) for r in chunk)
            if files:
                info.append(
                    {
                        "scan_file": str(scan_file),
                        "scan_id": data["scan_id"],
                        "out": str(out_dir / f"code-map-{a}{suffix}.json"),
                        "files": files,
                    }
                )
        n = sum(len(r["files"]) for r in entries)
        tokens = sum(r["bytes"] for r in entries) // 4
        summary["areas"][a] = {
            "files": n,
            "deleted": sum(len(r["deleted"]) for r in entries),
            "tokens": tokens,
            "needs_mapper": n > 0,
            "mappers": info,
        }
        summary["total_files"] += n
        summary["tokens"] += tokens
    return summary


def _chunk(entries, chunk_files, chunk_tokens):
    """Split an area's repo entries into mapper-sized chunks. A chunked area is replaced file by file: each chunk
    re-maps its own files, and the deletions (files gone or no longer this area's) ride in the repo's first chunk,
    so chunks can be ingested in any order without wiping each other's rows."""
    if sum(len(e["files"]) for e in entries) <= chunk_files and sum(e["bytes"] for e in entries) // 4 <= chunk_tokens:
        return [entries]
    chunks, cur, n_files, n_bytes = [], [], 0, 0

    def part_for(e, first):
        part = next((x for x in cur if x["path"] == e["path"]), None)
        if part is None:
            part = {
                "path": e["path"],
                "commit": e["commit"],
                "full": False,
                "files": [],
                "hashes": {},
                "deleted": e["deleted"] if first else [],
                "bytes": 0,
            }
            cur.append(part)
        return part

    for e in entries:
        first = True
        if not e["files"]:
            part_for(e, True)
            continue
        for rel in e["files"]:
            if n_files and (n_files + 1 > chunk_files or (n_bytes + e["sizes"].get(rel, 0)) // 4 > chunk_tokens):
                chunks.append(cur)
                cur, n_files, n_bytes = [], 0, 0
            part = part_for(e, first)
            first = False
            part["files"].append(rel)
            part["hashes"][rel] = e["hashes"].get(rel, "")
            part["bytes"] += e["sizes"].get(rel, 0)
            n_files += 1
            n_bytes += e["sizes"].get(rel, 0)
    if cur:
        chunks.append(cur)
    return chunks
