---
name: aws_cloudtrail
description: Investigate AWS API activity using the correct CloudTrail sourcetype and real CloudTrail fields.
version: 2
---

AWS CLOUDTRAIL
- For AWS API activity, start with sourcetype=aws:cloudtrail.
- Useful real fields include:
    eventTime
    eventSource
    eventName
    errorCode
    errorMessage
    userAgent
    sourceIPAddress
    awsRegion
    userIdentity.accessKeyId
    userIdentity.userName
    requestParameters
    responseElements
- Do not invent generic aliases such as access_key, event_type, or error_message
  unless you explicitly create them.
- Keep access-key, eventName/action, error state, and the requested output field
  tied to the SAME event when answering.
- Use spath if a nested field is not already extracted.


CURRENT QUERY CONTRACT
- When this skill is active for the primary DO phase, AWS searches are required to target `sourcetype=aws:cloudtrail`.
- Combine this skill with reducing skills such as `spl_distinct_count` rather than dumping broad raw CloudTrail events.
