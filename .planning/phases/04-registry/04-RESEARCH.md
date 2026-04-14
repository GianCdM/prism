# Phase 4: Registry - Research

**Researched:** 2026-04-14
**Domain:** Multi-registry management, Cloudflare Workers, GitHub API, HTTP caching, CLI wizard patterns
**Confidence:** HIGH

## Summary

Phase 4 delivers the full registry management layer: CLI commands for creating, configuring, and managing registries (`prism registry create/add/remove/list/default`), token management (`prism registry token create/revoke`), a bundled registry template (Cloudflare Worker + CI + scripts + schema), multi-registry reads with merge and caching, and multi-registry writes with per-registry delta tracking. The existing Lens Worker source is the template base and must be adapted to match the payload schema that Phase 3's `/publish-skills` already sends.

The most critical finding is a **payload schema mismatch** between the Lens Worker and the Prism client. The Lens Worker's `POST /publish` expects `{skills: [{name, skill_md, plugin_json}]}` (raw JSON strings), while the Prism `/publish-skills` sends to `POST /api/skills/publish` with `{skills: [{name, description, author, repository, category, source, commit_date, source_hash, content}]}` (flat fields). The template Worker must be written to accept the Prism client's schema, not Lens's. This is the single most important adaptation.

**Primary recommendation:** Build the template Worker fresh from the Lens Worker's structure but with the Prism client's payload schema. All other components (registries.json, caching, CLI subcommands, wizard) use Python stdlib patterns already established in the codebase.

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions
- **D-01:** Registry configuration lives in a separate `~/.prism/registries.json` file, not inline in `config.json`. Clean separation.
- **D-02:** The existing `config.json` `registry_url` field becomes a migration path -- if `registries.json` doesn't exist but `config.json` has `registry_url`, auto-migrate it as the `"default"` registry entry on first access.
- **D-03:** Fetch `skill-registry.json` from every configured registry, merge into one deduplicated list, tag each result with `[registry-name]`. Queries always search everything.
- **D-04:** One cache file per registry at `~/.prism/cache/{registry-name}.json` with filesystem mtime-based TTL check (24h). Each registry cached independently.
- **D-05:** `prism registry default` affects writes only -- reads always merge all configured registries.
- **D-06:** `/publish-skills` resolves the target registry (default or `--registry NAME`), checks writable flag, diffs against `.published.json` entry for that specific registry name.
- **D-07:** `.published.json` already supports per-registry keys from Phase 3 (uses `"default"` key). Phase 4 extends this to use actual registry names as keys.
- **D-08:** `prism registry create` is a guided wizard with manual steps -- user runs wrangler deploy and wrangler secret put themselves.
- **D-09:** Template files live under `templates/registry/` in the Prism repo.
- **D-10:** Template Worker adapted from Lens Worker with improvements: updated endpoint paths (`/publish` -> `/api/skills/publish`), Wrangler v4 + latest workers-types, `User-Agent: Prism-Worker`, service name `prism-registry`.
- **D-11:** Tokens managed Worker-side (stored in `REGISTRY_TOKENS` secret as comma-separated list). `prism registry token create` generates locally, instructs user to add to Worker secret. Local storage in `registries.json`.

### Claude's Discretion
- `registries.json` schema structure (required fields per registry entry)
- Exact wizard step text and verification checks
- Cache directory creation and cleanup strategy
- How `prism registry list` formats output
- Token generation algorithm (random hex, UUID, etc.)
- Migration logic trigger and one-time flag
- How `prism registry remove` handles removing the default registry

