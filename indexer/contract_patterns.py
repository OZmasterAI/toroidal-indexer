"""Data-driven contract pattern registry for cross-repo contract detection.

Adding a new language/framework = adding pattern dicts to CONTRACT_PATTERNS.
No new extractor functions needed.
"""

import re

CONTRACT_PATTERNS = {
    "http": {
        "normalize": "http",
        "patterns": [
            # ── HTTP Providers ──
            # Next.js App Router: app/api/**/route.ts with GET/POST/PUT/DELETE/PATCH exports
            {
                "name": "nextjs_app_router",
                "file_re": r"app/api/(.+)/route\.(ts|js)$",
                "name_re": r"^(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)$",
                "role": "provider",
                "confidence": 0.95,
                "contract_id_template": "http::{name}::/api/{path}",
            },
            # Next.js Pages Router: pages/api/**/*.ts
            {
                "name": "nextjs_pages_router",
                "file_re": r"pages/api/(.+)\.(ts|js)$",
                "name_re": r"^(default|handler)$",
                "role": "provider",
                "confidence": 0.85,
                "contract_id_template": "http::ANY::/api/{path}",
            },
            # Express/Koa/Fastify: router.get/post/put/delete or app.get etc.
            {
                "name": "express_router",
                "file_re": r"(routes?|controllers?|api)/.*\.(ts|js|mjs)$",
                "name_re": r"^(get|post|put|delete|patch|router|handle)",
                "callee_re": r"(router|app)\.(get|post|put|delete|patch|all|use)",
                "role": "provider",
                "confidence": 0.8,
                "contract_id_template": "http::ANY::{file_path}",
            },
            # Flask/FastAPI decorators: @app.route, @router.get, etc.
            {
                "name": "flask_fastapi",
                "file_re": r"(routes?|views?|api|endpoints?)/.*\.py$",
                "name_re": r".",
                "source_re": r"@(app|router|blueprint)\.(route|get|post|put|delete|patch)\s*\(\s*[\"']([^\"']+)[\"']",
                "role": "provider",
                "confidence": 0.9,
                "contract_id_template": "http::{method}::{route_path}",
            },
            # Actix-web / Axum: #[get("/path")], #[post("/path")]
            {
                "name": "actix_axum",
                "file_re": r"(routes?|handlers?|api)/.*\.rs$",
                "name_re": r".",
                "source_re": r"#\[(get|post|put|delete|patch)\s*\(\s*\"([^\"]+)\"",
                "role": "provider",
                "confidence": 0.9,
                "contract_id_template": "http::{method}::{route_path}",
            },
            # Go net/http, Gin, Chi, Echo: http.HandleFunc("/path", handler)
            {
                "name": "go_http",
                "file_re": r"(routes?|handlers?|api|server)/.*\.go$",
                "name_re": r"(Handler|Handle|Serve|Route)",
                "callee_re": r"(HandleFunc|Handle|GET|POST|PUT|DELETE|Group)",
                "role": "provider",
                "confidence": 0.8,
                "contract_id_template": "http::ANY::{file_path}",
            },
            # Spring Boot: @GetMapping, @PostMapping, @RequestMapping
            {
                "name": "spring_boot",
                "file_re": r"(controller|rest|api)/.*\.java$",
                "name_re": r".",
                "source_re": r"@(Get|Post|Put|Delete|Patch|Request)Mapping\s*\(\s*(?:value\s*=\s*)?[\"']([^\"']+)[\"']",
                "role": "provider",
                "confidence": 0.9,
                "contract_id_template": "http::{method}::{route_path}",
            },
            # ASP.NET: [HttpGet], [HttpPost], [Route("api/...")]
            {
                "name": "aspnet",
                "file_re": r"(Controllers?|Api)/.*\.cs$",
                "name_re": r".",
                "source_re": r"\[Http(Get|Post|Put|Delete|Patch)\s*\(\s*\"?([^\")\]]*)",
                "role": "provider",
                "confidence": 0.85,
                "contract_id_template": "http::{method}::{route_path}",
            },
            # Rails: routes detected from controller naming convention
            {
                "name": "rails_controller",
                "file_re": r"app/controllers/(.+)_controller\.rb$",
                "name_re": r"^(index|show|create|update|destroy|new|edit)$",
                "role": "provider",
                "confidence": 0.8,
                "contract_id_template": "http::ANY::/api/{path}",
            },
            # Laravel: Route::get/post in routes/ or controller methods
            {
                "name": "laravel",
                "file_re": r"(routes|Controllers)/.*\.php$",
                "name_re": r"^(index|show|store|update|destroy)$",
                "role": "provider",
                "confidence": 0.75,
                "contract_id_template": "http::ANY::/api/{path}",
            },
            # ── HTTP Consumers ──
            # fetch/axios/requests/httpx/reqwest/http.Get consumers
            {
                "name": "http_consumer_callee",
                "file_re": r"\.(ts|tsx|js|jsx|mjs|py|rs|go)$",
                "name_re": r".",
                "callee_re": r"^(fetch|axios\.(get|post|put|delete|patch|request)|requests\.(get|post|put|delete|patch)|httpx\.(get|post|put|delete|patch|request)|reqwest|http\.(Get|Post|NewRequest)|HttpClient|Alamofire|dio\.(get|post)|Faraday|Tesla|guzzle|urllib)$",
                "role": "consumer",
                "confidence": 0.7,
                "contract_id_template": "http::ANY::{name}",
            },
        ],
    },
    "lib": {
        "normalize": "lib",
        "patterns": [
            # ── Lib manifests (parsed at file level, not per-node) ──
            {
                "name": "npm_package",
                "manifest": "package.json",
                "field": "dependencies",
                "dev_field": "devDependencies",
                "role": "consumer",
                "provider_field": "name",
                "confidence": 1.0,
            },
            {
                "name": "cargo_toml",
                "manifest": "Cargo.toml",
                "field": "dependencies",
                "dev_field": "dev-dependencies",
                "role": "consumer",
                "provider_field": "package.name",
                "confidence": 1.0,
            },
            {
                "name": "pyproject_toml",
                "manifest": "pyproject.toml",
                "field": "project.dependencies",
                "dev_field": "project.optional-dependencies",
                "role": "consumer",
                "provider_field": "project.name",
                "confidence": 1.0,
            },
            {
                "name": "go_mod",
                "manifest": "go.mod",
                "field": "require",
                "role": "consumer",
                "provider_field": "module",
                "confidence": 1.0,
            },
            {
                "name": "build_gradle",
                "manifest": "build.gradle",
                "field": "dependencies",
                "role": "consumer",
                "provider_field": "group",
                "confidence": 0.9,
            },
            {
                "name": "composer_json",
                "manifest": "composer.json",
                "field": "require",
                "dev_field": "require-dev",
                "role": "consumer",
                "provider_field": "name",
                "confidence": 1.0,
            },
            {
                "name": "gemfile",
                "manifest": "Gemfile",
                "field": "gems",
                "role": "consumer",
                "provider_field": "name",
                "confidence": 0.9,
            },
        ],
    },
    "topic": {
        "normalize": "topic",
        "patterns": [
            # Pub/sub producers — require real framework callees, not bare verbs
            {
                "name": "topic_producer",
                "file_re": r"\.(ts|js|py|rs|go|java)$",
                "file_exclude_re": r"(test|spec|__test__|_test)\.",
                "name_re": r".",
                "callee_re": r"(kafka\.(publish|send|produce)|nats\.(publish|request)|amqp\.(publish|sendToQueue)|bull\.(add|process)|sqs\.(sendMessage|send)|sns\.publish|pubsub\.(publish|topic)|redis\.(publish|xadd)|EventEmitter\.(emit|send)|emitter\.(emit|send)|rabbitMQ\.|eventBridge\.|kinesis\.put)",
                "role": "provider",
                "confidence": 0.7,
                "contract_id_template": "topic::{name}",
            },
            # Pub/sub consumers — require real framework callees
            {
                "name": "topic_consumer",
                "file_re": r"\.(ts|js|py|rs|go|java)$",
                "file_exclude_re": r"(test|spec|__test__|_test)\.",
                "name_re": r".",
                "callee_re": r"(kafka\.(subscribe|consume|on)|nats\.(subscribe|on)|amqp\.(consume|assertQueue)|bull\.(process|on)|sqs\.(receiveMessage|receive)|sns\.subscribe|pubsub\.(subscribe|subscription)|redis\.(subscribe|xread)|EventEmitter\.on|emitter\.on|rabbitMQ\.|eventBridge\.|kinesis\.get)",
                "role": "consumer",
                "confidence": 0.7,
                "contract_id_template": "topic::{name}",
            },
        ],
    },
    "grpc": {
        "normalize": "grpc",
        "patterns": [
            # Protobuf service definitions (provider)
            {
                "name": "proto_service",
                "file_re": r"\.proto$",
                "name_re": r".",
                "node_type_re": r"(function|class)",
                "role": "provider",
                "confidence": 0.95,
                "contract_id_template": "grpc::{name}",
            },
            # gRPC client stubs (consumer) — tightened: require gRPC-specific callees
            {
                "name": "grpc_client",
                "file_re": r"\.(ts|js|py|rs|go|java)$",
                "name_re": r"(Client|Stub|Service|Rpc|grpc|proto)",
                "callee_re": r"(grpc\.|\.grpc|ServiceClient|ServiceStub|_pb2_grpc\.|channel\.(unary|stream)|stub\.|\.connect\(|proto\.)",
                "role": "consumer",
                "confidence": 0.7,
                "contract_id_template": "grpc::{name}",
            },
            # Solidity on-chain API provider — function nodes in .sol files
            {
                "name": "solidity_onchain",
                "file_re": r"\.sol$",
                "name_re": r".",
                "node_type_re": r"function",
                "role": "provider",
                "confidence": 0.85,
                "contract_id_template": "grpc::{name}",
            },
        ],
    },
}


