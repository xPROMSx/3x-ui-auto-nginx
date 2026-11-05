# Static cover sites

New installations select one supplied A template for the panel domain and one B
for REALITY. Optional Vision gets a different B template. Each site is generated
once from the reviewed variants using Python secrets. This improves ordinary
public HTTP content; it is not a proven defense against DPI, active probing or
IP blocking.

Sites live under /var/www/html/cover-panel, cover-reality and optional cover-vision.
The private /etc/x-ui/cover-sites.json records selections, never credentials.
Backup v3 preserves the sites and selections; Restore and Change Domains do not
regenerate content. Existing legacy sites are not migrated automatically.