### Deferred Ideas (OUT OF SCOPE)
None -- discussion stayed within phase scope.
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| REG-01 | `prism registry create` -- interactive flow: create GitHub repo from template, deploy Worker, generate tokens, configure local Prism | Wizard pattern research, `gh repo create --template` usage, wrangler deploy/secret workflow |
| REG-02 | `prism registry add NAME --url URL [--token T] [--read-only]` -- add registry to config | registries.json schema design, config.py extension patterns |
| REG-03 | `prism registry remove NAME` -- remove registry from config | Default-removal edge case handling |
| REG-04 | `prism registry list` -- show configured registries | Output formatting patterns |
| REG-05 | `prism registry default NAME` -- set default push target | Write-only default semantics |
| REG-06 | `prism registry token create NAME` -- generate new API token | Token generation (secrets.token_hex), Worker secret update instructions |
| REG-07 | `prism registry token revoke NAME TOKEN` -- revoke an API token | Worker-side token management instructions |
| REG-08 | Registry template bundled in tool repo | Template directory structure, Lens Worker adaptation, CI workflow adaptation |
| REG-09 | Multi-registry reads: merge skill-registry.json from all sources, cache with 24h TTL | urllib.request fetch patterns, mtime-based caching, merge/dedup strategy |
| REG-10 | Multi-registry writes: publish delta tracked per registry via .published.json | Per-registry key extension of existing .published.json structure |
| REG-11 | Query results tagged with source registry | Tag injection in advise-skills and audit-code SKILL.md updates |
| REG-12 | /publish-skills resolves target registry, checks writable, diffs per-registry | Multi-registry resolution in SKILL.md, registries.json lookup |
</phase_requirements>

## Standard Stack

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| Python stdlib (json, pathlib, os, time, hashlib, secrets, urllib.request) | 3.12+ | All registry management CLI code | Zero-dependency constraint. Already used throughout codebase. [VERIFIED: codebase inspection] |
| TypeScript | 5.x | Template Worker source | Cloudflare Workers runtime is TS-native. [VERIFIED: Lens Worker is TypeScript] |
| Wrangler | ^4.82.2 | Worker dev/deploy in template | Latest v4 mainline. [VERIFIED: npm registry -- 4.82.2 current] |
| @cloudflare/workers-types | ^4.20260414.1 | TS type definitions in template | Date-stamped, auto-generated. [VERIFIED: npm registry] |

### Supporting
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| `gh` CLI | 2.87+ | `prism registry create` wizard (repo creation) | Only during registry creation. Runtime dependency for that one command. [VERIFIED: installed at 2.87.3] |
| `secrets` (Python stdlib) | 3.12+ | Token generation (`secrets.token_hex(32)`) | `prism registry token create` only. Cryptographically secure. [VERIFIED: stdlib since Python 3.6] |
| `jsonschema` (Python) | CI-only | Skill validation in template CI workflows | Never a runtime dependency. `pip install jsonschema` in GitHub Actions only. [VERIFIED: Lens CI pattern] |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| `secrets.token_hex(32)` | `uuid.uuid4()` | Both stdlib. `secrets` is purpose-built for tokens (cryptographic RNG). Prefer `secrets`. |
| `os.path.getmtime()` for cache TTL | Custom timestamp in JSON | mtime is simpler, no parsing needed, D-04 locks this choice. |
| `urllib.request` for HTTP | `requests` library | `requests` is external dependency. `urllib.request` is stdlib and already used in publish-skills. |

**Installation (template only):**
```bash
cd templates/registry/worker && npm install
```

## Architecture Patterns

### Recommended Project Structure
```
templates/registry/
  worker/
    src/index.ts          # Adapted Prism Worker
    wrangler.toml         # Template config (placeholders)
    package.json          # wrangler ^4.82, workers-types
    tsconfig.json         # TypeScript config
  ci/
    validate-pr.yml       # GitHub Actions workflow
    build-registry.yml    # GitHub Actions workflow
  scripts/
    validate.py           # Skill validation (CI-only)
    build_registry.py     # Registry index builder
  schemas/
    plugin.schema.json    # Skill validation schema
  README.md               # Setup instructions for registry admins

lib/registry.py           # NEW: registry management functions
lib/commands.py           # EXTEND: add cmd_registry_* functions
lib/cli.py                # EXTEND: add registry subcommand group
```

