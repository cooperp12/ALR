---
name: botsv3_environment
description: Preload the stable BOTSv3 index, historical timeframe, and common sourcetypes so investigations do not waste tool calls rediscovering them.
version: 1
---

BOTSv3 ENVIRONMENT
- Use index=botsv3 for this training dataset.
- BOTSv3 activity relevant to this lab is historical, centred on August 2018.
- Common sourcetypes and uses:
    aws:cloudtrail
        AWS API calls, IAM activity, EC2 operations
    stream:smtp
        SMTP/email content
    ms:o365:reporting:messagetrace
        O365 message trace / email metadata
    WinEventLog:Security
        Windows authentication and security events
    XmlWinEventLog:Microsoft-Windows-Sysmon/Operational
        Endpoint process/file telemetry
    suricata
        Network/security telemetry
    stream:tcp
        TCP/network telemetry
    stream:http
        HTTP traffic
    stream:dns
        DNS lookups
    code42:security
        File exposure/security monitoring
    osquery:results
        Host/IR telemetry
- Choose the sourcetype from the investigation concept rather than probing every
  sourcetype first.
- For CloudTrail, errorCode is important for separating failures from successes.
- Use stats/timechart/correlation only when they answer the question; do not add
  expensive commands merely because they are available.
