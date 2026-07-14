# Security policy

## Supported versions

Version 0.1.0 and later stable releases are supported until superseded by a
newer stable release. Security fixes target the latest stable version;
older releases may be asked to upgrade rather than receive a backport.

## Reporting a vulnerability

Please do not disclose a suspected vulnerability in a public issue, discussion,
or pull request. When available for the published repository, use its private
vulnerability reporting form:

<https://github.com/glguida/cyclo/security/advisories/new>

Cyclo's local release tooling never reads or changes remote repository
settings. If the private form is unavailable, contact the maintainer through a
private channel listed on the publication profile without including exploit or
credential details in a public message.

Include, when possible:

- the affected version or commit;
- impact and the security boundary involved;
- minimal reproduction steps or a proof of concept;
- relevant logs with tokens, API keys, paths, and user data removed; and
- any suggested mitigation or fix.

Use disposable test credentials and accounts. Do not test against systems or
provider accounts you do not own or have explicit permission to assess.

## Response process

The maintainer aims to acknowledge a report within three business days and
provide an initial assessment within seven business days. Fix and disclosure
timing will be coordinated with the reporter and will depend on severity,
provider coordination, and release complexity.

Reports about credential storage, scoped gateway capabilities, container or
mount isolation, filesystem queue integrity, and unintended dashboard exposure
are especially useful. Provider outages, social engineering, and attacks that
require an already-compromised Docker host are normally outside Cyclo's own
security boundary, but reports showing an unexpected amplification are still
welcome.
