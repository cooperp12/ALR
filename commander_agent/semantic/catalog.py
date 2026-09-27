"""Domain semantic catalog used by ALR legacy compatibility.

This is tradecraft/schema knowledge, not challenge-answer knowledge. Roles describe
what fields mean and how they should be used (scope, filtering, measurement,
extraction, temporal ordering, enrichment pivots).
"""
from __future__ import annotations

CLOUDTRAIL_FIELDS = {
    "eventSource": {
        "roles": ["service_scope"],
        "description": "AWS service that received the API request.",
        "examples": ["iam.amazonaws.com", "ec2.amazonaws.com"],
    },
    "eventName": {
        "roles": ["api_operation"],
        "description": "AWS API operation/action recorded by CloudTrail.",
        "examples": ["CreateAccessKey", "RunInstances"],
    },
    "userIdentity.accessKeyId": {
        "roles": ["actor_credential", "group_identifier"],
        "description": "Access key credential associated with the caller.",
    },
    "userIdentity.userName": {
        "roles": ["actor_identity", "group_identifier"],
        "description": "IAM username associated with the caller when present.",
    },
    "errorCode": {
        "roles": ["failure_presence", "failure_category"],
        "description": "Service error/category for a failed API request; useful for detecting failure and grouping broad error types.",
    },
    "errorMessage": {
        "roles": ["detailed_failure_description"],
        "description": "Detailed description of the failure; can distinguish materially different failed actions that share one broad error code.",
    },
    "requestParameters": {
        "roles": ["request_target_container", "request_input"],
        "description": "Structured parameters supplied to the AWS API operation, often including the requested target/resource.",
    },
    "requestParameters.imageId": {
        "roles": ["external_identifier", "ami_id"],
        "description": "AMI/image identifier supplied to an EC2 launch request when extracted.",
    },
    "userAgent": {
        "roles": ["client_identifier"],
        "description": "Client/application that originated the API request.",
    },
    "eventTime": {
        "roles": ["event_time"],
        "description": "CloudTrail event timestamp.",
    },
    "_time": {
        "roles": ["event_time"],
        "description": "Splunk event timestamp.",
    },
}

EMAIL_FIELDS = {
    "subject": {"roles": ["message_subject"], "description": "Message subject."},
    "body": {"roles": ["message_content"], "description": "Message body/content when extracted."},
    "url": {"roles": ["external_reference"], "description": "URL/reference extracted from message content."},
}

DOMAIN_CATALOGS = {
    "aws_cloudtrail": CLOUDTRAIL_FIELDS,
    "email": EMAIL_FIELDS,
}

ROLE_DESCRIPTIONS = {
    "service_scope": "Field used to constrain the service/domain of activity.",
    "api_operation": "Field naming the operation/action performed.",
    "actor_credential": "Credential identifying the caller.",
    "actor_identity": "Identity/principal associated with the caller.",
    "group_identifier": "Entity suitable for grouping/ranking.",
    "failure_presence": "Field/presence condition indicating a failed event.",
    "failure_category": "Coarse categorical error/failure type.",
    "detailed_failure_description": "Specific failure description suitable for distinguishing different errors.",
    "request_target_container": "Structured request data containing target/resource information.",
    "client_identifier": "Client/application identifier or user-agent field.",
    "external_identifier": "Identifier that normally needs external enrichment.",
    "ami_id": "AWS machine image identifier.",
    "event_time": "Timestamp used for chronological selection.",
    "external_reference": "Reference/URL that should be followed in another source.",
}


def fields_for_role(domain: str, role: str):
    catalog = DOMAIN_CATALOGS.get(str(domain or ""), {})
    return [name for name, meta in catalog.items() if role in meta.get("roles", [])]


def field_meta(domain: str, field: str):
    return dict(DOMAIN_CATALOGS.get(str(domain or ""), {}).get(str(field or ""), {}))


def compact_catalog(domain: str, roles=None):
    roles = set(roles or [])
    lines = []
    for field, meta in DOMAIN_CATALOGS.get(str(domain or ""), {}).items():
        field_roles = list(meta.get("roles", []))
        if roles and not roles.intersection(field_roles):
            continue
        lines.append(f"- {field}: roles={','.join(field_roles)}; {meta.get('description','')}")
    return "\n".join(lines)