def load_contract_patterns():
    """Return the contract pattern registry. Extensible with project-level overrides later."""
    return CONTRACT_PATTERNS


def _extract_path_from_file(file_path, file_re_match):
    """Extract API path from a file path using the regex match groups."""
    if (
        not file_re_match
        or file_re_match.lastindex is None
        or file_re_match.lastindex < 1
    ):
        return ""
    if not file_re_match.group(1):
        return ""
    raw = file_re_match.group(1)
    # Strip file extension remnants
    raw = re.sub(r"\.(ts|js|tsx|jsx|mjs|py|rs|go|java|rb|php|cs)$", "", raw)
    # Convert directory separators to URL path
    return raw.replace("\\", "/")


def match_pattern(
    type_config, file, name, node_type="function", callees=None, source_content=None
):
    """Test a code_node against a contract type's patterns.

    Returns contract dict or None.
    """
    for pattern in type_config["patterns"]:
        # Skip manifest patterns (handled separately)
        if "manifest" in pattern:
            continue

        # File path check
        file_re = pattern.get("file_re")
        if file_re:
            file_match = re.search(file_re, file)
            if not file_match:
                continue
        else:
            file_match = None

        # File exclusion check (e.g. skip test files)
        file_exclude_re = pattern.get("file_exclude_re")
        if file_exclude_re and re.search(file_exclude_re, file):
            continue

        # Node type check
        node_type_re = pattern.get("node_type_re")
        if node_type_re and not re.search(node_type_re, node_type):
            continue

        # Name check
        name_re = pattern.get("name_re")
        if name_re and name_re != "." and not re.search(name_re, name):
            continue

        # Source content check (for decorator-based patterns)
        source_re = pattern.get("source_re")
        if source_re:
            if not source_content:
                continue
            source_match = re.search(source_re, source_content, re.IGNORECASE)
            if not source_match:
                continue
        else:
            source_match = None

        # Callee check
        callee_re = pattern.get("callee_re")
        if callee_re:
            if not callees:
                continue
            callee_matched = any(re.search(callee_re, c) for c in callees)
            if not callee_matched:
                continue

        # Build contract_id and normalize
        contract_id = _build_contract_id(pattern, file, name, file_match, source_match)
        normalize_type = type_config.get("normalize", "http")
        contract_id = normalize_contract_id(contract_id, normalize_type)

        return {
            "contract_id": contract_id,
            "role": pattern["role"],
            "confidence": pattern["confidence"],
            "pattern_name": pattern["name"],
        }

    return None


