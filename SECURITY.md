# Security policy

## Supported versions

Until the first public release, `main` is pre-release software. After release,
security fixes will target the latest published version. Older releases may be
asked to upgrade rather than receive a backport.

## Reporting a vulnerability

Please do not disclose a suspected vulnerability in a public issue, discussion,
or pull request. Use GitHub's private vulnerability reporting form:

<https://github.com/glguida/cyclo/security/advisories/new>

Private vulnerability reporting must be enabled in the repository settings
before the first public release. If the form is temporarily unavailable,
contact the maintainer through the GitHub profile without including exploit or
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
