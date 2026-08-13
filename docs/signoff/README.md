# Sign-off packs

Generated, not written: `make signoff` reads the running system and exports what a reviewer
needs to decide whether the public bot may answer — the externally visible corpus with its
approvals, the conduct prompt with its hash, the surface configuration, each control with the
file that implements it, and the red-team and isolation evidence.

The pack in this directory was generated against the **seeded fixture corpus**, so it is an
example of the artefact and not a statement about production. It carries one finding by
design: the seed script inserts documents directly rather than through the publish path, so
their approval trail is empty — which is precisely the check that would catch a real document
published without one.

Regenerate before any sign-off meeting; the pack is dated and hashed for that reason.