def _build_contract_id(pattern, file, name, file_match, source_match):
    """Build a contract ID from a pattern template and match data."""
    template = pattern["contract_id_template"]

    # Extract path from file match
    path = _extract_path_from_file(file, file_match) if file_match else ""

    # For decorator-based patterns, extract method and route from source
    method = "ANY"
    route_path = ""
    if source_match:
        groups = source_match.groups()
        if len(groups) >= 2:
            method_raw = groups[0].upper()
            # Normalize Spring-style mapping names
            method_map = {
                "GET": "GET",
                "POST": "POST",
                "PUT": "PUT",
                "DELETE": "DELETE",
                "PATCH": "PATCH",
                "REQUEST": "ANY",
                "ROUTE": "ANY",
            }
            method = method_map.get(method_raw, method_raw)
            route_path = groups[1] if len(groups) >= 2 else ""
        if len(groups) >= 3 and not route_path:
            route_path = groups[2]

    result = template.format(
        name=name,
        path=path,
        file_path=file,
        method=method,
        route_path=route_path,
    )
    return result


_PARAM_RE = re.compile(r"\[([^\]]+)\]|:([^/]+)|{([^}]+)}")


def normalize_contract_id(raw_id, contract_type):
    """Normalize a contract ID for consistent matching.

    HTTP: lowercase, collapse param patterns to {param}, strip trailing /
    Lib: lowercase
    Topic: lowercase
    gRPC: lowercase package/service, preserve method case
    """
    if contract_type == "http":
        parts = raw_id.split("::", 2)
        if len(parts) == 3:
            prefix, method, path = parts
            method = method.lower()
            path = _PARAM_RE.sub("{param}", path)
            path = path.lower().rstrip("/")
            if not path:
                path = "/"
            return f"{prefix}::{method}::{path}"
        return raw_id.lower()

    if contract_type in ("lib", "topic"):
        return raw_id.lower()

    if contract_type == "grpc":
        parts = raw_id.rsplit("::", 1)
        if len(parts) == 2:
            return f"{parts[0].lower()}::{parts[1]}"
        return raw_id.lower()

    return raw_id.lower()
