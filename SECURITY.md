# Security and private data

Inkform processes local images, PDFs, and structured profile data. Its normal
workflow keeps handwriting evidence and writer profiles in the user's private
data directory. Public demonstration assets use synthetic forms.

## Reporting a vulnerability

Use the repository's **Security → Report a vulnerability** option when it is
available. If private reporting is unavailable, open an issue asking for a
private reporting channel without publishing the vulnerability details.
Do not include credentials, private handwriting, writer profiles, personal
documents, or identifying source paths in a public report.

A useful private report includes the affected version, platform, reproduction
steps, expected impact, and a small synthetic input. Please allow maintainers
time to investigate before public disclosure. Response times and a supported
release schedule are not currently guaranteed; use the latest release and
check its release notes for fixes.

## Reporting accidental disclosure

If a public issue, commit, or release appears to contain private handwriting or
personal metadata, contact maintainers through the same private channel. Link
to the location instead of copying the material into another public message.
Removing a file from the latest revision alone does not remove it from Git
history or previously downloaded packages.
