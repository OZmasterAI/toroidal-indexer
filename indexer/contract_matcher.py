"""Contract matching engine: match providers to consumers across projects.

Pass 1: exact match on normalized contract_id (skip same-project).
Pass 2: wildcard match for scoped packages (e.g. @torus/* matches @torus/utils).
Pass 3: on-chain fuzzy match — tokenize names and match on significant overlap.
"""

import fnmatch
import re

_CAMEL_RE = re.compile(r"[A-Z][a-z]+|[A-Z]+(?=[A-Z][a-z]|\d|\b)|[a-z]+|[A-Z]+|\d+")


def _tokenize_name(name):
    """Split a name into lowercase tokens for fuzzy matching.

    'getDistributorClient' -> {'get', 'distributor', 'client'}
    'TRSDistributor' -> {'trs', 'distributor'}
    """
    return {t.lower() for t in _CAMEL_RE.findall(name) if len(t) >= 3}


def build_provider_index(contracts):
    """Build lookup from normalized contract_id to list of provider contracts."""
    index = {}
    for c in contracts:
        cid = c["contract_id"]
        index.setdefault(cid, []).append(c)
    return index


def match_contracts(providers, consumers):
    """Match consumers to providers across projects. No self-matches.

    Returns list of link dicts with consumer, provider, match_type, confidence, contract_id.
    """
    provider_index = build_provider_index(providers)
    links = []

    for consumer in consumers:
        cid = consumer["contract_id"]
        consumer_project = consumer["project"]

        # Pass 1: exact match
        for provider in provider_index.get(cid, []):
            if provider["project"] == consumer_project:
                continue
            links.append(
                {
                    "consumer": consumer,
                    "provider": provider,
                    "match_type": "exact",
                    "confidence": min(
                        consumer.get("confidence", 1.0), provider.get("confidence", 1.0)
                    ),
                    "contract_id": cid,
                }
            )

        # Pass 2: wildcard match (scoped packages like @scope/*)
        if "::" in cid:
            prefix, name = cid.split("::", 1)
            for provider_cid, provider_list in provider_index.items():
                if provider_cid == cid:
                    continue
                if "::" not in provider_cid:
                    continue
                p_prefix, p_name = provider_cid.split("::", 1)
                if p_prefix != prefix:
                    continue
                if fnmatch.fnmatch(p_name, name) or fnmatch.fnmatch(name, p_name):
                    for provider in provider_list:
                        if provider["project"] == consumer_project:
                            continue
                        already = any(
                            l["consumer"] is consumer and l["provider"] is provider
                            for l in links
                        )
                        if not already:
                            links.append(
                                {
                                    "consumer": consumer,
                                    "provider": provider,
                                    "match_type": "wildcard",
                                    "confidence": min(
                                        consumer.get("confidence", 1.0),
                                        provider.get("confidence", 1.0),
                                    )
                                    * 0.8,
                                    "contract_id": provider_cid,
                                }
                            )

    # Pass 3: on-chain fuzzy match — tokenize names, match on significant overlap
    onchain_providers = [p for p in providers if p.get("contract_type") == "onchain"]
    onchain_consumers = [c for c in consumers if c.get("contract_type") == "onchain"]
    if onchain_providers and onchain_consumers:
        matched_pairs = {(id(l["consumer"]), id(l["provider"])) for l in links}
        for consumer in onchain_consumers:
            c_name = consumer["contract_id"].rsplit("::", 1)[-1]
            c_tokens = _tokenize_name(c_name)
            if not c_tokens:
                continue
            for provider in onchain_providers:
                if provider["project"] == consumer["project"]:
                    continue
                if (id(consumer), id(provider)) in matched_pairs:
                    continue
                p_name = provider["contract_id"].rsplit("::", 1)[-1]
                p_tokens = _tokenize_name(p_name)
                if not p_tokens:
                    continue
                overlap = c_tokens & p_tokens
                if not overlap:
                    continue
                score = len(overlap) / min(len(c_tokens), len(p_tokens))
                if score < 0.3:
                    continue
                links.append(
                    {
                        "consumer": consumer,
                        "provider": provider,
                        "match_type": "fuzzy",
                        "confidence": min(
                            consumer.get("confidence", 1.0),
                            provider.get("confidence", 1.0),
                        )
                        * score,
                        "contract_id": provider["contract_id"],
                    }
                )
                matched_pairs.add((id(consumer), id(provider)))

    return links