### Pattern 1: Nested Argparse Subcommands
**What:** `prism registry` needs sub-subcommands (create, add, remove, list, default, token).
**When to use:** CLI with command groups.
**Example:**
```python
# Source: existing lib/cli.py argparse pattern [VERIFIED: codebase]
registry_parser = subparsers.add_parser("registry", help="Manage registries")
registry_sub = registry_parser.add_subparsers(dest="registry_command")

add_parser = registry_sub.add_parser("add", help="Add a registry")
add_parser.add_argument("name", help="Registry name")
add_parser.add_argument("--url", required=True, help="Registry Worker URL")
add_parser.add_argument("--token", help="API token")
add_parser.add_argument("--read-only", action="store_true")

# Token has its own sub-subcommands
token_parser = registry_sub.add_parser("token", help="Manage API tokens")
token_sub = token_parser.add_subparsers(dest="token_command")
token_create = token_sub.add_parser("create", help="Generate new token")
token_create.add_argument("name", help="Registry name")
```

### Pattern 2: registries.json Schema
**What:** Dedicated config file for multi-registry state.
**Example:**
```json
{
  "registries": [
    {
      "name": "team",
      "url": "https://acme-prism.workers.dev",
      "token": "prism_abc123...",
      "writable": true
    },
    {
      "name": "community",
      "url": "https://community-prism.workers.dev",
      "token": "prism_def456...",
      "writable": false
    }
  ],
  "default": "team"
}
```
[ASSUMED] -- schema details are Claude's discretion per CONTEXT.md. Token stored directly (not env var reference) since D-11 says "local Prism stores the token per-registry in registries.json". This differs from Phase 3's REGISTRY_TOKEN env var approach -- the env var pattern remains as fallback for backward compatibility.

### Pattern 3: mtime-based Cache with TTL
**What:** Cache registry fetches using filesystem mtime.
**Example:**
```python
# Source: Python stdlib patterns [VERIFIED: os.path.getmtime docs]
import os, time, json, urllib.request

CACHE_DIR = PRISM_HOME / "cache"
CACHE_TTL = 86400  # 24 hours in seconds

def get_cached_registry(name: str, url: str, token: str) -> dict:
    cache_path = CACHE_DIR / f"{name}.json"
    if cache_path.exists():
        age = time.time() - os.path.getmtime(str(cache_path))
        if age < CACHE_TTL:
            with open(cache_path) as f:
                return json.load(f)
    # Fetch fresh
    req = urllib.request.Request(
        f"{url}/registry",
        headers={"Authorization": f"Bearer {token}", "User-Agent": "Prism/1.0"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode())
    # Write cache atomically
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = str(cache_path) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.rename(tmp, str(cache_path))
    return data
```

### Pattern 4: Multi-Registry Merge with Source Tagging
**What:** Fetch from all registries, merge skills, tag with source.
**Example:**
```python
def fetch_all_registries() -> list:
    registries = load_registries()
    all_skills = []
    for reg in registries.get("registries", []):
        try:
            data = get_cached_registry(reg["name"], reg["url"], reg.get("token", ""))
            for skill in data.get("skills", []):
                skill["_registry"] = reg["name"]
                all_skills.append(skill)
        except Exception:
            continue  # Skip unreachable registries
    return all_skills
```

### Pattern 5: Guided Wizard (prism registry create)
**What:** Step-by-step interactive flow with manual verification between steps.
**Flow:**
1. Ask user for registry name and GitHub org/repo
2. Run `gh repo create {org}/{repo} --template {template_repo} --private --clone`
3. Print instructions: "cd into repo, run `npm install && wrangler deploy`"
4. Wait for user confirmation
5. Generate token: `secrets.token_hex(32)`
6. Print instructions: "Run `wrangler secret put REGISTRY_TOKENS` and paste: {token}"
7. Print instructions: "Run `wrangler secret put GH_TOKEN` and paste your GitHub PAT"
8. Auto-run: `prism registry add {name} --url https://{name}.workers.dev --token {token}`
9. Print summary

**Key insight for D-08:** The wizard does NOT automate wrangler deploy or wrangler secret. It generates instructions. This avoids requiring wrangler on the machine running `prism registry create` (though it is needed on the machine where the Worker is deployed, which may be the same or different).

