"""The security world's shared vocabulary: id patterns, enums, weights and data rules.

Everything an id can look like is a strict pattern from the start (payments
decision 81): ids reach service paths, so no slash, percent sign or dot-only
segment can pass. Every address and domain in the world's data must be a
documentation one: RFC 5737 IPv4 ranges and RFC 2606 names.
"""

from __future__ import annotations

import ipaddress
from enum import StrEnum

HOST_PATTERN = r"^[a-z][a-z0-9-]{0,31}$"
USER_PATTERN = r"^[a-z][a-z0-9_-]{0,31}(\.[a-z0-9_-]{1,31})?$"
ALERT_PATTERN = r"^ALR-[0-9]{3,6}$"
TICKET_PATTERN = r"^INC-[0-9]{3,6}$"
CHANGE_PATTERN = r"^CHG-[0-9]{3,6}$"
ACTION_PATTERN = r"^act_[a-z0-9]{12}$"
KEY_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,47}$"
_OCTET = r"(25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])"
IP_PATTERN = rf"^{_OCTET}(\.{_OCTET}){{3}}$"
DOMAIN_PATTERN = r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$"
SHA256_PATTERN = r"^[a-f0-9]{64}$"
ENTITY_PATTERN = r"^[a-z0-9]+([._-][a-z0-9]+)*$"
"""A host, account, IP address or domain: what ``timeline`` pivots on."""
PROCESS_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"

DOCUMENTATION_NETWORKS = tuple(
    ipaddress.ip_network(net) for net in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")
)
"""RFC 5737: TEST-NET-1, -2 and -3. The only IPv4 addresses the world's data may use."""

EXAMPLE_SUFFIXES = (".example", ".test", ".invalid", ".localhost")
EXAMPLE_DOMAINS = ("example.com", "example.net", "example.org")
"""RFC 2606: reserved top-level names and second-level example domains."""


class Severity(StrEnum):
    """An alert's or ticket's severity, lowest first."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


Criticality = Severity
"""An asset's business criticality uses the same four steps."""

ORDER = {level: rank for rank, level in enumerate(Severity)}

IMPACT_WEIGHT = {
    Severity.LOW: 1,
    Severity.MEDIUM: 2,
    Severity.HIGH: 5,
    Severity.CRITICAL: 10,
}
"""Business impact of disrupting an asset, by its criticality (decision 92)."""

DEFAULT_SLA_MINUTES = {
    Severity.CRITICAL: 15,
    Severity.HIGH: 60,
    Severity.MEDIUM: 240,
    Severity.LOW: 1_440,
}
"""Minutes from a ticket opening to escalation or closure, by severity (decision 93)."""


class Source(StrEnum):
    """The five log sources the telemetry store holds."""

    AUTH = "auth"
    PROCESS = "process"
    NETWORK = "network"
    DNS = "dns"
    CLOUD = "cloud"


class Classification(StrEnum):
    """A ticket's classification, after Microsoft Sentinel's incident classifications."""

    TRUE_POSITIVE = "true_positive"
    BENIGN_POSITIVE = "benign_positive"
    FALSE_POSITIVE = "false_positive"
    UNDETERMINED = "undetermined"


class Verdict(StrEnum):
    """What a report concludes."""

    MALICIOUS = "malicious"
    BENIGN = "benign"


class IncidentType(StrEnum):
    """What happened, in a closed list a grader can compare (benign kinds included)."""

    PHISHING = "phishing"
    MALWARE = "malware"
    PASSWORD_SPRAY = "password_spray"
    ACCOUNT_TAKEOVER = "account_takeover"
    LATERAL_MOVEMENT = "lateral_movement"
    DATA_EXFILTRATION = "data_exfiltration"
    ADMIN_ACTIVITY = "admin_activity"
    SCHEDULED_JOB = "scheduled_job"
    TRAVEL = "travel"
    SECURITY_TESTING = "security_testing"
    MISCONFIGURATION = "misconfiguration"
    BENIGN_SOFTWARE = "benign_software"


class Action(StrEnum):
    """Every response action. ``collect_evidence`` and ``restore`` disrupt nothing."""

    COLLECT_EVIDENCE = "collect_evidence"
    ISOLATE_HOST = "isolate_host"
    DISABLE_ACCOUNT = "disable_account"
    RESET_CREDENTIALS = "reset_credentials"
    BLOCK_INDICATOR = "block_indicator"
    REIMAGE_HOST = "reimage_host"
    RESTORE = "restore"


DISRUPTIVE = frozenset(
    {
        Action.ISOLATE_HOST,
        Action.DISABLE_ACCOUNT,
        Action.RESET_CREDENTIALS,
        Action.BLOCK_INDICATOR,
        Action.REIMAGE_HOST,
    }
)
LASTING = frozenset({Action.ISOLATE_HOST, Action.DISABLE_ACCOUNT, Action.BLOCK_INDICATOR})
"""Actions that stay in force until restored."""
HOST_ACTIONS = frozenset({Action.COLLECT_EVIDENCE, Action.ISOLATE_HOST, Action.REIMAGE_HOST})
ACCOUNT_ACTIONS = frozenset({Action.DISABLE_ACCOUNT, Action.RESET_CREDENTIALS})


class IndicatorKind(StrEnum):
    """What ``block_indicator`` can block."""

    IP = "ip"
    DOMAIN = "domain"
    SHA256 = "sha256"


class EntityKind(StrEnum):
    """What a root cause can be."""

    HOST = "host"
    USER = "user"
    INDICATOR = "indicator"


def is_documentation_ip(value: str) -> bool:
    """Whether an IPv4 address is in an RFC 5737 documentation range."""
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return any(address in network for network in DOCUMENTATION_NETWORKS)


def is_example_domain(value: str) -> bool:
    """Whether a domain is reserved by RFC 2606."""
    name = value.lower().rstrip(".")
    return name.endswith(EXAMPLE_SUFFIXES) or any(
        name == domain or name.endswith(f".{domain}") for domain in EXAMPLE_DOMAINS
    )


__all__ = [
    "ACCOUNT_ACTIONS",
    "ACTION_PATTERN",
    "ALERT_PATTERN",
    "CHANGE_PATTERN",
    "DEFAULT_SLA_MINUTES",
    "DISRUPTIVE",
    "DOMAIN_PATTERN",
    "ENTITY_PATTERN",
    "HOST_ACTIONS",
    "HOST_PATTERN",
    "IMPACT_WEIGHT",
    "IP_PATTERN",
    "KEY_PATTERN",
    "LASTING",
    "ORDER",
    "PROCESS_PATTERN",
    "SHA256_PATTERN",
    "TICKET_PATTERN",
    "USER_PATTERN",
    "Action",
    "Classification",
    "Criticality",
    "EntityKind",
    "IncidentType",
    "IndicatorKind",
    "Severity",
    "Source",
    "Verdict",
    "is_documentation_ip",
    "is_example_domain",
]
