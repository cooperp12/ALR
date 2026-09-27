---
name: ami_release_resolution
description: Resolve an exact regional EC2 AMI discovered in evidence to an Ubuntu release, then map that release to its two-word codename.
version: 2
---

AMI RELEASE RESOLUTION
1. Obtain the AMI ID and AWS region from the scoped CloudTrail event.
2. Never infer the OS from the AMI identifier itself.
3. Prefer Canonical's Ubuntu EC2 image locator for exact AMI evidence.
4. If the historical AMI is absent there, search the public web using the exact AMI and region.
5. Require the enrichment evidence to contain the exact AMI plus a release series/version.
6. For release -> codename, first construct official Ubuntu release URLs from the evidenced version (release archive, cloud-image release page, and ubuntu.com release page). Fetch those pages directly before using search.
7. If direct official pages are unavailable, search only official Ubuntu domains for explicit release/codename evidence.
8. When multiple official direct pages yield a codename, require them to agree; a conflict is unresolved evidence, not a tie-break opportunity.
9. Do not keep a local release/codename answer catalogue in the runtime or skill.
10. Enforce the requested answer shape only after both external links are evidenced.