### Anti-Patterns to Avoid
- **Storing tokens in config.json:** Tokens go in `registries.json` only (D-01, D-11). `config.json` stays personal settings only.
- **Fetching registries sequentially with no timeout:** Each `urllib.request.urlopen` call must have a timeout (15s recommended). One unreachable registry must not block all queries.
- **Caching all registries in one file:** D-04 mandates one cache file per registry for independent invalidation.
- **Publishing to all writable registries:** D-05/D-06 say publish goes to ONE target (default or --registry NAME), not broadcast.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Cryptographic token generation | Random string with `random.choice` | `secrets.token_hex(32)` | `random` is not cryptographically secure. `secrets` uses OS entropy. [VERIFIED: Python docs] |
| HTTP requests | Custom socket handling | `urllib.request.Request` + `urlopen` | Already used in publish-skills. Handles redirects, headers, SSL. [VERIFIED: codebase] |
| JSON Schema validation | Custom field checks in Worker | Existing `validatePublishRequest` + CI `validate.py` | Lens already has comprehensive validation. Adapt, don't rewrite. [VERIFIED: Lens source] |
| Atomic file writes | `open(path, 'w')` directly | temp file + `os.rename()` | Established codebase pattern. Prevents corruption on crash. [VERIFIED: codebase] |
| GitHub repo operations | Raw GitHub API calls | `gh repo create --template` | `gh` CLI handles auth, pagination, error handling. [VERIFIED: gh --help] |

## Common Pitfalls

