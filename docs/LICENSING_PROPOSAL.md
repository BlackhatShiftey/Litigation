# Licensing proposal

> **Status: pending owner decision. This is a policy proposal, not an effective
> license or legal grant.** No `LICENSE`, `COPYING`, or `NOTICE` file is currently
> tracked, and GitHub reports no detected license for the public repository.

## Recommendation

Use the [Business Source License 1.1](https://mariadb.com/bsl11/) with a carefully
drafted Additional Use Grant that permits personal, research, and internal
business self-hosting, including multi-machine clusters. Require a separate
commercial license to offer Litigation itself as a hosted or managed service to
third parties. Set an Apache-2.0 Change License four years after the first public
distribution of each covered version.

This matches the current preference for source availability and hosted-service
protection while preserving a defined path to an open-source license. BUSL 1.1
is source-available, not an open-source license. If eventual conversion is not
desired, BUSL 1.1 is not a fit.

The simpler alternative is [MIT](https://choosealicense.com/licenses/mit/),
which permits commercial use, copying, modification, redistribution, and sale
subject to retaining its notice. Choose MIT if broad reuse and hosted commercial
offerings should be permitted without a separate agreement.

## Scope and activation checks

SEAM is separately licensed, is not bundled, and is optional to Litigation core.
This proposal does not relicense SEAM or any third-party component. Before
adding an effective license, the owner should confirm:

- the copyright holder and authority over every contribution;
- the dependency, vendored-code, asset, and generated-file license inventory;
- compatibility of the final BUSL parameters and Apache-2.0 conversion;
- exact Additional Use Grant language, including the hosted/managed-service
  boundary, affiliates, contractors, evaluation, and redistribution;
- per-version change-date metadata and the commercial licensing contact.

Do not add a `LICENSE` file or declare a repository license until the owner
chooses the business restriction or permissive MIT and approves the exact text.