### Pitfall 1: Payload Schema Mismatch Between Client and Worker
**What goes wrong:** The Lens Worker expects `{skills: [{name, skill_md, plugin_json}]}` on `POST /publish`. The Prism client (Phase 3's `/publish-skills`) sends `{skills: [{name, description, author, repository, category, source, commit_date, source_hash, content}]}` to `POST /api/skills/publish`. If the template Worker copies Lens's validation verbatim, it will reject all Prism publishes.
**Why it happens:** Lens uses `plugin_json` as a raw JSON string and `skill_md` as content. Prism flattens plugin fields and uses `content` for SKILL.md.
**How to avoid:** The template Worker must accept the Prism client's schema. Rewrite `validatePublishRequest()` and the file-building logic to match. The Worker should reconstruct `plugin.json` from the flat fields and use `content` as `SKILL.md`.
**Warning signs:** 400 errors from Worker on publish, "missing 'plugin_json'" validation errors.
[VERIFIED: compared Lens Worker index.ts lines 194-267 with publish-skills/SKILL.md lines 196-226]

### Pitfall 2: Registry Endpoint Path Mismatch
**What goes wrong:** Lens Worker routes are `/registry`, `/skills/:name`, `/publish`. Prism client expects `/api/skills/registry`, `/api/skills/publish`. If template Worker keeps Lens routes, client gets 404s.
**Why it happens:** Phase 3 chose `/api/skills/*` prefix for cleaner API namespacing.
**How to avoid:** Template Worker must use `/api/skills/registry` (GET), `/api/skills/publish` (POST), and keep `/registry` as an alias for backward compatibility. Also update `/skills/:name` to `/api/skills/:name`.
**Warning signs:** 404 responses from Worker API.
[VERIFIED: Lens Worker routes vs Prism client URLs in codebase grep]

### Pitfall 3: Migration Race Condition
**What goes wrong:** If `registries.json` doesn't exist and `config.json` has `registry_url`, the migration creates `registries.json`. But if two processes (e.g., a command and a background hook) both detect the migration condition simultaneously, they could write conflicting files.
**Why it happens:** Multiple Prism processes can run concurrently (CLI command + hook background process).
**How to avoid:** Use atomic write (temp file + rename) for the migration. The second writer's rename atomically replaces the first -- both write the same content (same source `registry_url`), so the race is benign. Add a comment documenting this.
**Warning signs:** Corrupt or truncated `registries.json`.

### Pitfall 4: Cache Directory Not Created
**What goes wrong:** `get_cached_registry()` tries to write to `~/.prism/cache/` which doesn't exist on fresh installs.
**Why it happens:** `install.sh` and `init_prism_home()` don't create the `cache/` directory.
**How to avoid:** `mkdir -p` (Python: `CACHE_DIR.mkdir(parents=True, exist_ok=True)`) before every cache write. Also add to `install.sh`.
**Warning signs:** `FileNotFoundError` on first registry query.

### Pitfall 5: Token Storage Security
**What goes wrong:** Tokens stored in `registries.json` are readable by any process with user permissions.
**Why it happens:** Flat file storage with no encryption.
**How to avoid:** This is acceptable per the project constraints (token-based auth, no OAuth/SSO). File permissions should be `0600`. Document that tokens are stored in plaintext. Users who need more security can use environment variables as override.
**Warning signs:** Tokens visible in `prism registry list` output -- mask them (show first 8 chars + `...`).

### Pitfall 6: urllib.request SSL on macOS
**What goes wrong:** Python's `urllib.request` may fail with SSL certificate errors on macOS if the system Python (3.9.6) is used without `certifi`.
**Why it happens:** macOS system Python may not have up-to-date CA certificates.
**How to avoid:** The target is Python 3.12+, which bundles `certifi`. If users are on system Python 3.9, they may hit this. Document Python 3.12+ as the minimum in error messages.
**Warning signs:** `ssl.SSLCertVerificationError` on registry fetch.
[ASSUMED] -- based on known macOS Python SSL issues.

## Code Examples

### Template Worker Adapted Routes
```typescript
// Source: Adapted from Lens Worker [VERIFIED: /Users/gaurav/codes/Lens/cloudfare_worker/src/index.ts]
// Key changes: endpoint paths, payload schema, service name

// GET /registry OR /api/skills/registry -- returns skill-registry.json
if ((path === "/registry" || path === "/api/skills/registry") && request.method === "GET") {
  const resp = await fetchFromGitHub(env, "skill-registry.json");
  if (!resp.ok) return json({ error: "Failed to fetch registry" }, resp.status);
  const body = await resp.text();
  return new Response(body, { headers: { "Content-Type": "application/json" } });
}

// POST /api/skills/publish -- accept Prism client payload
if (path === "/api/skills/publish" && request.method === "POST") {
  // ... validate Prism-format payload, reconstruct plugin.json + SKILL.md
}
```

### Template Worker Prism-Format Validation
```typescript
// Source: New code based on Prism client payload schema [VERIFIED: publish-skills/SKILL.md]
interface PrismSkillPayload {
  name: string;
  description: string;
  author: string;
  repository: string;
  category: string[];
  source: string;
  commit_date: string;
  source_hash: string | null;
  content: string;  // SKILL.md content
}

function validatePrismPublish(body: any): { ok: true; data: PrismPublishRequest } | { ok: false; error: string } {
  if (!body || typeof body !== "object") return { ok: false, error: "Request body must be JSON" };
  const skills = body.skills;
  if (!Array.isArray(skills) || skills.length === 0) return { ok: false, error: "'skills' must be non-empty array" };

  for (let i = 0; i < skills.length; i++) {
    const s = skills[i];
    if (!s.name || !/^[a-z0-9][a-z0-9-]*[a-z0-9]$/.test(s.name)) return { ok: false, error: `skills[${i}]: invalid name` };
    if (!s.content || s.content.length < 50) return { ok: false, error: `skills[${i}]: content too short` };
    if (!s.repository) return { ok: false, error: `skills[${i}]: missing repository` };
    if (!s.description) return { ok: false, error: `skills[${i}]: missing description` };
    if (!s.author) return { ok: false, error: `skills[${i}]: missing author` };
  }

  // All skills must share same repository
  const repos = new Set(skills.map((s: any) => s.repository));
  if (repos.size > 1) return { ok: false, error: `All skills must have same repository` };

  return { ok: true, data: { repository: [...repos][0], skills, description: body.description || "" } };
}
```

### Template Worker File Building (Prism Format)
```typescript
// Source: Adapted from Lens Worker createPullRequest [VERIFIED: Lens index.ts lines 78-170]
// Key change: reconstruct plugin.json from flat fields, use 'content' as SKILL.md
const files: { path: string; content: string }[] = [];
for (const skill of skills) {
  const dir = `skills/${skill.repository}/${skill.name}`;
  // Reconstruct plugin.json from flat fields
  const pluginJson = JSON.stringify({
    name: skill.name,
    description: skill.description,
    author: skill.author,
    repository: skill.repository,
    category: skill.category,
    source: skill.source,
    commit_date: skill.commit_date,
    source_hash: skill.source_hash,
  }, null, 2);
  files.push({ path: `${dir}/plugin.json`, content: pluginJson });
  files.push({ path: `${dir}/SKILL.md`, content: skill.content });
}
```

### Loading and Migrating registries.json
```python
# Source: Extending existing lib/config.py patterns [VERIFIED: codebase]
import json, os, time
from pathlib import Path
from .config import PRISM_HOME, get_config

REGISTRIES_PATH = PRISM_HOME / "registries.json"

def load_registries() -> dict:
    """Load registries.json, auto-migrating from config.json if needed."""
    if REGISTRIES_PATH.exists():
        try:
            with open(REGISTRIES_PATH) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {"registries": [], "default": None}

    # Migration: config.json registry_url -> registries.json
    config = get_config()
    registry_url = config.get("registry_url", "")
    if registry_url:
        registries = {
            "registries": [{
                "name": "default",
                "url": registry_url,
                "token": "",  # User must set via prism registry add --token
                "writable": True,
            }],
            "default": "default",
        }
        save_registries(registries)
        return registries

    return {"registries": [], "default": None}

def save_registries(data: dict) -> None:
    """Atomic write of registries.json."""
    REGISTRIES_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(REGISTRIES_PATH) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.rename(tmp, str(REGISTRIES_PATH))
```

### Token Generation
```python
# Source: Python stdlib [VERIFIED: Python docs for secrets module]
import secrets

def generate_token() -> str:
    """Generate a cryptographically secure API token."""
    return "prism_" + secrets.token_hex(32)
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Lens: single registry URL in config | Prism: dedicated registries.json with multi-registry | Phase 4 | Enables team + community registries simultaneously |
| Lens: `POST /publish` with `{skill_md, plugin_json}` | Prism: `POST /api/skills/publish` with flat fields | Phase 3 client | Template Worker must match Prism's format |
| Lens: wrangler ^3.99.0 | Prism: wrangler ^4.82 | 2025 | v4 is current mainline, explicit upgrade |
| Lens: `User-Agent: Lens-Worker` | Prism: `User-Agent: Prism-Worker` | Phase 4 | Branding update |
| REGISTRY_TOKEN env var (Phase 3) | Token stored in registries.json per-registry (Phase 4) | Phase 4 | Multiple tokens for multiple registries |

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | Token stored directly in registries.json (not env var reference) based on D-11 "local Prism stores the token per-registry in registries.json" | Architecture Patterns / registries.json Schema | If token should remain env-var-only, the registries.json schema and all token-reading code changes. Medium risk -- CONTEXT.md is explicit. |
| A2 | macOS system Python 3.9 may have SSL cert issues with urllib.request | Common Pitfalls | Low risk -- Prism targets Python 3.12+ which bundles certifi. |
| A3 | `gh repo create --template` works with local template directories pushed to GitHub | Common Pitfalls | The `--template` flag requires the template to be an existing GitHub repo, not a local directory. The wizard must either (a) push the template dir to a temp repo first, or (b) use `gh repo create` without `--template` and push files manually. **This needs validation.** |

**Critical note on A3:** The `gh repo create --template` flag requires the template to be a GitHub repository (e.g., `org/prism-registry-template`), NOT a local directory. Since D-09 says "Template files live under `templates/registry/` in the Prism repo" and templates are bundled locally, the wizard has two options:
1. **Push-based:** `gh repo create {name}` (empty), then `git init && git add . && git push` the template files from `~/.prism/templates/registry/`.
2. **Template repo on GitHub:** Maintain a GitHub template repo that `gh repo create --template` can reference. This contradicts the "bundled in tool repo" decision.

**Recommendation:** Use option 1 (push-based). The wizard creates an empty repo with `gh repo create`, copies template files in, initializes git, and pushes. This keeps templates bundled locally per D-09.

## Open Questions

1. **Token backward compatibility**
   - What we know: Phase 3 uses `REGISTRY_TOKEN` env var. Phase 4 stores tokens in `registries.json`.
   - What's unclear: Should `REGISTRY_TOKEN` env var still work as fallback/override?
   - Recommendation: Yes -- check env var first (backward compat), then fall back to registries.json token. This matches the established pattern and eases migration.

2. **registries.json file permissions**
   - What we know: Tokens are stored in plaintext per D-11.
   - What's unclear: Should `save_registries()` set `chmod 0600`?
   - Recommendation: Yes -- set `os.chmod(path, 0o600)` after atomic write. Consistent with secret-handling best practices.

3. **advise-skills/audit-code endpoint path**
   - What we know: These currently fetch from `{registry_url}/api/skills/registry`. The Lens Worker serves `/registry`.
   - What's unclear: Whether the template Worker should serve both paths.
   - Recommendation: Serve both `/registry` and `/api/skills/registry` for maximum compatibility. The template Worker should alias them.

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| Python 3.12+ | All registry management code | Partial (3.9.6 system) | 3.9.6 | Works but untested on 3.9; user should install 3.12+ |
| Node.js | Template Worker development | Yes | 24.14.0 | -- |
| Wrangler | Template Worker deploy (user runs) | Yes | 4.79.0 | User installs via npm |
| gh CLI | `prism registry create` wizard | Yes | 2.87.3 | -- |
| git | Template repo initialization | Yes | -- | -- |

**Missing dependencies with no fallback:** None -- all critical tools are available.

**Missing dependencies with fallback:**
- Python 3.9.6 is below 3.12 floor but should work for stdlib-only code. The developer environment has a newer Python available via other paths.

## Security Domain

### Applicable ASVS Categories

| ASVS Category | Applies | Standard Control |
|---------------|---------|-----------------|
| V2 Authentication | Yes | Bearer token auth on Worker API. Tokens validated against REGISTRY_TOKENS secret. |
| V3 Session Management | No | Stateless API -- no sessions. |
| V4 Access Control | Yes | `writable` flag on registries. Token scope is all-or-nothing per Worker. |
| V5 Input Validation | Yes | Worker-side payload validation (skill name regex, required fields, content length). |
| V6 Cryptography | Yes | `secrets.token_hex(32)` for token generation. Never `random`. |

### Known Threat Patterns for This Stack

| Pattern | STRIDE | Standard Mitigation |
|---------|--------|---------------------|
| Token leakage via config file | Information Disclosure | File permissions 0600 on registries.json. Mask in `prism registry list` output. |
| Malicious skill content injection | Tampering | Worker validates skill format. CI runs validate.py on PRs. Human review on PR merge. |
| Token replay across registries | Spoofing | Each registry has independent tokens. Compromise of one doesn't affect others. |
| Worker API abuse (no rate limiting) | Denial of Service | Cloudflare's built-in DDoS protection. Worker validates auth before any GitHub API calls. |

## Sources

### Primary (HIGH confidence)
- Lens Worker source (`/Users/gaurav/codes/Lens/cloudfare_worker/src/index.ts`) -- complete Worker implementation, 400 lines
- Lens CI workflows (`/Users/gaurav/codes/Lens/.github/workflows/`) -- validate-pr.yml, build-registry.yml
- Lens scripts (`/Users/gaurav/codes/Lens/scripts/`) -- validate.py, build_registry.py
- Lens schema (`/Users/gaurav/codes/Lens/schemas/plugin.schema.json`) -- skill validation schema
- Prism codebase (`lib/config.py`, `lib/commands.py`, `lib/cli.py`) -- existing patterns
- Prism skills (`skills/publish-skills/SKILL.md`, `skills/advise-skills/SKILL.md`, `skills/audit-code/SKILL.md`) -- client-side API expectations
- npm registry: wrangler 4.82.2, @cloudflare/workers-types 4.20260414.1 -- current versions

### Secondary (MEDIUM confidence)
- `gh repo create --help` -- template flag requires GitHub repo, not local dir
- Python `secrets` module docs -- `token_hex` is cryptographically secure

### Tertiary (LOW confidence)
- macOS Python SSL certificate issues -- known historical issue, may not apply with Python 3.12+

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH -- all libraries verified against npm registry and codebase
- Architecture: HIGH -- patterns derived from existing codebase inspection and locked decisions
- Pitfalls: HIGH -- payload mismatch verified by direct source comparison
- Template Worker adaptation: HIGH -- exact diff between Lens and Prism schemas documented

**Research date:** 2026-04-14
**Valid until:** 2026-05-14 (stable domain, 30 days)
